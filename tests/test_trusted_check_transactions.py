from __future__ import annotations

import tempfile

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.memory_store import CampaignMemoryStore
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.world_state import WorldState
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import Action, ActionType, Bond, Character


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
    skills: dict[str, int] | None = None,
    bonds: list[Bond] | None = None,
    mp: int = 20,
) -> Character:
    return Character(
        name=name,
        attributes={"DEX": 8, "INS": 10, "MIG": 8, "WLP": 8},
        max_hp=45,
        hp=45,
        max_mp=45,
        mp=mp,
        fabula_points=fabula_points,
        identity=identity,
        traits=["pc"],
        skills=dict(skills or {}),
        bonds=list(bonds or []),
    )


def _interceptor(*characters: Character, dice: list[int]) -> ActionInterceptor:
    manager = CharacterManager()
    for character in characters:
        manager.add(character)
    rules = RulesEngine()
    rules._rng = _FixedDice(dice)
    return ActionInterceptor(
        rules,
        manager,
        ClockManager(),
        ConflictManager(manager),
        WorldState(),
    )


def _check(actor: str) -> Action:
    return Action(
        ActionType.REQUEST_ROLL,
        {
            "actor": actor,
            "target": "封死的闸门",
            "attributes": ["DEX", "INS"],
            "target_number": 12,
            "non_damage": True,
        },
    )


def _trust_window(resolution):
    return next(
        window
        for window in resolution.payload["skill_decision_windows"]
        if window.get("skill") == "予以信任"
    )


def test_trust_replays_ally_check_and_charges_the_helper() -> None:
    target = _character(
        "伊莉雅",
        identity="谨慎的巡路者",
        fabula_points=2,
        mp=10,
    )
    helper = _character(
        "艾薇娅",
        fabula_points=2,
        skills={"予以信任": 1},
        bonds=[Bond(target="伊莉雅", emotions=["敬意"])],
    )
    interceptor = _interceptor(target, helper, dice=[2, 3, 7, 8])

    first = interceptor.resolve(_check("伊莉雅"))
    trust = _trust_window(first)
    revised = interceptor.resolve(
        Action(
            ActionType.RESOLVE_DECISION,
            {
                "actor": "艾薇娅",
                "window_id": trust["window_id"],
                "selected_option": {
                    "choice": "assist_trait",
                    "trait": "谨慎的巡路者",
                    "target": "伊莉雅",
                },
            },
        )
    )

    assert first.payload["check_result_provisional"]
    assert not first.payload["roll"].success
    assert revised.payload["roll"].success
    assert revised.payload["roll"].actor == "伊莉雅"
    assert not revised.payload.get("check_result_provisional")
    assert "伊莉雅" in interceptor.pending_check_transactions
    silent_windows = interceptor.decision_window_manager.pending(
        kind="trait_invocation",
        owner="伊莉雅",
    )
    assert len(silent_windows) == 1
    assert not silent_windows[0].blocking
    assert interceptor.character_manager.get("艾薇娅").fabula_points == 1
    assert interceptor.character_manager.get("伊莉雅").fabula_points == 2
    assert interceptor.character_manager.get("伊莉雅").mp == 20
    assert revised.payload["assisted_mp_change"].target == "伊莉雅"


def test_target_cannot_accept_while_trust_choice_is_pending() -> None:
    target = _character(
        "伊莉雅",
        identity="谨慎的巡路者",
        fabula_points=1,
    )
    helper = _character(
        "艾薇娅",
        fabula_points=1,
        skills={"予以信任": 1},
    )
    interceptor = _interceptor(target, helper, dice=[2, 3])

    first = interceptor.resolve(_check("伊莉雅"))
    trait = next(
        window
        for window in first.payload["post_check_windows"]
        if window.get("kind") == "trait_invocation"
    )

    try:
        interceptor.resolve(
            Action(
                ActionType.RESOLVE_DECISION,
                {
                    "actor": "伊莉雅",
                    "window_id": trait["window_id"],
                    "post_check_acceptance": True,
                },
            )
        )
    except ValueError as exc:
        assert "艾薇娅" in str(exc)
    else:
        raise AssertionError("pending Trust must block final acceptance")


def test_trust_choice_is_presented_before_checked_actor_reroll_choices() -> None:
    target = _character(
        "伊莉雅",
        identity="谨慎的巡路者",
        fabula_points=1,
    )
    helper = _character(
        "艾薇娅",
        fabula_points=1,
        skills={"予以信任": 1},
    )
    interceptor = _interceptor(target, helper, dice=[2, 3])

    interceptor.resolve(_check("伊莉雅"))

    waiting = interceptor.decision_window_manager.awaiting_player_response()
    summaries = interceptor.decision_window_manager.public_summary()
    assert waiting[0].owner == "艾薇娅"
    assert waiting[0].kind == "skill_parameter"
    assert waiting[0].payload["label"] == "予以信任"
    assert summaries[0]["window_id"] == waiting[0].window_id
    assert summaries[0]["response_priority"] < summaries[1]["response_priority"]


def test_declining_the_only_trust_window_commits_the_original_roll() -> None:
    target = _character(
        "伊莉雅",
        identity="谨慎的巡路者",
        fabula_points=0,
    )
    helper = _character(
        "艾薇娅",
        fabula_points=1,
        skills={"予以信任": 1},
    )
    interceptor = _interceptor(target, helper, dice=[2, 3])

    first = interceptor.resolve(_check("伊莉雅"))
    trust = _trust_window(first)
    declined = interceptor.resolve(
        Action(
            ActionType.RESOLVE_DECISION,
            {
                "actor": "艾薇娅",
                "window_id": trust["window_id"],
                "selected_option": {"choice": "decline"},
            },
        )
    )

    assert first.payload["check_result_provisional"]
    assert not declined.payload.get("check_result_provisional")
    assert declined.payload["roll"].dice == [(8, 2), (10, 3)]
    assert interceptor.character_manager.get("艾薇娅").fabula_points == 1
    assert "伊莉雅" not in interceptor.pending_check_transactions


def test_trust_window_resumes_the_ally_check_after_save_and_load() -> None:
    target = _character(
        "伊莉雅",
        identity="谨慎的巡路者",
        fabula_points=0,
    )
    helper = _character(
        "艾薇娅",
        fabula_points=1,
        skills={"予以信任": 1},
    )
    interceptor = _interceptor(target, helper, dice=[2, 3])
    first = interceptor.resolve(_check("伊莉雅"))
    trust = _trust_window(first)

    with tempfile.TemporaryDirectory() as tmpdir:
        store = CampaignMemoryStore(tmpdir)
        store.save_campaign(
            "trust-resume",
            world_state=interceptor.world_state,
            character_manager=interceptor.character_manager,
            clock_manager=interceptor.clock_manager,
            conflict_manager=interceptor.conflict_manager,
        )
        resumed = _interceptor(
            _character("伊莉雅"),
            _character("艾薇娅"),
            dice=[7, 8],
        )
        store.load_campaign(
            "trust-resume",
            world_state=resumed.world_state,
            character_manager=resumed.character_manager,
            clock_manager=resumed.clock_manager,
            conflict_manager=resumed.conflict_manager,
        )

        result = resumed.resolve(
            Action(
                ActionType.RESOLVE_DECISION,
                {
                    "actor": "艾薇娅",
                    "window_id": trust["window_id"],
                    "selected_option": {
                        "choice": "assist_trait",
                        "trait": "谨慎的巡路者",
                        "target": "伊莉雅",
                    },
                },
            )
        )

    assert result.payload["roll"].actor == "伊莉雅"
    assert result.payload["roll"].success
    assert resumed.character_manager.get("艾薇娅").fabula_points == 0
