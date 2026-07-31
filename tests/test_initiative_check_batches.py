from __future__ import annotations

import tempfile

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.memory_store import CampaignMemoryStore
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.world_state import WorldState
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import Action, ActionType, Character


class _FixedDice:
    def __init__(self, values):
        self.values = list(values)

    def randint(self, low, high):
        value = self.values.pop(0)
        assert low <= value <= high
        return value


def _character(
    name: str,
    *,
    traits: list[str],
    identity: str = "",
    fabula_points: int = 0,
    initiative: int = 0,
) -> Character:
    return Character(
        name=name,
        attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
        max_hp=45,
        hp=45,
        max_mp=40,
        mp=40,
        fabula_points=fabula_points,
        identity=identity,
        initiative=initiative,
        defenses={"physical": 8, "magic": 8},
        traits=traits,
    )


def _interceptor(dice: list[int]) -> ActionInterceptor:
    characters = CharacterManager()
    characters.add(_character("伊莉雅", traits=["pc"]))
    characters.add(_character("赛璃", traits=["pc"]))
    characters.add(_character("财团机兵", traits=["enemy"], initiative=10))
    rules = RulesEngine()
    rules._rng = _FixedDice(dice)
    return ActionInterceptor(
        rules,
        characters,
        ClockManager(),
        ConflictManager(characters),
        WorldState(),
    )


def _start() -> Action:
    return Action(
        ActionType.START_CONFLICT,
        {
            "scene_name": "旧路伏击",
            "pcs": ["伊莉雅", "赛璃"],
            "enemies": ["财团机兵"],
            "leader": "伊莉雅",
            "supporters": ["赛璃"],
        },
    )


def test_plain_team_initiative_finishes_before_conflict_starts() -> None:
    interceptor = _interceptor([6, 5, 5, 4])

    result = interceptor.resolve(_start())

    assert interceptor.conflict_manager.state.active
    assert result.payload["players_first"]
    assert result.payload["initiative"].leader_roll.dice == [(8, 6), (8, 5)]
    assert result.payload["initiative"].support_outcomes[0].roll.dice == [
        (8, 5),
        (8, 4),
    ]
    assert interceptor.world_state.pending_check_batches == {}
    assert len(interceptor.world_state.check_batch_history) == 1


def test_leader_trait_reroll_finishes_before_supporter_rolls() -> None:
    interceptor = _interceptor([2, 3, 6, 5, 5, 5])
    leader = interceptor.character_manager.get("伊莉雅")
    leader.identity = "守住旧路的人"
    leader.fabula_points = 1

    pending = interceptor.resolve(_start())

    assert not interceptor.conflict_manager.state.active
    assert pending.payload["initiative_pending"]
    batch = next(iter(interceptor.world_state.pending_check_batches.values()))
    assert batch.rolls == {}
    trait_window = next(
        window
        for window in pending.payload["decision_windows"]
        if window["kind"] == "trait_invocation"
    )

    revised = interceptor.resolve(
        Action(
            ActionType.INVOKE_TRAIT,
            {
                "actor": "伊莉雅",
                "window_id": trait_window["window_id"],
                "trait_name": "守住旧路的人",
            },
        )
    )

    assert interceptor.conflict_manager.state.active
    assert revised.payload["players_first"]
    assert revised.payload["initiative"].leader_roll.dice == [(8, 6), (8, 5)]
    assert revised.payload["initiative"].support_outcomes[0].roll.dice == [
        (8, 5),
        (8, 5),
    ]
    assert interceptor.character_manager.get("伊莉雅").fabula_points == 0


def test_critical_opportunity_blocks_initiative_until_resolved() -> None:
    interceptor = _interceptor([6, 6, 4, 5])

    pending = interceptor.resolve(_start())

    assert not interceptor.conflict_manager.state.active
    batch = next(iter(interceptor.world_state.pending_check_batches.values()))
    assert list(batch.rolls) == ["伊莉雅"]
    opportunity = next(
        window
        for window in pending.payload["decision_windows"]
        if window["kind"] == "critical_opportunity"
    )

    resolved = interceptor.resolve(
        Action(
            ActionType.TRIGGER_OPPORTUNITY,
            {
                "actor": "伊莉雅",
                "window_id": opportunity["window_id"],
                "effect": "情报",
                "description": "发现机兵队形留下的空隙。",
            },
        )
    )

    assert interceptor.conflict_manager.state.active
    assert resolved.payload["players_first"]
    assert len(interceptor.world_state.check_batch_history) == 1


def test_supporter_reroll_preserves_the_final_leader_roll() -> None:
    interceptor = _interceptor([4, 5, 2, 3, 6, 5])
    supporter = interceptor.character_manager.get("赛璃")
    supporter.identity = "风铃塔的巡礼者"
    supporter.fabula_points = 1

    pending = interceptor.resolve(_start())

    assert not interceptor.conflict_manager.state.active
    batch = next(iter(interceptor.world_state.pending_check_batches.values()))
    assert batch.rolls["伊莉雅"].dice == [(8, 4), (8, 5)]
    assert "赛璃" not in batch.rolls
    trait_window = next(
        window
        for window in pending.payload["decision_windows"]
        if window["kind"] == "trait_invocation" and window["owner"] == "赛璃"
    )

    revised = interceptor.resolve(
        Action(
            ActionType.INVOKE_TRAIT,
            {
                "actor": "赛璃",
                "window_id": trait_window["window_id"],
                "trait_name": "风铃塔的巡礼者",
            },
        )
    )

    assert interceptor.conflict_manager.state.active
    assert revised.payload["initiative"].leader_roll.dice == [(8, 4), (8, 5)]
    assert revised.payload["initiative"].support_outcomes[0].roll.dice == [
        (8, 6),
        (8, 5),
    ]
    assert revised.payload["initiative"].final_total == 10
    assert revised.payload["players_first"]


def test_pending_initiative_survives_campaign_save_and_load() -> None:
    interceptor = _interceptor([2, 3])
    leader = interceptor.character_manager.get("伊莉雅")
    leader.identity = "守住旧路的人"
    leader.fabula_points = 1
    pending = interceptor.resolve(_start())
    trait_window = next(
        window
        for window in pending.payload["decision_windows"]
        if window["kind"] == "trait_invocation"
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        store = CampaignMemoryStore(tmpdir)
        store.save_campaign(
            "initiative-resume",
            world_state=interceptor.world_state,
            character_manager=interceptor.character_manager,
            clock_manager=interceptor.clock_manager,
            conflict_manager=interceptor.conflict_manager,
        )
        resumed = _interceptor([6, 5, 5, 5])
        store.load_campaign(
            "initiative-resume",
            world_state=resumed.world_state,
            character_manager=resumed.character_manager,
            clock_manager=resumed.clock_manager,
            conflict_manager=resumed.conflict_manager,
        )

        result = resumed.resolve(
            Action(
                ActionType.INVOKE_TRAIT,
                {
                    "actor": "伊莉雅",
                    "window_id": trait_window["window_id"],
                    "trait_name": "守住旧路的人",
                },
            )
        )

    assert resumed.conflict_manager.state.active
    assert result.payload["players_first"]
    assert resumed.world_state.pending_check_batches == {}
    assert len(resumed.world_state.check_batch_history) == 1
