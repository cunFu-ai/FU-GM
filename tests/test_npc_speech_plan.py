from __future__ import annotations

import pytest

from fu_gm.components.npc_speech_plan import (
    NPCSpeechPlanValidationError,
    normalize_public_segments,
    normalize_speech_plan,
)


def test_simple_npc_reply_does_not_require_segment_tags() -> None:
    segments = normalize_public_segments(
        [{"text": "“今晚不开北门。”"}]
    )

    assert segments == [
        {
            "id": "segment_01",
            "text": "“今晚不开北门。”",
            "tags": [],
        }
    ]


def test_long_player_request_reports_exact_segment_repair() -> None:
    segments = normalize_public_segments(
        [
            {
                "text": "维拉先陈述局势。" * 30 + "你们要交出碎片吗？",
                "tags": ["player_request"],
            }
        ]
    )

    with pytest.raises(NPCSpeechPlanValidationError) as raised:
        normalize_speech_plan({}, public_segments=segments)

    assert "short answerable request" in str(raised.value)
    assert "只把NPC此刻要求玩家回答的最后一个短问题单独成段" in (
        raised.value.correction_hint
    )


def test_new_gate_segment_alias_normalizes_to_canonical_condition() -> None:
    segments = normalize_public_segments(
        [
            {
                "text": "“先把通行牌交给我。”",
                "tags": ["new_gate"],
            },
            {
                "text": "“验过以后，我会开东门。”",
                "tags": ["gate_payoff"],
            },
        ]
    )
    plan = normalize_speech_plan(
        {"speech_act": "new_gate"},
        public_segments=segments,
    )

    assert segments[0]["tags"] == ["gate_requirement"]
    assert plan["speech_act"] == "condition"
    assert plan["condition"] == "先把通行牌交给我。"
    assert plan["promised_result"] == "验过以后，我会开东门。"


def test_alias_and_canonical_tag_in_one_segment_are_deduplicated() -> None:
    segments = normalize_public_segments(
        [
            {
                "text": "“先把通行牌交给我。”",
                "tags": ["new_gate", "gate_requirement"],
            }
        ]
    )

    assert segments[0]["tags"] == ["gate_requirement"]


def test_fact_effect_kind_repeated_as_segment_tag_is_safely_removed() -> None:
    segments = normalize_public_segments(
        [
            {
                "text": "“有人说白花碑钟塔夜里会响。”",
                "tags": ["direct_answer", "claim"],
            }
        ]
    )

    assert segments[0]["tags"] == ["direct_answer"]
