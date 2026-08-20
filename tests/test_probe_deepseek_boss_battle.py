from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import probe_deepseek_boss_battle as probe

from fu_gm.http_server import FUGMHttpService
from fu_gm.components.gm_reply_grounding_verifier import (
    GMReplyGroundingVerifier,
)
from fu_gm.gm_tool_contracts import GMToolExecutionContext
from fu_gm.models import Action, ActionType


def test_default_mode_is_offline_and_has_no_seed_override() -> None:
    args = probe.parse_args([])

    assert args.live is False
    assert not hasattr(args, "seed")
    with pytest.raises(SystemExit):
        probe.parse_args(["--seed", "7"])


def test_isolated_server_uses_host_port_and_keyword_service(monkeypatch) -> None:
    service = object()
    expected_server = object()
    observed: list[tuple[object, ...]] = []

    def fake_make_server(host: str, port: int, *, service: object) -> object:
        observed.append((host, port, service))
        return expected_server

    monkeypatch.setattr(probe, "make_server", fake_make_server)

    assert probe.make_isolated_server(service) is expected_server
    assert observed == [("127.0.0.1", 0, service)]


def test_receipt_failure_report_keeps_recovery_without_hiding_terminal_errors() -> None:
    source = {"source_event": {"event_id": "event-1"}}
    response = {
        "tool_receipts": [
            {
                "tool_name": "resolve_rule_window",
                "ok": False,
                "retryable": True,
                "error_code": "CHOICES_REQUIRED",
                "result": source,
            },
            {
                "tool_name": "resolve_rule_window",
                "ok": True,
                "result": source,
            },
            {
                "tool_name": "perform_character_action",
                "ok": False,
                "retryable": False,
                "error_code": "ILLEGAL_TARGET",
                "result": source,
            },
        ]
    }

    unresolved, recovered = probe.receipt_failure_report(response)

    assert [row["error_code"] for row in unresolved] == ["ILLEGAL_TARGET"]
    assert [row["error_code"] for row in recovered] == ["CHOICES_REQUIRED"]
    assert recovered[0]["recovered_by_later_success"] is True


def test_current_skill_and_future_capability_contracts_are_separate() -> None:
    skills = probe.selected_skill_matrix()
    capabilities = probe.optional_capability_matrix([])

    assert set(skills) == {
        "防御精通",
        "双盾战士",
        "利刃风暴",
        "挺身守护",
        "集中心智",
        "知识就是力量",
        "快速评估",
        "元素魔法",
    }
    assert all(row["required"] for row in skills.values())
    assert capabilities["protector_reaction"]["required"] is False
    assert capabilities["dual_wield"]["required"] is True
    assert capabilities["minor_action"]["required"] is True
    assert capabilities["team_assist"]["required"] is True
    assert capabilities["protector_reaction"]["status"] == "covered_by_skill_matrix"
    assert capabilities["dual_wield"]["status"] == "planned"


def test_front_setup_sequence_is_minor_then_assist_then_bladestorm() -> None:
    assert probe.required_front_setup_step(
        actor=probe.FRONT_HERO,
        protect_armed=True,
        minor_kinds=set(),
        teamwork_kinds=set(),
    ) == "minor_action"
    assert probe.required_front_setup_step(
        actor=probe.FRONT_HERO,
        protect_armed=True,
        minor_kinds={"settled_without_check"},
        teamwork_kinds=set(),
    ) == "team_assist"
    assert probe.required_front_setup_step(
        actor=probe.FRONT_HERO,
        protect_armed=True,
        minor_kinds={"settled_without_check"},
        teamwork_kinds={"registered_and_turn_consumed"},
    ) == "bladestorm"
    assert probe.required_front_setup_step(
        actor=probe.FRONT_HERO,
        protect_armed=True,
        minor_kinds={"settled_without_check"},
        teamwork_kinds={
            "registered_and_turn_consumed",
            "consumed_by_check",
        },
    ) == ""
    # Protect redirection evidence is intentionally not a gate: the real live
    # run can reach 诺艾尔 after the Boss action while redirection evidence is
    # still absent or invalid, but her required setup must not be skipped.


def test_provisional_bladestorm_defers_teamwork_consumption_evidence() -> None:
    failure = probe.expected_setup_evidence_failure(
        protect_arm_request=False,
        minor_action_request=False,
        team_assist_request=False,
        teamwork_check_expected=True,
        provisional_check_waiting=True,
        protect_kinds={"armed_out_of_turn"},
        minor_kinds={"settled_without_check"},
        teamwork_kinds={"registered_and_turn_consumed"},
        minor_status="observed",
        teamwork_status="partial",
    )

    assert failure is None


def test_settled_bladestorm_still_requires_teamwork_consumption_evidence() -> None:
    failure = probe.expected_setup_evidence_failure(
        protect_arm_request=False,
        minor_action_request=False,
        team_assist_request=False,
        teamwork_check_expected=True,
        provisional_check_waiting=False,
        protect_kinds={"armed_out_of_turn"},
        minor_kinds={"settled_without_check"},
        teamwork_kinds={"registered_and_turn_consumed"},
        minor_status="observed",
        teamwork_status="partial",
    )

    assert failure == {
        "category": "CAPABILITY_EVIDENCE_MISSING",
        "expected": ["consumed_by_check"],
        "observed": ["registered_and_turn_consumed"],
    }


def test_invalid_provisional_teamwork_evidence_still_fails_immediately() -> None:
    failure = probe.expected_setup_evidence_failure(
        protect_arm_request=False,
        minor_action_request=False,
        team_assist_request=False,
        teamwork_check_expected=True,
        provisional_check_waiting=True,
        protect_kinds={"armed_out_of_turn"},
        minor_kinds={"settled_without_check"},
        teamwork_kinds={"registered_and_turn_consumed"},
        minor_status="observed",
        teamwork_status="invalid",
    )

    assert failure is not None
    assert failure["category"] == "CAPABILITY_EVIDENCE_MISSING"


def test_protect_trigger_message_keeps_intended_target_with_support() -> None:
    message = probe.boss_protect_trigger_message()

    assert "intended target/target 必须仍为【星澜】" in message
    assert "不得在工具参数、selected_action或叙述中预先改成【诺艾尔】" in message
    assert "只能由Python权威的挺身守护反应" in message
    assert "实际检定/伤害目标改为【诺艾尔】" in message
    assert "检查是否应把实际目标改为诺艾尔" not in message
    assert "使用真实骰子结算" in message


def test_live_orchestration_uses_unredirected_protect_trigger_prompt() -> None:
    source = inspect.getsource(probe.run_live)

    assert "actor == BOSS_NAME and protect_armed and not protect_redirected" in source
    assert "message = boss_protect_trigger_message(actor)" in source


def test_bladestorm_prompt_is_one_skill_attack_with_multi_not_dual_wield(
    tmp_path: Path,
) -> None:
    service = FUGMHttpService(data_root=tmp_path, use_llm=False, rules_seed=72491)
    runtime, _fixture = probe.build_boss_fixture(service)

    message = probe._bladestorm_message(runtime)

    assert "发动职业技能【利刃风暴】" in message
    assert "主手【符文盾】" in message
    assert "进行一次近战攻击" in message
    assert "因该技能获得多重(2)" in message
    assert f"【{probe.BOSS_NAME}】和【{probe.MINION_NAME}】两个不同目标" in message
    assert "不是双武器攻击" in message
    assert "副手【青铜盾】只提供防御、不参与攻击" in message
    assert "不得设置details.dual_wield" in message
    assert "真实骰子" in message


def test_front_basic_attack_prompt_is_single_target_without_skill_or_dual_wield(
    tmp_path: Path,
) -> None:
    service = FUGMHttpService(data_root=tmp_path, use_llm=False, rules_seed=72492)
    runtime, _fixture = probe.build_boss_fixture(service)
    counters = {probe.FRONT_HERO: 1}

    message = probe._planned_player_message(
        runtime,
        probe.FRONT_HERO,
        counters,
    )

    assert "一次单目标普通Attack" in message
    assert "仅用主手【符文盾】" in message
    assert f"唯一目标【{probe.BOSS_NAME}】" in message
    assert probe.MINION_NAME not in message
    assert "不使用双武器" in message
    assert "不发动任何技能" in message
    assert "不施放任何法术" in message
    assert "不得设置details.dual_wield" in message
    assert "不得填写skill_name" in message
    assert "不得添加第二个目标" in message
    assert "双盾普通攻击" not in message
    assert "真实骰子" in message
    assert counters[probe.FRONT_HERO] == 2


def test_player_plan_targets_remaining_minion_after_boss_escapes(
    tmp_path: Path,
) -> None:
    service = FUGMHttpService(data_root=tmp_path, use_llm=False, rules_seed=72493)
    runtime, _fixture = probe.build_boss_fixture(service)
    conflict = runtime.app.conflict_manager.state
    conflict.escaped_combatants.add(probe.BOSS_NAME)
    runtime.app.character_manager.get(probe.BOSS_NAME).hp = 0

    front_message = probe._planned_player_message(
        runtime,
        probe.FRONT_HERO,
        {probe.FRONT_HERO: 1},
    )
    support_message = probe._planned_player_message(
        runtime,
        probe.SUPPORT_HERO,
        {probe.SUPPORT_HERO: 3},
    )

    assert probe.MINION_NAME in front_message
    assert probe.BOSS_NAME not in front_message
    assert probe.MINION_NAME in support_message
    assert probe.BOSS_NAME not in support_message


def test_low_mp_support_plan_requests_same_target_dual_wield_without_derived_mp_claim(
    tmp_path: Path,
) -> None:
    service = FUGMHttpService(data_root=tmp_path, use_llm=False, rules_seed=724931)
    runtime, _fixture = probe.build_boss_fixture(service)
    runtime.app.character_manager.get(probe.SUPPORT_HERO).mp = 3

    message = probe._planned_player_message(
        runtime,
        probe.SUPPORT_HERO,
        {probe.SUPPORT_HERO: 3},
    )
    brief = probe._state_brief(runtime)
    support = brief["combatants"][probe.SUPPORT_HERO]

    assert "MP不足" not in message
    assert "这次不施法" in message
    assert "双武器攻击" in message
    assert "主手晨星匕首和副手暮影匕首" in message
    assert f"都攻击{probe.BOSS_NAME}" in message
    assert "分别进行两次真实命中检定" in message
    assert support["mp"] == 3
    assert support["spells"] == ["元素幕障", "炎弹"]
    assert support["equipped_main_hand"] == "晨星匕首"
    assert support["equipped_off_hand"] == "暮影匕首"


def test_persistence_projection_excludes_only_same_runtime_ephemeral_windows(
    tmp_path: Path,
) -> None:
    service = FUGMHttpService(data_root=tmp_path, use_llm=False, rules_seed=72494)
    runtime, _fixture = probe.build_boss_fixture(service)
    manager = runtime.app.interceptor.decision_window_manager
    durable = manager.create(
        kind="skill_parameter",
        owner=probe.SUPPORT_HERO,
        blocking=True,
        payload={"label": "durable"},
    )
    ephemeral = manager.create(
        kind="trait_invocation",
        owner=probe.FRONT_HERO,
        blocking=False,
        action_type="InvokeTrait",
        resume_point="post_check",
        payload={"ephemeral_same_runtime": True},
    )

    pending_ids = {window.window_id for window in manager.pending()}
    projected_ids = {
        str(window.get("window_id") or "")
        for window in probe.state_projection(runtime)["pending_windows"]
    }

    assert durable.window_id in pending_ids
    assert ephemeral.window_id in pending_ids
    assert durable.window_id in projected_ids
    assert ephemeral.window_id not in projected_ids
    assert probe.verify_persistence(
        tmp_path,
        probe.CAMPAIGN_ID,
        runtime,
    )["matched"]
    # Verification must not expire a still-legal same-process invocation.
    assert manager.find_pending(window_id=ephemeral.window_id) is not None


def test_persistence_projection_hash_changes_for_triggered_multiattack_only(
    tmp_path: Path,
) -> None:
    service = FUGMHttpService(data_root=tmp_path, use_llm=False, rules_seed=72495)
    runtime, _fixture = probe.build_boss_fixture(service)
    boss = runtime.app.character_manager.get(probe.BOSS_NAME)
    before = probe.state_projection(runtime)
    before_hash = probe._canonical_hash(before)

    boss.npc_skill_effects["triggered_multiattack"] = {"熔核横扫": 2}

    after = probe.state_projection(runtime)
    after_hash = probe._canonical_hash(after)
    assert before_hash != after_hash
    assert before["characters"][probe.BOSS_NAME]["npc_skill_effects"] == {}
    assert after["characters"][probe.BOSS_NAME]["npc_skill_effects"] == {
        "triggered_multiattack": {"熔核横扫": 2}
    }
    # The persistence projection itself must remain safe to emit as canonical
    # JSON; no dataclass, enum, set, or arbitrary object may leak into it.
    json.dumps(after, ensure_ascii=False, sort_keys=True)


def test_persistence_projection_matches_crisis_and_phase_state_after_reload(
    tmp_path: Path,
) -> None:
    service = FUGMHttpService(data_root=tmp_path, use_llm=False, rules_seed=72496)
    runtime, _fixture = probe.build_boss_fixture(service)
    boss = runtime.app.character_manager.get(probe.BOSS_NAME)
    conflict = runtime.app.conflict_manager.state
    boss.hp = boss.crisis_threshold
    boss.npc_skill_effects["triggered_multiattack"] = {"熔核横扫": 2}
    conflict.current_escalation_stage[probe.BOSS_NAME] = 0

    runtime.app.save_campaign_memory(probe.CAMPAIGN_ID)
    before = probe.state_projection(runtime)
    reloaded_service = FUGMHttpService(data_root=tmp_path, use_llm=False)
    reloaded = reloaded_service._runtime(probe.CAMPAIGN_ID)
    after = probe.state_projection(reloaded)

    before_boss = before["characters"][probe.BOSS_NAME]
    after_boss = after["characters"][probe.BOSS_NAME]
    for field in (
        "crisis_threshold",
        "in_crisis",
        "npc_skill_effects",
        "npc_ability_profiles",
        "npc_attacks",
    ):
        assert after_boss[field] == before_boss[field]
    assert after["conflict"]["escalation_stages"] == before["conflict"][
        "escalation_stages"
    ]
    assert after["conflict"]["current_escalation_stage"] == before["conflict"][
        "current_escalation_stage"
    ]
    assert after_boss["in_crisis"] is True
    assert after["conflict"]["current_escalation_stage"][probe.BOSS_NAME] == 0
    assert probe._canonical_hash(after) == probe._canonical_hash(before)


def test_fixture_obeys_two_player_champion_three_and_persists(tmp_path: Path) -> None:
    service = FUGMHttpService(data_root=tmp_path, use_llm=False, rules_seed=918273)
    runtime, fixture = probe.build_boss_fixture(service)
    guards = probe.install_randomness_guards(service, runtime)

    assert fixture["ok"] is True
    assert all(fixture["checks"].values())
    assert fixture["characters"][probe.BOSS_NAME]["max_hp"] == 120
    assert fixture["characters"][probe.BOSS_NAME]["max_mp"] == 70
    assert fixture["characters"][probe.FRONT_HERO]["max_hp"] == 65
    assert fixture["characters"][probe.FRONT_HERO]["weapon_damage"] == 7
    assert fixture["characters"][probe.SUPPORT_HERO]["max_mp"] == 58
    assert fixture["characters"][probe.SUPPORT_HERO]["equipped_main_hand"] == "晨星匕首"
    assert fixture["characters"][probe.SUPPORT_HERO]["equipped_off_hand"] == "暮影匕首"
    assert fixture["checks"]["boss_minor_villain_five_ultima"] is True
    assert runtime.app.conflict_manager.state.ultima_points[probe.BOSS_NAME] == 5
    assert fixture["villain"] == {
        "type": "minor",
        "initial_ultima_points": 5,
        "zero_hp_escape_cost": 1,
        "expected_remaining_after_unspent_escape": 4,
    }
    assert fixture["characters"][probe.MINION_NAME]["initiative"] == 9
    assert fixture["characters"][probe.MINION_NAME]["defenses"] == {
        "physical": 10,
        "magic": 8,
    }
    assert fixture["characters"][probe.MINION_NAME]["affinities"] == {
        "poison": "immune",
        "earth": "resist",
        "lightning": "resist",
        "wind": "resist",
    }
    assert fixture["characters"][probe.MINION_NAME]["status_immunities"] == [
        "poisoned"
    ]
    assert fixture["encounter_budget"] == {
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
    }
    assert guards["combat"]["rng_type"] == "random.Random"
    assert guards["combat"]["probe_outcome_preload_used"] is False
    assert guards["combat"]["internal_real_roll_replay_preserved"] is True
    assert (
        runtime.app.interceptor.rules_engine.force_next_check_outcome.__func__.__name__
        == "force_next_check_outcome"
    )
    probe.assert_no_pending_outcome_replay(runtime)
    assert probe.verify_persistence(tmp_path, probe.CAMPAIGN_ID, runtime)["matched"]


def test_turn_87_dual_wield_grounding_uses_real_compressed_state_builder(
    tmp_path: Path,
) -> None:
    class NeverSemanticClient:
        config = type("Config", (), {"response_format_enabled": True})()

        def create_chat_completion(self, **_kwargs: object) -> str:
            raise AssertionError("exact authoritative dual wield must stay local")

    message = (
        "星澜的MP不足以继续施法，改用双武器攻击；"
        "晨星匕首和暮影匕首都攻击赤炉大将，"
        "分别进行两次真实命中检定。"
    )
    service = FUGMHttpService(data_root=tmp_path, use_llm=False, rules_seed=87)
    runtime, _fixture = probe.build_boss_fixture(service)
    runtime.app.character_manager.get(probe.SUPPORT_HERO).mp = 3
    runtime.app.interceptor.decision_window_manager.cancel_matching(
        kind="skill_parameter",
        reason="test_ready_for_conflict_action",
    )
    context = GMToolExecutionContext(
        campaign_id=probe.CAMPAIGN_ID,
        session_id=probe.SESSION_ID,
        channel_id=probe.CHANNEL_ID,
        speaker=probe.SUPPORT_PLAYER,
        gate_status="adventure",
        directly_addressed=True,
        metadata={
            "current_message": message,
            "current_turn_events": [
                {
                    "event_id": "boss-probe-087",
                    "speaker": probe.SUPPORT_PLAYER,
                    "text": message,
                }
            ],
            "gm_dynamic_capabilities_enabled": True,
            "gm_hot_adventure_capabilities_enabled": True,
            "gm_capability_routing_mode": "intent",
            "gm_state_context_mode": "summary_delta",
        },
    )

    state = service.gm_agent_message_coordinator.state_builder.build(context)
    verifier = GMReplyGroundingVerifier(NeverSemanticClient(), model="unused")
    review = verifier.verify_tool_proposal(
        current_message=message,
        recent_context="",
        observed_state=state,
        tool_name="perform_character_action",
        arguments={
            "action_type": "Attack",
            "actor": probe.SUPPORT_HERO,
            "target": probe.BOSS_NAME,
            "timing": "immediate",
            "details": {
                "dual_wield": True,
                "targets": [probe.BOSS_NAME, probe.BOSS_NAME],
            },
            "source_event_id": "message:boss-probe-087",
        },
        deadline=999999999.0,
    )

    assert context.metadata["gm_intent_profile_ids"] == ["conflict"]
    assert state["observation"]["profile"] == "intent_compact"
    assert review.valid is True
    assert review.category == "local_authoritative_same_target_dual_wield"


def test_turn_14_known_spell_grounding_uses_real_compressed_state_builder(
    tmp_path: Path,
) -> None:
    class NeverSemanticClient:
        config = type("Config", (), {"response_format_enabled": True})()

        def create_chat_completion(self, **_kwargs: object) -> str:
            raise AssertionError("literal authoritative spell intent must stay local")

    message = "星澜施放元素幕障，选择火元素，保护诺艾尔和星澜。"
    service = FUGMHttpService(data_root=tmp_path, use_llm=False, rules_seed=14)
    runtime, _fixture = probe.build_boss_fixture(service)
    runtime.app.interceptor.decision_window_manager.cancel_matching(
        kind="skill_parameter",
        reason="test_ready_for_conflict_action",
    )
    context = GMToolExecutionContext(
        campaign_id=probe.CAMPAIGN_ID,
        session_id=probe.SESSION_ID,
        channel_id=probe.CHANNEL_ID,
        speaker=probe.SUPPORT_PLAYER,
        gate_status="adventure",
        directly_addressed=True,
        metadata={
            "current_message": message,
            "current_turn_events": [
                {
                    "event_id": "boss-probe-014",
                    "speaker": probe.SUPPORT_PLAYER,
                    "text": message,
                }
            ],
            "gm_dynamic_capabilities_enabled": True,
            "gm_hot_adventure_capabilities_enabled": True,
            "gm_capability_routing_mode": "intent",
            "gm_state_context_mode": "summary_delta",
        },
    )

    state = service.gm_agent_message_coordinator.state_builder.build(context)
    hero = next(
        row
        for row in state["gameplay"]["characters"]
        if row["name"] == probe.SUPPORT_HERO
    )
    verifier = GMReplyGroundingVerifier(NeverSemanticClient(), model="unused")
    review = verifier.verify_tool_proposal(
        current_message=message,
        recent_context="",
        observed_state=state,
        tool_name="perform_character_action",
        arguments={
            "action_type": "Spell",
            "actor": probe.SUPPORT_HERO,
            "target": probe.FRONT_HERO,
            "timing": "immediate",
            "details": {
                "spell_name": "元素幕障",
                "element": "火",
                "targets": [probe.FRONT_HERO, probe.SUPPORT_HERO],
            },
        },
        deadline=999999999.0,
    )

    assert context.metadata["gm_intent_profile_ids"] == ["check_action"]
    assert state["observation"]["profile"] == "intent_compact"
    assert hero["spells"] == ["元素幕障", "炎弹"]
    assert review.valid is True
    assert review.category == "local_authoritative_known_spell_intent"


def test_fixture_skill_evidence_only_marks_deterministic_setup_checks(
    tmp_path: Path,
) -> None:
    service = FUGMHttpService(data_root=tmp_path, use_llm=False, rules_seed=12345)
    _runtime, fixture = probe.build_boss_fixture(service)
    matrix = probe.selected_skill_matrix()

    probe.update_skill_evidence(matrix, fixture=fixture)

    assert matrix["防御精通"]["status"] == "observed"
    assert matrix["双盾战士"]["status"] == "observed"
    assert matrix["集中心智"]["status"] == "observed"
    assert matrix["元素魔法"]["status"] == "missing"
    assert matrix["利刃风暴"]["status"] == "missing"
    assert matrix["挺身守护"]["status"] == "missing"
    assert matrix["知识就是力量"]["status"] == "missing"
    assert matrix["快速评估"]["status"] == "missing"


def test_active_skill_evidence_requires_committed_authoritative_resolution() -> None:
    matrix = probe.selected_skill_matrix()
    intent_only = {
        "turn_id": "turn-001",
        "route": "/v1/game/turn",
        "message": f"{probe.FRONT_HERO}使用利刃风暴。",
        "tool_receipts": [],
    }
    probe.update_skill_evidence(matrix, turn=intent_only)
    assert matrix["利刃风暴"]["status"] == "missing"

    provisional = {
        **intent_only,
        "authoritative_resolutions": [
            {
                "ok": True,
                "action": {
                    "action_type": "Skill",
                    "parameters": {"skill_name": "利刃风暴"},
                },
                "payload": {
                    "skill_name": "利刃风暴",
                    "check_result_provisional": True,
                },
            }
        ],
    }
    probe.update_skill_evidence(matrix, turn=provisional)
    assert matrix["利刃风暴"]["status"] == "missing"

    settled = {
        **provisional,
        "authoritative_resolutions": [
            {
                "ok": True,
                "action": {
                    "action_type": "Skill",
                    "parameters": {"skill_name": "利刃风暴"},
                },
                "payload": {"skill_name": "利刃风暴"},
            }
        ],
    }
    probe.update_skill_evidence(matrix, turn=settled)
    assert matrix["利刃风暴"]["status"] == "observed"

    scholar_check = {
        "turn_id": "turn-scholar",
        "authoritative_resolutions": [
            {
                "ok": True,
                "action": {
                    "action_type": "RequestRoll",
                    "parameters": {"actor": probe.SUPPORT_HERO},
                },
                "payload": {
                    "skill_trigger_effects": [
                        {"source": "知识就是力量", "modifier": 1}
                    ]
                },
            }
        ],
    }
    probe.update_skill_evidence(matrix, turn=scholar_check)
    assert matrix["知识就是力量"]["status"] == "observed"


def test_elemental_magic_requires_both_barrier_and_fire_spell_evidence() -> None:
    matrix = probe.selected_skill_matrix()

    barrier_turn = {
        "turn_id": "barrier",
        "authoritative_resolutions": [
            {
                "ok": True,
                "action": {
                    "action_type": "Spell",
                    "parameters": {
                        "actor": probe.SUPPORT_HERO,
                        "spell_name": "元素幕障",
                    },
                },
                "payload": {"spell_name": "元素幕障"},
            }
        ],
    }
    fire_turn = {
        "turn_id": "fire",
        "authoritative_resolutions": [
            {
                "ok": True,
                "action": {
                    "action_type": "Spell",
                    "parameters": {
                        "actor": probe.SUPPORT_HERO,
                        "spell_name": "炎弹",
                    },
                },
                "payload": {"spell_name": "炎弹"},
            }
        ],
    }

    probe.update_skill_evidence(matrix, turn=barrier_turn)
    assert matrix["元素魔法"]["status"] == "partial"
    probe.update_skill_evidence(matrix, turn=fire_turn)
    assert matrix["元素魔法"]["status"] == "observed"
    assert {
        item["kind"] for item in matrix["元素魔法"]["evidence"]
    } == {"elemental_barrier_cast", "fire_spell_cast"}


def test_dual_wield_capability_requires_all_authoritative_invariants() -> None:
    matrix = probe.optional_capability_matrix([])
    turn = {
        "turn_id": "turn-002",
        "authoritative_resolutions": [
            {
                "ok": True,
                "action": {
                    "action_type": "Attack",
                    "parameters": {"actor": probe.SUPPORT_HERO},
                },
                "payload": {
                    "dual_wield": True,
                    "dual_wield_weapons": ["晨星匕首", "暮影匕首"],
                    "dual_wield_targets": [probe.BOSS_NAME, probe.MINION_NAME],
                    "dual_wield_attacks": [
                        {
                            "strike": 1,
                            "roll": {
                                "success": False,
                                "high_roll": 3,
                                "damage": 0,
                            },
                        },
                        {
                            "strike": 2,
                            "roll": {
                                "success": False,
                                "high_roll": 2,
                                "damage": 0,
                            },
                        },
                    ],
                    "rolls": [
                        {"success": False, "high_roll": 3, "damage": 0},
                        {"success": False, "high_roll": 2, "damage": 0},
                    ],
                    "dual_wield_high_roll_override": 0,
                    "multi_attack_suppressed": True,
                },
            }
        ],
    }

    probe.update_capability_evidence(matrix, turn=turn)

    assert matrix["dual_wield"]["status"] == "observed"
    assert all(
        matrix["dual_wield"]["evidence"][0]["invariants"].values()
    )


def test_dual_wield_capability_ignores_declaration_before_mechanics_payload() -> None:
    matrix = probe.optional_capability_matrix([])
    turn = {
        "turn_id": "turn-dual-final",
        "authoritative_resolutions": [
            {
                "ok": True,
                "action": {
                    "action_type": "Attack",
                    "parameters": {
                        "actor": probe.SUPPORT_HERO,
                        "dual_wield": True,
                        "targets": [probe.BOSS_NAME, probe.MINION_NAME],
                    },
                },
                "payload": {
                    "dual_wield": True,
                    "dual_wield_weapons": ["晨星匕首", "暮影匕首"],
                    "dual_wield_targets": [probe.BOSS_NAME, probe.MINION_NAME],
                    "dual_wield_attacks": [
                        {"strike": 1, "roll": {"high_roll": 0}},
                        {"strike": 2, "roll": {"high_roll": 0}},
                    ],
                    "rolls": [{"high_roll": 0}, {"high_roll": 0}],
                    "dual_wield_high_roll_override": 0,
                    "multi_attack_suppressed": True,
                },
            }
        ],
    }

    probe.update_capability_evidence(matrix, turn=turn)

    assert matrix["dual_wield"]["status"] == "observed"
    assert matrix["dual_wield"]["evidence"][0]["path"].endswith("payload")
    assert all(matrix["dual_wield"]["evidence"][0]["invariants"].values())


def test_protect_skill_requires_receipt_arm_redirect_and_nonreuse() -> None:
    matrix = probe.selected_skill_matrix()
    roll = {
        "actor": probe.BOSS_NAME,
        "attributes": ["MIG", "MIG"],
        "dice": [[6, 4], [6, 3]],
        "total": 8,
        "target_number": 10,
        "target": probe.FRONT_HERO,
    }
    armed_turn = {
        "turn_id": "turn-arm",
        "tool_receipts": [
            {
                "ok": True,
                "result": {
                    "committed_action": {
                        "action_type": "Skill",
                        "skill_name": "挺身守护",
                        "target": probe.SUPPORT_HERO,
                    }
                },
            }
        ],
        "authoritative_resolutions": [
            {
                "ok": True,
                "action": {
                    "action_type": "Skill",
                    "parameters": {
                        "actor": probe.FRONT_HERO,
                        "target": probe.SUPPORT_HERO,
                        "skill_name": "挺身守护",
                    },
                },
                "payload": {
                    "protect_reaction_armed": True,
                    "protector": probe.FRONT_HERO,
                    "protected_target": probe.SUPPORT_HERO,
                    "out_of_turn": True,
                    "turn_consumed": False,
                },
            }
        ],
    }
    probe.update_protect_evidence(matrix, turn=armed_turn)
    assert matrix["挺身守护"]["status"] == "partial"

    redirected_turn = {
        "turn_id": "turn-hit",
        "state_before": {"turn_serial": 1},
        "authoritative_resolutions": [
            {
                "ok": True,
                "action": {
                    "action_type": "NPCAct",
                    "parameters": {
                        "actor": probe.BOSS_NAME,
                        "target": probe.SUPPORT_HERO,
                        "npc_action_type": "Attack",
                    },
                },
                "rules_text": (
                    f"{probe.FRONT_HERO}发动【挺身守护】，"
                    f"代替{probe.SUPPORT_HERO}承受这次攻击。"
                ),
                "payload": {
                    "roll": roll,
                    "cover_text": (
                        f"{probe.FRONT_HERO}发动【挺身守护】，"
                        f"代替{probe.SUPPORT_HERO}承受这次攻击。"
                    ),
                },
            }
        ],
    }
    probe.update_protect_evidence(matrix, turn=redirected_turn)
    assert matrix["挺身守护"]["status"] == "partial"

    not_reused_turn = {
        "turn_id": "turn-next-hit",
        "state_before": {"turn_serial": 2},
        "authoritative_resolutions": [
            {
                "ok": True,
                "action": {
                    "action_type": "NPCAct",
                    "parameters": {
                        "actor": probe.BOSS_NAME,
                        "target": probe.SUPPORT_HERO,
                        "npc_action_type": "Attack",
                    },
                },
                "rules_text": "普通攻击。",
                "payload": {
                    "roll": {**roll, "target": probe.SUPPORT_HERO},
                },
            }
        ],
    }
    probe.update_protect_evidence(matrix, turn=not_reused_turn)
    assert matrix["挺身守护"]["status"] == "observed"
    assert {item["kind"] for item in matrix["挺身守护"]["evidence"]} == {
        "successful_skill_receipt",
        "armed_out_of_turn",
        "redirected",
        "not_reused",
    }
    assert matrix["挺身守护"]["observed_redirect_count"] == 1


def test_real_protect_reaction_is_out_of_turn_and_redirects_next_attack(
    tmp_path: Path,
) -> None:
    service = FUGMHttpService(data_root=tmp_path, use_llm=False, rules_seed=271828)
    runtime, _fixture = probe.build_boss_fixture(service)
    conflict = runtime.app.conflict_manager
    interceptor = runtime.app.interceptor
    quick_window = interceptor.decision_window_manager.find_pending(
        kind="skill_parameter",
        owner=probe.SUPPORT_HERO,
    )
    interceptor.decision_window_manager.resolve(
        window_id=quick_window.window_id,
        responder=probe.SUPPORT_HERO,
        resolution={"choice": "decline"},
    )
    actor_before = conflict.state.current_actor()
    records = probe.install_resolution_capture(runtime)

    armed = interceptor.resolve(
        Action(
            ActionType.SKILL,
            {
                "actor": probe.FRONT_HERO,
                "target": probe.SUPPORT_HERO,
                "skill_name": "挺身守护",
                "_enforce_turn_order": True,
            },
        )
    )
    assert armed.payload["protect_reaction_armed"] is True
    assert armed.payload["turn_consumed"] is False
    assert conflict.state.current_actor() == actor_before == probe.BOSS_NAME

    attack = interceptor.resolve(
        Action(
            ActionType.ATTACK,
            {
                "actor": probe.BOSS_NAME,
                "target": probe.SUPPORT_HERO,
                "_enforce_turn_order": True,
            },
        )
    )
    assert "挺身守护" in attack.payload["cover_text"]
    assert attack.payload["roll"].target == probe.FRONT_HERO
    reaction_effect = next(
        effect
        for effect in conflict.state.active_effects
        if effect.effect_type == "protect_reaction"
    )
    assert reaction_effect.data["used"] is True

    second = interceptor.resolve(
        Action(
            ActionType.ATTACK,
            {
                "actor": probe.BOSS_NAME,
                "target": probe.SUPPORT_HERO,
                "_enforce_turn_order": True,
            },
        )
    )
    assert second.payload["roll"].target == probe.SUPPORT_HERO
    assert [record["action"]["action_type"] for record in records] == [
        "Skill",
        "Attack",
        "Attack",
    ]
    assert records[0]["payload"]["out_of_turn"] is True
    assert records[1]["payload"]["roll"]["target"] == probe.FRONT_HERO
    assert records[2]["payload"]["roll"]["target"] == probe.SUPPORT_HERO


def test_minor_action_and_team_assist_require_authoritative_lifecycle_evidence() -> None:
    matrix = probe.optional_capability_matrix([])
    roll = {
        "actor": probe.FRONT_HERO,
        "attributes": ["MIG", "MIG"],
        "dice": [[10, 6], [10, 4]],
        "total": 10,
        "target_number": 10,
        "target": probe.BOSS_NAME,
        "high_roll": 6,
        "success": True,
    }
    minor_turn = {
        "turn_id": "minor",
        "state_before": {"actor": probe.FRONT_HERO, "turn_serial": 2},
        "state_after": {
            "actor": probe.FRONT_HERO,
            "turn_serial": 3,
            "story_item": {"state_note": "断开辅助燃料"},
        },
        "authoritative_resolutions": [
            {
                "ok": True,
                "action": {
                    "action_type": "MinorAction",
                    "parameters": {
                        "actor": probe.FRONT_HERO,
                        "mode": "interact",
                        "item_name": "炉心安全栓",
                        "state_note": "断开辅助燃料",
                    },
                },
                "payload": {
                    "minor_action": True,
                    "minor_action_mode": "interact",
                    "story_item": {"current_state": "断开辅助燃料"},
                },
            }
        ],
    }
    probe.update_capability_evidence(matrix, turn=minor_turn)
    assert matrix["minor_action"]["status"] == "partial"

    assist_turn = {
        "turn_id": "assist",
        "state_before": {"actor": probe.FRONT_HERO, "turn_serial": 3},
        "state_after": {
            "actor": probe.FRONT_HERO,
            "turn_serial": 4,
            "acted_this_round": [probe.SUPPORT_HERO],
            "pending_assists": {probe.FRONT_HERO: [probe.SUPPORT_HERO]},
            "action_penalties": {probe.SUPPORT_HERO: 1},
        },
        "authoritative_resolutions": [
            {
                "ok": True,
                "action": {
                    "action_type": "Guard",
                    "parameters": {
                        "actor": probe.SUPPORT_HERO,
                        "assist_target": probe.FRONT_HERO,
                        "reasoning": "协助利刃风暴",
                    },
                },
                "payload": {
                    "team_assist_registered": True,
                    "out_of_turn": True,
                    "supporter": probe.SUPPORT_HERO,
                    "leader": probe.FRONT_HERO,
                },
            }
        ],
    }
    probe.update_capability_evidence(matrix, turn=assist_turn)
    assert matrix["team_assist"]["status"] == "partial"

    checked_turn = {
        "turn_id": "bladestorm",
        "state_before": {"actor": probe.FRONT_HERO, "turn_serial": 4},
        "state_after": {
            "actor": probe.BOSS_NAME,
            "turn_serial": 4,
            "acted_this_round": [probe.SUPPORT_HERO, probe.FRONT_HERO],
            "pending_assists": {},
        },
        "authoritative_resolutions": [
            {
                "ok": True,
                "action": {
                    "action_type": "Skill",
                    "parameters": {
                        "actor": probe.FRONT_HERO,
                        "skill_name": "利刃风暴",
                        "target": probe.BOSS_NAME,
                    },
                },
                "payload": {
                    "roll": roll,
                    "conflict_teamwork": {
                        "supporters": [probe.SUPPORT_HERO],
                        "support_bonus": 1,
                    },
                },
            }
        ],
    }
    probe.update_capability_evidence(matrix, turn=checked_turn)
    assert matrix["minor_action"]["status"] == "observed"
    assert matrix["team_assist"]["status"] == "partial"
    assert matrix["team_assist"]["observed_consumption_count"] == 1

    later_check = {
        "turn_id": "later-check",
        "state_before": {"actor": probe.FRONT_HERO, "turn_serial": 8},
        "state_after": {"actor": probe.BOSS_NAME, "turn_serial": 8},
        "authoritative_resolutions": [
            {
                "ok": True,
                "action": {
                    "action_type": "Attack",
                    "parameters": {
                        "actor": probe.FRONT_HERO,
                        "target": probe.BOSS_NAME,
                    },
                },
                "payload": {"roll": {**roll, "dice": [[10, 5], [10, 4]]}},
            }
        ],
    }
    probe.update_capability_evidence(matrix, turn=later_check)
    assert matrix["team_assist"]["status"] == "observed"
    assert {
        item["kind"] for item in matrix["team_assist"]["evidence"]
    } == {
        "registered_and_turn_consumed",
        "consumed_by_check",
        "not_reused",
    }


def test_minor_main_action_evidence_survives_deferred_critical_decline() -> None:
    matrix = probe.optional_capability_matrix([])
    minor_turn = {
        "turn_id": "minor",
        "state_before": {"actor": probe.FRONT_HERO, "turn_serial": 2},
        "state_after": {
            "actor": probe.FRONT_HERO,
            "turn_serial": 3,
            "story_item": {"state_note": "断开辅助燃料"},
        },
        "authoritative_resolutions": [
            {
                "ok": True,
                "action": {
                    "action_type": "MinorAction",
                    "parameters": {
                        "actor": probe.FRONT_HERO,
                        "mode": "interact",
                        "item_name": "炉心安全栓",
                    },
                },
                "payload": {
                    "minor_action": True,
                    "minor_action_mode": "interact",
                    "story_item": {"current_state": "断开辅助燃料"},
                },
            }
        ],
    }
    probe.update_capability_evidence(matrix, turn=minor_turn)

    critical_bladestorm = {
        "turn_id": "bladestorm-critical",
        "awaiting_rule_window": True,
        "state_before": {"actor": probe.FRONT_HERO, "turn_serial": 3},
        "state_after": {
            "actor": probe.FRONT_HERO,
            "turn_serial": 3,
            "acted_this_round": [probe.SUPPORT_HERO],
        },
        "authoritative_resolutions": [
            {
                "ok": True,
                "action": {
                    "action_type": "Skill",
                    "parameters": {
                        "actor": probe.FRONT_HERO,
                        "skill_name": "利刃风暴",
                        "target": probe.BOSS_NAME,
                    },
                },
                "payload": {
                    "roll": {
                        "actor": probe.FRONT_HERO,
                        "attributes": ["MIG", "MIG"],
                        "dice": [[10, 8], [10, 8]],
                        "total": 16,
                        "target_number": 10,
                        "target": probe.BOSS_NAME,
                        "high_roll": 8,
                        "success": True,
                        "critical_success": True,
                    },
                    "decision_windows": [
                        {
                            "kind": "critical_opportunity",
                            "owner": probe.FRONT_HERO,
                            "blocking": True,
                        }
                    ],
                },
            }
        ],
    }
    probe.update_capability_evidence(matrix, turn=critical_bladestorm)

    assert matrix["minor_action"]["status"] == "partial"
    assert matrix["minor_action"]["_pending_main_action_preservation"] == {
        "turn_id": "bladestorm-critical",
        "path": "authoritative_resolutions.0",
        "skill_name": "利刃风暴",
        "turn_serial_before": 3,
        "real_check_resolved": True,
    }

    declined = {
        "turn_id": "critical-declined",
        "state_before": {"actor": probe.FRONT_HERO, "turn_serial": 3},
        "state_after": {
            "actor": probe.BOSS_NAME,
            "turn_serial": 3,
            "acted_this_round": [probe.SUPPORT_HERO, probe.FRONT_HERO],
        },
        "authoritative_resolutions": [
            {
                "ok": True,
                "action": {
                    "action_type": "TriggerOpportunity",
                    "parameters": {
                        "actor": probe.FRONT_HERO,
                        "effect": "decline",
                    },
                },
                "payload": {
                    "opportunity_declined": True,
                    "resume_deferred_action": True,
                    "deferred_action_type": "Skill",
                    "deferred_action_owner": probe.FRONT_HERO,
                },
            }
        ],
    }
    probe.update_capability_evidence(matrix, turn=declined)

    assert matrix["minor_action"]["status"] == "observed"
    assert "_pending_main_action_preservation" not in matrix["minor_action"]
    main_action = next(
        item
        for item in matrix["minor_action"]["evidence"]
        if item["kind"] == "main_action_preserved"
    )
    assert main_action["source"] == "deferred_action_resolution"
    assert main_action["origin_turn_id"] == "bladestorm-critical"
    assert all(main_action["invariants"].values())


def test_team_assist_counts_deferred_check_acceptance_only_once() -> None:
    matrix = probe.optional_capability_matrix([])
    roll = {
        "actor": probe.FRONT_HERO,
        "attributes": ["MIG", "MIG"],
        "dice": [[10, 4], [10, 1]],
        "total": 6,
        "modifier": 1,
        "target_number": 10,
        "target": probe.BOSS_NAME,
        "high_roll": 4,
        "success": False,
    }
    teamwork = {
        "supporters": [probe.SUPPORT_HERO],
        "support_bonus": 1,
        "turns_consumed": True,
    }
    probe.update_capability_evidence(
        matrix,
        turn={
            "turn_id": "assist",
            "state_before": {
                "actor": probe.FRONT_HERO,
                "turn_serial": 3,
            },
            "state_after": {
                "actor": probe.FRONT_HERO,
                "turn_serial": 4,
                "acted_this_round": [probe.SUPPORT_HERO],
                "pending_assists": {
                    probe.FRONT_HERO: [probe.SUPPORT_HERO]
                },
                "action_penalties": {probe.SUPPORT_HERO: 1},
            },
            "authoritative_resolutions": [
                {
                    "ok": True,
                    "action": {
                        "action_type": "Assist",
                        "parameters": {
                            "actor": probe.SUPPORT_HERO,
                            "assist_target": probe.FRONT_HERO,
                            "reasoning": "协助利刃风暴命中检定",
                        },
                    },
                    "payload": {
                        "team_assist_registered": True,
                        "out_of_turn": True,
                        "supporter": probe.SUPPORT_HERO,
                        "leader": probe.FRONT_HERO,
                    },
                }
            ],
        },
    )

    committed_payload = {
        "roll": roll,
        "rolls": [roll, {**roll, "target": probe.MINION_NAME}],
        "conflict_teamwork": teamwork,
    }
    probe.update_capability_evidence(
        matrix,
        turn={
            "turn_id": "deferred-check-accepted",
            "state_before": {
                "actor": probe.FRONT_HERO,
                "turn_serial": 4,
                "pending_assists": {
                    probe.FRONT_HERO: [probe.SUPPORT_HERO]
                },
            },
            "state_after": {
                "actor": probe.BOSS_NAME,
                "turn_serial": 4,
                "acted_this_round": [probe.SUPPORT_HERO, probe.FRONT_HERO],
                "pending_assists": {},
            },
            "authoritative_resolutions": [
                {
                    "ok": True,
                    "action": {
                        "action_type": "Skill",
                        "parameters": {
                            "actor": probe.FRONT_HERO,
                            "skill_name": "利刃风暴",
                            "target": probe.BOSS_NAME,
                        },
                    },
                    "payload": committed_payload,
                },
                {
                    "ok": True,
                    "action": {
                        "action_type": "ResolveDecision",
                        "parameters": {
                            "actor": probe.FRONT_HERO,
                            "choice": "accept_result",
                            "post_check_acceptance": True,
                        },
                    },
                    "payload": {
                        **committed_payload,
                        "before_roll": roll,
                        "committed_source_action": {
                            "action_type": "Skill",
                            "parameters": {
                                "actor": probe.FRONT_HERO,
                                "skill_name": "利刃风暴",
                            },
                        },
                        "resume_deferred_action": True,
                        "deferred_action_type": "Skill",
                        "deferred_action_owner": probe.FRONT_HERO,
                    },
                },
            ],
        },
    )

    assert matrix["team_assist"]["status"] == "partial"
    assert matrix["team_assist"]["observed_consumption_count"] == 1
    assert len(matrix["team_assist"]["consumption_fingerprints"]) == 1

    probe.update_capability_evidence(
        matrix,
        turn={
            "turn_id": "later-check",
            "state_before": {
                "actor": probe.FRONT_HERO,
                "turn_serial": 8,
            },
            "state_after": {
                "actor": probe.BOSS_NAME,
                "turn_serial": 8,
            },
            "authoritative_resolutions": [
                {
                    "ok": True,
                    "action": {
                        "action_type": "Attack",
                        "parameters": {
                            "actor": probe.FRONT_HERO,
                            "target": probe.BOSS_NAME,
                        },
                    },
                    "payload": {"roll": {**roll, "modifier": 0}},
                }
            ],
        },
    )

    assert matrix["team_assist"]["status"] == "observed"
    assert matrix["team_assist"]["observed_consumption_count"] == 1


def test_roll_extraction_keeps_real_fields_and_deduplicates() -> None:
    roll = {
        "actor": probe.FRONT_HERO,
        "target": probe.BOSS_NAME,
        "attributes": ["MIG", "MIG"],
        "dice": [[10, 7], [10, 3]],
        "total": 10,
        "modifier": 0,
        "high_roll": 7,
        "target_number": 10,
        "success": True,
        "critical_success": False,
        "fumble": False,
        "damage": 14,
        "damage_type": "physical",
        "applied_affinity": "normal",
        "hp_after": 106,
    }
    payload = {"tool_receipts": [{"result": {"roll": roll, "copy": roll}}]}

    rows = probe.extract_rolls(payload, turn_id="turn-001")

    assert len(rows) == 1
    assert rows[0]["turn_id"] == "turn-001"
    assert rows[0]["dice"] == [[10, 7], [10, 3]]
    assert rows[0]["damage"] == 14

    destination: list[dict[str, object]] = []
    seen: set[str] = set()
    probe.append_unique_rolls(destination, rows, seen=seen)
    replay = [{**rows[0], "turn_id": "turn-confirm", "path": "replay.roll"}]
    probe.append_unique_rolls(destination, replay, seen=seen)
    assert len(destination) == 1


def test_roll_extraction_preserves_two_identical_indexed_strikes() -> None:
    identical = {
        "actor": probe.SUPPORT_HERO,
        "target": probe.BOSS_NAME,
        "attributes": ["DEX", "MIG"],
        "dice": [[6, 3], [8, 4]],
        "total": 7,
        "modifier": 0,
        "high_roll": 0,
        "target_number": 10,
        "success": False,
        "critical_success": False,
        "fumble": False,
        "damage": 0,
        "damage_type": "physical",
        "applied_affinity": "normal",
        "hp_after": None,
    }
    payload = {
        "roll": identical,
        "rolls": [identical, identical],
        "dual_wield_attacks": [
            {"strike": 1, "roll": identical},
            {"strike": 2, "roll": identical},
        ],
    }

    rows = probe.extract_rolls(payload, turn_id="dual-identical")

    assert len(rows) == 2
    assert [row["occurrence_index"] for row in rows] == [0, 1]


def test_roll_extraction_uses_dual_strikes_not_convenience_aliases() -> None:
    first = {
        "actor": probe.SUPPORT_HERO,
        "target": probe.BOSS_NAME,
        "dice": [[6, 2], [10, 8]],
        "total": 9,
        "success": True,
        "high_roll": 0,
        "damage": 4,
        "hp_after": 87,
    }
    second = {
        "actor": probe.SUPPORT_HERO,
        "target": probe.MINION_NAME,
        "dice": [[6, 1], [10, 10]],
        "total": 11,
        "success": True,
        "high_roll": 0,
        "damage": 4,
        "hp_after": 7,
    }
    payload = {
        "dual_wield_attacks": [
            {"strike": 1, "roll": first},
            {"strike": 2, "roll": second},
        ],
        "rolls": [first, second],
        "roll": second,
        "check_roll_sequence": [first, second],
    }

    rows = probe.extract_rolls(payload, turn_id="dual-settled")

    assert len(rows) == 2
    assert [row["target"] for row in rows] == [probe.BOSS_NAME, probe.MINION_NAME]
    assert all("dual_wield_attacks" in row["path"] for row in rows)


def test_check_roll_confirmation_window_is_a_normal_roll_choice() -> None:
    window = type(
        "Window",
        (),
        {"kind": "check_roll_confirmation", "owner": probe.SUPPORT_HERO},
    )()

    speaker, message = probe._window_response(window)

    assert speaker == probe.SUPPORT_PLAYER
    assert "确认现在投骰" in message


def test_opportunity_window_response_explicitly_declines_without_deferral() -> None:
    window = type(
        "Window",
        (),
        {"kind": "critical_opportunity", "owner": probe.FRONT_HERO},
    )()

    speaker, message = probe._window_response(window)

    assert speaker == probe.FRONT_PLAYER
    assert "立即放弃本次机会" in message
    assert "不产生机会效果" in message
    assert "不把机会保留到稍后" in message


def test_npc_fate_window_response_chooses_a_concrete_nonlethal_fate() -> None:
    window = type(
        "Window",
        (),
        {
            "kind": "npc_fate",
            "owner": probe.SUPPORT_HERO,
            "payload": {"target": probe.MINION_NAME},
        },
    )()

    speaker, message = probe._window_response(window)

    assert speaker == probe.SUPPORT_PLAYER
    assert probe.MINION_NAME in message
    assert "俘虏" in message
    assert "不杀死" in message
    assert "NPC命运窗口" in message


def test_blocking_npc_fate_precedes_natural_end_until_resolved() -> None:
    fate = type(
        "Window",
        (),
        {
            "kind": "npc_fate",
            "owner": probe.SUPPORT_HERO,
            "blocking": True,
            "payload": {"target": probe.MINION_NAME},
        },
    )()

    first = probe._priority_probe_request(
        actor=probe.SUPPORT_HERO,
        natural_end_ready=True,
        blocking=[fate],
        quick=None,
    )

    assert first is not None
    first_speaker, first_message, first_route, first_actor = first
    assert first_speaker == probe.SUPPORT_PLAYER
    assert first_route == "/v1/game/turn"
    assert first_actor == probe.SUPPORT_HERO
    assert probe.MINION_NAME in first_message
    assert "俘虏" in first_message
    assert "end_conflict" not in first_message

    second = probe._priority_probe_request(
        actor=probe.SUPPORT_HERO,
        natural_end_ready=True,
        blocking=[],
        quick=None,
    )

    assert second is not None
    second_speaker, second_message, second_route, second_actor = second
    assert second_speaker == "系统"
    assert second_route == "/v1/game/gm-beat"
    assert second_actor == probe.SUPPORT_HERO
    assert "end_conflict" in second_message


def test_raw_roll_audit_accounts_for_mastery_guard_affinity_and_hp_delta() -> None:
    turn = {
        "turn_id": "raw-001",
        "state_before": {
            "current_stage": -1,
            "hp": {probe.FRONT_HERO: 65, probe.BOSS_NAME: 120},
            "combatants": {
                probe.BOSS_NAME: {
                    "hp": 120,
                    "max_hp": 120,
                    "weapon_damage": 5,
                    "equipment_attack_damage_bonus": 0,
                },
                probe.FRONT_HERO: {
                    "hp": 65,
                    "max_hp": 65,
                    "skills": {"防御精通": 2},
                    "equipped_shield": "青铜盾",
                    "guarding": True,
                },
            },
        },
        "state_after": {
            "current_stage": -1,
            "hp": {probe.FRONT_HERO: 62, probe.BOSS_NAME: 120},
            "combatants": {},
            "escaped": [],
        },
        "authoritative_resolutions": [
            {
                "ok": True,
                "action": {
                    "action_type": "Attack",
                    "parameters": {
                        "actor": probe.BOSS_NAME,
                        "target": probe.FRONT_HERO,
                    },
                },
                "payload": {
                    "roll": {
                        "actor": probe.BOSS_NAME,
                        "target": probe.FRONT_HERO,
                        "attributes": ["MIG", "MIG"],
                        "dice": [[6, 4], [6, 4]],
                        "total": 8,
                        "modifier": 0,
                        "high_roll": 4,
                        "target_number": 13,
                        "success": True,
                        "critical_success": True,
                        "fumble": False,
                        # HR4 + weapon5 - mastery2 = 7; Guard halves to 3.
                        "damage": 3,
                        "damage_type": "fire",
                        "applied_affinity": "normal",
                        "hp_after": 62,
                    }
                },
            }
        ],
    }

    audit = probe.audit_authoritative_rules([turn])

    roll_audit = audit["roll_checks"][0]
    assert roll_audit["status"] == "passed"
    assert {item["name"]: item["status"] for item in roll_audit["checks"]} == {
        "success_from_total_critical_fumble": "passed",
        "damage_formula": "passed",
        "damage_hp_delta": "passed",
    }
    # No terminal branch occurred, so the overall audit honestly remains
    # unknown rather than pretending that absent evidence passed.
    assert audit["status"] == "unknown"


def test_raw_roll_audit_fails_closed_on_damage_mismatch() -> None:
    turn = {
        "turn_id": "raw-bad",
        "state_before": {
            "current_stage": -1,
            "hp": {probe.BOSS_NAME: 120, probe.FRONT_HERO: 65},
            "combatants": {
                probe.BOSS_NAME: {
                    "hp": 120,
                    "max_hp": 120,
                    "weapon_damage": 5,
                    "equipment_attack_damage_bonus": 0,
                },
                probe.FRONT_HERO: {
                    "hp": 65,
                    "max_hp": 65,
                    "skills": {},
                    "equipped_shield": "",
                    "guarding": False,
                },
            },
        },
        "state_after": {
            "current_stage": -1,
            "hp": {probe.BOSS_NAME: 120, probe.FRONT_HERO: 51},
            "combatants": {},
            "escaped": [],
        },
        "authoritative_resolutions": [
            {
                "ok": True,
                "action": {
                    "action_type": "Attack",
                    "parameters": {"actor": probe.BOSS_NAME},
                },
                "payload": {
                    "roll": {
                        "actor": probe.BOSS_NAME,
                        "target": probe.FRONT_HERO,
                        "attributes": ["MIG", "MIG"],
                        "dice": [[6, 5], [6, 4]],
                        "total": 9,
                        "modifier": 0,
                        "high_roll": 5,
                        "target_number": 8,
                        "success": True,
                        "critical_success": False,
                        "fumble": False,
                        "damage": 14,
                        "damage_type": "fire",
                        "applied_affinity": "normal",
                        "hp_after": 51,
                    }
                },
            }
        ],
    }

    audit = probe.audit_authoritative_rules([turn])

    assert audit["status"] == "failed"
    formula = next(
        item
        for item in audit["roll_checks"][0]["checks"]
        if item["name"] == "damage_formula"
    )
    assert formula["status"] == "failed"
    assert formula["expected"] == 10
    assert formula["observed"] == 14


def _phase_zero_hp_trigger_turn() -> dict[str, object]:
    return {
        "turn_id": "phase-trigger",
        "state_before": {
            "current_stage": -1,
            "ultima": 5,
            "hp": {probe.BOSS_NAME: 2},
            "combatants": {
                probe.BOSS_NAME: {
                    "hp": 2,
                    "max_hp": 120,
                    "mp": 12,
                    "max_mp": 70,
                }
            },
        },
        "state_after": {
            "current_stage": 0,
            "ultima": 5,
            "hp": {probe.BOSS_NAME: 120},
            "combatants": {
                probe.BOSS_NAME: {
                    "hp": 120,
                    "max_hp": 120,
                    "mp": 70,
                    "max_mp": 70,
                    "temporary_affinities": {"fire": "resist"},
                }
            },
            "defeated": [],
            "escaped": [],
        },
        "authoritative_resolutions": [
            {
                "ok": True,
                "action": {
                    "action_type": "Attack",
                    "parameters": {"target": probe.BOSS_NAME},
                },
                "rules_text": "赤炉大将进入新阶段。",
                "payload": {
                    "actual_hp_loss": 2,
                    "conflict_event": {
                        "target": probe.BOSS_NAME,
                        "event_type": "boss_phase",
                        "summary": "火焰滑散，公开显示它已获得火系抵抗。",
                        "hp_after": 120,
                        "mp_after": 70,
                    },
                },
            }
        ],
    }


def _terminal_zero_hp_trigger_turn(
    *,
    other_hostile: bool = False,
) -> dict[str, object]:
    active_hostiles = [probe.MINION_NAME] if other_hostile else []
    combatants: dict[str, object] = {
        probe.BOSS_NAME: {"hp": 0, "max_hp": 120}
    }
    if other_hostile:
        combatants[probe.MINION_NAME] = {"hp": 20, "max_hp": 40}
    return {
        "turn_id": "escape-trigger",
        "state_before": {
            "current_stage": 0,
            "ultima": 5,
            "hp": {probe.BOSS_NAME: 1},
            "combatants": {probe.BOSS_NAME: {"hp": 1, "max_hp": 120}},
        },
        "state_after": {
            "active": other_hostile,
            "current_stage": 0,
            "ultima": 4,
            "hp": {probe.BOSS_NAME: 0},
            "combatants": combatants,
            "defeated": [],
            "escaped": [probe.BOSS_NAME],
            "surrendered": [],
            "resolution_status": {"active_hostiles": active_hostiles},
        },
        "authoritative_resolutions": [
            {
                "ok": True,
                "action": {
                    "action_type": "Attack",
                    "parameters": {"target": probe.BOSS_NAME},
                },
                "payload": {
                    "actual_hp_loss": 1,
                    "conflict_event": {
                        "target": probe.BOSS_NAME,
                        "event_type": "villain_escape",
                        "summary": "消耗1点终结点逃脱。",
                        "ultima_spent": 1,
                        "hp_after": 0,
                    },
                },
            }
        ],
    }


def test_phase_and_minor_villain_ultima_audit_require_public_state_evidence() -> None:
    phase_turn = {
        "turn_id": "phase",
        "state_before": {
            "current_stage": -1,
            "ultima": 5,
            "hp": {probe.BOSS_NAME: 2},
            "combatants": {
                probe.BOSS_NAME: {"hp": 2, "max_hp": 120, "mp": 12, "max_mp": 70}
            },
        },
        "state_after": {
            "current_stage": 0,
            "hp": {probe.BOSS_NAME: 120},
            "combatants": {
                probe.BOSS_NAME: {
                    "hp": 120,
                    "max_hp": 120,
                    "mp": 70,
                    "max_mp": 70,
                    "temporary_affinities": {"fire": "resist"},
                }
            },
            "defeated": [],
            "escaped": [],
            "ultima": 5,
        },
        "authoritative_resolutions": [
            {
                "ok": True,
                "action": {"action_type": "Attack", "parameters": {}},
                "rules_text": "赤炉大将进入新阶段。",
                "payload": {
                    "actual_hp_loss": 2,
                    "conflict_event": {
                        "target": probe.BOSS_NAME,
                        "event_type": "boss_phase",
                        "summary": "火焰在新外壳上滑散，公开显示它已获得火系抵抗。",
                        "hp_after": 120,
                        "mp_after": 70,
                    }
                },
            }
        ],
    }
    escape_turn = {
        "turn_id": "escape",
        "state_before": {
            "current_stage": 0,
            "ultima": 5,
            "hp": {probe.BOSS_NAME: 1},
            "combatants": {probe.BOSS_NAME: {"hp": 1, "max_hp": 120}},
        },
        "state_after": {
            "current_stage": 0,
            "hp": {probe.BOSS_NAME: 0},
            "combatants": {probe.BOSS_NAME: {"hp": 0, "max_hp": 120}},
            "defeated": [],
            "escaped": [probe.BOSS_NAME],
            "ultima": 4,
            "active": False,
            "resolution_status": {"active_hostiles": []},
        },
        "authoritative_resolutions": [
            {
                "ok": True,
                "action": {"action_type": "Attack", "parameters": {}},
                "payload": {
                    "actual_hp_loss": 1,
                    "conflict_event": {
                        "target": probe.BOSS_NAME,
                        "event_type": "villain_escape",
                        "summary": "消耗1点终结点逃脱。",
                        "ultima_spent": 1,
                        "hp_after": 0,
                    }
                },
            }
        ],
    }

    audit = probe.audit_authoritative_rules([phase_turn, escape_turn])

    assert audit["phase_checks"][0]["status"] == "passed"
    assert audit["terminal_ultima_check"]["status"] == "passed"
    assert audit["status"] == "passed"


@pytest.mark.parametrize(
    ("marker", "value"),
    [
        ("check_result_provisional", True),
        ("action_uncommitted", True),
        ("held_action", {"reason": "awaiting decision"}),
        ("turn_held_for_decision", True),
    ],
)
def test_boss_zero_hp_trigger_ignores_unsettled_authoritative_records(
    marker: str,
    value: object,
) -> None:
    turn = _phase_zero_hp_trigger_turn()
    record = turn["authoritative_resolutions"][0]
    record["payload"][marker] = value

    audit = probe.audit_authoritative_rules([turn])

    assert audit["phase_checks"] == []
    assert audit["terminal_ultima_check"]["status"] == "unknown"
    assert audit["status"] == "unknown"


def test_boss_stage_change_without_positive_to_zero_crossing_is_inconclusive() -> None:
    turn = _phase_zero_hp_trigger_turn()
    turn["authoritative_resolutions"][0]["payload"]["actual_hp_loss"] = 1

    audit = probe.audit_authoritative_rules([turn])

    assert audit["phase_checks"] == []
    assert audit["terminal_ultima_check"]["status"] == "unknown"
    assert audit["status"] == "unknown"


@pytest.mark.parametrize(
    ("broken_invariant", "expected_failed_check"),
    [
        ("stage", "stage_minus_one_to_zero_same_transaction"),
        ("event", "boss_phase_event_same_resolution"),
        ("cue", "public_phase_cue"),
        ("hp", "full_hp_restore"),
        ("mp", "full_mp_restore"),
        ("defeated", "not_prematurely_defeated"),
        ("affinity", "fire_affinity_state"),
    ],
)
def test_stage_zero_hp_trigger_fails_each_missing_phase_invariant(
    broken_invariant: str,
    expected_failed_check: str,
) -> None:
    turn = _phase_zero_hp_trigger_turn()
    after = turn["state_after"]
    boss = after["combatants"][probe.BOSS_NAME]
    payload = turn["authoritative_resolutions"][0]["payload"]
    if broken_invariant == "stage":
        after["current_stage"] = -1
    elif broken_invariant == "event":
        payload["conflict_event"] = {}
    elif broken_invariant == "cue":
        payload["conflict_event"]["summary"] = "炉心形态出现。"
    elif broken_invariant == "hp":
        boss["hp"] = 119
    elif broken_invariant == "mp":
        boss["mp"] = 69
    elif broken_invariant == "defeated":
        after["defeated"] = [probe.BOSS_NAME]
    elif broken_invariant == "affinity":
        boss["temporary_affinities"] = {}

    audit = probe.audit_authoritative_rules([turn])
    checks = {
        item["name"]: item["status"]
        for item in audit["phase_checks"][0]["checks"]
    }

    assert checks["positive_hp_crossed_zero"] == "passed"
    assert checks[expected_failed_check] == "failed"
    assert audit["phase_checks"][0]["status"] == "failed"
    assert audit["status"] == "failed"


@pytest.mark.parametrize(
    ("broken_invariant", "expected_failed_check"),
    [
        ("event", "villain_escape_event_same_resolution"),
        ("event_spend", "one_ultima_spent_on_zero_hp_escape"),
        ("ultima_delta", "minor_villain_ultima_exactly_five_to_four"),
        ("escaped", "boss_entered_escaped_state"),
        ("inactive", "conflict_activity_after_escape"),
        ("remaining_hostile_closed", "conflict_activity_after_escape"),
    ],
)
def test_terminal_zero_hp_trigger_fails_each_escape_invariant(
    broken_invariant: str,
    expected_failed_check: str,
) -> None:
    turn = _terminal_zero_hp_trigger_turn()
    after = turn["state_after"]
    payload = turn["authoritative_resolutions"][0]["payload"]
    if broken_invariant == "event":
        payload["conflict_event"] = {}
    elif broken_invariant == "event_spend":
        payload["conflict_event"]["ultima_spent"] = 0
    elif broken_invariant == "ultima_delta":
        after["ultima"] = 3
    elif broken_invariant == "escaped":
        after["escaped"] = []
    elif broken_invariant == "inactive":
        after["active"] = True
    elif broken_invariant == "remaining_hostile_closed":
        after["combatants"][probe.MINION_NAME] = {"hp": 20, "max_hp": 40}
        after["resolution_status"]["active_hostiles"] = [probe.MINION_NAME]
        after["active"] = False

    audit = probe.audit_authoritative_rules([turn])
    terminal = audit["terminal_ultima_check"]
    checks = {item["name"]: item["status"] for item in terminal["checks"]}

    assert checks["positive_hp_crossed_zero"] == "passed"
    assert checks[expected_failed_check] == "failed"
    assert terminal["status"] == "failed"
    assert audit["status"] == "failed"


def test_terminal_escape_may_leave_conflict_active_with_another_hostile() -> None:
    turn = _terminal_zero_hp_trigger_turn(other_hostile=True)

    audit = probe.audit_authoritative_rules([turn])
    terminal = audit["terminal_ultima_check"]
    activity = next(
        item
        for item in terminal["checks"]
        if item["name"] == "conflict_activity_after_escape"
    )

    assert activity["status"] == "passed"
    assert activity["observed_active"] is True
    assert activity["remaining_hostiles"] == [probe.MINION_NAME]
    assert terminal["status"] == "passed"
    assert audit["status"] == "passed"


@pytest.mark.parametrize(
    ("error", "status", "body", "category"),
    [
        (TimeoutError("timed out"), None, {}, "PROVIDER_TIMEOUT"),
        (RuntimeError("empty response"), None, {}, "PROVIDER_EMPTY_RESPONSE"),
        (None, 502, {}, "PROVIDER_HTTP"),
        (ValueError("invalid JSON schema"), None, {}, "PROVIDER_SCHEMA"),
        (None, 200, {"agent_error": "tool missing"}, "MODEL_TOOL_MISSING"),
        (RuntimeError("FIXTURE_INVALID: hp"), None, {}, "FIXTURE_INVALID"),
    ],
)
def test_error_classification_is_explicit(
    error: BaseException | None,
    status: int | None,
    body: dict[str, object],
    category: str,
) -> None:
    assert (
        probe.classify_error(error, http_status=status, response=body) == category
    )
    assert category in probe.ERROR_CATEGORIES


@pytest.mark.parametrize(
    ("body", "category"),
    [
        (
            {"ok": True, "provider_error_category": "rate_limit"},
            "PROVIDER_HTTP",
        ),
        (
            {"ok": True, "provider_error_category": "upstream_reset"},
            "PROVIDER_HTTP",
        ),
        (
            {"ok": True, "agent_error": "tool missing"},
            "MODEL_TOOL_MISSING",
        ),
        (
            {"ok": True, "agent_loop": {"terminal_reason": "deadline"}},
            "PROVIDER_TIMEOUT",
        ),
        (
            {"ok": True, "agent_loop": {"terminal_reason": "unresolved"}},
            "MODEL_TOOL_MISSING",
        ),
    ],
)
def test_ok_http_envelope_does_not_hide_agent_or_provider_failure(
    body: dict[str, object], category: str
) -> None:
    assert (
        probe.response_error_category(None, http_status=200, response=body)
        == category
    )

    completed = {
        "ok": True,
        "agent_error": "",
        "provider_error_category": "",
        "agent_loop": {"terminal_reason": "completed"},
    }
    assert (
        probe.response_error_category(None, http_status=200, response=completed)
        == ""
    )


def test_owned_skill_reviewer_rejection_is_not_model_tool_missing() -> None:
    response = {
        "ok": True,
        "reply": "你的技能列表里并没有利刃风暴。",
        "agent_loop": {"terminal_reason": "completed"},
        "tool_trace": [
            {
                "decision": "call_tool",
                "tool_name": "perform_character_action",
                "protocol_error": "SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED",
            }
        ],
        "tool_receipts": [],
    }
    state_before = {
        "actor": probe.FRONT_HERO,
        "combatants": {
            probe.FRONT_HERO: {"skills": {"利刃风暴": 1}}
        },
    }

    category, detail = probe.turn_response_error(
        None,
        http_status=200,
        response=response,
        state_before=state_before,
        requested_message="诺艾尔使用利刃风暴攻击赤炉大将。",
    )

    assert category == "REVIEWER_FALSE_REJECTION"
    assert category != "MODEL_TOOL_MISSING"
    assert detail == {
        "actor": probe.FRONT_HERO,
        "requested_owned_skills": ["利刃风暴"],
        "reviewer_protocol_error": "SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED",
        "reviewer_rejection_count": 1,
        "reply": "你的技能列表里并没有利刃风暴。",
    }


def _known_spell_semantic_response(
    *,
    spell_name: str = "元素幕障",
    element: str = "火",
    targets: list[str] | None = None,
    provider_category: str = "",
) -> dict[str, object]:
    proposed_targets = targets or [probe.FRONT_HERO, probe.SUPPORT_HERO]
    return {
        "ok": True,
        "reply": "星澜目前没有已登记或公开的元素幕障法术。",
        "provider_error_category": provider_category,
        "agent_loop": {"terminal_reason": "completed"},
        "tool_trace": [
            {
                "decision": "call_tool",
                "tool_name": "perform_character_action",
                "arguments": {
                    "action_type": "Spell",
                    "actor": probe.SUPPORT_HERO,
                    "target": proposed_targets[0],
                    "timing": "immediate",
                    "details": {
                        "spell_name": spell_name,
                        "element": element,
                        "targets": proposed_targets,
                    },
                },
                "protocol_error": "SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED",
            },
            {
                "decision": "call_tool",
                "tool_name": "perform_character_action",
                "arguments": {
                    "action_type": "Spell",
                    "actor": probe.SUPPORT_HERO,
                    "target": proposed_targets[0],
                    "timing": "immediate",
                    "details": {
                        "spell_name": spell_name,
                        "element": element,
                        "targets": proposed_targets,
                    },
                },
                "protocol_error": "SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED",
            },
        ],
        "tool_receipts": [],
    }


def _known_spell_state_before() -> dict[str, object]:
    return {
        "actor": probe.SUPPORT_HERO,
        "combatants": {
            probe.FRONT_HERO: {},
            probe.SUPPORT_HERO: {
                "skills": {"元素魔法": 2},
                "spells": ["元素幕障", "炎弹"],
            },
        },
    }


def test_known_spell_reviewer_rejection_is_not_model_tool_missing() -> None:
    message = (
        f"{probe.SUPPORT_HERO}施放元素幕障，选择火元素，保护"
        f"{probe.FRONT_HERO}和{probe.SUPPORT_HERO}。"
    )
    response = _known_spell_semantic_response()

    category, detail = probe.turn_response_error(
        None,
        http_status=200,
        response=response,
        state_before=_known_spell_state_before(),
        requested_message=message,
    )

    assert category == "REVIEWER_FALSE_REJECTION"
    assert category != "MODEL_TOOL_MISSING"
    assert detail is not None
    assert detail["grounded_capability"] == "known_spell_cast"
    assert detail["requested_owned_spells"] == ["元素幕障"]
    assert detail["proposed_spell"] == "元素幕障"
    assert detail["proposed_element"] == "火"
    assert detail["proposed_targets"] == [
        probe.FRONT_HERO,
        probe.SUPPORT_HERO,
    ]
    assert detail["reviewer_rejection_count"] == 2
    assert detail["grounded_rejection_count"] == 2
    assert all(detail["message_argument_mapping"].values())


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            _known_spell_semantic_response(spell_name="冰封术"),
            f"{probe.SUPPORT_HERO}施放冰封术保护"
            f"{probe.FRONT_HERO}和{probe.SUPPORT_HERO}。",
        ),
        (
            _known_spell_semantic_response(element="冰"),
            f"{probe.SUPPORT_HERO}施放元素幕障，选择火元素，保护"
            f"{probe.FRONT_HERO}和{probe.SUPPORT_HERO}。",
        ),
        (
            _known_spell_semantic_response(
                targets=[probe.FRONT_HERO, probe.BOSS_NAME]
            ),
            f"{probe.SUPPORT_HERO}施放元素幕障，选择火元素，保护"
            f"{probe.FRONT_HERO}和{probe.SUPPORT_HERO}。",
        ),
    ],
)
def test_known_spell_false_rejection_detector_rejects_ungrounded_mapping(
    response: dict[str, object],
    message: str,
) -> None:
    category, detail = probe.turn_response_error(
        None,
        http_status=200,
        response=response,
        state_before=_known_spell_state_before(),
        requested_message=message,
    )

    assert category == ""
    assert detail is None


def test_concrete_provider_failure_is_not_hidden_by_known_spell_evidence() -> None:
    message = (
        f"{probe.SUPPORT_HERO}施放元素幕障，选择火元素，保护"
        f"{probe.FRONT_HERO}和{probe.SUPPORT_HERO}。"
    )
    response = _known_spell_semantic_response(provider_category="rate_limit")

    category, detail = probe.turn_response_error(
        None,
        http_status=200,
        response=response,
        state_before=_known_spell_state_before(),
        requested_message=message,
    )

    assert category == "PROVIDER_HTTP"
    assert detail is not None


def _same_target_dual_wield_semantic_response(
    *,
    provider_category: str = "unknown",
    targets: list[str] | None = None,
) -> dict[str, object]:
    proposed_targets = targets or [probe.BOSS_NAME, probe.BOSS_NAME]
    return {
        "ok": True,
        "reply": "本轮最后仍未通过事实一致性审校。",
        "agent_error": (
            "GM工具循环达到最大次数。；SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED"
        ),
        "provider_error_category": provider_category,
        "agent_loop": {"terminal_reason": "iteration_exhausted"},
        "tool_trace": [
            {
                "decision": "call_tool",
                "tool_name": "perform_character_action",
                "arguments": {
                    "action_type": "Attack",
                    "actor": probe.SUPPORT_HERO,
                    "target": proposed_targets[0],
                    "timing": "immediate",
                    "details": {
                        "dual_wield": True,
                        "targets": proposed_targets,
                    },
                },
                "protocol_error": "SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED",
            }
        ],
        "tool_receipts": [],
    }


def _dual_wield_state_before() -> dict[str, object]:
    return {
        "actor": probe.SUPPORT_HERO,
        "combatants": {
            probe.SUPPORT_HERO: {
                "skills": {},
                "equipment": ["无防具", "晨星匕首", "暮影匕首"],
                "equipped_main_hand": "晨星匕首",
                "equipped_off_hand": "暮影匕首",
            },
            probe.BOSS_NAME: {},
            probe.MINION_NAME: {},
        },
        "resolution_status": {
            "active_hostiles": [probe.BOSS_NAME, probe.MINION_NAME],
        },
    }


def test_same_target_dual_wield_reviewer_rejection_overrides_unknown_provider_label(
) -> None:
    message = (
        f"{probe.SUPPORT_HERO}这次不施法，改用双武器攻击：主手晨星匕首和"
        f"副手暮影匕首都攻击{probe.BOSS_NAME}。这是一个完整Attack动作。"
    )
    response = _same_target_dual_wield_semantic_response()

    # The outer helper sees the coordinator's ambiguous ``unknown`` label;
    # turn-level evidence must replace it with the concrete reviewer cause.
    assert (
        probe.response_error_category(None, http_status=200, response=response)
        == "PROVIDER_HTTP"
    )
    category, detail = probe.turn_response_error(
        None,
        http_status=200,
        response=response,
        state_before=_dual_wield_state_before(),
        requested_message=message,
    )

    assert category == "REVIEWER_FALSE_REJECTION"
    assert detail is not None
    assert detail["grounded_capability"] == "dual_wield"
    assert detail["authoritative_loadout"] == {
        "main_hand": "晨星匕首",
        "off_hand": "暮影匕首",
    }
    assert detail["proposed_targets"] == [probe.BOSS_NAME, probe.BOSS_NAME]
    assert detail["same_target"] is True
    assert detail["grounded_rejection_count"] == 1
    assert all(detail["message_argument_mapping"].values())


def test_dual_wield_false_rejection_detector_does_not_accept_changed_second_target(
) -> None:
    message = (
        f"{probe.SUPPORT_HERO}用双武器攻击，主手晨星匕首和副手暮影匕首"
        f"都攻击{probe.BOSS_NAME}。"
    )
    response = _same_target_dual_wield_semantic_response(
        targets=[probe.BOSS_NAME, probe.MINION_NAME]
    )

    category, detail = probe.turn_response_error(
        None,
        http_status=200,
        response=response,
        state_before=_dual_wield_state_before(),
        requested_message=message,
    )

    assert category == "PROVIDER_HTTP"
    assert detail is None


def test_concrete_provider_failure_is_not_hidden_by_dual_wield_reviewer_evidence(
) -> None:
    message = (
        f"{probe.SUPPORT_HERO}用双武器攻击，主手晨星匕首和副手暮影匕首"
        f"都攻击{probe.BOSS_NAME}。"
    )
    response = _same_target_dual_wield_semantic_response(
        provider_category="rate_limit"
    )

    category, detail = probe.turn_response_error(
        None,
        http_status=200,
        response=response,
        state_before=_dual_wield_state_before(),
        requested_message=message,
    )

    assert category == "PROVIDER_HTTP"
    assert detail is not None


def test_semantic_rejection_without_authoritative_ownership_is_not_called_false() -> None:
    response = {
        "ok": True,
        "agent_loop": {"terminal_reason": "completed"},
        "tool_trace": [
            {"protocol_error": "SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED"}
        ],
    }

    category, detail = probe.turn_response_error(
        None,
        http_status=200,
        response=response,
        state_before={
            "actor": probe.FRONT_HERO,
            "combatants": {probe.FRONT_HERO: {"skills": {}}},
        },
        requested_message="诺艾尔使用不存在的技能。",
    )

    assert category == ""
    assert detail is None


def test_reviewer_rejection_recovered_by_successful_receipt_is_not_terminal() -> None:
    response = {
        "ok": True,
        "agent_loop": {"terminal_reason": "completed"},
        "tool_trace": [
            {
                "tool_name": "declare_check_action",
                "protocol_error": "SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED",
            },
            {
                "decision": "call_tool",
                "tool_name": "declare_check_action",
                "receipt": {"tool_name": "declare_check_action", "ok": True},
            },
        ],
        "tool_receipts": [
            {
                "tool_name": "declare_check_action",
                "ok": True,
                "rolled_back": False,
            }
        ],
        "reply": "检定已声明，要投吗？",
    }

    category, detail = probe.turn_response_error(
        None,
        http_status=200,
        response=response,
        state_before={
            "actor": probe.SUPPORT_HERO,
            "combatants": {
                probe.SUPPORT_HERO: {"skills": {"知识就是力量": 1}}
            },
        },
        requested_message="星澜利用知识就是力量分析炉心结构。",
    )

    assert category == ""
    assert detail is None


def test_unrelated_success_does_not_mask_owned_skill_reviewer_rejection() -> None:
    response = {
        "ok": True,
        "agent_loop": {"terminal_reason": "completed"},
        "tool_trace": [
            {
                "tool_name": "perform_character_action",
                "protocol_error": "SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED",
            },
            {
                "tool_name": "inspect_supervisor_state",
                "receipt": {"tool_name": "inspect_supervisor_state", "ok": True},
            },
        ],
        "tool_receipts": [
            {"tool_name": "inspect_supervisor_state", "ok": True}
        ],
    }

    category, detail = probe.turn_response_error(
        None,
        http_status=200,
        response=response,
        state_before={
            "actor": probe.FRONT_HERO,
            "combatants": {
                probe.FRONT_HERO: {"skills": {"利刃风暴": 1}}
            },
        },
        requested_message="诺艾尔发动利刃风暴。",
    )

    assert category == "REVIEWER_FALSE_REJECTION"
    assert detail is not None
    assert detail["reviewer_rejection_count"] == 1


def test_identical_rule_action_retries_are_classified_as_stuck_model_loop() -> None:
    arguments = {
        "action_type": "Attack",
        "actor": probe.FRONT_HERO,
        "target": probe.BOSS_NAME,
        "details": {
            "skill_name": "利刃风暴",
            "dual_wield": True,
            "targets": [probe.BOSS_NAME, probe.MINION_NAME],
        },
    }
    response = {
        "ok": True,
        "agent_error": "工具连续三次未能完成事务。",
        "agent_loop": {"terminal_reason": "unresolved"},
        "tool_trace": [
            {
                "iteration": iteration,
                "decision": "call_tool",
                "tool_name": "perform_character_action",
                "arguments": arguments,
            }
            for iteration in range(1, 5)
        ],
    }
    receipt_failures = [
        {
            "tool_name": "perform_character_action",
            "error_code": "RULE_ACTION_REJECTED",
            "message": "双武器战斗中的主手【符文盾】不是可识别武器。",
            "correction_hint": "根据错误信息修正action参数。",
        }
        for _ in range(3)
    ]

    evidence = probe.repeated_rule_action_rejection(
        response,
        receipt_failures=receipt_failures,
    )
    category, reviewer_detail = probe.turn_response_error(
        None,
        http_status=200,
        response=response,
        state_before={
            "actor": probe.FRONT_HERO,
            "combatants": {probe.FRONT_HERO: {"skills": {"利刃风暴": 1}}},
        },
        requested_message="诺艾尔发动职业技能【利刃风暴】。",
        receipt_failures=receipt_failures,
    )

    assert category == "MODEL_RULE_RETRY_STUCK"
    assert reviewer_detail is None
    assert evidence is not None
    assert evidence["identical_attempt_count"] == 4
    assert evidence["unchanged_retry_count"] == 3
    assert evidence["rule_error_code"] == "RULE_ACTION_REJECTED"
    assert evidence["rule_message"] == receipt_failures[0]["message"]


def test_corrected_rule_retry_remains_a_rules_receipt_error() -> None:
    response = {
        "ok": True,
        "agent_loop": {"terminal_reason": "completed"},
        "tool_trace": [
            {
                "decision": "call_tool",
                "tool_name": "perform_character_action",
                "arguments": {"details": {"dual_wield": True}},
            },
            {
                "decision": "call_tool",
                "tool_name": "perform_character_action",
                "arguments": {"details": {"dual_wield": True}},
            },
            {
                "decision": "call_tool",
                "tool_name": "perform_character_action",
                "arguments": {"details": {"skill_name": "利刃风暴"}},
            },
        ],
    }
    receipt_failures = [
        {
            "tool_name": "perform_character_action",
            "error_code": "RULE_ACTION_REJECTED",
            "message": "规则参数不合法。",
            "correction_hint": "修正参数。",
        }
        for _ in range(3)
    ]

    category, _detail = probe.turn_response_error(
        None,
        http_status=200,
        response=response,
        state_before={},
        requested_message="",
        receipt_failures=receipt_failures,
    )

    assert probe.repeated_rule_action_rejection(
        response,
        receipt_failures=receipt_failures,
    ) is None
    assert category == "RULE_RECEIPT_ERROR"


def test_production_digest_detects_change_without_exposing_content(
    tmp_path: Path,
) -> None:
    campaign = tmp_path / "campaign-a"
    campaign.mkdir()
    snapshot = campaign / "snapshot.json"
    snapshot.write_text('{"secret":"alpha"}', encoding="utf-8")

    before = probe.production_authority_digest(tmp_path)
    snapshot.write_text('{"secret":"beta"}', encoding="utf-8")
    after = probe.production_authority_digest(tmp_path)

    assert before["sha256"] != after["sha256"]
    assert "alpha" not in json.dumps(before)
    assert "beta" not in json.dumps(after)


def test_turn_summary_enforces_boss_alternation_and_three_actions() -> None:
    def settled_npc_act(actor: str) -> list[dict[str, object]]:
        return [
            {
                "ok": True,
                "action": {
                    "action_type": "NPCAct",
                    "parameters": {"actor": actor},
                },
            }
        ]

    turns = [
        {
            "turn_id": "1",
            "round_before": 1,
            "actor_before": probe.BOSS_NAME,
            "action_completed": True,
            "authoritative_resolutions": settled_npc_act(probe.BOSS_NAME),
        },
        {"turn_id": "2", "round_before": 1, "actor_before": probe.FRONT_HERO, "action_completed": True},
        {
            "turn_id": "3",
            "round_before": 1,
            "actor_before": probe.BOSS_NAME,
            "action_completed": True,
            "authoritative_resolutions": settled_npc_act(probe.BOSS_NAME),
        },
        {"turn_id": "4", "round_before": 1, "actor_before": probe.SUPPORT_HERO, "action_completed": True},
        {
            "turn_id": "5",
            "round_before": 1,
            "actor_before": probe.BOSS_NAME,
            "action_completed": True,
            "authoritative_resolutions": settled_npc_act(probe.BOSS_NAME),
        },
        {
            "turn_id": "6",
            "round_before": 1,
            "actor_before": probe.BOSS_NAME,
            "action_completed": True,
            "authoritative_resolutions": [
                {
                    "ok": True,
                    "action": {
                        "action_type": "TriggerOpportunity",
                        "parameters": {"actor": probe.FRONT_HERO},
                    },
                }
            ],
        },
        {
            "turn_id": "7",
            "round_before": 1,
            "actor_before": probe.BOSS_NAME,
            "action_completed": True,
            "authoritative_resolutions": [
                {
                    "ok": True,
                    "action": {
                        "action_type": "NPCAct",
                        "parameters": {"actor": probe.BOSS_NAME},
                    },
                    "payload": {"check_result_provisional": True},
                }
            ],
        },
    ]

    result = probe.summarize_turn_order(turns)

    assert result["alternation_passed"] is True
    assert result["three_action_round_observed"] is True
    assert result["boss_actions_by_round"] == {1: 3}
    assert result["completed_turns"][-1]["success"] is False
    assert result["completed_turns"][-2]["success"] is False


def test_turn_summary_reports_consecutive_boss_actions() -> None:
    result = probe.summarize_turn_order(
        [
            {"turn_id": "1", "round_before": 1, "actor_before": probe.BOSS_NAME, "action_completed": True},
            {"turn_id": "2", "round_before": 1, "actor_before": probe.BOSS_NAME, "action_completed": True},
        ]
    )

    assert result["alternation_passed"] is False
    assert result["illegal_consecutive_boss_actions"] == [
        {"left": "1", "right": "2"}
    ]

    exhausted_round = probe.summarize_turn_order(
        [
            {
                "turn_id": "3",
                "round_before": 1,
                "actor_before": probe.BOSS_NAME,
                "action_completed": True,
                "state_before": {"players_can_still_act_this_round": False},
            },
            {
                "turn_id": "4",
                "round_before": 1,
                "actor_before": probe.BOSS_NAME,
                "action_completed": True,
                "state_before": {"players_can_still_act_this_round": False},
            },
        ]
    )
    assert exhausted_round["alternation_passed"] is True


def test_latency_summary_reports_http_provider_and_cache_fields() -> None:
    summary = probe.latency_summary(
        [
            {"http_wall_ms": 1200},
            {"http_wall_ms": 800},
            {"http_wall_ms": 2000},
        ],
        [
            {
                "elapsed_ms": 700,
                "ok": True,
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "cached_tokens": 80,
                    "cache_miss_tokens": 20,
                    "cache_usage_reported": True,
                },
            },
            {
                "elapsed_ms": 400,
                "ok": True,
                "usage": {
                    "prompt_tokens": 50,
                    "completion_tokens": 10,
                    "cached_tokens": 0,
                    "cache_miss_tokens": 50,
                    "cache_usage_reported": True,
                },
            },
        ],
    )

    assert summary["http_total_ms"] == 4000
    assert summary["http_p50_ms"] == 1200
    assert summary["http_p90_ms"] == 2000
    assert summary["provider_total_ms"] == 1100
    assert summary["cache"]["cached_tokens"] == 80
    assert summary["cache"]["cache_token_hit_rate"] == pytest.approx(80 / 150)


def test_offline_run_writes_private_artifacts_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("offline validation attempted network access")

    monkeypatch.setattr(probe, "request_json", unexpected_network)
    monkeypatch.setattr(probe, "_production_snapshot", unexpected_network)
    args = probe.parse_args(["--output-root", str(tmp_path)])
    output = tmp_path / "offline"

    result = probe.run_offline(args, output)

    assert result["status"] == "passed"
    assert result["provider_call_count"] == 0
    expected = {
        "summary.json",
        "turns.jsonl",
        "rolls.jsonl",
        "skill_matrix.json",
        "capability_matrix.json",
        "rules_audit.json",
        "secret_scan.json",
    }
    assert {path.name for path in output.iterdir()} == expected
    assert os.stat(output).st_mode & 0o777 == 0o700
    assert all(os.stat(output / name).st_mode & 0o777 == 0o600 for name in expected)
    scan = json.loads((output / "secret_scan.json").read_text(encoding="utf-8"))
    assert scan["passed"] is True


def test_script_contains_no_selectable_seed_or_test_rng_assignment() -> None:
    source = (SCRIPTS / "probe_deepseek_boss_battle.py").read_text(encoding="utf-8")

    assert 'add_argument("--seed"' not in source
    assert "._rng =" not in source
    assert "Fake" + "Random" not in source
    assert "force_next_check_outcome(" not in source
    assert "resolution_records = install_resolution_capture(runtime)" in source
    assert '"authoritative_resolutions": resolution_slice' in source
    assert source.count("assert_no_pending_outcome_replay(runtime)") >= 3
    assert '"SKILL_EVIDENCE_MISSING",' in source
