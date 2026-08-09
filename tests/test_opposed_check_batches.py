from __future__ import annotations

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
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
    identity: str = "",
    fabula_points: int = 0,
) -> Character:
    return Character(
        name=name,
        attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
        max_hp=45,
        hp=45,
        max_mp=40,
        mp=40,
        identity=identity,
        fabula_points=fabula_points,
        traits=["pc"],
    )


def _interceptor(
    left: Character,
    right: Character,
    dice: list[int],
) -> ActionInterceptor:
    characters = CharacterManager()
    characters.add(left)
    characters.add(right)
    rules = RulesEngine()
    rules._rng = _FixedDice(dice)
    return ActionInterceptor(
        rules,
        characters,
        ClockManager(),
        ConflictManager(characters),
        WorldState(),
    )


def _pvp() -> Action:
    return Action(
        ActionType.PLAYER_VS_PLAYER,
        {
            "actor": "伊莉雅",
            "target": "赛璃",
            "attributes": ["WLP", "WLP"],
            "consent_confirmed": True,
        },
    )


def test_pvp_trait_reroll_is_final_before_the_other_side_rolls() -> None:
    interceptor = _interceptor(
        _character(
            "伊莉雅",
            identity="绝不退让的骑士",
            fabula_points=1,
        ),
        _character("赛璃"),
        [2, 3, 6, 5, 4, 4],
    )

    pending = interceptor.resolve(_pvp())

    assert pending.payload["pvp_pending"]
    batch = next(iter(interceptor.world_state.pending_check_batches.values()))
    assert batch.rolls == {}
    trait_window = next(
        window
        for window in pending.payload["decision_windows"]
        if window["kind"] == "trait_invocation"
    )

    result = interceptor.resolve(
        Action(
            ActionType.INVOKE_TRAIT,
            {
                "actor": "伊莉雅",
                "window_id": trait_window["window_id"],
                "trait_name": "绝不退让的骑士",
                "invocation_rationale": "作为绝不退让的骑士，伊莉雅在这场意志对抗中不会先低头。",
            },
        )
    )

    assert result.payload["opposed_check"].winner == "伊莉雅"
    assert result.payload["opposed_check"].left_roll.dice == [(8, 6), (8, 5)]
    assert result.payload["opposed_check"].right_roll.dice == [(8, 4), (8, 4)]
    assert interceptor.world_state.pending_check_batches == {}


def test_pvp_tie_starts_a_new_audited_round() -> None:
    interceptor = _interceptor(
        _character("伊莉雅"),
        _character("赛璃"),
        [4, 4, 5, 3, 6, 4, 4, 5],
    )

    result = interceptor.resolve(_pvp())

    opposed = result.payload["opposed_check"]
    assert opposed.winner == "伊莉雅"
    assert opposed.attempts == 2
    history = interceptor.world_state.check_batch_history[0]
    assert len(history.roll_history) == 1
    assert history.roll_history[0]["伊莉雅"].total == 8
    assert history.roll_history[0]["赛璃"].total == 8
    assert history.rolls["伊莉雅"].total == 10
    assert history.rolls["赛璃"].total == 9
