import pytest

from fu_gm.components.clock_lifecycle_coordinator import ClockLifecycleCoordinator
from fu_gm.components.clock_manager import ClockManager
from fu_gm.models import Action, ActionResolution, ActionType, Clock, ClockChange


def test_completed_pressure_clock_is_archived_after_consequence_commits() -> None:
    clocks = ClockManager()
    clocks.add(
        Clock(
            name="巡逻队逼近",
            max_segments=6,
            current=6,
            clock_type="threat",
            completion_consequence="巡逻队包围驿站",
        )
    )
    resolution = ActionResolution(
        Action(ActionType.NARRATE, {}),
        "",
        {
            "clock_change": ClockChange(
                clock_name="巡逻队逼近",
                before=5,
                after=6,
                delta=1,
                max_segments=6,
                clock_type="threat",
                completion_consequence="巡逻队包围驿站",
            )
        },
    )

    settled = ClockLifecycleCoordinator(clocks).settle_resolution(resolution)

    assert settled[0]["clock_name"] == "巡逻队逼近"
    assert not clocks.exists("巡逻队逼近")
    assert clocks.archived()[0].status == "resolved"
    assert resolution.payload["world_consequence_required"] is True
    assert resolution.payload["committed_world_consequences"] == ["巡逻队包围驿站"]
    assert resolution.payload["pressure_clock_fulfilled"] is True
    assert resolution.payload["local_question_changed"] is True
    assert resolution.payload["session_reversal"] is True


def test_direct_clock_change_settlement_uses_same_pressure_lifecycle() -> None:
    clocks = ClockManager()
    clocks.add(
        Clock(
            name="闸门崩塌",
            max_segments=4,
            current=4,
            clock_type="threat",
            completion_consequence="闸门彻底崩塌，原路已经断绝。",
        )
    )
    payload = {
        "auto_clock_changes": [
            ClockChange(
                clock_name="闸门崩塌",
                before=3,
                after=4,
                delta=1,
                max_segments=4,
                clock_type="threat",
                completion_consequence="闸门彻底崩塌，原路已经断绝。",
            )
        ]
    }

    settled = ClockLifecycleCoordinator(clocks).settle_changes(
        payload["auto_clock_changes"],
        payload=payload,
    )

    assert settled[0]["consequence"] == "闸门彻底崩塌，原路已经断绝。"
    assert payload["settled_pressure_clocks"] == settled
    assert payload["session_reversal"] is True
    assert not clocks.exists("闸门崩塌")


def test_conditional_stakes_are_not_repeated_as_unfulfilled_consequence() -> None:
    clocks = ClockManager()
    clocks.add(
        Clock(
            name="记忆集中协议",
            max_segments=6,
            current=6,
            clock_type="villain",
            stakes="填满后艾蕾娜能上传旅人的记忆。",
        )
    )
    change = ClockChange(
        clock_name="记忆集中协议",
        before=5,
        after=6,
        delta=1,
        max_segments=6,
        clock_type="villain",
        stakes="填满后艾蕾娜能上传旅人的记忆。",
    )

    settled = ClockLifecycleCoordinator(clocks).settle_changes([change])

    assert settled[0]["consequence"] == "命刻【记忆集中协议】的后果已经发生"
    assert change.completion_consequence == "命刻【记忆集中协议】的后果已经发生"


def test_full_objective_clock_resolves_immediately() -> None:
    clocks = ClockManager()
    clocks.add(Clock(name="打开闸门", max_segments=6, current=6, clock_type="objective"))
    resolution = ActionResolution(
        Action(ActionType.OBJECTIVE, {}),
        "",
        {
            "clock_change": ClockChange(
                clock_name="打开闸门",
                before=5,
                after=6,
                delta=1,
                max_segments=6,
                clock_type="objective",
            )
        },
    )

    settled = ClockLifecycleCoordinator(clocks).settle_resolution(resolution)

    assert settled[0]["clock_name"] == "打开闸门"
    assert not clocks.exists("打开闸门")
    assert clocks.archived()[0].status == "resolved"
    assert resolution.payload["settled_objective_clocks"][0]["clock_name"] == "打开闸门"
    assert resolution.payload["committed_world_consequences"] == ["目标已经达成"]


def test_full_ritual_clock_stays_ready_until_final_cast() -> None:
    clocks = ClockManager()
    clocks.add(Clock(name="仪式：封住裂隙", max_segments=4, clock_type="ritual"))
    clocks.advance("仪式：封住裂隙", 4)
    resolution = ActionResolution(
        Action(ActionType.CONTRIBUTE_RITUAL, {}),
        "",
        {
            "clock_change": ClockChange(
                clock_name="仪式：封住裂隙",
                before=3,
                after=4,
                delta=1,
                max_segments=4,
                clock_type="ritual",
            )
        },
    )

    assert ClockLifecycleCoordinator(clocks).settle_resolution(resolution) == []
    assert clocks.exists("仪式：封住裂隙")
    assert clocks.get("仪式：封住裂隙").status == "ready"
    assert clocks.formatted_public() == [
        "【仪式：封住裂隙】4/4。仪式的准备已经抵达临界点。"
    ]


def test_reconcile_fulfilled_archives_legacy_pressure_but_keeps_ritual_ready() -> None:
    clocks = ClockManager()
    clocks.add(
        Clock(
            name="巡逻队逼近",
            max_segments=6,
            current=6,
            clock_type="threat",
            completion_consequence="巡逻队已经包围现场。",
        )
    )
    clocks.add(
        Clock(
            name="仪式：风铃回声",
            max_segments=4,
            current=4,
            clock_type="ritual",
        )
    )
    payload: dict[str, object] = {}

    settled = ClockLifecycleCoordinator(clocks).reconcile_fulfilled(
        payload=payload
    )

    assert [item["clock_name"] for item in settled] == ["巡逻队逼近"]
    assert not clocks.exists("巡逻队逼近")
    assert clocks.get("仪式：风铃回声").status == "ready"
    assert payload["pressure_clock_fulfilled"] is True


def test_local_resolution_retires_only_unfinished_scene_clocks() -> None:
    clocks = ClockManager()
    clocks.begin_scene("scene-7")
    clocks.add(
        Clock(
            name="巡逻队逼近",
            max_segments=8,
            current=2,
            clock_type="threat",
            scope="session",
        )
    )
    clocks.add(
        Clock(
            name="打开旧路",
            max_segments=6,
            current=3,
            clock_type="objective",
            scope="scene",
            scene_id="scene-7",
        )
    )
    clocks.add(
        Clock(
            name="反派长期阴谋",
            max_segments=10,
            current=4,
            clock_type="villain",
            scope="campaign",
        )
    )

    settled = ClockLifecycleCoordinator(clocks).settle_local_resolution(
        {
            "material_change": True,
            "local_question_resolved": True,
            "commitment_level": "consequence",
            "public_fact": "旧路已经安全开放，巡逻标记也被切断。",
        },
        scene_id="scene-7",
    )

    assert {item["clock_name"] for item in settled} == {"打开旧路"}
    assert clocks.exists("巡逻队逼近")
    assert not clocks.exists("打开旧路")
    assert clocks.exists("反派长期阴谋")
    assert {clock.status for clock in clocks.archived()} == {"abandoned"}


def test_clock_add_normalizes_a_full_clock_to_terminal_ready_state() -> None:
    clocks = ClockManager()
    clocks.add(Clock(name="打开闸门", max_segments=6, current=9, clock_type="objective"))
    clocks.add(Clock(name="仪式：封印", max_segments=4, current=4, clock_type="ritual"))

    assert clocks.get("打开闸门").current == 6
    assert clocks.get("打开闸门").status == "fulfilled"
    assert clocks.get("仪式：封印").status == "ready"


def test_local_change_without_resolution_keeps_clocks_active() -> None:
    clocks = ClockManager()
    clocks.add(Clock("巡逻队逼近", 8, current=2, clock_type="threat", scope="session"))

    settled = ClockLifecycleCoordinator(clocks).settle_local_resolution(
        {
            "material_change": True,
            "local_question_resolved": False,
            "commitment_level": "action",
        }
    )

    assert settled == []
    assert clocks.exists("巡逻队逼近")


def test_resolved_clock_name_remains_a_tombstone_after_archival() -> None:
    clocks = ClockManager()
    clocks.add(Clock(name="巡逻队逼近", max_segments=6, current=6, clock_type="threat"))
    clocks.resolve("巡逻队逼近", note="巡逻队已经包围现场", archive=True)

    assert clocks.is_retired("巡逻队逼近")
    assert clocks.is_retired("【巡逻队逼近】 6/6")
    assert clocks.archived_match("[巡逻队逼近] 6/6") is not None


@pytest.mark.parametrize(
    "name",
    [
        "本轮场景意图契约，后台使用，不得原样输出",
        "scene_intent_contract visibility=private",
    ],
)
def test_private_control_plane_text_can_never_become_a_clock(name: str) -> None:
    clocks = ClockManager()

    with pytest.raises(ValueError, match="后台控制信息"):
        clocks.add(Clock(name=name, max_segments=6, clock_type="objective"))
