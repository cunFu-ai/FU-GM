from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from fu_gm.config import LLMConfig
from fu_gm.http_server import FUGMHttpService


def build_session_zero_service(
    *,
    use_llm: bool = True,
    model: str = "",
    reasoning_effort: str = "",
    thinking_enabled: bool | None = None,
    gm_personality_prompt: str = "",
) -> FUGMHttpService:
    """Build the same single-authority runtime used by HTTP and AstrBot."""

    if not use_llm:
        raise RuntimeError(
            "第零章自然语言 CLI 不再提供启发式主持器；请配置 LLM，"
            "或直接使用类型化工具测试。"
        )
    if model:
        os.environ["FU_GM_CORE_GM_MODEL"] = model
        os.environ["FU_GM_TOOL_AGENT_MODEL"] = model
    if reasoning_effort:
        os.environ["FU_GM_CORE_GM_REASONING_EFFORT"] = reasoning_effort
    if thinking_enabled is not None:
        os.environ["FU_GM_CORE_GM_THINKING"] = "1" if thinking_enabled else "0"

    return FUGMHttpService(
        use_llm=True,
        gm_style_prompt=gm_personality_prompt,
    )


def main() -> None:
    LLMConfig.from_env()
    parser = argparse.ArgumentParser(
        description="通过 FU-GM 单一工具智能体运行交互式第零章。",
    )
    parser.add_argument(
        "--participants",
        nargs="*",
        default=[],
        help="玩家名列表，例如：--participants 阿凛 白河",
    )
    parser.add_argument(
        "--campaign-id",
        default="session-zero-cli",
        help="本次第零章使用的战役 ID。",
    )
    parser.add_argument(
        "--session-id",
        default="session-zero",
        help="本次第零章使用的场次 ID。",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("FU_GM_SESSION_ZERO_MODEL", ""),
        help="覆盖本次 CLI 使用的语义与工具智能体模型。",
    )
    parser.add_argument(
        "--reasoning-effort",
        default=os.environ.get("FU_GM_SESSION_ZERO_REASONING_EFFORT", ""),
        choices=["", "low", "medium", "high"],
        help="覆盖本次 CLI 的 reasoning_effort。",
    )
    parser.add_argument(
        "--thinking",
        choices=["default", "on", "off"],
        default=os.environ.get("FU_GM_SESSION_ZERO_THINKING", "default"),
        help="是否启用模型 thinking。",
    )
    parser.add_argument(
        "--gm-style-file",
        default=os.environ.get("FU_GM_STYLE_FILE", ""),
        help="GM 人格文档路径。",
    )
    parser.add_argument(
        "--log-file",
        default=os.environ.get("FU_GM_SESSION_ZERO_LOG_FILE", ""),
        help="JSONL 日志路径；默认写入 logs/session_zero_时间戳.jsonl。",
    )
    args = parser.parse_args()

    service = build_session_zero_service(
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        thinking_enabled=_parse_thinking_flag(args.thinking),
        gm_personality_prompt=_load_gm_style(args.gm_style_file),
    )
    if service.gm_tool_agent is None:
        raise SystemExit(
            "未能启动 FU-GM 工具智能体。请检查 .env 中的 API Key、端点和模型配置。"
        )

    campaign_id = str(args.campaign_id or "session-zero-cli").strip()
    session_id = str(args.session_id or "session-zero").strip()
    participants = [name.strip() for name in args.participants if name.strip()]
    log_path = _resolve_log_path(args.log_file)
    status, opening = service.handle(
        "POST",
        "/v1/session-zero/start",
        {
            "campaign_id": campaign_id,
            "session_id": session_id,
            "channel_id": "cli",
            "participants": participants,
        },
    )
    if status >= 400 or not isinstance(opening, dict) or not opening.get("ok"):
        raise SystemExit(f"第零章初始化失败：{opening}")

    print("=== FU-GM Session 0 ===")
    print("输入格式：玩家名: 发言。没有冒号时使用当前轮询玩家或“玩家”。")
    print("命令：/snapshot /save [槽名] /exit")
    print(f"日志：{log_path}")
    _append_log(log_path, event="opening", payload=opening)

    runtime = service._runtime(campaign_id)
    while True:
        try:
            raw = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已结束第零章交互。")
            return
        if not raw:
            continue
        if raw in {"/exit", "exit", "quit", "退出"}:
            print("已结束第零章交互。")
            return
        if raw == "/snapshot":
            snapshot = runtime.app.session_zero_snapshot()
            print(json.dumps(snapshot, ensure_ascii=False, indent=2))
            _append_log(log_path, event="snapshot", payload=snapshot)
            continue
        if raw.startswith("/save"):
            slot = raw.removeprefix("/save").strip()
            save_payload: dict[str, Any] = {"campaign_id": campaign_id}
            if slot:
                save_payload["slot"] = slot
            save_status, saved = service.handle(
                "POST",
                "/v1/campaigns/save",
                save_payload,
            )
            print(
                str(saved.get("reply") or saved.get("path") or saved)
                if isinstance(saved, dict)
                else str(saved)
            )
            _append_log(
                log_path,
                event="save",
                payload={"status": save_status, "body": saved},
            )
            continue

        speaker, message = _parse_speaker(raw, runtime.app)
        response_status, response = service.handle(
            "POST",
            "/v1/session-zero/message",
            {
                "campaign_id": campaign_id,
                "session_id": session_id,
                "channel_id": "cli",
                "speaker": speaker,
                "message": message,
                "force_gm_reply": True,
                "message_id": f"cli-{datetime.now().timestamp()}",
            },
        )
        if not isinstance(response, dict):
            print(f"时悠：{response}")
        else:
            reply = str(response.get("reply") or "").strip()
            if reply:
                print(f"时悠：{reply}")
            elif response.get("target") == "silent":
                print("（时悠保持安静。）")
            else:
                print(f"（未生成回复：{response.get('agent_error') or response_status}）")
        _append_log(
            log_path,
            event="message",
            payload={
                "speaker": speaker,
                "message": message,
                "status": response_status,
                "response": response,
            },
        )


def _parse_speaker(raw: str, app: Any) -> tuple[str, str]:
    for separator in (":", "："):
        if separator in raw:
            speaker, message = raw.split(separator, 1)
            return speaker.strip() or "玩家", message.strip()
    speaker = app.session_zero_manager.current_participant_name() or "玩家"
    return speaker, raw


def _parse_thinking_flag(value: str) -> bool | None:
    if value == "on":
        return True
    if value == "off":
        return False
    return None


def _load_gm_style(path_text: str) -> str:
    if not path_text:
        return ""
    path = Path(path_text).expanduser()
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"无法读取 GM 性格文档：{path}；{exc}") from exc


def _resolve_log_path(path_text: str) -> Path:
    if path_text:
        path = Path(path_text).expanduser()
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = Path("logs") / f"session_zero_{stamp}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append_log(path: Path, *, event: str, payload: Any) -> None:
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "event": event,
        "payload": _json_safe(payload),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return {
            key: _json_safe(getattr(value, key))
            for key in value.__dataclass_fields__
        }
    return value


if __name__ == "__main__":
    main()
