from __future__ import annotations

from fu_gm.components.npc_speech_plan import (
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
