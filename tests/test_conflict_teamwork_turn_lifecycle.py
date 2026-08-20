from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.models import Character, EffectTiming, TimedEffect


def _teamwork_conflict() -> tuple[ConflictManager, CharacterManager]:
    characters = CharacterManager()
    for name in ("伊莉雅", "洛岚"):
        characters.add(
            Character(
                name=name,
                attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
                max_hp=40,
                hp=40,
                max_mp=20,
                mp=20,
                traits=["pc"],
            )
        )
    conflict = ConflictManager(characters)
    conflict.start_scene("风铃廊冲突", ["伊莉雅", "洛岚"])
    conflict.begin_current_turn()
    return conflict, characters


def test_team_assist_immediately_runs_supporter_turn_lifecycle_once() -> None:
    conflict, characters = _teamwork_conflict()
    conflict.apply_guard("洛岚")
    conflict.register_effect(
        TimedEffect(
            owner="洛岚",
            effect_type="owner_turn_end_marker",
            expires_on=EffectTiming.OWNER_TURN_END,
        )
    )
    characters.get("洛岚").trigger_cooldowns.add("npc:interpose:used")
    listener_events: list[tuple[str, int, bool, bool]] = []

    def observe_turn_start(actor: str, serial: int) -> None:
        listener_events.append(
            (
                actor,
                serial,
                characters.get("洛岚").guarding,
                any(
                    effect.owner == "洛岚"
                    and effect.effect_type == "owner_turn_end_marker"
                    for effect in conflict.state.active_effects
                ),
            )
        )

    conflict.register_turn_start_listener(observe_turn_start)
    serial_before = conflict.state.turn_serial

    assert conflict.register_team_assist("洛岚", "伊莉雅", reason="稳住盾阵")

    assert conflict.state.turn_started_actor == "伊莉雅"
    assert conflict.state.turn_serial == serial_before + 1
    assert listener_events == [("洛岚", serial_before + 1, False, True)]
    assert not characters.get("洛岚").guarding
    assert "npc:interpose:used" not in characters.get("洛岚").trigger_cooldowns
    assert not any(effect.owner == "洛岚" for effect in conflict.state.active_effects)
    assert "洛岚" in conflict.state.acted_this_round

    # Finishing the leader's turn skips the supporter's already-consumed slot.
    # Its owner-turn effects and listener must not run a second time there.
    assert conflict.next_turn() == "伊莉雅"
    assert [event[0] for event in listener_events] == ["洛岚"]
    conflict.begin_current_turn()
    assert [event[0] for event in listener_events].count("洛岚") == 1


def test_pending_assist_is_consumed_only_when_a_real_check_enters() -> None:
    conflict, _ = _teamwork_conflict()
    assert conflict.register_team_assist("洛岚", "伊莉雅")

    # Guard/Equip and other actions without a check may inspect this path, but
    # must neither receive helpers nor destroy the pending assistance.
    assert conflict.consume_pending_assists("伊莉雅", check_entered=False) == []
    assert conflict.state.pending_assists == {"伊莉雅": ["洛岚"]}

    assert conflict.consume_pending_assists("伊莉雅", check_entered=True) == ["洛岚"]
    assert conflict.state.pending_assists == {}
    assert conflict.consume_pending_assists("伊莉雅", check_entered=True) == []
