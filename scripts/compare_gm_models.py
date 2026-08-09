#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fu_gm.testing.model_benchmark import (  # noqa: E402
    compare_probe_results,
    load_provider_from_dotenv,
    run_kariba_provider_probe,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="在同一卡里巴村开章状态上比较两个 FU-GM 核心模型。",
    )
    parser.add_argument("--luna-env", default=str(ROOT / ".env"))
    parser.add_argument(
        "--deepseek-env",
        default=str(ROOT / ".env.deepseek"),
    )
    parser.add_argument("--luna-model", default="gpt-5.6-luna")
    parser.add_argument("--deepseek-model", default="deepseek-v4-flash")
    parser.add_argument("--only", choices=("luna", "deepseek", "both"), default="both")
    parser.add_argument("--output-root", default=str(ROOT / "outputs" / "model_comparison"))
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_root = Path(args.output_root) / stamp
    run_root.mkdir(parents=True, exist_ok=True)
    specs = []
    if args.only in {"luna", "both"}:
        specs.append(
            load_provider_from_dotenv(
                args.luna_env,
                name="luna",
                model=args.luna_model,
            )
        )
    if args.only in {"deepseek", "both"}:
        specs.append(
            load_provider_from_dotenv(
                args.deepseek_env,
                name="deepseek-v4-flash",
                model=args.deepseek_model,
            )
        )

    results = [
        run_kariba_provider_probe(spec, output_root=run_root)
        for spec in specs
    ]
    report = compare_probe_results(results)
    report_path = run_root / "comparison.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({
        "ok": bool(results),
        "report": str(report_path),
        "ranking": report["ranking"],
        "providers": [
            {
                "provider": item["provider"],
                "model": item["model"],
                "endpoint_host": item["endpoint_host"],
                "quality_score": item["quality_score"],
                "behavior_quality_score": item["behavior_quality_score"],
                "infrastructure_score": item["infrastructure_score"],
                "end_to_end_score": item["end_to_end_score"],
                "provider_available": item["provider_available"],
                "availability_error": item["availability_error"],
                "p50_latency_ms": item["p50_latency_ms"],
            }
            for item in results
        ],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
