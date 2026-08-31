from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from fu_gm.components.npc_voice_renderer import NPCVoiceRenderer
from fu_gm.deepseek_roleplay import INNER_OS_MARKER


class ScriptedClient:
    def __init__(self, *responses: str, thinking_enabled: bool = False) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []
        self.config = SimpleNamespace(
            response_format_enabled=True,
            thinking_enabled=thinking_enabled,
        )

    def create_chat_completion(self, **kwargs: object) -> str:
        self.calls.append(dict(kwargs))
        if not self.responses:
            raise RuntimeError("unexpected model call")
        return self.responses.pop(0)


def persona() -> SimpleNamespace:
    return SimpleNamespace(
        name="白花守望会会长",
        public_identity="白花守望会的负责人",
        role_in_story="旧路守护者",
        manner="克制而警惕",
        speech_style="短句，先回答再说明边界",
        traits=["谨慎", "负责"],
        voice_examples=["“先说清楚你们要去哪儿。门开之后，其他事情再慢慢谈。”"],
        current_mood="仍有戒心",
        current_stance="愿意听取请求",
        core_drive="保护驿站与受庇护者",
        active_goal="确认英雄是否可信",
        authority_scope="可决定旧路是否开放",
        knowledge_scope="熟悉驿站和旧路",
        refusal_move="关门并疏散平民",
        taboos=["不拿平民冒险"],
    )


def segments(*, tags: list[str] | None = None) -> list[dict[str, object]]:
    return [
        {
            "id": "answer",
            "text": "东侧旧路今晚可以通行，但只能由巡守带队。",
            "tags": list(tags if tags is not None else ["direct_answer"]),
        }
    ]


def render(renderer: NPCVoiceRenderer, *, source=None, plan=None, deadline=None):
    return renderer.render(
        persona=persona(),
        public_segments=source or segments(),
        speech_plan=plan
        or {
            "speech_act": "answer",
            "proposal_outcome": "none",
            "condition_outcome": "none",
            "commitment_outcome": "none",
        },
        current_message="伊莉雅问：东侧旧路今晚能走吗？",
        recent_context="众人站在风铃廊里。",
        scene=SimpleNamespace(name="白花碑驿站", location="风铃廊"),
        deadline=deadline,
    )


def test_voice_renderer_uses_deepseek_voice_then_semantic_audit() -> None:
    voice = ScriptedClient(
        '{"rendered_segments":[{"id":"answer","text":"会长把钥匙按在掌下。“能走，但得由我们的巡守领路。”"}]}'
    )
    auditor = ScriptedClient(
        '{"valid":true,"missing_segment_ids":[],"unsupported_claims":[],"reason":"含义完整一致"}'
    )
    renderer = NPCVoiceRenderer(
        client=voice,
        model="deepseek-v4-flash",
        audit_client=auditor,
        audit_model="gpt-5.6-terra",
        audit_mode="high_risk",
    )

    result = render(renderer)

    assert result.text == "会长把钥匙按在掌下。“能走，但得由我们的巡守领路。”"
    assert result.used_model is True
    assert result.used_fallback is False
    assert result.audit_performed is True
    assert result.audit_passed is True
    assert voice.calls[0]["operation"] == "npc_voice_render"
    assert auditor.calls[0]["operation"] == "npc_voice_grounding_audit"
    messages = voice.calls[0]["messages"]
    assert messages[0].cache_family == "npc-voice"
    assert "东侧旧路今晚可以通行" not in messages[0].content
    assert "东侧旧路今晚可以通行" in messages[1].content


def test_fact_effects_force_voice_grounding_audit_even_without_fact_tag() -> None:
    voice = ScriptedClient(
        '{"rendered_segments":[{"id":"answer","text":"“昨晚守后门的是弥纱。”"}]}'
    )
    auditor = ScriptedClient(
        '{"valid":true,"missing_segment_ids":[],"unsupported_claims":[],"reason":"一致"}'
    )
    renderer = NPCVoiceRenderer(
        client=voice,
        model="deepseek-v4-flash",
        audit_client=auditor,
        audit_model="gpt-5.6-terra",
        audit_mode="high_risk",
    )
    source = [
        {
            "id": "answer",
            "text": "昨晚负责旧路后门的是巡守弥纱。",
            "tags": [],
        }
    ]
    plan = {
        "speech_act": "answer",
        "proposal_outcome": "none",
        "condition_outcome": "none",
        "commitment_outcome": "none",
        "fact_effects": [
            {
                "kind": "objective",
                "scope": "local",
                "fact": "昨晚负责旧路后门的是巡守弥纱。",
            }
        ],
    }

    result = render(renderer, source=source, plan=plan)

    assert result.audit_performed is True
    assert result.audit_passed is True
    assert "fact_effects" in auditor.calls[0]["messages"][-1].content


def test_voice_renderer_rejects_missing_or_reordered_segments() -> None:
    source = [
        {"id": "answer", "text": "今晚可以通行。", "tags": ["direct_answer"]},
        {"id": "limit", "text": "只能由巡守带队。", "tags": ["fact"]},
    ]
    voice = ScriptedClient(
        '{"rendered_segments":[{"id":"limit","text":"只能由巡守带队。"}]}'
    )
    renderer = NPCVoiceRenderer(
        client=voice,
        model="deepseek-v4-flash",
        audit_client=ScriptedClient(),
        audit_model="gpt-5.6-terra",
    )

    result = render(renderer, source=source)

    assert result.text == "今晚可以通行。只能由巡守带队。"
    assert result.used_fallback is True
    assert "segment_ids_changed" in result.fallback_reason


def test_voice_renderer_rejects_player_agency_theft_before_audit() -> None:
    voice = ScriptedClient(
        '{"rendered_segments":[{"id":"answer","text":"你点了点头。会长说：‘能走，但只能由巡守带队。’"}]}'
    )
    auditor = ScriptedClient()
    renderer = NPCVoiceRenderer(
        client=voice,
        model="deepseek-v4-flash",
        audit_client=auditor,
        audit_model="gpt-5.6-terra",
        audit_mode="high_risk",
    )

    result = render(renderer)

    assert result.text == segments()[0]["text"]
    assert result.used_fallback is True
    assert "player_agency_violation" in result.fallback_reason
    assert auditor.calls == []


def test_voice_renderer_falls_back_when_terra_audit_rejects_added_fact() -> None:
    voice = ScriptedClient(
        '{"rendered_segments":[{"id":"answer","text":"‘能走。财团的追兵还有十分钟才到。’"}]}'
    )
    auditor = ScriptedClient(
        '{"valid":false,"missing_segment_ids":[],"unsupported_claims":["新增追兵抵达时间"],"reason":"候选增加了未授权事实"}'
    )
    renderer = NPCVoiceRenderer(
        client=voice,
        model="deepseek-v4-flash",
        audit_client=auditor,
        audit_model="gpt-5.6-terra",
        audit_mode="high_risk",
    )

    result = render(renderer)

    assert result.text == segments()[0]["text"]
    assert result.used_fallback is True
    assert result.audit_performed is True
    assert "audit_rejected" in result.fallback_reason


def test_low_risk_mannerism_can_skip_semantic_audit() -> None:
    source = [{"id": "reaction", "text": "会长沉默了一瞬。", "tags": []}]
    voice = ScriptedClient(
        '{"rendered_segments":[{"id":"reaction","text":"会长的指节在钥匙上停了一瞬。"}]}'
    )
    renderer = NPCVoiceRenderer(
        client=voice,
        model="deepseek-v4-flash",
        audit_client=None,
        audit_model="",
        audit_mode="high_risk",
    )

    result = render(renderer, source=source)

    assert result.text == "会长的指节在钥匙上停了一瞬。"
    assert result.audit_performed is False
    assert result.used_fallback is False


def test_high_risk_voice_falls_back_if_auditor_is_unavailable() -> None:
    voice = ScriptedClient(
        '{"rendered_segments":[{"id":"answer","text":"‘能走，但只能由巡守领路。’"}]}'
    )
    renderer = NPCVoiceRenderer(
        client=voice,
        model="deepseek-v4-flash",
        audit_client=None,
        audit_model="",
        audit_mode="high_risk",
    )

    result = render(renderer)

    assert result.used_fallback is True
    assert result.fallback_reason == "npc_voice_auditor_unavailable"


def test_npc_voice_default_skips_second_model_audit() -> None:
    voice = ScriptedClient(
        '{"rendered_segments":[{"id":"answer","text":"“能走。巡守带路。”"}]}'
    )
    auditor = ScriptedClient()
    renderer = NPCVoiceRenderer(
        client=voice,
        model="deepseek-v4-flash",
        audit_client=auditor,
        audit_model="gpt-5.6-terra",
    )

    result = render(renderer)

    assert result.text == "“能走。巡守带路。”"
    assert result.used_fallback is False
    assert result.audit_performed is False
    assert auditor.calls == []
    assert "简洁不等于电报体" in voice.calls[0]["messages"][0].content


def test_npc_voice_explicitly_disables_thinking_for_render_and_audit() -> None:
    voice = ScriptedClient(
        '{"rendered_segments":[{"id":"answer","text":"“今晚能走，不过得让巡守领路。”"}]}',
        thinking_enabled=True,
    )
    auditor = ScriptedClient(
        '{"valid":true,"missing_segment_ids":[],"unsupported_claims":[],"reason":"一致"}',
        thinking_enabled=True,
    )
    renderer = NPCVoiceRenderer(
        client=voice,
        model="deepseek-v4-flash",
        audit_client=auditor,
        audit_model="gpt-5.6-terra",
        audit_mode="high_risk",
        deepseek_roleplay_mode="inner_os",
    )

    result = render(renderer)

    assert result.used_fallback is False
    assert voice.calls[0]["thinking_enabled"] is False
    assert not voice.calls[0]["messages"][-1].content.endswith(INNER_OS_MARKER)
    assert voice.calls[0]["max_tokens"] == 900
    assert voice.calls[0]["max_recovery_retries"] == 1
    assert voice.calls[0]["retry_without_response_format_on_empty"] is True
    assert voice.calls[0]["deadline"] > time.monotonic()
    assert auditor.calls[0]["thinking_enabled"] is False
    assert auditor.calls[0]["max_tokens"] == 500
    assert auditor.calls[0]["max_recovery_retries"] == 1
    assert auditor.calls[0]["deadline"] > time.monotonic()


def test_npc_voice_expired_outer_deadline_falls_back_without_model_call() -> None:
    voice = ScriptedClient(
        '{"rendered_segments":[{"id":"answer","text":"不应被调用"}]}'
    )
    renderer = NPCVoiceRenderer(
        client=voice,
        model="deepseek-v4-flash",
    )

    result = render(renderer, deadline=time.monotonic() - 1)

    assert result.used_fallback is True
    assert result.text == segments()[0]["text"]
    assert "deadline_budget_exhausted" in result.fallback_reason
    assert voice.calls == []


def test_npc_voice_rejects_leaked_inner_monologue() -> None:
    voice = ScriptedClient(
        '{"rendered_segments":[{"id":"answer","text":"（心想：先骗过他们。）今晚能走，不过得让巡守领路。"}]}',
        thinking_enabled=True,
    )
    renderer = NPCVoiceRenderer(
        client=voice,
        model="deepseek-v4-flash",
        deepseek_roleplay_mode="inner_os",
    )

    result = render(renderer)

    assert result.used_fallback is True
    assert result.text == segments()[0]["text"]
    assert "backstage_text_leaked" in result.fallback_reason


def test_strict_npc_voice_propagates_model_failure_instead_of_fallback() -> None:
    renderer = NPCVoiceRenderer(
        client=ScriptedClient(),
        model="gpt-5.6-sol",
        allow_fallback=False,
    )

    with pytest.raises(RuntimeError, match="npc_voice_failed"):
        render(renderer)

    assert renderer.last_result is None


def test_strict_npc_voice_propagates_expired_outer_deadline() -> None:
    voice = ScriptedClient(
        '{"rendered_segments":[{"id":"answer","text":"不应被调用"}]}'
    )
    renderer = NPCVoiceRenderer(
        client=voice,
        model="gpt-5.6-sol",
        allow_fallback=False,
    )

    with pytest.raises(RuntimeError, match="deadline_budget_exhausted"):
        render(renderer, deadline=time.monotonic() - 1)

    assert voice.calls == []
