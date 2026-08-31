from __future__ import annotations

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.session_episode_tracker import SessionEpisodeTracker
from fu_gm.models import Action, ActionResolution, ActionType, Character, RollOutcome


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


def test_immediate_protect_reaction_counts_as_a_committed_consequence() -> None:
    pacing = RecordingPacingManager()
    tracker = SessionEpisodeTracker(pacing, CharacterManager())  # type: ignore[arg-type]
    resolution = ActionResolution(
        action=Action(
            ActionType.SKILL,
            {"actor": "伊莉雅", "target": "禾音", "skill_name": "挺身守护"},
        ),
        rules_text="【伊莉雅】发动【挺身守护】，代替【禾音】承受眼前这次险情。",
        payload={
            "protect_reaction_triggered": True,
            "immediate_scene_protection": True,
        },
    )

    tracker.turn_resolved(resolution)

    assert pacing.observations[-1]["consequence"] == (
        "【伊莉雅】发动【挺身守护】，代替【禾音】承受眼前这次险情。"
    )


def test_persistent_spell_effect_counts_as_a_committed_consequence() -> None:
    pacing = RecordingPacingManager()
    tracker = SessionEpisodeTracker(pacing, CharacterManager())  # type: ignore[arg-type]
    resolution = ActionResolution(
        action=Action(
            ActionType.SPELL,
            {"actor": "赛璃", "target": "伊莉雅", "spell_name": "屏障"},
        ),
        rules_text="赛璃施放【屏障】，伊莉雅的物防至少为12，持续至场景结束。",
        payload={"spell_effect": {"effect_type": "defense_floor"}},
    )

    tracker.turn_resolved(resolution)

    assert pacing.observations[-1]["consequence"] == (
        "赛璃施放【屏障】，伊莉雅的物防至少为12，持续至场景结束。"
    )


def test_successful_planned_investigation_records_hidden_answer_as_reveal() -> None:
    pacing = RecordingPacingManager()
    characters = CharacterManager()
    characters.add(_pc("伊莉雅"))
    tracker = SessionEpisodeTracker(pacing, characters)  # type: ignore[arg-type]
    action = Action(
        ActionType.INVESTIGATE,
        {
            "actor": "伊莉雅",
            "target": "泥地上的足印",
            "success_observation": "足印属于三名穿辉钢重靴的巡逻兵，并朝东门延伸。",
        },
    )
    roll = RollOutcome(
        actor="伊莉雅",
        attributes=["INS", "INS"],
        dice=[(10, 7), (10, 5)],
        total=12,
        modifier=0,
        high_roll=7,
        target_number=8,
        success=True,
        critical_success=False,
        fumble=False,
    )

    tracker.turn_resolved(
        ActionResolution(action=action, rules_text="检定成功。", payload={"roll": roll})
    )

    assert pacing.observations[-1]["reveal"] == (
        "足印属于三名穿辉钢重靴的巡逻兵，并朝东门延伸。"
    )


def test_provisional_or_failed_investigation_does_not_reveal_hidden_answer() -> None:
    pacing = RecordingPacingManager()
    tracker = SessionEpisodeTracker(pacing, CharacterManager())  # type: ignore[arg-type]
    action = Action(
        ActionType.INVESTIGATE,
        {
            "actor": "伊莉雅",
            "success_observation": "封蜡来自王室密库。",
        },
    )
    failed = RollOutcome(
        actor="伊莉雅",
        attributes=["INS", "INS"],
        dice=[(8, 2), (8, 3)],
        total=5,
        modifier=0,
        high_roll=3,
        target_number=8,
        success=False,
        critical_success=False,
        fumble=False,
    )

    tracker.turn_resolved(
        ActionResolution(
            action=action,
            rules_text="暂定失败。",
            payload={"roll": failed, "check_result_provisional": True},
        )
    )

    assert pacing.observations[-1]["reveal"] == ""
