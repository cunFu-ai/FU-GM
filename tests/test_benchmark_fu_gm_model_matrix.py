from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark_fu_gm_model_matrix.py"
SPEC = importlib.util.spec_from_file_location("benchmark_fu_gm_model_matrix", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_mimo_config_is_non_thinking_and_uses_no_client_cache_extensions() -> None:
    config = MODULE._mimo_config("mimo-v2.5", "test-secret")

    assert config.api_base_url == "https://api.xiaomimimo.com/v1"
    assert config.thinking_enabled is False
    assert config.response_format_enabled is True
    assert config.prompt_cache_enabled is False
    assert config.prompt_cache_mode == "off"
    assert config.backup_api_base_urls == ()


def test_cost_is_conservative_when_cache_usage_is_unknown() -> None:
    result = MODULE._cost_cny(
        "mimo-v2.5",
        {
            "prompt_tokens": 1_000_000,
            "completion_tokens": 100_000,
            "cached_tokens": 900_000,
            "cache_usage_reported_calls": 0,
        },
    )

    assert result["cache_accounting"] == "all_input_as_cache_miss"
    assert result["estimate"] == 1.2
    assert result["pricing_window"] == "flat"
    assert result["rates_per_million_tokens"] == {
        "hit": 0.02,
        "miss": 1.0,
        "output": 2.0,
    }


def test_deepseek_pricing_uses_shanghai_peak_windows() -> None:
    morning_peak = datetime(2026, 8, 20, 1, 30, tzinfo=timezone.utc)
    midday_off_peak = datetime(2026, 8, 20, 4, 0, tzinfo=timezone.utc)
    afternoon_peak = datetime(2026, 8, 20, 6, 0, tzinfo=timezone.utc)
    evening_off_peak = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)

    assert MODULE._pricing_for_model(
        "deepseek-v4-flash", at=morning_peak
    )["pricing_window"] == "peak_09_12_or_14_18"
    assert MODULE._pricing_for_model(
        "deepseek-v4-flash", at=midday_off_peak
    )["pricing_window"] == "off_peak"
    assert MODULE._pricing_for_model(
        "deepseek-v4-flash", at=afternoon_peak
    )["pricing_window"] == "peak_09_12_or_14_18"
    assert MODULE._pricing_for_model(
        "deepseek-v4-flash", at=evening_off_peak
    )["pricing_window"] == "off_peak"


def test_deepseek_cost_reports_selected_window_and_current_rates() -> None:
    usage = {
        "prompt_tokens": 1_000_000,
        "completion_tokens": 100_000,
        "cached_tokens": 0,
        "cache_usage_reported_calls": 0,
    }
    peak = MODULE._cost_cny(
        "deepseek-v4-flash",
        usage,
        pricing_at=datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc),
    )
    off_peak = MODULE._cost_cny(
        "deepseek-v4-flash",
        usage,
        pricing_at=datetime(2026, 8, 20, 4, 0, tzinfo=timezone.utc),
    )

    assert peak["estimate"] == 3.9
    assert peak["rates_per_million_tokens"] == {
        "hit": 0.10,
        "miss": 3.0,
        "output": 9.0,
    }
    assert off_peak["estimate"] == 1.95
    assert off_peak["rates_per_million_tokens"] == {
        "hit": 0.05,
        "miss": 1.5,
        "output": 4.5,
    }


def test_operational_ranking_keeps_hard_gate_ahead_of_fast_failure() -> None:
    ranked = MODULE._ranking(
        [
            {
                "model": "fast-but-failed",
                "hard_gate_passed": False,
                "scenarios_passed": 5,
                "p50_http_wall_ms": 10,
                "usage": {"failed_attempts": 0},
            },
            {
                "model": "slow-but-correct",
                "hard_gate_passed": True,
                "scenarios_passed": 5,
                "p50_http_wall_ms": 2000,
                "usage": {"failed_attempts": 0},
            },
        ]
    )

    assert ranked == ["slow-but-correct", "fast-but-failed"]


def test_secret_scan_detects_exact_key_without_echoing_it(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.json"
    artifact.write_text('{"safe": true}\n', encoding="utf-8")
    assert MODULE._secret_scan((artifact,), ("test-secret",))["passed"] is True

    artifact.write_text('{"value": "test-secret"}\n', encoding="utf-8")
    scan = MODULE._secret_scan((artifact,), ("test-secret",))
    assert scan["passed"] is False
    assert scan["forbidden_marker_count"] == 1


def test_failure_category_distinguishes_receipt_failure() -> None:
    category = MODULE._failure_category(
        fatal_error="",
        rows=[
            {
                "receipts": [
                    {"tool_name": "perform_character_action", "ok": False}
                ],
                "response": {},
                "provider": {},
                "passed": False,
            }
        ],
        run={},
    )

    assert category == "python_receipt_or_transaction"


def test_failure_category_reads_nested_scenario_provider_failures() -> None:
    row = {
        "receipts": [],
        "response": {},
        "provider": {
            "client": {"usage": {"failed_attempts": 1}}
        },
        "passed": False,
    }

    assert MODULE._scenario_failed_attempts(row) == 1
    assert (
        MODULE._failure_category(
            fatal_error="required scenario failed: ordinary_fact",
            rows=[row],
            run={},
        )
        == "provider_or_network"
    )


def test_aborted_model_summary_keeps_usage_and_failure_cause() -> None:
    run = {
        "abort_error": "required scenario failed: ordinary_fact",
        "preparation": {
            "diagnostics": {"model_reviewed": True},
            "provider": {
                "call_count": 2,
                "prompt_tokens": 1000,
                "completion_tokens": 50,
                "cache_usage_reported_calls": 2,
            },
        },
        "route_provider_total": {
            "call_count": 3,
            "prompt_tokens": 2000,
            "completion_tokens": 70,
            "cache_usage_reported_calls": 2,
            "failed_attempts": 1,
        },
        "checks": {"required_scenarios_completed": False},
    }
    rows = [
        {
            "kind": "ordinary_fact",
            "required": True,
            "passed": False,
            "http_wall_ms": 100,
            "receipts": [],
            "response": {},
            "provider": {
                "client": {"usage": {"failed_attempts": 1}}
            },
        }
    ]

    summary = MODULE._model_summary(
        model="mimo-v2.5",
        rows=rows,
        run=run,
        fatal_error="",
    )

    assert summary["hard_gate_passed"] is False
    assert summary["fatal_error"] == run["abort_error"]
    assert summary["failure_category"] == "provider_or_network"
    assert summary["usage"]["call_count"] == 5
    assert summary["usage"]["prompt_tokens"] == 3000
    assert summary["usage"]["completion_tokens"] == 120
    assert summary["usage"]["failed_attempts"] == 1
