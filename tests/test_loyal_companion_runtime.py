from __future__ import annotations

from fu_gm.app_factory import build_app
from fu_gm.models import Action, ActionType, Character, RollOutcome, SceneType, StatusEffect


def _owner() -> Character:
    return Character(
        name="旅人",
        attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
        max_hp=45,
        hp=45,
        max_mp=45,
        mp=45,
        level=5,
        crisis_threshold=22,
        defenses={"physical": 8, "magic": 8},
        traits=["pc"],
        skills={"忠诚伙伴": 2},
    )


def _enemy(*, hp: int = 40) -> Character:
    return Character(
        name="铁皮盗匪",
        attributes={"DEX": 6, "INS": 6, "MIG": 8, "WLP": 6},
        max_hp=hp,
        hp=hp,
        max_mp=20,
        mp=20,
        crisis_threshold=hp // 2,
        defenses={"physical": 6, "magic": 6},
        traits=["enemy", "humanoid"],
    )


def _build_runtime():
    app = build_app(use_llm=False)
    app.character_manager.add(_owner())
    app.character_manager.add(_enemy())
    app.scene_manager.start_scene(
        "废弃车站",
        SceneType.STANDARD,
        location="月台",
        participants=["旅人", "铁皮盗匪"],
    )
    companion = app.loyal_companion_manager.create(
        "旅人",
        "铜铃",
        species="构装体",
        traits=["忠心", "好奇", "坚固", "小型"],
        attribute_spread="标准",
        attribute_order=["力量", "敏捷", "洞察", "意志"],
        selected_skills=["强化生命", "特殊攻击"],
        skill_options={},
        attacks=[
            {
                "name": "铁尾横扫",
                "attributes": ["力量", "敏捷"],
                "damage_type": "物理",
                "range": "melee",
                "status_effect_on_hit": "迟缓",
            }
        ],
        profile={"core_drive": "保护旅人"},
    )
    return app, companion


def _forced_roll(*, total: int = 12, critical: bool = False) -> RollOutcome:
    value = total // 2
    return RollOutcome(
        actor="",
        attributes=[],
        dice=[(10, value), (10, value if critical else max(1, total - value))],
        total=total,
        modifier=0,
        high_roll=max(value, total - value),
        target_number=0,
        success=True,
        critical_success=critical,
        fumble=False,
        opportunity_count=1 if critical else 0,
    )


def test_companion_creation_uses_wayfarer_formula_and_follows_owner() -> None:
    app, companion = _build_runtime()

    assert companion.level == 5
    assert companion.max_hp == 32
    assert companion.crisis_threshold == 16
    assert companion.initiative == 0
    assert companion.weapon_accuracy_modifier == 2
    assert "ally" not in companion.traits
    assert "enemy" not in companion.traits
    assert companion.name in app.scene_manager.current_scene.participants
    assert app.loyal_companion_manager.public_state("旅人")["attacks"][0][
        "name"
    ] == "铁尾横扫"


def test_companion_never_receives_an_independent_conflict_turn() -> None:
    app, _companion = _build_runtime()

    app.conflict_manager.start_scene(
        "月台伏击",
        ["旅人", "铜铃", "铁皮盗匪"],
        player_side=["旅人", "铜铃"],
        enemy_side=["铁皮盗匪"],
    )

    assert app.conflict_manager.state.turn_order == ["旅人", "铁皮盗匪"]
    assert "铜铃" not in app.conflict_manager.state.player_side


def test_companion_critical_opportunity_is_owned_by_player() -> None:
    app, _companion = _build_runtime()
    app.conflict_manager.start_scene(
        "月台伏击",
        ["旅人", "铁皮盗匪"],
        player_side=["旅人"],
        enemy_side=["铁皮盗匪"],
    )
    app.interceptor.rules_engine.force_next_check_outcome(
        _forced_roll(total=16, critical=True)
    )

    resolution = app.interceptor.resolve(
        Action(
            ActionType.SKILL,
            {
                "actor": "旅人",
                "skill_name": "忠诚伙伴",
                "companion_action_type": "Attack",
                "attack_name": "铁尾横扫",
                "target": "铁皮盗匪",
                "_enforce_turn_order": True,
            },
        )
    )

    window = app.interceptor.decision_window_manager.find_pending(
        kind="critical_opportunity",
        owner="旅人",
    )
    assert window is not None
    assert window.payload["source_actor"] == "铜铃"
    assert resolution.payload["nested_resolution"].payload["roll"].actor == "铜铃"


def test_companion_final_blow_gives_fate_choice_to_owner() -> None:
    app, _companion = _build_runtime()
    app.character_manager.get("铁皮盗匪").hp = 1
    app.conflict_manager.start_scene(
        "月台伏击",
        ["旅人", "铁皮盗匪"],
        player_side=["旅人"],
        enemy_side=["铁皮盗匪"],
    )
    app.interceptor.rules_engine.force_next_check_outcome(
        _forced_roll(total=12)
    )

    app.interceptor.resolve(
        Action(
            ActionType.SKILL,
            {
                "actor": "旅人",
                "skill_name": "忠诚伙伴",
                "companion_action_type": "Attack",
                "attack_name": "铁尾横扫",
                "target": "铁皮盗匪",
                "_enforce_turn_order": True,
            },
        )
    )

    fate = app.interceptor.decision_window_manager.find_pending(
        kind="npc_fate",
        owner="旅人",
    )
    assert fate is not None
    assert fate.payload["source_actor"] == "旅人"


def test_companion_guard_expires_at_owner_next_turn() -> None:
    app, companion = _build_runtime()
    app.conflict_manager.apply_guard(companion.name)
    assert companion.guarding is True

    app.loyal_companion_manager.on_owner_turn_start("旅人", 2)

    assert companion.guarding is False


def test_companion_retreats_at_zero_hp_and_rejoins_next_scene_at_crisis() -> None:
    app, companion = _build_runtime()
    companion.hp = 0

    event = app.conflict_manager.resolve_zero_hp(companion.name)

    assert event.event_type == "loyal_companion_retreat"
    assert companion.name not in app.scene_manager.current_scene.participants
    assert not app.interceptor.decision_window_manager.pending(kind="npc_fate")

    app.scene_manager.end_scene("队伍离开月台。")
    app.scene_manager.start_scene(
        "维修棚",
        SceneType.STANDARD,
        location="工棚",
        participants=["旅人"],
    )

    assert companion.name in app.scene_manager.current_scene.participants
    assert companion.hp == companion.crisis_threshold
    assert (
        app.loyal_companion_manager.public_state("旅人")["awaiting_rejoin"]
        is False
    )


def test_companion_receives_same_rest_benefits_as_owner() -> None:
    app, companion = _build_runtime()
    companion.hp = 1
    companion.mp = 1
    companion.statuses.append(StatusEffect.SLOW)

    resolution = app.interceptor.resolve(
        Action(
            ActionType.REST,
            {
                "actor": "旅人",
                "rest_type": "wilderness",
                "safe_source": "友善猎人的营地",
                "participants": ["旅人"],
            },
        )
    )

    assert companion.hp == companion.max_hp
    assert companion.mp == companion.max_mp
    assert companion.statuses == []
    assert resolution.payload["loyal_companion_recoveries"][0]["name"] == "铜铃"
