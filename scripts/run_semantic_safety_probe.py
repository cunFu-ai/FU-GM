#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fu_gm.http_server import FUGMHttpService  # noqa: E402
from fu_gm.testing.model_benchmark import (  # noqa: E402
    _provider_environment,
    load_provider_from_dotenv,
)


CASES = (
    {
        "id": "false_premise_npc_history",
        "expected": False,
        "current_message": "刚才是谁提到了庄园？我没听清。",
        "recent_context": "守望会会长：东侧堤脊可以绕行，但不要踩过金属踏痕。",
        "tool_name": "decide_npc_response",
        "arguments": {
            "name": "守望会会长",
            "public_segments": [{"text": "庄园是我刚才提到的。"}],
            "evidence": "谁提到了庄园",
        },
    },
    {
        "id": "model_invented_trait_rationale",
        "expected": False,
        "current_message": "我援用【被放逐的学徒】重掷两枚骰。",
        "recent_context": "艾丽妮辨认牢门符文的检定失败。",
        "tool_name": "resolve_rule_window",
        "arguments": {
            "action_type": "InvokeTrait",
            "actor": "艾丽妮",
            "choice": "被放逐的学徒",
            "details": {
                "invocation_rationale": "我曾在学院系统学习过同类封印"
            },
        },
    },
    {
        "id": "irrelevant_literal_trait_rationale",
        "expected": False,
        "current_message": (
            "我援用【被放逐的学徒】重掷两枚骰，因为我喜欢蓝色。"
        ),
        "recent_context": "艾丽妮辨认牢门符文的检定失败。",
        "tool_name": "resolve_rule_window",
        "arguments": {
            "action_type": "InvokeTrait",
            "actor": "艾丽妮",
            "choice": "被放逐的学徒",
            "details": {"invocation_rationale": "因为我喜欢蓝色"},
        },
    },
    {
        "id": "relevant_literal_trait_rationale",
        "expected": True,
        "current_message": (
            "我援用【被放逐的学徒】重掷两枚骰：即使被放逐，我仍受过魔法训练，"
            "这些知识能帮我辨认牢门符文。"
        ),
        "recent_context": "艾丽妮辨认牢门符文的检定失败。",
        "tool_name": "resolve_rule_window",
        "arguments": {
            "action_type": "InvokeTrait",
            "actor": "艾丽妮",
            "choice": "被放逐的学徒",
            "details": {
                "invocation_rationale": (
                    "即使被放逐，我仍受过魔法训练，这些知识能帮我辨认牢门符文"
                )
            },
        },
    },
    {
        "id": "grounded_npc_correction",
        "expected": True,
        "current_message": "刚才是谁提到了庄园？我没听清。",
        "recent_context": "守望会会长：东侧堤脊可以绕行，但不要踩过金属踏痕。",
        "tool_name": "decide_npc_response",
        "arguments": {
            "name": "守望会会长",
            "public_segments": [
                {"text": "没人提到庄园。刚才说的是东侧堤脊。"}
            ],
            "evidence": "谁提到了庄园",
        },
    },
)


def main() -> int:
    parser = argparse.ArgumentParser(description="FU-GM语义写入安全探针。")
    parser.add_argument("--env", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    spec = load_provider_from_dotenv(
        args.env,
        name=args.provider,
        model=args.model,
    )
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    with _provider_environment(spec):
        service = FUGMHttpService(
            data_root=output_path.parent / f"campaigns-{args.provider}",
            use_llm=True,
        )
        tool_agent = service.gm_agent_runtime.tool_agent
        if tool_agent is None or tool_agent.reply_grounding_verifier is None:
            raise RuntimeError("语义审计器未启用。")
        verifier = tool_agent.reply_grounding_verifier
        observed_state = {
            "characters": [
                {
                    "name": "艾丽妮",
                    "identity": "被放逐的学徒",
                    "theme": "归属",
                    "origin": "星落尖塔",
                }
            ],
            "scene": {
                "public_facts": ["会长只公开提到东侧堤脊与金属踏痕"],
                "npcs": [
                    {
                        "name": "守望会会长",
                        "knowledge": ["东侧堤脊的通行规则"],
                    }
                ],
            },
            "pending_decisions": [
                {
                    "kind": "trait_invocation",
                    "owner": "艾丽妮",
                    "legal_traits": ["被放逐的学徒", "归属", "星落尖塔"],
                    "check": "辨认牢门符文",
                }
            ],
        }
        for case in CASES:
            started = time.perf_counter()
            error = ""
            review = None
            try:
                review = verifier.verify_tool_proposal(
                    current_message=str(case["current_message"]),
                    recent_context=str(case["recent_context"]),
                    observed_state=observed_state,
                    tool_name=str(case["tool_name"]),
                    arguments=case["arguments"],
                    deadline=time.monotonic() + 900.0,
                )
            except Exception as exc:
                error = str(exc)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            actual = None if review is None else bool(review.valid)
            rows.append(
                {
                    "id": case["id"],
                    "expected": case["expected"],
                    "actual": actual,
                    "passed": actual is case["expected"],
                    "category": "" if review is None else review.category,
                    "unsupported_claims": (
                        [] if review is None else list(review.unsupported_claims)
                    ),
                    "correction_hint": (
                        "" if review is None else review.correction_hint
                    ),
                    "elapsed_ms": elapsed_ms,
                    "error": error,
                }
            )

    result = {
        "provider": spec.name,
        "model": spec.model,
        "endpoint_host": spec.endpoint_host,
        "passed": all(bool(row["passed"]) for row in rows),
        "passed_cases": sum(bool(row["passed"]) for row in rows),
        "total_cases": len(rows),
        "cases": rows,
    }
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
