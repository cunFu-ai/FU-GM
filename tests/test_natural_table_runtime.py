from __future__ import annotations

from dataclasses import replace

from fu_gm.testing.luna_player_agent import PlayerPersona
from fu_gm.testing.natural_table_runtime import NaturalTableRuntime
from fu_gm.testing.player_simulator import SimulatedUtterance
from fu_gm.testing.replay_models import LegalActionContext


class FakeNaturalAgent:
    def __init__(self, player_name: str, decisions: list[SimulatedUtterance]) -> None:
        self.player_name = player_name
        self.decisions = list(decisions)
        self.calls: list[dict[str, object]] = []
        self.model = "fake-natural-player"
        self.use_llm = True
        self.client = None
        self.delivered: list[str] = []

    def compose(self, **kwargs: object) -> SimulatedUtterance:
        self.calls.append(dict(kwargs))
        return self.decisions.pop(0)

    def telemetry_payload(self) -> dict[str, object]:
        return {"calls": len(self.calls)}

    def record_delivered(self, _speaker: str, text: str) -> None:
        self.delivered.append(text)


def _personas() -> dict[str, PlayerPersona]:
    return {
        "甲": PlayerPersona("甲", "甲英雄", "先听", "简短"),
        "乙": PlayerPersona("乙", "乙英雄", "爱商量", "温和"),
        "丙": PlayerPersona("丙", "丙英雄", "敢行动", "直接"),
    }


def _runtime(
    decisions: dict[str, list[SimulatedUtterance]],
    *,
    player_briefs: dict[str, str] | None = None,
) -> tuple[NaturalTableRuntime, dict[str, FakeNaturalAgent]]:
    agents: dict[str, FakeNaturalAgent] = {}

    def factory(persona: PlayerPersona) -> FakeNaturalAgent:
        agent = FakeNaturalAgent(
            persona.player_name,
            decisions[persona.player_name],
        )
        agents[persona.player_name] = agent
        return agent

    return (
        NaturalTableRuntime(
            personas=_personas(),
            agent_factory=factory,
            player_briefs=player_briefs,
        ),
        agents,
    )


def _context(_player: str, _hero: str) -> LegalActionContext:
    return LegalActionContext(
        stage_goal="公开局面",
        scene_name="雨夜牢区",
        current_actor="乙英雄",
        conflict_active=True,
        legal_actions=["攻击", "防御", "推进目标"],
    )


def _wait(**changes: object) -> SimulatedUtterance:
    values: dict[str, object] = {
        "text": "",
        "decision": "wait",
        "utterance_kind": "wait",
        "audience": "table",
    }
    values.update(changes)
    return SimulatedUtterance(**values)


def _speak(text: str, *, delay: int, **changes: object) -> SimulatedUtterance:
    values: dict[str, object] = {
        "text": text,
        "decision": "speak",
        "utterance_kind": "table_discussion",
        "audience": "table",
        "speak_after_ms": delay,
    }
    values.update(changes)
    return SimulatedUtterance(**values)


def test_every_player_independently_receives_event_and_transport_uses_own_delay() -> None:
    runtime, agents = _runtime(
        {
            "甲": [_speak("先听听她的条件。", delay=3600)],
            "乙": [_wait(private_mind_update={"focus": "保护旅人"})],
            "丙": [_speak("我去看门外。", delay=900, utterance_kind="action", audience="gm")],
        }
    )
    event = runtime.new_event(speaker="时悠", text="门外响起脚步声。")

    wave = runtime.react(
        event,
        context_factory=_context,
        recent_public_context="时悠：门外响起脚步声。",
        last_gm_reply="门外响起脚步声。",
    )

    assert [item.player_name for item in wave.candidates] == ["丙", "甲"]
    assert wave.waiting_players == ("乙",)
    assert wave.heartbeat_due is False
    assert set(agents) == {"甲", "乙", "丙"}
    assert all(len(agent.calls) == 1 for agent in agents.values())
    assert runtime.minds["乙"].focus == "保护旅人"
    action_bars = {
        name: agent.calls[0]["natural_table_event"]["action_bar"]
        for name, agent in agents.items()
    }
    assert action_bars["乙"]["you_are_current_actor"] is True
    assert action_bars["甲"]["you_are_current_actor"] is False


def test_equal_delays_use_transport_tie_break_and_only_delivered_draft_commits() -> None:
    runtime, agents = _runtime(
        {
            "甲": [
                _speak(
                    "我先说一个想法。",
                    delay=0,
                    private_mind_update={"commitment": "提出想法"},
                )
            ],
            "乙": [
                _speak(
                    "我也有个方向。",
                    delay=0,
                    private_mind_update={"commitment": "提出方向"},
                )
            ],
            "丙": [_wait()],
        }
    )
    event = runtime.new_event(speaker="时悠", text="你们有什么想法？")

    wave = runtime.react(
        event,
        context_factory=_context,
        recent_public_context="时悠：你们有什么想法？",
    )

    expected = sorted(
        ("甲", "乙"),
        key=lambda name: runtime._transport_tie_break(event.event_id, name),
    )
    assert [item.player_name for item in wave.candidates] == expected
    assert runtime.minds["甲"].commitment == ""
    assert runtime.minds["乙"].commitment == ""
    selected = wave.candidates[0]
    runtime.commit_candidate(selected)
    assert runtime.minds[selected.player_name].commitment
    assert agents[selected.player_name].delivered == [selected.text]
    other = next(name for name in ("甲", "乙") if name != selected.player_name)
    assert runtime.minds[other].commitment == ""
    assert agents[other].delivered == []


def test_player_observes_but_does_not_reply_to_own_message() -> None:
    runtime, agents = _runtime(
        {
            "甲": [_speak("不应被调用", delay=0)],
            "乙": [_wait()],
            "丙": [_wait()],
        }
    )
    event = runtime.new_event(speaker="甲", role="player", text="谁来守门？")

    wave = runtime.react(
        event,
        context_factory=_context,
        recent_public_context="甲：谁来守门？",
    )

    assert agents["甲"].calls == []
    assert runtime.minds["甲"].last_seen_event_id == event.event_id
    assert wave.all_wait is True
    assert wave.heartbeat_due is True


def test_only_decision_owner_receives_blocking_window() -> None:
    runtime, agents = _runtime(
        {
            "甲": [_wait()],
            "乙": [_speak("我接受原结果。", delay=0, audience="gm")],
            "丙": [_wait()],
        }
    )

    def context(player: str, hero: str) -> LegalActionContext:
        return replace(
            _context(player, hero),
            pending_decisions=[
                {"owner": "乙英雄", "kind": "trait_invocation"}
            ],
        )

    event = runtime.new_event(speaker="时悠", text="乙英雄要不要援用特质？")
    wave = runtime.react(
        event,
        context_factory=context,
        recent_public_context="时悠：乙英雄要不要援用特质？",
    )

    assert [item.player_name for item in wave.candidates] == ["乙"]
    assert agents["乙"].calls[0]["legal_context"].pending_decisions
    assert agents["甲"].calls[0]["legal_context"].pending_decisions == []
    assert (
        agents["甲"].calls[0]["natural_table_event"]["action_bar"]
        ["another_player_has_pending_decision"]
        is True
    )


def test_all_wait_requests_heartbeat_and_mind_snapshot_restores() -> None:
    runtime, _agents = _runtime(
        {"甲": [_wait()], "乙": [_wait()], "丙": [_wait()]}
    )
    runtime.minds["甲"].focus = "记住东门"
    event = runtime.new_event(speaker="时悠", text="屋里暂时安静下来。")

    wave = runtime.react(
        event,
        context_factory=_context,
        recent_public_context="时悠：屋里暂时安静下来。",
    )
    snapshot = runtime.snapshot()

    assert wave.heartbeat_due is True
    restored, _ = _runtime(
        {"甲": [_wait()], "乙": [_wait()], "丙": [_wait()]}
    )
    restored.restore(snapshot)
    assert restored.minds["甲"].focus == "记住东门"
    assert restored.minds["甲"].last_seen_event_id == event.event_id
    assert restored.snapshot()["event_counter"] == event.event_id


def test_stale_draft_is_given_back_to_original_player_for_reconsideration() -> None:
    runtime, agents = _runtime(
        {
            "甲": [_wait(), _wait()],
            "乙": [_wait(), _wait()],
            "丙": [_wait(), _wait()],
        }
    )
    stale = _speak("我去开门。", delay=5000, utterance_kind="action", audience="gm")
    stale_candidate = runtime.react(
        runtime.new_event(speaker="时悠", text="门还关着。"),
        context_factory=lambda player, hero: _context(player, hero),
        recent_public_context="时悠：门还关着。",
    )
    assert stale_candidate.all_wait

    from fu_gm.testing.natural_table_runtime import NaturalTableCandidate

    draft = NaturalTableCandidate("甲", "甲英雄", 1, stale, 0)
    newer = runtime.new_event(speaker="时悠", text="守卫主动把门打开了。")
    runtime.react(
        newer,
        context_factory=_context,
        recent_public_context="时悠：守卫主动把门打开了。",
        stale_drafts={"甲": draft},
    )

    payload = agents["甲"].calls[-1]["natural_table_event"]
    assert payload["stale_draft"]["text"] == "我去开门。"
    assert payload["text"] == "守卫主动把门打开了。"


def test_session_zero_brief_and_missing_items_are_private_to_each_player() -> None:
    runtime, agents = _runtime(
        {"甲": [_wait()], "乙": [_wait()], "丙": [_wait()]},
        player_briefs={"甲": "想做一名流亡骑士。", "乙": "想提出浮空岛。"},
    )
    event = runtime.new_event(
        speaker="时悠",
        text="大家先说说各自想玩的世界吧。",
        action_bar={
            "phase": "session_zero",
            "session_zero_missing_by_player": {
                "甲": ["王国贡献"],
                "乙": ["世界奥秘"],
                "丙": [],
            },
            "hero_missing_by_player": {
                "甲": ["身份"],
                "乙": ["技能"],
            },
        },
    )

    runtime.react(
        event,
        context_factory=lambda player, hero: replace(
            _context(player, hero),
            conflict_active=False,
            current_actor="",
        ),
        recent_public_context="时悠：大家先说说各自想玩的世界吧。",
    )

    assert agents["甲"].calls[0]["player_mind"]["private_brief"] == "想做一名流亡骑士。"
    assert agents["乙"].calls[0]["player_mind"]["private_brief"] == "想提出浮空岛。"
    assert agents["丙"].calls[0]["player_mind"]["private_brief"] == ""
    bars = {
        name: agent.calls[0]["natural_table_event"]["action_bar"]
        for name, agent in agents.items()
    }
    assert bars["甲"]["your_session_zero_missing"] == ["王国贡献"]
    assert bars["甲"]["your_hero_missing"] == ["身份"]
    assert "session_zero_missing_by_player" not in bars["甲"]
    assert bars["乙"]["your_session_zero_missing"] == ["世界奥秘"]
    assert bars["丙"]["your_hero_missing"] == []


def test_private_brief_survives_snapshot_without_becoming_mutable_mind_update() -> None:
    runtime, _ = _runtime(
        {
            "甲": [_wait(private_mind_update={"private_brief": "篡改", "mood": "期待"})],
            "乙": [_wait()],
            "丙": [_wait()],
        },
        player_briefs={"甲": "原始创作意向"},
    )
    runtime.react(
        runtime.new_event(speaker="时悠", text="开团吧。"),
        context_factory=_context,
        recent_public_context="时悠：开团吧。",
    )
    snapshot = runtime.snapshot()

    assert runtime.minds["甲"].private_brief == "原始创作意向"
    assert runtime.minds["甲"].mood == "期待"
    restored, _ = _runtime(
        {"甲": [_wait()], "乙": [_wait()], "丙": [_wait()]}
    )
    restored.restore(snapshot)
    assert restored.minds["甲"].private_brief == "原始创作意向"
