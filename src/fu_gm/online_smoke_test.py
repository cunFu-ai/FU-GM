from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from fu_gm.action_brain import LLMActionBrain
from fu_gm.config import LLMConfig
from fu_gm.expressor import LLMExpressor
from fu_gm.interceptor import ActionInterceptor
from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.main import build_demo_app


def run_online_smoke_test() -> Path:
    config = LLMConfig.from_env()
    client = OpenAICompatibleClient(config)
    app = build_demo_app()

    action_brain = LLMActionBrain(
        client=client,
        model=config.action_model,
        fallback=app.action_brain,
    )
    expressor = LLMExpressor(
        client=client,
        model=config.expressor_model,
        fallback=app.expressor,
    )
    app.action_brain = action_brain
    app.expressor = expressor

    player_input = "玩家[瓦莉亚]: 我要用雷电魔法攻击机甲！请给出完整结算与JRPG风格描述。"
    panel = app.build_panel(player_input)
    action = app.action_brain.decide(panel)
    resolution = app.interceptor.resolve(action)
    narration = app.expressor.render(resolution)

    logs_dir = Path("logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = logs_dir / f"online_smoke_test_{timestamp}.json"

    serialized_payload = {}
    for key, value in resolution.payload.items():
        if hasattr(value, "__dataclass_fields__"):
            serialized_payload[key] = asdict(value)
        else:
            serialized_payload[key] = value

    record = {
        "timestamp_utc": timestamp,
        "api_base_url": config.api_base_url,
        "action_model": config.action_model,
        "expressor_model": config.expressor_model,
        "player_input": player_input,
        "panel": panel.__dict__,
        "action_brain_debug": {
            "used_fallback": action_brain.last_used_fallback,
            "error": action_brain.last_error,
            "raw_model_output": action_brain.last_raw_content,
        },
        "action": {
            "action_type": action.action_type.value,
            "parameters": action.parameters,
        },
        "resolution": {
            "rules_text": resolution.rules_text,
            "payload": serialized_payload,
        },
        "expressor_debug": {
            "used_fallback": expressor.last_used_fallback,
            "error": expressor.last_error,
            "raw_model_output": expressor.last_raw_content,
        },
        "narration": narration,
    }
    log_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return log_path


def main() -> None:
    log_path = run_online_smoke_test()
    print(str(log_path))


if __name__ == "__main__":
    main()
