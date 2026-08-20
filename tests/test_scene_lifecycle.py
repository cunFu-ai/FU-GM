from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.scene_frame_manager import SceneFrame
from fu_gm.components.world_state import WorldState
from fu_gm.expressor import Expressor
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import (
    Action,
    ActionType,
    Character,
    Clock,
    DecisionWindowStatus,
    SceneRecord,
    SceneType,
)
from fu_gm.scene_orchestrator import SceneOrchestrator


class _Brain:
    def decide(self, panel):
        raise AssertionError("not used")


def _app():
    characters = CharacterManager()
    clocks = ClockManager()
    conflict = ConflictManager(characters)
    world = WorldState()
    return SceneOrchestrator(
        character_manager=characters,
        clock_manager=clocks,
        conflict_manager=conflict,
        world_state=world,
        interceptor=ActionInterceptor(RulesEngine(seed=0), characters, clocks, conflict, world),
        expressor=Expressor(),
        scene_manager=SceneManager(),
    )


def _pc(name: str) -> Character:
    return Character(
        name=name,
        attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
        max_hp=45,
        hp=45,
        max_mp=45,
        mp=45,
        traits=["pc"],
    )


def test_scene_end_archives_scene_clocks_but_keeps_campaign_clocks() -> None:
    app = _app()
    scene = app.start_scene("桥头", SceneType.STANDARD)
    app.clock_manager.add(Clock("守住桥头", 6, scope="scene"))
    app.clock_manager.add(Clock("帝国完成日蚀炮", 10, clock_type="villain"))

    app.end_scene("英雄撤离。")

    assert not app.clock_manager.exists("守住桥头")
    assert app.clock_manager.exists("帝国完成日蚀炮")
    archived = app.clock_manager.archived()
    assert archived[-1].scene_id == scene.scene_id
    assert archived[-1].status == "archived"


def test_scene_end_archives_provenance_brief_with_ended_frame() -> None:
    app = _app()
    scene = app.start_scene("卡里巴村监狱", SceneType.STANDARD)
    app.scene_frame_manager.current_frame = SceneFrame(
        scene_key=f"{scene.scene_id}|卡里巴村监狱",
        scene_name="卡里巴村监狱",
        source_scene_id=scene.scene_id,
        working_brief={
            "version": 1,
            "source_events": [
                {
                    "event_id": "event-1",
                    "speaker": "诺艾尔",
                    "text": "诺艾尔试着撬锁。",
                    "status": "tool_committed",
                    "tool_names": ["resolve_check"],
                }
            ],
            "committed_transactions": [
                {
                    "event_type": "action_resolution",
                    "tool_name": "resolve_check",
                    "status": "committed",
                    "source_event_id": "event-1",
                    "source_speaker": "诺艾尔",
                    "declaration": "诺艾尔试着撬锁。",
                    "outcome": "锁芯弹开。",
                    "public_facts": ["诺艾尔的牢门已经打开。"],
                }
            ],
            "fact_evidence": [
                {
                    "text": "诺艾尔的牢门已经打开。",
                    "source_event_id": "event-1",
                    "source_speaker": "诺艾尔",
                    "tool_name": "resolve_check",
                }
            ],
            "last_authoritative_outcome": "锁芯弹开。",
            "last_public_reply": "锁舌轻响一声，牢门向外松开。",
            "updated_at": "2026-08-04T00:00:00+00:00",
        },
    )

    app.end_scene("两人离开牢区。")

    archived_frame = app.scene_frame_manager.history[-1]
    assert archived_frame.source_scene_id == scene.scene_id
    assert archived_frame.working_brief["fact_evidence"][0]["text"] == (
        "诺艾尔的牢门已经打开。"
    )
    assert app.scene_frame_manager.current_frame is None


def test_session_end_only_archives_session_scope() -> None:
    clocks = ClockManager()
    clocks.add(Clock("本场追踪", 6, scope="session"))
    clocks.add(Clock("反派大计", 10, scope="campaign"))

    clocks.end_session()

    assert not clocks.exists("本场追踪")
    assert clocks.exists("反派大计")


def test_scene_end_expires_concrete_and_legacy_scene_windows() -> None:
    app = _app()
    app.start_scene("桥头", SceneType.STANDARD)
    concrete = app.decision_window_manager.create(
        kind="opportunity",
        owner="瓦莉亚",
        scope_kind="scene",
        scope_id="scene-1",
        blocking=True,
    )
    legacy = app.decision_window_manager.create(
        kind="post_check",
        owner="瓦莉亚",
        scope_kind="scene",
        scope_id="current",
        blocking=True,
    )

    app.end_scene("英雄撤离。")

    assert concrete.status == DecisionWindowStatus.EXPIRED
    assert legacy.status == DecisionWindowStatus.EXPIRED
    assert not app.decision_window_manager.has_blocking()


def test_scene_end_expires_nonblocking_conflict_windows_and_clears_conflict_once() -> None:
    app = _app()
    app.start_conflict_scene("桥头冲突", ["瓦莉亚"])
    window = app.decision_window_manager.create(
        kind="held_action",
        owner="瓦莉亚",
        scope_kind="conflict",
        scope_id="桥头冲突",
        blocking=False,
    )

    app.end_conflict_scene()

    assert window.status == DecisionWindowStatus.EXPIRED
    assert not app.conflict_manager.state.active
    assert app.scene_manager.current_scene is None


def test_ending_split_branch_preserves_other_branch_windows_and_effects() -> None:
    app = _app()
    app.character_manager.add(_pc("甲"))
    app.character_manager.add(_pc("乙"))
    first = app.start_scene(
        "甲线",
        SceneType.STANDARD,
        location="北塔",
        participants=["甲"],
    )
    first_window = app.decision_window_manager.create(
        kind="post_check",
        owner="甲",
        scope_kind="scene",
        scope_id=first.scene_id,
        blocking=True,
    )
    app.conflict_manager.apply_guard("甲")

    second, mode = app.scene_manager.focus_actor_branch(
        "乙",
        name="乙线",
        location="南塔",
    )
    assert mode == "created"
    second_window = app.decision_window_manager.create(
        kind="post_check",
        owner="乙",
        scope_kind="scene",
        scope_id=second.scene_id,
        blocking=True,
    )
    app.conflict_manager.apply_guard("乙")

    app.end_scene("乙离开南塔。")

    assert second_window.status == DecisionWindowStatus.EXPIRED
    assert first_window.status == DecisionWindowStatus.PENDING
    assert not app.character_manager.get("乙").guarding
    assert app.character_manager.get("甲").guarding
    assert {
        (effect.owner, effect.effect_type)
        for effect in app.conflict_manager.state.active_effects
    } == {("甲", "guard"), ("甲", "guard_action_used")}
    assert app.scene_manager.current_scene is first


def test_post_check_window_uses_current_scene_id() -> None:
    app = _app()
    app.character_manager.add(_pc("甲"))
    scene = app.start_scene(
        "钟楼调查",
        SceneType.STANDARD,
        participants=["甲"],
    )

    scope_kind, scope_id = app.interceptor.post_check_decisions._decision_scope()

    assert scope_kind == "scene"
    assert scope_id == scene.scene_id


def test_legacy_end_conflict_helper_cannot_discard_blocking_decision() -> None:
    app = _app()
    app.character_manager.add(_pc("瓦莉亚"))
    app.start_conflict_scene("断桥之战", ["瓦莉亚"])
    app.character_manager.get("瓦莉亚").hp = 0
    app.conflict_manager.resolve_zero_hp("瓦莉亚")

    try:
        app.end_conflict_scene()
    except ValueError as exc:
        assert "规则选择" in str(exc)
    else:
        raise AssertionError("旧冲突结束入口不应丢弃阻塞中的玩家选择。")

    assert app.conflict_manager.state.active
    assert app.scene_manager.current_scene is not None
    assert app.interceptor.decision_window_manager.find_pending(
        kind="zero_hp",
        owner="瓦莉亚",
    )


def test_pc_who_gave_up_recovers_only_when_their_next_scene_begins() -> None:
    app = _app()
    fallen = _pc("瓦莉亚")
    witness = _pc("露琪亚")
    app.character_manager.add(fallen)
    app.character_manager.add(witness)
    fallen = app.character_manager.get("瓦莉亚")
    app.start_conflict_scene("断桥之战", ["瓦莉亚", "露琪亚"])
    fallen.hp = 0
    app.conflict_manager.resolve_zero_hp("瓦莉亚")
    app.conflict_manager.resolve_pending_zero_hp(
        "瓦莉亚",
        choice="give_up_resistance",
        consequence="失散：被洪流冲到下游",
    )

    assert fallen.hp == 0
    assert app.conflict_manager.state.fallen_pcs["瓦莉亚"] == "失散：被洪流冲到下游"
    assert app.conflict_manager.state.pc_defeat_consequences["瓦莉亚"] == [
        "失散：被洪流冲到下游"
    ]

    app.end_conflict_scene()
    unrelated = app.start_scene(
        "下游搜索",
        SceneType.STANDARD,
        participants=["露琪亚"],
    )
    assert unrelated.recovered_fallen_pcs == []
    assert fallen.hp == 0
    assert "瓦莉亚" in app.conflict_manager.state.fallen_pcs

    recovered_scene = app.start_scene(
        "河岸重逢",
        SceneType.STANDARD,
        participants=["瓦莉亚", "露琪亚"],
    )

    assert recovered_scene.recovered_fallen_pcs == ["瓦莉亚"]
    assert fallen.hp == fallen.max_hp // 2
    assert "瓦莉亚" not in app.conflict_manager.state.fallen_pcs
    assert app.conflict_manager.state.pc_defeat_consequences["瓦莉亚"] == [
        "失散：被洪流冲到下游"
    ]


def test_fallen_pc_recovers_when_entering_an_existing_scene_branch() -> None:
    app = _app()
    fallen = _pc("瓦莉亚")
    witness = _pc("露琪亚")
    app.character_manager.add(fallen)
    app.character_manager.add(witness)
    app.start_conflict_scene("断桥之战", ["瓦莉亚", "露琪亚"])
    fallen.hp = 0
    app.conflict_manager.resolve_zero_hp("瓦莉亚")
    app.conflict_manager.resolve_pending_zero_hp(
        "瓦莉亚",
        choice="give_up_resistance",
        consequence="被俘：押往河岸岗哨",
    )
    app.end_conflict_scene()

    app.start_scene(
        "下游搜索",
        SceneType.STANDARD,
        location="下游浅滩",
        participants=["露琪亚"],
    )
    destination = SceneRecord(
        name="河岸岗哨",
        scene_type=SceneType.STANDARD,
        location="河岸岗哨",
        participants=["岗哨看守"],
        participant_locations={"岗哨看守": "河岸岗哨"},
        scene_id="scene-existing-aftermath",
    )
    app.scene_manager.suspended_scenes.append(destination)

    landed, mode = app.scene_manager.move_participants_to_location(
        ["瓦莉亚"],
        "河岸岗哨",
    )

    assert mode == "restored"
    assert landed is destination
    assert "瓦莉亚" in landed.participants
    assert landed.recovered_fallen_pcs == ["瓦莉亚"]
    recovered = app.character_manager.get("瓦莉亚")
    assert recovered.hp == recovered.max_hp // 2
    assert "瓦莉亚" not in app.conflict_manager.state.fallen_pcs


def test_fallen_pc_recovers_when_movement_creates_their_next_scene() -> None:
    app = _app()
    fallen = _pc("瓦莉亚")
    witness = _pc("露琪亚")
    app.character_manager.add(fallen)
    app.character_manager.add(witness)
    app.start_conflict_scene("断桥之战", ["瓦莉亚", "露琪亚"])
    fallen.hp = 0
    app.conflict_manager.resolve_zero_hp("瓦莉亚")
    app.conflict_manager.resolve_pending_zero_hp(
        "瓦莉亚",
        choice="give_up_resistance",
        consequence="失散：被洪流冲到下游",
    )
    app.end_conflict_scene()
    app.start_scene(
        "下游搜索",
        SceneType.STANDARD,
        location="下游浅滩",
        participants=["露琪亚"],
    )

    landed, mode = app.scene_manager.move_participants_to_location(
        ["瓦莉亚"],
        "芦苇滩临时营地",
        scene_name="瓦莉亚醒来",
    )

    assert mode == "created"
    assert landed.participants == ["瓦莉亚"]
    assert landed.recovered_fallen_pcs == ["瓦莉亚"]
    recovered = app.character_manager.get("瓦莉亚")
    assert recovered.hp == recovered.max_hp // 2
    assert "瓦莉亚" not in app.conflict_manager.state.fallen_pcs


def test_same_location_subset_movement_creates_parallel_scene_branch() -> None:
    app = _app()
    app.start_scene(
        "监狱走廊",
        SceneType.STANDARD,
        location="卡里巴村监狱相邻牢区",
        participants=["诺艾尔", "艾丽妮", "狱卒"],
    )

    landed, mode = app.scene_manager.move_participants_to_location(
        ["诺艾尔", "狱卒"],
        "卡里巴村监狱相邻牢区",
        scene_name="诺艾尔再度被押入牢房",
    )

    assert mode == "created"
    assert landed.participants == ["诺艾尔", "狱卒"]
    assert len(app.scene_manager.suspended_scenes) == 1
    original = app.scene_manager.suspended_scenes[0]
    assert original.name == "监狱走廊"
    assert original.participants == ["艾丽妮"]

def test_fallen_pc_cannot_act_in_the_same_scene_even_if_hp_is_modified() -> None:
    app = _app()
    fallen = _pc("瓦莉亚")
    app.character_manager.add(fallen)
    app.start_conflict_scene("断桥之战", ["瓦莉亚"])
    fallen.hp = 0
    app.conflict_manager.resolve_zero_hp("瓦莉亚")
    app.conflict_manager.resolve_pending_zero_hp(
        "瓦莉亚",
        choice="give_up_resistance",
        consequence="绝望：敌人夺取了桥头",
    )
    app.character_manager.modify_resource("瓦莉亚", "hp", 20)

    try:
        app.interceptor.resolve(Action(ActionType.GUARD, {"actor": "瓦莉亚"}))
    except ValueError as exc:
        assert "失去意识" in str(exc)
    else:
        raise AssertionError("放弃抵抗的PC不应在同一场景重新行动。")


def test_rekindle_hope_restores_only_a_fallen_pc_once_and_keeps_consequence() -> None:
    app = _app()
    healer = _pc("露琪亚")
    healer.hero_skills = ["重燃希望"]
    healer.mp = healer.max_mp = 80
    fallen = _pc("瓦莉亚")
    app.character_manager.add(healer)
    app.character_manager.add(fallen)
    healer = app.character_manager.get("露琪亚")
    fallen = app.character_manager.get("瓦莉亚")
    app.start_conflict_scene("断桥之战", ["露琪亚", "瓦莉亚"])
    fallen.hp = 0
    app.conflict_manager.resolve_zero_hp("瓦莉亚")
    app.conflict_manager.resolve_pending_zero_hp(
        "瓦莉亚",
        choice="give_up_resistance",
        consequence="遗落：魔剑坠入洪流",
    )

    result = app.interceptor.resolve(
        Action(
            ActionType.SKILL,
            {
                "actor": "露琪亚",
                "target": "瓦莉亚",
                "skill_name": "重燃希望",
            },
        )
    )

    assert "恢复意识" in result.rules_text
    assert healer.mp == 40
    assert fallen.hp == fallen.max_hp // 2
    assert "瓦莉亚" not in app.conflict_manager.state.fallen_pcs
    assert app.conflict_manager.state.pc_defeat_consequences["瓦莉亚"] == [
        "遗落：魔剑坠入洪流"
    ]

    fallen.hp = 0
    app.conflict_manager.resolve_zero_hp("瓦莉亚")
    app.conflict_manager.resolve_pending_zero_hp(
        "瓦莉亚",
        choice="give_up_resistance",
        consequence="绝望：敌人夺走桥头",
    )
    try:
        app.interceptor.resolve(
            Action(
                ActionType.SKILL,
                {
                    "actor": "露琪亚",
                    "target": "瓦莉亚",
                    "skill_name": "重燃希望",
                },
            )
        )
    except ValueError as exc:
        assert "已经被【重燃希望】影响过一次" in str(exc)
    else:
        raise AssertionError("同一PC在一个场景中只能被【重燃希望】影响一次。")
