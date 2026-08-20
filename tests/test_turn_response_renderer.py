from fu_gm.components.turn_response_renderer import TurnResponseRenderer
from fu_gm.models import Action, ActionResolution, ActionType, ClockChange
from fu_gm.turn_pipeline import (
    TurnReplyContext,
    TurnReplyPipeline,
    TurnReplyStage,
)


class RecordingExpressor:
    def __init__(self) -> None:
        self.calls = 0

    def render(self, _resolution: ActionResolution) -> str:
        self.calls += 1
        return "通用表达"


def test_prepared_public_reply_bypasses_general_expressor_and_keeps_clock_state() -> None:
    expressor = RecordingExpressor()
    resolution = ActionResolution(
        action=Action(
            ActionType.NARRATE,
            {"player_facing_reply": "守门人说：‘今晚可以借路。’"},
        ),
        rules_text="",
        payload={
            "turn_auto_advanced": True,
            "auto_clock_changes": [
                ClockChange(
                    clock_name="巡逻队逼近",
                    before=1,
                    after=2,
                    max_segments=6,
                    delta=1,
                )
            ],
            "clock_progress": ["【巡逻队逼近】2/6"],
        },
    )

    rendered = TurnResponseRenderer().render(resolution, expressor=expressor)

    assert expressor.calls == 0
    assert rendered == "守门人说：‘今晚可以借路。’\n【巡逻队逼近】2/6"


def test_unprepared_resolution_uses_general_expressor() -> None:
    expressor = RecordingExpressor()
    resolution = ActionResolution(
        action=Action(ActionType.NARRATE, {"summary": "继续。"}),
        rules_text="",
        payload={},
    )

    assert TurnResponseRenderer().render(resolution, expressor=expressor) == "通用表达"
    assert expressor.calls == 1


def test_held_action_notice_has_one_public_author() -> None:
    notice = "@南星，轮到【赛璃】了；刚才缓存的是：目标：伊莉雅。要改动作就直接说新的动作。"

    class LegacyExpressor:
        def render(self, _resolution: ActionResolution) -> str:
            return f"洛岚的妨碍行动成功。\n{notice}"

    resolution = ActionResolution(
        action=Action(ActionType.HINDER, {"actor": "洛岚"}),
        rules_text="",
        payload={"held_action_notice": notice},
    )

    rendered = TurnResponseRenderer().render(resolution, expressor=LegacyExpressor())

    assert rendered.count(notice) == 1


def test_held_action_notice_is_not_duplicated_after_clock_refresh() -> None:
    notice = "@南星，轮到【赛璃】了；刚才缓存的是：目标：伊莉雅。要改动作就直接说新的动作。"

    class LegacyExpressor:
        def render(self, _resolution: ActionResolution) -> str:
            return f"【巡逻队逼近】2/6\n{notice}"

    resolution = ActionResolution(
        action=Action(ActionType.HINDER, {"actor": "洛岚"}),
        rules_text="",
        payload={
            "clock_status_refresh": True,
            "clock_progress": ["【巡逻队逼近】2/6"],
            "held_action_notice": notice,
        },
    )

    rendered = TurnResponseRenderer().render(resolution, expressor=LegacyExpressor())

    assert rendered.count(notice) == 1


def test_prepared_reply_announces_completed_clock_consequence_once() -> None:
    expressor = RecordingExpressor()
    resolution = ActionResolution(
        action=Action(ActionType.NARRATE, {"player_facing_reply": "会长说：‘先关门。’"}),
        rules_text="",
        payload={
            "turn_auto_advanced": True,
            "auto_clock_changes": [
                ClockChange(
                    clock_name="巡逻队逼近",
                    before=5,
                    after=6,
                    max_segments=6,
                    delta=1,
                    clock_type="threat",
                    completion_consequence="填满后财团巡逻队包围驿站",
                )
            ],
        },
    )

    rendered = TurnResponseRenderer().render(resolution, expressor=expressor)

    assert rendered == "会长说：‘先关门。’\n【巡逻队逼近】6/6\n财团巡逻队包围驿站。"
    assert expressor.calls == 0


def test_reply_pipeline_has_observable_order_and_cannot_mutate_resolution() -> None:
    resolution = ActionResolution(
        action=Action(ActionType.NARRATE, {"summary": "现场变化"}),
        rules_text="规则保持不变",
        payload={"counter": 1},
    )
    pipeline = TurnReplyPipeline(
        [
            TurnReplyStage("trim", lambda reply, _resolution, _context: reply.strip()),
            TurnReplyStage("punctuate", lambda reply, _resolution, _context: reply + "。"),
        ]
    )

    reply, changed = pipeline.run(
        " 现场变化 ",
        resolution,
        TurnReplyContext(recent_chat="玩家：我推开门。"),
    )

    assert reply == "现场变化。"
    assert changed == ["trim", "punctuate"]
    assert resolution.rules_text == "规则保持不变"
    assert resolution.payload == {"counter": 1}


def test_public_text_helpers_insert_before_clock_state_without_duplication() -> None:
    reply = "巡守仍在犹豫。\n【巡逻队逼近】2/6"

    assert TurnResponseRenderer.contains_public_text(reply, "巡守仍在犹豫")
    assert (
        TurnResponseRenderer.insert_before_public_state(reply, "他还没有接过路牌。")
        == "巡守仍在犹豫。\n他还没有接过路牌。\n【巡逻队逼近】2/6"
    )
