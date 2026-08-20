from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from fu_gm.components.gm_intent_capability_router import (
    GMIntentCapabilityRouter,
)
from fu_gm.gm_tool_contracts import GMToolExecutionContext


ALL_TOOLS = {
    tool_name
    for profile in GMIntentCapabilityRouter.profiles()
    for tool_name in profile.tool_names
}


def _context(message: str, *, gate_status: str = "adventure") -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="风铃团",
        session_id="session-1",
        channel_id="group-1",
        speaker="玩家甲",
        gate_status=gate_status,
        metadata={"current_message": message},
    )


def _state() -> dict[str, object]:
    return {
        "scene": {
            "active": True,
            "scene_id": "prison-1",
            "participants": ["诺艾尔", "守卫长"],
        },
        "gameplay": {
            "controlled_characters": ["诺艾尔"],
            "player_character_aliases": {"玩家甲": ["诺艾尔"]},
            "characters": [{"name": "诺艾尔"}],
            "current_scene": {
                "name": "牢房",
                "location": "卡里巴村监狱",
                "participants": ["诺艾尔", "守卫长"],
            },
            "pending_decisions": [],
        },
        "npcs": {
            "present_npcs": [
                {
                    "name": "守卫长",
                    "aliases": ["守卫"],
                    "entity_kind": "individual",
                },
                # Simulate a corrupt legacy persona collision.  It must never
                # make a player-owned hero eligible for NPC speech tools.
                {
                    "name": "旧档案",
                    "aliases": ["诺艾尔"],
                    "entity_kind": "individual",
                },
            ],
            "known_npc_index": [],
            "relevant_npcs": [],
        },
        "turn_participants": {
            "controlled_characters_by_speaker": {"玩家甲": ["诺艾尔"]},
        },
        "processes": {"decisions": {"pending": []}},
    }


def _route(
    message: str,
    *,
    state: dict[str, object] | None = None,
    phase_tools: set[str] | None = None,
    registered_tools: set[str] | None = None,
    gate_status: str = "adventure",
):
    return GMIntentCapabilityRouter.route(
        _context(message, gate_status=gate_status),
        state if state is not None else _state(),
        ALL_TOOLS if phase_tools is None else phase_tools,
        ALL_TOOLS if registered_tools is None else registered_tools,
    )


@pytest.mark.parametrize(
    ("message", "profile_id", "expected_tool"),
    [
        ("我观察一下牢门。", "check_action", "declare_check_action"),
        ("诺艾尔走到牢门旁边。", "movement", "declare_movement_check"),
        ("请查询一下伤害规则怎么算。", "rule_read", "get_rule_reference"),
        ("请帮我保存存档。", "campaign_admin", "save_campaign"),
        ("结束当前场景，进入下一幕。", "scene_lifecycle", "end_scene"),
        ("诺艾尔攻击守卫长。", "conflict", "perform_character_action"),
        (
            "星澜施放元素幕障，选择火元素，保护诺艾尔和星澜。",
            "check_action",
            "perform_character_action",
        ),
    ],
)
def test_high_confidence_intents_choose_fixed_profiles(
    message: str,
    profile_id: str,
    expected_tool: str,
) -> None:
    plan = _route(message)

    assert plan.profile_ids == (profile_id,)
    assert expected_tool in plan.tool_names
    assert plan.confidence >= 0.9
    assert plan.fallback_discovery is False


def test_boss_dual_shield_message_with_fenbie_routes_to_conflict() -> None:
    plan = _route(
        "诺艾尔使用利刃风暴，以双盾分别攻击赤炉大将和熔炉侍从；"
        "这是当前回合的完整动作，请按真实骰子结算两个不同目标。"
    )

    assert plan.profile_ids == ("conflict",)
    assert "perform_character_action" in plan.tool_names
    assert plan.confidence >= 0.9
    assert plan.fallback_discovery is False


@pytest.mark.parametrize(
    "word",
    ["分别", "个别", "区别", "识别", "告别", "特别", "类别", "级别"],
)
def test_bie_inside_an_ordinary_word_is_not_a_negation(word: str) -> None:
    assert GMIntentCapabilityRouter._positive_term_hit(
        f"诺艾尔{word}攻击守卫长",
        ("攻击",),
    ) is True


def test_authoritative_blocking_decision_has_priority_over_message_keywords() -> None:
    state = _state()
    state["gameplay"]["pending_decisions"] = [  # type: ignore[index]
        {
            "window_id": "window-7",
            "kind": "check_roll_confirmation",
            "owner": "诺艾尔",
            "blocking": True,
            "allowed_responders": ["诺艾尔"],
        }
    ]

    plan = _route("我想先去问守卫。", state=state)

    assert plan.profile_ids == ("pending_window",)
    assert plan.subjects == ("诺艾尔",)
    assert plan.tool_names == ("get_gameplay_state", "resolve_rule_window")
    assert plan.confidence == 1.0


def test_authoritative_npc_entity_enables_npc_response_profile() -> None:
    plan = _route("我问守卫钥匙在哪里。")

    assert plan.profile_ids == ("npc_response",)
    assert plan.subjects == ("守卫长",)
    assert plan.tool_names == (
        "create_npc_profile",
        "decide_collective_response",
        "decide_npc_response",
        "get_npc_profiles",
    )
    assert "authority:mentioned_npc_entity" in plan.proofs


def test_player_character_collision_never_routes_as_npc() -> None:
    plan = _route("我告诉诺艾尔这件事。")

    assert "npc_response" not in plan.profile_ids
    assert "decide_npc_response" not in plan.tool_names
    assert plan.profile_ids == ("reply_only",)
    assert "authority:player_character_not_npc" in plan.proofs


def test_explicit_authoritative_confirmation_uses_reply_only_profile() -> None:
    plan = _route("时悠，请只用一句话确认第一章已经开始，不要改变状态。")

    assert plan.profile_ids == ("reply_only",)
    assert plan.tool_names == ()
    assert plan.fallback_discovery is False


def test_rule_word_inside_check_request_does_not_become_rule_lookup() -> None:
    plan = _route(
        "诺艾尔仔细观察牢门，寻找逃生线索。请按规则先声明一次洞察检定并等我确认，不要替我投骰。"
    )

    assert plan.profile_ids == ("check_declare",)
    assert "declare_check_action" in plan.tool_names
    assert plan.tool_names == ("declare_check_action",)
    assert "get_rule_reference" not in plan.tool_names


def test_unknown_person_is_not_invented_as_an_npc_subject() -> None:
    plan = _route("我问问神秘旅人钥匙在哪里。")

    assert plan.profile_ids == ("ambiguous_hot",)
    assert plan.subjects == ()
    assert plan.fallback_discovery is True


@pytest.mark.parametrize(
    "message",
    [
        "不要移动，我只是随口一说。",
        "我不是要攻击任何人。",
        "别攻击守卫长。",
        "星澜不施放元素幕障。",
        "不要查伤害规则。",
        "随便看看吧。",
    ],
)
def test_negated_or_ambiguous_messages_fall_back_conservatively(message: str) -> None:
    plan = _route(message)

    assert plan.profile_ids == ("ambiguous_hot",)
    assert plan.confidence < 0.5
    assert plan.fallback_discovery is True
    assert "discover_capabilities" in plan.tool_names


def test_no_authoritative_scene_uses_bootstrap_fallback() -> None:
    plan = _route("随便聊聊。", state={})

    assert plan.profile_ids == ("bootstrap",)
    assert plan.fallback_discovery is True
    assert plan.state_scopes == ("capability_catalog", "kernel", "speaker")


def test_tools_are_always_capped_by_phase_and_registry() -> None:
    phase_tools = {
        "declare_check_action",
        "perform_scene_action",
        "discover_capabilities",
        "not_registered",
    }
    registered_tools = {
        "declare_check_action",
        "perform_character_action",
        "discover_capabilities",
    }

    plan = _route(
        "我观察一下牢门。",
        phase_tools=phase_tools,
        registered_tools=registered_tools,
    )

    assert plan.profile_ids == ("check_action",)
    assert plan.tool_names == ("declare_check_action",)
    assert set(plan.tool_names) <= phase_tools & registered_tools


def test_unavailable_profile_degrades_only_to_allowed_discovery() -> None:
    plan = _route(
        "我观察一下牢门。",
        phase_tools={"discover_capabilities"},
        registered_tools={"discover_capabilities", "declare_check_action"},
    )

    assert plan.profile_ids == ("check_action",)
    assert plan.tool_names == ("discover_capabilities",)
    assert plan.fallback_discovery is True
    assert "fallback:selected_profile_unavailable" in plan.proofs


@pytest.mark.parametrize(
    ("message", "profile_id", "required", "excluded"),
    [
        (
            "界限：不要出现蜘蛛。",
            "session_zero_safety",
            {"record_safety_boundary"},
            {"create_world_setting", "update_hero_draft"},
        ),
        (
            "我赞成白河刚才的地图提案，就按白钟大陆。",
            "session_zero_proposal",
            {"confirm_session_zero_proposal", "propose_session_zero_update"},
            {"record_safety_boundary", "update_hero_draft"},
        ),
        (
            "我的角色主题我选责任，确认写进角色草稿。",
            "session_zero_hero",
            {"confirm_hero_draft", "update_hero_draft"},
            {"create_world_setting", "record_safety_boundary"},
        ),
        (
            "大家都同意，现在进入第一章。",
            "session_zero_opening",
            {"get_session_zero_readiness", "start_adventure"},
            {"create_world_setting", "update_hero_draft"},
        ),
        (
            "我贡献钟鸣公国，位于镜线内海北岸。",
            "session_zero_world",
            {"create_world_setting", "query_world_settings"},
            {"confirm_hero_draft", "record_safety_boundary"},
        ),
        (
            "让我想想，稍后回答，暂停提问。",
            "session_zero_nudge",
            {"pause_session_zero_nudges"},
            {"create_world_setting", "update_hero_draft"},
        ),
    ],
)
def test_session_zero_high_confidence_intents_use_fixed_profiles(
    message: str,
    profile_id: str,
    required: set[str],
    excluded: set[str],
) -> None:
    plan = _route(message, gate_status="session_zero")

    assert plan.profile_ids == (profile_id,)
    assert required <= set(plan.tool_names)
    assert set(plan.tool_names).isdisjoint(excluded)
    assert plan.confidence >= 0.94
    assert plan.fallback_discovery is False


def test_ambiguous_session_zero_message_keeps_the_legacy_hot_safety_net() -> None:
    plan = _route(
        "我有个想法，先听听大家。",
        gate_status="session_zero",
    )

    assert plan.profile_ids == ("session_zero_ambiguous",)
    assert plan.fallback_discovery is True
    assert {
        "create_world_setting",
        "confirm_session_zero_proposal",
        "record_safety_boundary",
        "update_hero_draft",
    } <= set(plan.tool_names)


@pytest.mark.parametrize(
    ("message", "profile_id"),
    [
        ("伊莉雅职业技能先选保镖。", "session_zero_hero"),
        (
            "我的玩家名是阿凛，角色名伊莉雅。职业分配：守护者3级。属性骰：敏捷d8。",
            "session_zero_hero",
        ),
        (
            "魔法和科技可以并存，灵魂晶炉驱动车辆和工坊。",
            "session_zero_world",
        ),
        ("职业技能规则怎么选？", "rule_read"),
    ],
)
def test_session_zero_profiles_cover_representative_setup_phrasing(
    message: str,
    profile_id: str,
) -> None:
    plan = _route(message, gate_status="session_zero")

    assert plan.profile_ids == (profile_id,)
    assert plan.fallback_discovery is False


def test_session_zero_multi_intent_unions_only_relevant_fixed_profiles() -> None:
    plan = _route(
        "我的国家正式定为岚国。地图要不要做成环形大陆，大家觉得呢？",
        gate_status="session_zero",
    )

    assert plan.profile_ids == (
        "session_zero_proposal",
        "session_zero_world",
    )
    assert {
        "confirm_session_zero_proposal",
        "create_world_setting",
        "propose_session_zero_update",
        "update_world_setting",
    } <= set(plan.tool_names)
    assert "update_hero_draft" not in plan.tool_names
    assert plan.confidence == 0.9


def test_session_zero_tone_discussion_does_not_select_adventure_conflict_tools() -> None:
    plan = _route(
        "我希望第一章至少有一场冲突不靠战斗解决，要靠证据和承诺。",
        gate_status="session_zero",
    )

    assert plan.profile_ids == ("session_zero_ambiguous",)
    assert "start_conflict" not in plan.tool_names
    assert "record_prologue_setup_answer" in plan.tool_names
    assert plan.fallback_discovery is True


def test_profile_catalog_and_route_output_are_stable_and_immutable() -> None:
    before_state = _state()
    context = _context("我走到守卫面前并问问他。")
    metadata_before = deepcopy(context.metadata)
    state_before = deepcopy(before_state)

    first = GMIntentCapabilityRouter.route(context, before_state, ALL_TOOLS, ALL_TOOLS)
    second = GMIntentCapabilityRouter.route(context, before_state, ALL_TOOLS, ALL_TOOLS)
    profiles = GMIntentCapabilityRouter.profiles()

    assert first == second
    assert first.profile_ids == tuple(sorted(first.profile_ids))
    assert first.tool_names == tuple(sorted(first.tool_names))
    assert first.state_scopes == tuple(sorted(first.state_scopes))
    assert first.proofs == tuple(sorted(first.proofs))
    assert tuple(profile.profile_id for profile in profiles) == tuple(
        sorted(profile.profile_id for profile in profiles)
    )
    assert context.metadata == metadata_before
    assert before_state == state_before
    with pytest.raises(FrozenInstanceError):
        first.confidence = 0.0  # type: ignore[misc]
