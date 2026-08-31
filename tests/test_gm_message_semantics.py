from __future__ import annotations

import pytest

from fu_gm.components.gm_message_semantics import (
    GMMessageSemantics,
    GMMessageSemanticsError,
    semantic_change_tool_names,
    tool_semantic_authority_error,
)


def source_events() -> list[dict[str, object]]:
    return [
        {
            "event_id": "event-loading",
            "speaker": "loading",
            "text": "要不我也试试看看能有多少人",
        },
        {
            "event_id": "event-villager",
            "speaker": "村夫",
            "text": "行",
        },
    ]


def semantics_payload() -> dict[str, object]:
    return {
        "version": "1",
        "events": [
            {
                "event_id": "event-loading",
                "speaker": "loading",
                "relation": "player",
                "targets": ["村夫"],
                "dialogue_act": "proposal",
                "action_commitment": "tentative",
                "responds_to_event_id": "",
                "reason": "向队友提出观察人数的方案。",
            },
            {
                "event_id": "event-villager",
                "speaker": "村夫",
                "relation": "player",
                "targets": ["loading"],
                "dialogue_act": "agreement",
                "action_commitment": "none",
                "responds_to_event_id": "event-loading",
                "reason": "紧接着同意队友刚提出的方案。",
            },
        ],
    }


def single_state_semantics(
    *,
    operation: str,
    scope: str,
    subject: str,
    target: str = "",
) -> GMMessageSemantics:
    event_id = f"event-{operation}-{scope}-{subject}"
    return GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "阿凛",
                    "relation": "gm",
                    "targets": ["时悠"],
                    "dialogue_act": (
                        "proposal" if operation == "propose" else "state_contribution"
                    ),
                    "action_commitment": (
                        "tentative" if operation == "propose" else "committed"
                    ),
                    "state_scope": scope,
                    "state_intents": [
                        {
                            "operation": operation,
                            "scope": scope,
                            "subject": subject,
                            "target": target,
                            "summary": "当前消息中的结构化状态含义",
                        }
                    ],
                    "responds_to_event_id": "",
                    "reason": "玩家向主持人表达一项状态意图。",
                }
            ],
        },
        source_events=[
            {
                "event_id": event_id,
                "speaker": "阿凛",
                "text": "测试消息原文",
            }
        ],
    )


def test_semantic_change_plan_distinguishes_tentative_hero_and_world_proposal() -> None:
    hero = single_state_semantics(
        operation="propose",
        scope="hero",
        subject="hero_profile",
        target="伊莉雅",
    )
    world = single_state_semantics(
        operation="propose",
        scope="world",
        subject="kingdoms",
        target="钟声王国",
    )

    assert semantic_change_tool_names(hero) == frozenset()
    assert semantic_change_tool_names(world) == frozenset(
        {"propose_session_zero_update", "query_world_settings"}
    )
    assert world.events[0].state_intents[0].target == "钟声王国"


def test_hero_field_agreement_cannot_become_full_draft_confirmation() -> None:
    with pytest.raises(GMMessageSemanticsError) as caught:
        single_state_semantics(
            operation="confirm",
            scope="hero",
            subject="hero_classes",
            target="赛璃",
        )

    assert caught.value.code == "MESSAGE_HERO_FIELD_CONFIRMATION_INVALID"


def test_explicit_full_hero_confirmation_exposes_only_confirmation_tools() -> None:
    semantics = single_state_semantics(
        operation="confirm",
        scope="hero",
        subject="hero_confirmation",
        target="赛璃",
    )

    assert semantic_change_tool_names(semantics) == frozenset(
        {"get_hero_drafts", "confirm_hero_draft"}
    )


def test_common_confirmation_enum_alias_is_normalized_at_protocol_boundary() -> None:
    event_id = "event-confirm-hero"
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "阿凛",
                    "relation": "gm",
                    "targets": ["时悠"],
                    "dialogue_act": "confirmation",
                    "action_commitment": "answer",
                    "state_scope": "hero",
                    "state_intents": [
                        {
                            "operation": "confirm",
                            "scope": "hero",
                            "subject": "hero_confirmation",
                            "target": "伊莉雅",
                            "summary": "确认按当前版本正式建卡",
                        }
                    ],
                    "responds_to_event_id": "",
                    "reason": "玩家确认整张角色草稿。",
                }
            ],
        },
        source_events=[
            {
                "event_id": event_id,
                "speaker": "阿凛",
                "text": "确认，就按这版正式建卡。",
            }
        ],
    )

    assert semantics.events[0].dialogue_act == "agreement"


@pytest.mark.parametrize(
    ("operation", "expected_tool"),
    [
        ("skip", "mark_session_zero_topic_complete"),
        ("defer", "pause_session_zero_nudges"),
    ],
)
def test_session_zero_focus_response_has_semantic_lifecycle_tool(
    operation: str,
    expected_tool: str,
) -> None:
    semantics = single_state_semantics(
        operation=operation,
        scope="world",
        subject="mysteries",
    )

    assert expected_tool in semantic_change_tool_names(semantics)


def test_session_zero_skip_tool_must_match_semantic_topic() -> None:
    semantics = single_state_semantics(
        operation="skip",
        scope="world",
        subject="mysteries",
    )

    allowed = tool_semantic_authority_error(
        tool_name="mark_session_zero_topic_complete",
        arguments={"topic": "mystery"},
        semantics=semantics,
    )
    rejected = tool_semantic_authority_error(
        tool_name="mark_session_zero_topic_complete",
        arguments={"topic": "threat"},
        semantics=semantics,
    )

    assert allowed is None
    assert rejected is not None
    assert rejected.code == "MESSAGE_SKIP_TOPIC_TOOL_MISMATCH"


def test_safety_skip_opens_completion_tool_without_creating_fake_boundary() -> None:
    semantics = single_state_semantics(
        operation="skip",
        scope="safety",
        subject="safety_boundary",
    )

    assert semantic_change_tool_names(semantics) == frozenset(
        {"mark_session_zero_topic_complete"}
    )
    assert tool_semantic_authority_error(
        tool_name="mark_session_zero_topic_complete",
        arguments={"topic": "safety"},
        semantics=semantics,
    ) is None
    rejected = tool_semantic_authority_error(
        tool_name="mark_session_zero_topic_complete",
        arguments={"topic": "mystery"},
        semantics=semantics,
    )
    assert rejected is not None
    assert rejected.code == "MESSAGE_SKIP_TOPIC_TOOL_MISMATCH"


def test_mixed_world_and_safety_message_has_two_explicit_state_scopes() -> None:
    event_id = "event-tone-and-safety"
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "阿凛",
                    "relation": "table",
                    "targets": [],
                    "dialogue_act": "state_contribution",
                    "action_commitment": "none",
                    "state_scope": "mixed",
                    "state_intents": [
                        {
                            "operation": "contribute",
                            "scope": "world",
                            "subject": "tone_preferences",
                            "summary": "史诗而有希望，代价存在但不绝望",
                        },
                        {
                            "operation": "contribute",
                            "scope": "safety",
                            "subject": "safety_boundary",
                            "summary": "避免纯粹恐怖与过度情感纠葛",
                        },
                    ],
                    "responds_to_event_id": "",
                    "reason": "同一句同时贡献基调并声明安全界限。",
                }
            ],
        },
        source_events=[
            {
                "event_id": event_id,
                "speaker": "阿凛",
                "text": "我想要史诗希望感，也希望避开纯粹恐怖。",
            }
        ],
    )

    assert semantics.events[0].state_scope == "mixed"
    assert {item.scope for item in semantics.events[0].state_intents} == {
        "world",
        "safety",
    }
    assert "record_safety_boundary" in semantic_change_tool_names(semantics)
    assert "create_world_setting" in semantic_change_tool_names(semantics)


def test_world_contribution_cannot_be_saved_as_tentative_proposal() -> None:
    semantics = single_state_semantics(
        operation="contribute",
        scope="world",
        subject="kingdoms",
        target="索朗帝国",
    )

    assert semantic_change_tool_names(semantics) == frozenset(
        {"create_world_setting", "update_world_setting", "query_world_settings"}
    )
    error = tool_semantic_authority_error(
        tool_name="propose_session_zero_update",
        arguments={},
        semantics=semantics,
    )

    assert error is not None
    assert error.code == "MESSAGE_STATE_INTENT_TOOL_MISMATCH"


def test_confirm_intent_binds_and_enforces_authoritative_proposal_id() -> None:
    event_id = "event-confirm-proposal"
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "阿凛",
                    "relation": "table",
                    "targets": ["南星"],
                    "dialogue_act": "agreement",
                    "action_commitment": "none",
                    "state_scope": "world",
                    "state_intents": [
                        {
                            "operation": "confirm",
                            "scope": "world",
                            "subject": "historical_events",
                            "target": "流浪钟匠的记忆钟事件",
                            "proposal_id": "proposal-memory-bell",
                            "summary": "赞成加入流浪钟匠留下记忆钟的事件",
                        }
                    ],
                    "responds_to_event_id": "",
                    "reason": "玩家明确赞成刚才的待定历史事件。",
                }
            ],
        },
        source_events=[
            {
                "event_id": event_id,
                "speaker": "阿凛",
                "text": "这个呼应很棒，我赞成加入这个事件。",
            }
        ],
    )

    intent = semantics.events[0].state_intents[0]
    assert intent.proposal_id == "proposal-memory-bell"
    error = tool_semantic_authority_error(
        tool_name="confirm_session_zero_proposal",
        arguments={
            "proposal_id": "proposal-other",
            "source_event_id": event_id,
        },
        semantics=semantics,
    )
    assert error is not None
    assert error.code == "MESSAGE_CONFIRM_PROPOSAL_TOOL_MISMATCH"


def test_world_correction_enforces_model_selected_category_and_target() -> None:
    semantics = single_state_semantics(
        operation="correct",
        scope="world",
        subject="kingdoms",
        target="索朗帝国",
    )

    category_error = tool_semantic_authority_error(
        tool_name="update_world_setting",
        arguments={"category": "factions", "name": "索朗帝国"},
        semantics=semantics,
    )
    target_error = tool_semantic_authority_error(
        tool_name="update_world_setting",
        arguments={"category": "kingdoms", "name": "自由城邦联盟"},
        semantics=semantics,
    )

    assert category_error is not None
    assert category_error.code == "MESSAGE_STATE_CATEGORY_TOOL_MISMATCH"
    assert target_error is not None
    assert target_error.code == "MESSAGE_STATE_TARGET_TOOL_MISMATCH"


def test_list_update_uses_exact_old_record_without_matching_human_target_label() -> None:
    semantics = single_state_semantics(
        operation="contribute",
        scope="world",
        subject="world_threats",
        target="记忆瘟疫",
    )
    old_record = (
        "随着记忆炉扩散，出现了记忆瘟疫；受害者会失去名字和过去。"
    )

    error = tool_semantic_authority_error(
        tool_name="update_world_setting",
        arguments={
            "category": "world_threats",
            "name": old_record,
            "value": old_record + "他们的记忆或许只是被锁住。",
        },
        semantics=semantics,
    )

    assert error is None


def test_list_fact_can_use_the_lossless_semantic_summary_as_storage_identity() -> None:
    event_id = "event-list-fact"
    complete = (
        "很久以前，人们从一片受侵蚀的古大陆渡海而来，"
        "把记忆和技艺带到了新土地。"
    )
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "南星",
                    "relation": "table",
                    "targets": ["时悠"],
                    "dialogue_act": "state_contribution",
                    "action_commitment": "none",
                    "state_scope": "world",
                    "state_intents": [
                        {
                            "operation": "contribute",
                            "scope": "world",
                            "subject": "historical_events",
                            "target": "大迁徙",
                            "summary": complete,
                        }
                    ],
                    "responds_to_event_id": "",
                    "reason": "玩家贡献一项历史事件。",
                }
            ],
        },
        source_events=[
            {"event_id": event_id, "speaker": "南星", "text": complete}
        ],
    )

    error = tool_semantic_authority_error(
        tool_name="update_world_setting",
        arguments={
            "category": "historical_events",
            "name": complete,
            "value": complete,
            "source_event_id": event_id,
        },
        semantics=semantics,
    )

    assert error is None


def test_session_zero_write_requires_structured_state_intent() -> None:
    event_id = "event-no-state-plan"
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "阿凛",
                    "relation": "gm",
                    "targets": ["时悠"],
                    "dialogue_act": "other",
                    "action_commitment": "none",
                    "responds_to_event_id": "",
                    "reason": "没有识别到持久状态变化。",
                }
            ],
        },
        source_events=[
            {"event_id": event_id, "speaker": "阿凛", "text": "先聊聊。"}
        ],
    )

    error = tool_semantic_authority_error(
        tool_name="create_world_setting",
        arguments={"category": "kingdoms", "name": "索朗帝国"},
        semantics=semantics,
    )

    assert error is not None
    assert error.code == "MESSAGE_STATE_INTENT_REQUIRED"


def test_semantics_preserve_each_speaker_and_response_relation() -> None:
    semantics = GMMessageSemantics.parse(
        semantics_payload(),
        source_events=source_events(),
    )

    assert semantics.events[1].relation == "player"
    assert semantics.events[1].dialogue_act == "agreement"
    assert semantics.events[1].responds_to_event_id == "event-loading"


def test_semantics_accept_explicit_request_without_recasting_it_as_question() -> None:
    event_id = "event-start-session-zero"
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "阿凛",
                    "relation": "gm",
                    "targets": ["时悠"],
                    "dialogue_act": "request",
                    "action_commitment": "none",
                    "responds_to_event_id": "",
                    "reason": "玩家请求GM开始第零章。",
                }
            ],
        },
        source_events=[
            {
                "event_id": event_id,
                "speaker": "阿凛",
                "text": "@时悠，请开始第零章。",
            }
        ],
    )

    assert semantics.events[0].dialogue_act == "request"


def test_group_scope_cannot_be_written_into_one_hero_draft() -> None:
    event_id = "event-group-origin"
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "南星",
                    "relation": "table",
                    "targets": ["时悠", "阿凛"],
                    "dialogue_act": "state_contribution",
                    "action_commitment": "committed",
                    "state_scope": "group",
                    "state_intents": [
                        {
                            "operation": "contribute",
                            "scope": "group",
                            "subject": "group_concept",
                            "summary": "两名英雄在边境驿站相遇并决定同行",
                        }
                    ],
                    "responds_to_event_id": "",
                    "reason": "提出两名英雄相遇并决定结伴同行的共同来历。",
                }
            ],
        },
        source_events=[
            {
                "event_id": event_id,
                "speaker": "南星",
                "text": "她们在边境驿站偶遇，目标相合后决定结伴同行。",
            }
        ],
    )

    error = tool_semantic_authority_error(
        tool_name="update_hero_draft",
        arguments={},
        semantics=semantics,
    )

    assert error is not None
    assert error.code == "MESSAGE_STATE_SCOPE_TOOL_MISMATCH"
    assert "group_concept" in error.correction_hint


def test_personal_hero_scope_can_update_hero_draft() -> None:
    event_id = "event-hero-origin"
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "南星",
                    "relation": "gm",
                    "targets": ["时悠"],
                    "dialogue_act": "state_contribution",
                    "action_commitment": "committed",
                    "state_scope": "hero",
                    "state_intents": [
                        {
                            "operation": "contribute",
                            "scope": "hero",
                            "subject": "hero_profile",
                            "summary": "赛璃小时候曾在边境驿站生活",
                        }
                    ],
                    "responds_to_event_id": "",
                    "reason": "补充自己英雄的个人经历。",
                }
            ],
        },
        source_events=[
            {
                "event_id": event_id,
                "speaker": "南星",
                "text": "赛璃小时候曾在边境驿站生活。",
            }
        ],
    )

    assert (
        tool_semantic_authority_error(
            tool_name="update_hero_draft",
            arguments={},
            semantics=semantics,
        )
        is None
    )


def test_mixed_hero_and_world_scope_can_update_planned_hero_field() -> None:
    event_id = "event-hero-origin-and-new-location"
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "阿凛",
                    "relation": "gm",
                    "targets": ["时悠"],
                    "dialogue_act": "answer",
                    "action_commitment": "none",
                    "state_scope": "mixed",
                    "state_intents": [
                        {
                            "operation": "contribute",
                            "scope": "hero",
                            "subject": "hero_origin",
                            "target": "伊莉雅",
                            "summary": "伊莉雅来自白花碑驿站。",
                        },
                        {
                            "operation": "contribute",
                            "scope": "world",
                            "subject": "map_locations",
                            "target": "白花碑驿站",
                            "summary": "围着白色纪念碑建起的边境驿站。",
                        },
                    ],
                    "responds_to_event_id": "",
                    "reason": "同时回答英雄故乡并贡献此前不存在的新地点。",
                }
            ],
        },
        source_events=[
            {
                "event_id": event_id,
                "speaker": "阿凛",
                "text": "伊莉雅来自白花碑驿站；那里围着白色纪念碑而建。",
            }
        ],
    )

    assert (
        tool_semantic_authority_error(
            tool_name="update_hero_draft",
            arguments={
                "subject": "阿凛",
                "patch": {"origin": "白花碑驿站"},
            },
            semantics=semantics,
        )
        is None
    )


def test_semantics_preserve_compound_confirmation_and_new_proposal() -> None:
    event_id = "event-compound-world-intent"
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "阿凛",
                    "relation": "table",
                    "targets": ["南星", "时悠"],
                    "dialogue_act": "agreement",
                    "action_commitment": "none",
                    "state_scope": "world",
                    "state_intents": [
                        {
                            "operation": "confirm",
                            "scope": "world",
                            "subject": "world_threats",
                            "summary": "确认回声枯竭作为世界威胁",
                        },
                        {
                            "operation": "propose",
                            "scope": "world",
                            "subject": "kingdoms",
                            "summary": "提议庄严的钟声王国作为北方国家",
                        },
                    ],
                    "responds_to_event_id": "",
                    "reason": "同一句先赞成旧威胁，再提出一个待讨论的新王国。",
                }
            ],
        },
        source_events=[
            {
                "event_id": event_id,
                "speaker": "阿凛",
                "text": "我同意回声枯竭；北边可以有个钟声王国，大家觉得呢？",
            }
        ],
    )

    assert [
        (intent.operation, intent.subject)
        for intent in semantics.events[0].state_intents
    ] == [("confirm", "world_threats"), ("propose", "kingdoms")]


def test_semantics_accept_all_authoritative_world_setting_categories() -> None:
    event_id = "event-faction-proposal"
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "南星",
                    "relation": "table",
                    "targets": ["阿凛"],
                    "dialogue_act": "proposal",
                    "action_commitment": "tentative",
                    "state_scope": "world",
                    "state_intents": [
                        {
                            "operation": "propose",
                            "scope": "world",
                            "subject": "factions",
                            "summary": "提议静默会作为幕后组织",
                        },
                        {
                            "operation": "propose",
                            "scope": "world",
                            "subject": "map_locations",
                            "summary": "提议静默会总部位于沉钟塔",
                        },
                    ],
                    "responds_to_event_id": "",
                    "reason": "提出组织与地点并征求同伴意见。",
                }
            ],
        },
        source_events=[
            {
                "event_id": event_id,
                "speaker": "南星",
                "text": "静默会可以藏在沉钟塔，大家觉得呢？",
            }
        ],
    )

    assert [intent.subject for intent in semantics.events[0].state_intents] == [
        "factions",
        "map_locations",
    ]


def test_semantics_unwrap_accidental_full_decision_shell() -> None:
    payload = semantics_payload()
    semantics = GMMessageSemantics.parse(
        {
            "decision": "silent",
            "message_kind": "discussion",
            "audience": "players",
            "message_semantics": payload,
            "reply": "",
            "reason": "玩家正在讨论。",
        },
        source_events=source_events(),
    )

    assert semantics.to_dict() == payload


def test_semantics_strip_known_outer_fields_nested_beside_events() -> None:
    payload = semantics_payload()
    payload.update(
        {
            "message_kind": "discussion",
            "audience": "players",
            "reply": "",
            "claims": [],
            "reason": "玩家正在讨论。",
        }
    )

    semantics = GMMessageSemantics.parse(
        payload,
        source_events=source_events(),
    )

    assert semantics.to_dict() == semantics_payload()


def test_semantics_accept_exact_redundant_transport_fields() -> None:
    sources = source_events()
    sources[0].update(
        {"speaker_id": "player-loading", "text": sources[0]["text"]}
    )
    sources[1].update(
        {"speaker_id": "player-villager", "text": sources[1]["text"]}
    )
    payload = semantics_payload()
    payload["events"][0].update(
        {"speaker_id": "player-loading", "text": sources[0]["text"]}
    )
    payload["events"][1].update(
        {"speaker_id": "player-villager", "text": sources[1]["text"]}
    )

    semantics = GMMessageSemantics.parse(payload, source_events=sources)

    assert semantics.to_dict() == semantics_payload()


def test_semantics_reject_changed_redundant_player_text() -> None:
    sources = source_events()
    payload = semantics_payload()
    payload["events"][0]["text"] = "我已经执行了观察。"

    with pytest.raises(GMMessageSemanticsError) as exc_info:
        GMMessageSemantics.parse(payload, source_events=sources)

    assert exc_info.value.code == "MESSAGE_SEMANTICS_SOURCE_MISMATCH"


def test_semantics_do_not_unwrap_unknown_wrapper_fields() -> None:
    with pytest.raises(GMMessageSemanticsError) as exc_info:
        GMMessageSemantics.parse(
            {
                "message_semantics": semantics_payload(),
                "untrusted_extra": "value",
            },
            source_events=source_events(),
        )

    assert exc_info.value.code == "MESSAGE_SEMANTICS_SCHEMA_INVALID"


def test_semantics_require_complete_event_coverage() -> None:
    payload = semantics_payload()
    payload["events"] = list(payload["events"])[1:]

    with pytest.raises(GMMessageSemanticsError) as exc_info:
        GMMessageSemantics.parse(payload, source_events=source_events())

    assert exc_info.value.code == "MESSAGE_SEMANTICS_EVENT_MISSING"


def test_semantics_reject_actor_substitution() -> None:
    payload = semantics_payload()
    payload["events"][1]["speaker"] = "loading"

    with pytest.raises(GMMessageSemanticsError) as exc_info:
        GMMessageSemantics.parse(payload, source_events=source_events())

    assert exc_info.value.code == "MESSAGE_SEMANTICS_SPEAKER_MISMATCH"


def test_player_agreement_cannot_resolve_gm_rule_window() -> None:
    semantics = GMMessageSemantics.parse(
        semantics_payload(),
        source_events=source_events(),
    )

    error = tool_semantic_authority_error(
        tool_name="resolve_rule_window",
        arguments={"source_event_id": "event-villager"},
        semantics=semantics,
    )

    assert error is not None
    assert error.code == "RULE_WINDOW_NOT_ANSWERED_BY_SOURCE_MESSAGE"


def test_tentative_plan_cannot_execute_player_action() -> None:
    semantics = GMMessageSemantics.parse(
        semantics_payload(),
        source_events=source_events(),
    )

    error = tool_semantic_authority_error(
        tool_name="perform_character_action",
        arguments={"source_event_id": "event-loading"},
        semantics=semantics,
    )

    assert error is not None
    assert error.code == "PLAYER_ACTION_NOT_COMMITTED"


def test_npc_encouragement_without_new_proposition_cannot_force_response() -> None:
    sources = [
        {
            "event_id": "event-encourage",
            "speaker": "南星",
            "text": "就这样，别停！",
        }
    ]
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": "event-encourage",
                    "speaker": "南星",
                    "relation": "npc",
                    "targets": ["归帆庆人群"],
                    "dialogue_act": "roleplay_speech",
                    "action_commitment": "none",
                    "responds_to_event_id": "",
                    "reason": "只是鼓励已经在持续敲击的人群，没有新增行动或条件。",
                }
            ],
        },
        source_events=sources,
    )

    error = tool_semantic_authority_error(
        tool_name="decide_collective_response",
        arguments={},
        semantics=semantics,
    )

    assert error is not None
    assert error.code == "NPC_RESPONSE_NOT_REQUESTED_BY_SOURCE_MESSAGE"


def test_roleplay_explanation_can_request_npc_response_without_being_an_action() -> None:
    event_id = "event-explain-fragment-to-guard"
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "南星",
                    "relation": "npc",
                    "targets": ["守卫"],
                    "dialogue_act": "roleplay_speech",
                    "action_commitment": "none",
                    "response_expectation": "npc",
                    "responds_to_event_id": "",
                    "reason": "赛璃解释守卫刚才追问的碎片来源，等待守卫判断。",
                }
            ],
        },
        source_events=[
            {
                "event_id": event_id,
                "speaker": "南星",
                "text": (
                    "我手上这块碎片是我在路边捡到的，上面有些奇怪的纹路，"
                    "想看看是不是什么重要物品。我没有别的意思，只是好奇而已。"
                ),
            }
        ],
    )

    assert semantics.events[0].action_commitment == "none"
    assert semantics.events[0].response_expectation == "npc"
    assert (
        tool_semantic_authority_error(
            tool_name="decide_npc_response",
            arguments={"source_event_id": event_id},
            semantics=semantics,
        )
        is None
    )


def test_new_npc_request_can_authorize_collective_response() -> None:
    sources = [
        {
            "event_id": "event-command",
            "speaker": "南星",
            "text": "敲响身边能响的东西，别让声音停！",
        }
    ]
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": "event-command",
                    "speaker": "南星",
                    "relation": "npc",
                    "targets": ["归帆庆人群"],
                    "dialogue_act": "roleplay_speech",
                    "action_commitment": "committed",
                    "responds_to_event_id": "",
                    "reason": "首次要求人群用连续敲击补住声响空隙。",
                }
            ],
        },
        source_events=sources,
    )

    assert (
        tool_semantic_authority_error(
            tool_name="decide_collective_response",
            arguments={},
            semantics=semantics,
        )
        is None
    )


def test_explicit_npc_request_can_authorize_collective_response() -> None:
    sources = [
        {
            "event_id": "event-command-request",
            "speaker": "南星",
            "text": "请把东门打开，让伤员先走。",
        }
    ]
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": "event-command-request",
                    "speaker": "南星",
                    "relation": "npc",
                    "targets": ["城门守卫"],
                    "dialogue_act": "request",
                    "action_commitment": "committed",
                    "responds_to_event_id": "",
                    "reason": "首次要求守卫打开城门并放行伤员。",
                }
            ],
        },
        source_events=sources,
    )

    assert (
        tool_semantic_authority_error(
            tool_name="decide_collective_response",
            arguments={},
            semantics=semantics,
        )
        is None
    )


def test_direct_answer_to_npc_can_authorize_npc_response() -> None:
    event_id = "event-answer-npc-condition"
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "南星",
                    "relation": "npc",
                    "targets": ["老钟匠霍恩"],
                    "dialogue_act": "answer",
                    "action_commitment": "committed",
                    "state_scope": "scene",
                    "state_intents": [
                        {
                            "operation": "confirm",
                            "scope": "scene",
                            "subject": "scene_fact",
                            "summary": "赛璃答应条件并请霍恩带路",
                        }
                    ],
                    "responds_to_event_id": "",
                    "reason": "回答NPC条件并提出需要NPC履行的新请求。",
                }
            ],
        },
        source_events=[
            {
                "event_id": event_id,
                "speaker": "南星",
                "text": "我答应。现在能带我去地下室吗？",
            }
        ],
    )

    assert (
        tool_semantic_authority_error(
            tool_name="decide_npc_response",
            arguments={"source_event_id": event_id},
            semantics=semantics,
        )
        is None
    )


def test_npc_answer_commitment_can_authorize_npc_response() -> None:
    event_id = "event-answer-horn-location"
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "阿凛",
                    "relation": "npc",
                    "targets": ["老钟匠霍恩"],
                    "dialogue_act": "answer",
                    "action_commitment": "answer",
                    "state_scope": "scene",
                    "state_intents": [
                        {
                            "operation": "contribute",
                            "scope": "scene",
                            "subject": "scene_fact",
                            "summary": "告知霍恩废弃钟塔的位置并请他查找记录",
                        }
                    ],
                    "responds_to_event_id": "",
                    "reason": "回答霍恩对钟塔位置的提问，并请求继续查找。",
                }
            ],
        },
        source_events=[
            {
                "event_id": event_id,
                "speaker": "阿凛",
                "text": "钟塔在驿站东边半里地，请帮我翻到记录里的那几页。",
            }
        ],
    )

    assert (
        tool_semantic_authority_error(
            tool_name="decide_npc_response",
            arguments={"source_event_id": event_id},
            semantics=semantics,
        )
        is None
    )


def test_direct_npc_question_does_not_require_redundant_commitment_flag() -> None:
    event_id = "event-ask-horn-page"
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "南星",
                    "relation": "npc",
                    "targets": ["老钟匠霍恩"],
                    "dialogue_act": "question",
                    "action_commitment": "none",
                    "state_scope": "none",
                    "state_intents": [],
                    "responds_to_event_id": "",
                    "reason": "赛璃直接询问霍恩被撕页面是否留下压痕。",
                }
            ],
        },
        source_events=[
            {
                "event_id": event_id,
                "speaker": "南星",
                "text": "霍恩先生，那页撕掉的部分还有压痕吗？",
            }
        ],
    )

    assert (
        tool_semantic_authority_error(
            tool_name="decide_npc_response",
            arguments={"source_event_id": event_id},
            semantics=semantics,
        )
        is None
    )


def test_committed_action_remains_authorized_when_dialogue_act_is_agreement() -> None:
    event_id = "event-agree-and-go"
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "南星",
                    "relation": "table",
                    "targets": ["阿凛", "白河"],
                    "dialogue_act": "agreement",
                    "action_commitment": "committed",
                    "state_scope": "scene",
                    "state_intents": [
                        {
                            "operation": "contribute",
                            "scope": "scene",
                            "subject": "scene_fact",
                            "target": "静默图书馆",
                            "summary": "赛璃与队友立即前往静默图书馆。",
                        }
                    ],
                    "responds_to_event_id": "",
                    "reason": "同意队友后同时落实了当前移动。",
                }
            ],
        },
        source_events=[
            {
                "event_id": event_id,
                "speaker": "南星",
                "text": "好，那我们一起去图书馆。",
            }
        ],
    )

    assert (
        tool_semantic_authority_error(
            tool_name="move_scene_group",
            arguments={"source_event_id": event_id},
            semantics=semantics,
        )
        is None
    )


def test_later_withdrawal_supersedes_committed_action() -> None:
    sources = [
        {
            "event_id": "event-attack",
            "speaker": "loading",
            "text": "伊大石抬起大黑锅冲向卡尔。",
        },
        {
            "event_id": "event-cancel",
            "speaker": "loading",
            "text": "还是算了。",
        },
    ]
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": "event-attack",
                    "speaker": "loading",
                    "relation": "table",
                    "targets": ["卡尔"],
                    "dialogue_act": "action_declaration",
                    "action_commitment": "committed",
                    "responds_to_event_id": "",
                    "reason": "声明角色立即冲向卡尔。",
                },
                {
                    "event_id": "event-cancel",
                    "speaker": "loading",
                    "relation": "table",
                    "targets": [],
                    "dialogue_act": "action_withdrawal",
                    "action_commitment": "withdrawn",
                    "responds_to_event_id": "event-attack",
                    "reason": "撤回刚才的冲锋行动。",
                },
            ],
        },
        source_events=sources,
    )

    error = tool_semantic_authority_error(
        tool_name="perform_character_action",
        arguments={"source_event_id": "event-attack"},
        semantics=semantics,
    )

    assert error is not None
    assert error.code == "PLAYER_ACTION_SUPERSEDED"


def test_explicit_gm_answer_can_resolve_rule_window() -> None:
    sources = [{"event_id": "event-roll", "speaker": "村夫", "text": "投"}]
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": "event-roll",
                    "speaker": "村夫",
                    "relation": "gm",
                    "targets": ["时悠"],
                    "dialogue_act": "answer",
                    "action_commitment": "answer",
                    "responds_to_event_id": "",
                    "reason": "回答主持人刚才是否投骰的提问。",
                }
            ],
        },
        source_events=sources,
    )

    assert (
        tool_semantic_authority_error(
            tool_name="resolve_rule_window",
            arguments={},
            semantics=semantics,
        )
        is None
    )


def test_hero_theme_semantics_require_theme_patch_not_notes_only() -> None:
    sources = [
        {
            "event_id": "event-theme",
            "speaker": "南星",
            "text": "她最怕有人被彻底遗忘，所以总想替别人守住名字。",
        }
    ]
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": "event-theme",
                    "speaker": "南星",
                    "relation": "gm",
                    "targets": ["时悠"],
                    "dialogue_act": "answer",
                    "action_commitment": "answer",
                    "state_scope": "hero",
                    "state_intents": [
                        {
                            "operation": "contribute",
                            "scope": "hero",
                            "subject": "hero_theme",
                            "target": "赛璃",
                            "summary": "不让任何人被彻底遗忘。",
                        }
                    ],
                    "responds_to_event_id": "",
                    "reason": "回答主持人对角色核心驱动的提问。",
                }
            ],
        },
        source_events=sources,
    )

    rejected = tool_semantic_authority_error(
        tool_name="update_hero_draft",
        arguments={"subject": "南星", "patch": {"notes": ["守住名字"]}},
        semantics=semantics,
    )
    accepted = tool_semantic_authority_error(
        tool_name="update_hero_draft",
        arguments={
            "subject": "南星",
            "patch": {
                "theme": "不让任何人被彻底遗忘",
                "notes": ["她会替别人守住名字。"],
            },
        },
        semantics=semantics,
    )

    assert rejected is not None
    assert rejected.code == "MESSAGE_HERO_FIELD_TOOL_MISMATCH"
    assert accepted is None
