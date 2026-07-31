from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from fu_gm.config import LLMConfig
from fu_gm.http_server import FUGMHttpService
from fu_gm.main import build_demo_app


def _json_safe(value: Any) -> Any:
    """Preserve nested typed rule events in smoke-test artifacts."""

    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def run_online_smoke_test() -> Path:
    """Exercise the same natural-message -> typed-tool path used by AstrBot."""

    config = LLMConfig.from_env()
    if not config.api_key:
        raise RuntimeError("在线冒烟测试需要在 .env 中配置 LLM API Key。")

    service = FUGMHttpService(use_llm=True)
    if service.gm_tool_agent is None:
        raise RuntimeError("FU-GM 类型化工具智能体未启动。")

    campaign_id = "online-smoke"
    session_id = "combat"
    runtime = service._runtime(campaign_id, auto_load=False)
    runtime.app = build_demo_app(use_llm=True)
    runtime.app.memory_store = service._memory_store()
    runtime.app.topic_memory_store.root = service.data_root
    runtime.app.set_campaign_id(campaign_id)
    service.session_gates.activate(
        campaign_id,
        "online-smoke",
        session_id,
        status="adventure",
        reason="在线单一智能体冒烟测试",
    )

    player_input = "瓦莉亚用落雷攻击帝国机甲。"
    status, response = service.handle(
        "POST",
        "/v1/game/turn",
        {
            "campaign_id": campaign_id,
            "session_id": session_id,
            "channel_id": "online-smoke",
            "speaker": "玩家",
            "message": player_input,
            "is_at_bot": True,
            "message_id": "online-smoke-turn-1",
        },
    )
    if not isinstance(response, dict):
        raise RuntimeError(f"在线冒烟测试返回了非 JSON 响应：{response!r}")

    logs_dir = Path("logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = logs_dir / f"online_smoke_test_{timestamp}.json"
    record = {
        "timestamp_utc": timestamp,
        "api_base_url": config.api_base_url,
        "tool_agent_model": getattr(service.gm_tool_agent, "model", ""),
        "player_input": player_input,
        "http_status": status,
        "response": _json_safe(response),
        "tool_receipts": _json_safe(response.get("tool_receipts") or []),
        "agent_trace": _json_safe(response.get("agent_trace") or []),
        "character_state": _json_safe(
            {
                name: {
                    "hp": character.hp,
                    "mp": character.mp,
                    "statuses": list(character.statuses),
                }
                for character in runtime.app.character_manager.all()
                for name in (character.name,)
            }
        ),
    }
    log_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return log_path


def main() -> None:
    print(run_online_smoke_test())


if __name__ == "__main__":
    main()
