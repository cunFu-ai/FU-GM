from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.session_resource_tracker import SessionResourceTracker
from fu_gm.models import Character, SessionEpisodeProgress


def _hero() -> Character:
    return Character(
        name="伊莉雅",
        attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
        max_hp=50,
        hp=50,
        max_mp=40,
        mp=40,
        max_inventory_points=6,
        inventory_points=6,
        fabula_points=3,
        traits=["pc"],
    )


def test_committed_resource_decreases_accumulate_session_pressure() -> None:
    characters = CharacterManager()
    hero = _hero()
    characters.add(hero)
    hero = characters.get(hero.name)
    tracker = SessionResourceTracker(characters)
    progress = SessionEpisodeProgress(session_number=1)
    tracker.begin(progress)

    hero.hp -= 10
    hero.mp -= 20
    tracker.observe(progress)

    assert progress.resource_spend_events == 2
    assert progress.resource_pressure_ratio == 0.7


def test_resource_snapshot_survives_tracker_recreation_and_ignores_recovery() -> None:
    characters = CharacterManager()
    hero = _hero()
    characters.add(hero)
    hero = characters.get(hero.name)
    progress = SessionEpisodeProgress(session_number=1)
    first = SessionResourceTracker(characters)
    first.begin(progress)
    hero.mp -= 10
    first.observe(progress)

    recreated = SessionResourceTracker(characters)
    hero.mp += 5
    recreated.observe(progress)
    hero.inventory_points -= 2
    recreated.observe(progress)

    assert progress.resource_spend_events == 2
    assert progress.resource_snapshot["伊莉雅"]["inventory_points"] == 4
    assert progress.resource_pressure_ratio > 0.5


def test_immediate_events_keep_spend_even_when_same_action_recovers_it() -> None:
    characters = CharacterManager()
    hero = _hero()
    characters.add(hero)
    hero = characters.get(hero.name)
    tracker = SessionResourceTracker(characters)
    progress = SessionEpisodeProgress(session_number=1)
    tracker.begin(progress)

    hero.mp = 30
    tracker.record_change(
        progress,
        character_name=hero.name,
        field_name="mp",
        before=40,
        after=30,
    )
    hero.mp = 40
    tracker.record_change(
        progress,
        character_name=hero.name,
        field_name="mp",
        before=30,
        after=40,
    )

    assert progress.resource_spend_events == 1
    assert progress.resource_pressure_ratio == 0.25
