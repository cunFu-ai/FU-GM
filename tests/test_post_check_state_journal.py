from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.post_check_state_journal import PostCheckStateJournal
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.models import Action, ActionType, Character, Clock, ClockChange, RollOutcome


def _outcome(*, success: bool, total: int, target_number: int) -> RollOutcome:
    return RollOutcome(
        actor="伊莉雅",
        attributes=["INS", "WLP"],
        dice=[(8, 4), (8, 4)],
        total=total,
        modifier=0,
        high_roll=4,
        target_number=target_number,
        success=success,
        critical_success=False,
        fumble=False,
        margin=total - target_number,
    )


def _journal(clocks: ClockManager) -> PostCheckStateJournal:
    return PostCheckStateJournal(
        rules_engine=RulesEngine(),
        clock_manager=clocks,
        ensure_clock_exists=lambda *_args, **_kwargs: False,
    )


def _hero() -> Character:
    return Character(
        name="伊莉雅",
        attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
        max_hp=45,
        hp=45,
        max_mp=45,
        mp=45,
        traits=["pc"],
    )


def test_roll_context_and_advantage_have_separate_lifetimes() -> None:
    journal = _journal(ClockManager())
    outcome = _outcome(success=True, total=8, target_number=7)

    journal.remember_roll(outcome)
    journal.grant_advantage("伊莉雅")
    journal.clear_roll_context()

    assert journal.roll_for("伊莉雅") is None
    assert journal.consume_advantage("伊莉雅") == 4
    assert journal.consume_advantage("伊莉雅") == 0


def test_clock_reconciliation_restores_baseline_before_applying_new_result() -> None:
    clocks = ClockManager()
    clocks.add(Clock(name="打开旧路", max_segments=6, current=0, clock_type="objective"))
    journal = _journal(clocks)
    original = _outcome(success=True, total=8, target_number=7)
    action = Action(
        ActionType.OBJECTIVE,
        {"actor": "伊莉雅", "clock_name": "打开旧路", "clock_direction": 1},
    )
    clocks.advance("打开旧路", 1)
    journal.remember_clock_check(
        action,
        original,
        {
            "clock_change": ClockChange(
                clock_name="打开旧路",
                before=0,
                after=1,
                delta=1,
                max_segments=6,
            )
        },
    )

    improved = _outcome(success=True, total=13, target_number=7)
    result = journal.reconcile_clock_check(
        _hero(),
        improved,
    )

    assert result["clock_change"].before == 0
    assert result["clock_change"].after == 3
    assert clocks.get("打开旧路").current == 3


def test_explicit_clock_direction_is_preserved_for_threat_clock() -> None:
    clocks = ClockManager()
    clocks.add(Clock(name="巡逻队逼近", max_segments=6, current=3, clock_type="threat"))
    journal = _journal(clocks)
    action = Action(
        ActionType.OBJECTIVE,
        {"actor": "伊莉雅", "clock_name": "巡逻队逼近", "clock_direction": -1},
    )
    original = _outcome(success=True, total=8, target_number=7)
    clocks.advance("巡逻队逼近", -1)
    journal.remember_clock_check(
        action,
        original,
        {
            "clock_change": ClockChange(
                clock_name="巡逻队逼近",
                before=3,
                after=2,
                delta=-1,
                max_segments=6,
            )
        },
    )

    result = journal.reconcile_clock_check(
        _hero(),
        _outcome(success=True, total=8, target_number=7),
    )

    assert clocks.get("巡逻队逼近").current == 2
