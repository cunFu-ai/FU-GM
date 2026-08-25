from __future__ import annotations

from fu_gm.components.gm_message_integrity import (
    GMMessageIntegrityValidator,
)
from fu_gm.gm_tool_contracts import GMNarrativeEvent, GMToolReceipt


def world_receipt(category: str, *, event_id: str = "") -> GMToolReceipt:
    result: dict[str, object] = {
        "operation": "create",
        "category": category,
        "visibility": "public",
        "authority": "player_confirmed",
    }
    if event_id:
        result["source_event"] = {"event_id": event_id}
    return GMToolReceipt.success(
        "create_world_setting",
        result=result,
        state_changed=True,
    )


def test_complex_world_contribution_requires_each_independent_category() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "我的国家是东部海岸的奥涅里亚，灯塔舰队维持贸易。"
        "老国王病倒后，摄政王把王室海图抵押给财团；"
        "灯塔为什么能照见已经消失的岛，是我想留下的奥秘。"
        "若港口行会与王室决裂，财团就会拿走失踪群岛调查权。",
        gate_status="session_zero",
        source_event_id="event-a",
    )

    assert plan.world_categories == (
        "kingdoms",
        "historical_events",
        "mysteries",
        "world_threats",
    )


def test_implicit_forest_history_and_hostile_plan_are_detected() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "我贡献东南内陆的沉默森林，以及森林南侧的树誓村社。"
        "碎月之夜后，森林第一次拒绝所有人类祈祷；"
        "树皮写下的名字里为何有人仍活着，是这里的奥秘。"
        "苍白司教团想把森林变成灰晶病圣地。",
        gate_status="session_zero",
    )

    assert plan.world_categories == (
        "kingdoms",
        "historical_events",
        "mysteries",
        "world_threats",
    )


def test_political_faction_covers_session_zero_community_contribution() -> None:
    message = (
        "我贡献东南内陆的沉默森林，以及森林南侧的树誓村社。"
        "村社不认王权，只和奥灵立约。碎月之夜后，森林第一次拒绝所有人类祈祷；"
        "树皮写下的名字里为何有人仍活着，是这里的奥秘。"
        "苍白司教团想把森林变成灰晶病圣地。"
    )
    plan = GMMessageIntegrityValidator.plan(message, gate_status="session_zero")
    receipts = [
        world_receipt("map_locations"),
        world_receipt("factions"),
        world_receipt("historical_events"),
        world_receipt("mysteries"),
        world_receipt("world_threats"),
    ]

    assert GMMessageIntegrityValidator.validate_terminal(plan, receipts) is None


def test_missing_community_and_history_return_category_specific_repair_hints() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "我贡献树誓村社。碎月之夜后，森林第一次拒绝所有人类祈祷。",
        gate_status="session_zero",
    )

    issue = GMMessageIntegrityValidator.validate_terminal(
        plan,
        [world_receipt("map_locations")],
    )

    assert issue is not None
    assert issue.missing == ("kingdoms", "historical_events")
    assert "name 必须填写" in issue.correction_hint
    assert "省略 name" in issue.correction_hint
    assert "不要重建已有地点" in issue.correction_hint


def test_playstyle_preference_requires_a_persisted_world_receipt() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "我希望第一章至少有一场冲突不靠战斗解决，要靠证据和承诺。",
        gate_status="session_zero",
    )

    assert plan.world_categories == ("playstyle_themes",)
    issue = GMMessageIntegrityValidator.validate_terminal(plan, [])
    assert issue is not None
    assert issue.error_code == "SESSION_ZERO_CONTRIBUTION_INCOMPLETE"
    assert issue.missing == ("playstyle_themes",)
    assert (
        GMMessageIntegrityValidator.validate_terminal(
            plan,
            [world_receipt("consensus_notes")],
        )
        is None
    )


def test_opening_pacing_preference_does_not_create_a_safety_obligation() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "我希望整体有史诗奇幻的希望感，但别一上来就是拯救世界。"
        "先从边境小事开始，真相到中期再掀开。",
        gate_status="session_zero",
    )

    assert plan.safety_declarations == ()


def test_positive_reaction_to_uncomfortable_setting_is_not_a_safety_obligation() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "这地方一下就有了让人不舒服的分量，我喜欢这个设定。",
        gate_status="session_zero",
    )

    assert plan.safety_declarations == ()


def test_explicit_story_content_restriction_remains_a_safety_obligation() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "故事里不要出现针对儿童的伤害。",
        gate_status="session_zero",
    )

    assert plan.safety_kinds == ("line",)


def test_proposal_and_skip_do_not_become_confirmed_world_obligations() -> None:
    proposal = GMMessageIntegrityValidator.plan(
        "我提议新增一个国家叫雾港，大家觉得这样行不行？",
        gate_status="session_zero",
    )
    skipped = GMMessageIntegrityValidator.plan(
        "这项我先跳过，暂时不贡献世界奥秘。",
        gate_status="session_zero",
    )
    committed = GMMessageIntegrityValidator.plan(
        "我提议叫雾港，大家都同意了，就这样定。国家：雾港。",
        gate_status="session_zero",
    )

    assert proposal.proposal is True
    assert proposal.world_categories == ()
    assert skipped.skipped is True
    assert skipped.world_categories == ()
    assert committed.proposal is False
    assert committed.world_categories == ("kingdoms",)


def test_explicit_group_direction_proposal_requires_a_persisted_proposal() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "小队我先提个还没定的方向：大家是临时守护者。你们觉得合适吗？",
        gate_status="session_zero",
        source_event_id="proposal-1",
    )

    assert plan.proposal_persistence_required is True
    assert plan.proposal_subjects == ("group_concept",)

    missing = GMMessageIntegrityValidator.validate_terminal(plan, [])
    assert missing is not None
    assert missing.error_code == "SESSION_ZERO_PROPOSAL_INCOMPLETE"

    persisted = GMToolReceipt.success(
        "propose_session_zero_update",
        result={
            "proposal": {"proposed_updates": {"group_concept": "临时守护者"}},
            "source_event": {"event_id": "proposal-1"},
        },
        state_changed=True,
    )
    assert GMMessageIntegrityValidator.validate_terminal(plan, [persisted]) is None


def test_semantically_complete_proposal_does_not_expand_category_alternatives() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "一片森林不与任何王国结盟，只和奥灵立约，某天夜里突然拒绝祈祷，"
        "树皮却写着仍在世者的名字。这可以作为地区、历史或威胁的种子，"
        "你们觉得呢？",
        gate_status="session_zero",
        source_event_id="proposal-forest",
    )
    assert len(plan.proposal_subjects) > 1
    persisted = GMToolReceipt.success(
        "propose_session_zero_update",
        result={
            "proposal": {
                "summary": "森林异常种子，分类仍待桌面决定。",
                "world_operations": [
                    {
                        "operation": "create",
                        "category": "mysteries",
                        "value": "森林拒绝祈祷，树皮写着仍在世者的名字。",
                        "visibility": "public",
                    }
                ],
            },
            "semantic_source_complete": True,
            "source_event": {"event_id": "proposal-forest"},
        },
        state_changed=True,
    )

    assert GMMessageIntegrityValidator.validate_terminal(plan, [persisted]) is None


def test_explicit_pending_proposal_cannot_use_formal_world_crud() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "我先丢一个还没定的地图想法：大陆叫白钟大陆。大家觉得合适吗？",
        gate_status="session_zero",
    )

    issue = GMMessageIntegrityValidator.validate_decision(
        plan,
        {
            "decision": "call_tool",
            "tool_name": "create_world_setting",
            "arguments": {"category": "continent_name", "value": "白钟大陆"},
        },
    )

    assert issue is not None
    assert issue.error_code == "SESSION_ZERO_PROPOSAL_MISCOMMITTED"


def test_adventure_table_question_does_not_require_proposal_persistence() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "我提议先调查门锁，大家觉得怎么样？",
        gate_status="adventure",
    )

    assert plan.proposal_persistence_required is False
    assert GMMessageIntegrityValidator.validate_terminal(plan, []) is None


def test_explicit_group_proposal_agreement_requires_confirmation_receipt() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "我赞成白河的小队方向。我们就是在白花碑驿站结成的临时守护者。",
        gate_status="session_zero",
        source_event_id="proposal-confirm-1",
    )

    assert plan.proposal_confirmation_subjects == ("group_concept",)
    issue = GMMessageIntegrityValidator.validate_terminal(plan, [])
    assert issue is not None
    assert issue.error_code == "SESSION_ZERO_PROPOSAL_CONFIRMATION_INCOMPLETE"

    confirmed = GMToolReceipt.success(
        "confirm_session_zero_proposal",
        result={
            "proposal_id": "proposal-1",
            "source_event": {"event_id": "proposal-confirm-1"},
        },
        state_changed=True,
    )
    assert GMMessageIntegrityValidator.validate_terminal(plan, [confirmed]) is None


def test_confirmation_without_a_persisted_proposal_can_use_formal_map_writes() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "我赞成刚才的地图轮廓，就按白钟大陆来。",
        gate_status="session_zero",
    )
    persisted = GMToolReceipt.success(
        "create_world_setting",
        result={
            "operation": "create",
            "category": "continent_name",
            "visibility": "public",
            "authority": "table_consensus",
        },
        state_changed=True,
    )

    assert plan.proposal_confirmation_subjects == ("world_map",)
    assert GMMessageIntegrityValidator.validate_terminal(plan, [persisted]) is None


def test_skipping_one_world_category_does_not_erase_other_contributions() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "国家先跳过。我补一个地区：沉默森林。历史事件：碎月之夜。"
        "奥秘：树皮上的活人名字。威胁：苍白司教团会污染森林。",
        gate_status="session_zero",
    )

    assert plan.skipped is True
    assert plan.skipped_world_categories == ("kingdoms",)
    assert plan.world_categories == (
        "historical_events",
        "mysteries",
        "world_threats",
    )

    receipts = [
        world_receipt("historical_events"),
        world_receipt("mysteries"),
        world_receipt("world_threats"),
    ]
    issue = GMMessageIntegrityValidator.validate_terminal(
        plan,
        receipts,
        semantic_message_kind="state_contribution",
    )
    assert issue is not None
    assert issue.error_code == "SESSION_ZERO_TOPIC_SKIP_INCOMPLETE"
    assert issue.missing == ("kingdoms",)

    receipts.append(
        GMToolReceipt.success(
            "mark_session_zero_topic_complete",
            result={"topic": "kingdom"},
            state_changed=True,
        )
    )
    assert (
        GMMessageIntegrityValidator.validate_terminal(
            plan,
            receipts,
            semantic_message_kind="state_contribution",
        )
        is None
    )


def test_rolled_back_topic_skip_must_be_submitted_again() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "国家这一项我先跳过。我补一个地区：潮鸢群岛。",
        gate_status="session_zero",
        source_event_id="skip-event",
    )
    rolled_back = GMToolReceipt.success(
        "mark_session_zero_topic_complete",
        result={
            "topic": "kingdom",
            "rolled_back": True,
            "source_event": {"event_id": "skip-event"},
        },
        state_changed=False,
    )

    issue = GMMessageIntegrityValidator.validate_terminal(
        plan,
        [rolled_back],
        semantic_message_kind="state_contribution",
    )

    assert issue is not None
    assert issue.error_code == "SESSION_ZERO_TOPIC_SKIP_INCOMPLETE"
    assert issue.details["required_skip_topics"] == ["kingdom"]


def test_partial_world_receipts_report_exact_missing_category() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "我的国家是奥涅里亚。三十年前王室海图被抵押。"
        "灯塔为何照见失踪岛是奥秘。如果行会决裂就会引发危机。",
        gate_status="session_zero",
        source_event_id="event-a",
    )
    receipts = [
        world_receipt("kingdoms", event_id="event-a"),
        world_receipt("mysteries", event_id="event-a"),
        world_receipt("world_threats", event_id="event-a"),
    ]

    issue = GMMessageIntegrityValidator.validate_terminal(plan, receipts)

    assert issue is not None
    assert issue.error_code == "SESSION_ZERO_CONTRIBUTION_INCOMPLETE"
    assert issue.missing == ("historical_events",)
    assert issue.protocol_error()["protocol_error"]["retryable"] is True
    assert "当前消息事务内" in issue.correction_hint


def test_map_location_does_not_cover_a_country_contribution() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "我贡献地点：雾港。历史事件：旧塔坠落。"
        "奥秘：钟声来自哪里。威胁：海雾会吞没港口。",
        gate_status="session_zero",
    )
    receipts = [
        world_receipt("map_locations"),
        world_receipt("historical_events"),
        world_receipt("mysteries"),
        world_receipt("world_threats"),
    ]

    issue = GMMessageIntegrityValidator.validate_terminal(plan, receipts)
    assert issue is not None
    assert issue.missing == ("kingdoms",)


def test_receipts_from_another_source_event_do_not_cover_this_message() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "我的国家是雾港。历史事件：旧塔坠落。",
        gate_status="session_zero",
        source_event_id="event-a",
    )
    receipts = [
        world_receipt("kingdoms", event_id="event-a"),
        world_receipt("historical_events", event_id="event-b"),
    ]

    issue = GMMessageIntegrityValidator.validate_terminal(plan, receipts)

    assert issue is not None
    assert issue.missing == ("historical_events",)


def test_rolled_back_or_non_mutating_success_does_not_cover_obligation() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "历史事件：旧塔坠落。",
        gate_status="session_zero",
    )
    rolled_back = world_receipt("historical_events")
    rolled_back.result["rolled_back"] = True
    no_change = GMToolReceipt.success(
        "create_world_setting",
        result={"category": "historical_events"},
        state_changed=False,
    )

    rolled_back_issue = GMMessageIntegrityValidator.validate_terminal(
        plan,
        [rolled_back],
    )
    no_change_issue = GMMessageIntegrityValidator.validate_terminal(
        plan,
        [no_change],
    )

    assert rolled_back_issue is not None
    assert rolled_back_issue.missing == ("historical_events",)
    assert no_change_issue is not None
    assert no_change_issue.missing == ("historical_events",)


def test_narrative_event_provenance_is_used_when_result_has_no_source_event() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "历史事件：旧塔坠落。",
        gate_status="session_zero",
        source_event_id="event-a",
    )
    receipt = world_receipt("historical_events")
    receipt.narrative_events.append(
        GMNarrativeEvent(
            event_type="campaign_setup_change",
            tool_name="create_world_setting",
            source_event_id="event-b",
        )
    )

    issue = GMMessageIntegrityValidator.validate_terminal(plan, [receipt])

    assert issue is not None
    assert issue.missing == ("historical_events",)


def test_explicit_line_and_veil_both_need_success_receipts() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "我的界限：虐待儿童；我的帷幕：手术过程淡出。",
        gate_status="session_zero",
    )
    line = GMToolReceipt.success(
        "record_safety_boundary",
        result={"kind": "line", "content": "虐待儿童"},
        state_changed=True,
    )

    issue = GMMessageIntegrityValidator.validate_terminal(plan, [line])

    assert plan.safety_kinds == ("line", "veil")
    assert issue is not None
    assert issue.error_code == "SAFETY_BOUNDARY_INCOMPLETE"
    assert issue.missing == ("veil:手术过程",)


def test_explicit_veil_treatment_needs_only_the_semantically_complete_receipt() -> None:
    content = "严重或残酷的身体伤害可以作为结果存在，但不要具体描写过程和伤口"
    plan = GMMessageIntegrityValidator.plan(
        f"我这边加一条帷幕：{content}。",
        gate_status="session_zero",
    )
    receipt = GMToolReceipt.success(
        "record_safety_boundary",
        result={"kind": "veil", "content": content},
        state_changed=True,
    )

    assert [(item.kind, item.content) for item in plan.safety_declarations] == [
        ("veil", content)
    ]
    assert GMMessageIntegrityValidator.validate_terminal(plan, [receipt]) is None


def test_two_lines_need_two_matching_kind_and_content_receipts() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "界限：蜘蛛，虐待儿童。",
        gate_status="session_zero",
    )
    only_one = GMToolReceipt.success(
        "record_safety_boundary",
        result={"kind": "line", "content": "蜘蛛"},
        state_changed=True,
    )

    issue = GMMessageIntegrityValidator.validate_terminal(plan, [only_one])

    assert issue is not None
    assert issue.error_code == "SAFETY_BOUNDARY_INCOMPLETE"
    assert issue.missing == ("line:虐待儿童",)


def test_explicit_safety_enumeration_accepts_one_receipt_per_concrete_topic() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "我先把底线说清：性暴力、酷刑和现实仇恨煽动是界限；"
        "儿童遇险、身体病变和亲密内容请帷幕淡出。",
        gate_status="session_zero",
    )
    receipts = [
        GMToolReceipt.success(
            "record_safety_boundary",
            result={"kind": kind, "content": content},
            state_changed=True,
        )
        for kind, content in (
            ("line", "性暴力"),
            ("line", "酷刑"),
            ("line", "现实仇恨煽动"),
            ("veil", "儿童遇险"),
            ("veil", "身体病变"),
            ("veil", "亲密内容"),
        )
    ]

    assert [
        (item.kind, item.content) for item in plan.safety_declarations
    ] == [
        ("line", "性暴力"),
        ("line", "酷刑"),
        ("line", "现实仇恨煽动"),
        ("veil", "儿童遇险"),
        ("veil", "身体病变"),
        ("veil", "亲密内容"),
    ]
    assert GMMessageIntegrityValidator.validate_terminal(plan, receipts) is None


def test_natural_safety_context_prefix_matches_canonical_tool_content() -> None:
    validator = GMMessageIntegrityValidator()
    plan = validator.plan(
        "界限：不要在游戏里出现蜘蛛。",
        gate_status="adventure",
        source_event_id="safety-context-1",
    )

    issue = validator.validate_terminal(
        plan,
        [
            GMToolReceipt.success(
                "record_safety_boundary",
                result={
                    "kind": "line",
                    "content": "蜘蛛",
                    "source_event": {"event_id": "safety-context-1"},
                },
                state_changed=True,
            )
        ],
    )

    assert issue is None


def test_hero_confirmation_semantics_are_not_inferred_by_integrity_validator() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "补上最后一项，然后确认角色并正式建卡。",
        gate_status="session_zero",
    )
    ready_update = GMToolReceipt.success(
        "update_hero_draft",
        result={"ready": True},
        state_changed=True,
    )

    issue = GMMessageIntegrityValidator.validate_terminal(plan, [ready_update])

    assert issue is None
    assert plan.empty


def test_incomplete_update_and_failed_confirmation_do_not_loop_integrity_gate() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "确认角色并正式建卡。",
        gate_status="session_zero",
    )
    incomplete_update = GMToolReceipt.success(
        "update_hero_draft",
        result={"ready": False},
        state_changed=True,
    )
    failed_confirm = GMToolReceipt.failure(
        "confirm_hero_draft",
        "HERO_DRAFT_INCOMPLETE",
        "角色仍有缺项。",
        "补齐缺项后再确认。",
        retryable=False,
    )

    assert (
        GMMessageIntegrityValidator.validate_terminal(plan, [incomplete_update])
        is None
    )
    assert (
        GMMessageIntegrityValidator.validate_terminal(plan, [failed_confirm])
        is None
    )


def test_ritual_casting_attribute_must_use_skill_options_not_attributes() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "拟兽系仪式的施法属性我选洞察+意志。",
        gate_status="session_zero",
        source_event_id="event-a",
    )
    wrong_decision = {
        "decision": "call_tool",
        "tool_name": "update_hero_draft",
        "arguments": {
            "source_event_id": "event-a",
            "subject": "澄砚",
            "patch": {"attributes": {"洞察": 10, "意志": 10}},
        },
    }

    issue = GMMessageIntegrityValidator.validate_decision(plan, wrong_decision)

    assert [(item.skill_name, item.choice) for item in plan.hero_skill_options] == [
        ("拟兽系仪式", "洞察+意志")
    ]
    assert issue is not None
    assert issue.error_code == "SESSION_ZERO_HERO_OPTION_MISMAPPED"
    assert issue.details["base_attributes_submitted"] is True


def test_correct_ritual_skill_option_passes_pre_execution_and_receipt_checks() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "拟兽系仪式的施法属性我选洞察＋意志。",
        gate_status="session_zero",
    )
    decision = {
        "decision": "call_tool",
        "tool_name": "update_hero_draft",
        "arguments": {
            "subject": "澄砚",
            "patch": {"skill_options": {"拟兽系仪式": ["洞察+意志"]}},
        },
    }
    receipt = GMToolReceipt.success(
        "update_hero_draft",
        result={
            "ready": False,
            "changed_fields": ["skill_options"],
            "applied_skill_options": {"拟兽系仪式": ["洞察+意志"]},
        },
        state_changed=True,
    )

    assert GMMessageIntegrityValidator.validate_decision(plan, decision) is None
    assert GMMessageIntegrityValidator.validate_terminal(plan, [receipt]) is None


def test_old_success_receipt_without_applied_skill_options_cannot_claim_coverage() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "拟兽系仪式的施法属性我选洞察+意志。",
        gate_status="session_zero",
    )
    ambiguous_receipt = GMToolReceipt.success(
        "update_hero_draft",
        result={"ready": False},
        state_changed=True,
    )

    issue = GMMessageIntegrityValidator.validate_terminal(
        plan,
        [ambiguous_receipt],
    )

    assert issue is not None
    assert issue.error_code == "SESSION_ZERO_HERO_OPTION_INCOMPLETE"


def test_skill_option_decision_for_another_source_event_is_not_misjudged() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "拟兽系仪式的施法属性我选洞察+意志。",
        gate_status="session_zero",
        source_event_id="event-a",
    )
    other_event_decision = {
        "decision": "call_tool",
        "tool_name": "update_hero_draft",
        "arguments": {
            "source_event_id": "event-b",
            "subject": "另一位玩家",
            "patch": {"attributes": {"力量": 10}},
        },
    }

    assert (
        GMMessageIntegrityValidator.validate_decision(plan, other_event_decision)
        is None
    )


def test_integrity_rules_are_inactive_for_ordinary_adventure_messages() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "老国王病倒后，我调查灯塔为什么会熄灭。",
        gate_status="adventure",
    )

    assert plan.world_categories == ()


def test_tone_preference_requires_authoritative_receipt_without_safety_pollution() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "我希望整体有史诗奇幻的希望感，但别一上来就是拯救世界。"
        "先从边境小事开始，真相到中期再掀开。",
        gate_status="session_zero",
    )

    assert plan.world_categories == ("tone_preferences",)
    assert plan.safety_declarations == ()
    issue = GMMessageIntegrityValidator.validate_terminal(plan, [])
    assert issue is not None
    assert issue.error_code == "SESSION_ZERO_CONTRIBUTION_INCOMPLETE"
    assert issue.missing == ("tone_preferences",)
    assert (
        GMMessageIntegrityValidator.validate_terminal(
            plan,
            [world_receipt("tone_preferences")],
        )
        is None
    )


def test_magic_technology_relationship_requires_its_own_receipt() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "魔法和科技可以并存。灵魂晶炉驱动车辆和工坊机器，"
        "古老御魂术负责安抚灵魂之河。",
        gate_status="session_zero",
    )

    assert plan.world_categories == ("magic_tech_role",)
    issue = GMMessageIntegrityValidator.validate_terminal(plan, [])
    assert issue is not None
    assert issue.missing == ("magic_tech_role",)
    assert (
        GMMessageIntegrityValidator.validate_terminal(
            plan,
            [world_receipt("magic_tech_role")],
        )
        is None
    )


def test_explicit_world_shape_is_an_independent_session_zero_obligation() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "我赞成地图提案。它就是普通的类地球大陆，不用异形世界。",
        gate_status="session_zero",
        source_event_id="event-world-shape",
    )

    assert "world_shape" in plan.world_categories
    issue = GMMessageIntegrityValidator.validate_terminal(plan, [])
    assert issue is not None
    assert issue.error_code == "SESSION_ZERO_PROPOSAL_CONFIRMATION_INCOMPLETE"

    issue = GMMessageIntegrityValidator.validate_terminal(
        plan,
        [world_receipt("continent_name")],
    )
    assert issue is not None
    assert issue.error_code == "SESSION_ZERO_CONTRIBUTION_INCOMPLETE"
    assert issue.missing == ("world_shape",)
    assert (
        GMMessageIntegrityValidator.validate_terminal(
            plan,
            [world_receipt("continent_name"), world_receipt("world_shape")],
        )
        is None
    )


def test_initial_hero_sentence_requires_every_explicit_core_field() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "我的玩家名是阿凛，角色名伊莉雅。身份：赤羽遗民的盾誓骑士；"
        "主题：责任；故乡：白花碑驿站。职业分配：守护者3级、元素使2级。"
        "属性骰：敏捷d8、洞察d8、力量d10、意志d6。",
        gate_status="session_zero",
        speaker="阿凛",
    )
    partial = GMToolReceipt.success(
        "update_hero_draft",
        result={
            "player_name": "阿凛",
            "changed_fields": [
                "hero_name",
                "identity",
                "theme",
                "origin",
                "classes",
            ],
        },
        state_changed=True,
    )

    assert plan.hero_fields == (
        "hero_name",
        "identity",
        "theme",
        "origin",
        "classes",
        "attributes",
    )
    issue = GMMessageIntegrityValidator.validate_terminal(plan, [partial])
    assert issue is not None
    assert issue.error_code == "SESSION_ZERO_HERO_FIELDS_INCOMPLETE"
    assert issue.missing == ("attributes",)


def test_final_hero_sentence_tracks_only_structured_hero_fields() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "伊莉雅法术选择元素幕障。初始装备：钢匕首、青铜盾、旅行装束。"
        "羁绊：赛璃：信赖；洛岚：钦佩。背景钩子：姐姐的名字刻在风铃内侧。"
        "伊莉雅确认角色并正式建卡。",
        gate_status="session_zero",
        speaker="阿凛",
    )

    assert plan.hero_fields == ("spells", "equipment", "bonds", "notes")


def test_arcana_contract_and_device_choice_are_separate_hero_fields() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "苍祈与魔典奥灵缔结了起始契约；洛岚的便携装置选择魔导装置。",
        gate_status="session_zero",
    )

    assert plan.hero_fields == ("bound_arcana", "skill_options")


def test_terse_repeat_skill_choice_uses_authoritative_hero_name() -> None:
    state_summary = {
        "session_zero": {
            "hero_drafts": [{"player_name": "阿凛", "hero_name": "伊莉雅"}]
        }
    }

    hero_plan = GMMessageIntegrityValidator.plan(
        "伊莉雅再选防御精通。",
        gate_status="session_zero",
        state_summary=state_summary,
    )
    map_plan = GMMessageIntegrityValidator.plan(
        "地图我们再选环形大陆。",
        gate_status="session_zero",
        state_summary=state_summary,
    )

    assert hero_plan.hero_fields == ("skills",)
    assert map_plan.hero_fields == ()


def test_group_proposal_destination_does_not_create_country_proposal() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "小队我先提个还没定的方向：大家是在白花碑驿站临时结成的守护者，"
        "护送失忆旅人和碎月遗物去钟鸣公国。你们觉得合适吗？",
        gate_status="session_zero",
    )

    assert plan.proposal_subjects == ("group_concept",)
    assert plan.hero_skill_options == ()
