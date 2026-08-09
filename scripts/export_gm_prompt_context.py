#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fu_gm

from fu_gm.config import LLMConfig
from fu_gm.gm_tool_agent import LLMGMToolAgent
from fu_gm.gm_tool_contracts import GMToolExecutionContext
from fu_gm.http_server import FUGMHttpService


class _NoNetworkClient:
    """Provide agent configuration while making accidental API use impossible."""

    config = type("_Config", (), {"timeout_seconds": 30.0})()

    def create_chat_completion(self, **_kwargs: object) -> str:
        raise AssertionError("提示词导出不得调用任何外部模型。")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="重建并导出核心GM实际会收到的完整消息上下文。",
    )
    parser.add_argument(
        "--data-root",
        default="/Users/example/.fu-gm/data/campaigns",
        help="FU-GM战役数据目录。",
    )
    parser.add_argument("--campaign", default="default")
    parser.add_argument("--session", default="200000001")
    parser.add_argument("--channel", default="200000001")
    parser.add_argument("--speaker", default="测试玩家甲")
    parser.add_argument("--speaker-id", default="100000001")
    parser.add_argument(
        "--message",
        default="@时悠，我先观察牢门上的蓝色符文。",
    )
    parser.add_argument(
        "--output",
        default="docs/generated/gm_prompt_context_default_2026-08-09.md",
    )
    parser.add_argument(
        "--json-output",
        default="docs/generated/gm_prompt_context_default_2026-08-09.json",
    )
    return parser.parse_args()


def _resolve_path(project_root: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else project_root / path


def _model_name(config: LLMConfig) -> str:
    return (
        os.environ.get("FU_GM_TOOL_AGENT_MODEL", "").strip()
        or os.environ.get("FU_GM_CORE_GM_MODEL", "").strip()
        or config.action_model
    )


def _payload(args: argparse.Namespace) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    event_id = "prompt-preview-event-1"
    message_id = "prompt-preview-message-1"
    return {
        "campaign_id": args.campaign,
        "session_id": args.session,
        "channel_id": args.channel,
        "speaker": args.speaker,
        "speaker_id": args.speaker_id,
        "message": args.message,
        "message_id": message_id,
        "is_at_bot": True,
        "force_gm_reply": True,
        "current_turn_events": [
            {
                "event_id": event_id,
                "message_id": message_id,
                "speaker": args.speaker,
                "speaker_id": args.speaker_id,
                "text": args.message,
                "created_at": now,
                "is_private": False,
                "is_at_gm": True,
                "is_reply_to_gm": False,
            }
        ],
        "conversation_turn_id": "prompt-preview-turn-1",
        "turn_force_gm_reply": True,
    }


def _section_sizes(request: dict[str, Any]) -> list[tuple[str, int]]:
    result: list[tuple[str, int]] = []
    for key, value in request.items():
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        result.append((key, len(rendered)))
    return sorted(result, key=lambda item: item[1], reverse=True)


def export_context(args: argparse.Namespace) -> tuple[Path, Path]:
    project_root = Path(__file__).resolve().parents[1]
    data_root = Path(args.data_root).expanduser()
    service = FUGMHttpService(data_root=data_root, use_llm=False)
    config = LLMConfig.from_env()
    agent = LLMGMToolAgent(
        _NoNetworkClient(),
        model=_model_name(config),
        registry=service.gm_tool_registry,
        gm_personality_prompt=service.gm_style_prompt,
    )

    payload = _payload(args)
    envelope = service.gm_message_envelope_builder.build(payload)
    gate = service.session_gates.get(
        envelope.campaign_id,
        envelope.channel_id,
        envelope.session_id,
    )
    runtime = service._runtime(envelope.campaign_id)
    recent_context = runtime.log_manager.format_live_context(
        envelope.campaign_id,
        envelope.session_id,
        limit=8,
    )
    coordinator = service.gm_agent_message_coordinator
    metadata = coordinator._request_metadata(
        payload,
        message=envelope.current_message,
        recent_context=recent_context,
    )
    metadata["recent_public_messages"] = coordinator._recent_public_messages(
        runtime,
        envelope.campaign_id,
        envelope.session_id,
    )
    metadata["recent_message_delivery_context"] = (
        coordinator._recent_message_delivery_context(
            envelope.campaign_id,
            envelope.session_id,
            envelope.channel_id,
            current_message_id=str(payload["message_id"]),
        )
    )
    metadata["gm_dynamic_capabilities_enabled"] = True
    context = GMToolExecutionContext(
        campaign_id=envelope.campaign_id,
        session_id=envelope.session_id,
        channel_id=envelope.channel_id,
        speaker=envelope.speaker,
        gate_status=gate.status,
        is_private=envelope.is_private,
        directly_addressed=True,
        metadata=metadata,
    )
    state = coordinator.state_builder.build(context)
    messages = agent._build_decision_messages(
        current_message=envelope.current_message,
        recent_context=recent_context,
        context=context,
        observed_state=state,
        receipts=[],
        history=[],
    )
    if len(messages) != 2:
        raise RuntimeError(f"预期2条模型消息，实际得到{len(messages)}条。")

    request = json.loads(messages[1].content)
    tool_names = [
        str(tool.get("name") or "")
        for tool in list(request.get("available_tools") or [])
        if isinstance(tool, dict)
    ]
    section_sizes = _section_sizes(request)
    size_rows = "\n".join(
        f"| `{name}` | {size:,} |"
        for name, size in section_sizes
    )
    generated_at = datetime.now(timezone.utc).isoformat()
    code_source = str(Path(str(fu_gm.__file__ or "")).resolve())
    system_message = messages[0]
    user_message = messages[1]

    markdown = f"""# FU-GM 核心 GM 完整提示词上下文

生成时间：{generated_at}

这份文件由导出进程实际加载的 FU-GM 运行时代码直接重建，**没有调用外部模型**。它使用部署目录中的 `{args.campaign}` 存档与 `{args.session}` 场次，并添加了一条合成的新消息，用来展示下一次核心 GM 决策实际会收到的两条消息。

> 注意：`current_state_summary` 可能包含 GM 私有准备、隐藏动机和未公开线索，不应把本文分享给玩家。

## 请求概况

- 模型：`{agent.model}`
- 运行时代码：`{code_source}`
- 人格来源：`{service.gm_persona_source}`
- 战役 / 场次 / 频道：`{envelope.campaign_id}` / `{envelope.session_id}` / `{envelope.channel_id}`
- 门控阶段：`{gate.status}`
- 合成当前消息：`{envelope.current_message}`
- System 字符数：{len(system_message.content):,}
- User JSON 字符数：{len(user_message.content):,}
- 本轮开放工具数：{len(tool_names)}
- 最近公开消息数：{len(list(request.get('recent_messages') or []))}
- System 缓存族：`{system_message.cache_family}`
- System 缓存断点：`{list(system_message.cache_breakpoint_offsets)}`
- User 缓存断点：`{list(user_message.cache_breakpoint_offsets)}`

## User JSON 各顶层部分尺寸

| 部分 | 紧凑 JSON 字符数 |
|---|---:|
{size_rows}

## 本轮开放工具名称

```text
{chr(10).join(tool_names)}
```

## Message 1：system

```text
{system_message.content}
```

## Message 2：user

```json
{json.dumps(request, ensure_ascii=False, indent=2)}
```
"""

    output_path = _resolve_path(project_root, args.output)
    json_output_path = _resolve_path(project_root, args.json_output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    json_output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    json_output_path.write_text(
        json.dumps(
            {
                "generated_at": generated_at,
                "model": agent.model,
                "code_source": code_source,
                "persona_source": service.gm_persona_source,
                "campaign_id": envelope.campaign_id,
                "session_id": envelope.session_id,
                "channel_id": envelope.channel_id,
                "gate_status": gate.status,
                "messages": [
                    {
                        "role": message.role,
                        "content": message.content,
                        "cache_breakpoint": message.cache_breakpoint,
                        "cache_family": message.cache_family,
                        "cache_breakpoint_offsets": list(
                            message.cache_breakpoint_offsets
                        ),
                    }
                    for message in messages
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return output_path, json_output_path


def main() -> None:
    markdown_path, json_path = export_context(_arguments())
    print(markdown_path)
    print(json_path)


if __name__ == "__main__":
    main()
