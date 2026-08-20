from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from fu_gm.config import LLMConfig
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import Character, HeroDraft, SceneType
from fu_gm.scene_orchestrator import SceneOrchestrator


_SMOKE_ACTOR = "冒烟测试角色"
_SMOKE_TARGET = "训练靶"
_SMOKE_TARGET_INITIAL_HP = 40


def _json_safe(value: Any) -> Any:
    """把嵌套的类型化规则事件安全写入冒烟测试产物。"""

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


def _seed_online_smoke_fixture(app: SceneOrchestrator) -> None:
    """只为在线探针建立最小规则夹具，不向正常战役注入预制内容。"""

    app.world_state.world_profile.hero_drafts["玩家"] = HeroDraft(
        player_name="玩家",
        hero_name=_SMOKE_ACTOR,
    )
    app.character_manager.add(
        Character(
            name=_SMOKE_ACTOR,
            attributes={"DEX": 12, "MIG": 12, "INS": 6, "WLP": 6},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
            weapon_damage=5,
            weapon_type="physical",
            traits=["pc"],
        )
    )
    app.character_manager.add(
        Character(
            name=_SMOKE_TARGET,
            attributes={"DEX": 6, "MIG": 6, "INS": 6, "WLP": 6},
            max_hp=_SMOKE_TARGET_INITIAL_HP,
            hp=_SMOKE_TARGET_INITIAL_HP,
            max_mp=0,
            mp=0,
            defenses={"physical": 2, "magic": 10},
            traits=["enemy"],
        )
    )
    scene_name = "在线 Agent 冒烟测试"
    app.scene_manager.start_scene(
        scene_name,
        SceneType.CONFLICT,
        location="隔离测试场",
        participants=[_SMOKE_ACTOR, _SMOKE_TARGET],
        objective="验证自然语言动作能够经类型化工具完成规则结算",
    )
    app.conflict_manager.start_scene(
        scene_name,
        [_SMOKE_ACTOR, _SMOKE_TARGET],
        player_side=[_SMOKE_ACTOR],
        enemy_side=[_SMOKE_TARGET],
    )


def run_online_smoke_test() -> Path:
    """验证 AstrBot 实际使用的自然消息到类型化工具链路。"""

    config = LLMConfig.from_env()
    if not config.api_key:
        raise RuntimeError("在线冒烟测试需要在 .env 中配置 LLM API Key。")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")

    service = FUGMHttpService(use_llm=True, rules_seed=1)
    if service.gm_tool_agent is None:
        raise RuntimeError("FU-GM 类型化工具智能体未启动。")

    campaign_id = "online-smoke"
    session_id = "combat"
    runtime = service._runtime(campaign_id, auto_load=False)
    _seed_online_smoke_fixture(runtime.app)
    service.session_gates.activate(
        campaign_id,
        "online-smoke",
        session_id,
        status="adventure",
        reason="在线单一智能体冒烟测试",
    )

    player_input = f"{_SMOKE_ACTOR}用武器普通攻击{_SMOKE_TARGET}。"
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
            "message_id": f"online-smoke-turn-{run_id}",
        },
    )
    if not isinstance(response, dict):
        raise RuntimeError(f"在线冒烟测试返回了非 JSON 响应：{response!r}")

    logs_dir = Path("logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"online_smoke_test_{run_id}.json"
    tool_receipts = list(response.get("tool_receipts") or [])
    agent_trace = list(response.get("agent_trace") or [])
    character_state = {
        character.name: {
            "hp": character.hp,
            "mp": character.mp,
            "statuses": list(character.statuses),
        }
        for character in runtime.app.character_manager.all()
    }
    target_hp = runtime.app.character_manager.get(_SMOKE_TARGET).hp
    validation_checks = {
        "http_ok": status == 200 and bool(response.get("ok")),
        "fresh_response": response.get("route") != "deduplicated",
        "successful_character_action_receipt": any(
            receipt.get("tool_name") == "perform_character_action"
            and bool(receipt.get("ok"))
            for receipt in tool_receipts
            if isinstance(receipt, dict)
        ),
        "target_hp_decreased": target_hp < _SMOKE_TARGET_INITIAL_HP,
    }
    validation_passed = all(validation_checks.values())
    record = {
        "timestamp_utc": run_id,
        "api_base_url": config.api_base_url,
        "tool_agent_model": getattr(service.gm_tool_agent, "model", ""),
        "player_input": player_input,
        "http_status": status,
        "response": _json_safe(response),
        "tool_receipts": _json_safe(tool_receipts),
        "agent_trace": _json_safe(agent_trace),
        "character_state": _json_safe(character_state),
        "validation": {
            "passed": validation_passed,
            "checks": validation_checks,
        },
    }
    log_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if not validation_passed:
        raise RuntimeError(f"在线冒烟测试未完成规则结算，详见 {log_path}。")
    return log_path


def main() -> None:
    print(run_online_smoke_test())


if __name__ == "__main__":
    main()
