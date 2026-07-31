from __future__ import annotations

from fu_gm.gm_evidence import (
    is_current_message_evidence,
    normalize_literal_evidence,
)
from fu_gm.gm_tool_contracts import GMToolExecutionContext


def _context(message: str) -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="evidence-test",
        session_id="s1",
        channel_id="group-1",
        speaker="玩家",
        gate_status="adventure",
        metadata={"current_message": message},
    )


def test_literal_evidence_ignores_transport_whitespace_only() -> None:
    context = _context("我们先离开驿站，\n再沿东侧堤脊前进。")

    assert is_current_message_evidence(
        context,
        "我们先离开驿站， 再沿东侧堤脊前进。",
    )
    assert normalize_literal_evidence("甲\r\n\t乙") == "甲 乙"


def test_literal_evidence_still_rejects_paraphrase_and_changed_tense() -> None:
    context = _context("我示意巡守接过牌子。")

    assert not is_current_message_evidence(context, "巡守已经接过牌子。")
    assert not is_current_message_evidence(context, "我把牌子交给巡守。")
    assert not is_current_message_evidence(context, "")
