from __future__ import annotations

import json
from dataclasses import replace

from fu_gm.testing.luna_player_agent import LunaPlayerAgent, PlayerPersona
from fu_gm.testing.replay_models import LegalActionContext, ReplayStep


class ScriptedClient:
    def __init__(self, responses: list[dict[str, object] | str]) -> None:
        self.responses = [
            item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
            for item in responses
        ]
        self.calls: list[dict[str, object]] = []

    def create_chat_completion(self, **kwargs) -> str:
        self.calls.append(dict(kwargs))
        return self.responses.pop(0)

    def telemetry_payload(self) -> dict[str, object]:
        return {
            "total_calls": len(self.calls),
            "failed_calls": 0,
            "latency": {"sample_count": len(self.calls), "p50_ms": 12},
            "prompt_cache": {
                "enabled": True,
                "eligible_calls": len(self.calls),
                "hit_calls": max(0, len(self.calls) - 1),
                "by_family": [{"family": "fu-pl-v2", "calls": len(self.calls)}],
            },
        }


def _step(
    *,
    speaker: str = "阿凛",
    actor: str = "伊莉雅",
    kind: str = "player_message",
    stage_goal: str = "",
) -> ReplayStep:
    return ReplayStep(
        id="fu-pl-v2-test",
        kind=kind,
        speaker=speaker,
        actor=actor,
        stage_goal=stage_goal,
    )


def _context(**changes: object) -> LegalActionContext:
    values: dict[str, object] = {
        "stage_goal": "测试目标不可见",
        "scene_name": "雨夜石牢",
        "scene_location": "西侧牢区",
        "known_pcs": ["伊莉雅", "赛璃", "洛岚", "艾薇娅", "苍祈"],
        "present_pcs": ["伊莉雅", "赛璃"],
        "present_npcs": ["守卫维蕾娅"],
        "legal_actions": ["调查", "防御", "推进目标"],
        "legal_spells": ["元素幕障"],
        "legal_skills": ["元素魔法"],
        "visible_scene_elements": ["不稳定的牢门符文", "相邻牢房的铁栏"],
        "pc_resources": {"伊莉雅": {"hp": 40, "mp": 45}},
    }
    values.update(changes)
    return LegalActionContext(**values)


def _answer(
    text: str,
    *,
    decision: str = "speak",
    audience: str = "gm",
) -> dict[str, object]:
    return {
        "decision": decision,
        "audience": audience,
        "text": text,
        "reason": "基于公开局面回应。",
    }


def _decision_answer(
    *,
    text: str,
) -> dict[str, object]:
    return {
        **_answer(text),
        "kind": "out_of_character",
        "action_commitment": "answer",
    }


def _pending_review(
    *,
    option: str,
    evidence: str,
    valid: bool = True,
    starts_action: bool = False,
    selected_parameters: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "answers_current_window": valid,
        "selected_option_is_supported": valid,
        "required_parameters_complete": valid,
        "starts_independent_scene_action": starts_action,
        "selected_public_option": option if valid else "",
        "selected_parameters": dict(selected_parameters or {}),
        "evidence": evidence,
        "reason": "只回答了当前选择。" if valid else "没有选择当前机会效果。",
    }


def _open_npc_review(
    *,
    evidence: str,
    valid: bool,
    derivative: bool = False,
) -> dict[str, object]:
    return {
        "eligible_responder": True,
        "directly_answers_refuses_or_counters": valid,
        "takes_clear_incompatible_alternative_action": False,
        "only_asks_derivative_question": derivative,
        "ignores_open_request": not valid and not derivative,
        "evidence": evidence,
        "reason": (
            "已经直接提出反条件。"
            if valid
            else "只继续追问枝节，没有处理NPC索要的碎片。"
        ),
    }


def _action_progress_review(
    *,
    evidence: str,
    valid: bool,
    micro_lane: bool = False,
    controls_other_players: bool = False,
) -> dict[str, object]:
    return {
        "valid_action_progress": valid,
        "mostly_restates_known_information": False,
        "repeats_completed_action": not valid,
        "concrete_new_action": True,
        "grounded_in_public_context": True,
        "materially_advances_current_situation": valid,
        "repeats_micro_investigation_lane": micro_lane,
        "responds_to_current_pressure_or_choice": valid,
        "actionable_result_or_explicit_choice_is_already_public": not valid,
        "uses_public_result_or_answers_choice": valid,
        "opens_another_detail_layer": not valid,
        "procedural_micro_clarification_after_sufficient_plan": False,
        "spends_limited_resource_without_public_tactical_basis": False,
        "matches_prior_rejected_lane": False,
        "reopens_exhausted_npc_knowledge_lane": False,
        "new_public_evidence_reopens_npc_knowledge_lane": False,
        "controls_other_player_characters": controls_other_players,
        "party_action_authorized_by_public_consensus": False,
        "controls_npc_outcome_without_public_answer": False,
        "npc_outcome_already_public": False,
        "movement_claimed": False,
        "movement_is_authorized_by_public_context": False,
        "violates_story_item_custody": False,
        "acts_outside_authoritative_actor_location": False,
        "repeats_resolved_information_delivery": False,
        "evidence": evidence,
        "reason": (
            "行动开始利用NPC刚公开的新路线。"
            if valid
            else "同一扇门已经连续检查并给出可行动结论，这次仍在拆分子部件。"
        ),
    }


def test_explicit_persona_catalog_replaces_default_table() -> None:
    persona = PlayerPersona(
        player_name="阿凛",
        hero_name="伊莉雅",
        table_style="直接",
        character_voice="坚定",
    )

    agent = LunaPlayerAgent(
        use_llm=False,
        personas={"阿凛": persona},
    )

    assert agent.personas == {"阿凛": persona}
    assert agent.guard.player_character_aliases == {"阿凛": "伊莉雅"}


def test_natural_broadcast_can_wait_and_repairs_out_of_turn_action() -> None:
    client = ScriptedClient(
        [
            {
                **_answer("伊莉雅趁机冲向牢门。", audience="gm"),
                "kind": "action",
                "speak_after_ms": 500,
            },
            {
                **_answer("", decision="wait", audience="table"),
                "kind": "wait",
                "speak_after_ms": 0,
            },
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-luna")
    step = replace(
        _step(),
        payload={"natural_broadcast": True},
    )

    utterance = agent.compose(
        step=step,
        legal_context=_context(
            conflict_active=True,
            current_actor="赛璃",
        ),
        recent_public_context="时悠：现在轮到赛璃。",
        natural_table_event={
            "event_id": 8,
            "speaker": "时悠",
            "role": "gm",
            "text": "现在轮到赛璃。",
            "action_bar": {"you_are_current_actor": False},
        },
    )

    assert utterance.decision == "wait"
    assert utterance.text == ""
    assert len(client.calls) == 2
    assert "不能抢先行动" in client.calls[1]["messages"][1].content


def test_natural_player_can_address_another_player_by_table_name() -> None:
    client = ScriptedClient(
        [
            {
                **_answer("白河，你觉得先定内海还是先定国家？", audience="player"),
                "kind": "table_discussion",
                "reply_to_event_id": 3,
                "speak_after_ms": 700,
            }
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-luna")

    utterance = agent.compose(
        step=replace(_step(kind="session_zero_message"), payload={"natural_broadcast": True}),
        legal_context=_context(conflict_active=False),
        recent_public_context="时悠：大家想先聊哪一块？",
        natural_table_event={
            "event_id": 3,
            "speaker": "时悠",
            "role": "gm",
            "text": "大家想先聊哪一块？",
            "action_bar": {"phase": "session_zero"},
        },
    )

    assert utterance.used_fallback is False
    assert utterance.audience == "player"
    assert utterance.text.startswith("白河")


def test_natural_reply_to_stale_event_is_repaired() -> None:
    client = ScriptedClient(
        [
            {
                **_answer("我同意这个方向。", audience="table"),
                "kind": "table_discussion",
                "reply_to_event_id": 2,
            },
            {
                **_answer("我同意刚才这个方向。", audience="table"),
                "kind": "table_discussion",
                "reply_to_event_id": 4,
            },
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-luna")

    utterance = agent.compose(
        step=replace(_step(), payload={"natural_broadcast": True}),
        legal_context=_context(conflict_active=False),
        natural_table_event={
            "event_id": 4,
            "speaker": "白河",
            "role": "player",
            "text": "我觉得中央放一片内海。",
            "action_bar": {"phase": "session_zero"},
        },
    )

    assert utterance.reply_to_event_id == 4
    assert len(client.calls) == 2
    assert "旧草稿不能直接补发" in client.calls[1]["messages"][1].content


def test_natural_player_repairs_own_near_verbatim_repeat_into_wait() -> None:
    repeated = "我想到一个钟鸣公国，正午大钟能安抚灵魂，也让贵族控制谁的哀悼能被听见。"
    client = ScriptedClient(
        [
            {
                **_answer(repeated, audience="table"),
                "kind": "table_discussion",
                "reply_to_event_id": 1,
                "speak_after_ms": 2500,
            },
            {
                **_answer(repeated, audience="table"),
                "kind": "table_discussion",
                "reply_to_event_id": 2,
                "speak_after_ms": 2500,
            },
            {
                **_answer("", decision="wait", audience="table"),
                "kind": "wait",
                "reply_to_event_id": None,
                "speak_after_ms": 0,
            },
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-luna")
    step = replace(_step(kind="session_zero_message"), payload={"natural_broadcast": True})

    first = agent.compose(
        step=step,
        legal_context=_context(conflict_active=False),
        natural_table_event={
            "event_id": 1,
            "speaker": "时悠",
            "role": "gm",
            "text": "大家想先提哪个国家？",
            "action_bar": {"phase": "session_zero"},
        },
    )
    second = agent.compose(
        step=step,
        legal_context=_context(conflict_active=False),
        natural_table_event={
            "event_id": 2,
            "speaker": "南星",
            "role": "player",
            "text": "这个方向我赞成。",
            "action_bar": {"phase": "session_zero"},
        },
    )

    assert first.decision == "speak"
    assert second.decision == "wait"
    assert len(client.calls) == 3
    assert "近似重复" in client.calls[2]["messages"][1].content


def test_luna_player_agent_uses_independent_model_and_public_perspective() -> None:
    client = ScriptedClient([_answer("伊莉雅贴近铁栏，想看看符文在哪一处最不稳定。")])
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-luna")

    utterance = agent.compose(
        step=_step(stage_goal="隐藏测试答案：守卫其实会放人"),
        legal_context=_context(),
        last_gm_reply="雨水正从牢门上方滴落。",
        recent_public_context="时悠：牢门上的蓝白符文正在明灭。",
    )

    assert utterance.used_fallback is False
    assert utterance.text == "伊莉雅贴近铁栏，想看看符文在哪一处最不稳定。"
    assert client.calls[0]["model"] == "gpt-5.6-luna"
    messages = client.calls[0]["messages"]
    assert messages[0].cache_family == "fu-pl-v2"
    assert messages[0].cache_breakpoint is True
    assert len(messages[1].cache_breakpoint_offsets) == 2
    assert "隐藏测试答案" not in messages[1].content
    assert "测试目标不可见" not in messages[1].content
    assert "雨水正从牢门上方滴落" in messages[1].content


def test_zero_hp_decision_defaults_to_survival_without_exit_intent() -> None:
    instruction = LunaPlayerAgent._mode_instruction(
        "decision",
        _context(
            pending_decisions=[
                {
                    "kind": "zero_hp",
                    "owner": "伊莉雅",
                    "resolution_options": ["sacrifice", "surrender"],
                }
            ]
        ),
    )

    assert "默认选择放弃抵抗" in instruction
    assert "不确定就活下来" in instruction
    assert "永久退场" in instruction


def test_check_confirmation_uses_natural_player_language_without_internal_binding() -> None:
    window_id = "check-window-1"
    client = ScriptedClient(
        [
            _decision_answer(
                text="投，我要割断牵绳。",
            ),
            _pending_review(option="投骰", evidence="投"),
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-sol")
    context = _context(
        pending_decisions=[
            {
                "window_id": window_id,
                "kind": "check_roll_confirmation",
                "owner": "伊莉雅",
                "options": [
                    {"choice": "roll", "label": "投骰"},
                    {"choice": "cancel", "label": "取消这次检定"},
                    {"choice": "revise", "label": "改换做法"},
                ],
            }
        ]
    )

    utterance = agent.compose(
        step=replace(_step(), message="投。"),
        legal_context=context,
        recent_public_context="时悠：需要进行检定，难度等级10。要投吗？",
    )

    assert utterance.used_fallback is False
    assert utterance.text == "投，我要割断牵绳。"
    assert utterance.action_commitment == "answer"
    assert utterance.model_attempts[0]["decision_selections"] == {}
    prompt = client.calls[0]["messages"][1].content
    assert window_id not in prompt
    assert "window_response" not in prompt
    assert "投骰" in prompt
    assert "只在顶层text中写一条真人会发送的回答" in prompt


def test_empty_decision_retries_but_natural_reply_does_not_require_machine_choice() -> None:
    window_id = "check-window-2"
    client = ScriptedClient(
        [
            {
                **_answer("", decision="wait", audience="table"),
                "kind": "wait",
            },
            _decision_answer(text="投，我试着割断牵绳。"),
            _pending_review(option="投骰", evidence="投"),
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-sol")
    context = _context(
        pending_decisions=[
            {
                "window_id": window_id,
                "kind": "check_roll_confirmation",
                "owner": "伊莉雅",
                "options": [
                    {"choice": "roll", "label": "投骰"},
                    {"choice": "cancel", "label": "取消这次检定"},
                    {"choice": "revise", "label": "改换做法"},
                ],
            }
        ]
    )

    utterance = agent.compose(
        step=replace(_step(), message="投。"),
        legal_context=context,
        recent_public_context="时悠：需要进行检定，难度等级10。要投吗？",
    )

    assert utterance.used_fallback is False
    assert utterance.text == "投，我试着割断牵绳。"
    assert len(client.calls) == 3
    assert any(
        "自然语言回答" in error
        for error in utterance.model_attempts[0]["validation_errors"]
    )


def test_pending_opportunity_rejects_new_scene_action_and_repairs_choice() -> None:
    client = ScriptedClient(
        [
            _decision_answer(text="我把碎片按进锁孔，试试能不能转动它。"),
            _pending_review(
                option="",
                evidence="把碎片按进锁孔",
                valid=False,
                starts_action=True,
            ),
            _decision_answer(text="这次机会我选【情报】。"),
            _pending_review(option="情报", evidence="选【情报】"),
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-sol")
    context = _context(
        pending_decisions=[
            {
                "window_id": "critical-window-1",
                "kind": "critical_opportunity",
                "owner": "伊莉雅",
                "prompt": "这次大成功带来一个机会，你想要怎么使用它？",
                "options": [
                    {"effect": "情报", "summary": "发现一条有用线索或情报。"},
                    {"effect": "优势", "summary": "下一次检定获得+4。"},
                    {"effect": "decline", "summary": "放弃本次机会。"},
                ],
            }
        ]
    )

    utterance = agent.compose(
        step=replace(_step(), message="这次机会我选【情报】。"),
        legal_context=context,
        recent_public_context=(
            "时悠：检定大成功。碎片边缘与门锁凹槽吻合。"
            "这次大成功带来一个机会，你想要怎么使用它？"
        ),
    )

    assert utterance.used_fallback is False
    assert utterance.text == "这次机会我选【情报】。"
    assert utterance.action_commitment == "answer"
    assert len(client.calls) == 4
    assert any(
        error.startswith("pending_decision_adds_new_action")
        for error in utterance.model_attempts[0]["validation_errors"]
    )


def test_reveal_opportunity_rejects_object_target_and_repairs_to_creature() -> None:
    client = ScriptedClient(
        [
            _decision_answer(text="我把【揭示】用于【报告】。"),
            _pending_review(
                option="揭示",
                evidence="【揭示】用于【报告】",
                selected_parameters={"target": "报告"},
            ),
            _decision_answer(text="我把【揭示】用于【守卫维蕾娅】，想知道她的目标。"),
            _pending_review(
                option="揭示",
                evidence="【揭示】用于【守卫维蕾娅】",
                selected_parameters={"target": "守卫维蕾娅"},
            ),
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-sol")
    context = _context(
        pending_decisions=[
            {
                "window_id": "critical-window-reveal",
                "kind": "critical_opportunity",
                "owner": "伊莉雅",
                "prompt": "这次大成功带来一个机会，你想要怎么使用它？",
                "options": [
                    {"effect": "揭示", "requires": ["target"]},
                    {"effect": "优势", "requires": ["target"]},
                ],
            }
        ]
    )

    utterance = agent.compose(
        step=replace(_step(), message="我选择揭示。"),
        legal_context=context,
        recent_public_context=(
            "时悠：检定大成功。桌上摊着报告，守卫维蕾娅站在门边。"
            "这次大成功带来一个机会，你想要怎么使用它？"
        ),
    )

    assert utterance.used_fallback is False
    assert utterance.text == "我把【揭示】用于【守卫维蕾娅】，想知道她的目标。"
    assert len(client.calls) == 4
    assert any(
        error.startswith("does_not_answer_pending_decision")
        for error in utterance.model_attempts[0]["validation_errors"]
    )
    assert agent.last_pending_decision_review["structural_parameter_errors"] == []


def test_repair_reuses_persona_and_full_perspective_cache_prefixes() -> None:
    client = ScriptedClient(
        [
            _answer("赛璃立刻接过钥匙，替伊莉雅打开牢门。"),
            _answer("伊莉雅把钥匙举给赛璃看：你要不要试试这把？"),
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-luna")

    utterance = agent.compose(
        step=_step(),
        legal_context=_context(story_items=[{"name": "黄铜钥匙", "holder": "伊莉雅"}]),
        last_gm_reply="赛璃在相邻牢房里抬起头。",
        recent_public_context="伊莉雅刚刚从墙缝里找到一把黄铜钥匙。",
    )

    assert utterance.used_fallback is False
    assert len(client.calls) == 2
    assert "你要不要" in utterance.text
    first_user = client.calls[0]["messages"][1]
    second_user = client.calls[1]["messages"][1]
    assert first_user.cache_breakpoint_offsets == second_user.cache_breakpoint_offsets
    persona_end, perspective_end = first_user.cache_breakpoint_offsets
    assert first_user.content[:persona_end] == second_user.content[:persona_end]
    assert first_user.content[:perspective_end] == second_user.content[:perspective_end]
    assert "替其他玩家角色" in second_user.content[perspective_end:]
    assert utterance.model_attempts[0]["validation_errors"]
    assert utterance.model_attempts[1]["validation_errors"] == []


def test_story_item_custody_is_repaired_without_controlling_holder() -> None:
    client = ScriptedClient(
        [
            _answer("伊莉雅捡起艾丽妮手里的铁片，开始撬锁。"),
            _answer("伊莉雅隔着铁栏问艾丽妮：那片铁片能借我看看吗？"),
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-luna")
    context = _context(
        known_pcs=["伊莉雅", "艾丽妮"],
        present_pcs=["伊莉雅", "艾丽妮"],
        story_items=[{"name": "细长铁片", "holder": "艾丽妮"}],
    )

    utterance = agent.compose(
        step=_step(),
        legal_context=context,
        recent_public_context="艾丽妮刚刚捡起细长铁片。",
    )

    assert utterance.used_fallback is False
    assert utterance.text.endswith("那片铁片能借我看看吗？")
    assert "剧情物件" in client.calls[1]["messages"][1].content


def test_passive_spell_granting_skill_is_repaired_into_known_spell() -> None:
    client = ScriptedClient(
        [
            _answer("赛璃以【灵魂魔法】干扰机兵的行动核心。"),
            _answer("赛璃对自己施放【屏障】，先挡住机兵的下一轮横扫。"),
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-terra")

    utterance = agent.compose(
        step=_step(speaker="南星", actor="赛璃"),
        legal_context=_context(
            current_actor="赛璃",
            conflict_active=True,
            known_pcs=["赛璃", "艾薇娅"],
            present_pcs=["赛璃", "艾薇娅"],
            legal_spells=["屏障"],
            legal_spell_rules=[
                {
                    "name": "屏障",
                    "mp_cost": 5,
                    "target_label": "至多三个生物",
                    "description": "令目标获得至少12点物防。",
                }
            ],
            legal_skills=["灵魂魔法"],
            legal_skill_rules=[
                {
                    "name": "灵魂魔法",
                    "can_declare_as_action": False,
                    "description": "每级学习一个御魂使法术。",
                }
            ],
        ),
        recent_public_context="财团机兵刚刚挥臂逼退艾薇娅。",
    )

    assert utterance.used_fallback is False
    assert "施放【屏障】" in utterance.text
    assert len(client.calls) == 2
    assert "不能单独声明为一次主动行动" in client.calls[1]["messages"][1].content


def test_spell_targeting_npc_does_not_count_as_controlling_npc() -> None:
    client = ScriptedClient([_answer("赛璃对失忆旅人施放【屏障】，先护住他。")])
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-terra")

    utterance = agent.compose(
        step=_step(speaker="南星", actor="赛璃"),
        legal_context=_context(
            current_actor="赛璃",
            conflict_active=True,
            present_npcs=["失忆旅人"],
            pc_resources={"赛璃": {"hp": 35, "mp": 10}},
            legal_spells=["屏障"],
            legal_spell_rules=[
                {
                    "name": "屏障",
                    "mp_cost": 5,
                    "target_label": "至多三个生物",
                    "description": "令目标获得至少12点物防。",
                }
            ],
        ),
        recent_public_context="失忆旅人暴露在财团狙击手的枪口下。",
    )

    assert utterance.used_fallback is False
    assert utterance.validation_errors == []
    assert "失忆旅人" in utterance.text


def test_unaffordable_spell_is_repaired_before_reaching_gm() -> None:
    client = ScriptedClient(
        [
            _answer("赛璃对失忆旅人施放【屏障】，先护住他。"),
            _answer("赛璃进入防御姿态，挡在失忆旅人与狙击手之间。"),
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-terra")

    utterance = agent.compose(
        step=_step(speaker="南星", actor="赛璃"),
        legal_context=_context(
            current_actor="赛璃",
            conflict_active=True,
            present_npcs=["失忆旅人"],
            pc_resources={"赛璃": {"hp": 20, "mp": 0}},
            legal_spells=["屏障"],
            legal_spell_rules=[
                {
                    "name": "屏障",
                    "mp_cost": 5,
                    "target_label": "至多三个生物",
                    "description": "令目标获得至少12点物防。",
                }
            ],
        ),
        recent_public_context="失忆旅人暴露在财团狙击手的枪口下。",
    )

    assert utterance.used_fallback is False
    assert "防御姿态" in utterance.text
    assert len(client.calls) == 2
    assert "当前精神值0不足以施放【屏障】" in client.calls[1]["messages"][1].content


def test_spell_target_count_is_limited_by_affordable_total_cost() -> None:
    client = ScriptedClient(
        [
            _answer("赛璃对伊莉雅、洛岚和自己施放【治愈术】。"),
            _answer("赛璃只对自己施放【治愈术】。"),
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-terra")
    context = _context(
        current_actor="赛璃",
        conflict_active=True,
        known_pcs=["赛璃", "伊莉雅", "洛岚"],
        present_pcs=["赛璃", "伊莉雅", "洛岚"],
        pc_resources={"赛璃": {"hp": 25, "mp": 10}},
        legal_spells=["治愈术"],
        legal_spell_rules=[
            {
                "name": "治愈术",
                "mp_cost": 10,
                "mp_cost_per_target": True,
                "max_affordable_targets": 1,
                "target_label": "至多三个生物",
            }
        ],
    )

    utterance = agent.compose(
        step=_step(speaker="南星", actor="赛璃"),
        legal_context=context,
        recent_public_context="伊莉雅和洛岚都在赛璃身边。",
    )

    assert utterance.used_fallback is False
    assert utterance.text == "赛璃只对自己施放【治愈术】。"
    assert "至多支持【治愈术】影响1个目标" in client.calls[1]["messages"][1].content


def test_same_conflict_turn_repairs_second_npc_question_into_action() -> None:
    client = ScriptedClient(
        [
            _answer("苍祈继续问旅人：你还记得是谁说的吗？", audience="npc"),
            _answer("苍祈进入防御姿态，守在旅人与院墙枪口之间。"),
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-terra")

    utterance = agent.compose(
        step=ReplayStep(
            id="forced-action",
            kind="player_message",
            speaker="澄砚",
            actor="苍祈",
            payload={"must_consume_turn": True},
        ),
        legal_context=_context(
            current_actor="苍祈",
            conflict_active=True,
            known_pcs=["苍祈"],
            present_pcs=["苍祈"],
            legal_actions=["攻击", "防御", "调查", "推进目标命刻"],
        ),
        recent_public_context="苍祈刚问过失忆旅人一个问题，旅人已经回答。",
    )

    assert utterance.used_fallback is False
    assert "防御姿态" in utterance.text
    assert len(client.calls) == 2
    assert "必须声明一项会消耗回合的行动" in client.calls[1]["messages"][1].content


def test_action_slot_repairs_player_only_question_and_vague_preparation() -> None:
    client = ScriptedClient(
        [
            _answer(
                "伊莉雅，你离得近，能看出旅人有什么反应吗？我先准备着。",
                audience="player",
            ),
            _answer(
                "赛璃走到旅人另一侧，放低声音问他现在能不能听清我们说话。",
                audience="npc",
            ),
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-terra")

    utterance = agent.compose(
        step=ReplayStep(
            id="required-action",
            kind="player_message",
            speaker="南星",
            actor="赛璃",
            payload={"must_submit_action_slot": True},
        ),
        legal_context=_context(
            known_pcs=["伊莉雅", "赛璃"],
            present_pcs=["伊莉雅", "赛璃"],
            present_npcs=["失忆旅人"],
        ),
        recent_public_context="失忆旅人刚刚说出了名字‘白铃’。",
    )

    assert utterance.used_fallback is False
    assert utterance.text.startswith("赛璃走到旅人另一侧")
    assert len(client.calls) == 2
    assert "不能只向另一名玩家提问" in client.calls[1]["messages"][1].content


def test_free_discussion_can_wait_without_forcing_a_player_line() -> None:
    client = ScriptedClient([_answer("", decision="wait")])
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-luna")

    utterance = agent.compose(
        step=_step(stage_goal="正在和其他玩家短暂商量下一步"),
        legal_context=_context(),
        recent_public_context="南星：谁方便盯着门外？",
    )

    assert utterance.text == ""
    assert utterance.used_fallback is False
    assert agent.last_table_discussion_review["pure_table_discussion"] is True


def test_natural_player_waits_when_another_player_already_acknowledged_for_party() -> None:
    client = ScriptedClient(
        [
            {
                **_answer(
                    "维拉，多谢提醒。我们会尽快处理金属板，不会让你久等。",
                    audience="npc",
                ),
                "kind": "in_character",
                "action_commitment": "none",
                "reply_to_event_id": 31,
                "speak_after_ms": 900,
            },
            {
                "pure_table_discussion": False,
                "commits_character_action": False,
                "commits_party_action": False,
                "directed_at_gm_or_npc": True,
                "requests_authoritative_world_answer": False,
                "answerable_by_players_from_public_context": True,
                "recommended_audience": "npc",
                "adds_new_substantive_content": False,
                "merely_repeats_prior_agreement": True,
                "exchange_already_answered_for_group": True,
                "acknowledgement_still_needed": False,
                "settled_immediate_plan": "记录金属板与震动的对应顺序",
                "candidate_begins_settled_plan": False,
                "recommended_disposition": "wait",
                "evidence": "我们会尽快处理金属板",
                "reason": "队友已经代表全队确认，NPC也已接受，这句只是重复保证。",
            },
            {
                **_answer("", decision="wait", audience="table"),
                "kind": "wait",
                "action_commitment": "none",
                "reply_to_event_id": 31,
                "speak_after_ms": 0,
            },
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-luna")
    recent = (
        "时悠：仪式提前了，城门今晚封锁。\n"
        "阿凛：我们马上确认金属板的顺序，然后离开。\n"
        "时悠：好，你们动作快些，我只能拖住财团一会儿。"
    )

    utterance = agent.compose(
        step=replace(_step(speaker="南星", actor="赛璃"), payload={"natural_broadcast": True}),
        legal_context=_context(conflict_active=False),
        recent_public_context=recent,
        natural_table_event={
            "event_id": 31,
            "speaker": "时悠",
            "role": "gm",
            "text": "好，你们动作快些，我只能拖住财团一会儿。",
            "action_bar": {"phase": "adventure", "you_are_current_actor": True},
        },
    )

    assert utterance.decision == "wait"
    assert utterance.text == ""
    assert len(client.calls) == 3
    assert utterance.model_attempts[0]["table_discussion_review"][
        "merely_repeats_prior_agreement"
    ] is True
    assert any(
        error.startswith("redundant_social_echo_after_consensus:")
        for error in utterance.model_attempts[0]["validation_errors"]
    )


def test_natural_player_repairs_redundant_acknowledgement_into_settled_action() -> None:
    action_text = "赛璃蹲到金属板前，开始记录一整轮震动强弱与符号位置的对应顺序。"
    client = ScriptedClient(
        [
            {
                **_answer(
                    "维拉，我们也会尽快，不会让你久等。",
                    audience="npc",
                ),
                "kind": "in_character",
                "action_commitment": "none",
                "reply_to_event_id": 32,
            },
            {
                "pure_table_discussion": False,
                "commits_character_action": False,
                "commits_party_action": False,
                "directed_at_gm_or_npc": True,
                "requests_authoritative_world_answer": False,
                "answerable_by_players_from_public_context": True,
                "recommended_audience": "npc",
                "adds_new_substantive_content": False,
                "merely_repeats_prior_agreement": True,
                "exchange_already_answered_for_group": True,
                "acknowledgement_still_needed": False,
                "settled_immediate_plan": "记录金属板与震动的对应顺序",
                "candidate_begins_settled_plan": False,
                "recommended_disposition": "act",
                "evidence": "我们也会尽快",
                "reason": "已有队友回应，当前应开始执行已经商定的观察。",
            },
            {
                **_answer(action_text, audience="gm"),
                "kind": "action",
                "action_commitment": "committed",
                "reply_to_event_id": 32,
            },
            _action_progress_review(evidence="开始记录一整轮震动强弱", valid=True),
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-luna")

    utterance = agent.compose(
        step=replace(_step(speaker="南星", actor="赛璃"), payload={"natural_broadcast": True}),
        legal_context=_context(conflict_active=False),
        recent_public_context=(
            "阿凛：我们马上确认金属板的顺序，然后离开。\n"
            "时悠：好，你们动作快些，我只能拖住财团一会儿。"
        ),
        natural_table_event={
            "event_id": 32,
            "speaker": "时悠",
            "role": "gm",
            "text": "好，你们动作快些，我只能拖住财团一会儿。",
            "action_bar": {"phase": "adventure", "you_are_current_actor": True},
        },
    )

    assert utterance.used_fallback is False
    assert utterance.utterance_kind == "action"
    assert utterance.action_commitment == "committed"
    assert utterance.text == action_text
    assert len(client.calls) == 4
    assert utterance.model_attempts[1]["action_progress_review"][
        "valid_action_progress"
    ] is True


def test_free_discussion_repairs_a_direct_npc_address_into_table_talk() -> None:
    client = ScriptedClient(
        [
            _answer(
                "会长，请先告诉我们驿卒能躲去哪里？",
                audience="npc",
            ),
            _answer(
                "我倾向先安置驿卒；你们觉得谁方便去问会长？",
                audience="table",
            ),
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-luna")

    utterance = agent.compose(
        step=_step(stage_goal="你正在和其他玩家短暂商量下一步"),
        legal_context=_context(),
        recent_public_context="会长仍在等队伍回答由谁护送旅人。",
    )

    assert utterance.used_fallback is False
    assert utterance.text == "我倾向先安置驿卒；你们觉得谁方便去问会长？"
    assert len(client.calls) == 2
    assert "自由讨论只能面向其他玩家或全桌" in client.calls[1]["messages"][1].content


def test_free_discussion_repairs_ambiguous_first_person_action() -> None:
    client = ScriptedClient(
        [
            _answer(
                "我想先弄清楚风铃有没有魔法迹象；外面谁方便盯着？",
                audience="table",
            ),
            _answer(
                "这串风铃很可疑。你们觉得等下由谁来查，谁留意门外？",
                audience="table",
            ),
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-luna")

    utterance = agent.compose(
        step=_step(
            speaker="澄砚",
            actor="苍祈",
            stage_goal="正在和其他玩家短暂商量下一步",
        ),
        legal_context=_context(),
        recent_public_context="风铃的回声比周围慢半拍。",
    )

    assert utterance.used_fallback is False
    assert utterance.text.startswith("这串风铃很可疑")
    assert len(client.calls) == 2
    assert "自由讨论用了会被理解为立即行动" in client.calls[1]["messages"][1].content


def test_action_repairs_player_name_into_hero_name() -> None:
    client = ScriptedClient(
        [
            _answer("洛岚跟上澄砚往旧路闸门去。"),
            _answer("洛岚跟上苍祈往旧路闸门去。"),
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-luna")

    utterance = agent.compose(
        step=_step(speaker="白河", actor="洛岚"),
        legal_context=_context(
            known_pcs=["洛岚", "苍祈"],
            present_pcs=["洛岚", "苍祈"],
        ),
        recent_public_context="苍祈刚往旧路闸门方向走。",
    )

    assert utterance.used_fallback is False
    assert utterance.text == "洛岚跟上苍祈往旧路闸门去。"
    assert len(client.calls) == 2
    assert "桌外玩家名" in client.calls[1]["messages"][1].content


def test_out_of_turn_action_is_repaired_into_table_advice() -> None:
    client = ScriptedClient(
        [
            _answer("伊莉雅马上冲过去攻击守卫。"),
            _answer(
                "赛璃，守卫右手离警铃很近，你先留意那边。",
                audience="player",
            ),
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-luna")

    utterance = agent.compose(
        step=_step(),
        legal_context=_context(conflict_active=True, current_actor="赛璃"),
        recent_public_context="时悠：现在轮到赛璃。",
    )

    assert utterance.used_fallback is False
    assert "你先留意" in utterance.text
    assert "不能替伊莉雅抢先行动" in client.calls[1]["messages"][1].content


def test_session_zero_focus_repairs_targeted_discussion_into_direct_answer() -> None:
    client = ScriptedClient(
        [
            {
                **_answer(
                    "这个静默区的想法很有意思，也许它还会挑选接住记忆的人。",
                    audience="table",
                ),
                "kind": "table_discussion",
                "session_zero_response": "discussion",
            },
            {
                **_answer(
                    "我的世界威胁是：静默区会把被选中的探险者留下，变成新的记忆看守者。",
                    audience="table",
                ),
                "kind": "table_discussion",
                "session_zero_response": "contribute",
            },
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-luna")
    step = replace(
        _step(speaker="南星", actor="赛璃"),
        payload={"natural_broadcast": True},
    )

    utterance = agent.compose(
        step=step,
        legal_context=_context(),
        recent_public_context="时悠刚问南星还想贡献哪一种世界性威胁。",
        natural_table_event={
            "event_id": 12,
            "speaker": "时悠",
            "role": "gm",
            "text": "南星，你还想贡献哪一种世界性威胁？",
            "action_bar": {
                "phase": "session_zero",
                "session_zero_focus": {
                    "player": "南星",
                    "topic": "世界性威胁",
                },
            },
        },
    )

    assert utterance.used_fallback is False
    assert utterance.text.startswith("我的世界威胁是")
    assert len(client.calls) == 2
    assert "不要继续把问题扩写成讨论分支" in client.calls[1]["messages"][1].content


def test_session_zero_targeted_invalid_replies_fall_back_to_explicit_skip() -> None:
    repeated = "我确实没有新的王国想法了，之前已经补过无声钟。"
    client = ScriptedClient(
        [
            {
                **_answer(repeated, audience="table"),
                "kind": "table_discussion",
                "session_zero_response": "contribute",
            },
            {
                **_answer("", decision="wait", audience="table"),
                "kind": "wait",
                "session_zero_response": "none",
            },
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-luna")
    agent.record_delivered("白河", repeated)
    step = replace(
        _step(speaker="白河", actor="洛岚"),
        payload={"natural_broadcast": True},
    )

    utterance = agent.compose(
        step=step,
        legal_context=_context(),
        recent_public_context="时悠再次问白河是否还要贡献国家。",
        natural_table_event={
            "event_id": 49,
            "speaker": "时悠",
            "role": "gm",
            "text": "白河，你还想补一个国家吗？",
            "action_bar": {
                "phase": "session_zero",
                "session_zero_focus": {
                    "player": "白河",
                    "topic": "kingdom_contributions",
                    "topic_key": "kingdom",
                },
            },
        },
    )

    assert utterance.used_fallback is True
    assert utterance.decision == "speak"
    assert utterance.audience == "table"
    assert utterance.utterance_kind == "table_discussion"
    assert utterance.text == "这一项我先跳过。"
    assert "不能wait" in client.calls[1]["messages"][1].content
    assert "只要当前字段仍显示缺失，就不要wait" in client.calls[1]["messages"][0].content


def test_unavailable_luna_has_explicit_reported_fallback() -> None:
    agent = LunaPlayerAgent(use_llm=False, model="gpt-5.6-luna")

    utterance = agent.compose(
        step=_step(),
        legal_context=_context(),
    )

    assert utterance.used_fallback is True
    assert utterance.fallback_kind == "luna_v2_unavailable"
    assert utterance.validation_errors == ["luna_player_unavailable"]


def test_natural_player_semantically_repairs_action_mislabeled_as_table_chat() -> None:
    client = ScriptedClient(
        [
            {
                **_answer(
                    "碎片上的符号和锁孔周围的符号可能对得上，我试试把碎片凑到锁孔附近看看能不能对上。",
                    audience="player",
                ),
                "kind": "table_discussion",
                "action_commitment": "tentative",
                "reply_to_event_id": 18,
            },
            {
                "pure_table_discussion": False,
                "commits_character_action": True,
                "commits_party_action": False,
                "directed_at_gm_or_npc": False,
                "evidence": "我试试把碎片凑到锁孔附近看看",
                "reason": "这句话已经让角色开始把碎片凑向锁孔。",
            },
            {
                **_answer(
                    "碎片和锁孔的符号可能对得上。你们觉得先比一比，还是先问守卫？",
                    audience="table",
                ),
                "kind": "table_discussion",
                "action_commitment": "tentative",
                "reply_to_event_id": 18,
            },
            {
                "pure_table_discussion": True,
                "commits_character_action": False,
                "commits_party_action": False,
                "directed_at_gm_or_npc": False,
                "evidence": "你们觉得先比一比，还是先问守卫",
                "reason": "只是在向队友比较两个方案。",
            },
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-luna")

    utterance = agent.compose(
        step=replace(_step(), payload={"natural_broadcast": True}),
        legal_context=_context(conflict_active=False),
        recent_public_context="时悠：碎片上的符号被余温遮得看不清。",
        natural_table_event={
            "event_id": 18,
            "speaker": "时悠",
            "role": "gm",
            "text": "碎片上的符号被余温遮得看不清。",
            "action_bar": {"you_are_current_actor": True},
        },
    )

    assert len(client.calls) == 4
    assert utterance.utterance_kind == "table_discussion"
    assert utterance.action_commitment == "tentative"
    assert "table_discussion_declares_character_action" in (
        client.calls[2]["messages"][1].content
    )
    assert utterance.model_attempts[0]["table_discussion_review"][
        "commits_character_action"
    ] is True


def test_natural_player_repairs_hidden_world_question_mislabeled_as_table_chat() -> None:
    client = ScriptedClient(
        [
            {
                **_answer(
                    "财团派人过来大概要多久？如果还有时间，我们要不要先看看门后？",
                    audience="table",
                ),
                "kind": "table_discussion",
                "action_commitment": "tentative",
                "reply_to_event_id": 24,
            },
            {
                "pure_table_discussion": False,
                "commits_character_action": False,
                "commits_party_action": False,
                "directed_at_gm_or_npc": False,
                "requests_authoritative_world_answer": True,
                "answerable_by_players_from_public_context": False,
                "recommended_audience": "gm",
                "evidence": "财团派人过来大概要多久",
                "reason": "抵达时间是尚未公开、只能由主持人确认的世界事实。",
            },
            {
                **_answer(
                    "维拉只说时间不多。我们要不要趁现在看看门后，还是先带走碎片？",
                    audience="table",
                ),
                "kind": "table_discussion",
                "action_commitment": "tentative",
                "reply_to_event_id": 24,
            },
            {
                "pure_table_discussion": True,
                "commits_character_action": False,
                "commits_party_action": False,
                "directed_at_gm_or_npc": False,
                "requests_authoritative_world_answer": False,
                "answerable_by_players_from_public_context": True,
                "recommended_audience": "table",
                "evidence": "我们要不要趁现在看看门后，还是先带走碎片",
                "reason": "只是在依据NPC已公开的时间压力和队友商量取舍。",
            },
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-luna")

    utterance = agent.compose(
        step=replace(_step(), payload={"natural_broadcast": True}),
        legal_context=_context(conflict_active=False),
        recent_public_context="维拉：财团不会坐视不管。你们还有一点时间，但不会太久。",
        natural_table_event={
            "event_id": 24,
            "speaker": "维拉·铜须",
            "role": "gm",
            "text": "财团不会坐视不管。你们还有一点时间，但不会太久。",
            "action_bar": {"you_are_current_actor": True},
        },
    )

    assert len(client.calls) == 4
    assert utterance.audience == "table"
    assert utterance.action_commitment == "tentative"
    assert utterance.text.startswith("维拉只说时间不多")
    assert "table_discussion_audience_mismatch" in (
        client.calls[2]["messages"][1].content
    )
    assert utterance.model_attempts[0]["table_discussion_review"][
        "requests_authoritative_world_answer"
    ] is True


def test_natural_player_repairs_derivative_question_into_open_npc_response() -> None:
    client = ScriptedClient(
        [
            {
                **_answer(
                    "维拉，你拿到碎片以后具体先看哪一道刻痕？",
                    audience="npc",
                ),
                "kind": "in_character",
                "action_commitment": "none",
                "reply_to_event_id": 31,
            },
            _open_npc_review(
                evidence="具体先看哪一道刻痕",
                valid=False,
                derivative=True,
            ),
            {
                **_answer(
                    "碎片可以给你，但你得先答应不把它交给财团。",
                    audience="npc",
                ),
                "kind": "in_character",
                "action_commitment": "none",
                "reply_to_event_id": 31,
            },
            _open_npc_review(
                evidence="碎片可以给你，但你得先答应不把它交给财团",
                valid=True,
            ),
        ]
    )
    agent = LunaPlayerAgent(
        client=client,
        model="gpt-5.6-luna",
        personas={
            "白河": PlayerPersona("白河", "洛岚", "务实", "直接"),
        },
    )

    utterance = agent.compose(
        step=replace(
            _step(speaker="白河", actor="洛岚"),
            payload={"natural_broadcast": True},
        ),
        legal_context=_context(
            known_pcs=["洛岚"],
            present_pcs=["洛岚"],
            present_npcs=["维拉·铜须"],
        ),
        recent_public_context=(
            "维拉·铜须：把碎片交给我。财团来之前，我还能保住门里的一部分记忆。"
        ),
        natural_table_event={
            "event_id": 31,
            "speaker": "维拉·铜须",
            "role": "gm",
            "text": "把碎片交给我。",
            "action_bar": {
                "phase": "adventure",
                "you_are_current_actor": True,
                "open_npc_request": {
                    "npc": "维拉·铜须",
                    "addressed_actor": "洛岚",
                    "response_scope": "actor_only",
                    "summary": "交出碎片",
                    "remaining_items": [
                        {"item_id": "fragment", "prompt": "是否交出碎片"}
                    ],
                },
            },
        },
    )

    assert utterance.text.startswith("碎片可以给你")
    assert len(client.calls) == 4
    assert "does_not_handle_open_npc_request" in (
        client.calls[2]["messages"][1].content
    )
    assert utterance.model_attempts[0]["open_npc_request_review"][
        "only_asks_derivative_question"
    ] is True


def test_natural_player_named_by_open_npc_request_cannot_silently_skip_it() -> None:
    client = ScriptedClient(
        [
            {
                **_answer("", decision="wait", audience="table"),
                "kind": "wait",
                "action_commitment": "none",
            },
            {
                **_answer("我不知道它的来路，也不会编一个答案。", audience="npc"),
                "kind": "in_character",
                "action_commitment": "none",
            },
            _open_npc_review(
                evidence="我不知道它的来路",
                valid=True,
            ),
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-luna")

    utterance = agent.compose(
        step=replace(_step(), payload={"natural_broadcast": True}),
        legal_context=_context(),
        recent_public_context="守卫维蕾娅：这枚碎片从哪里来的？",
        natural_table_event={
            "event_id": 32,
            "speaker": "守卫维蕾娅",
            "role": "gm",
            "text": "这枚碎片从哪里来的？",
            "action_bar": {
                "phase": "adventure",
                "open_npc_request": {
                    "npc": "守卫维蕾娅",
                    "addressed_actor": "伊莉雅",
                    "response_scope": "actor_only",
                    "summary": "说明碎片来路",
                    "remaining_items": [
                        {"item_id": "origin", "prompt": "碎片从哪里来"}
                    ],
                },
            },
        },
    )

    assert utterance.text == "我不知道它的来路，也不会编一个答案。"
    assert len(client.calls) == 3
    assert "open_npc_request_requires_response" in (
        client.calls[1]["messages"][1].content
    )


def test_natural_committed_action_repairs_repeated_micro_investigation() -> None:
    client = ScriptedClient(
        [
            {
                **_answer("洛岚再检查一遍金属门第三道细缝里的粉末。"),
                "kind": "action",
                "action_commitment": "committed",
                "reply_to_event_id": 41,
            },
            _action_progress_review(
                evidence="再检查一遍金属门第三道细缝",
                valid=False,
                micro_lane=True,
            ),
            {
                **_answer(
                    "洛岚不再折腾金属门，转向赫恩说：‘带我去旧采区找压力阀。’",
                    audience="npc",
                ),
                "kind": "action",
                "action_commitment": "committed",
                "reply_to_event_id": 41,
            },
            _action_progress_review(
                evidence="带我去旧采区找压力阀",
                valid=True,
            ),
        ]
    )
    agent = LunaPlayerAgent(
        client=client,
        model="gpt-5.6-luna",
        personas={
            "白河": PlayerPersona("白河", "洛岚", "务实", "直接"),
        },
    )

    utterance = agent.compose(
        step=replace(
            _step(speaker="白河", actor="洛岚"),
            payload={"natural_broadcast": True},
        ),
        legal_context=_context(
            known_pcs=["洛岚"],
            present_pcs=["洛岚"],
            present_npcs=["老矿工赫恩"],
            visible_scene_elements=["已经连续检查过的金属门", "通往旧采区的坡道"],
            established_scene_facts=["赫恩知道旧采区压力阀的位置，并愿意带路"],
        ),
        recent_public_context=(
            "洛岚已经两次检查金属门，确认门内记忆正在被抽走。\n"
            "老矿工赫恩：旧采区有一只压力阀，我可以带你们过去。"
        ),
        natural_table_event={
            "event_id": 41,
            "speaker": "老矿工赫恩",
            "role": "gm",
            "text": "旧采区有一只压力阀，我可以带你们过去。",
            "action_bar": {
                "phase": "adventure",
                "you_are_current_actor": True,
            },
        },
    )

    assert "旧采区找压力阀" in utterance.text
    assert len(client.calls) == 4
    assert "semantic_action_without_progress" in (
        client.calls[2]["messages"][1].content
    )
    assert utterance.model_attempts[0]["action_progress_review"][
        "repeats_micro_investigation_lane"
    ] is True
    assert utterance.model_attempts[1]["action_progress_review"][
        "valid_action_progress"
    ] is True


def test_natural_player_does_not_claim_another_heroes_injury_as_own() -> None:
    client = ScriptedClient(
        [
            {
                **_answer(
                    "我肩膀没事，只擦破点皮；我们继续沿轨道走。",
                    audience="table",
                ),
                "kind": "action",
                "action_commitment": "committed",
                "reply_to_event_id": 60,
            },
            _action_progress_review(
                evidence="我肩膀没事",
                valid=True,
                controls_other_players=True,
            ),
            {
                **_answer(
                    "赛璃，你肩膀还撑得住吗？我先检查前方轨道有没有安全落脚点。",
                    audience="table",
                ),
                "kind": "action",
                "action_commitment": "committed",
                "reply_to_event_id": 60,
            },
            _action_progress_review(
                evidence="检查前方轨道有没有安全落脚点",
                valid=True,
            ),
        ]
    )
    agent = LunaPlayerAgent(
        client=client,
        model="gpt-5.6-luna",
        personas={"白河": PlayerPersona("白河", "洛岚", "务实", "直接")},
    )

    utterance = agent.compose(
        step=replace(
            _step(speaker="白河", actor="洛岚"),
            payload={"natural_broadcast": True},
        ),
        legal_context=_context(
            known_pcs=["洛岚", "赛璃"],
            present_pcs=["洛岚", "赛璃"],
        ),
        recent_public_context=(
            "时悠：碎石砸中了赛璃的肩膀。\n"
            "南星：赛璃倒吸一口气，提醒大家小心头顶。"
        ),
        natural_table_event={
            "event_id": 60,
            "speaker": "南星",
            "role": "player",
            "text": "赛璃倒吸一口气，提醒大家小心头顶。",
            "action_bar": {"phase": "adventure", "you_are_current_actor": True},
        },
    )

    assert utterance.used_fallback is False
    assert "赛璃，你肩膀还撑得住吗" in utterance.text
    assert "我肩膀没事" not in utterance.text
    assert len(client.calls) == 4
    assert "semantic_action_controls_other_players" in (
        client.calls[2]["messages"][1].content
    )


def test_action_progress_review_receives_public_recent_check_attempts() -> None:
    candidate = "赛璃换个角度观察金属板上的符号排列。"
    client = ScriptedClient(
        [
            {
                **_answer(candidate),
                "kind": "action",
                "action_commitment": "committed",
                "reply_to_event_id": 61,
            },
            _action_progress_review(evidence="观察金属板", valid=True),
        ]
    )
    agent = LunaPlayerAgent(client=client, model="gpt-5.6-luna")

    utterance = agent.compose(
        step=replace(_step(speaker="南星", actor="赛璃"), payload={"natural_broadcast": True}),
        legal_context=_context(
            recent_check_attempts=[
                {
                    "attempt_id": "check-1",
                    "actor": "洛岚",
                    "target": "金属板",
                    "purpose": "观察符号排列与震动节奏的关系",
                    "difficulty": 10,
                    "total": 7,
                    "outcome": "failure",
                    "failure_authority": "attempt",
                    "material_change": False,
                    "public": True,
                }
            ]
        ),
        recent_public_context="洛岚观察金属板失败，没有得到新结果。",
        natural_table_event={
            "event_id": 61,
            "speaker": "时悠",
            "role": "gm",
            "text": "洛岚没有从金属板上看出新规律。",
            "action_bar": {"phase": "adventure", "you_are_current_actor": True},
        },
    )

    assert utterance.text == candidate
    review_request = json.loads(client.calls[1]["messages"][1].content)
    assert review_request["recent_check_attempts"][0]["target"] == "金属板"
    assert review_request["recent_check_attempts"][0]["failure_authority"] == "attempt"


def test_natural_committed_action_survives_truncated_auxiliary_review() -> None:
    candidate = "我走近金属碎片，蹲下来查看上面的符号。"
    client = ScriptedClient(
        [
            {
                **_answer(candidate),
                "kind": "action",
                "action_commitment": "committed",
                "reply_to_event_id": 51,
            },
            '{"valid_action_progress":true,"concrete_new_action":true',
        ]
    )
    agent = LunaPlayerAgent(client=client, model="deepseek-v4-flash-vision-exp")

    utterance = agent.compose(
        step=replace(_step(), payload={"natural_broadcast": True}),
        legal_context=_context(
            visible_scene_elements=["刻着陌生符号的金属碎片"],
            present_npcs=[],
        ),
        recent_public_context=(
            "时悠：城门内散落着几块刻着陌生符号的金属碎片。你们要怎么做？"
        ),
        natural_table_event={
            "event_id": 51,
            "speaker": "时悠",
            "role": "gm",
            "text": "城门内散落着几块刻着陌生符号的金属碎片。",
            "action_bar": {
                "phase": "adventure",
                "you_are_current_actor": True,
            },
        },
    )

    assert utterance.text == candidate
    assert utterance.used_fallback is False
    assert utterance.validation_errors == []
    review = utterance.model_attempts[0]["action_progress_review"]
    assert review["error"] == "ValueError"
    assert review["non_blocking"] is True
    assert review["raw_excerpt"].startswith('{"valid_action_progress"')
    assert client.calls[1]["max_tokens"] == 760
