from __future__ import annotations

from types import SimpleNamespace

from fu_gm.testing.luna_player_agent import PlayerPersona
from fu_gm.testing.natural_session_zero import (
    NaturalSessionZeroLoopPolicy,
    build_natural_session_zero_status,
)


class FakeSessionZeroManager:
    def __init__(self) -> None:
        self.state = SimpleNamespace(stage=SimpleNamespace(value="heroes"))
        self.missing = ["每位玩家的 Session 0 贡献", "角色创建缺项"]

    def progress_summary(self) -> dict[str, bool]:
        return {"world_shape": True, "heroes": False}

    def contribution_roster(self) -> list[dict[str, object]]:
        return [
            {
                "player": "甲",
                "missing_topics": [
                    {"code": "mystery", "label": "世界奥秘"},
                ],
            },
            {"player": "乙", "missing_topics": []},
        ]

    def hero_creation_status(self) -> dict[str, object]:
        return {
            "ready": False,
            "missing_by_player": {
                "甲英雄": ["职业技能"],
                "乙": ["装备"],
            },
        }

    def missing_topics(self) -> list[str]:
        return list(self.missing)


def _personas() -> dict[str, PlayerPersona]:
    return {
        "甲": PlayerPersona("甲", "甲英雄", "", ""),
        "乙": PlayerPersona("乙", "乙英雄", "", ""),
    }


def test_status_projects_only_each_players_own_missing_items() -> None:
    manager = FakeSessionZeroManager()
    status = build_natural_session_zero_status(manager, _personas())
    action_bar = status.action_bar()

    assert status.stage == "heroes"
    assert status.ready is False
    assert status.missing_by_player["甲"] == ("世界奥秘",)
    assert status.hero_missing_by_player["甲"] == ("职业技能",)
    assert status.hero_missing_by_player["乙"] == ("装备",)
    assert action_bar["session_zero_missing_by_player"]["甲"] == ["世界奥秘"]


def test_loop_policy_detects_real_progress_and_then_stagnation() -> None:
    manager = FakeSessionZeroManager()
    status = build_natural_session_zero_status(manager, _personas())
    policy = NaturalSessionZeroLoopPolicy(max_waves=10, max_stagnant_waves=2)

    policy.observe(status)
    policy.observe(status)
    assert policy.failure_reason(status) == ""
    policy.observe(status)
    assert "没有结构化进展" in policy.failure_reason(status)

    manager.missing = []
    complete = build_natural_session_zero_status(manager, _personas())
    policy.observe(complete)
    assert complete.ready is True
    assert policy.stagnant_waves == 0


def test_loop_policy_restores_checkpoint_counters_and_fingerprint() -> None:
    manager = FakeSessionZeroManager()
    status = build_natural_session_zero_status(manager, _personas())
    policy = NaturalSessionZeroLoopPolicy(
        max_waves=20,
        max_stagnant_waves=5,
        initial_wave_count=11,
        initial_stagnant_waves=2,
        previous_fingerprint=status.fingerprint,
    )

    policy.observe(status)

    assert policy.wave_count == 12
    assert policy.stagnant_waves == 3


def test_loop_policy_counts_a_new_gm_handoff_once_without_masking_a_deadlock() -> None:
    manager = FakeSessionZeroManager()
    status = build_natural_session_zero_status(manager, _personas())
    policy = NaturalSessionZeroLoopPolicy(
        max_waves=20,
        max_stagnant_waves=3,
        initial_stagnant_waves=2,
        previous_fingerprint=status.fingerprint,
    )
    handoff = '{"player":"乙","stage":"character_creation","status":"targeted"}'

    policy.observe(status, coordination_fingerprint=handoff)
    assert policy.stagnant_waves == 0
    assert policy.coordination_fingerprint == handoff

    policy.observe(status, coordination_fingerprint=handoff)
    assert policy.stagnant_waves == 1
    policy.observe(status)
    policy.observe(status)
    assert "没有结构化进展" in policy.failure_reason(status)
