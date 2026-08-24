from __future__ import annotations

import unittest

from fu_gm.components.decision_window_manager import DecisionWindowManager
from fu_gm.components.world_state import WorldState


class FixedTimeDecisionWindowManager(DecisionWindowManager):
    @staticmethod
    def _now() -> str:
        return "2026-08-20T00:00:00+00:00"


class DecisionWindowManagerTests(unittest.TestCase):
    def test_equal_timestamps_keep_rule_creation_order(self) -> None:
        manager = FixedTimeDecisionWindowManager(WorldState())
        manager.create(
            kind="skill_parameter",
            owner="伊莉雅",
            prompt="先处理",
            payload={"skill": "死战不退", "label": "死战不退"},
        )
        manager.create(
            kind="skill_parameter",
            owner="伊莉雅",
            prompt="后处理",
            payload={"skill": "鹰眼", "label": "鹰眼"},
        )

        self.assertEqual(
            [window.payload["skill"] for window in manager.pending()],
            ["死战不退", "鹰眼"],
        )
        self.assertEqual(
            [item["label"] for item in manager.public_summary()],
            ["死战不退", "鹰眼"],
        )


if __name__ == "__main__":
    unittest.main()
