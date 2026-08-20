from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from fu_gm.context_compaction import StructuredContextCompactor


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_chars(value: object) -> int:
    return len(_json_text(value))


_WIRE_ROOT_ORDER = (
    # Stable request layout and capability contracts must lead the physical
    # JSON sent to the provider.  Prefix caches compare bytes, so this order
    # must survive StructuredContextCompactor's semantic retention ordering.
    "prompt_layout_version",
    "available_tools",
    "current_state_summary",
    "turn_state_delta",
    # Turn-local and progressively changing sections follow the stable prefix.
    "current_message",
    "current_turn",
    "recent_messages",
    "session",
    "request_context",
    "history",
    "runtime_feedback",
    # Compaction diagnostics describe this particular rendered request and
    # therefore belong after all ordinary model inputs.
    "_fu_gm_context_compaction",
)


@dataclass(frozen=True)
class GMContextBudget:
    """本轮模型视图的软预算，不限制权威存档与审计日志。"""

    warning_chars: int = 32000
    proactive_compaction_chars: int = 40000
    hard_chars: int = 48000
    target_chars: int = 32000
    recent_message_limit: int = 8
    history_limit: int = 8
    full_history_entries: int = 2

    @classmethod
    def from_env(cls) -> "GMContextBudget":
        warning = max(
            8000,
            int(os.environ.get("FU_GM_CONTEXT_WARNING_CHARS", "32000")),
        )
        proactive = max(
            warning,
            int(os.environ.get("FU_GM_CONTEXT_PROACTIVE_CHARS", "40000")),
        )
        hard = max(
            proactive,
            int(os.environ.get("FU_GM_CONTEXT_HARD_CHARS", "48000")),
        )
        target = max(
            6000,
            min(
                proactive,
                int(os.environ.get("FU_GM_CONTEXT_TARGET_CHARS", str(warning))),
            ),
        )
        return cls(
            warning_chars=warning,
            proactive_compaction_chars=proactive,
            hard_chars=hard,
            target_chars=target,
            recent_message_limit=max(
                4,
                int(os.environ.get("FU_GM_CONTEXT_RECENT_MESSAGE_LIMIT", "8")),
            ),
            history_limit=max(
                4,
                int(os.environ.get("FU_GM_CONTEXT_HISTORY_LIMIT", "8")),
            ),
            full_history_entries=max(
                1,
                int(os.environ.get("FU_GM_CONTEXT_FULL_HISTORY_ENTRIES", "2")),
            ),
        )


@dataclass(frozen=True)
class GMContextManifest:
    """不含玩家文本的上下文审计清单。"""

    strategy: tuple[str, ...]
    pressure: str
    original_chars: int
    projected_chars: int
    approximate_tokens: int
    section_chars: dict[str, int] = field(default_factory=dict)
    omitted: dict[str, int] = field(default_factory=dict)
    protected_paths: tuple[str, ...] = ()
    state_version: int = 0
    prompt_fingerprint: str = ""
    prompt_layout_version: str = ""
    layout_fingerprint: str = ""
    model_view_only: bool = True

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class GovernedGMContext:
    request: dict[str, object]
    rendered: str
    manifest: GMContextManifest


@dataclass(frozen=True)
class ModelResultProjection:
    result: dict[str, object]
    original_chars: int
    projected_chars: int
    omitted_keys: tuple[str, ...] = ()
    protected_budget_exceeded: bool = False


class GMToolResultBudgeter:
    """压缩工具的模型回执副本，同时保留事务恢复所需字段。"""

    _PROTECTED_KEYS = frozenset(
        {
            "action",
            "action_type",
            "actor",
            "allowed_followup_tools",
            "campaign_id",
            "campaign_state_version",
            "check",
            "check_result",
            "clock",
            "clock_change",
            "clock_changes",
            "condition_id",
            "commitment_id",
            "difficulty",
            "difficulty_level",
            "effect",
            "expected_actor",
            "fulfilled_condition",
            "gate",
            "hp_change",
            "hp_changes",
            "item_id",
            "mixed_message_followup_pending",
            "mp_change",
            "mp_changes",
            "natural_resolution_pending",
            "pending_decision",
            "pending_decisions",
            "public_facts",
            "required_followup_calls",
            "required_followup_mode",
            "required_followup_resolved",
            "required_followup_tools",
            "required_next_tool",
            "resource_changes",
            "roll",
            "rolled_back",
            "scene_id",
            "source_event",
            "source_event_id",
            "state_version",
            "suggested_arguments",
            "target",
            "triggered_commitment",
            "window_id",
        }
    )
    _PRIORITY_KEYS = (
        "name",
        "title",
        "status",
        "summary",
        "result",
        "current",
        "maximum",
        "location",
        "participants",
    )
    _PROFILES = (
        (1200, 20, 40, 6),
        (600, 12, 24, 5),
        (280, 6, 16, 4),
    )

    @classmethod
    def project(
        cls,
        result: dict[str, object],
        *,
        max_chars: int,
    ) -> ModelResultProjection:
        source = deepcopy(dict(result or {}))
        original_chars = _json_chars(source)
        if max_chars <= 0 or original_chars <= max_chars:
            return ModelResultProjection(source, original_chars, original_chars)

        protected = {
            key: deepcopy(value)
            for key, value in source.items()
            if key in cls._PROTECTED_KEYS
        }
        optional_keys = [
            key
            for key in cls._ordered_keys(source)
            if key not in cls._PROTECTED_KEYS
        ]
        best: dict[str, object] = dict(protected)
        best_omitted = tuple(optional_keys)
        for string_limit, list_limit, dict_limit, max_depth in cls._PROFILES:
            candidate = dict(protected)
            omitted: list[str] = []
            included_optional: list[str] = []
            for key in optional_keys:
                candidate[key] = cls._bounded(
                    source[key],
                    depth=0,
                    string_limit=string_limit,
                    list_limit=list_limit,
                    dict_limit=dict_limit,
                    max_depth=max_depth,
                )
                if _json_chars(candidate) > max_chars:
                    candidate.pop(key, None)
                    omitted.append(key)
                else:
                    included_optional.append(key)
            metadata = {
                "applied": True,
                "full_receipt_retained_by_host": True,
                "authoritative_state_refreshed_next_iteration": True,
                "omitted_keys": omitted,
            }
            candidate["_fu_gm_model_view"] = metadata
            while _json_chars(candidate) > max_chars and included_optional:
                removed = included_optional.pop()
                candidate.pop(removed, None)
                omitted.append(removed)
                metadata["omitted_keys"] = omitted
            if _json_chars(candidate) <= max_chars:
                best = candidate
                best_omitted = tuple(omitted)
                break
            best = candidate
            best_omitted = tuple(omitted)

        projected_chars = _json_chars(best)
        protected_exceeded = projected_chars > max_chars
        if protected_exceeded:
            best["_fu_gm_model_view"] = {
                "applied": True,
                "full_receipt_retained_by_host": True,
                "authoritative_state_refreshed_next_iteration": True,
                "omitted_keys": list(best_omitted),
                "budget_exceeded_by_protected_fields": True,
            }
            projected_chars = _json_chars(best)
        return ModelResultProjection(
            result=best,
            original_chars=original_chars,
            projected_chars=projected_chars,
            omitted_keys=best_omitted,
            protected_budget_exceeded=protected_exceeded,
        )

    @classmethod
    def _ordered_keys(cls, value: dict[str, object]) -> list[str]:
        rank = {key: index for index, key in enumerate(cls._PRIORITY_KEYS)}
        return sorted(value, key=lambda key: (rank.get(key, len(rank)), key))

    @classmethod
    def _bounded(
        cls,
        value: Any,
        *,
        depth: int,
        string_limit: int,
        list_limit: int,
        dict_limit: int,
        max_depth: int,
    ) -> Any:
        if isinstance(value, str):
            return cls._shorten(value, string_limit)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if depth >= max_depth:
            return cls._shorten(_json_text(value), string_limit)
        if isinstance(value, list):
            selected = value[:list_limit]
            rendered = [
                cls._bounded(
                    item,
                    depth=depth + 1,
                    string_limit=string_limit,
                    list_limit=list_limit,
                    dict_limit=dict_limit,
                    max_depth=max_depth,
                )
                for item in selected
            ]
            if len(value) > list_limit:
                rendered.append({"_fu_gm_omitted_items": len(value) - list_limit})
            return rendered
        if isinstance(value, dict):
            keys = list(value)[:dict_limit]
            rendered = {
                str(key): cls._bounded(
                    value[key],
                    depth=depth + 1,
                    string_limit=string_limit,
                    list_limit=list_limit,
                    dict_limit=dict_limit,
                    max_depth=max_depth,
                )
                for key in keys
            }
            if len(value) > dict_limit:
                rendered["_fu_gm_omitted_keys"] = len(value) - dict_limit
            return rendered
        return cls._shorten(str(value), string_limit)

    @staticmethod
    def _shorten(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        head = max(40, int(limit * 0.7))
        tail = max(20, limit - head - 18)
        return f"{value[:head]}...[省略{len(value) - head - tail}字]...{value[-tail:]}"


class GMContextGovernor:
    """为一次核心GM调用构建有预算、可审计的非权威模型视图。"""

    _PROTECTED_ROOT_PATHS = (
        "current_message",
        "current_turn",
        "session",
        "request_context",
        "runtime_feedback",
        "current_state_summary.runtime",
        "current_state_summary.processes",
        "current_state_summary.clocks.active",
        "current_state_summary.gameplay.pending_decisions",
        "current_state_summary.scene.public_facts",
        "current_state_summary.scene.private_situation",
        "current_state_summary.npcs.present_npcs",
    )
    _STATE_TOP_LEVEL_KEYS = frozenset(
        {
            "current_campaign_id",
            "message_campaign_id",
            "gate_status",
            "inspection_focus",
            "observation",
            "speaker_controlled_characters",
            "turn_participants",
        }
    )
    _SCENE_PROTECTED_KEYS = frozenset(
        {
            "active",
            "frame_active",
            "scene_id",
            "frame_source_scene_id",
            "name",
            "location",
            "participants",
            "participant_locations",
            "participant_positions",
            "participant_activities",
            "current_scene_is_camera_focus",
            "objective",
            "current_pressure",
            "public_facts",
            "revealed_clues",
            "private_situation",
            "working_brief",
            "unresolved_questions",
        }
    )
    _GAMEPLAY_PROTECTED_KEYS = frozenset(
        {
            "speaker",
            "controlled_characters",
            "characters",
            "pending_decisions",
            "silent_invocation_rights",
            "character_locations",
            "character_positions",
            "conflict",
        }
    )
    _NPC_PROTECTED_KEYS = frozenset(
        {
            "scene_id",
            "location",
            "present_npcs",
            "dialogue_authority",
        }
    )

    def __init__(self, budget: GMContextBudget | None = None) -> None:
        self.budget = budget or GMContextBudget()
        self._compactor = StructuredContextCompactor()

    def govern(
        self,
        request: dict[str, object],
        *,
        state_version: int = 0,
        prompt_layout_version: str = "",
        layout_fingerprint: str = "",
        protected_root_fields: Iterable[str] | None = None,
    ) -> GovernedGMContext:
        source = deepcopy(dict(request or {}))
        exact_root_fields = self._normalize_protected_root_fields(
            protected_root_fields
        )
        exact_roots = {
            key: deepcopy(source[key])
            for key in exact_root_fields
            if key in source
        }
        absent_exact_roots = frozenset(set(exact_root_fields) - set(source))
        original_chars = _json_chars(source)
        strategies: list[str] = []
        omitted: dict[str, int] = {}

        recent, recent_omitted = self._trim_recent_messages(source)
        if recent_omitted:
            source["recent_messages"] = recent
            omitted["recent_messages"] = recent_omitted
            strategies.append("recent-tail")

        history, history_omitted, history_compacted = self._microcompact_history(
            list(source.get("history") or [])
        )
        if history_omitted or history_compacted:
            source["history"] = history
            if history_omitted:
                omitted["history"] = history_omitted
            strategies.append("history-microcompact")

        # ``protected_root_fields`` is an exact boundary, unlike the ordinary
        # protected kernel below which merges selected authoritative fields
        # back into a compacted view.  State-delta hashes cover the complete
        # JSON values, so adding, removing, or shortening even one nested key
        # would invalidate them.
        self._restore_exact_roots(source, exact_roots, absent_exact_roots)

        rendered = _json_text(source)
        if len(rendered) >= self.budget.proactive_compaction_chars:
            kernel = self._protected_kernel(source)
            compacted = self._compactor.compact(
                rendered,
                max_chars=self.budget.target_chars,
            )
            if compacted.strategy not in {"unchanged", "not-json", "json-too-large"}:
                candidate = json.loads(compacted.text)
                self._deep_overlay(candidate, kernel)
                self._restore_exact_roots(
                    candidate,
                    exact_roots,
                    absent_exact_roots,
                )
                source = candidate
                rendered = _json_text(source)
                omitted["proactive_compaction_chars"] = compacted.omitted_chars
                strategies.append(compacted.strategy)

        if len(rendered) > self.budget.hard_chars:
            # 受保护核心可以合法超过软预算。再次压缩只处理其余模型视图；
            # 绝不为了迎合字符上限而裁断当前发言、回合或待决规则状态。
            kernel = self._protected_kernel(source)
            compacted = self._compactor.compact(
                rendered,
                max_chars=self.budget.hard_chars,
            )
            if compacted.strategy not in {"unchanged", "not-json", "json-too-large"}:
                candidate = json.loads(compacted.text)
                self._deep_overlay(candidate, kernel)
                self._restore_exact_roots(
                    candidate,
                    exact_roots,
                    absent_exact_roots,
                )
                source = candidate
                rendered = _json_text(source)
                omitted["hard_compaction_chars"] = compacted.omitted_chars
                strategies.append("hard-" + compacted.strategy)

        # Keep this final restore next to serialization as a fail-closed guard
        # against future compaction stages being inserted above it.
        self._restore_exact_roots(source, exact_roots, absent_exact_roots)
        source = self._canonical_wire_order(source)
        rendered = _json_text(source)

        projected_chars = len(rendered)
        if projected_chars > self.budget.hard_chars:
            pressure = "protected_kernel_exceeds_hard_limit"
        elif strategies and projected_chars >= self.budget.warning_chars:
            pressure = "compacted_warning"
        elif strategies:
            pressure = "compacted"
        elif projected_chars >= self.budget.proactive_compaction_chars:
            pressure = "proactive_threshold"
        elif projected_chars >= self.budget.warning_chars:
            pressure = "warning"
        else:
            pressure = "normal"
        section_chars = {
            str(key): _json_chars(value)
            for key, value in source.items()
        }
        fingerprint = hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16]
        manifest = GMContextManifest(
            strategy=tuple(strategies or ["unchanged"]),
            pressure=pressure,
            original_chars=original_chars,
            projected_chars=projected_chars,
            approximate_tokens=max(1, (projected_chars + 2) // 3),
            section_chars=section_chars,
            omitted=omitted,
            protected_paths=tuple(
                dict.fromkeys((*self._PROTECTED_ROOT_PATHS, *exact_root_fields))
            ),
            state_version=max(0, int(state_version or 0)),
            prompt_fingerprint=fingerprint,
            prompt_layout_version=str(prompt_layout_version or ""),
            layout_fingerprint=str(layout_fingerprint or ""),
        )
        return GovernedGMContext(source, rendered, manifest)

    @staticmethod
    def _canonical_wire_order(
        source: dict[str, object],
    ) -> dict[str, object]:
        """Return one deterministic provider-visible root-key order.

        StructuredContextCompactor intentionally prioritizes current-turn data
        while deciding what to retain.  That semantic priority is independent
        from wire order: after retention is complete, stable schemas and the
        verified state base lead so provider prefix caching remains useful.
        Values are reused without reconstruction, preserving exact protected
        roots such as current_state_summary and turn_state_delta.
        """

        ordered = {
            key: source[key]
            for key in _WIRE_ROOT_ORDER
            if key in source
        }
        ordered.update(
            {
                key: source[key]
                for key in sorted(source)
                if key not in ordered
            }
        )
        return ordered

    @staticmethod
    def _normalize_protected_root_fields(
        fields: Iterable[str] | None,
    ) -> tuple[str, ...]:
        if fields is None:
            return ()
        if isinstance(fields, str):
            raise TypeError("protected_root_fields must be an iterable of names")
        normalized: list[str] = []
        for field_name in fields:
            if not isinstance(field_name, str):
                raise TypeError("protected root field names must be strings")
            clean = field_name.strip()
            if not clean:
                raise ValueError("protected root field names cannot be empty")
            if clean not in normalized:
                normalized.append(clean)
        return tuple(normalized)

    @staticmethod
    def _restore_exact_roots(
        target: dict[str, object],
        exact_roots: dict[str, object],
        absent_roots: frozenset[str],
    ) -> None:
        for key in absent_roots:
            target.pop(key, None)
        for key, value in exact_roots.items():
            target[key] = deepcopy(value)

    def _trim_recent_messages(
        self,
        request: dict[str, object],
    ) -> tuple[list[object], int]:
        rows = list(request.get("recent_messages") or [])
        if len(rows) <= self.budget.recent_message_limit:
            return rows, 0
        protected_ids = self._protected_message_ids(request)
        selected_indices = set(
            range(
                max(0, len(rows) - self.budget.recent_message_limit),
                len(rows),
            )
        )
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            message_id = str(row.get("message_id") or "").strip()
            if message_id and message_id in protected_ids:
                selected_indices.add(index)
        selected = [row for index, row in enumerate(rows) if index in selected_indices]
        return selected, max(0, len(rows) - len(selected))

    @staticmethod
    def _protected_message_ids(request: dict[str, object]) -> set[str]:
        result: set[str] = set()
        request_context = request.get("request_context")
        if isinstance(request_context, dict):
            quoted = request_context.get("quoted_message")
            if isinstance(quoted, dict):
                message_id = str(quoted.get("message_id") or "").strip()
                if message_id:
                    result.add(message_id)
        current_turn = request.get("current_turn")
        if isinstance(current_turn, dict):
            for event in list(current_turn.get("events") or []):
                if not isinstance(event, dict):
                    continue
                for key in ("message_id", "quoted_message_id"):
                    message_id = str(event.get(key) or "").strip()
                    if message_id:
                        result.add(message_id)
        return result

    def _microcompact_history(
        self,
        history: list[object],
    ) -> tuple[list[object], int, int]:
        if not history:
            return [], 0, 0
        latest_error_by_code: dict[str, int] = {}
        for index, item in enumerate(history):
            code = self._protocol_error_code(item)
            if code:
                latest_error_by_code[code] = index

        keep_from = max(0, len(history) - self.budget.history_limit)
        full_from = max(0, len(history) - self.budget.full_history_entries)
        selected: list[object] = []
        omitted = 0
        compacted = 0
        for index, item in enumerate(history):
            code = self._protocol_error_code(item)
            if code and latest_error_by_code.get(code) != index:
                omitted += 1
                continue
            requires_full = self._history_requires_full(item)
            if index < keep_from and not requires_full:
                omitted += 1
                continue
            if index >= full_from or requires_full:
                selected.append(deepcopy(item))
                continue
            selected.append(self._compact_history_entry(item))
            compacted += 1
        return selected, omitted, compacted

    @staticmethod
    def _protocol_error_code(item: object) -> str:
        if not isinstance(item, dict):
            return ""
        error = item.get("protocol_error")
        if not isinstance(error, dict):
            return ""
        return str(error.get("error_code") or "").strip()

    @staticmethod
    def _history_requires_full(item: object) -> bool:
        if not isinstance(item, dict):
            return False
        receipt = item.get("tool_receipt")
        if isinstance(receipt, dict):
            if receipt.get("ok") is False:
                return True
            result = receipt.get("result")
            if isinstance(result, dict) and any(
                result.get(key) not in (None, "", [], {}, False)
                for key in (
                    "required_followup_tools",
                    "required_followup_calls",
                    "pending_decision",
                    "pending_decisions",
                    "natural_resolution_pending",
                )
            ):
                return True
        return False

    @classmethod
    def _compact_history_entry(cls, item: object) -> object:
        if not isinstance(item, dict):
            return item
        result: dict[str, object] = {}
        decision = item.get("model_decision")
        if isinstance(decision, dict):
            compact_decision = {
                key: deepcopy(decision[key])
                for key in (
                    "decision",
                    "tool_name",
                    "batch_index",
                )
                if key in decision
            }
            arguments = decision.get("arguments")
            if isinstance(arguments, dict):
                compact_decision["argument_identity"] = {
                    key: deepcopy(arguments[key])
                    for key in (
                        "source_event_id",
                        "actor",
                        "target",
                        "name",
                        "window_id",
                        "scene_id",
                    )
                    if key in arguments
                }
            result["model_decision"] = compact_decision
        receipt = item.get("tool_receipt")
        if isinstance(receipt, dict):
            compact_receipt = {
                key: deepcopy(receipt[key])
                for key in (
                    "tool_name",
                    "ok",
                    "error_code",
                    "message",
                    "correction_hint",
                    "retryable",
                    "state_changed",
                    "lock_public_reply",
                )
                if key in receipt
            }
            receipt_result = receipt.get("result")
            if isinstance(receipt_result, dict):
                compact_receipt["result"] = GMToolResultBudgeter.project(
                    receipt_result,
                    max_chars=1800,
                ).result
            result["tool_receipt"] = compact_receipt
        for key in ("protocol_error",):
            if key in item:
                result[key] = deepcopy(item[key])
        return result or deepcopy(item)

    def _protected_kernel(
        self,
        request: dict[str, object],
    ) -> dict[str, object]:
        kernel = {
            key: deepcopy(request[key])
            for key in (
                "current_message",
                "current_turn",
                "session",
                "request_context",
                "runtime_feedback",
            )
            if key in request
        }
        state = request.get("current_state_summary")
        if not isinstance(state, dict):
            return kernel
        protected_state = {
            key: deepcopy(value)
            for key, value in state.items()
            if key in self._STATE_TOP_LEVEL_KEYS
        }
        session = request.get("session")
        gate_status = (
            str(session.get("gate_status") or "").strip()
            if isinstance(session, dict)
            else ""
        )
        if gate_status in {"pre_session", "session_zero"}:
            for key in ("session_zero", "hero_drafts"):
                if key in state:
                    protected_state[key] = deepcopy(state[key])
        for key in ("runtime", "processes", "clocks"):
            if key in state:
                protected_state[key] = deepcopy(state[key])
        self._copy_selected_section(
            state,
            protected_state,
            "scene",
            self._SCENE_PROTECTED_KEYS,
        )
        self._copy_selected_section(
            state,
            protected_state,
            "gameplay",
            self._GAMEPLAY_PROTECTED_KEYS,
        )
        self._copy_selected_section(
            state,
            protected_state,
            "npcs",
            self._NPC_PROTECTED_KEYS,
        )
        supervisor = state.get("supervisor")
        if isinstance(supervisor, dict):
            protected_state["supervisor"] = {
                key: deepcopy(supervisor[key])
                for key in ("active_alerts", "open_circuits")
                if key in supervisor
            }
        kernel["current_state_summary"] = protected_state
        return kernel

    @staticmethod
    def _copy_selected_section(
        source: dict[str, object],
        target: dict[str, object],
        section: str,
        keys: frozenset[str],
    ) -> None:
        value = source.get(section)
        if not isinstance(value, dict):
            return
        target[section] = {
            key: deepcopy(item)
            for key, item in value.items()
            if key in keys
        }

    @classmethod
    def _deep_overlay(
        cls,
        target: dict[str, object],
        protected: dict[str, object],
    ) -> None:
        for key, value in protected.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                cls._deep_overlay(target[key], value)  # type: ignore[arg-type]
            else:
                target[key] = deepcopy(value)
