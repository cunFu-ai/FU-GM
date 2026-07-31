#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from fu_gm.gm_persona import load_gm_persona_text
from fu_gm.gm_tool_agent import LLMGMToolAgent
from fu_gm.gm_tool_contracts import GMToolExecutionContext, GMToolRegistry


class _NoopClient:
    config = type("_Config", (), {"timeout_seconds": 30.0})()

    def create_chat_completion(self, **_kwargs: object) -> str:
        raise AssertionError("导出system prompt不应调用模型。")


def _context(
    *,
    gate_status: str,
    heartbeat: bool = False,
) -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="prompt-export",
        session_id="prompt-export",
        channel_id="prompt-export",
        speaker="示例玩家",
        gate_status=gate_status,
        directly_addressed=True,
        metadata={"system_gm_beat_request": True} if heartbeat else {},
    )


def export_prompts(project_root: Path, output_path: Path) -> None:
    style_path = project_root / "config/gm_styles/acg_highschool_gm.md"
    persona_text, persona_source = load_gm_persona_text(
        environ={"FU_GM_STYLE_FILE": str(style_path)},
        base_dir=project_root,
    )
    agent = LLMGMToolAgent(
        _NoopClient(),
        model="prompt-export",
        registry=GMToolRegistry(),
        gm_personality_prompt=persona_text,
    )
    free_state = {"runtime": {"conflict": {"active": False}}}
    conflict_state = {"runtime": {"conflict": {"active": True}}}
    cases = [
        (
            "群聊与管理",
            _context(gate_status="inactive"),
            free_state,
            False,
        ),
        (
            "第零章",
            _context(gate_status="session_zero"),
            free_state,
            False,
        ),
        (
            "冒险场景",
            _context(gate_status="adventure"),
            free_state,
            False,
        ),
        (
            "冲突场景",
            _context(gate_status="adventure"),
            conflict_state,
            False,
        ),
        (
            "自由场景主动节拍",
            _context(gate_status="adventure", heartbeat=True),
            free_state,
            False,
        ),
        (
            "冲突场景主动节拍",
            _context(gate_status="adventure", heartbeat=True),
            conflict_state,
            False,
        ),
        (
            "第零章工具收尾",
            _context(gate_status="session_zero"),
            free_state,
            True,
        ),
        (
            "冒险工具收尾",
            _context(gate_status="adventure"),
            free_state,
            True,
        ),
    ]
    rendered: list[tuple[str, str]] = []
    for title, context, state, has_receipts in cases:
        rendered.append(
            (
                title,
                agent._system_prompt(
                    context,
                    observed_state=state,
                    has_receipts=has_receipts,
                ),
            )
        )

    rows = "\n".join(
        f"| {title} | {len(prompt):,} | {len(prompt.splitlines()):,} |"
        for title, prompt in rendered
    )
    sections = "\n\n".join(
        f"## {title}\n\n```text\n{prompt}\n```"
        for title, prompt in rendered
    )
    output = f"""# FU-GM 压缩后完整 System Prompt

生成时间：{datetime.now(timezone.utc).isoformat()}

人格来源：`{persona_source}`

这些内容由运行时代码直接构造，和实际发送给核心 GM 模型的 system message 一致。`available_tools`、当前消息、近期公开聊天与权威状态位于随后单独发送的 user message，不在本文重复。

## 尺寸

| 场景 | 字符数 | 行数 |
|---|---:|---:|
{rows}

压缩前首轮共享提示为 19,698 字，且每个阶段都会携带全部规则。压缩后按权威阶段组合，只发送当前需要的规则。

{sections}
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="导出FU-GM实际system prompt。")
    parser.add_argument(
        "--output",
        default="docs/generated/gm_system_prompts_compressed.md",
    )
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = project_root / output_path
    export_prompts(project_root, output_path)
    print(output_path)


if __name__ == "__main__":
    main()
