from __future__ import annotations

from types import SimpleNamespace

from fu_gm.testing.luna_player_agent import PlayerPersona
from fu_gm.testing.natural_session_zero import (
    NaturalSessionZeroLoopPolicy,
    build_natural_session_zero_status,
)


class FakeSessionZeroManager:
    def __init__(self) -> None:
        self.state = SimpleNamespace(
            stage=SimpleNamespace(value="heroes"),
            world=SimpleNamespace(pending_proposals=[]),
        )
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


def test_status_projects_recent_public_proposals_without_internal_ids() -> None:
    manager = FakeSessionZeroManager()
    manager.state.world.pending_proposals = [
        {
            "id": "proposal-secret-id",
            "speaker": "甲",
            "summary": "把漂流群岛定为追逐季风的船城。",
            "world_operations": [
                {"operation": "create", "category": "map_locations"},
            ],
        }
    ]

    status = build_natural_session_zero_status(manager, _personas())
    proposal = status.action_bar()["latest_pending_proposals"][0]

    assert proposal == {
        "speaker": "甲",
        "summary": "把漂流群岛定为追逐季风的船城。",
        "categories": ["map_locations"],
    }
    assert "proposal-secret-id" not in str(proposal)


def test_pending_proposal_churn_does_not_count_as_committed_progress() -> None:
    manager = FakeSessionZeroManager()
    before = build_natural_session_zero_status(manager, _personas())

    manager.state.world.pending_proposals.append(
        {
            "speaker": "乙",
            "summary": "也许木鼓可以提醒潮汐危险。",
            "proposed_updates": {"consensus_notes": ["待讨论"]},
        }
    )
    after = build_natural_session_zero_status(manager, _personas())

    assert after.latest_pending_proposals
    assert after.fingerprint == before.fingerprint


def test_loop_policy_detects_real_progress_and_then_stagnation() -> None:
    manager = FakeSessionZeroManager()
    status = build_natural_session_zero_status(manager, _personas())
    policy = NaturalSessionZeroLoopPolicy(max_waves=10, max_stagnant_waves=2)

    policy.observe(status)
    policy.observe(status)
    assert policy.failure_reason(status) == ""
    policy.observe(status)
    assert "没有公开活动" in policy.failure_reason(status)

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
    assert policy.inactive_waves == 1


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
    next_handoff = (
        '{"player":"乙","public_handoff_wave":12,'
        '"stage":"character_creation","status":"targeted"}'
    )
    policy.observe(status, coordination_fingerprint=next_handoff)
    assert policy.stagnant_waves == 0
    policy.observe(status)
    policy.observe(status)
    policy.observe(status)
    assert "没有公开活动" in policy.failure_reason(status)


def test_active_discussion_can_stagnate_without_being_a_deadlock() -> None:
    manager = FakeSessionZeroManager()
    status = build_natural_session_zero_status(manager, _personas())
    policy = NaturalSessionZeroLoopPolicy(
        max_waves=20,
        max_stagnant_waves=2,
        previous_fingerprint=status.fingerprint,
    )

    policy.observe(status, table_activity=True)
    policy.observe(status, table_activity=True)
    policy.observe(status, table_activity=True)

    assert policy.stagnant_waves == 3
    assert policy.inactive_waves == 0
    assert policy.failure_reason(status) == ""


def test_progress_nudge_is_keyed_to_committed_status_not_player_wording() -> None:
    manager = FakeSessionZeroManager()
    status = build_natural_session_zero_status(manager, _personas())
    policy = NaturalSessionZeroLoopPolicy(
        max_waves=20,
        max_stagnant_waves=10,
        initial_stagnant_waves=3,
        previous_fingerprint=status.fingerprint,
    )

    assert policy.progress_nudge_due(
        status,
        after_stagnant_waves=3,
        last_nudge_fingerprint="",
    ) is True
    assert policy.progress_nudge_due(
        status,
        after_stagnant_waves=3,
        last_nudge_fingerprint=status.fingerprint,
    ) is False


def test_ignored_progress_nudge_retries_only_after_cooldown() -> None:
    manager = FakeSessionZeroManager()
    status = build_natural_session_zero_status(manager, _personas())
    policy = NaturalSessionZeroLoopPolicy(
        initial_wave_count=8,
        initial_stagnant_waves=8,
        previous_fingerprint=status.fingerprint,
    )

    assert policy.progress_nudge_due(
        status,
        after_stagnant_waves=3,
        last_nudge_fingerprint=status.fingerprint,
        last_nudge_wave=4,
        repeat_after_waves=6,
    ) is False

    policy.observe(status, table_activity=True)

    assert policy.progress_nudge_due(
        status,
        after_stagnant_waves=3,
        last_nudge_fingerprint=status.fingerprint,
        last_nudge_wave=4,
        repeat_after_waves=6,
    ) is True


def test_progress_rearms_nudge_for_the_new_authoritative_status() -> None:
    manager = FakeSessionZeroManager()
    old_status = build_natural_session_zero_status(manager, _personas())
    policy = NaturalSessionZeroLoopPolicy(
        initial_stagnant_waves=3,
        previous_fingerprint=old_status.fingerprint,
    )

    manager.missing = ["角色创建缺项"]
    new_status = build_natural_session_zero_status(manager, _personas())

    assert policy.progress_nudge_due(
        new_status,
        after_stagnant_waves=3,
        last_nudge_fingerprint=old_status.fingerprint,
    ) is True
