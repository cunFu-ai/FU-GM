#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_kariba_first_session.py"


def _run_provider(
    *,
    stamp: str,
    output_root: Path,
    provider: str,
    model: str,
    env_path: Path,
    max_turns: int,
) -> dict[str, object]:
    run_root = output_root / stamp / provider
    run_root.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(RUNNER),
        "--env",
        str(env_path),
        "--provider",
        provider,
        "--model",
        model,
        "--max-turns",
        str(max_turns),
        "--output-root",
        str(output_root),
        "--stamp",
        stamp,
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    (run_root / "runner.log").write_text(
        completed.stdout,
        encoding="utf-8",
    )
    report_path = run_root / "report.json"
    report: dict[str, object] = {}
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    return {
        "provider": provider,
        "model": model,
        "exit_code": completed.returncode,
        "report_path": str(report_path),
        "log_path": str(run_root / "runner.log"),
        "report": report,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="并发运行 Luna、Terra 与 DeepSeek 的卡里巴村完整首场长测。",
    )
    parser.add_argument("--pixel-env", default=str(ROOT / ".env"))
    parser.add_argument("--deepseek-env", default=str(ROOT / ".env.deepseek"))
    parser.add_argument("--luna-model", default="gpt-5.6-luna")
    parser.add_argument("--terra-model", default="gpt-5.6-terra")
    parser.add_argument("--deepseek-model", default="deepseek-v4-flash")
    parser.add_argument("--max-turns", type=int, default=120)
    parser.add_argument(
        "--output-root",
        default=str(ROOT / "outputs" / "kariba_first_session"),
    )
    parser.add_argument(
        "--sequential",
        action="store_true",
        help="顺序运行，用于取得不受并发负载影响的延迟基线。",
    )
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = Path(args.output_root)
    specs = (
        ("luna", args.luna_model, Path(args.pixel_env)),
        ("terra", args.terra_model, Path(args.pixel_env)),
        ("deepseek-v4-flash", args.deepseek_model, Path(args.deepseek_env)),
    )

    def launch(spec: tuple[str, str, Path]) -> dict[str, object]:
        provider, model, env_path = spec
        return _run_provider(
            stamp=stamp,
            output_root=output_root,
            provider=provider,
            model=model,
            env_path=env_path,
            max_turns=args.max_turns,
        )

    if args.sequential:
        runs = [launch(spec) for spec in specs]
    else:
        with ThreadPoolExecutor(max_workers=len(specs)) as executor:
            futures = {executor.submit(launch, spec): spec for spec in specs}
            runs = [future.result() for future in as_completed(futures)]
        order = {provider: index for index, (provider, _, _) in enumerate(specs)}
        runs.sort(key=lambda item: order[str(item["provider"])])

    summaries: list[dict[str, object]] = []
    for run in runs:
        report = dict(run.pop("report") or {})
        assertions = dict(report.get("assertions") or {})
        prompt_cache = dict(
            dict(report.get("llm_telemetry") or {}).get("prompt_cache") or {}
        )
        summaries.append(
            {
                **run,
                "passed": bool(report.get("passed")),
                "turn_count": int(report.get("turn_count") or 0),
                "assertions_passed": sum(bool(value) for value in assertions.values()),
                "assertions_total": len(assertions),
                "latency": dict(report.get("latency") or {}),
                "stalled_reason": str(report.get("stalled_reason") or ""),
                "outcome_branch": str(report.get("outcome_branch") or ""),
                "prompt_cache": prompt_cache,
            }
        )

    matrix = {
        "stamp": stamp,
        "mode": "sequential" if args.sequential else "parallel",
        "latency_note": (
            "顺序运行，可作为延迟基线。"
            if args.sequential
            else "三个模型并发运行；延迟包含共享网络与供应商并发负载，不宜直接当作单请求基线。"
        ),
        "runs": summaries,
    }
    matrix_path = output_root / stamp / "matrix.json"
    matrix_path.write_text(
        json.dumps(matrix, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({**matrix, "matrix_path": str(matrix_path)}, ensure_ascii=False, indent=2))
    return 0 if all(int(run["exit_code"]) == 0 for run in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
