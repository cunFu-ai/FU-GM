#!/usr/bin/env python3
"""Run a sequential, isolated FU-GM A/B/C model screen.

The matrix reuses the production-like Chapter One/non-opening smoke harness,
but injects one client bundle per model.  It never writes credentials to disk,
never posts to the production service, and stores only sanitized public output,
tool receipts, aggregate usage, latency, and error categories.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import statistics
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"
for path in (SRC, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from benchmark_deepseek_nonopening import (  # noqa: E402
    _production_unchanged,
    _run_smoke,
    _scenario_latency_summary,
)
from benchmark_deepseek_opening_ab import _production_snapshot  # noqa: E402
from probe_deepseek_session_prep_json import (  # noqa: E402
    provider_config,
    read_dotenv,
)
from fu_gm.config import LLMConfig  # noqa: E402


MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"
MODELS = ("mimo-v2.5", "deepseek-v4-flash", "mimo-v2.5-pro")
MIMO_PRICES_CNY_PER_MTOK = {
    "mimo-v2.5": {"hit": 0.02, "miss": 1.0, "output": 2.0},
    "mimo-v2.5-pro": {"hit": 0.025, "miss": 3.0, "output": 6.0},
}
DEEPSEEK_PRICES_CNY_PER_MTOK = {
    "peak": {"hit": 0.10, "miss": 3.0, "output": 9.0},
    "off_peak": {"hit": 0.05, "miss": 1.5, "output": 4.5},
}
PRICING_TIMEZONE = ZoneInfo("Asia/Shanghai")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an isolated FU-GM model matrix for MiMo 2.5/Pro and DeepSeek V4 Flash."
    )
    parser.add_argument(
        "--deepseek-dotenv",
        type=Path,
        default=Path.home() / ".fu-gm" / "fu_gm.env",
    )
    parser.add_argument("--output-root", type=Path, default=ROOT / "artifacts")
    parser.add_argument("--seed", type=int, default=20260820)
    parser.add_argument("--prewarm-timeout", type=float, default=180.0)
    parser.add_argument("--background-timeout", type=float, default=180.0)
    parser.add_argument(
        "--models",
        nargs="+",
        choices=MODELS,
        default=list(MODELS),
    )
    parser.add_argument("--skip-npc-dialogue", action="store_true")
    parser.add_argument("--skip-session-close", action="store_true")
    return parser.parse_args()


def _mimo_api_key() -> str:
    value = str(os.environ.get("FU_GM_BENCH_MIMO_API_KEY") or "").strip()
    if value:
        return value
    if not sys.stdin.isatty():
        raise RuntimeError(
            "MiMo key is absent; set FU_GM_BENCH_MIMO_API_KEY or run from a TTY"
        )
    value = getpass.getpass("MiMo API key: ").strip()
    if not value:
        raise RuntimeError("MiMo API key is empty")
    return value


def _mimo_config(model: str, api_key: str) -> LLMConfig:
    return LLMConfig(
        api_base_url=MIMO_BASE_URL,
        api_key=api_key,
        action_model=model,
        expressor_model=model,
        backup_api_base_urls=(),
        timeout_seconds=180.0,
        endpoint_attempt_timeout_seconds=90.0,
        reasoning_effort="",
        thinking_enabled=False,
        response_format_enabled=True,
        # MiMo caching is automatic.  Do not send FU-GM/OpenAI cache-key
        # extensions that are not in MiMo's public Chat API contract.
        prompt_cache_enabled=False,
        prompt_cache_mode="off",
        reactive_recovery_enabled=True,
        reactive_recovery_max_retries=1,
        reactive_recovery_target_chars=48000,
        allow_heuristic_fallback=False,
    )


def _deepseek_config(path: Path) -> LLMConfig:
    config = provider_config(read_dotenv(path))
    return replace(
        config,
        timeout_seconds=180.0,
        endpoint_attempt_timeout_seconds=90.0,
        backup_api_base_urls=(),
        thinking_enabled=False,
        reactive_recovery_max_retries=1,
    )


def provider_configs(
    *,
    models: Iterable[str],
    deepseek_dotenv: Path,
    mimo_api_key: str,
) -> list[LLMConfig]:
    result: list[LLMConfig] = []
    deepseek: LLMConfig | None = None
    for model in models:
        if model.startswith("mimo-"):
            result.append(_mimo_config(model, mimo_api_key))
        else:
            if deepseek is None:
                deepseek = _deepseek_config(deepseek_dotenv)
            if deepseek.action_model != model:
                raise ValueError(
                    f"DeepSeek dotenv selects {deepseek.action_model}, expected {model}"
                )
            result.append(deepseek)
    return result


def _sum_usage(*rows: dict[str, object]) -> dict[str, object]:
    keys = (
        "call_count",
        "model_elapsed_ms",
        "prompt_tokens",
        "completion_tokens",
        "cached_tokens",
        "cache_miss_tokens",
        "cache_hit_calls",
        "cache_usage_reported_calls",
        "retry_attempts",
        "failed_attempts",
    )
    result = {
        key: sum(int(row.get(key) or 0) for row in rows)
        for key in keys
    }
    prompt = int(result["prompt_tokens"])
    cached = int(result["cached_tokens"])
    result["cache_token_hit_rate"] = (
        round(cached / prompt, 6) if prompt else None
    )
    result["cache_usage_status"] = (
        "reported"
        if int(result["cache_usage_reported_calls"]) == int(result["call_count"])
        and int(result["call_count"]) > 0
        else (
            "partial"
            if int(result["cache_usage_reported_calls"]) > 0
            else "unknown"
        )
    )
    return result


def _pricing_for_model(
    model: str,
    *,
    at: datetime | None = None,
) -> dict[str, object]:
    observed_at = at or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    shanghai = observed_at.astimezone(PRICING_TIMEZONE)
    if model == "deepseek-v4-flash":
        minute = shanghai.hour * 60 + shanghai.minute
        peak = (9 * 60 <= minute < 12 * 60) or (
            14 * 60 <= minute < 18 * 60
        )
        window = "peak_09_12_or_14_18" if peak else "off_peak"
        rates = DEEPSEEK_PRICES_CNY_PER_MTOK[
            "peak" if peak else "off_peak"
        ]
    else:
        window = "flat"
        rates = MIMO_PRICES_CNY_PER_MTOK[model]
    return {
        "pricing_window": window,
        "pricing_timezone": "Asia/Shanghai",
        "pricing_observed_at": shanghai.isoformat(),
        "rates_per_million_tokens": dict(rates),
    }


def _cost_cny(
    model: str,
    usage: dict[str, object],
    *,
    pricing_at: datetime | None = None,
) -> dict[str, object]:
    pricing = _pricing_for_model(model, at=pricing_at)
    rates = dict(pricing["rates_per_million_tokens"])
    prompt = max(0, int(usage.get("prompt_tokens") or 0))
    completion = max(0, int(usage.get("completion_tokens") or 0))
    cached = max(0, min(prompt, int(usage.get("cached_tokens") or 0)))
    reported = int(usage.get("cache_usage_reported_calls") or 0) > 0
    # If a provider omits cache usage, price every input token as a miss.  The
    # result is deliberately conservative rather than inventing a 0% hit rate.
    billed_cached = cached if reported else 0
    billed_miss = max(0, prompt - billed_cached)
    total = (
        billed_cached * rates["hit"]
        + billed_miss * rates["miss"]
        + completion * rates["output"]
    ) / 1_000_000
    return {
        "currency": "CNY",
        "estimate": round(total, 6),
        "cache_accounting": "reported" if reported else "all_input_as_cache_miss",
        **pricing,
    }


def _scenario_failed_attempts(row: dict[str, object]) -> int:
    provider = dict(row.get("provider") or {})
    client = dict(provider.get("client") or {})
    usage = dict(client.get("usage") or {})
    return max(0, int(usage.get("failed_attempts") or 0))


def _failure_category(
    *,
    fatal_error: str,
    rows: list[dict[str, object]],
    run: dict[str, object],
) -> str:
    prep = dict(run.get("preparation") or {})
    prep_diagnostics = dict(prep.get("diagnostics") or {})
    if prep and not bool(prep_diagnostics.get("model_reviewed")):
        return "model_session_prep_contract"
    text = " ".join(
        [fatal_error]
        + [
            str(dict(row.get("response") or {}).get("agent_error") or "")
            for row in rows
        ]
    ).lower()
    if any(marker in text for marker in ("llmhttp", "timeout", "rate_limit", "429", "502", "503")):
        return "provider_or_network"
    if any(
        _scenario_failed_attempts(row) > 0
        for row in rows
    ) or int(dict(run.get("route_provider_total") or {}).get("failed_attempts") or 0) > 0:
        return "provider_or_network"
    if "json" in text or "protocol" in text or "iteration_exhausted" in text:
        return "model_protocol"
    if any(
        any(not bool(receipt.get("ok")) for receipt in list(row.get("receipts") or []))
        for row in rows
    ):
        return "python_receipt_or_transaction"
    if fatal_error or any(not bool(row.get("passed")) for row in rows):
        return "scenario_acceptance"
    return ""


def _model_summary(
    *,
    model: str,
    rows: list[dict[str, object]],
    run: dict[str, object],
    fatal_error: str,
    pricing_at: datetime | None = None,
) -> dict[str, object]:
    effective_fatal_error = str(
        fatal_error or run.get("abort_error") or ""
    )
    preparation = dict(dict(run.get("preparation") or {}).get("provider") or {})
    route = dict(run.get("route_provider_total") or {})
    usage = _sum_usage(preparation, route)
    executed = [row for row in rows if not bool(row.get("skipped"))]
    required = [row for row in executed if bool(row.get("required"))]
    optional = [row for row in executed if not bool(row.get("required"))]
    checks = dict(run.get("checks") or {})
    prep = dict(run.get("preparation") or {})
    prep_diagnostics = dict(prep.get("diagnostics") or {})
    prep_model_reviewed = bool(prep_diagnostics.get("model_reviewed"))
    latencies = [int(row.get("http_wall_ms") or 0) for row in executed]
    hard_gate = bool(
        not effective_fatal_error
        and prep_model_reviewed
        and required
        and all(bool(row.get("passed")) for row in required)
        and all(bool(row.get("passed")) for row in optional)
        and checks
        and all(bool(value) for value in checks.values())
    )
    estimated_cost = _cost_cny(model, usage, pricing_at=pricing_at)
    return {
        "model": model,
        "hard_gate_passed": hard_gate,
        "failure_category": _failure_category(
            fatal_error=effective_fatal_error,
            rows=rows,
            run=run,
        ),
        "fatal_error": effective_fatal_error,
        "session_prep_model_reviewed": prep_model_reviewed,
        "session_prep_status": dict(prep.get("status") or {}),
        "session_prep_diagnostics": prep_diagnostics,
        "scenarios_executed": len(executed),
        "scenarios_passed": sum(bool(row.get("passed")) for row in executed),
        "required_passed": bool(required)
        and all(bool(row.get("passed")) for row in required),
        "optional_passed": all(bool(row.get("passed")) for row in optional),
        "p50_http_wall_ms": int(statistics.median(latencies)) if latencies else 0,
        "max_http_wall_ms": max(latencies, default=0),
        "usage": usage,
        "estimated_cost": estimated_cost,
        "pricing_window": estimated_cost["pricing_window"],
        "pricing_rates_per_million_tokens": estimated_cost[
            "rates_per_million_tokens"
        ],
        "scenario_latency": _scenario_latency_summary(rows),
        "run_checks": checks,
    }


def _ranking(summaries: Iterable[dict[str, object]]) -> list[str]:
    return [
        str(item.get("model") or "")
        for item in sorted(
            summaries,
            key=lambda item: (
                not bool(item.get("hard_gate_passed")),
                -int(item.get("scenarios_passed") or 0),
                int(dict(item.get("usage") or {}).get("failed_attempts") or 0),
                int(item.get("p50_http_wall_ms") or 0),
            ),
        )
    ]


def _write_secure(path: Path, value: object, *, jsonl: bool = False) -> None:
    if jsonl:
        with path.open("w", encoding="utf-8") as handle:
            for row in value if isinstance(value, list) else []:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    else:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    os.chmod(path, 0o600)


def _secret_scan(paths: Iterable[Path], secrets: Iterable[str]) -> dict[str, object]:
    secret_values = [str(value or "").strip() for value in secrets if str(value or "").strip()]
    forbidden_literals = ("Authorization:", "Bearer ", "api-key:")
    findings: list[dict[str, str]] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for marker in forbidden_literals:
            if marker in text:
                findings.append({"file": path.name, "marker": marker})
        for value in secret_values:
            if value in text:
                findings.append({"file": path.name, "marker": "configured_secret"})
    return {
        "checked_files": [path.name for path in paths],
        "forbidden_marker_count": len(findings),
        "passed": not findings,
    }


def _configure_process() -> None:
    os.environ.update(
        {
            "FU_GM_DOTENV_PATH": "/dev/null",
            "FU_GM_TOOL_AGENT_TIMEOUT_SECONDS": "300",
            "FU_GM_TOOL_AGENT_MAX_TOKENS": "2500",
            "FU_GM_CORE_GM_TIMEOUT_SECONDS": "300",
            "FU_GM_CORE_GM_ENDPOINT_ATTEMPT_TIMEOUT_SECONDS": "90",
            "FU_GM_PUBLIC_EXPRESSION_MODE": "core",
            "FU_GM_EXPRESSOR_RULE_RESULT_PROSE_ENABLED": "0",
            "FU_GM_DEEPSEEK_ROLEPLAY_MODE": "default",
            "FU_GM_NPC_VOICE_AUDIT_MODE": "off",
            "FU_GM_NPC_BLUEPRINT_MAX_WORKERS": "1",
            "FU_GM_NPC_BLUEPRINT_BACKGROUND_DEFER_SECONDS": "20",
            "FU_GM_IMAGE_ENABLED": "0",
            "FU_GM_CAPABILITY_ROUTING_MODE": "intent",
            "FU_GM_STATE_CONTEXT_MODE": "summary_delta",
        }
    )


def main() -> int:
    os.umask(0o077)
    args = parse_args()
    if args.prewarm_timeout <= 0 or args.background_timeout <= 0:
        raise ValueError("timeouts must be positive")
    _configure_process()
    needs_mimo = any(str(model).startswith("mimo-") for model in args.models)
    mimo_key = _mimo_api_key() if needs_mimo else ""
    configs = provider_configs(
        models=args.models,
        deepseek_dotenv=args.deepseek_dotenv,
        mimo_api_key=mimo_key,
    )
    deepseek_keys = [
        config.api_key for config in configs if config.action_model == "deepseek-v4-flash"
    ]

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = args.output_root / f"fu_gm_model_matrix_{stamp}"
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(output_dir, 0o700)
    manifest_path = output_dir / "manifest.json"
    runs_path = output_dir / "runs.jsonl"
    comparison_path = output_dir / "comparison.json"
    security_path = output_dir / "secret_scan.json"

    production_before = _production_snapshot()
    all_rows: list[dict[str, object]] = []
    summaries: list[dict[str, object]] = []
    for index, config in enumerate(configs, start=1):
        model = config.action_model
        pricing_at = datetime.now(timezone.utc)
        rows: list[dict[str, object]] = []
        run: dict[str, object] = {}
        fatal_error = ""
        print(f"[{index}/{len(configs)}] model={model} starting", flush=True)
        try:
            run = _run_smoke(
                config=config,
                args=SimpleNamespace(
                    seed=int(args.seed),
                    prewarm_timeout=float(args.prewarm_timeout),
                    background_timeout=float(args.background_timeout),
                    capability_routing_mode="intent",
                    state_context_mode="summary_delta",
                    skip_npc_dialogue=bool(args.skip_npc_dialogue),
                    skip_session_close=bool(args.skip_session_close),
                    allow_degraded_prewarm=True,
                ),
                scenario_rows=rows,
            )
            fatal_error = str(run.get("abort_error") or "")
        except Exception as exc:
            fatal_error = f"{type(exc).__name__}: {' '.join(str(exc).split())}"[:800]
        for row in rows:
            all_rows.append({"model": model, **row})
        summary = _model_summary(
            model=model,
            rows=rows,
            run=run,
            fatal_error=fatal_error,
            pricing_at=pricing_at,
        )
        summaries.append(summary)
        print(
            f"[{index}/{len(configs)}] model={model} "
            f"hard_gate={summary['hard_gate_passed']} "
            f"scenarios={summary['scenarios_passed']}/{summary['scenarios_executed']} "
            f"category={summary['failure_category'] or 'none'}",
            flush=True,
        )

    production_after = _production_snapshot()
    production_is_unchanged = _production_unchanged(
        production_before,
        production_after,
    )
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_kind": "single_pass_representative_fu_gm_screen",
        "model_order": [config.action_model for config in configs],
        "rules_seed": int(args.seed),
        "thinking": "disabled",
        "top_p": "provider_default_0.95_for_mimo",
        "temperature_policy": "existing_fu_gm_component_temperatures",
        "core_max_output_tokens": 2500,
        "session_prep_output_budget": "existing_component_contract",
        "pricing_basis": "model_run_start_time",
        "pricing_timezone": "Asia/Shanghai",
        "capability_routing_mode": "intent",
        "state_context_mode": "summary_delta",
        "production_mutation_attempted": False,
    }
    comparison = {
        **manifest,
        "production_before": production_before,
        "production_after": production_after,
        "production_unchanged": production_is_unchanged,
        "models": summaries,
        "operational_ranking": _ranking(summaries),
        "ranking_scope": (
            "Hard gates, scenario completion, provider failures, then p50 latency. "
            "Narrative quality is not inferred from this operational ranking."
        ),
        "all_hard_gates_passed": bool(summaries)
        and all(bool(item.get("hard_gate_passed")) for item in summaries),
    }
    _write_secure(manifest_path, manifest)
    _write_secure(runs_path, all_rows, jsonl=True)
    _write_secure(comparison_path, comparison)
    scan = _secret_scan(
        (manifest_path, runs_path, comparison_path),
        [mimo_key, *deepseek_keys],
    )
    _write_secure(security_path, scan)
    final_scan = _secret_scan(
        (manifest_path, runs_path, comparison_path, security_path),
        [mimo_key, *deepseek_keys],
    )
    if not bool(scan.get("passed")) or not bool(final_scan.get("passed")):
        raise RuntimeError("artifact secret scan failed")
    print(
        f"comparison={comparison_path} production_unchanged={production_is_unchanged}",
        flush=True,
    )
    return 0 if bool(comparison["all_hard_gates_passed"]) and production_is_unchanged else 1


if __name__ == "__main__":
    raise SystemExit(main())
