from __future__ import annotations

import json
from typing import Any

from fu_gm.testing.tool_receipt_diagnostics import is_unrecovered_rejection


def _public_turn(call: dict[str, Any]) -> dict[str, Any]:
    """Return only table-visible context, never private prompts or secrets."""

    return {
        "index": call.get("index"),
        "label": str(call.get("label") or ""),
        "speaker": str(call.get("speaker") or ""),
        "message": str(call.get("message") or ""),
        "reply": str(call.get("reply") or ""),
        "route": str(call.get("route") or ""),
        "status": int(call.get("status") or 0),
        "ok": bool(call.get("ok")),
    }


def _tool_receipt_summary(receipt: dict[str, Any]) -> dict[str, Any]:
    result = receipt.get("result")
    result_dict = result if isinstance(result, dict) else {}
    return {
        "tool_name": str(receipt.get("tool_name") or receipt.get("name") or ""),
        "ok": bool(receipt.get("ok")),
        "state_changed": bool(receipt.get("state_changed")),
        "error_code": str(
            receipt.get("error_code")
            or result_dict.get("error_code")
            or ""
        ),
        "message": str(
            receipt.get("message")
            or result_dict.get("message")
            or result_dict.get("error")
            or ""
        )[:1000],
    }


def _error_reasons(call: dict[str, Any]) -> list[str]:
    body = call.get("body")
    body_dict = body if isinstance(body, dict) else {}
    reasons: list[str] = []
    status = int(call.get("status") or 0)
    if status >= 400:
        reasons.append(f"http_{status}")
    if call.get("ok") is False:
        reasons.append("call_not_ok")
    if call.get("strict_semantic_failure"):
        reasons.append("strict_semantic_failure")
    if call.get("service_recovery_attempts"):
        reasons.append("service_recovery")
    for key in ("agent_error", "error"):
        if str(body_dict.get(key) or "").strip():
            reasons.append(key)
    for key in ("llm_invalid_output", "llm_unavailable"):
        if bool(body_dict.get(key)):
            reasons.append(key)
    receipts = [
        item
        for item in body_dict.get("tool_receipts", [])
        if isinstance(item, dict)
    ]
    if any(is_unrecovered_rejection(receipt) for receipt in receipts):
        reasons.append("failed_tool_receipt")
    diagnostics = call.get("llm_diagnostics")
    for component_name, component in (
        diagnostics.items() if isinstance(diagnostics, dict) else []
    ):
        if not isinstance(component, dict):
            continue
        if bool(component.get("used_fallback")):
            reasons.append(f"{component_name}_fallback")
        if str(component.get("error") or "").strip():
            reasons.append(f"{component_name}_error")
        if component.get("recovery_attempts"):
            reasons.append(f"{component_name}_recovery")
    return list(dict.fromkeys(reasons))


def collect_error_contexts(
    calls: list[dict[str, Any]],
    *,
    recent_turns: int = 6,
    fatal_contexts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Collect reproducible failure evidence without exporting model prompts."""

    contexts: list[dict[str, Any]] = []
    for offset, call in enumerate(calls):
        reasons = _error_reasons(call)
        if not reasons:
            continue
        body = call.get("body")
        body_dict = body if isinstance(body, dict) else {}
        receipts = [
            _tool_receipt_summary(item)
            for item in body_dict.get("tool_receipts", [])
            if isinstance(item, dict) and is_unrecovered_rejection(item)
        ]
        contexts.append(
            {
                "kind": "call_error",
                "reasons": reasons,
                "recent_public_context": [
                    _public_turn(item)
                    for item in calls[max(0, offset - recent_turns) : offset]
                ],
                "current_call": _public_turn(call),
                "failure": {
                    "route": str(body_dict.get("route") or ""),
                    "agent_error": str(body_dict.get("agent_error") or "")[:2000],
                    "error": str(body_dict.get("error") or "")[:2000],
                    "provider_error_category": str(
                        body_dict.get("provider_error_category") or ""
                    ),
                    "llm_failure_kind": str(body_dict.get("llm_failure_kind") or ""),
                    "retry_safe": body_dict.get("retry_safe"),
                    "failed_tool_receipts": receipts,
                    "service_recovery_attempts": list(
                        call.get("service_recovery_attempts") or []
                    ),
                    "strict_semantic_failure": call.get("strict_semantic_failure"),
                    "private_failure_diagnostics": call.get(
                        "private_failure_diagnostics"
                    ),
                },
                "llm_diagnostics": call.get("llm_diagnostics") or {},
                "pipeline_span": call.get("pipeline_span") or {},
                "clock_boundaries": call.get("clock_boundaries") or [],
            }
        )
    contexts.extend(list(fatal_contexts or []))
    return contexts


def build_fatal_error_context(
    calls: list[dict[str, Any]],
    *,
    error: BaseException,
    traceback_text: str,
    recent_turns: int = 6,
) -> dict[str, Any]:
    return {
        "kind": "fatal_exception",
        "error_type": type(error).__name__,
        "error": str(error),
        "recent_public_context": [
            _public_turn(item) for item in calls[-max(1, recent_turns) :]
        ],
        "traceback": str(traceback_text),
    }


def format_error_contexts(contexts: list[dict[str, Any]]) -> str:
    if not contexts:
        return "FU-GM 长测错误上下文\n\n本次没有记录到模型、工具、HTTP 或运行时错误。\n"

    lines = ["FU-GM 长测错误上下文", ""]
    for index, context in enumerate(contexts, start=1):
        lines.extend(
            [
                f"=== 错误 {index}: {context.get('kind')} ===",
                json.dumps(context, ensure_ascii=False, indent=2, default=str),
                "",
            ]
        )
    return "\n".join(lines)
