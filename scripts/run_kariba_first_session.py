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

from fu_gm.http_server import FUGMHttpService  # noqa: E402
from fu_gm.testing.kariba_first_session import (  # noqa: E402
    KaribaFirstSessionRunner,
)
from fu_gm.testing.model_benchmark import (  # noqa: E402
    _provider_environment,
    load_provider_from_dotenv,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="运行一整场状态驱动的卡里巴村越狱真人化长测。",
    )
    parser.add_argument("--env", default=str(ROOT / ".env"))
    parser.add_argument("--provider", default="luna")
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument(
        "--endpoint",
        choices=("primary", "backup"),
        default="primary",
        help="选择 .env 中的主端点或备用端点；不会输出 API Key。",
    )
    parser.add_argument("--max-turns", type=int, default=90)
    parser.add_argument(
        "--client-recovery-retries",
        type=int,
        default=5,
        help="单次GM调用在主备端点间允许的有界恢复次数。",
    )
    parser.add_argument(
        "--provider-retry-limit",
        type=int,
        default=3,
        help="客户端恢复耗尽后，整条未提交玩家消息最多重发几次。",
    )
    parser.add_argument(
        "--provider-retry-delay",
        type=float,
        default=30.0,
        help="重发未提交玩家消息前等待的秒数。",
    )
    parser.add_argument(
        "--endpoint-attempt-timeout",
        type=float,
        default=60.0,
        help=(
            "单个模型端点一次尝试的最长秒数；只影响长测环境，"
            "不会修改生产配置。"
        ),
    )
    parser.add_argument(
        "--core-endpoint-attempt-timeout",
        type=float,
        default=90.0,
        help="核心GM代理单个端点一次尝试的最长秒数。",
    )
    parser.add_argument(
        "--rules-seed",
        type=int,
        default=0,
        help="固定规则骰种子，保证不同模型经历相同骰运；不用于生产服务。",
    )
    parser.add_argument(
        "--output-root",
        default=str(ROOT / "outputs" / "kariba_first_session"),
    )
    parser.add_argument(
        "--stamp",
        default="",
        help="可选的共享运行批次标识，供并发模型矩阵使用。",
    )
    args = parser.parse_args()

    provider = load_provider_from_dotenv(
        args.env,
        name=args.provider,
        model=args.model,
        base_url_key=(
            "FU_GM_BACKUP_API_BASE_URL"
            if args.endpoint == "backup"
            else "FU_GM_API_BASE_URL"
        ),
    )
    stamp = args.stamp.strip() or datetime.now().strftime("%Y%m%d_%H%M%S")
    run_root = Path(args.output_root) / stamp / args.provider
    campaign_id = f"kariba_first_session_{args.provider}_{stamp}"
    with _provider_environment(
        provider,
        include_backups=True,
        core_recovery_max_retries=args.client_recovery_retries,
        endpoint_attempt_timeout_seconds=args.endpoint_attempt_timeout,
        core_endpoint_attempt_timeout_seconds=(
            args.core_endpoint_attempt_timeout
        ),
    ):
        service = FUGMHttpService(
            data_root=run_root / "campaigns",
            use_llm=True,
            rules_seed=args.rules_seed,
        )
        runner = KaribaFirstSessionRunner(
            service,
            provider=provider,
            output_root=run_root,
            campaign_id=campaign_id,
            max_turns=args.max_turns,
            provider_retry_limit=args.provider_retry_limit,
            provider_retry_delay_seconds=args.provider_retry_delay,
        )
        result = runner.run()

    summary = {
        "ok": True,
        "passed": result["passed"],
        "provider": result["provider"],
        "model": result["model"],
        "endpoint_host": result["endpoint_host"],
        "turn_count": result["turn_count"],
        "scene_names": result["scene_names"],
        "assertions": result["assertions"],
        "latency": result["latency"],
        "prompt_cache": dict(
            dict(result.get("llm_telemetry") or {}).get("prompt_cache") or {}
        ),
        "provider_recovery": dict(result.get("provider_recovery") or {}),
        "report": str(run_root / "report.json"),
        "conversation": str(run_root / "conversation.txt"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if bool(result["passed"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
