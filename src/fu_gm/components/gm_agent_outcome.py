from __future__ import annotations

from dataclasses import dataclass, field

from fu_gm.conversation.reply import DeliveryIntent
from fu_gm.gm_tool_contracts import GMToolReceipt


@dataclass
class GMToolAgentOutcome:
    handled: bool
    reply: str = ""
    reply_parts: list[str] = field(default_factory=list)
    receipts: list[GMToolReceipt] = field(default_factory=list)
    trace: list[dict[str, object]] = field(default_factory=list)
    error: str = ""
    target: str = "fu_gm"
    mode: str = "gm_agent_tool"
    stop_astrbot: bool = True
    reason: str = ""
    terminal_action: str = ""
    delivery: DeliveryIntent = field(default_factory=DeliveryIntent)

    @property
    def state_changed(self) -> bool:
        return any(receipt.state_changed for receipt in self.receipts)
