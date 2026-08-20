from dataclasses import asdict
from types import SimpleNamespace

from fu_gm.testing.kariba_first_session import (
    KaribaFirstSessionDirector,
    KaribaFirstSessionRunner,
    KaribaSessionBeat,
    KaribaSessionTurn,
)


def test_first_session_agenda_is_long_and_contains_real_table_rhythm() -> None:
    director = KaribaFirstSessionDirector()

    player_beats = [beat for beat in director.beats if beat.kind == "player"]
    idle_beats = [beat for beat in director.beats if beat.kind == "idle"]
    discussion_beats = [
        beat for beat in player_beats if beat.expectation == "silent"
    ]

    assert len(player_beats) >= 30
    assert len(idle_beats) >= 3
    assert len(discussion_beats) >= 4
    assert director.beats[0].reply_to_gm is True
    assert director.beats[-1].beat_id == "end-session"


def test_player_agenda_does_not_use_conditional_action_placeholders() -> None:
    director = KaribaFirstSessionDirector()
    player_text = "\n".join(
        beat.text for beat in director.beats if beat.kind == "player"
    )

    assert "如果" not in player_text
    assert "若对方" not in player_text
    assert "若失败" not in player_text
    move_beat = next(
        beat for beat in director.beats if beat.beat_id == "move-to-duty-room"
    )
    assert "两人" not in move_beat.text
    assert all(
        "带队" not in beat.text
        for beat in director.beats
        if beat.beat_id in {"move-to-duty-room", "inspect-transfer-cart"}
    )


def test_window_followups_close_core_check_lifecycle() -> None:
    cases = {
        "check_roll_confirmation": "投。",
        "critical_opportunity": "目标是【艾丽妮】",
        "opportunity_parameter": "目标是【艾丽妮】",
        "npc_fate": "留他一命",
    }

    for kind, expected in cases.items():
        window = SimpleNamespace(
            kind=kind,
            owner="艾丽妮",
            options=[],
        )
        assert expected in KaribaFirstSessionRunner._window_reply_text(window)

    hidden_trait_window = SimpleNamespace(
        kind="trait_invocation",
        owner="艾丽妮",
        options=[{"trait": "被放逐的学徒"}],
    )
    assert KaribaFirstSessionRunner._window_reply_text(hidden_trait_window) == ""


def test_corrected_tool_failure_is_not_an_unrecovered_run_failure() -> None:
    runner = object.__new__(KaribaFirstSessionRunner)
    runner.turns = [
        KaribaSessionTurn(
            index=1,
            beat_id="move",
            kind="player",
            speaker="loading",
            message="艾丽妮前往值班室。",
            expectation="reply",
            status=200,
            elapsed_ms=1,
            target="gm",
            route="gm_agent",
            send_reply=True,
            reply="艾丽妮抵达值班室。",
            receipts=[
                {
                    "tool_name": "declare_check_action",
                    "ok": False,
                    "error_code": "OBJECTIVE_CLOCK_NOT_FOUND",
                },
                {
                    "tool_name": "declare_check_action",
                    "ok": True,
                    "state_changed": True,
                },
            ],
        )
    ]

    recovered, unrecovered = runner._classify_failed_receipts()

    assert len(recovered) == 1
    assert not unrecovered


def test_turn_report_preserves_complete_agent_trace() -> None:
    turn = KaribaSessionTurn(
        index=1,
        beat_id="trace",
        kind="player",
        speaker="loading",
        message="艾丽妮观察牢门。",
        expectation="reply",
        status=200,
        elapsed_ms=1,
        target="gm",
        route="gm_agent",
        send_reply=True,
        reply="需要检定。",
        agent_trace=[
            {"action": "discover_capabilities"},
            {"action": "call_tool", "tool_name": "declare_check_action"},
        ],
    )

    assert asdict(turn)["agent_trace"] == turn.agent_trace


def test_specialized_write_recovers_retryable_generic_tool_failure() -> None:
    runner = object.__new__(KaribaFirstSessionRunner)
    source_event = {"event_id": "event-ritual-1"}
    runner.turns = [
        KaribaSessionTurn(
            index=1,
            beat_id="ritual",
            kind="player",
            speaker="loading",
            message="艾丽妮启动仪式。",
            expectation="reply",
            status=200,
            elapsed_ms=1,
            target="gm",
            route="gm_agent",
            send_reply=True,
            reply="仪式完成。",
            receipts=[
                {
                    "tool_name": "perform_character_action",
                    "ok": False,
                    "retryable": True,
                    "error_code": "PASSIVE_SKILL_IS_NOT_ACTION",
                    "result": {"source_event": source_event},
                },
                {
                    "tool_name": "perform_ritual_project_action",
                    "ok": True,
                    "state_changed": True,
                    "result": {"source_event": source_event},
                },
            ],
        )
    ]

    recovered, unrecovered = runner._classify_failed_receipts()

    assert len(recovered) == 1
    assert not unrecovered


def test_aftermath_director_moves_missing_hero_to_existing_safe_location() -> None:
    director = KaribaFirstSessionDirector()
    locations = {
        "诺艾尔": "卡里巴村监狱·服务出口外的村内雨巷",
        "艾丽妮": "卡里巴村监狱后巷·转运设施一侧检修通道",
    }
    scene_manager = SimpleNamespace(
        current_scene=SimpleNamespace(location=locations["艾丽妮"]),
        location_of=lambda name: locations[name],
        locations_overlap=lambda left, right: left == right,
    )
    runtime = SimpleNamespace(
        app=SimpleNamespace(
            scene_manager=scene_manager,
            scene_frame_manager=SimpleNamespace(
                current_frame=SimpleNamespace(session_opportunity_role="")
            ),
        )
    )
    beat = next(
        item for item in director.beats if item.beat_id == "pc-aftermath"
    )

    adapted = director._adapt_to_authoritative_state(
        beat,
        runtime,
        turns=[],
        conflict_seen=False,
    )

    assert adapted is not None
    assert adapted.speaker == "loading"
    assert "卡里巴村监狱·服务出口外的村内雨巷" in adapted.text
    assert "实际前往" in adapted.text


def test_service_exit_outside_counts_as_aftermath_but_interior_does_not() -> None:
    assert KaribaFirstSessionDirector._safe_aftermath_location(
        "卡里巴村监狱·服务出口外的村内雨巷"
    )
    assert not KaribaFirstSessionDirector._safe_aftermath_location(
        "卡里巴村监狱·转运设施一侧的检修通道"
    )


def test_director_uses_success_receipt_to_recognize_a_located_route() -> None:
    source = KaribaSessionTurn(
        index=1,
        beat_id="locate-lower-prison-1",
        kind="player",
        speaker="loading",
        message="艾丽妮寻找下层入口。",
        expectation="reply",
        status=200,
        elapsed_ms=1,
        target="gm",
        route="gm_agent",
        send_reply=True,
        reply="需要检定。",
    )
    resolution = KaribaSessionTurn(
        index=2,
        beat_id="window-check-roll",
        kind="window",
        speaker="loading",
        message="投。",
        expectation="reply",
        status=200,
        elapsed_ms=1,
        target="gm",
        route="gm_agent",
        send_reply=True,
        reply="成功，入口就在维护门后。",
        receipts=[
            {
                "tool_name": "resolve_rule_window",
                "ok": True,
                "result": {
                    "source_event": {"text": "艾丽妮寻找下层入口。"},
                    "check_receipt": {"success": True},
                },
            }
        ],
    )

    assert KaribaFirstSessionDirector._successful_check_from_beat(
        [source, resolution],
        "locate-lower-prison-",
    )


def test_blocked_equipment_without_present_npc_does_not_invent_one() -> None:
    director = KaribaFirstSessionDirector()
    app = SimpleNamespace(
        character_manager=SimpleNamespace(
            all=lambda: [SimpleNamespace(name="诺艾尔", traits=["pc"])]
        ),
        scene_manager=SimpleNamespace(
            current_scene=SimpleNamespace(participants=["诺艾尔"])
        ),
    )

    beat = director._adapt_blocked_equipment(
        app,
        {"诺艾尔": {"细剑"}},
        phase="test",
    )

    assert "先留下" in beat.text
    assert "负责这里的人" not in beat.text
    assert director.abandoned_equipment["诺艾尔"] == {"细剑"}


def test_uncorrected_tool_failure_remains_a_run_failure() -> None:
    runner = object.__new__(KaribaFirstSessionRunner)
    runner.turns = [
        KaribaSessionTurn(
            index=1,
            beat_id="heartbeat",
            kind="heartbeat",
            speaker="时悠",
            message="<桌面自然停顿>",
            expectation="gm_beat",
            status=200,
            elapsed_ms=1,
            target="gm",
            route="gm_agent",
            send_reply=False,
            reply="",
            receipts=[
                {
                    "tool_name": "commit_scene_response",
                    "ok": False,
                    "error_code": "SOURCE_EVENT_NOT_AVAILABLE",
                }
            ],
        )
    ]

    recovered, unrecovered = runner._classify_failed_receipts()

    assert not recovered
    assert len(unrecovered) == 1


def test_rolled_back_tool_failure_is_recovered_by_next_transaction_retry() -> None:
    runner = object.__new__(KaribaFirstSessionRunner)
    runner.turns = [
        KaribaSessionTurn(
            index=1,
            beat_id="pc-aftermath",
            kind="player",
            speaker="loading",
            message="艾丽妮走到诺艾尔身边。",
            expectation="silent",
            status=200,
            elapsed_ms=1,
            target="gm",
            route="gm_agent_message_transaction_rolled_back",
            send_reply=False,
            reply="",
            receipts=[
                {
                    "tool_name": "discover_capabilities",
                    "ok": False,
                    "error_code": "PLAYER_CHARACTER_NOT_NPC",
                }
            ],
        ),
        KaribaSessionTurn(
            index=2,
            beat_id="pc-aftermath-retry-1",
            kind="player",
            speaker="loading",
            message="艾丽妮走到诺艾尔身边。",
            expectation="silent",
            status=200,
            elapsed_ms=1,
            target="gm",
            route="gm_agent_silent_commit",
            send_reply=False,
            reply="",
            receipts=[
                {
                    "tool_name": "perform_in_scene_action",
                    "ok": True,
                    "state_changed": True,
                    "result": {"silent_commit_allowed": True},
                }
            ],
        ),
    ]

    recovered, unrecovered = runner._classify_failed_receipts()

    assert recovered[0]["recovered_by_turn"] == 2
    assert not unrecovered


def test_provider_error_is_recovered_by_successful_provider_retry() -> None:
    runner = object.__new__(KaribaFirstSessionRunner)
    runner.turns = [
        KaribaSessionTurn(
            index=1,
            beat_id="observe",
            kind="player",
            speaker="loading",
            message="艾丽妮观察符文。",
            expectation="reply",
            status=200,
            elapsed_ms=1,
            target="silent",
            route="gm_agent_unavailable_silent",
            send_reply=False,
            reply="",
            agent_error="LLM HTTP 429",
        ),
        KaribaSessionTurn(
            index=2,
            beat_id="observe-provider-retry-1",
            kind="player",
            speaker="loading",
            message="艾丽妮观察符文。",
            expectation="reply",
            status=200,
            elapsed_ms=1,
            target="gm",
            route="gm_agent_tool",
            send_reply=True,
            reply="需要检定。",
        ),
    ]

    recovered, unrecovered = runner._classify_agent_errors()

    assert recovered[0]["recovered_by_turn"] == 2
    assert not unrecovered


def test_silent_commit_receipt_is_not_a_dialogue_write_violation() -> None:
    assert KaribaFirstSessionRunner._receipt_allows_silent_commit(
        {
            "ok": True,
            "state_changed": True,
            "result": {"silent_commit_allowed": True},
        }
    )
    assert not KaribaFirstSessionRunner._receipt_allows_silent_commit(
        {"ok": True, "state_changed": True, "result": {}}
    )


def test_fail_closed_tool_rejection_is_recovered_by_safe_silence() -> None:
    runner = object.__new__(KaribaFirstSessionRunner)
    runner.turns = [
        KaribaSessionTurn(
            index=1,
            beat_id="pc-discussion",
            kind="player",
            speaker="loading",
            message="艾丽妮问诺艾尔怎么看。",
            expectation="silent",
            status=200,
            elapsed_ms=1,
            target="gm",
            route="gm_agent_silent",
            send_reply=False,
            reply="",
            receipts=[
                {
                    "tool_name": "discover_capabilities",
                    "ok": False,
                    "retryable": True,
                    "error_code": "PLAYER_CHARACTER_NOT_NPC",
                }
            ],
        )
    ]

    recovered, unrecovered = runner._classify_failed_receipts()

    assert recovered[0]["recovered_by_terminal_route"] == "gm_agent_silent"
    assert not unrecovered


def test_rolled_back_message_is_retried_with_original_authorization() -> None:
    runner = object.__new__(KaribaFirstSessionRunner)
    runner.turns = [
        KaribaSessionTurn(
            index=1,
            beat_id="consent",
            kind="player",
            speaker="测试玩家甲",
            message="嗯，进入第一章吧。",
            expectation="reply",
            status=200,
            elapsed_ms=1,
            target="fu_gm",
            route="gm_agent_message_transaction_rolled_back",
            send_reply=True,
            reply="麻烦再说一次。",
        )
    ]
    original = KaribaFirstSessionDirector().beats[0]
    runner.sent_beats = {1: original}
    runner.transaction_retry_attempts = {}
    runner.director = KaribaFirstSessionDirector()

    retry = runner._rolled_back_message_retry()

    assert retry is not None
    assert retry.text == original.text
    assert retry.reply_to_gm is True
    assert retry.quoted_text == original.quoted_text


def test_provider_unavailable_player_message_gets_bounded_delayed_auditable_retries() -> None:
    runner = object.__new__(KaribaFirstSessionRunner)
    runner.turns = [
        KaribaSessionTurn(
            index=1,
            beat_id="consent",
            kind="player",
            speaker="测试玩家甲",
            message="嗯，进入第一章吧。",
            expectation="reply",
            status=200,
            elapsed_ms=1,
            target="silent",
            route="gm_agent_unavailable_silent",
            send_reply=False,
            reply="",
        )
    ]
    original = KaribaFirstSessionDirector().beats[0]
    runner.sent_beats = {1: original}
    runner.provider_retry_attempts = {}
    runner.provider_retry_events = []
    runner.provider_retry_limit = 2
    runner.provider_retry_delay_seconds = 30.0
    sleeps: list[float] = []
    runner._provider_retry_sleep = sleeps.append
    runner.director = KaribaFirstSessionDirector()

    retry = runner._provider_unavailable_message_retry()

    assert retry is not None
    assert retry.beat_id == "consent-provider-retry-1"
    assert retry.text == original.text
    assert retry.reply_to_gm is True
    assert sleeps == [30.0]
    assert runner.provider_retry_events == [
        {
            "beat_id": "consent",
            "attempt": 1,
            "delay_seconds": 30.0,
            "source_turn": 1,
            "agent_error": "",
        }
    ]

    runner.turns.append(
        KaribaSessionTurn(
            index=2,
            beat_id=retry.beat_id,
            kind="player",
            speaker=retry.speaker,
            message=retry.text,
            expectation="reply",
            status=200,
            elapsed_ms=1,
            target="silent",
            route="gm_agent_unavailable_silent",
            send_reply=False,
            reply="",
        )
    )
    runner.sent_beats[2] = retry

    second_retry = runner._provider_unavailable_message_retry()
    assert second_retry is not None
    assert second_retry.beat_id == "consent-provider-retry-2"
    assert sleeps == [30.0, 30.0]

    runner.turns.append(
        KaribaSessionTurn(
            index=3,
            beat_id=second_retry.beat_id,
            kind="player",
            speaker=second_retry.speaker,
            message=second_retry.text,
            expectation="reply",
            status=200,
            elapsed_ms=1,
            target="silent",
            route="gm_agent_unavailable_silent",
            send_reply=False,
            reply="",
        )
    )
    runner.sent_beats[3] = second_retry

    assert runner._provider_unavailable_message_retry() is None
    assert "连续3次" in runner.director.stalled_reason


def test_provider_retry_keeps_logical_source_but_uses_new_delivery_id() -> None:
    runner = object.__new__(KaribaFirstSessionRunner)
    runner.turns = [SimpleNamespace()]
    runner.sent_beats = {}
    runner.campaign_id = "campaign-1"
    runner.session_id = "session-1"
    runner.channel_id = "group-1"
    captured: list[dict[str, object]] = []
    runner._invoke = lambda **kwargs: captured.append(kwargs)

    runner._send_message(
        KaribaSessionBeat(
            beat_id="observe-provider-retry-1",
            speaker="loading",
            text="艾丽妮观察符文。",
            addressed=True,
        )
    )

    payload = captured[0]["payload"]
    assert payload["message_id"] == "kariba-session-2"
    assert payload["logical_source_event_id"] == (
        "kariba:campaign-1:session-1:observe"
    )
    assert payload["retry_attempt"] == 1
    assert payload["retry_reason"] == "provider_unavailable"


def test_longrun_answers_explicit_escape_clarification_before_next_agenda_beat() -> None:
    runner = object.__new__(KaribaFirstSessionRunner)
    runner.director = KaribaFirstSessionDirector()
    runner.answered_gm_request_turns = set()
    runner.turns = [
        KaribaSessionTurn(
            index=1,
            beat_id="escape-back-route",
            kind="player",
            speaker="测试玩家甲",
            message="诺艾尔开始撤离。",
            expectation="reply",
            status=200,
            elapsed_ms=1,
            target="fu_gm",
            route="gm_agent_tool",
            send_reply=True,
            reply=(
                "排水旧道通向村西祭祀堂底下。请明确要前往的具体地点，"
                "以及哪些人愿意跟随同行？"
            ),
        )
    ]
    scene_manager = SimpleNamespace(
        actors_share_movement_origin=lambda _left, _right: False,
    )
    runtime = SimpleNamespace(
        app=SimpleNamespace(
            world_state=SimpleNamespace(npc_personas={}),
            scene_manager=scene_manager,
        )
    )

    followup = runner._gm_clarification_followup(runtime)

    assert followup is not None
    assert followup.reply_to_gm is True
    assert "卡里巴村西祭祀堂地下排水旧道出口" in followup.text
    assert runner._gm_clarification_followup(runtime) is None


def test_longrun_answers_npc_request_for_a_concrete_response() -> None:
    runner = object.__new__(KaribaFirstSessionRunner)
    runner.director = KaribaFirstSessionDirector()
    runner.answered_gm_request_turns = set()
    runner.turns = [
        KaribaSessionTurn(
            index=1,
            beat_id="talk-to-guard",
            kind="player",
            speaker="测试玩家甲",
            message="诺艾尔压低武器，要求守卫让路。",
            expectation="reply",
            status=200,
            elapsed_ms=1,
            target="fu_gm",
            route="gm_agent_tool",
            send_reply=True,
            reply="守卫盯着她：你具体打算怎么接，开口谈，还是别的办法？",
        )
    ]
    runtime = SimpleNamespace(app=SimpleNamespace())

    followup = runner._gm_clarification_followup(runtime)

    assert followup is not None
    assert followup.speaker == "测试玩家甲"
    assert "封印先失控" in followup.text
    assert "你要什么条件" in followup.text
    assert followup.reply_to_gm is True


def test_safe_aftermath_rejects_locations_still_inside_prison() -> None:
    director = KaribaFirstSessionDirector()

    assert not director._safe_aftermath_location("卡里巴村监狱地下检修通道")
    assert director._safe_aftermath_location("卡里巴村监狱外·村西排水旧道出口")


def test_blocked_equipment_changes_player_approach_before_accepting_loss() -> None:
    director = KaribaFirstSessionDirector()
    blocked = {"艾丽妮": {"法杖", "魔典"}}
    app = SimpleNamespace(
        character_manager=SimpleNamespace(
            all=lambda: [
                SimpleNamespace(name="艾丽妮", traits=["pc"]),
                SimpleNamespace(name="狱卒", traits=["enemy"]),
            ]
        ),
        scene_manager=SimpleNamespace(
            current_scene=SimpleNamespace(participants=["艾丽妮", "狱卒"])
        ),
    )

    ask = director._adapt_blocked_equipment(app, blocked, phase="test")
    negotiate = director._adapt_blocked_equipment(app, blocked, phase="test")
    leave = director._adapt_blocked_equipment(app, blocked, phase="test")

    assert "要满足什么条件" in ask.text
    assert "牢号、封条和物品归属" in negotiate.text
    assert "先留下" in leave.text
    assert len({ask.text, negotiate.text, leave.text}) == 3
    assert director.abandoned_equipment["艾丽妮"] == {"法杖", "魔典"}
    assert not director.stalled_reason


def test_conflict_director_changes_tactic_when_same_turn_does_not_advance() -> None:
    director = KaribaFirstSessionDirector()
    state = SimpleNamespace(
        current_actor=lambda: "诺艾尔",
        enemy_side=["监狱守卫"],
        escaped_combatants=set(),
        surrendered_combatants=set(),
        defeated_combatants=set(),
        round_number=4,
        turn_serial=12,
    )
    characters = {
        "诺艾尔": SimpleNamespace(
            hp=30,
            equipped_main_hand="细剑",
            unavailable_equipment={"钢匕首", "细剑"},
        ),
        "监狱守卫": SimpleNamespace(
            hp=30,
            equipped_main_hand="长枪",
            unavailable_equipment={},
        ),
    }
    runtime = SimpleNamespace(
        app=SimpleNamespace(
            conflict_manager=SimpleNamespace(state=state),
            character_manager=SimpleNamespace(
                exists=lambda name: name in characters,
                get=lambda name: characters[name],
            ),
        )
    )

    escape = director.conflict_action(runtime)
    create_opening = director.conflict_action(runtime)
    retry_escape = director.conflict_action(runtime)
    defend = director.conflict_action(runtime)
    next_escape = director.conflict_action(runtime)

    assert escape is not None and "撤离监狱" in escape.text
    assert create_opening is not None and "逼出一个能走的空当" in create_opening.text
    assert retry_escape is not None and "抓住刚才制造的空当" in retry_escape.text
    assert defend is not None and "防御行动" in defend.text
    assert next_escape is not None and "撤离监狱" in next_escape.text
    assert len({escape.text, create_opening.text, retry_escape.text, defend.text}) == 4
    assert director.abandoned_equipment["诺艾尔"] == {"钢匕首", "细剑"}


def test_conflict_director_remembers_failed_escape_across_rounds() -> None:
    director = KaribaFirstSessionDirector()
    state = SimpleNamespace(
        current_actor=lambda: "艾丽妮",
        enemy_side=["监狱守卫"],
        escaped_combatants=set(),
        surrendered_combatants=set(),
        defeated_combatants=set(),
        round_number=4,
        turn_serial=12,
    )
    characters = {
        "艾丽妮": SimpleNamespace(hp=30, equipped_main_hand="法杖"),
        "监狱守卫": SimpleNamespace(hp=30, equipped_main_hand="长枪"),
    }
    runtime = SimpleNamespace(
        app=SimpleNamespace(
            conflict_manager=SimpleNamespace(state=state),
            character_manager=SimpleNamespace(
                exists=lambda name: name in characters,
                get=lambda name: characters[name],
            ),
        )
    )

    first_round = director.conflict_action(runtime)
    state.turn_serial = 19
    state.round_number = 5
    next_round = director.conflict_action(runtime)

    assert first_round is not None and "撤离监狱" in first_round.text
    assert next_round is not None and "改为妨碍" in next_round.text


def test_guard_beat_reacts_to_current_unopposed_waterway_scene() -> None:
    director = KaribaFirstSessionDirector()
    beat = next(item for item in director.beats if item.beat_id == "talk-to-guard")
    scene = SimpleNamespace(
        location="卡里巴村监狱值班室",
        participants=["艾丽妮"],
    )
    runtime = SimpleNamespace(
        app=SimpleNamespace(
            scene_manager=SimpleNamespace(current_scene=scene),
            scene_frame_manager=SimpleNamespace(
                current_frame=SimpleNamespace(session_opportunity_role="")
            ),
        )
    )
    turns = [
        KaribaSessionTurn(
            index=1,
            beat_id="idle-before-opposition",
            kind="heartbeat",
            speaker="时悠",
            message="",
            expectation="gm_beat",
            status=200,
            elapsed_ms=1,
            target="fu_gm",
            route="",
            send_reply=True,
            reply="排水格栅被顶开，露出一条直通雨水巷的狭窄水道。",
        )
    ]

    adapted = director._adapt_to_authoritative_state(
        beat,
        runtime,
        turns=turns,
        conflict_seen=True,
    )

    assert adapted is not None
    assert adapted.speaker == "loading"
    assert "狭窄水道" in adapted.text
    assert "武器" not in adapted.text


def test_conflict_director_varies_ordinary_tactics_across_turns() -> None:
    director = KaribaFirstSessionDirector()
    state = SimpleNamespace(
        current_actor=lambda: "艾丽妮",
        enemy_side=["监狱守卫"],
        escaped_combatants=set(),
        surrendered_combatants=set(),
        defeated_combatants=set(),
        round_number=1,
        turn_serial=2,
    )
    characters = {
        "艾丽妮": SimpleNamespace(hp=30, equipped_main_hand="法杖"),
        "监狱守卫": SimpleNamespace(hp=30, equipped_main_hand="长枪"),
    }
    runtime = SimpleNamespace(
        app=SimpleNamespace(
            conflict_manager=SimpleNamespace(state=state),
            character_manager=SimpleNamespace(
                exists=lambda name: name in characters,
                get=lambda name: characters[name],
            ),
        )
    )

    first = director.conflict_action(runtime)
    state.turn_serial = 5
    state.round_number = 2
    second = director.conflict_action(runtime)
    state.turn_serial = 8
    state.round_number = 3
    third = director.conflict_action(runtime)

    assert first is not None and "妨碍行动" in first.text
    assert second is not None and "防御行动" in second.text
    assert third is not None and "再次妨碍" in third.text
    assert len({first.text, second.text, third.text}) == 3


def test_runner_classifies_split_capture_without_treating_it_as_party_defeat() -> None:
    runner = object.__new__(KaribaFirstSessionRunner)
    runner.outcome_branch = ""
    runner.split_captured_heroes = []
    runner.split_escaped_heroes = []
    state = SimpleNamespace(
        fallen_pcs={"艾丽妮": "被俘：押回内牢"},
        pc_defeat_consequences={"艾丽妮": ["被俘：押回内牢"]},
        escaped_combatants={"诺艾尔"},
    )
    runtime = SimpleNamespace(
        app=SimpleNamespace(conflict_manager=SimpleNamespace(state=state))
    )

    branch = runner._detect_outcome_branch(runtime)

    assert branch == "split_capture"
    assert runner.split_captured_heroes == ["艾丽妮"]
    assert runner.split_escaped_heroes == ["诺艾尔"]


def test_runner_classifies_partial_capture_when_other_hero_remains_free() -> None:
    runner = object.__new__(KaribaFirstSessionRunner)
    runner.outcome_branch = ""
    runner.split_captured_heroes = []
    runner.split_escaped_heroes = []
    runner.partial_captured_heroes = []
    runner.partial_free_heroes = []
    state = SimpleNamespace(
        fallen_pcs={"诺艾尔": "被俘：押回内牢"},
        pc_defeat_consequences={"诺艾尔": ["被俘：押回内牢"]},
        escaped_combatants=set(),
    )
    runtime = SimpleNamespace(
        app=SimpleNamespace(conflict_manager=SimpleNamespace(state=state))
    )

    branch = runner._detect_outcome_branch(runtime)

    assert branch == "partial_capture"
    assert runner.partial_captured_heroes == ["诺艾尔"]
    assert runner.partial_free_heroes == ["艾丽妮"]


def test_split_capture_aftermath_never_moves_the_fallen_hero_as_if_free() -> None:
    runner = object.__new__(KaribaFirstSessionRunner)
    runner.split_captured_heroes = ["艾丽妮"]
    runner.split_escaped_heroes = ["诺艾尔"]
    runner.split_cursor = 0

    captured = runner._next_split_aftermath_beat()
    escaped = runner._next_split_aftermath_beat()

    assert captured is not None and captured.speaker == "loading"
    assert "恢复意识" in captured.text
    assert "撤离" not in captured.text
    assert escaped is not None and escaped.speaker == "测试玩家甲"
    assert "没有立刻折返" in escaped.text


def test_partial_capture_aftermath_preserves_free_hero_position() -> None:
    runner = object.__new__(KaribaFirstSessionRunner)
    runner.partial_captured_heroes = ["诺艾尔"]
    runner.partial_free_heroes = ["艾丽妮"]
    runner.partial_cursor = 0

    captured = runner._next_partial_aftermath_beat()
    free = runner._next_partial_aftermath_beat()

    assert captured is not None and captured.speaker == "测试玩家甲"
    assert "恢复意识" in captured.text
    assert free is not None and free.speaker == "loading"
    assert "留在自己实际所在的位置" in free.text
    assert "已经逃脱" not in free.text


def test_property_room_obstruction_changes_approach_before_leaving_gear() -> None:
    director = KaribaFirstSessionDirector()
    beat = next(item for item in director.beats if item.beat_id == "search-property")
    hero = SimpleNamespace(unavailable_equipment={"钢匕首", "细剑"})
    runtime = SimpleNamespace(
        app=SimpleNamespace(
            character_manager=SimpleNamespace(get=lambda _name: hero),
            scene_manager=SimpleNamespace(
                current_scene=SimpleNamespace(location="监狱走廊"),
                location_of=lambda _name: "监狱走廊",
            ),
            scene_frame_manager=SimpleNamespace(
                current_frame=SimpleNamespace(session_opportunity_role="")
            ),
        )
    )
    turns = [
        SimpleNamespace(
            reply="证物柜就在值班室入口后，但活铁藤和守卫封住了通路。"
        )
    ]

    move = director._adapt_to_authoritative_state(
        beat, runtime, turns=turns, conflict_seen=False
    )
    alternate = director._adapt_to_authoritative_state(
        beat, runtime, turns=turns, conflict_seen=False
    )
    negotiate = director._adapt_to_authoritative_state(
        beat, runtime, turns=turns, conflict_seen=False
    )
    force = director._adapt_to_authoritative_state(
        beat, runtime, turns=turns, conflict_seen=False
    )
    leave = director._adapt_to_authoritative_state(
        beat, runtime, turns=turns, conflict_seen=False
    )
    skipped = director._adapt_to_authoritative_state(
        beat, runtime, turns=turns, conflict_seen=False
    )

    assert move is not None and "实际前往" in move.text
    assert alternate is not None and alternate.speaker == "loading"
    assert "活铁藤与符文供能" in alternate.text
    assert negotiate is not None and "要怎样才肯放行" in negotiate.text
    assert force is not None and "强行突破" in force.text
    assert leave is not None and "先留下" in leave.text
    assert skipped is None
    assert director.abandoned_equipment["诺艾尔"] == {"钢匕首", "细剑"}


def test_unknown_property_room_changes_from_search_to_confrontation_then_leaves() -> None:
    director = KaribaFirstSessionDirector()
    beat = next(item for item in director.beats if item.beat_id == "search-property")
    hero = SimpleNamespace(unavailable_equipment={"钢匕首", "细剑"})
    runtime = SimpleNamespace(
        app=SimpleNamespace(
            character_manager=SimpleNamespace(get=lambda _name: hero),
            scene_manager=SimpleNamespace(
                current_scene=SimpleNamespace(location="监狱走廊"),
                location_of=lambda _name: "监狱走廊",
            ),
            scene_frame_manager=SimpleNamespace(
                current_frame=SimpleNamespace(session_opportunity_role="")
            ),
        )
    )
    turns = [SimpleNamespace(reply="乌诺不知道证物柜具体在哪。")]

    first = director._adapt_to_authoritative_state(
        beat, runtime, turns=turns, conflict_seen=False
    )
    second = director._adapt_to_authoritative_state(
        beat, runtime, turns=turns, conflict_seen=False
    )
    third = director._adapt_to_authoritative_state(
        beat, runtime, turns=turns, conflict_seen=False
    )
    confrontation = director._adapt_to_authoritative_state(
        beat, runtime, turns=turns, conflict_seen=False
    )
    leave = director._adapt_to_authoritative_state(
        beat, runtime, turns=turns, conflict_seen=False
    )
    skipped = director._adapt_to_authoritative_state(
        beat, runtime, turns=turns, conflict_seen=False
    )

    assert first is not None and first.beat_id.endswith("-1")
    assert second is not None and second.beat_id.endswith("-2")
    assert third is not None and "明确回应" in third.text
    assert confrontation is not None and "夺下钥匙" in confrontation.text
    assert "如果" not in confrontation.text
    assert leave is not None and "先处理越狱" in leave.text
    assert skipped is None
    assert not director.stalled_reason
    assert director.abandoned_equipment["诺艾尔"] == {"钢匕首", "细剑"}


def test_strategic_window_spends_limited_fabula_on_high_stakes_failure() -> None:
    runner = object.__new__(KaribaFirstSessionRunner)
    runner.turns = [
        SimpleNamespace(
            beat_id="enter-property-room-1",
            kind="player",
        )
    ]
    runner.fabula_rerolls_by_actor = {}
    runner.fabula_reroll_sources = set()
    character = SimpleNamespace(
        fabula_points=3,
        identity="离家出走的猫耳秘宝猎人",
        theme="野心",
        origin="托伦",
    )
    runtime = SimpleNamespace(
        app=SimpleNamespace(
            character_manager=SimpleNamespace(
                exists=lambda name: name == "诺艾尔",
                get=lambda _name: character,
            )
        )
    )
    window = SimpleNamespace(
        kind="trait_invocation",
        owner="诺艾尔",
        payload={
            "roll_success": False,
            "source_action": {
                "parameters": {"check_label": "寻找值班室通路"},
            },
        },
        options=[{"trait": "离家出走的猫耳秘宝猎人"}, {"trait": "野心"}],
    )

    first = runner._strategic_window_reply_text(runtime, window)
    second = runner._strategic_window_reply_text(runtime, window)
    third = runner._strategic_window_reply_text(runtime, window)

    assert "援用【离家出走的猫耳秘宝猎人】重掷" in first
    assert "寻找机关、路线和守卫破绽" in first
    assert second == ""
    assert third == ""
    assert runner.fabula_rerolls_by_actor["诺艾尔"] == 1


def test_strategic_window_can_reroll_a_later_independent_check() -> None:
    runner = object.__new__(KaribaFirstSessionRunner)
    runner.turns = [SimpleNamespace(beat_id="work-lock-1", kind="player")]
    runner.fabula_rerolls_by_actor = {}
    runner.fabula_reroll_sources = set()
    character = SimpleNamespace(
        fabula_points=3,
        identity="离家出走的猫耳秘宝猎人",
        theme="野心",
        origin="托伦",
    )
    runtime = SimpleNamespace(
        app=SimpleNamespace(
            character_manager=SimpleNamespace(
                exists=lambda name: name == "诺艾尔",
                get=lambda _name: character,
            )
        )
    )
    window = SimpleNamespace(
        kind="trait_invocation",
        owner="诺艾尔",
        payload={
            "roll_success": False,
            "source_action": {"parameters": {"check_label": "撬开机关锁"}},
        },
        options=[{"trait": "离家出走的猫耳秘宝猎人"}],
    )

    first = runner._strategic_window_reply_text(runtime, window)
    runner.turns.append(SimpleNamespace(beat_id="leave-cell-row-1", kind="player"))
    window.payload["source_action"]["parameters"]["check_label"] = "寻找撤离路线"
    second = runner._strategic_window_reply_text(runtime, window)

    assert first
    assert second
    assert runner.fabula_rerolls_by_actor["诺艾尔"] == 2


def test_strategic_window_does_not_reroll_successful_check() -> None:
    runner = object.__new__(KaribaFirstSessionRunner)
    runner.turns = [SimpleNamespace(beat_id="conflict-01", kind="player")]
    runner.fabula_rerolls_by_actor = {}
    runtime = SimpleNamespace(
        app=SimpleNamespace(
            character_manager=SimpleNamespace(
                exists=lambda _name: True,
                get=lambda _name: SimpleNamespace(
                    fabula_points=3,
                    identity="离家出走的猫耳秘宝猎人",
                    theme="野心",
                    origin="托伦",
                ),
            )
        )
    )
    window = SimpleNamespace(
        kind="trait_invocation",
        owner="诺艾尔",
        payload={"roll_success": True},
        options=[{"trait": "野心"}],
    )

    reply = runner._strategic_window_reply_text(runtime, window)

    assert reply == ""
    assert not runner.fabula_rerolls_by_actor


def test_first_session_includes_false_premise_probe_without_seeding_it() -> None:
    director = KaribaFirstSessionDirector()
    probe = next(
        beat for beat in director.beats if beat.beat_id == "false-premise-manor"
    )
    earlier_text = "\n".join(
        beat.text
        for beat in director.beats[: director.beats.index(probe)]
        if beat.kind == "player"
    )

    assert "谁提到了庄园" in probe.text
    assert "庄园" not in earlier_text
