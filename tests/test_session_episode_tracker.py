from __future__ import annotations

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.session_episode_tracker import SessionEpisodeTracker
from fu_gm.models import Action, ActionResolution, ActionType, Character


class RecordingPacingManager:
    def __init__(self) -> None:
        self.observations: list[dict] = []

    def observe_turn(self, **kwargs):
        self.observations.append(dict(kwargs))


def _pc(name: str) -> Character:
    return Character(
        name=name,
        attributes={"DEX": 8, "INS": 10, "MIG": 6, "WLP": 8},
        max_hp=35,
        hp=35,
        max_mp=45,
        mp=45,
        traits=["pc"],
    )


def test_committed_post_check_window_tracks_original_action_not_window_reply() -> None:
    pacing = RecordingPacingManager()
    characters = CharacterManager()
    characters.add(_pc("伊莉雅"))
    tracker = SessionEpisodeTracker(pacing, characters)  # type: ignore[arg-type]
    original = Action(
        ActionType.INVESTIGATE,
        {"actor": "伊莉雅", "target": "后门封蜡"},
    )
    resolution = ActionResolution(
        action=Action(
            ActionType.INVOKE_TRAIT,
            {"actor": "伊莉雅", "trait": "边境骑士"},
        ),
        rules_text="重掷后检定成功。",
        payload={
            "committed_source_action": original,
            "investigation_reveal": "封蜡里混着财团巡逻甲胄的辉钢粉。",
        },
    )

    tracker.turn_resolved(
        resolution,
        player_message="伊莉雅消耗一点物语点，援用边境骑士重掷。",
        public_reply="封蜡裂开，银灰色粉末沾上她的手套。",
        player_actor="伊莉雅",
    )

    observation = pacing.observations[-1]
    assert observation["player_action"] is True
    assert "后门封蜡" in observation["action_summary"]
    assert "物语点" not in observation["action_summary"]
    assert observation["reveal"] == "封蜡里混着财团巡逻甲胄的辉钢粉。"


def test_fulfilled_scene_condition_records_its_concession_as_local_payoff() -> None:
    pacing = RecordingPacingManager()
    characters = CharacterManager()
    characters.add(_pc("伊莉雅"))
    tracker = SessionEpisodeTracker(pacing, characters)  # type: ignore[arg-type]
    action = Action(
        ActionType.NARRATE,
        {
            "actor": "监察官艾蕾娜",
            "resolved_scene_condition_id": "condition-1",
            "scene_condition_promised_result": "解除入口第一层封锁，开放旧路十分钟",
        },
    )
    resolution = ActionResolution(
        action=action,
        rules_text="艾蕾娜解除入口封锁。",
        payload={},
    )

    tracker.turn_resolved(
        resolution,
        public_reply="艾蕾娜收起权杖，入口的辉钢锁熄灭了。",
    )

    observation = pacing.observations[-1]
    assert observation["local_payoff"] == "解除入口第一层封锁，开放旧路十分钟"


def test_completed_accepted_exchange_records_settled_terms_as_local_payoff() -> None:
    pacing = RecordingPacingManager()
    tracker = SessionEpisodeTracker(pacing, CharacterManager())  # type: ignore[arg-type]
    action = Action(
        ActionType.NARRATE,
        {
            "settled_exchange_outcome": "accepted",
            "settled_exchange_player_performance": "complete",
            "settled_exchange_terms": "使者接受去路片段，并开放通往旧钟仓的侧门",
        },
    )

    tracker.turn_resolved(ActionResolution(action=action, rules_text="", payload={}))

    assert pacing.observations[-1]["local_payoff"] == "使者接受去路片段，并开放通往旧钟仓的侧门"


def test_pending_accepted_exchange_is_not_a_local_payoff() -> None:
    pacing = RecordingPacingManager()
    tracker = SessionEpisodeTracker(pacing, CharacterManager())  # type: ignore[arg-type]
    action = Action(
        ActionType.NARRATE,
        {
            "settled_exchange_outcome": "accepted",
            "settled_exchange_player_performance": "pending",
            "settled_exchange_terms": "英雄之后再交出去路片段",
        },
    )

    tracker.turn_resolved(ActionResolution(action=action, rules_text="", payload={}))

    assert pacing.observations[-1]["local_payoff"] == ""
