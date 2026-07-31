from __future__ import annotations

from dataclasses import dataclass, field

from fu_gm.gm_tool_contracts import GMToolReceipt


@dataclass
class GMToolAgentOutcome:
    handled: bool
    reply: str = ""
    receipts: list[GMToolReceipt] = field(default_factory=list)
    trace: list[dict[str, object]] = field(default_factory=list)
    error: str = ""
    target: str = "fu_gm"
    mode: str = "gm_agent_tool"
    stop_astrbot: bool = True
    reason: str = ""
    terminal_action: str = ""

    @property
    def state_changed(self) -> bool:
        return any(receipt.state_changed for receipt in self.receipts)
