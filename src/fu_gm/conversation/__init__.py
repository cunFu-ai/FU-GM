"""Conversation-facing primitives for FU-GM.

This package deliberately sits outside the deterministic game-rules core.  It
tracks who said what, whether the GM should speak, and where a visible reply
must be delivered without mutating character or campaign state.
"""

from __future__ import annotations

from fu_gm.conversation.events import MessageEvent
from fu_gm.conversation.intent import plan_resolution_speech
from fu_gm.conversation.ledger import ReplyLedger
from fu_gm.conversation.presence import PresenceDecision, TablePresenceScheduler
from fu_gm.conversation.reply import ReplyEnvelope, SpeechIntent

__all__ = [
    "MessageEvent",
    "PresenceDecision",
    "ReplyEnvelope",
    "ReplyLedger",
    "SpeechIntent",
    "TablePresenceScheduler",
    "plan_resolution_speech",
]
