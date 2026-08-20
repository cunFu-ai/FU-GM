from __future__ import annotations

import copy

import pytest

from fu_gm.components.gm_turn_state_delta import (
    GMProjectionValidationError,
    GMTurnStateDeltaBudget,
    GMTurnStateDeltaTracker,
    apply_state_delta,
    canonical_projection_json,
    projection_hash,
)


_PERMISSIVE_BUDGET = GMTurnStateDeltaBudget(
    max_ratio=None,
    max_operations=None,
    max_chars=None,
)


def _tracker(
    base: dict[str, object],
    *,
    budget: GMTurnStateDeltaBudget = _PERMISSIVE_BUDGET,
) -> GMTurnStateDeltaTracker:
    return GMTurnStateDeltaTracker(
        base,
        base_revision=7,
        projection_version="authority-v1",
        scopes=("scene", "kernel"),
        profile="scene_check",
        visibility={"audience": "public", "channel": "group-1"},
        budget=budget,
    )


def test_delta_adds_replaces_and_removes_nested_object_fields() -> None:
    base = {
        "keep": 1,
        "remove_me": "old",
        "scene": {"pressure": "low", "location": "牢门"},
    }
    current = {
        "keep": 1,
        "added": True,
        "scene": {"pressure": "high", "location": "牢门"},
    }
    tracker = _tracker(base)

    envelope = tracker.update(current, source_tool="declare_check_action")
    by_path = {operation["path"]: operation for operation in envelope["ops"]}

    assert by_path["/added"]["op"] == "add"
    assert by_path["/added"]["value"] is True
    assert by_path["/remove_me"]["op"] == "remove"
    assert "value" not in by_path["/remove_me"]
    assert by_path["/scene/pressure"] == {
        "sequence": 1,
        "op": "replace",
        "path": "/scene/pressure",
        "source_tool": "declare_check_action",
        "value": "high",
    }
    assert apply_state_delta(envelope["base_projection"], envelope["ops"]) == current
    assert tracker.verify(current) is True


def test_delta_is_cumulative_and_preserves_latest_tool_provenance_per_path() -> None:
    tracker = _tracker({"scene": {"pressure": 1, "clock": 1}})

    tracker.update(
        {"scene": {"pressure": 2, "clock": 1}},
        source_tool="advance_pressure",
    )
    envelope = tracker.update(
        {"scene": {"pressure": 2, "clock": 2}},
        source_tool="advance_clock",
    )

    assert envelope["mutation_sequence"] == 2
    assert {
        operation["path"]: (operation["sequence"], operation["source_tool"])
        for operation in envelope["ops"]
    } == {
        "/scene/clock": (2, "advance_clock"),
        "/scene/pressure": (1, "advance_pressure"),
    }
    assert tracker.apply() == {"scene": {"pressure": 2, "clock": 2}}


def test_lists_are_replaced_as_one_value_without_index_operations() -> None:
    tracker = _tracker({"participants": ["诺艾尔", "艾丽妮"]})

    envelope = tracker.update(
        {"participants": ["诺艾尔", "艾丽妮", "守卫"]},
        source_tool="enter_scene",
    )

    assert envelope["ops"] == [
        {
            "sequence": 1,
            "op": "replace",
            "path": "/participants",
            "source_tool": "enter_scene",
            "value": ["诺艾尔", "艾丽妮", "守卫"],
        }
    ]
    assert all("/0" not in operation["path"] for operation in envelope["ops"])


def test_canonical_json_and_hash_ignore_dict_insertion_order() -> None:
    left = {"地点": "牢门", "nested": {"b": 2, "a": 1}}
    right = {"nested": {"a": 1, "b": 2}, "地点": "牢门"}

    assert canonical_projection_json(left) == canonical_projection_json(right)
    assert projection_hash(left) == projection_hash(right)

    tracker = _tracker(left)
    initial = tracker.envelope()
    assert initial["base_hash"] == projection_hash(left)
    assert initial["effective_hash"] == projection_hash(left)

    updated = tracker.update({**right, "pressure": "守卫接近"}, source_tool="tick")
    reconstructed = apply_state_delta(
        updated["base_projection"],
        updated["ops"],
    )
    assert updated["effective_hash"] == projection_hash(reconstructed)


def test_returning_to_base_clears_cumulative_delta() -> None:
    base = {"scene": {"pressure": "low"}}
    tracker = _tracker(base)
    tracker.update(
        {"scene": {"pressure": "high"}},
        source_tool="advance_pressure",
    )

    envelope = tracker.update(base, source_tool="rollback_transaction")

    assert envelope["mutation_sequence"] == 2
    assert envelope["ops"] == []
    assert envelope["reset_reason"] is None
    assert envelope["base_hash"] == envelope["effective_hash"]
    assert tracker.apply() == base


@pytest.mark.parametrize(
    ("budget", "expected_reason"),
    [
        (
            GMTurnStateDeltaBudget(
                max_ratio=None,
                max_operations=1,
                max_chars=None,
            ),
            "delta_operation_budget_exceeded",
        ),
        (
            GMTurnStateDeltaBudget(
                max_ratio=None,
                max_operations=None,
                max_chars=1,
            ),
            "delta_char_budget_exceeded",
        ),
        (
            GMTurnStateDeltaBudget(
                max_ratio=0.0001,
                max_operations=None,
                max_chars=None,
            ),
            "delta_ratio_budget_exceeded",
        ),
    ],
)
def test_exceeding_any_delta_budget_rebases(
    budget: GMTurnStateDeltaBudget,
    expected_reason: str,
) -> None:
    tracker = _tracker(
        {"a": "x" * 100, "b": "y" * 100},
        budget=budget,
    )
    current = {"a": "changed-a", "b": "changed-b"}

    envelope = tracker.update(current, source_tool="large_mutation")

    assert envelope["reset_reason"] == expected_reason
    assert envelope["ops"] == []
    assert envelope["base_projection"] == current
    assert envelope["base_hash"] == envelope["effective_hash"]
    assert tracker.verify(current) is True


@pytest.mark.parametrize(
    ("context_change", "expected_reason", "expected_field"),
    [
        ({"base_revision": 8}, "revision_changed", ("base_revision", 8)),
        (
            {"projection_version": "authority-v2"},
            "projection_version_changed",
            ("projection_version", "authority-v2"),
        ),
        (
            {"scopes": ("kernel", "rules")},
            "scopes_changed",
            ("scopes", ["kernel", "rules"]),
        ),
        (
            {"profile": "rule_read"},
            "profile_changed",
            ("profile", "rule_read"),
        ),
        (
            {"visibility": {"audience": "private", "user": "u-1"}},
            "visibility_changed",
            (
                "visibility",
                {"audience": "private", "user": "u-1"},
            ),
        ),
    ],
)
def test_external_context_changes_force_a_rebase(
    context_change: dict[str, object],
    expected_reason: str,
    expected_field: tuple[str, object],
) -> None:
    tracker = _tracker({"fact": "old"})

    envelope = tracker.update(
        {"fact": "new"},
        source_tool="external_refresh",
        **context_change,
    )

    assert envelope["reset_reason"] == expected_reason
    assert envelope["ops"] == []
    assert envelope["base_projection"] == {"fact": "new"}
    field, expected = expected_field
    assert envelope[field] == expected


def test_force_rebase_is_explicit_and_keeps_auditable_reason() -> None:
    tracker = _tracker({"value": 1})
    tracker.update({"value": 2}, source_tool="increment")

    envelope = tracker.force_rebase(reason="scene_transition")

    assert envelope["reset_reason"] == "scene_transition"
    assert envelope["mutation_sequence"] == 2
    assert envelope["ops"] == []
    assert envelope["base_projection"] == {"value": 2}


def test_tracker_never_retains_or_returns_mutable_input_references() -> None:
    base = {"scene": {"facts": ["门关闭"]}}
    base_before = copy.deepcopy(base)
    tracker = _tracker(base)
    base["scene"]["facts"].append("外部偷偷修改")

    assert tracker.envelope()["base_projection"] == base_before

    current = {"scene": {"facts": ["门关闭", "锁松动"]}}
    current_before = copy.deepcopy(current)
    envelope = tracker.update(current, source_tool="inspect_door")
    current["scene"]["facts"].append("调用后修改")
    envelope["base_projection"]["scene"]["facts"].append("修改返回值")
    envelope["ops"][0]["value"].append("修改返回操作")

    fresh = tracker.envelope()
    assert tracker.apply() == current_before
    assert fresh["base_projection"] == base_before
    assert fresh["ops"][0]["value"] == ["门关闭", "锁松动"]


def test_json_pointer_escaping_round_trips_unusual_object_keys() -> None:
    tracker = _tracker({"a/b": {"til~de": 1}})

    envelope = tracker.update(
        {"a/b": {"til~de": 2}},
        source_tool="escaped_key_tool",
    )

    assert envelope["ops"][0]["path"] == "/a~1b/til~0de"
    assert tracker.apply() == {"a/b": {"til~de": 2}}


def test_non_json_or_non_dict_projection_is_rejected() -> None:
    with pytest.raises(GMProjectionValidationError):
        GMTurnStateDeltaTracker(
            {"bad": {1, 2}},
            base_revision=1,
            projection_version="v1",
        )
    with pytest.raises(GMProjectionValidationError):
        canonical_projection_json(["not", "a", "dict"])
