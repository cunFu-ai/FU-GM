from __future__ import annotations

import tempfile

from fu_gm.http_server import FUGMHttpService
from fu_gm.models import Action, ActionType, Character, SceneType


def _routing_app(*, leader_skills: dict[str, int] | None = None):
    data_root = tempfile.TemporaryDirectory()
    # Keep opportunity windows deterministic: these tests exercise assist
    # consumption, not critical/fumble branching.  An unfixed seed can open a
    # legitimate blocking opportunity after the first check and make the
    # second action fail for an unrelated reason.
    service = FUGMHttpService(
        data_root=data_root.name,
        use_llm=False,
        rules_seed=12345,
    )
    app = service._runtime("pending-assist-routing").app
    for character in (
        Character(
            name="伊莉雅",
            attributes={"DEX": 8, "INS": 10, "MIG": 8, "WLP": 8},
            max_hp=50,
            hp=50,
            max_mp=50,
            mp=50,
            inventory_points=6,
            max_inventory_points=6,
            traits=["pc"],
            skills=dict(leader_skills or {}),
            weapon_damage=5,
        ),
        Character(
            name="洛岚",
            attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 10},
            max_hp=45,
            hp=45,
            max_mp=40,
            mp=40,
            traits=["pc"],
        ),
        Character(
            name="机兵",
            attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 6},
            max_hp=80,
            hp=80,
            max_mp=20,
            mp=20,
            defenses={"physical": 8, "magic": 8},
            traits=["enemy"],
        ),
    ):
        app.character_manager.add(character)
    app.scene_manager.start_scene(
        "风铃廊冲突",
        SceneType.CONFLICT,
        participants=["伊莉雅", "洛岚", "机兵"],
    )
    app.conflict_manager.start_scene(
        "风铃廊冲突",
        ["伊莉雅", "机兵", "洛岚"],
        player_side=["伊莉雅", "洛岚"],
        enemy_side=["机兵"],
    )
    assert app.conflict_manager.register_team_assist("洛岚", "伊莉雅")
    return data_root, app


def _execute_rules_transaction(app, action: Action):
    """Drive the same structured-turn entrypoint used by the GM tool agent."""

    captured: dict[str, object] = {}
    app.build_panel = lambda _recent_chat: object()
    app._settle_bound_scene_condition = lambda _resolution: None
    app._auto_advance_conflict_turn = lambda _action, _resolution: None
    app._auto_advance_free_scene_action = lambda *_args, **_kwargs: None

    def complete(**kwargs):
        captured["action"] = kwargs["action"]
        captured["resolution"] = kwargs["resolution"]
        return "ok"

    app._complete_resolved_player_turn = complete
    assert app.run_structured_turn(action, "test action", speaker="player") == "ok"
    return captured["action"], captured["resolution"]


def test_non_check_actions_preserve_pending_assist_before_attack_consumes_it() -> None:
    data_root, app = _routing_app()
    try:
        actions = (
            Action(ActionType.GUARD, {"actor": "伊莉雅"}),
            Action(ActionType.EQUIP, {"actor": "伊莉雅", "items": []}),
            Action(
                ActionType.USE_INVENTORY,
                {
                    "actor": "伊莉雅",
                    "item_name": "治疗剂",
                    "target": "伊莉雅",
                },
            ),
        )
        for action in actions:
            routed, resolution = _execute_rules_transaction(app, action)
            assert "supporters" not in routed.parameters
            assert app.conflict_manager.state.pending_assists == {
                "伊莉雅": ["洛岚"]
            }
            assert "conflict_teamwork" not in resolution.payload
            assert app.conflict_manager.state.pending_assists == {
                "伊莉雅": ["洛岚"]
            }

        attack, resolution = _execute_rules_transaction(
            app,
            Action(
                ActionType.ATTACK,
                {
                    "actor": "伊莉雅",
                    "target": "机兵",
                    "attributes": ["DEX", "MIG"],
                    "weapon_damage": 5,
                },
            )
        )
        assert attack.parameters["supporters"] == ["洛岚"]
        assert "teamwork_turns_already_consumed" not in attack.parameters
        assert app.conflict_manager.state.pending_assists == {}

        assert resolution.payload["conflict_teamwork"]["supporters"] == ["洛岚"]
        assert resolution.payload["conflict_teamwork"]["support_bonus"] == 1
    finally:
        data_root.cleanup()


def test_hinder_consumes_pending_assist_exactly_once() -> None:
    data_root, app = _routing_app()
    try:
        hinder, resolution = _execute_rules_transaction(
            app,
            Action(
                ActionType.HINDER,
                {
                    "actor": "伊莉雅",
                    "target": "机兵",
                    "attributes": ["INS", "WLP"],
                    "target_number": 10,
                    "status_effect": "slow",
                },
            )
        )
        assert hinder.parameters["supporters"] == ["洛岚"]
        assert app.conflict_manager.state.pending_assists == {}
        assert resolution.payload["conflict_teamwork"]["supporters"] == ["洛岚"]

        second, second_resolution = _execute_rules_transaction(
            app,
            Action(
                ActionType.HINDER,
                {
                    "actor": "伊莉雅",
                    "target": "机兵",
                    "attributes": ["INS", "WLP"],
                    "target_number": 10,
                },
            )
        )
        assert "supporters" not in second.parameters
        assert "conflict_teamwork" not in second_resolution.payload
    finally:
        data_root.cleanup()


def test_skill_routing_uses_the_implemented_skill_behavior() -> None:
    data_root, app = _routing_app(leader_skills={"鼓舞": 1, "暗影击": 1})
    try:
        automatic_skill, automatic_resolution = _execute_rules_transaction(
            app,
            Action(
                ActionType.SKILL,
                {"actor": "伊莉雅", "skill_name": "鼓舞", "target": "伊莉雅"},
            )
        )
        assert "supporters" not in automatic_skill.parameters
        assert app.conflict_manager.state.pending_assists == {
            "伊莉雅": ["洛岚"]
        }
        assert "conflict_teamwork" not in automatic_resolution.payload

        checked_skill, resolution = _execute_rules_transaction(
            app,
            Action(
                ActionType.SKILL,
                {"actor": "伊莉雅", "skill_name": "暗影击", "target": "机兵"},
            )
        )
        assert checked_skill.parameters["supporters"] == ["洛岚"]
        assert app.conflict_manager.state.pending_assists == {}
        assert resolution.payload["conflict_teamwork"]["supporters"] == ["洛岚"]
    finally:
        data_root.cleanup()


def test_spell_routing_preserves_buff_assist_and_consumes_damage_check() -> None:
    data_root, app = _routing_app()
    try:
        buff, buff_resolution = _execute_rules_transaction(
            app,
            Action(
                ActionType.SPELL,
                {
                    "actor": "伊莉雅",
                    "spell_name": "魔导屏障",
                    "target": "伊莉雅",
                },
            )
        )
        assert "supporters" not in buff.parameters
        assert app.conflict_manager.state.pending_assists == {
            "伊莉雅": ["洛岚"]
        }
        assert "conflict_teamwork" not in buff_resolution.payload

        damage_spell, resolution = _execute_rules_transaction(
            app,
            Action(
                ActionType.SPELL,
                {"actor": "伊莉雅", "spell_name": "落雷", "target": "机兵"},
            )
        )
        assert damage_spell.parameters["supporters"] == ["洛岚"]
        assert app.conflict_manager.state.pending_assists == {}
        assert resolution.payload["conflict_teamwork"]["supporters"] == ["洛岚"]
    finally:
        data_root.cleanup()


def test_failed_spell_without_a_roll_does_not_consume_pending_assist_as_check() -> None:
    data_root, app = _routing_app()
    try:
        app.character_manager.get("伊莉雅").mp = 0

        routed, resolution = _execute_rules_transaction(
            app,
            Action(
                ActionType.SPELL,
                {"actor": "伊莉雅", "spell_name": "落雷", "target": "机兵"},
            ),
        )

        assert routed.parameters["supporters"] == ["洛岚"]
        assert resolution.payload["spell_failed"] is True
        assert "roll" not in resolution.payload
        assert "conflict_teamwork" not in resolution.payload
        assert app.conflict_manager.state.pending_assists == {
            "伊莉雅": ["洛岚"]
        }
    finally:
        data_root.cleanup()
