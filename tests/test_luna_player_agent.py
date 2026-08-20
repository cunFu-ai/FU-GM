from __future__ import annotations

import json

from fu_gm.testing.luna_player_agent import LunaPlayerAgent
from fu_gm.testing.replay_models import LegalActionContext, ReplayStep


class ScriptedClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = [json.dumps(item, ensure_ascii=False) for item in responses]
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


def test_unavailable_luna_has_explicit_reported_fallback() -> None:
    agent = LunaPlayerAgent(use_llm=False, model="gpt-5.6-luna")

    utterance = agent.compose(
        step=_step(),
        legal_context=_context(),
    )

    assert utterance.used_fallback is True
    assert utterance.fallback_kind == "luna_v2_unavailable"
    assert utterance.validation_errors == ["luna_player_unavailable"]
