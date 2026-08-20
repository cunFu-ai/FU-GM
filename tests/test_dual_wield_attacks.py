import tempfile

import pytest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.world_state import WorldState
from fu_gm.expressor import Expressor
from fu_gm.gm_tool_agent import GMToolExecutionContext
from fu_gm.http_server import FUGMHttpService
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import (
    Action,
    ActionType,
    Bond,
    Character,
    HeroDraft,
    PendingCheckBatch,
    SceneType,
)


class FakeRandom:
    def __init__(self, values: list[int]) -> None:
        self.values = list(values)

    def randint(self, low: int, high: int) -> int:
        if not self.values:
            raise AssertionError("双武器攻击进行了预期外的额外掷骰。")
        value = self.values.pop(0)
        if value < low or value > high:
            raise AssertionError(f"掷骰值 {value} 超出范围 {low}-{high}")
        return value

    def getstate(self):
        return tuple(self.values)

    def setstate(self, state) -> None:
        self.values = list(state)


def _hero(*, main_hand: str = "青铜剑", off_hand: str = "细剑", hero_skills=None) -> Character:
    return Character(
        name="双刀客",
        attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
        max_hp=40,
        hp=40,
        max_mp=40,
        mp=40,
        defenses={"physical": 10, "magic": 10},
        traits=["pc"],
        equipment=[main_hand, off_hand],
        equipped_main_hand=main_hand,
        equipped_off_hand=off_hand,
        weapon_accuracy_attributes=["DEX", "MIG"],
        weapon_accuracy_modifier=1,
        weapon_damage=6,
        weapon_range="melee",
        hero_skills=list(hero_skills or []),
    )


def _enemy(name: str) -> Character:
    return Character(
        name=name,
        attributes={"DEX": 6, "MIG": 6, "INS": 6, "WLP": 6},
        max_hp=40,
        hp=40,
        max_mp=20,
        mp=20,
        defenses={"physical": 5, "magic": 5},
        traits=["enemy"],
    )


def _runtime(
    *,
    main_hand: str = "青铜剑",
    off_hand: str = "细剑",
    hero_skills=None,
    dice=None,
):
    characters = CharacterManager()
    characters.add(
        _hero(
            main_hand=main_hand,
            off_hand=off_hand,
            hero_skills=hero_skills,
        )
    )
    characters.add(_enemy("甲"))
    characters.add(_enemy("乙"))
    rules = RulesEngine()
    fake_random = FakeRandom(list(dice or [6, 5, 4, 3]))
    rules._rng = fake_random
    conflict = ConflictManager(characters)
    interceptor = ActionInterceptor(
        rules,
        characters,
        ClockManager(),
        conflict,
        WorldState(),
    )
    return interceptor, characters, conflict, fake_random


def _dual_attack(*targets: str, **extra) -> Action:
    parameters = {
        "actor": "双刀客",
        "target": targets[0],
        "targets": list(targets),
        "dual_wield": True,
        **extra,
    }
    return Action(ActionType.ATTACK, parameters)


def test_dual_wield_rolls_twice_against_same_target_with_zero_damage_hr() -> None:
    interceptor, characters, _, fake_random = _runtime()
    actor = characters.get("双刀客")
    actor.equipment_multi_attack = 3
    actor.hero_skills.append("疾风连打")

    resolution = interceptor.resolve(_dual_attack("甲", "甲"))

    assert len(resolution.payload["rolls"]) == 2
    assert [roll.dice for roll in resolution.payload["rolls"]] == [
        [(8, 6), (8, 5)],
        [(8, 4), (8, 3)],
    ]
    assert all(roll.success for roll in resolution.payload["rolls"])
    assert [roll.high_roll for roll in resolution.payload["rolls"]] == [0, 0]
    assert [roll.damage for roll in resolution.payload["rolls"]] == [6, 6]
    assert characters.get("甲").hp == 28
    assert resolution.payload["multi_attack_suppressed"] is True
    assert fake_random.values == []


def test_dual_wield_may_split_its_two_independent_checks_between_targets() -> None:
    interceptor, characters, _, _ = _runtime()

    resolution = interceptor.resolve(_dual_attack("甲", "乙"))

    assert [roll.target for roll in resolution.payload["rolls"]] == ["甲", "乙"]
    assert characters.get("甲").hp == 34
    assert characters.get("乙").hp == 34
    rendered = Expressor().render(resolution)
    assert "【双武器攻击】" in rendered
    assert "双刀客 -> 甲" in rendered
    assert "双刀客 -> 乙" in rendered


def test_dual_wield_requires_same_weapon_category_without_flexible_dual_wield() -> None:
    interceptor, characters, _, fake_random = _runtime(off_hand="钢匕首")

    with pytest.raises(ValueError, match="武器类型相同"):
        interceptor.resolve(_dual_attack("甲", "甲"))

    assert characters.get("甲").hp == 40
    assert fake_random.values == [6, 5, 4, 3]


@pytest.mark.parametrize("skill_storage", ["skills", "hero_skills", "abilities"])
def test_flexible_dual_wield_allows_legacy_and_current_skill_storage(skill_storage) -> None:
    interceptor, characters, _, _ = _runtime(off_hand="钢匕首")
    actor = characters.get("双刀客")
    if skill_storage == "skills":
        actor.skills["灵活双持"] = 1
    else:
        getattr(actor, skill_storage).append("灵活双持")

    resolution = interceptor.resolve(_dual_attack("甲", "甲"))

    assert [roll.damage for roll in resolution.payload["rolls"]] == [6, 4]
    assert characters.get("甲").hp == 30


def test_dual_wield_rejects_two_handed_weapon_before_any_roll() -> None:
    interceptor, characters, _, fake_random = _runtime(main_hand="巨剑")

    with pytest.raises(ValueError, match="必须是单手武器"):
        interceptor.resolve(_dual_attack("甲", "甲"))

    assert characters.get("甲").hp == 40
    assert fake_random.values == [6, 5, 4, 3]


@pytest.mark.parametrize(
    "followup_marker",
    [
        {"opportunity_action": True},
        {"_reaction_followup": "反击"},
        {"_acceleration_window_id": "window"},
        {"_immediate_attack_window_id": "window"},
        {"_skill_followup_window_id": "window"},
    ],
)
def test_dual_wield_is_forbidden_for_opportunity_attacks(followup_marker) -> None:
    interceptor, characters, _, fake_random = _runtime()
    action = _dual_attack("甲", "甲", **followup_marker)

    with pytest.raises(ValueError, match="顺势攻击不能使用双武器"):
        if "_skill_followup_window_id" in followup_marker:
            # The public coordinator first authenticates this internal window.
            # Exercise the attack resolver directly to prove that even a valid
            # authenticated skill follow-up cannot become a dual attack.
            interceptor._resolve_attack(action)
        else:
            interceptor.resolve(action)

    assert characters.get("甲").hp == 40
    assert fake_random.values == [6, 5, 4, 3]


def test_dual_wield_is_logged_as_one_main_action_in_conflict() -> None:
    interceptor, _, conflict, _ = _runtime()
    conflict.start_scene(
        "双武器回归战",
        ["双刀客", "甲"],
        player_side=["双刀客"],
        enemy_side=["甲"],
    )

    resolution = interceptor.resolve(_dual_attack("甲", "甲"))

    attack_logs = [
        entry
        for entry in conflict.state.combat_log
        if entry.actor == "双刀客" and entry.event_type == ActionType.ATTACK.value
    ]
    assert len(attack_logs) == 1
    assert conflict.state.turn_serial == 1
    assert resolution.payload["dual_wield"] is True


def test_dual_wield_failed_second_check_replays_both_rolls_atomically_on_acceptance() -> None:
    interceptor, characters, _, fake_random = _runtime(dice=[6, 5, 2, 1])
    actor = characters.get("双刀客")
    actor.identity = "以双剑守护同伴的浪客"
    actor.fabula_points = 2

    provisional = interceptor.resolve(_dual_attack("甲", "甲"))

    assert provisional.payload["check_result_provisional"] is True
    assert [roll.success for roll in provisional.payload["rolls"]] == [True, False]
    assert characters.get("甲").hp == 40
    assert fake_random.values == []

    accepted = interceptor.resolve(
        Action(
            ActionType.NARRATE,
            {
                "actor": "双刀客",
                "summary": "接受第二次命中检定的失败。",
                "scene_clarification": True,
                "post_check_acceptance": True,
            },
        )
    )

    assert accepted.payload["check_transaction_accepted"] is True
    assert [roll.success for roll in accepted.payload["rolls"]] == [True, False]
    assert characters.get("甲").hp == 34
    assert fake_random.values == []


def test_dual_wield_defers_npc_fate_until_failed_second_check_is_final() -> None:
    interceptor, characters, conflict, fake_random = _runtime(
        dice=[6, 5, 2, 1]
    )
    actor = characters.get("双刀客")
    actor.identity = "以双剑守护同伴的浪客"
    actor.fabula_points = 2
    characters.get("甲").hp = 2

    provisional = interceptor.resolve(_dual_attack("甲", "甲"))

    assert provisional.payload["check_result_provisional"] is True
    assert characters.get("甲").hp == 2
    pending = interceptor.decision_window_manager.pending()
    assert [window.kind for window in pending] == ["trait_invocation"]
    assert not interceptor.decision_window_manager.pending(kind="npc_fate")

    accepted = interceptor.resolve(
        Action(
            ActionType.NARRATE,
            {
                "actor": "双刀客",
                "summary": "接受第二次命中检定的失败。",
                "scene_clarification": True,
                "post_check_acceptance": True,
                "window_id": pending[0].window_id,
            },
        )
    )

    assert accepted.payload["check_transaction_accepted"] is True
    assert characters.get("甲").hp == 0
    fate_windows = interceptor.decision_window_manager.pending(kind="npc_fate")
    assert len(fate_windows) == 1

    resolved = interceptor.resolve(
        Action(
            ActionType.RESOLVE_DECISION,
            {
                "actor": "双刀客",
                "window_id": fate_windows[0].window_id,
                "choice": "capture",
                "selected_option": {"choice": "capture"},
            },
        )
    )

    assert resolved.payload["npc_fate_resolved"] is True
    assert not interceptor.decision_window_manager.pending()
    assert conflict.state.defeated_npc_fates == {"甲": "被俘虏"}
    assert sum(
        entry.event_type == "npc_fate_resolved"
        for entry in conflict.state.combat_log
    ) == 1
    assert fake_random.values == []


def test_dual_wield_trait_reroll_does_not_keep_speculative_npc_fate() -> None:
    interceptor, characters, _, fake_random = _runtime(
        dice=[6, 5, 2, 1, 8, 7]
    )
    actor = characters.get("双刀客")
    actor.identity = "以双剑守护同伴的浪客"
    actor.fabula_points = 2
    characters.get("甲").hp = 2

    provisional = interceptor.resolve(_dual_attack("甲", "甲"))
    trait_window = next(
        window
        for window in interceptor.decision_window_manager.pending()
        if window.kind == "trait_invocation"
    )
    invoked = interceptor.resolve(
        Action(
            ActionType.INVOKE_TRAIT,
            {
                "actor": "双刀客",
                "trait_name": actor.identity,
                "invocation_rationale": "浪客重整副手攻势。",
                "reroll_indices": [0, 1],
                "reroll_index_base": 0,
                "window_id": trait_window.window_id,
            },
        )
    )

    assert provisional.payload["check_result_provisional"] is True
    assert invoked.payload["check_transaction_replayed"] is True
    assert invoked.payload["rolls"][1].success is True
    assert characters.get("甲").hp == 0
    assert len(interceptor.decision_window_manager.pending(kind="npc_fate")) == 1
    assert fake_random.values == []


def test_check_rollback_preserves_window_present_in_original_snapshot() -> None:
    interceptor, characters, _, _ = _runtime(dice=[6, 5, 2, 1])
    actor = characters.get("双刀客")
    actor.identity = "以双剑守护同伴的浪客"
    actor.fabula_points = 2
    existing = interceptor.decision_window_manager.create(
        kind="scene_choice",
        owner="双刀客",
        prompt="原有场景选择",
        blocking=False,
    )

    provisional = interceptor.resolve(_dual_attack("甲", "甲"))

    assert provisional.payload["check_result_provisional"] is True
    restored = interceptor.decision_window_manager.find_pending(
        window_id=existing.window_id
    )
    assert restored is not None
    assert restored.prompt == "原有场景选择"
    assert interceptor.decision_window_manager.pending(kind="trait_invocation")


def test_check_rollback_rejects_unlisted_pre_final_window_and_batch() -> None:
    interceptor, _, _, _ = _runtime()
    snapshot = interceptor.check_transaction_manager.snapshot()
    forged = interceptor.decision_window_manager.create(
        kind="trait_invocation",
        owner="双刀客",
        blocking=True,
        transaction_id="forged-batch",
        payload={"source_actor": "双刀客"},
    )
    interceptor.world_state.pending_check_batches["forged-batch"] = (
        PendingCheckBatch(
            batch_id="forged-batch",
            kind="forged",
            source_action_type="Attack",
            source_parameters={},
            actor_order=["双刀客"],
        )
    )

    interceptor.check_transaction_manager.restore(
        snapshot,
        preserve_control_window_ids=[],
        actor="双刀客",
        batch_id="forged-batch",
    )

    assert interceptor.decision_window_manager.get(forged.window_id) is None
    assert "forged-batch" not in interceptor.world_state.pending_check_batches


def test_dual_wield_failed_first_check_is_not_hidden_by_successful_second_check() -> None:
    interceptor, characters, _, fake_random = _runtime(dice=[2, 1, 6, 5])
    actor = characters.get("双刀客")
    actor.identity = "以双剑守护同伴的浪客"
    actor.fabula_points = 2

    provisional = interceptor.resolve(_dual_attack("甲", "甲"))

    assert provisional.payload["check_result_provisional"] is True
    assert provisional.payload["check_roll_index"] == 0
    assert [roll.success for roll in provisional.payload["rolls"]] == [False, True]
    assert characters.get("甲").hp == 40
    first_windows = provisional.payload["post_check_windows"]
    assert {window["kind"] for window in first_windows} == {"trait_invocation"}
    assert {window["check_roll_index"] for window in first_windows} == {0}

    accepted = interceptor.resolve(
        Action(
            ActionType.NARRATE,
            {
                "actor": "双刀客",
                "summary": "接受第一击失败，保留第二击命中。",
                "scene_clarification": True,
                "post_check_acceptance": True,
                "window_id": first_windows[0]["window_id"],
            },
        )
    )

    assert accepted.payload["check_transaction_accepted"] is True
    assert [roll.success for roll in accepted.payload["rolls"]] == [False, True]
    assert characters.get("甲").hp == 34
    assert fake_random.values == []


def test_dual_wield_trait_invocation_rerolls_only_first_check_and_replays_second() -> None:
    interceptor, characters, _, fake_random = _runtime(
        dice=[2, 1, 6, 5, 8, 7]
    )
    actor = characters.get("双刀客")
    actor.identity = "以双剑守护同伴的浪客"
    actor.fabula_points = 2
    provisional = interceptor.resolve(_dual_attack("甲", "甲"))
    trait_window = next(
        window
        for window in provisional.payload["post_check_windows"]
        if window["kind"] == "trait_invocation"
    )

    invoked = interceptor.resolve(
        Action(
            ActionType.INVOKE_TRAIT,
            {
                "actor": "双刀客",
                "trait_name": actor.identity,
                "invocation_rationale": "浪客不愿让主手的失误破坏并肩攻势。",
                "reroll_indices": [0, 1],
                "reroll_index_base": 0,
                "window_id": trait_window["window_id"],
            },
        )
    )

    assert invoked.payload["check_transaction_replayed"] is True
    assert [roll.dice for roll in invoked.payload["rolls"]] == [
        [(8, 8), (8, 7)],
        [(8, 6), (8, 5)],
    ]
    assert all(roll.success for roll in invoked.payload["rolls"])
    assert all(roll.high_roll == 0 for roll in invoked.payload["rolls"])
    assert characters.get("甲").hp == 28
    assert characters.get("双刀客").fabula_points == 1
    assert fake_random.values == []


def test_dual_wield_bond_invocation_adjusts_only_selected_first_check() -> None:
    interceptor, characters, _, fake_random = _runtime(dice=[2, 1, 6, 5])
    actor = characters.get("双刀客")
    actor.bonds = [Bond(target="同伴", emotions=["信赖", "敬意"])]
    actor.fabula_points = 2
    provisional = interceptor.resolve(_dual_attack("甲", "甲"))
    bond_window = next(
        window
        for window in provisional.payload["post_check_windows"]
        if window["kind"] == "bond_invocation"
    )

    invoked = interceptor.resolve(
        Action(
            ActionType.INVOKE_BOND,
            {
                "actor": "双刀客",
                "bond_target": "同伴",
                "window_id": bond_window["window_id"],
            },
        )
    )

    assert invoked.payload["check_transaction_replayed"] is True
    assert [roll.total for roll in invoked.payload["rolls"]] == [6, 12]
    assert all(roll.success for roll in invoked.payload["rolls"])
    assert all(roll.high_roll == 0 for roll in invoked.payload["rolls"])
    assert characters.get("双刀客").fabula_points == 1
    assert characters.get("甲").hp == 28
    assert fake_random.values == []


def test_dual_wield_two_failures_require_two_independent_acceptances() -> None:
    interceptor, characters, _, _ = _runtime(dice=[2, 1, 2, 1])
    actor = characters.get("双刀客")
    actor.identity = "以双剑守护同伴的浪客"
    actor.fabula_points = 2

    first = interceptor.resolve(_dual_attack("甲", "甲"))
    first_window = first.payload["post_check_windows"][0]
    assert first.payload["check_roll_index"] == 0

    after_first_acceptance = interceptor.resolve(
        Action(
            ActionType.NARRATE,
            {
                "actor": "双刀客",
                "post_check_acceptance": True,
                "scene_clarification": True,
                "window_id": first_window["window_id"],
            },
        )
    )

    assert after_first_acceptance.payload["check_result_provisional"] is True
    assert after_first_acceptance.payload["check_roll_index"] == 1
    assert {
        window["check_roll_index"]
        for window in after_first_acceptance.payload["post_check_windows"]
    } == {1}
    assert characters.get("甲").hp == 40

    second_window = after_first_acceptance.payload["post_check_windows"][0]
    final = interceptor.resolve(
        Action(
            ActionType.NARRATE,
            {
                "actor": "双刀客",
                "post_check_acceptance": True,
                "scene_clarification": True,
                "window_id": second_window["window_id"],
            },
        )
    )

    assert not final.payload.get("check_result_provisional")
    assert [roll.success for roll in final.payload["rolls"]] == [False, False]
    assert characters.get("甲").hp == 40


def test_dual_wield_keeps_one_critical_opportunity_per_independent_check() -> None:
    interceptor, characters, _, _ = _runtime(dice=[6, 6, 7, 7])

    resolution = interceptor.resolve(_dual_attack("甲", "甲"))

    criticals = interceptor.decision_window_manager.pending(
        kind="critical_opportunity",
        owner="双刀客",
    )
    assert len(criticals) == 2
    assert {window.payload["check_roll_index"] for window in criticals} == {0, 1}
    first = next(
        window for window in criticals if window.payload["check_roll_index"] == 0
    )
    interceptor.resolve(
        Action(
            ActionType.TRIGGER_OPPORTUNITY,
            {
                "actor": "双刀客",
                "effect": "情报",
                "window_id": first.window_id,
            },
        )
    )

    remaining = interceptor.decision_window_manager.pending(
        kind="critical_opportunity",
        owner="双刀客",
    )
    assert len(remaining) == 1
    assert remaining[0].payload["check_roll_index"] == 1
    assert [roll.critical_success for roll in resolution.payload["rolls"]] == [True, True]
    assert characters.get("甲").hp == 28


def test_dual_wield_declines_each_critical_opportunity_independently() -> None:
    interceptor, characters, _, _ = _runtime(dice=[6, 6, 7, 7])

    interceptor.resolve(_dual_attack("甲", "甲"))

    criticals = interceptor.decision_window_manager.pending(
        kind="critical_opportunity",
        owner="双刀客",
    )
    first = next(
        window for window in criticals if window.payload["check_roll_index"] == 0
    )
    declined = interceptor.resolve(
        Action(
            ActionType.TRIGGER_OPPORTUNITY,
            {
                "actor": "双刀客",
                "effect": "decline",
                "window_id": first.window_id,
            },
        )
    )

    remaining = interceptor.decision_window_manager.pending(
        kind="critical_opportunity",
        owner="双刀客",
    )
    assert declined.payload["opportunity_declined"] is True
    assert declined.rules_text == "这次机会未被使用。"
    assert len(remaining) == 1
    assert remaining[0].payload["check_roll_index"] == 1
    assert characters.get("甲").hp == 28


def test_resolving_first_critical_preserves_second_checks_trait_boundary() -> None:
    interceptor, characters, _, fake_random = _runtime(
        dice=[6, 6, 4, 3, 8, 7]
    )
    actor = characters.get("双刀客")
    actor.identity = "以双剑守护同伴的浪客"
    actor.fabula_points = 2
    resolution = interceptor.resolve(_dual_attack("甲", "甲"))
    critical = next(
        window
        for window in interceptor.decision_window_manager.pending(
            kind="critical_opportunity",
            owner="双刀客",
        )
        if window.payload["check_roll_index"] == 0
    )

    interceptor.resolve(
        Action(
            ActionType.TRIGGER_OPPORTUNITY,
            {
                "actor": "双刀客",
                "effect": "情报",
                "window_id": critical.window_id,
            },
        )
    )
    second_trait = next(
        window
        for window in interceptor.decision_window_manager.pending(
            kind="trait_invocation",
            owner="双刀客",
        )
        if window.payload["check_roll_index"] == 1
    )
    invoked = interceptor.resolve(
        Action(
            ActionType.INVOKE_TRAIT,
            {
                "actor": "双刀客",
                "trait_name": actor.identity,
                "invocation_rationale": "浪客把副手攻势也维持在与同伴约定的节奏中。",
                "reroll_indices": [0, 1],
                "reroll_index_base": 0,
                "window_id": second_trait.window_id,
            },
        )
    )

    assert invoked.payload["check_transaction_replayed"] is True
    assert [roll.dice for roll in invoked.payload["rolls"]] == [
        [(8, 6), (8, 6)],
        [(8, 8), (8, 7)],
    ]
    assert not interceptor.decision_window_manager.pending(
        kind="critical_opportunity",
        owner="双刀客",
    )
    assert characters.get("双刀客").fabula_points == 1
    assert characters.get("甲").hp == 28
    assert fake_random.values == []
    assert resolution.payload["rolls"][0].critical_success is True


def test_dual_wield_defers_first_critical_until_second_failure_is_final() -> None:
    interceptor, characters, _, _ = _runtime(dice=[6, 6, 2, 1])
    actor = characters.get("双刀客")
    actor.identity = "以双剑守护同伴的浪客"
    actor.fabula_points = 2

    provisional = interceptor.resolve(_dual_attack("甲", "甲"))

    assert provisional.payload["check_result_provisional"] is True
    assert provisional.payload["check_roll_index"] == 1
    assert not interceptor.decision_window_manager.pending(
        kind="critical_opportunity",
        owner="双刀客",
    )
    failure_window = provisional.payload["post_check_windows"][0]
    accepted = interceptor.resolve(
        Action(
            ActionType.NARRATE,
            {
                "actor": "双刀客",
                "post_check_acceptance": True,
                "scene_clarification": True,
                "window_id": failure_window["window_id"],
            },
        )
    )

    criticals = interceptor.decision_window_manager.pending(
        kind="critical_opportunity",
        owner="双刀客",
    )
    assert len(criticals) == 1
    assert criticals[0].payload["check_roll_index"] == 0
    assert not accepted.payload.get("check_result_provisional")
    assert [roll.success for roll in accepted.payload["rolls"]] == [True, False]
    assert characters.get("甲").hp == 34


def test_dual_wield_first_fumble_grants_its_own_fabula_and_opportunity() -> None:
    interceptor, characters, _, _ = _runtime(dice=[1, 1, 6, 5])

    resolution = interceptor.resolve(_dual_attack("甲", "甲"))

    assert [roll.fumble for roll in resolution.payload["rolls"]] == [True, False]
    assert characters.get("双刀客").fabula_points == 1
    assert len(resolution.payload["fabula_gains"]) == 1
    gm_windows = interceptor.decision_window_manager.pending(
        kind="fumble_opportunity",
        owner="__gm__",
    )
    assert len(gm_windows) == 1
    assert gm_windows[0].payload["check_roll_index"] == 0
    assert characters.get("甲").hp == 34


def test_perform_character_action_runs_dual_wield_end_to_end_and_advances_once() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        app = service._runtime("dual-wield-tool-test").app
        app.character_manager.add(_hero())
        app.character_manager.add(_enemy("甲"))
        app.character_manager.add(_enemy("乙"))
        app.world_state.world_profile.hero_drafts["玩家"] = HeroDraft(
            player_name="玩家",
            hero_name="双刀客",
        )
        app.scene_manager.start_scene(
            "试炼场",
            SceneType.CONFLICT,
            location="中央",
            participants=["双刀客", "甲", "乙"],
        )
        app.conflict_manager.start_scene(
            "试炼场",
            ["双刀客", "甲", "乙"],
            player_side=["双刀客"],
            enemy_side=["甲", "乙"],
        )
        app.interceptor.rules_engine._rng = FakeRandom([6, 5, 4, 3])
        context = GMToolExecutionContext(
            campaign_id="dual-wield-tool-test",
            session_id="s1",
            channel_id="c1",
            speaker="玩家",
            gate_status="adventure",
            directly_addressed=False,
            metadata={"current_message": "双刀客用青铜剑攻击甲，同时用细剑攻击乙。"},
        )

        receipt = service.gm_gameplay_tools.perform_character_action(
            context,
            {
                "action_type": "Attack",
                "actor": "双刀客",
                "target": "甲",
                "timing": "immediate",
                "details": {"dual_wield": True, "targets": ["甲", "乙"]},
                "evidence": "双刀客用青铜剑攻击甲，同时用细剑攻击乙",
            },
        )

        assert receipt.ok, receipt.message
        assert app.character_manager.get("甲").hp == 34
        assert app.character_manager.get("乙").hp == 34
        assert app.conflict_manager.state.current_actor() == "甲"
        assert "【双武器攻击】" in receipt.public_fallback_reply
        attack_logs = [
            entry
            for entry in app.conflict_manager.state.combat_log
            if entry.actor == "双刀客" and entry.event_type == ActionType.ATTACK.value
        ]
        assert len(attack_logs) == 1
