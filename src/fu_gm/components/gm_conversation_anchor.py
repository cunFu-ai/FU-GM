from __future__ import annotations

from typing import Any

from fu_gm.gm_tool_contracts import GMToolExecutionContext


class GMConversationAnchorBuilder:
    """Expose unresolved GM questions as non-blocking semantic context.

    An anchor is not a decision window and never changes game state by itself.
    It only preserves which earlier GM question a short natural reply may be
    answering, while the core model still decides whether the player accepted,
    declined, asked a question, or changed the subject.
    """

    @classmethod
    def build(
        cls,
        context: GMToolExecutionContext,
        state: dict[str, object],
    ) -> dict[str, object]:
        if context.metadata.get("system_gm_beat_request"):
            return {}
        if str(context.gate_status or "").strip() != "session_zero":
            return {}

        session_zero = dict(state.get("session_zero") or {})
        readiness = dict(session_zero.get("adventure_readiness") or {})
        transition = dict(session_zero.get("chapter_one_transition") or {})
        if not bool(readiness.get("ready")):
            return {}
        if str(transition.get("status") or "").strip() != "invited":
            return {}

        stored = transition.get("conversation_anchor")
        anchor = dict(stored) if isinstance(stored, dict) else {}
        anchor.setdefault("anchor_id", "session-zero:chapter-one-invitation")
        anchor.setdefault("kind", "chapter_one_invitation")
        anchor.setdefault("status", "awaiting_semantic_reply")
        anchor.setdefault(
            "question",
            "时悠已经询问全桌是否现在进入第一章并开始首场。",
        )
        anchor.setdefault("blocking", False)
        anchor.setdefault("player_visible", False)
        anchor.setdefault("accepted_action", "start_adventure")
        anchor.setdefault(
            "interpretation",
            (
                "结合最近公开聊天判断当前消息是否在回答这个问题；"
                "短答和省略句不必独立重述问题。接受则调用accepted_action，"
                "暂缓或继续补充则保持第零章，无关消息按原意处理。"
            ),
        )
        return cls._safe_anchor(anchor)

    @staticmethod
    def _safe_anchor(anchor: dict[str, Any]) -> dict[str, object]:
        scalar_fields = (
            "anchor_id",
            "kind",
            "status",
            "question",
            "accepted_action",
            "interpretation",
        )
        result: dict[str, object] = {
            key: str(anchor.get(key) or "")[:800]
            for key in scalar_fields
            if str(anchor.get(key) or "").strip()
        }
        result["blocking"] = bool(anchor.get("blocking", False))
        result["player_visible"] = bool(anchor.get("player_visible", False))
        return result


__all__ = ["GMConversationAnchorBuilder"]
