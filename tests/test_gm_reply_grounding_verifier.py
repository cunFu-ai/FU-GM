from __future__ import annotations

import json
from copy import deepcopy

from fu_gm.components.gm_reply_grounding_verifier import (
    GROUNDING_EVIDENCE_PROTOCOL,
    GMReplyGroundingReview,
    GMReplyGroundingVerifier,
    SILENCE_RESPONSIBILITY_SYSTEM_PROMPT,
    TOOL_PROPOSAL_BATCH_GROUNDING_SYSTEM_PROMPT,
    TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT,
)
from fu_gm.components.gm_agent_prompts import (
    CORE_GM_SYSTEM_PROMPT,
    HEARTBEAT_SYSTEM_PROMPT,
)
from fu_gm.gm_tool_agent import LLMGMToolAgent
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolReceipt,
    GMToolRegistry,
)


class ScriptedClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []
        self.config = type("Config", (), {"response_format_enabled": True})()

    def create_chat_completion(self, **kwargs) -> str:
        self.calls.append(dict(kwargs))
        return self.responses.pop(0)


def test_conditional_player_proposal_is_not_an_authorized_action() -> None:
    assert "要不要我试试" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    assert "不能调用declare_check_action" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    assert "仍只是等待队友确认的提议" in SILENCE_RESPONSIBILITY_SYSTEM_PROMPT


def _assert_bounded_non_thinking_json_call(call: dict[str, object]) -> None:
    assert call["thinking_enabled"] is False
    assert call["max_recovery_retries"] == 1
    assert call["retry_without_response_format_on_empty"] is True
    assert call["response_format"] == {"type": "json_object"}


def _context() -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="grounding-test",
        session_id="s1",
        channel_id="group-1",
        speaker="阿凛",
        gate_status="adventure",
        directly_addressed=False,
    )


def test_grounding_prompt_distinguishes_sensory_color_from_state_changes() -> None:
    assert "潮雾贴着廊柱" in GROUNDING_EVIDENCE_PROTOCOL
    assert "NPC已经拒绝或答应" in GROUNDING_EVIDENCE_PROTOCOL
    assert "不要要求把其余合规的感官描写一起改成概括性说明或行动菜单" in (
        GROUNDING_EVIDENCE_PROTOCOL
    )


def test_tool_grounding_prompt_uses_receipt_continuation_for_move_then_check() -> None:
    assert "continue_with_check=true" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    assert "continue_with_rule_action=true" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    assert "调用对应专用规则工具" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "declare_movement_check不接受continue_with_check" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    assert "一次declare_movement_check可以同时裁定抵达与一个具体静态发现" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "成功抵达本句落点，失败只改变该阻碍附近的处境" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "寻找、探索、逃离、追踪等方向性目标只证明行动方向" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "权威场景已经确认并与当前位置直接相连的下一处地点" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "额外的物件、线索或静态发现须对应玩家同句明确执行" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "success_observation、success_transition、purpose、obstacle和failure_consequence" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "本事务刚触发且已精确登记该后果" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "命刻、到期承诺、当前NPC行动或结构化场景危害" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "move_group_within_scene" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    assert "行动者独自移动时companions必须为空" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "仅仅在场、被看见、被交谈或提醒" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "设置continue_with_rule_action=true并调用对应专用规则工具" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "position_note只记录玩家角色站位" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "current_scene只是当前镜头" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    assert "declare_check_action等行动工具会在执行前把镜头聚焦" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "gm_must_repair" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    assert "needs_player_clarification" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    assert "base_observation可以由GM确认该对象存在" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "不要求先调用commit_scene_response" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "条件结果契约" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    assert "不得仅因该答案此前未公开就判为unsupported" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "眼前少量人物的准确人数" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "不应为了“再仔细数一遍”另设检定" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "timing=defer时明确只缓存" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    assert "不声称检定、攻击、法术、仪式或技能已经执行" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "declare_check_action、declare_movement_check" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "自动缓存" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    assert "当前行动者是NPC" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    assert "不能单独成为contradicts_state或gm_must_repair的理由" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "审计对象仅是proposed_tool的字段" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "current_message只是证据来源，不是待写入声明" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "fact_effects声明本轮新产生的持续事实" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "kind=objective" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    assert "kind=claim、rumor、lie" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    assert "不得仅因“此前没有写过”而拒绝" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "预备行动" in SILENCE_RESPONSIBILITY_SYSTEM_PROMPT
    assert "request_fulfilled应为true" in SILENCE_RESPONSIBILITY_SYSTEM_PROMPT
    assert "等待角色B确认的分工提议" in SILENCE_RESPONSIBILITY_SYSTEM_PROMPT
    assert "玩家在本句中声称" in SILENCE_RESPONSIBILITY_SYSTEM_PROMPT


def test_heartbeat_prompt_exposes_post_tool_narrative_fields() -> None:
    assert '"resolution_reply"' in HEARTBEAT_SYSTEM_PROMPT
    assert '"independent_reply"' in HEARTBEAT_SYSTEM_PROMPT
    assert "当前不在participants中的现有或新人物" in HEARTBEAT_SYSTEM_PROMPT
    assert "必须使用introduce_npc完成登场" in HEARTBEAT_SYSTEM_PROMPT


def test_core_prompt_requires_key_npc_to_answer_a_permission_request() -> None:
    assert "NPC正掌握当前许可、条件或现场决定" in CORE_GM_SYSTEM_PROMPT
    assert "即使没有问号" in CORE_GM_SYSTEM_PROMPT
    assert "也是要求该NPC据此表态" in CORE_GM_SYSTEM_PROMPT


def test_grounding_prompt_treats_introduce_npc_as_atomic_arrival() -> None:
    assert "场外具名NPC改用introduce_npc" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "introduce_npc本身会原子提交" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "把公开描述中明确指认的普通随从放入introduced_npcs" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "同一批次重复调用introduce_npc不构成合法的群体登场" in (
        TOOL_PROPOSAL_BATCH_GROUNDING_SYSTEM_PROMPT
    )


def test_grounding_prompt_forbids_replacing_an_explicit_target_with_collective() -> None:
    prompt = TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT

    assert "把具名个体替换为其所属集体" in prompt
    assert "而不是代玩家改目标" in prompt
    assert "start_conflict必须保留" in prompt


def test_semantic_grounding_verifier_parses_unsupported_claims() -> None:
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "valid": False,
                    "request_fulfilled": False,
                    "category": "unsupported_external_result",
                    "unsupported_claims": ["守卫已经倒下并被缴械"],
                    "correction_hint": "先使用冲突或场景工具提交结果。",
                },
                ensure_ascii=False,
            )
        ]
    )
    verifier = GMReplyGroundingVerifier(client, model="semantic-model")

    review = verifier.verify(
        current_message="我在对方倒下后收走他的武器。",
        recent_context="双方还在交涉。",
        observed_state={"conflict": {"active": False}},
        receipts=[],
        proposed_reply="守卫倒在地上，武器已经被你收走。",
        message_kind="performed_action",
        decision_reason="接受玩家行动。",
        deadline=999999999.0,
    )

    assert review.valid is False
    assert review.request_fulfilled is False
    assert review.category == "unsupported_external_result"
    assert review.unsupported_claims == ("守卫已经倒下并被缴械",)
    assert client.calls[0]["operation"] == "gm_reply_grounding_verification"
    _assert_bounded_non_thinking_json_call(client.calls[0])
    assert client.calls[0]["messages"][0].cache_family == "ground-reply"
    assert client.calls[0]["messages"][0].cache_breakpoint is True
    assert client.calls[0]["messages"][1].cache_breakpoint is True


def test_semantic_silence_responsibility_verifier_detects_table_fact_question() -> None:
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "requires_gm_reply": True,
                    "category": "table_fact_clarification",
                    "reason": "玩家在核对最近公开聊天中是否有人提过庄园。",
                },
                ensure_ascii=False,
            )
        ]
    )
    verifier = GMReplyGroundingVerifier(client, model="semantic-model")

    review = verifier.verify_silence_responsibility(
        current_message="诺艾尔皱眉：刚才是谁提到了庄园？我没听清。",
        recent_context="灰耳只提到了地下铁匣和看守手上的冷光。",
        gate_status="adventure",
        proposed_message_kind="discussion",
        proposed_audience="players",
        decision_reason="误判为角色内闲聊。",
        deadline=999999999.0,
        proposed_delivery={
            "mode": "normal",
            "semantic_targets": ["主持人"],
        },
        has_independent_followup=True,
        proposed_public_reply="我先把缺项理一遍，稍后给你们几个方向。",
        completed_receipts=[
            GMToolReceipt.success(
                "create_world_setting",
                result={
                    "operation": "create",
                    "category": "kingdoms",
                    "name": "索朗帝国",
                    "value": "一个很富饶的王国",
                    "silent_commit_allowed": True,
                    "source_message_already_public": True,
                    "completion_scope": "source_statement",
                    "changed_fields": ["kingdoms"],
                },
                state_changed=True,
            )
        ],
    )

    assert review.requires_gm_reply is True
    assert review.category == "table_fact_clarification"
    assert client.calls[0]["operation"] == "gm_silence_responsibility_verification"
    _assert_bounded_non_thinking_json_call(client.calls[0])
    assert client.calls[0]["max_tokens"] == 480
    assert client.calls[0]["messages"][0].cache_family == "route-silence"
    assert "不负责回答玩家" in SILENCE_RESPONSIBILITY_SYSTEM_PROMPT
    assert "没有明确向另一名PC或NPC发问" in SILENCE_RESPONSIBILITY_SYSTEM_PROMPT
    request = json.loads(client.calls[0]["messages"][-1].content)
    semantics = request["core_proposed_semantics"]
    assert request["proposed_public_reply"] == (
        "我先把缺项理一遍，稍后给你们几个方向。"
    )
    assert semantics["delivery"]["semantic_targets"] == ["主持人"]
    assert semantics["has_independent_followup"] is True
    completed = request["completed_tool_receipts"]
    assert completed[0]["tool_name"] == "create_world_setting"
    assert completed[0]["result"]["name"] == "索朗帝国"
    assert completed[0]["result"]["silent_commit_allowed"] is True
    assert completed[0]["result"]["source_message_already_public"] is True
    assert completed[0]["result"]["completion_scope"] == "source_statement"
    assert "delegated_gm_task" in SILENCE_RESPONSIBILITY_SYSTEM_PROMPT
    assert "不能把“后续再处理”当作完成" in SILENCE_RESPONSIBILITY_SYSTEM_PROMPT
    assert "不是可持续后台任务" in SILENCE_RESPONSIBILITY_SYSTEM_PROMPT
    assert "只读回执可以完成请求" in SILENCE_RESPONSIBILITY_SYSTEM_PROMPT
    assert "即使玩家没有重复艾特时悠也需要回应" in SILENCE_RESPONSIBILITY_SYSTEM_PROMPT
    assert "不能作为修改对方角色卡的授权" in SILENCE_RESPONSIBILITY_SYSTEM_PROMPT
    assert "追问为何没回复" in SILENCE_RESPONSIBILITY_SYSTEM_PROMPT
    assert "不得仅因没有艾特就归为player_discussion" in (
        SILENCE_RESPONSIBILITY_SYSTEM_PROMPT
    )
    assert "成功写入回执只是后台登记" in SILENCE_RESPONSIBILITY_SYSTEM_PROMPT
    assert "不得强迫核心GM在同一个HTTP请求中立即追问下一项" in (
        SILENCE_RESPONSIBILITY_SYSTEM_PROMPT
    )


def test_completion_review_accepts_a_finished_session_zero_opening() -> None:
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "request_fulfilled": True,
                    "category": "direct_gm_request",
                    "reason": "场次已开启，拟回复已提出首个讨论问题。",
                },
                ensure_ascii=False,
            )
        ]
    )
    verifier = GMReplyGroundingVerifier(client, model="semantic-model")

    review = verifier.verify_silence_responsibility(
        current_message="请开始第零章，先聊基调和安全边界。",
        recent_context="",
        gate_status="session_zero",
        proposed_message_kind="gm_request",
        proposed_audience="players",
        decision_reason="开启第零章讨论。",
        deadline=999999999.0,
        proposed_public_reply=(
            "好，我们先从基调聊起。大家希望故事更偏严肃正剧，"
            "还是明快的王道冒险？"
        ),
        completed_receipts=[
            GMToolReceipt.success(
                "start_session",
                result={"session_zero_opening_required": True},
                state_changed=True,
            )
        ],
    )

    assert review.requires_gm_reply is False
    request = json.loads(client.calls[0]["messages"][-1].content)
    assert request["review_question"].startswith("发布proposed_public_reply之后")


def test_grounding_prompt_treats_unintroduced_npc_name_as_private() -> None:
    from fu_gm.components.gm_reply_grounding_verifier import (
        REPLY_GROUNDING_SYSTEM_PROMPT,
    )

    assert "不能证明玩家已经知道" in REPLY_GROUNDING_SYSTEM_PROMPT
    assert "隔壁牢房的人" in REPLY_GROUNDING_SYSTEM_PROMPT
    assert "private_fact_disclosure" in REPLY_GROUNDING_SYSTEM_PROMPT
    assert "不授予公开权限" in REPLY_GROUNDING_SYSTEM_PROMPT


def test_semantic_grounding_verifier_reviews_tool_before_write() -> None:
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "valid": False,
                    "category": "false_premise",
                    "unsupported_claims": ["会长此前提到过庄园"],
                    "correction_hint": "澄清当前公开对话中没人提到庄园。",
                },
                ensure_ascii=False,
            )
        ]
    )
    verifier = GMReplyGroundingVerifier(client, model="semantic-model")
    prior_receipt = GMToolReceipt.success(
        "start_session",
        result={"required_followup_tools": ["start_scene"]},
        state_changed=True,
    )
    rejected_receipt = GMToolReceipt.failure(
        "confirm_session_zero_proposal",
        "PROPOSAL_REPLACEMENT_SCOPE_MISMATCH",
        "上一版修订越出范围。",
        "删除越界类别后重试。",
        result={"replacement_scope": [{"category": "world_shape"}]},
    )

    review = verifier.verify_tool_proposal(
        current_message="刚才是谁提到了庄园？",
        recent_context="会长只说了东侧堤脊。",
        observed_state={"scene": {"public_facts": ["东侧堤脊可以绕行"]}},
        tool_name="decide_npc_response",
        arguments={
            "name": "守望会会长",
            "public_segments": [{"text": "庄园是我刚才提到的。"}],
        },
        deadline=999999999.0,
        receipts=[prior_receipt, rejected_receipt],
    )

    assert review.valid is False
    assert review.category == "false_premise"
    assert client.calls[0]["operation"] == "gm_tool_proposal_grounding_verification"
    _assert_bounded_non_thinking_json_call(client.calls[0])
    assert client.calls[0]["messages"][0].cache_family == "ground-tool"
    assert client.calls[0]["messages"][0].cache_breakpoint is True
    assert client.calls[0]["messages"][1].cache_breakpoint is True
    request = json.loads(client.calls[0]["messages"][1].content)
    assert request["prior_tool_receipts"][0]["tool_name"] == "start_session"
    assert request["prior_tool_receipts"][0]["result"][
        "required_followup_tools"
    ] == ["start_scene"]
    assert len(request["prior_tool_receipts"]) == 1


def _same_target_dual_wield_state() -> dict[str, object]:
    return {
        "gameplay": {
            "controlled_characters": ["星澜"],
            "characters": [
                {
                    "name": "星澜",
                    "can_act": True,
                    "equipment_inventory": ["无防具", "晨星匕首", "暮影匕首"],
                    "equipment_templates": {
                        "晨星匕首": "钢匕首",
                        "暮影匕首": "钢匕首",
                    },
                    "equipped": {
                        "main_hand": "晨星匕首",
                        "off_hand": "暮影匕首",
                    },
                }
            ],
            "conflict": {"active": True},
        },
        "runtime": {
            "conflict": {
                "active": True,
                "resolution_status": {
                    "active_hostiles": ["赤炉大将", "熔炉侍从"],
                },
            }
        },
    }


def _same_target_dual_wield_arguments() -> dict[str, object]:
    return {
        "action_type": "Attack",
        "actor": "星澜",
        "target": "赤炉大将",
        "timing": "immediate",
        "details": {
            "dual_wield": True,
            "targets": ["赤炉大将", "赤炉大将"],
        },
        "source_event_id": "message:boss-probe-087",
    }


def _review_same_target_dual_wield(
    *,
    message: str,
    state: dict[str, object] | None = None,
    arguments: dict[str, object] | None = None,
) -> tuple[GMReplyGroundingReview, ScriptedClient]:
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "valid": False,
                    "category": "contradicts_state",
                    "unsupported_claims": ["fallback semantic rejection"],
                    "correction_hint": "do not accept",
                },
                ensure_ascii=False,
            )
        ]
    )
    verifier = GMReplyGroundingVerifier(client, model="semantic-model")
    review = verifier.verify_tool_proposal(
        current_message=message,
        recent_context="",
        observed_state=(
            state if state is not None else _same_target_dual_wield_state()
        ),
        tool_name="perform_character_action",
        arguments=(
            arguments
            if arguments is not None
            else _same_target_dual_wield_arguments()
        ),
        deadline=999999999.0,
    )
    return review, client


def test_exact_same_target_dual_wield_uses_authoritative_local_grounding() -> None:
    review, client = _review_same_target_dual_wield(
        message=(
            "星澜的MP不足以继续施法，改用双武器攻击；"
            "晨星匕首和暮影匕首都攻击赤炉大将，"
            "分别进行两次真实命中检定。"
        )
    )

    assert review == GMReplyGroundingReview(
        valid=True,
        category="local_authoritative_same_target_dual_wield",
    )
    assert client.calls == []


def test_local_dual_wield_grounding_respects_explicit_negation() -> None:
    review, client = _review_same_target_dual_wield(
        message=(
            "星澜不使用双持，也不要双武器；晨星匕首和暮影匕首"
            "都攻击赤炉大将，分别进行两次真实命中检定。"
        )
    )

    assert review.valid is False
    assert client.calls[0]["operation"] == "gm_tool_proposal_grounding_verification"


def test_local_dual_wield_grounding_does_not_invent_a_second_target() -> None:
    arguments = _same_target_dual_wield_arguments()
    arguments["details"] = {
        "dual_wield": True,
        "targets": ["赤炉大将"],
    }

    review, client = _review_same_target_dual_wield(
        message=(
            "星澜使用晨星匕首和暮影匕首双武器攻击赤炉大将，"
            "进行两次真实命中检定。"
        ),
        arguments=arguments,
    )

    assert review.valid is False
    assert client.calls[0]["operation"] == "gm_tool_proposal_grounding_verification"


def test_local_dual_wield_grounding_requires_literal_second_strike_target() -> None:
    review, client = _review_same_target_dual_wield(
        message=(
            "星澜用晨星匕首和暮影匕首进行双武器攻击；"
            "晨星匕首攻击赤炉大将，暮影匕首的目标尚未声明，"
            "随后进行两次真实命中检定。"
        )
    )

    assert review.valid is False
    assert client.calls[0]["operation"] == "gm_tool_proposal_grounding_verification"


def test_local_dual_wield_grounding_requires_both_equipped_weapons_in_message() -> None:
    review, client = _review_same_target_dual_wield(
        message=(
            "星澜改用双武器攻击；晨星匕首攻击赤炉大将，"
            "分别进行两次真实命中检定。"
        )
    )

    assert review.valid is False
    assert client.calls[0]["operation"] == "gm_tool_proposal_grounding_verification"


def test_local_dual_wield_grounding_rejects_fabricated_off_hand() -> None:
    state = _same_target_dual_wield_state()
    character = state["gameplay"]["characters"][0]
    character["equipped"]["off_hand"] = ""

    review, client = _review_same_target_dual_wield(
        message=(
            "星澜改用双武器攻击；晨星匕首和暮影匕首都攻击赤炉大将，"
            "分别进行两次真实命中检定。"
        ),
        state=state,
    )

    assert review.valid is False
    assert client.calls[0]["operation"] == "gm_tool_proposal_grounding_verification"


def test_local_dual_wield_grounding_requires_authoritative_active_hostile() -> None:
    state = _same_target_dual_wield_state()
    state["runtime"]["conflict"]["resolution_status"]["active_hostiles"] = [
        "熔炉侍从"
    ]

    review, client = _review_same_target_dual_wield(
        message=(
            "星澜改用双武器攻击；晨星匕首和暮影匕首都攻击赤炉大将，"
            "分别进行两次真实命中检定。"
        ),
        state=state,
    )

    assert review.valid is False
    assert client.calls[0]["operation"] == "gm_tool_proposal_grounding_verification"


def test_local_dual_wield_grounding_does_not_rewrite_same_target_intent() -> None:
    arguments = _same_target_dual_wield_arguments()
    arguments["details"] = {
        "dual_wield": True,
        "targets": ["赤炉大将", "熔炉侍从"],
    }

    review, client = _review_same_target_dual_wield(
        message=(
            "星澜改用双武器攻击；晨星匕首和暮影匕首都攻击赤炉大将，"
            "分别进行两次真实命中检定。"
        ),
        arguments=arguments,
    )

    assert review.valid is False
    assert client.calls[0]["operation"] == "gm_tool_proposal_grounding_verification"


def _known_spell_state() -> dict[str, object]:
    return {
        "gameplay": {
            "controlled_characters": ["星澜"],
            "characters": [
                {
                    "name": "星澜",
                    "spells": ["元素幕障", "炎弹"],
                }
            ],
        }
    }


def _barrier_spell_arguments() -> dict[str, object]:
    return {
        "action_type": "Spell",
        "actor": "星澜",
        "target": "诺艾尔",
        "timing": "immediate",
        "details": {
            "spell_name": "元素幕障",
            "element": "火",
            "targets": ["诺艾尔", "星澜"],
        },
    }


def _review_known_spell(
    *,
    message: str,
    state: dict[str, object] | None = None,
    arguments: dict[str, object] | None = None,
) -> tuple[GMReplyGroundingReview, ScriptedClient]:
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "valid": False,
                    "category": "gm_must_repair",
                    "unsupported_claims": ["fallback semantic rejection"],
                    "correction_hint": "do not accept",
                },
                ensure_ascii=False,
            )
        ]
    )
    verifier = GMReplyGroundingVerifier(client, model="semantic-model")
    review = verifier.verify_tool_proposal(
        current_message=message,
        recent_context="",
        observed_state=state if state is not None else _known_spell_state(),
        tool_name="perform_character_action",
        arguments=(
            arguments if arguments is not None else _barrier_spell_arguments()
        ),
        deadline=999999999.0,
    )
    return review, client


def test_literal_elemental_barrier_uses_authoritative_known_spell_grounding() -> None:
    review, client = _review_known_spell(
        message="星澜施放元素幕障，选择火元素，保护诺艾尔和星澜。"
    )

    assert review == GMReplyGroundingReview(
        valid=True,
        category="local_authoritative_known_spell_intent",
    )
    assert client.calls == []


def test_literal_fire_spell_uses_authoritative_known_spell_grounding() -> None:
    review, client = _review_known_spell(
        message="星澜施放炎弹攻击赤炉大将，选择火焰伤害并按真实骰子结算。",
        arguments={
            "action_type": "Spell",
            "actor": "星澜",
            "target": "赤炉大将",
            "timing": "immediate",
            "details": {
                "spell_name": "炎弹",
                "chosen_damage_type": "fire",
                "targets": ["赤炉大将"],
            },
        },
    )

    assert review.valid is True
    assert review.category == "local_authoritative_known_spell_intent"
    assert client.calls == []


def test_known_spell_grounding_respects_explicit_negation() -> None:
    review, client = _review_known_spell(
        message="星澜不施放元素幕障，也不保护诺艾尔和星澜。"
    )

    assert review.valid is False
    assert client.calls[0]["operation"] == "gm_tool_proposal_grounding_verification"


def test_known_spell_grounding_rejects_fictional_spell() -> None:
    arguments = _barrier_spell_arguments()
    arguments["details"] = {
        "spell_name": "虚空幕障",
        "element": "火",
        "targets": ["诺艾尔", "星澜"],
    }
    review, client = _review_known_spell(
        message="星澜施放虚空幕障，选择火元素，保护诺艾尔和星澜。",
        arguments=arguments,
    )

    assert review.valid is False
    assert client.calls[0]["operation"] == "gm_tool_proposal_grounding_verification"


def test_known_spell_grounding_rejects_changed_target() -> None:
    review, client = _review_known_spell(
        message="星澜施放炎弹攻击赤炉大将，选择火焰伤害。",
        arguments={
            "action_type": "Spell",
            "actor": "星澜",
            "target": "熔炉侍从",
            "timing": "immediate",
            "details": {
                "spell_name": "炎弹",
                "element": "火",
                "targets": ["熔炉侍从"],
            },
        },
    )

    assert review.valid is False
    assert client.calls[0]["operation"] == "gm_tool_proposal_grounding_verification"


def test_known_spell_grounding_rejects_unmentioned_element() -> None:
    arguments = _barrier_spell_arguments()
    arguments["details"] = {
        "spell_name": "元素幕障",
        "chosen_damage_type": "earth",
        "targets": ["诺艾尔", "星澜"],
    }
    review, client = _review_known_spell(
        message="星澜施放元素幕障，选择火元素，保护诺艾尔和星澜。",
        arguments=arguments,
    )

    assert review.valid is False
    assert client.calls[0]["operation"] == "gm_tool_proposal_grounding_verification"


def _natural_end_conflict_state() -> dict[str, object]:
    return {
        "runtime": {
            "conflict": {
                "active": True,
                "resolution_status": {
                    "ready_for_natural_end": True,
                    "natural_outcome": "hostile_side_removed",
                    "active_player_side": ["诺艾尔", "星澜"],
                    "active_hostiles": [],
                    "defeated_combatants": ["熔炉侍从"],
                    "escaped_combatants": ["赤炉大将"],
                    "pending_exit_transitions": [],
                    "pending_zero_hp_characters": [],
                },
            }
        },
        "gameplay": {"pending_decisions": []},
        "processes": {"decisions": {"pending": []}},
    }


def _review_natural_end_conflict(
    *,
    state: dict[str, object] | None = None,
    arguments: dict[str, object] | None = None,
) -> tuple[GMReplyGroundingReview, ScriptedClient]:
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "valid": False,
                    "category": "gm_must_repair",
                    "unsupported_claims": ["fallback semantic rejection"],
                    "correction_hint": "do not accept",
                },
                ensure_ascii=False,
            )
        ]
    )
    verifier = GMReplyGroundingVerifier(client, model="semantic-model")
    review = verifier.verify_tool_proposal(
        current_message=(
            "权威冲突状态显示一方已经没有可行动成员。"
            "请只调用end_conflict提交自然结局。"
        ),
        recent_context="",
        observed_state=(
            state if state is not None else _natural_end_conflict_state()
        ),
        tool_name="end_conflict",
        arguments=(
            arguments
            if arguments is not None
            else {
                "outcome": "hostile_side_removed",
                "continue_scene": True,
            }
        ),
        deadline=999999999.0,
    )
    return review, client


def test_minimal_authoritative_natural_end_conflict_stays_local() -> None:
    review, client = _review_natural_end_conflict()

    assert review == GMReplyGroundingReview(
        valid=True,
        category="local_authoritative_natural_end_conflict",
    )
    assert client.calls == []


def test_natural_end_conflict_rejects_extra_narrative_or_transitions() -> None:
    for extra in (
        {"public_reply": "赤炉大将逃入熔炉深处，战斗结束。"},
        {"exit_transitions": []},
        {"creative_direction": "写成壮烈大结局"},
    ):
        review, client = _review_natural_end_conflict(
            arguments={
                "outcome": "hostile_side_removed",
                "continue_scene": True,
                **extra,
            }
        )

        assert review.valid is False
        assert client.calls[0]["operation"] == (
            "gm_tool_proposal_grounding_verification"
        )


def test_natural_end_conflict_rejects_changed_outcome_or_parent_scene_end() -> None:
    for arguments in (
        {
            "outcome": "player_side_removed",
            "continue_scene": True,
        },
        {
            "outcome": "hostile_side_removed",
            "continue_scene": False,
        },
    ):
        review, client = _review_natural_end_conflict(arguments=arguments)

        assert review.valid is False
        assert client.calls[0]["operation"] == (
            "gm_tool_proposal_grounding_verification"
        )


def test_natural_end_conflict_requires_complete_authoritative_invariants() -> None:
    mutations = (
        ("active", False),
        ("ready_for_natural_end", False),
        ("active_player_side", []),
        ("active_hostiles", ["熔炉侍从"]),
        (
            "pending_exit_transitions",
            [{"destination": "外通道", "participants": ["诺艾尔"]}],
        ),
        ("pending_zero_hp_characters", ["星澜"]),
    )
    for field, value in mutations:
        state = _natural_end_conflict_state()
        if field == "active":
            state["runtime"]["conflict"][field] = value
        else:
            state["runtime"]["conflict"]["resolution_status"][field] = value
        review, client = _review_natural_end_conflict(state=state)

        assert review.valid is False
        assert client.calls[0]["operation"] == (
            "gm_tool_proposal_grounding_verification"
        )


def test_natural_end_conflict_rejects_unsettled_npc_fate() -> None:
    state = _natural_end_conflict_state()
    pending_fate = {
        "window_id": "fate-1",
        "kind": "npc_fate",
        "owner": "星澜",
        "scope_kind": "conflict",
        "blocking": True,
        "status": "pending",
    }
    state["gameplay"]["pending_decisions"] = [deepcopy(pending_fate)]
    state["processes"]["decisions"]["pending"] = [deepcopy(pending_fate)]

    review, client = _review_natural_end_conflict(state=state)

    assert review.valid is False
    assert client.calls[0]["operation"] == "gm_tool_proposal_grounding_verification"


def test_natural_end_conflict_fails_closed_without_decision_projection() -> None:
    state = _natural_end_conflict_state()
    state.pop("gameplay")
    state["processes"].pop("decisions")

    review, client = _review_natural_end_conflict(state=state)

    assert review.valid is False
    assert client.calls[0]["operation"] == "gm_tool_proposal_grounding_verification"


def test_agent_passes_prior_receipt_to_required_followup_grounding() -> None:
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="start_session",
            description="开启场次并签发开场义务。",
            handler=lambda _context, _arguments: GMToolReceipt.success(
                "start_session",
                result={
                    "required_followup_tools": ["start_scene"],
                    "opening_contract": {"location": "风铃廊"},
                },
                state_changed=True,
                lock_public_reply=True,
            ),
            side_effect="write",
        )
    )
    registry.register(
        GMToolDefinition(
            name="start_scene",
            description="建立首场。",
            handler=lambda _context, _arguments: GMToolReceipt.success(
                "start_scene",
                state_changed=True,
                public_reply="潮雾压着风铃廊。",
                lock_public_reply=True,
            ),
            side_effect="write",
        )
    )
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "call_tool",
                    "tool_name": "start_session",
                    "arguments": {},
                }
            ),
            json.dumps(
                {
                    "decision": "call_tool",
                    "tool_name": "start_scene",
                    "arguments": {},
                },
                ensure_ascii=False,
            ),
        ]
    )
    captured: list[dict[str, object]] = []

    class CapturingVerifier:
        def verify_tool_proposal(self, **kwargs) -> GMReplyGroundingReview:
            captured.append(dict(kwargs))
            return GMReplyGroundingReview(valid=True)

    agent = LLMGMToolAgent(
        client,
        model="semantic-model",
        registry=registry,
        reply_grounding_verifier=CapturingVerifier(),
    )

    outcome = agent.run(
        "进入第一章。",
        recent_context="时悠已经邀请大家开始。",
        context=_context(),
        state_summary={},
    )

    assert outcome.reply == "潮雾压着风铃廊。"
    assert len(captured) == 1
    prior = captured[0]["receipts"]
    assert isinstance(prior, list)
    assert [receipt.tool_name for receipt in prior] == ["start_session"]


def test_semantic_grounding_verifier_reviews_batch_in_one_model_call() -> None:
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "reviews": [
                        {
                            "proposal_index": 0,
                            "valid": True,
                            "category": "grounded",
                            "unsupported_claims": [],
                            "correction_hint": "",
                        },
                        {
                            "proposal_index": 1,
                            "valid": False,
                            "category": "contradicts_state",
                            "unsupported_claims": ["缺席NPC在当前场景发言"],
                            "correction_hint": "先确认NPC所在分支。",
                        },
                    ]
                },
                ensure_ascii=False,
            )
        ]
    )
    verifier = GMReplyGroundingVerifier(client, model="semantic-model")

    reviews = verifier.verify_tool_proposals(
        recent_context="会长仍在风铃廊。",
        observed_state={"scene": {"participants": ["白花守望会会长"]}},
        proposals=[
            {
                "current_message": "我赶到闸门。",
                "tool_name": "move_scene_group",
                "arguments": {"destination": "旧路闸门"},
            },
            {
                "current_message": "请会长回答。",
                "tool_name": "decide_npc_response",
                "arguments": {"name": "缺席人物"},
            },
        ],
        deadline=999999999.0,
    )

    assert len(client.calls) == 1
    assert client.calls[0]["operation"] == (
        "gm_tool_proposals_grounding_verification"
    )
    _assert_bounded_non_thinking_json_call(client.calls[0])
    assert client.calls[0]["messages"][0].cache_family == "ground-tool-batch"
    assert tuple(review.valid for review in reviews) == (True, False)
    assert reviews[1].unsupported_claims == ("缺席NPC在当前场景发言",)


def test_semantic_grounding_batch_rejects_incomplete_index_set() -> None:
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "reviews": [
                        {
                            "proposal_index": 0,
                            "valid": True,
                            "category": "grounded",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        ]
    )
    verifier = GMReplyGroundingVerifier(client, model="semantic-model")

    try:
        verifier.verify_tool_proposals(
            recent_context="",
            observed_state={},
            proposals=[
                {
                    "current_message": "移动。",
                    "tool_name": "move_scene_group",
                    "arguments": {},
                },
                {
                    "current_message": "回应。",
                    "tool_name": "decide_npc_response",
                    "arguments": {},
                },
            ],
            deadline=999999999.0,
        )
    except ValueError as exc:
        assert "没有逐项返回完整结果" in str(exc)
    else:
        raise AssertionError("不完整的批量审计结果必须失败关闭。")


def test_agent_uses_batch_grounding_once_for_multi_tool_decision() -> None:
    registry = GMToolRegistry()
    executed: list[str] = []
    for tool_name in ("move_scene_group", "decide_npc_response"):
        registry.register(
            GMToolDefinition(
                name=tool_name,
                description=tool_name,
                handler=lambda _context, _arguments, name=tool_name: (
                    executed.append(name)
                    or GMToolReceipt.success(name, state_changed=False)
                ),
                side_effect="read",
            )
        )
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "call_tools",
                    "message_kind": "mixed",
                    "audience": "table",
                    "calls": [
                        {
                            "tool_name": "move_scene_group",
                            "arguments": {},
                        },
                        {
                            "tool_name": "decide_npc_response",
                            "arguments": {},
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "decision": "final",
                    "message_kind": "mixed",
                    "audience": "table",
                    "reply": "处理完成。",
                },
                ensure_ascii=False,
            ),
        ]
    )

    class BatchVerifier:
        def __init__(self) -> None:
            self.batch_calls = 0

        def verify_tool_proposal(self, **_kwargs):
            raise AssertionError("多工具事务不应逐项调用语义模型。")

        def verify_tool_proposals(self, **kwargs):
            self.batch_calls += 1
            return tuple(
                GMReplyGroundingReview(valid=True)
                for _item in kwargs["proposals"]
            )

    verifier = BatchVerifier()
    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=registry,
        reply_grounding_verifier=verifier,
    )

    outcome = agent.run(
        "我移动，同时请会长回答。",
        recent_context="",
        context=_context(),
        state_summary={},
    )

    assert verifier.batch_calls == 1
    assert executed == ["move_scene_group", "decide_npc_response"]
    assert outcome.handled is True
    assert outcome.trace[0]["tool_proposal_grounding_mode"] == "batch"


def test_tool_grounding_prompt_blocks_false_premise_leaks_and_vague_check_answers() -> None:
    assert "不能借这个错误前提" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    assert "物件名、痕迹内容、方向地点或办法本身应具体可验证" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "closing_image" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    assert "由成功回执要求下一步调用declare_check_action" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )
    assert "只提交acquire属于半截意图" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    assert "不等于该PC已经接住或取得" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    assert "不能为了满足action_type枚举而改写成Guard" in (
        TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    )


def test_agent_rejects_guard_substitution_then_uses_movement_check() -> None:
    registry = GMToolRegistry()
    executed: list[str] = []

    registry.register(
        GMToolDefinition(
            name="perform_character_action",
            description="战斗行动。",
            parameters=(
                GMToolParameter("action_type", "string", "行动类型。", required=True),
                GMToolParameter("actor", "string", "行动者。", required=True),
            ),
            handler=lambda _context, _arguments: (
                executed.append("guard")
                or GMToolReceipt.success(
                    "perform_character_action",
                    state_changed=True,
                    public_reply="艾丽妮进入防御。",
                    lock_public_reply=True,
                )
            ),
            side_effect="write",
        )
    )
    registry.register(
        GMToolDefinition(
            name="declare_movement_check",
            description="移动检定。",
            parameters=(
                GMToolParameter("actor", "string", "行动者。", required=True),
                GMToolParameter("destination", "string", "目的地。", required=True),
            ),
            handler=lambda _context, _arguments: (
                executed.append("movement")
                or GMToolReceipt.success(
                    "declare_movement_check",
                    state_changed=True,
                    public_reply="需要进行移动检定。",
                    lock_public_reply=True,
                )
            ),
            side_effect="write",
        )
    )
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "call_tool",
                    "message_kind": "performed_action",
                    "audience": "table",
                    "tool_name": "perform_character_action",
                    "arguments": {"action_type": "Guard", "actor": "艾丽妮"},
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "decision": "call_tool",
                    "message_kind": "performed_action",
                    "audience": "table",
                    "tool_name": "declare_movement_check",
                    "arguments": {"actor": "艾丽妮", "destination": "监狱外"},
                },
                ensure_ascii=False,
            ),
        ]
    )

    class RejectGuardSubstitution:
        def verify_tool_proposal(self, **kwargs) -> GMReplyGroundingReview:
            if kwargs.get("tool_name") == "perform_character_action":
                return GMReplyGroundingReview(
                    valid=False,
                    category="gm_must_repair",
                    unsupported_claims=("玩家没有声明防御行动",),
                    correction_hint="保留撤离原意，改用移动检定工具。",
                )
            return GMReplyGroundingReview(valid=True)

    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=registry,
        reply_grounding_verifier=RejectGuardSubstitution(),
    )

    outcome = agent.run(
        "艾丽妮沿当前已经公开的出口撤离监狱。",
        recent_context="两名看守仍封住出口。",
        context=_context(),
        state_summary={"scene": {"location": "监狱走廊"}},
    )

    assert executed == ["movement"]
    assert outcome.reply == "需要进行移动检定。"
    assert any(
        row.get("tool_name") == "perform_character_action"
        and row.get("valid") is False
        for step in outcome.trace
        for row in step.get("tool_proposal_grounding", [])
    )


def test_agent_returns_unsupported_reply_to_itself_then_uses_scene_tool() -> None:
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="perform_scene_action",
            description="提交场景回应。",
            handler=lambda _context, _arguments: GMToolReceipt.success(
                "perform_scene_action",
                state_changed=True,
                public_reply="守卫仍站在盾后，双方还没有交手。",
                lock_public_reply=True,
            ),
            side_effect="write",
        )
    )
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "final",
                    "message_kind": "performed_action",
                    "audience": "table",
                    "reply": "守卫已经倒下，武器也被收走了。",
                    "reason": "接受玩家声明。",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "decision": "call_tool",
                    "message_kind": "performed_action",
                    "audience": "table",
                    "tool_name": "perform_scene_action",
                    "arguments": {},
                    "reason": "澄清当前权威局面。",
                },
                ensure_ascii=False,
            ),
        ]
    )

    class RejectFirstReply:
        def verify(self, **_kwargs) -> GMReplyGroundingReview:
            return GMReplyGroundingReview(
                valid=False,
                category="unsupported_external_result",
                unsupported_claims=("守卫已经倒下",),
                correction_hint="通过场景工具澄清当前状态。",
            )

    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=registry,
        reply_grounding_verifier=RejectFirstReply(),
    )

    outcome = agent.run(
        "诺艾尔在对方倒下后收走武器。",
        recent_context="守卫仍举盾挡路。",
        context=_context(),
        state_summary={"conflict": {"active": False}},
    )

    assert outcome.reply == "守卫仍站在盾后，双方还没有交手。"
    assert len(client.calls) == 2
    assert any(
        step.get("reply_grounding", {}).get("valid") is False
        for step in outcome.trace
    )


def test_locked_public_tool_reply_bypasses_semantic_review() -> None:
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="resolve_action",
            description="结算行动。",
            handler=lambda _context, _arguments: GMToolReceipt.success(
                "resolve_action",
                state_changed=True,
                public_reply="检定失败，守卫仍守在门前。",
                lock_public_reply=True,
            ),
            side_effect="write",
        )
    )
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "call_tool",
                    "message_kind": "performed_action",
                    "audience": "table",
                    "tool_name": "resolve_action",
                    "arguments": {},
                },
                ensure_ascii=False,
            )
        ]
    )

    class MustNotRun:
        def verify(self, **_kwargs):
            raise AssertionError("锁定工具回执不应再次接受语义审计。")

    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=registry,
        reply_grounding_verifier=MustNotRun(),
    )
    outcome = agent.run(
        "诺艾尔试图撞开守卫。",
        recent_context="",
        context=_context(),
        state_summary={},
    )

    assert outcome.reply == "检定失败，守卫仍守在门前。"


def test_agent_rejects_false_premise_npc_write_before_handler_runs() -> None:
    registry = GMToolRegistry()
    handler_calls: list[dict[str, object]] = []

    def handle(_context, arguments):
        handler_calls.append(dict(arguments))
        return GMToolReceipt.success(
            "decide_npc_response",
            state_changed=True,
            public_reply="没人提到庄园。刚才说的是东侧堤脊。",
            lock_public_reply=True,
        )

    registry.register(
        GMToolDefinition(
            name="decide_npc_response",
            description="提交NPC回应。",
            handler=handle,
            parameters=(
                GMToolParameter("name", "string", "NPC名。", required=True),
                GMToolParameter(
                    "public_segments",
                    "array",
                    "公开回应片段。",
                    required=True,
                ),
            ),
            side_effect="write",
        )
    )
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "call_tool",
                    "message_kind": "npc_or_world_interaction",
                    "audience": "table",
                    "tool_name": "decide_npc_response",
                    "arguments": {
                        "name": "守望会会长",
                        "public_segments": [{"text": "庄园是我刚才提到的。"}],
                    },
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "decision": "call_tool",
                    "message_kind": "npc_or_world_interaction",
                    "audience": "table",
                    "tool_name": "decide_npc_response",
                    "arguments": {
                        "name": "守望会会长",
                        "public_segments": [
                            {"text": "没人提到庄园。刚才说的是东侧堤脊。"}
                        ],
                    },
                },
                ensure_ascii=False,
            ),
        ]
    )

    class RejectThenAccept:
        def __init__(self) -> None:
            self.calls = 0

        def verify_tool_proposal(self, **_kwargs) -> GMReplyGroundingReview:
            self.calls += 1
            if self.calls == 1:
                return GMReplyGroundingReview(
                    valid=False,
                    category="false_premise",
                    unsupported_claims=("会长此前提到过庄园",),
                    correction_hint="澄清没人提过庄园，不要虚构说话者。",
                )
            return GMReplyGroundingReview(valid=True)

    verifier = RejectThenAccept()
    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=registry,
        reply_grounding_verifier=verifier,
    )

    outcome = agent.run(
        "刚才是谁提到了庄园？",
        recent_context="会长只说了东侧堤脊。",
        context=_context(),
        state_summary={"scene": {"public_facts": ["东侧堤脊可以绕行"]}},
    )

    assert outcome.reply == "没人提到庄园。刚才说的是东侧堤脊。"
    assert len(handler_calls) == 1
    assert handler_calls[0]["public_segments"][0]["text"].startswith("没人")
    assert any(
        step.get("tool_proposal_grounding", [{}])[0].get("valid") is False
        for step in outcome.trace
        if step.get("tool_proposal_grounding")
    )


def test_agent_rejects_partial_story_item_acquire_then_commits_final_place_silently() -> None:
    registry = GMToolRegistry()
    handler_calls: list[dict[str, object]] = []

    def handler(_context, arguments):
        handler_calls.append(dict(arguments))
        return GMToolReceipt.success(
            "commit_story_item_action",
            result={"silent_commit_allowed": True},
            state_changed=True,
        )

    registry.register(
        GMToolDefinition(
            name="commit_story_item_action",
            description="原子提交剧情物件的最终状态。",
            handler=handler,
            parameters=(
                GMToolParameter("operation", "string", "最终操作。", required=True),
                GMToolParameter("to_location", "string", "最终地点。"),
            ),
            side_effect="write",
        )
    )
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "call_tool",
                    "message_kind": "performed_action",
                    "audience": "table",
                    "tool_name": "commit_story_item_action",
                    "arguments": {"operation": "acquire"},
                    "reason": "先登记取得。",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "decision": "call_tool",
                    "message_kind": "performed_action",
                    "audience": "table",
                    "tool_name": "commit_story_item_action",
                    "arguments": {
                        "operation": "place",
                        "to_location": "艾丽妮牢房一侧",
                    },
                    "reason": "一次提交动作结束时的最终落点。",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "decision": "final",
                    "audience": "table",
                    "reply": "诺艾尔把铁片抛到了艾丽妮那边。",
                    "reason": "确认动作。",
                },
                ensure_ascii=False,
            ),
        ]
    )

    class RejectPartialAcquire:
        def verify_tool_proposal(self, **kwargs) -> GMReplyGroundingReview:
            arguments = kwargs.get("arguments") or {}
            if arguments.get("operation") == "acquire":
                return GMReplyGroundingReview(
                    valid=False,
                    category="gm_must_repair",
                    unsupported_claims=("只登记取得，遗漏随后抛出的最终落点",),
                    correction_hint="使用place一次提交物件的最终落点，不设置接收者。",
                )
            return GMReplyGroundingReview(valid=True)

    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=registry,
        reply_grounding_verifier=RejectPartialAcquire(),
    )
    message = "诺艾尔捡起细长铁片，和艾丽妮说完后，把铁片从铁栏缝隙抛了过去。"

    outcome = agent.run(
        message,
        recent_context="两人在相邻石牢，中间隔着铁栏。",
        context=_context(),
        state_summary={"scene": {"location": "卡里巴村监狱"}},
    )

    assert handler_calls == [
        {"operation": "place", "to_location": "艾丽妮牢房一侧"}
    ]
    assert outcome.target == "silent"
    assert outcome.reply == ""
    assert outcome.mode == "gm_agent_silent_commit"
    assert any(
        row.get("valid") is False
        for step in outcome.trace
        for row in step.get("tool_proposal_grounding", [])
    )
