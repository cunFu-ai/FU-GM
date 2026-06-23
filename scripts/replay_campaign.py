from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from fu_gm.testing.replay_models import ReplayScenario  # noqa: E402
from fu_gm.testing.replay_runner import HumanLikeReplayRunner  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a human-like FU-GM replay scenario.")
    parser.add_argument("--scenario", required=True, help="Path to a replay scenario JSON file.")
    parser.add_argument(
        "--mode",
        choices=["offline", "real_api"],
        default="offline",
        help="offline uses heuristic GM; real_api uses configured FU-GM LLM components.",
    )
    parser.add_argument(
        "--player-mode",
        choices=["heuristic", "llm"],
        default="heuristic",
        help="Whether synthetic players use templates or an LLM constrained by legal actions.",
    )
    parser.add_argument(
        "--output-root",
        default=str(PROJECT_ROOT / ".runtime" / "replay_tests"),
        help="Directory where replay artifacts will be written.",
    )
    args = parser.parse_args()

    scenario = ReplayScenario.load(args.scenario)
    runner = HumanLikeReplayRunner(
        scenario,
        output_root=args.output_root,
        use_llm_gm=args.mode == "real_api",
        use_llm_player=args.player_mode == "llm",
    )
    result = runner.run()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
