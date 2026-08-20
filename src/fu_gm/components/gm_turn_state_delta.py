from __future__ import annotations

"""Deterministic deltas for already-redacted, model-visible state.

This module deliberately knows nothing about FU-GM's authoritative runtime
objects.  Its only input is a JSON-compatible ``dict`` that has already been
projected and redacted by Python.  It can therefore compact repeated model
context without becoming another source of game truth.

The tracker keeps one immutable base projection and a minimal cumulative delta
from that base to the latest projection.  Lists are always replaced as a whole;
index-based list patches are intentionally forbidden because concurrent list
edits make those paths unstable.
"""

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


_MISSING = object()
_UNSET = object()
_DELTA_OPERATIONS = frozenset({"add", "replace", "remove"})


class GMStateDeltaError(ValueError):
    """Base exception for invalid projections and delta operations."""


class GMProjectionValidationError(GMStateDeltaError):
    """Raised when a purported model projection is not canonical JSON data."""


class GMStateDeltaVerificationError(GMStateDeltaError):
    """Raised when a delta cannot be applied exactly to its declared base."""


def _validate_json_value(
    value: object,
    *,
    ancestors: set[int] | None = None,
) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GMProjectionValidationError(
                "model projections cannot contain NaN or infinity"
            )
        return
    if isinstance(value, (dict, list)):
        active = ancestors if ancestors is not None else set()
        identity = id(value)
        if identity in active:
            raise GMProjectionValidationError(
                "model projections cannot contain cyclic containers"
            )
        active.add(identity)
        try:
            if isinstance(value, dict):
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise GMProjectionValidationError(
                            "model projection object keys must be strings"
                        )
                    _validate_json_value(item, ancestors=active)
            else:
                for item in value:
                    _validate_json_value(item, ancestors=active)
        finally:
            active.remove(identity)
        return
    raise GMProjectionValidationError(
        "model projections must contain only JSON-compatible values"
    )


def _validated_projection_copy(projection: object) -> dict[str, Any]:
    if not isinstance(projection, dict):
        raise GMProjectionValidationError(
            "a model projection must be a JSON-compatible dict"
        )
    _validate_json_value(projection)
    return copy.deepcopy(projection)


def _validated_value_copy(value: object) -> Any:
    _validate_json_value(value)
    return copy.deepcopy(value)


def _canonical_json_value(value: object) -> str:
    _validate_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_projection_json(projection: Mapping[str, Any]) -> str:
    """Return canonical JSON for an already-redacted model projection."""

    clean = _validated_projection_copy(projection)
    return _canonical_json_value(clean)


def projection_hash(projection: Mapping[str, Any]) -> str:
    """Return the full SHA-256 digest of a canonical model projection."""

    rendered = canonical_projection_json(projection)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _json_values_equal(left: object, right: object) -> bool:
    """Compare JSON values without Python's ``True == 1`` shortcuts."""

    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        if left.keys() != right.keys():
            return False
        return all(_json_values_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_values_equal(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, float):
        return _canonical_json_value(left) == _canonical_json_value(right)
    return bool(left == right)


def _escape_pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _unescape_pointer_token(token: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(token):
        character = token[index]
        if character != "~":
            result.append(character)
            index += 1
            continue
        if index + 1 >= len(token) or token[index + 1] not in {"0", "1"}:
            raise GMStateDeltaVerificationError("invalid JSON Pointer escape")
        result.append("~" if token[index + 1] == "0" else "/")
        index += 2
    return "".join(result)


def _child_path(parent: str, key: str) -> str:
    token = _escape_pointer_token(key)
    return f"{parent}/{token}" if parent else f"/{token}"


@dataclass(frozen=True)
class GMTurnStateDeltaBudget:
    """Budgets after which carrying a delta is more costly than rebasing."""

    max_ratio: float | None = 0.30
    max_operations: int | None = 64
    max_chars: int | None = 12_000

    def __post_init__(self) -> None:
        if self.max_ratio is not None:
            ratio = float(self.max_ratio)
            if not math.isfinite(ratio) or ratio < 0:
                raise ValueError("max_ratio must be a finite non-negative number")
            object.__setattr__(self, "max_ratio", ratio)
        for field_name in ("max_operations", "max_chars"):
            value = getattr(self, field_name)
            if value is None:
                continue
            if isinstance(value, bool) or int(value) != value or int(value) < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
            object.__setattr__(self, field_name, int(value))


@dataclass(frozen=True)
class GMTurnStateDeltaOp:
    """One typed JSON-object mutation with tool provenance."""

    sequence: int
    op: str
    path: str
    source_tool: str = ""
    value: Any = _MISSING

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or int(self.sequence) != self.sequence:
            raise GMStateDeltaError("delta operation sequence must be an integer")
        if int(self.sequence) < 0:
            raise GMStateDeltaError("delta operation sequence cannot be negative")
        object.__setattr__(self, "sequence", int(self.sequence))
        if self.op not in _DELTA_OPERATIONS:
            raise GMStateDeltaError("delta operation must be add, replace, or remove")
        if not isinstance(self.path, str) or (
            self.path and not self.path.startswith("/")
        ):
            raise GMStateDeltaError("delta operation path must be a JSON Pointer")
        if not isinstance(self.source_tool, str):
            raise GMStateDeltaError("source_tool must be a string")
        object.__setattr__(self, "source_tool", self.source_tool.strip())
        if self.op == "remove":
            if self.value is not _MISSING:
                raise GMStateDeltaError("remove operations cannot carry a value")
            return
        if self.value is _MISSING:
            raise GMStateDeltaError(f"{self.op} operations require a value")
        object.__setattr__(self, "value", _validated_value_copy(self.value))

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sequence": self.sequence,
            "op": self.op,
            "path": self.path,
            "source_tool": self.source_tool,
        }
        if self.op != "remove":
            payload["value"] = copy.deepcopy(self.value)
        return payload


@dataclass(frozen=True)
class _RawDeltaOp:
    op: str
    path: str
    value: Any = _MISSING


def _diff_json_objects(
    old: dict[str, Any],
    new: dict[str, Any],
    *,
    path: str = "",
) -> list[_RawDeltaOp]:
    operations: list[_RawDeltaOp] = []
    old_keys = set(old)
    new_keys = set(new)
    for key in sorted(old_keys - new_keys):
        operations.append(_RawDeltaOp("remove", _child_path(path, key)))
    for key in sorted(new_keys - old_keys):
        operations.append(
            _RawDeltaOp(
                "add",
                _child_path(path, key),
                copy.deepcopy(new[key]),
            )
        )
    for key in sorted(old_keys & new_keys):
        old_value = old[key]
        new_value = new[key]
        child = _child_path(path, key)
        if _json_values_equal(old_value, new_value):
            continue
        if isinstance(old_value, dict) and isinstance(new_value, dict):
            operations.extend(_diff_json_objects(old_value, new_value, path=child))
            continue
        # Lists are intentionally replaced wholesale.  We also use replace for
        # scalar/type changes so no list-index path can enter an envelope.
        operations.append(
            _RawDeltaOp("replace", child, copy.deepcopy(new_value))
        )
    return operations


def _coerce_operation(
    operation: GMTurnStateDeltaOp | Mapping[str, Any],
) -> GMTurnStateDeltaOp:
    if isinstance(operation, GMTurnStateDeltaOp):
        return GMTurnStateDeltaOp(
            sequence=operation.sequence,
            op=operation.op,
            path=operation.path,
            source_tool=operation.source_tool,
            **(
                {}
                if operation.op == "remove"
                else {"value": copy.deepcopy(operation.value)}
            ),
        )
    if not isinstance(operation, Mapping):
        raise GMStateDeltaError("delta operations must be mappings")
    op = operation.get("op")
    kwargs: dict[str, Any] = {
        "sequence": operation.get("sequence", 0),
        "op": op,
        "path": operation.get("path", ""),
        "source_tool": operation.get("source_tool", ""),
    }
    if op != "remove":
        if "value" not in operation:
            raise GMStateDeltaError(f"{op} operations require a value")
        kwargs["value"] = operation["value"]
    return GMTurnStateDeltaOp(**kwargs)


def _pointer_tokens(path: str) -> list[str]:
    if path == "":
        return []
    if not path.startswith("/"):
        raise GMStateDeltaVerificationError("delta path is not a JSON Pointer")
    return [_unescape_pointer_token(token) for token in path[1:].split("/")]


def apply_state_delta(
    base_projection: Mapping[str, Any],
    operations: Sequence[GMTurnStateDeltaOp | Mapping[str, Any]],
) -> dict[str, Any]:
    """Apply typed operations to a copy of ``base_projection``.

    Generated deltas never address list indices.  This verifier rejects such
    traversal too, preserving the whole-list replacement invariant.
    """

    result = _validated_projection_copy(base_projection)
    for raw_operation in operations:
        operation = _coerce_operation(raw_operation)
        tokens = _pointer_tokens(operation.path)
        if not tokens:
            if operation.op == "remove":
                raise GMStateDeltaVerificationError(
                    "the projection root cannot be removed"
                )
            if not isinstance(operation.value, dict):
                raise GMStateDeltaVerificationError(
                    "the projection root must remain a dict"
                )
            result = copy.deepcopy(operation.value)
            continue
        parent: dict[str, Any] = result
        for token in tokens[:-1]:
            if token not in parent:
                raise GMStateDeltaVerificationError(
                    "delta path has a missing parent"
                )
            child = parent[token]
            if not isinstance(child, dict):
                raise GMStateDeltaVerificationError(
                    "delta paths cannot traverse scalars or lists"
                )
            parent = child
        key = tokens[-1]
        if operation.op == "add":
            if key in parent:
                raise GMStateDeltaVerificationError(
                    "add operation targets an existing key"
                )
            parent[key] = copy.deepcopy(operation.value)
        elif operation.op == "replace":
            if key not in parent:
                raise GMStateDeltaVerificationError(
                    "replace operation targets a missing key"
                )
            parent[key] = copy.deepcopy(operation.value)
        else:
            if key not in parent:
                raise GMStateDeltaVerificationError(
                    "remove operation targets a missing key"
                )
            del parent[key]
    return _validated_projection_copy(result)


def _paths_overlap(left: str, right: str) -> bool:
    if left == right or not left or not right:
        return True
    return left.startswith(f"{right}/") or right.startswith(f"{left}/")


def _normalize_scopes(scopes: Iterable[object]) -> tuple[str, ...]:
    clean = {str(scope).strip() for scope in scopes if str(scope).strip()}
    return tuple(sorted(clean))


def _normalize_context_value(value: object, *, label: str) -> Any:
    try:
        return _validated_value_copy(value)
    except GMProjectionValidationError as exc:
        raise GMProjectionValidationError(
            f"{label} must be JSON-compatible"
        ) from exc


class GMTurnStateDeltaTracker:
    """Track a verified cumulative delta for one model-agent turn.

    ``base_revision`` is the external authoritative revision observed when the
    projection was built.  A caller that observes a different revision, scope,
    projection profile, or visibility must pass it to :meth:`update`; the
    tracker then rebases instead of combining data from incompatible views.
    """

    def __init__(
        self,
        base_projection: Mapping[str, Any],
        *,
        base_revision: object,
        projection_version: str,
        scopes: Iterable[object] = (),
        profile: str = "default",
        visibility: object = "public",
        budget: GMTurnStateDeltaBudget | None = None,
    ) -> None:
        if not isinstance(projection_version, str) or not projection_version.strip():
            raise ValueError("projection_version must be a non-empty string")
        if not isinstance(profile, str) or not profile.strip():
            raise ValueError("profile must be a non-empty string")
        self._budget = budget or GMTurnStateDeltaBudget()
        if not isinstance(self._budget, GMTurnStateDeltaBudget):
            raise TypeError("budget must be a GMTurnStateDeltaBudget")
        clean = _validated_projection_copy(base_projection)
        self._base_projection = clean
        self._current_projection = copy.deepcopy(clean)
        self._base_revision = _normalize_context_value(
            base_revision,
            label="base_revision",
        )
        self._projection_version = projection_version.strip()
        self._scopes = _normalize_scopes(scopes)
        self._profile = profile.strip()
        self._visibility = _normalize_context_value(
            visibility,
            label="visibility",
        )
        self._base_hash = projection_hash(clean)
        self._effective_hash = self._base_hash
        self._mutation_sequence = 0
        self._operations: list[GMTurnStateDeltaOp] = []
        self._provenance_history: list[GMTurnStateDeltaOp] = []
        self._reset_reason: str | None = None

    def update(
        self,
        current_projection: Mapping[str, Any],
        *,
        source_tool: str = "",
        base_revision: object = _UNSET,
        projection_version: object = _UNSET,
        scopes: object = _UNSET,
        profile: object = _UNSET,
        visibility: object = _UNSET,
    ) -> dict[str, Any]:
        """Accept a new redacted projection and return its safe envelope."""

        if not isinstance(source_tool, str):
            raise GMStateDeltaError("source_tool must be a string")
        clean = _validated_projection_copy(current_projection)
        context = self._resolved_context(
            base_revision=base_revision,
            projection_version=projection_version,
            scopes=scopes,
            profile=profile,
            visibility=visibility,
        )
        reset_reason = self._context_reset_reason(context)
        projection_changed = not _json_values_equal(
            clean,
            self._current_projection,
        )
        if reset_reason is not None:
            self._mutation_sequence += 1
            self._set_context(context)
            self._rebase(clean, reason=reset_reason)
            return self.envelope()
        if not projection_changed:
            return self.envelope()

        self._mutation_sequence += 1
        sequence = self._mutation_sequence
        step_changes = _diff_json_objects(self._current_projection, clean)
        for change in step_changes:
            self._provenance_history.append(
                GMTurnStateDeltaOp(
                    sequence=sequence,
                    op=change.op,
                    path=change.path,
                    source_tool=source_tool.strip(),
                    **(
                        {}
                        if change.op == "remove"
                        else {"value": copy.deepcopy(change.value)}
                    ),
                )
            )

        net_changes = _diff_json_objects(self._base_projection, clean)
        operations = [self._with_provenance(change) for change in net_changes]
        self._current_projection = copy.deepcopy(clean)
        self._effective_hash = projection_hash(clean)
        self._operations = operations
        self._reset_reason = None

        if not operations:
            # A complete rollback to the base should serialize as no delta and
            # should not leave stale provenance for future mutations.
            self._provenance_history.clear()
        else:
            budget_reason = self._budget_reset_reason(operations, clean)
            if budget_reason is not None:
                self._rebase(clean, reason=budget_reason)
                return self.envelope()

        self._assert_consistent()
        return self.envelope()

    def force_rebase(
        self,
        projection: Mapping[str, Any] | None = None,
        *,
        reason: str = "forced_rebase",
        base_revision: object = _UNSET,
        projection_version: object = _UNSET,
        scopes: object = _UNSET,
        profile: object = _UNSET,
        visibility: object = _UNSET,
    ) -> dict[str, Any]:
        """Explicitly replace the base, even when the view did not change."""

        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("force_rebase reason must be a non-empty string")
        clean = (
            copy.deepcopy(self._current_projection)
            if projection is None
            else _validated_projection_copy(projection)
        )
        context = self._resolved_context(
            base_revision=base_revision,
            projection_version=projection_version,
            scopes=scopes,
            profile=profile,
            visibility=visibility,
        )
        self._mutation_sequence += 1
        self._set_context(context)
        self._rebase(clean, reason=reason.strip())
        return self.envelope()

    def envelope(self) -> dict[str, Any]:
        """Return a detached, model-ready base-plus-delta envelope."""

        return {
            "base_revision": copy.deepcopy(self._base_revision),
            "projection_version": self._projection_version,
            "base_hash": self._base_hash,
            "effective_hash": self._effective_hash,
            "scopes": list(self._scopes),
            "profile": self._profile,
            "visibility": copy.deepcopy(self._visibility),
            "base_projection": copy.deepcopy(self._base_projection),
            "mutation_sequence": self._mutation_sequence,
            "ops": [operation.to_dict() for operation in self._operations],
            "reset_reason": self._reset_reason,
        }

    def apply(self) -> dict[str, Any]:
        """Reconstruct the current projection from the stored envelope."""

        return apply_state_delta(self._base_projection, self._operations)

    def verify(self, projection: Mapping[str, Any] | None = None) -> bool:
        """Verify base + delta, hashes, and optionally an expected projection."""

        expected = (
            self._current_projection
            if projection is None
            else _validated_projection_copy(projection)
        )
        try:
            applied = self.apply()
        except GMStateDeltaError:
            return False
        return (
            _json_values_equal(applied, expected)
            and projection_hash(self._base_projection) == self._base_hash
            and projection_hash(applied) == self._effective_hash
        )

    def _resolved_context(
        self,
        *,
        base_revision: object,
        projection_version: object,
        scopes: object,
        profile: object,
        visibility: object,
    ) -> dict[str, Any]:
        resolved_revision = (
            copy.deepcopy(self._base_revision)
            if base_revision is _UNSET
            else _normalize_context_value(base_revision, label="base_revision")
        )
        resolved_version = (
            self._projection_version
            if projection_version is _UNSET
            else projection_version
        )
        if not isinstance(resolved_version, str) or not resolved_version.strip():
            raise ValueError("projection_version must be a non-empty string")
        resolved_scopes = (
            self._scopes
            if scopes is _UNSET
            else _normalize_scopes(scopes)  # type: ignore[arg-type]
        )
        resolved_profile = self._profile if profile is _UNSET else profile
        if not isinstance(resolved_profile, str) or not resolved_profile.strip():
            raise ValueError("profile must be a non-empty string")
        resolved_visibility = (
            copy.deepcopy(self._visibility)
            if visibility is _UNSET
            else _normalize_context_value(visibility, label="visibility")
        )
        return {
            "base_revision": resolved_revision,
            "projection_version": resolved_version.strip(),
            "scopes": tuple(resolved_scopes),
            "profile": resolved_profile.strip(),
            "visibility": resolved_visibility,
        }

    def _context_reset_reason(self, context: Mapping[str, Any]) -> str | None:
        changes: list[str] = []
        if not _json_values_equal(
            context["base_revision"],
            self._base_revision,
        ):
            changes.append("revision")
        if context["projection_version"] != self._projection_version:
            changes.append("projection_version")
        if context["scopes"] != self._scopes:
            changes.append("scopes")
        if context["profile"] != self._profile:
            changes.append("profile")
        if not _json_values_equal(context["visibility"], self._visibility):
            changes.append("visibility")
        if not changes:
            return None
        if len(changes) == 1:
            return f"{changes[0]}_changed"
        return "context_changed:" + ",".join(changes)

    def _set_context(self, context: Mapping[str, Any]) -> None:
        self._base_revision = copy.deepcopy(context["base_revision"])
        self._projection_version = str(context["projection_version"])
        self._scopes = tuple(context["scopes"])
        self._profile = str(context["profile"])
        self._visibility = copy.deepcopy(context["visibility"])

    def _with_provenance(self, change: _RawDeltaOp) -> GMTurnStateDeltaOp:
        candidates = [
            event
            for event in self._provenance_history
            if _paths_overlap(event.path, change.path)
        ]
        source = max(candidates, key=lambda item: item.sequence) if candidates else None
        return GMTurnStateDeltaOp(
            sequence=(source.sequence if source else self._mutation_sequence),
            op=change.op,
            path=change.path,
            source_tool=(source.source_tool if source else ""),
            **(
                {}
                if change.op == "remove"
                else {"value": copy.deepcopy(change.value)}
            ),
        )

    def _budget_reset_reason(
        self,
        operations: Sequence[GMTurnStateDeltaOp],
        current_projection: Mapping[str, Any],
    ) -> str | None:
        if (
            self._budget.max_operations is not None
            and len(operations) > self._budget.max_operations
        ):
            return "delta_operation_budget_exceeded"
        operation_payload = [operation.to_dict() for operation in operations]
        delta_chars = len(_canonical_json_value(operation_payload))
        if (
            self._budget.max_chars is not None
            and delta_chars > self._budget.max_chars
        ):
            return "delta_char_budget_exceeded"
        if self._budget.max_ratio is not None:
            projection_chars = max(1, len(canonical_projection_json(current_projection)))
            if (delta_chars / projection_chars) > self._budget.max_ratio:
                return "delta_ratio_budget_exceeded"
        return None

    def _rebase(self, projection: Mapping[str, Any], *, reason: str) -> None:
        clean = _validated_projection_copy(projection)
        self._base_projection = clean
        self._current_projection = copy.deepcopy(clean)
        self._base_hash = projection_hash(clean)
        self._effective_hash = self._base_hash
        self._operations = []
        self._provenance_history = []
        self._reset_reason = reason
        self._assert_consistent()

    def _assert_consistent(self) -> None:
        if not self.verify(self._current_projection):
            raise GMStateDeltaVerificationError(
                "base projection and cumulative delta do not reconstruct current state"
            )


__all__ = [
    "GMProjectionValidationError",
    "GMStateDeltaError",
    "GMStateDeltaVerificationError",
    "GMTurnStateDeltaBudget",
    "GMTurnStateDeltaOp",
    "GMTurnStateDeltaTracker",
    "apply_state_delta",
    "canonical_projection_json",
    "projection_hash",
]
