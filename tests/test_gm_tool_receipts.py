from __future__ import annotations

from fu_gm.gm_tool_contracts import GMToolExecutionContext, GMToolReceipt
from fu_gm.gm_tool_receipts import GMToolReceiptPolicy


def _context(*, heartbeat: bool = False) -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="c",
        session_id="s",
        channel_id="group",
        speaker="系统主动节拍" if heartbeat else "阿凛",
        gate_status="adventure",
        metadata={"system_gm_beat_request": heartbeat},
    )


def _transition_receipt(
    *,
    followups: list[str],
    required: list[str] | None = None,
) -> GMToolReceipt:
    result = {"allowed_followup_tools": followups}
    if required is not None:
        result["required_followup_tools"] = required
    return GMToolReceipt.success(
        "transition_scene",
        result=result,
        state_changed=True,
        public_reply="伊莉雅抵达旧路闸门。",
        lock_public_reply=True,
    )


def test_locked_transition_with_optional_followup_grant_can_end_transaction() -> None:
    receipt = _transition_receipt(followups=["decide_npc_response"])

    assert GMToolReceiptPolicy.terminal_public_change_committed(
        receipt,
        terminal_public_tools=frozenset({"transition_scene"}),
    )
    assert GMToolReceiptPolicy.heartbeat_public_change_committed(
        _context(heartbeat=True),
        receipt,
    )


def test_locked_transition_with_required_followup_cannot_end_transaction() -> None:
    receipt = _transition_receipt(
        followups=["decide_npc_response"],
        required=["decide_npc_response"],
    )

    assert not GMToolReceiptPolicy.terminal_public_change_committed(
        receipt,
        terminal_public_tools=frozenset({"transition_scene"}),
    )
    assert not GMToolReceiptPolicy.heartbeat_public_change_committed(
        _context(heartbeat=True),
        receipt,
    )


def test_locked_transition_without_followup_grant_is_terminal() -> None:
    receipt = _transition_receipt(followups=[])

    assert GMToolReceiptPolicy.terminal_public_change_committed(
        receipt,
        terminal_public_tools=frozenset({"transition_scene"}),
    )
    assert GMToolReceiptPolicy.heartbeat_public_change_committed(
        _context(heartbeat=True),
        receipt,
    )


def test_preparatory_nonpublic_write_requires_followup_until_action_commits() -> None:
    focus = GMToolReceipt.success(
        "focus_scene_branch",
        result={
            "required_followup_tools": ["move_scene_group", "perform_in_scene_action"]
        },
        state_changed=True,
    )

    assert GMToolReceiptPolicy.required_followup_tools([focus]) == {
        "move_scene_group",
        "perform_in_scene_action",
    }

    movement = GMToolReceipt.success(
        "move_scene_group",
        state_changed=True,
        public_reply="赛璃与失忆旅人抵达登记小室。",
        lock_public_reply=True,
    )
    assert GMToolReceiptPolicy.required_followup_tools([focus, movement]) is None


def test_read_only_followup_explicitly_clears_preparatory_obligation() -> None:
    context = _context()
    focus = GMToolReceipt.success(
        "focus_scene_branch",
        result={
            "required_followup_tools": ["pass_in_scene_action"],
            "allowed_followup_tools": ["pass_in_scene_action"],
        },
        state_changed=True,
    )
    GMToolReceiptPolicy.apply_context(context, {}, focus)

    no_clock_pass = GMToolReceipt.success(
        "pass_in_scene_action",
        result={"recorded": False},
        state_changed=False,
    )
    GMToolReceiptPolicy.apply_context(context, {}, no_clock_pass)

    assert no_clock_pass.result["required_followup_tools"] == []
    assert GMToolReceiptPolicy.required_followup_tools(
        [focus, no_clock_pass]
    ) is None
    assert GMToolReceiptPolicy.required_followup_calls(
        [focus, no_clock_pass]
    ) == []


def test_retryable_failure_text_is_private_protocol_feedback() -> None:
    failure = GMToolReceipt.failure(
        "decide_npc_response",
        "NPC_PLAYER_RESPONSE_INVALID",
        "待答项目不匹配。",
        "使用准确item_id重试。",
        public_reply="这名NPC这次还没有作出有效回应。",
    )

    assert GMToolReceiptPolicy.receipt_fallback([failure]) == ""
    assert GMToolReceiptPolicy.interrupted_reply([failure]) == ""


def test_start_session_model_view_keeps_only_opening_scene_material() -> None:
    receipt = GMToolReceipt.success(
        "start_session",
        result={
            "adventure_opening_required": True,
            "saved_path": "/tmp/private-save.json",
            "world_map": {"large": "map.png"},
            "opening_contract": {"confirmed_heroes": ["诺艾尔", "艾丽妮"]},
            "opening_character_state": [{"name": "诺艾尔"}],
            "required_followup_tools": ["start_scene"],
            "session_situation_contract": {
                "title": "雨夜越狱",
                "dramatic_question": "英雄能否离开监狱？",
                "important_npcs": [
                    {"name": "玛尔塔", "goal_now": "守住监狱"},
                    {"name": "庄园总管", "goal_now": "销毁证据"},
                ],
                "clue_routes": [
                    {"route_id": "physical", "visible_lead": "倒插的封印钉"},
                    {"route_id": "later", "visible_lead": "庄园账册"},
                ],
                "potential_scenes": [
                    {
                        "scene_key": "opening",
                        "scene_role": "strong_start",
                        "title": "裂灯",
                        "npc_names": ["玛尔塔"],
                        "clue_route_ids": ["physical"],
                        "optional": False,
                    },
                    {
                        "scene_key": "later",
                        "scene_role": "climax_candidate",
                        "title": "庄园地窖",
                        "npc_names": ["庄园总管"],
                        "clue_route_ids": ["later"],
                        "optional": False,
                    },
                ],
            },
        },
        state_changed=True,
    )

    model_view = GMToolReceiptPolicy.model_view(receipt)
    model_result = model_view["result"]
    situation = model_result["session_situation_contract"]

    assert "saved_path" not in model_result
    assert "world_map" not in model_result
    assert situation["opening_scene"]["scene_key"] == "opening"
    assert [item["name"] for item in situation["opening_scene_npcs"]] == [
        "玛尔塔"
    ]
    assert [item["route_id"] for item in situation["opening_scene_clues"]] == [
        "physical"
    ]
    assert "potential_scenes" not in situation
    assert receipt.result["saved_path"] == "/tmp/private-save.json"
    assert len(receipt.result["session_situation_contract"]["potential_scenes"]) == 2


def test_silent_commit_fallback_is_kept_for_recovery_but_hidden_from_model() -> None:
    receipt = GMToolReceipt.success(
        "commit_session_zero_update",
        result={
            "silent_commit_allowed": True,
            "source_message_already_public": True,
        },
        state_changed=True,
        public_reply="好，记下了。",
    )

    model_view = GMToolReceiptPolicy.model_view(receipt)

    assert receipt.public_fallback_reply == "好，记下了。"
    assert model_view["public_fallback_reply"] == ""


def test_scene_response_followup_requires_exact_committed_payload() -> None:
    context = _context()
    unsafe = GMToolReceipt.success(
        "resolve_rule_window",
        result={"required_followup_tools": ["commit_scene_response"]},
        state_changed=True,
    )

    GMToolReceiptPolicy.apply_context(context, {}, unsafe)

    assert unsafe.result["required_followup_tools"] == []
    assert GMToolReceiptPolicy.REQUIRED_FOLLOWUP_CONTEXT_KEY not in context.metadata

    exact = GMToolReceipt.success(
        "resolve_rule_window",
        result={
            "required_followup_tools": ["commit_scene_response"],
            "scene_response_followup": {
                "public_reply": "升降台已经停在下层。",
                "public_facts": ["升降台已经停在下层。"],
            },
        },
        state_changed=True,
    )

    GMToolReceiptPolicy.apply_context(context, {}, exact)

    followup = context.metadata[GMToolReceiptPolicy.REQUIRED_FOLLOWUP_CONTEXT_KEY]
    assert followup["required_tools"] == ["commit_scene_response"]
    assert followup["scene_response_followup"]["public_reply"] == (
        "升降台已经停在下层。"
    )


def test_structured_tool_receipt_issues_due_scene_authority_once() -> None:
    context = _context()
    source = GMToolReceipt.success(
        "resolve_structured_hazard",
        result={
            "scene_change_authority": {
                "hazard_id": "lift-cycle-2",
                "source_kind": "structured_hazard",
                "status": "triggered",
                "scene_id": "scene-2",
                "public_reply": "升降台完成转动，东侧踏板降到下层。",
                "public_facts": ["东侧踏板降到下层。"],
            }
        },
        state_changed=True,
    )

    GMToolReceiptPolicy.apply_context(context, {}, source)
    GMToolReceiptPolicy.apply_context(context, {}, source)

    records = context.metadata["scene_change_authorities"]
    assert len(records) == 1
    assert records[0]["authority_id"] == "lift-cycle-2"
    assert records[0]["source_tool"] == "resolve_structured_hazard"


def test_required_followup_context_preserves_canonical_arguments_until_completion() -> None:
    context = _context()
    source = GMToolReceipt.success(
        "move_group_within_scene",
        result={
            "required_followup_tools": ["decide_npc_response"],
            "required_followup_calls": [
                {
                    "tool_name": "decide_npc_response",
                    "arguments": {
                        "name": "白花守望会会长",
                        "actor": "艾薇娅",
                        "condition_id": "scene-2|condition-1",
                    },
                }
            ],
            "fulfilled_condition": {
                "condition_id": "scene-2|condition-1",
                "player_fulfillment": "fulfilled",
            },
            "condition_payoff_due_from": "白花守望会会长",
            "triggered_commitment": {
                "commitment_id": "scene-2|promise-1",
                "trigger_status": "reached",
                "trigger_responder": "白花守望者",
            },
            "commitment_payoff_due_from": "白花守望者",
        },
        state_changed=True,
    )

    GMToolReceiptPolicy.apply_context(context, {}, source)
    followup = context.metadata[GMToolReceiptPolicy.REQUIRED_FOLLOWUP_CONTEXT_KEY]

    assert followup["required_calls"][0]["arguments"]["condition_id"] == (
        "scene-2|condition-1"
    )
    assert followup["triggered_commitment"]["commitment_id"] == (
        "scene-2|promise-1"
    )
    assert followup["commitment_payoff_due_from"] == "白花守望者"
    GMToolReceiptPolicy.apply_context(
        context,
        {},
        GMToolReceipt.success(
            "decide_npc_response",
            result={},
            state_changed=True,
        ),
    )
    assert GMToolReceiptPolicy.REQUIRED_FOLLOWUP_CONTEXT_KEY not in context.metadata


def test_all_mode_followups_remain_pending_until_every_obligation_completes() -> None:
    context = _context()
    source = GMToolReceipt.success(
        "perform_check_action",
        result={
            "required_followup_tools": [
                "resolve_gm_opportunity",
                "decide_npc_response",
            ],
            "required_followup_calls": [
                {
                    "tool_name": "resolve_gm_opportunity",
                    "arguments": {"window_id": "fumble-1"},
                },
                {
                    "tool_name": "decide_npc_response",
                    "arguments": {
                        "name": "白花守望会会长",
                        "condition_id": "condition-1",
                    },
                },
            ],
            "required_followup_mode": "all",
        },
        state_changed=True,
    )
    GMToolReceiptPolicy.apply_context(context, {}, source)

    opportunity = GMToolReceipt.success(
        "resolve_gm_opportunity",
        result={},
        state_changed=True,
    )
    GMToolReceiptPolicy.apply_context(context, {}, opportunity)

    assert opportunity.result["required_followup_tools"] == [
        "decide_npc_response"
    ]
    assert opportunity.result["required_followup_calls"] == [
        {
            "tool_name": "decide_npc_response",
            "arguments": {
                "name": "白花守望会会长",
                "condition_id": "condition-1",
            },
        }
    ]
    assert opportunity.result["required_followup_mode"] == "all"
    assert GMToolReceiptPolicy.required_followup_tools(
        [source, opportunity]
    ) == {"decide_npc_response"}

    payoff = GMToolReceipt.success(
        "decide_npc_response",
        result={},
        state_changed=True,
    )
    GMToolReceiptPolicy.apply_context(context, {}, payoff)

    assert GMToolReceiptPolicy.REQUIRED_FOLLOWUP_CONTEXT_KEY not in context.metadata
    assert GMToolReceiptPolicy.required_followup_tools(
        [source, opportunity, payoff]
    ) is None


def test_all_mode_same_tool_followups_preserve_call_multiplicity() -> None:
    context = _context()
    source = GMToolReceipt.success(
        "perform_check_action",
        result={
            "required_followup_tools": ["decide_npc_response"],
            "required_followup_calls": [
                {
                    "tool_name": "decide_npc_response",
                    "arguments": {
                        "name": "白花守望会会长",
                        "condition_id": "condition-1",
                    },
                },
                {
                    "tool_name": "decide_npc_response",
                    "arguments": {
                        "name": "白花巡守",
                        "commitment_id": "commitment-1",
                    },
                },
            ],
            "required_followup_mode": "all",
        },
        state_changed=True,
    )
    GMToolReceiptPolicy.apply_context(context, {}, source)

    first_payoff = GMToolReceipt.success(
        "decide_npc_response",
        result={},
        state_changed=True,
    )
    GMToolReceiptPolicy.apply_context(context, {}, first_payoff)

    assert first_payoff.result["required_followup_tools"] == [
        "decide_npc_response"
    ]
    assert first_payoff.result["required_followup_calls"] == [
        {
            "tool_name": "decide_npc_response",
            "arguments": {
                "name": "白花巡守",
                "commitment_id": "commitment-1",
            },
        }
    ]

    second_payoff = GMToolReceipt.success(
        "decide_npc_response",
        result={},
        state_changed=True,
    )
    GMToolReceiptPolicy.apply_context(context, {}, second_payoff)

    assert GMToolReceiptPolicy.REQUIRED_FOLLOWUP_CONTEXT_KEY not in context.metadata
    assert GMToolReceiptPolicy.required_followup_tools(
        [source, first_payoff, second_payoff]
    ) is None


def test_followup_with_wrong_stable_identity_does_not_complete_obligation() -> None:
    context = _context()
    source = GMToolReceipt.success(
        "perform_check_action",
        result={
            "required_followup_tools": ["decide_npc_response"],
            "required_followup_calls": [
                {
                    "tool_name": "decide_npc_response",
                    "arguments": {
                        "name": "白花守望会会长",
                        "condition_id": "condition-1",
                    },
                }
            ],
            "required_followup_mode": "all",
        },
        state_changed=True,
    )
    GMToolReceiptPolicy.apply_context(context, {}, source)

    wrong_npc = GMToolReceipt.success(
        "decide_npc_response",
        result={},
        state_changed=True,
    )
    GMToolReceiptPolicy.apply_context(
        context,
        {},
        wrong_npc,
        tool_arguments={
            "name": "白花巡守",
            "condition_id": "condition-1",
        },
    )

    assert wrong_npc.result["required_followup_tools"] == [
        "decide_npc_response"
    ]
    assert GMToolReceiptPolicy.required_followup_calls([source, wrong_npc]) == [
        {
            "tool_name": "decide_npc_response",
            "arguments": {
                "name": "白花守望会会长",
                "condition_id": "condition-1",
            },
        }
    ]
    assert not GMToolReceiptPolicy.followup_call_matches(
        [source, wrong_npc],
        tool_name="decide_npc_response",
        arguments={
            "name": "白花巡守",
            "condition_id": "condition-1",
        },
    )
    assert GMToolReceiptPolicy.followup_call_matches(
        [source, wrong_npc],
        tool_name="decide_npc_response",
        arguments={
            "name": "白花守望会会长",
            "condition_id": "condition-1",
            "public_segments": [{"kind": "speech", "text": "旧路会打开。"}],
        },
    )


def test_locked_reply_keeps_only_latest_structured_public_state_lines() -> None:
    movement = GMToolReceipt.success(
        "move_scene_group",
        result={"public_state_lines": ["【财团巡逻队逼近】4/8"]},
        state_changed=True,
        public_reply=(
            "苍祈与白花守望者抵达旧路入口。\n"
            "【财团巡逻队逼近】4/8"
        ),
        lock_public_reply=True,
    )
    payoff = GMToolReceipt.success(
        "decide_npc_response",
        result={"public_state_lines": ["【财团巡逻队逼近】4/8"]},
        state_changed=True,
        public_reply=(
            "白花守望者抬手示意众人沿内侧前进。\n"
            "【财团巡逻队逼近】4/8"
        ),
        lock_public_reply=True,
    )

    reply = GMToolReceiptPolicy.locked_public_reply([movement, payoff])

    assert reply == (
        "苍祈与白花守望者抵达旧路入口。\n"
        "白花守望者抬手示意众人沿内侧前进。\n"
        "【财团巡逻队逼近】4/8"
    )


def test_locked_read_reply_is_superseded_by_later_state_change() -> None:
    readiness = GMToolReceipt.success(
        "get_session_zero_readiness",
        result={"has_session_zero_context": False},
        public_reply="当前还没有开启第零章。",
        lock_public_reply=True,
    )
    commit = GMToolReceipt.success(
        "commit_session_zero_update",
        result={"applied_fields": ["world_shape"]},
        state_changed=True,
        public_reply="好，记下了。",
    )

    assert GMToolReceiptPolicy.locked_public_reply([readiness, commit]) == ""
    assert GMToolReceiptPolicy.authoritative_reply([readiness, commit]) == (
        "好，记下了。"
    )


def test_locked_read_reply_after_state_change_remains_current() -> None:
    commit = GMToolReceipt.success(
        "commit_session_zero_update",
        state_changed=True,
        public_reply="好，记下了。",
    )
    readiness = GMToolReceipt.success(
        "get_session_zero_readiness",
        result={"has_session_zero_context": True},
        public_reply="第零章已经开启。",
        lock_public_reply=True,
    )

    assert GMToolReceiptPolicy.locked_public_reply([commit, readiness]) == (
        "第零章已经开启。"
    )


def test_locked_reply_deduplicates_whitespace_only_variants() -> None:
    check = GMToolReceipt.success(
        "resolve_rule_window",
        state_changed=True,
        public_reply=(
            "诺艾尔进行突围检定：结算值 11 对抗难度等级 10，成功！\n"
            "诺艾尔抵达监狱外。"
        ),
        lock_public_reply=True,
    )
    conflict_end = GMToolReceipt.success(
        "end_conflict",
        state_changed=True,
        public_reply=(
            "诺艾尔进行突围检定：结算值 11 对抗难度等级10，成功！\n"
            "诺艾尔抵达监狱外。"
        ),
        lock_public_reply=True,
    )

    assert GMToolReceiptPolicy.locked_public_reply([check, conflict_end]) == (
        "诺艾尔进行突围检定：结算值 11 对抗难度等级 10，成功！\n"
        "诺艾尔抵达监狱外。"
    )


def test_locked_reply_deduplicates_shared_turn_handoff_across_tools() -> None:
    notice = "@南星，轮到【赛璃】了；刚才缓存的是：目标：伊莉雅。要改动作就直接说新的动作。"
    zero_hp = GMToolReceipt.success(
        "resolve_rule_window",
        state_changed=True,
        public_reply=f"艾薇娅被财团带离现场。\n{notice}",
        lock_public_reply=True,
    )
    deferred_action = GMToolReceipt.success(
        "declare_check_action",
        state_changed=True,
        public_reply=f"洛岚敲中机兵联轴，令它陷入迟缓。\n{notice}",
        lock_public_reply=True,
    )

    reply = GMToolReceiptPolicy.locked_public_reply([zero_hp, deferred_action])

    assert reply == (
        "艾薇娅被财团带离现场。\n"
        "洛岚敲中机兵联轴，令它陷入迟缓。\n"
        f"{notice}"
    )
    assert reply.count(notice) == 1
