from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from fu_gm.gm_tool_agent import (  # noqa: E402
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolReceipt,
    GMToolRegistry,
    LLMGMToolAgent,
)
from fu_gm.testing.codex_subagent_spool import CodexSubagentSpoolClient  # noqa: E402


def _normalized_fact(value: object) -> str:
    return str(value or "").strip().rstrip("。！？!?；;").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="运行一次 Codex 子智能体注入探针。")
    parser.add_argument("--spool-dir", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    args = parser.parse_args()

    spool_dir = Path(args.spool_dir).expanduser().resolve()
    state: dict[str, str] = {}

    def commit_session_zero_update(
        _context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        fact = str(arguments.get("fact") or "").strip()
        if not fact:
            return GMToolReceipt.failure(
                "commit_session_zero_update",
                "FACT_REQUIRED",
                "fact 不能为空。",
                "根据玩家刚确认的事实填写 fact。",
            )
        state["public_fact"] = fact
        return GMToolReceipt.success(
            "commit_session_zero_update",
            result={"public_fact": fact},
            state_changed=True,
            public_reply=f"已记录：{fact}",
        )

    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="commit_session_zero_update",
            description="记录全桌刚刚明确确认、且不与现状冲突的一条第零章世界事实。",
            handler=commit_session_zero_update,
            parameters=(
                GMToolParameter(
                    name="fact",
                    kind="string",
                    description="只填写玩家明确确认的事实，不扩写新内容。",
                    required=True,
                ),
            ),
            side_effect="write",
            max_successful_calls_per_message=1,
        )
    )
    client = CodexSubagentSpoolClient(
        spool_dir,
        timeout_seconds=args.timeout_seconds,
        test_only=True,
    )
    agent = LLMGMToolAgent(
        client,
        model="gpt-5.6-terra",
        registry=registry,
        max_iterations=3,
        timeout_seconds=args.timeout_seconds,
    )
    context = GMToolExecutionContext(
        campaign_id="codex-spool-probe",
        session_id="single-round",
        channel_id="local-only",
        speaker="阿凛",
        gate_status="session_zero",
        directly_addressed=True,
        metadata={"test_only_codex_spool": True},
    )
    player_message = "时悠，大家已经确认：旧井的水会在月落时倒流。请记下来。"
    outcome = agent.run(
        player_message,
        recent_context="阿凛：我们先确认旧井在夜里的变化。",
        context=context,
        state_summary={
            "phase": "第零章",
            "world_creation": {"confirmed_public_facts": []},
        },
    )
    report = {
        "ok": bool(
            outcome.handled
            and outcome.state_changed
            and _normalized_fact(state.get("public_fact"))
            == "旧井的水会在月落时倒流"
        ),
        "provider": "codex_subagent",
        "test_only": True,
        "player_message": player_message,
        "state": state,
        "reply": outcome.reply,
        "reply_parts": list(outcome.reply_parts),
        "error": outcome.error,
        "receipts": [receipt.to_dict() for receipt in outcome.receipts],
        "trace": list(outcome.trace),
        "telemetry": client.telemetry_payload(),
    }
    report_path = spool_dir / "round_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"REPORT={report_path}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
