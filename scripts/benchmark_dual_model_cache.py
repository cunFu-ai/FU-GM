#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from fu_gm.app_factory import _component_llm_config  # noqa: E402
from fu_gm.config import LLMConfig  # noqa: E402
from fu_gm.llm_client import ChatMessage, OpenAICompatibleClient  # noqa: E402


def _message(data: dict[str, Any]) -> ChatMessage:
    return ChatMessage(
        role=str(data.get("role") or "user"),
        content=str(data.get("content") or ""),
        cache_breakpoint=bool(data.get("cache_breakpoint")),
        cache_family=str(data.get("cache_family") or ""),
        cache_breakpoint_offsets=tuple(
            int(item) for item in list(data.get("cache_breakpoint_offsets") or [])
        ),
    )


def _replace_message(messages: list[ChatMessage], old: str, new: str) -> list[ChatMessage]:
    if len(old) != len(new):
        raise ValueError("A/B动态消息必须等长，才能保持既有缓存断点偏移。")
    return [
        replace(message, content=message.content.replace(old, new))
        for message in messages
    ]


def _call(
    client: OpenAICompatibleClient,
    *,
    model: str,
    messages: list[ChatMessage],
    operation: str,
    max_tokens: int,
) -> dict[str, Any]:
    before = len(client.recent_calls)
    started = time.perf_counter()
    output = client.create_chat_completion(
        model=model,
        messages=messages,
        temperature=0.2,
        response_format={"type": "json_object"},
        max_tokens=max_tokens,
        operation=operation,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    records = [dict(item) for item in client.recent_calls[before:]]
    final_record = records[-1] if records else {}
    usage = dict(final_record.get("usage") or {})
    prompt_tokens = max(0, int(usage.get("prompt_tokens") or 0))
    cached_tokens = max(0, int(usage.get("cached_tokens") or 0))
    return {
        "elapsed_ms": elapsed_ms,
        "output": output,
        "provider_records": records,
        "prompt_tokens": prompt_tokens,
        "cached_tokens": cached_tokens,
        "cache_usage_reported": bool(usage.get("cache_usage_reported")),
        "cache_hit": cached_tokens > 0,
        "cached_prompt_ratio": round(cached_tokens / prompt_tokens, 4)
        if prompt_tokens
        else 0.0,
        "cache_key": str(dict(final_record.get("prompt_cache") or {}).get("key") or ""),
        "base_fingerprint": str(
            dict(final_record.get("prompt_cache") or {}).get("base_fingerprint") or ""
        ),
        "prefix_fingerprint": str(
            dict(final_record.get("prompt_cache") or {}).get("prefix_fingerprint") or ""
        ),
    }


def _run_sequence(
    *,
    client: OpenAICompatibleClient,
    model: str,
    base_messages: list[ChatMessage],
    old_message: str,
    alternate_message: str,
    operation: str,
    max_tokens: int,
) -> list[dict[str, Any]]:
    variants = (
        ("A1", base_messages),
        ("B", _replace_message(base_messages, old_message, alternate_message)),
        ("A2", base_messages),
    )
    results: list[dict[str, Any]] = []
    for label, messages in variants:
        result = _call(
            client,
            model=model,
            messages=messages,
            operation=operation,
            max_tokens=max_tokens,
        )
        result["label"] = label
        results.append(result)
        print(
            f"{operation} {label}: {result['elapsed_ms']}ms, "
            f"cached={result['cached_tokens']}/{result['prompt_tokens']} "
            f"({result['cached_prompt_ratio']:.2%})",
            flush=True,
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="测量Terra与DeepSeek的A-B-A延迟和前缀缓存。")
    parser.add_argument(
        "--context",
        default=str(
            PROJECT_ROOT
            / "reports/context_exports/dual_model_context_default_2026-08-12.json"
        ),
    )
    parser.add_argument(
        "--output-root",
        default=str(PROJECT_ROOT / ".runtime/benchmarks"),
    )
    args = parser.parse_args()

    context = json.loads(Path(args.context).read_text(encoding="utf-8"))
    terra_messages = [_message(item) for item in list(context["messages"])]
    expressor_messages = [
        _message(item) for item in list(context["expressor_messages"])
    ]
    original = "@时悠，我们现在停在哪里？"
    alternate = "@时悠，我们下一步做什么？"

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    namespace = f"fugm-bench-{stamp}"
    base_config = LLMConfig.from_env()
    terra_config = replace(
        _component_llm_config(base_config, "ACTION"),
        prompt_cache_key_prefix=namespace + "-terra",
    )
    expressor_config = replace(
        _component_llm_config(base_config, "EXPRESSOR"),
        prompt_cache_key_prefix=namespace + "-expressor",
    )
    terra_client = OpenAICompatibleClient(terra_config)
    expressor_client = OpenAICompatibleClient(expressor_config)

    terra_results = _run_sequence(
        client=terra_client,
        model=terra_config.action_model,
        base_messages=terra_messages,
        old_message=original,
        alternate_message=alternate,
        operation="benchmark.terra",
        max_tokens=700,
    )
    expressor_results = _run_sequence(
        client=expressor_client,
        model=expressor_config.expressor_model,
        base_messages=expressor_messages,
        old_message=original,
        alternate_message=alternate,
        operation="benchmark.expressor",
        max_tokens=700,
    )

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "method": "A-B-A with a fresh prompt_cache_key namespace",
        "namespace": namespace,
        "context": str(Path(args.context).resolve()),
        "terra": {
            "model": terra_config.action_model,
            "endpoint": terra_config.chat_completions_url(),
            "results": terra_results,
            "telemetry": terra_client.telemetry_payload(),
        },
        "expressor": {
            "model": expressor_config.expressor_model,
            "endpoint": expressor_config.chat_completions_url(),
            "results": expressor_results,
            "telemetry": expressor_client.telemetry_payload(),
        },
    }
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    output = output_root / f"dual_model_cache_{stamp}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"REPORT={output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
