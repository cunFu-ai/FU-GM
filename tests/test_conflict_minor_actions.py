from __future__ import annotations

from copy import deepcopy

import pytest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.world_state import WorldState
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import Action, ActionType, Character


def _fixture(*, include_receiver: bool = False) -> tuple[ActionInterceptor, ConflictManager, WorldState]:
    characters = CharacterManager()
    characters.add(
        Character(
            name="诺艾尔",
            attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 8},
            max_hp=50,
            hp=50,
            max_mp=30,
            mp=30,
            traits=["pc"],
        )
    )
    characters.add(
        Character(
            name="燃炉卫兵",
            attributes={"DEX": 8, "INS": 6, "MIG": 8, "WLP": 6},
            max_hp=40,
            hp=40,
            max_mp=0,
            mp=0,
            traits=["enemy"],
        )
    )
    if include_receiver:
        characters.add(
            Character(
                name="莉欧",
                attributes={"DEX": 8, "INS": 10, "MIG": 6, "WLP": 8},
                max_hp=42,
                hp=42,
                max_mp=35,
                mp=35,
                traits=["pc"],
            )
        )
    world = WorldState()
    world.commit_story_item_action(
        operation="place",
        item_name="铁环钥匙",
        actor="GM",
        scene_location="熔炉前厅",
        public_fact="铁环钥匙落在熔炉前厅的地上。",
        source="test_fixture",
        to_location="熔炉前厅",
    )
    conflict = ConflictManager(characters)
    turn_order = ["诺艾尔", "燃炉卫兵"]
    player_side = ["诺艾尔"]
    if include_receiver:
        turn_order.append("莉欧")
        player_side.append("莉欧")
    conflict.start_scene(
        "熔炉前厅",
        turn_order,
        player_side=player_side,
        enemy_side=["燃炉卫兵"],
    )
    return (
        ActionInterceptor(
            RulesEngine(),
            characters,
            ClockManager(),
            conflict,
            world,
        ),
        conflict,
        world,
    )


def _register_equipment_story_item(
    interceptor: ActionInterceptor,
    world: WorldState,
    item_name: str,
    *,
    held: bool,
) -> None:
    actor = interceptor.character_manager.get("诺艾尔")
    if item_name not in actor.equipment:
        actor.equipment.append(item_name)
    world.commit_story_item_action(
        operation="acquire" if held else "place",
        item_name=item_name,
        actor="诺艾尔" if held else "GM",
        scene_location="熔炉前厅",
        public_fact=f"测试登记剧情装备【{item_name}】。",
        source="test_fixture",
        to_location="熔炉前厅" if not held else "",
    )


def _authoritative_snapshot(
    interceptor: ActionInterceptor,
    conflict: ConflictManager,
    world: WorldState,
) -> object:
    return deepcopy(
        (
            world.__dict__,
            interceptor.character_manager.all(),
            conflict.state,
        )
    )


def test_conflict_minor_action_changes_item_custody_without_spending_main_action() -> None:
    interceptor, conflict, world = _fixture()
    before_actor = conflict.state.current_actor()
    before_round = conflict.state.round_number

    resolution = interceptor.resolve(
        Action(
            ActionType.MINOR_ACTION,
            {
                "actor": "诺艾尔",
                "mode": "pickup",
                "item_name": "铁环钥匙",
            },
        )
    )

    item = world.find_story_item(name="铁环钥匙")
    assert item is not None
    assert item.holder == "诺艾尔"
    assert resolution.payload["minor_action"] is True
    assert conflict.state.current_actor() == before_actor
    assert conflict.state.round_number == before_round
    assert conflict.state.turn_started_actor == "诺艾尔"


def test_conflict_minor_actions_can_repeat_but_never_resolve_a_check() -> None:
    interceptor, _conflict, world = _fixture()

    with pytest.raises(ValueError, match="属于主要行动"):
        interceptor.resolve(
            Action(
                ActionType.MINOR_ACTION,
                {
                    "actor": "诺艾尔",
                    "mode": "pickup",
                    "item_name": "铁环钥匙",
                    "requires_check": True,
                },
            )
        )
    assert world.find_story_item(name="铁环钥匙").holder == ""

    interceptor.resolve(
        Action(
            ActionType.MINOR_ACTION,
            {
                "actor": "诺艾尔",
                "mode": "pickup",
                "item_name": "铁环钥匙",
            },
        )
    )
    second = interceptor.resolve(
        Action(
            ActionType.MINOR_ACTION,
            {
                "actor": "诺艾尔",
                "mode": "drop",
                "item_name": "铁环钥匙",
            },
        )
    )
    assert second.payload["minor_action"] is True
    assert world.find_story_item(name="铁环钥匙").holder == ""


def test_conflict_minor_action_refreshes_on_the_owners_next_turn() -> None:
    interceptor, conflict, world = _fixture()
    interceptor.resolve(
        Action(
            ActionType.MINOR_ACTION,
            {"actor": "诺艾尔", "mode": "pickup", "item_name": "铁环钥匙"},
        )
    )

    assert conflict.end_current_turn() == "诺艾尔"
    assert conflict.begin_current_turn() == "燃炉卫兵"
    assert conflict.end_current_turn() == "燃炉卫兵"
    assert conflict.prepare_current_turn_slot() == "诺艾尔"

    resolution = interceptor.resolve(
        Action(
            ActionType.MINOR_ACTION,
            {"actor": "诺艾尔", "mode": "drop", "item_name": "铁环钥匙"},
        )
    )

    assert resolution.payload["minor_action"] is True
    assert world.find_story_item(name="铁环钥匙").holder == ""


def test_pickup_can_immediately_equip_into_an_empty_slot() -> None:
    interceptor, _conflict, world = _fixture()
    _register_equipment_story_item(interceptor, world, "钢匕首", held=False)

    resolution = interceptor.resolve(
        Action(
            ActionType.MINOR_ACTION,
            {
                "actor": "诺艾尔",
                "mode": "pickup",
                "item_name": "钢匕首",
                "equip_slot": "main_hand",
            },
        )
    )

    actor = interceptor.character_manager.get("诺艾尔")
    assert actor.equipped_main_hand == "钢匕首"
    assert world.find_story_item(name="钢匕首").holder == "诺艾尔"
    assert resolution.payload["equipped_slots"]["main_hand"] == "钢匕首"


def test_pickup_cannot_replace_an_occupied_slot_and_failure_is_atomic() -> None:
    interceptor, conflict, world = _fixture()
    actor = interceptor.character_manager.get("诺艾尔")
    actor.equipment.extend(["铁锤", "钢匕首"])
    actor.equipped_main_hand = "铁锤"
    _register_equipment_story_item(interceptor, world, "钢匕首", held=False)
    before = _authoritative_snapshot(interceptor, conflict, world)

    with pytest.raises(ValueError, match="空栏位"):
        interceptor.resolve(
            Action(
                ActionType.MINOR_ACTION,
                {
                    "actor": "诺艾尔",
                    "mode": "pickup",
                    "item_name": "钢匕首",
                    "equip_slot": "main_hand",
                },
            )
        )

    assert _authoritative_snapshot(interceptor, conflict, world) == before


@pytest.mark.parametrize(
    ("mode", "extra_parameters", "expected_holder"),
    [
        ("drop", {}, ""),
        ("throw", {}, ""),
        ("pass", {"to_holder": "莉欧"}, "莉欧"),
    ],
)
def test_drop_throw_and_pass_clear_an_equipped_item(
    mode: str,
    extra_parameters: dict[str, str],
    expected_holder: str,
) -> None:
    interceptor, _conflict, world = _fixture(include_receiver=True)
    actor = interceptor.character_manager.get("诺艾尔")
    _register_equipment_story_item(interceptor, world, "钢匕首", held=True)
    actor.equipped_main_hand = "钢匕首"

    resolution = interceptor.resolve(
        Action(
            ActionType.MINOR_ACTION,
            {
                "actor": "诺艾尔",
                "mode": mode,
                "item_name": "钢匕首",
                **extra_parameters,
            },
        )
    )

    assert actor.equipped_main_hand == "徒手攻击"
    assert world.find_story_item(name="钢匕首").holder == expected_holder
    assert resolution.payload["unequipped_slots"] == ["main_hand"]


@pytest.mark.parametrize(
    ("item_name", "field_name", "empty_value", "expected_slot"),
    [
        ("钢匕首", "equipped_main_hand", "徒手攻击", "main_hand"),
        ("钢匕首", "equipped_off_hand", "", "off_hand"),
        ("青铜盾", "equipped_shield", "", "shield"),
        ("琥珀吊坠", "equipped_accessory", "", "accessory"),
    ],
)
def test_dropping_equipment_clears_its_corresponding_loadout_slot(
    item_name: str,
    field_name: str,
    empty_value: str,
    expected_slot: str,
) -> None:
    interceptor, _conflict, world = _fixture()
    actor = interceptor.character_manager.get("诺艾尔")
    _register_equipment_story_item(interceptor, world, item_name, held=True)
    setattr(actor, field_name, item_name)

    resolution = interceptor.resolve(
        Action(
            ActionType.MINOR_ACTION,
            {"actor": "诺艾尔", "mode": "drop", "item_name": item_name},
        )
    )

    assert getattr(actor, field_name) == empty_value
    assert resolution.payload["unequipped_slots"] == [expected_slot]


@pytest.mark.parametrize(
    ("to_holder", "register_absent", "error"),
    [
        ("诺艾尔", False, "另一名角色"),
        ("不存在的幽灵", False, "不是已登记角色"),
        ("场外旅者", True, "不在当前冲突场景"),
    ],
)
def test_transfer_requires_another_registered_character_in_the_active_conflict_and_is_atomic(
    to_holder: str,
    register_absent: bool,
    error: str,
) -> None:
    interceptor, conflict, world = _fixture()
    if register_absent:
        interceptor.character_manager.add(
            Character(
                name=to_holder,
                attributes={"DEX": 6, "INS": 8, "MIG": 6, "WLP": 8},
                max_hp=30,
                hp=30,
                max_mp=20,
                mp=20,
                traits=["npc"],
            )
        )
    _register_equipment_story_item(interceptor, world, "钢匕首", held=True)
    before = _authoritative_snapshot(interceptor, conflict, world)

    with pytest.raises(ValueError, match=error):
        interceptor.resolve(
            Action(
                ActionType.MINOR_ACTION,
                {
                    "actor": "诺艾尔",
                    "mode": "transfer",
                    "item_name": "钢匕首",
                    "to_holder": to_holder,
                },
            )
        )

    assert _authoritative_snapshot(interceptor, conflict, world) == before
