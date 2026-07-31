from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class SessionGateState:
    """某个聊天会话是否由 FU-GM 接管。"""

    campaign_id: str
    session_id: str
    channel_id: str = ""
    status: str = "inactive"
    reason: str = ""
    started_at: str = ""
    updated_at: str = ""

    @property
    def active(self) -> bool:
        return self.status in {"pre_session", "session_zero", "adventure"}

    @property
    def paused(self) -> bool:
        return self.status == "paused"


@dataclass
class SessionGateSignal:
    kind: str
    status: str = ""
    reason: str = ""


class SessionGateManager:
    """跑团会话门控。

    平时群聊交给 AstrBot；出现明确开团信号后，同一 channel/session 才进入 FU-GM 接管。
    """

    def __init__(self, root: str | Path = "data/campaigns") -> None:
        self.root = Path(root)
        self.path = self.root / "_session_gates.json"

    def get(self, campaign_id: str, channel_id: str = "", session_id: str = "") -> SessionGateState:
        data = self._load()
        key = self._key(campaign_id, channel_id, session_id)
        payload = data.get(key)
        if isinstance(payload, dict):
            return SessionGateState(**{**self._default_payload(campaign_id, channel_id, session_id), **payload})
        return SessionGateState(campaign_id=campaign_id, channel_id=channel_id, session_id=session_id or channel_id or "default")

    def activate(
        self,
        campaign_id: str,
        channel_id: str = "",
        session_id: str = "",
        *,
        status: str = "adventure",
        reason: str = "",
    ) -> SessionGateState:
        if status not in {"pre_session", "session_zero", "adventure"}:
            raise ValueError("会话门控只能激活为 pre_session、session_zero 或 adventure。")
        now = self._now()
        state = self.get(campaign_id, channel_id, session_id)
        if not state.started_at or state.status == "inactive":
            state.started_at = now
        # The storage key deliberately follows the stable group/channel, but
        # the active session id may change every time that group starts a new
        # tabletop session. Do not keep reporting the first session forever.
        state.campaign_id = campaign_id
        state.channel_id = channel_id
        state.session_id = session_id or channel_id or "default"
        state.status = status
        state.reason = reason
        state.updated_at = now
        return self._save_state(state)

    def pause(self, campaign_id: str, channel_id: str = "", session_id: str = "", *, reason: str = "") -> SessionGateState:
        state = self.get(campaign_id, channel_id, session_id)
        state.status = "paused"
        state.reason = reason
        state.updated_at = self._now()
        if not state.started_at:
            state.started_at = state.updated_at
        return self._save_state(state)

    def deactivate(self, campaign_id: str, channel_id: str = "", session_id: str = "", *, reason: str = "") -> SessionGateState:
        state = self.get(campaign_id, channel_id, session_id)
        state.status = "inactive"
        state.reason = reason
        state.updated_at = self._now()
        return self._save_state(state)

    def detect_signal(self, message: str, *, current_status: str = "inactive") -> SessionGateSignal | None:
        text = " ".join(str(message or "").strip().split())
        if not text or text.startswith("/"):
            return None
        lowered = text.lower()

        if current_status != "adventure" and self._contains_any(
            text,
            lowered,
            (
                "开始第零章",
                "开启第零章",
                "进入第零章",
                "开第零章",
                "开始session 0",
                "开始 session 0",
                "进入session 0",
                "进入 session 0",
                "开始世界创建",
                "开启最终物语第零章",
                "开始最终物语第零章",
            ),
        ):
            return SessionGateSignal(kind="start", status="session_zero", reason="明确进入第零章")
        if current_status not in {"session_zero", "adventure"}:
            explicit_pre_session = self._contains_any(
                text,
                lowered,
                (
                    "开团前共识",
                    "开始共识讨论",
                    "进入共识讨论",
                    "开始桌面共识",
                    "准备开团",
                    "准备跑最终物语",
                    "准备最终物语",
                    "开启最终物语跑团",
                    "开始最终物语跑团",
                    "开启最终物语",
                    "开始最终物语",
                    "最终物语开团",
                    "最终物语跑团",
                    "开最终物语",
                    "今晚开团",
                    "开团",
                    "开始跑团",
                ),
            )
            natural_pre_session = re.search(
                r"(?:准备|想|打算).{0,8}"
                r"(?:开团|(?:开始|开启|开一场|开始一场|开启一场)"
                r"\s*[《「『“\"']?(?:最终物语|跑团)|跑(?:一场)?\s*[《「『“\"']?(?:最终物语|跑团|团))",
                text,
                re.IGNORECASE,
            )
            if explicit_pre_session or natural_pre_session:
                return SessionGateSignal(kind="start", status="pre_session", reason="明确进入开团前共识")
        if self._contains_any(
            text,
            lowered,
            (
                "先别开第一章",
                "别开第一章",
                "不要开第一章",
                "先别进入第一章",
                "别进入第一章",
                "不要进入第一章",
                "先别开始第一章",
                "不要开始第一章",
            ),
        ):
            return None
        if self._contains_any(
            text,
            lowered,
            (
                "开第一章",
                "进入第一章",
                "开始第一章",
                "第一章开场",
                "第一章开始",
                "开第一场",
                "进入第一场",
                "开始第一场",
                "第一场开场",
                "第一场开始",
                "开始冒险",
                "进入冒险",
                "继续上次冒险",
                "继续跑团",
                "继续上次",
                "恢复跑团",
            ),
        ):
            return SessionGateSignal(kind="start", status="adventure", reason="明确开团或继续跑团")
        if self._contains_any(text, lowered, ("暂停跑团", "先暂停", "暂停一下", "中场休息", "跑团暂停", "暂停团")):
            return SessionGateSignal(kind="pause", status="paused", reason="明确暂停跑团")
        if self._contains_any(text, lowered, ("收团", "今天到这", "今天到这里", "本场结束", "结束跑团", "跑团结束", "今晚到这", "gm下线", "GM下线")):
            return SessionGateSignal(kind="end", status="inactive", reason="明确收团")
        if current_status == "paused" and self._contains_any(text, lowered, ("继续", "恢复", "回来了", "可以继续")):
            return SessionGateSignal(kind="start", status="adventure", reason="从暂停恢复")
        return None

    def _save_state(self, state: SessionGateState) -> SessionGateState:
        data = self._load()
        data[self._key(state.campaign_id, state.channel_id, state.session_id)] = asdict(state)
        self._atomic_write(data)
        return state

    def _atomic_write(self, data: dict[str, dict]) -> None:
        from fu_gm.components.memory_store import CampaignMemoryStore

        CampaignMemoryStore._atomic_write_text(
            self.path,
            json.dumps(data, ensure_ascii=False, indent=2),
        )

    def _load(self) -> dict[str, dict]:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            return {}
        return {}

    def _key(self, campaign_id: str, channel_id: str = "", session_id: str = "") -> str:
        scope = channel_id or session_id or "default"
        return f"{campaign_id}::{scope}"

    def _default_payload(self, campaign_id: str, channel_id: str = "", session_id: str = "") -> dict[str, str]:
        return {
            "campaign_id": campaign_id,
            "session_id": session_id or channel_id or "default",
            "channel_id": channel_id,
            "status": "inactive",
            "reason": "",
            "started_at": "",
            "updated_at": "",
        }

    def _contains_any(self, text: str, lowered: str, tokens: tuple[str, ...]) -> bool:
        return any(token in text or token.lower() in lowered for token in tokens)

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
