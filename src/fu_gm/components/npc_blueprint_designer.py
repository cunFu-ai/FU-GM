from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from contextlib import contextmanager
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
)
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from fu_gm.core_bestiary import (
    CORE_BESTIARY_ENTRIES,
    BestiaryEntry,
    bestiary_entry_by_name,
)
from fu_gm.components.bestiary_runtime_profiles import (
    ability_profiles_for_bestiary,
    attack_options_for_bestiary,
    attack_rules_for_bestiary,
)
from fu_gm.llm_client import ChatMessage
from fu_gm.models import (
    Affinity,
    NPCCombatBlueprint,
    NPCAttackProfile,
    NPCPersona,
    NPCSpellProfile,
    StatusEffect,
)
from fu_gm.npc_design_library import SPECIES_RULES, normalize_species


_SPECIES_ZH_TO_SLUG = {
    "野兽": "beast",
    "构装体": "construct",
    "恶魔": "demon",
    "元素": "elemental",
    "人型": "humanoid",
    "怪物": "monster",
    "植物": "plant",
    "不死族": "undead",
}

_BESTIARY_REVISION = hashlib.sha256(
    json.dumps(
        [asdict(entry) for entry in CORE_BESTIARY_ENTRIES],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
).hexdigest()[:16]


class NPCBlueprintDesigner:
    """Prepare private NPC combat sheets without expanding the core GM prompt.

    The optional model only chooses among a bounded set of legal bestiary
    candidates and proposes a tactical pattern.  All numbers are compiled from
    the core reference and validated locally, so an invented JSON field can
    never become authoritative state.
    """

    _SYSTEM_PROMPT = """你是《最终物语》NPC继承/改皮助手。
只能从候选模板中选择一个最贴合当前NPC概念、等级与场景职责的模板。
不要创造、修改或计算任何数值。输出一个JSON对象：
{
  "template_name": "候选模板准确名称",
  "selection_reason": "一句内部理由",
  "tactics": {
    "opening": "首轮倾向",
    "cycle": ["常规行动倾向1", "常规行动倾向2"],
    "crisis": "危机状态倾向",
    "telegraph": "强力行动前如何给玩家清晰预兆",
    "retreat": "何时撤退、投降或改变目标",
    "protect_policy": "always、priority或never",
    "protect_priority": ["优先保护的具体对象或角色特征，最多3项"]
  }
}
这些内容只给GM后台使用，不写玩家可见叙事。"""
    _PROMPT_SCHEMA_REVISION = "npc-blueprint-prompt-v2"
    _BLUEPRINT_SCHEMA_REVISION = "npc-combat-blueprint-v2"
    _DEFAULT_MODEL_TIMEOUT_SECONDS = 12.0
    _MIN_MODEL_BUDGET_SECONDS = 0.25
    _FOREGROUND_FALLBACK_RESERVE_SECONDS = 0.1
    _PUBLICATION_LEASE_WAIT_SECONDS = 30.0

    def __init__(
        self,
        world_state: Any,
        *,
        client: Any | None = None,
        model: str = "",
        current_scene_id: Callable[[], str] | None = None,
        max_workers: int = 1,
        background_defer_seconds: float = 0.0,
        model_timeout_seconds: float = _DEFAULT_MODEL_TIMEOUT_SECONDS,
        max_output_tokens: int = 900,
        publication_lock: Any | None = None,
    ) -> None:
        self.world_state = world_state
        self.client = client
        self.model = str(model or "").strip()
        self.current_scene_id = current_scene_id or (lambda: "")
        self.background_defer_seconds = max(
            0.0,
            float(background_defer_seconds),
        )
        self.model_timeout_seconds = max(
            self._MIN_MODEL_BUDGET_SECONDS,
            float(model_timeout_seconds),
        )
        self.max_output_tokens = max(256, int(max_output_tokens))
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="fu-gm-npc-design",
        )
        self._lock = threading.RLock()
        self._publication_lock = publication_lock or threading.RLock()
        self._publication_runtime: Any | None = None
        self._jobs: dict[str, dict[str, Any]] = {}
        self._latest_signature_by_npc: dict[str, str] = {}
        self._background_batch_not_before = 0.0

    def bind_runtime_publication(self, runtime: Any) -> None:
        """Bind final validation/publication to one campaign authority lock."""

        lock = getattr(runtime, "transaction_lock", None)
        if lock is None:
            raise ValueError("NPC蓝图发布需要runtime transaction lock。")
        with self._lock:
            self._publication_lock = lock
            self._publication_runtime = runtime

    def submit(
        self,
        persona: NPCPersona,
        *,
        level: int,
        species: str = "",
        rank: str = "soldier",
        champion_value: int = 1,
        combat_side: str = "enemy",
        is_villain: bool = False,
        ultima_points: int = 0,
        scene_id: str = "",
        scene_context: dict[str, Any] | None = None,
        preferred_template: str = "",
        background: bool = True,
        allow_scene_agnostic_reuse: bool = False,
        deadline: float | None = None,
        publication_lease_owner: str = "",
    ) -> dict[str, Any]:
        requested_deadline = self._normalize_deadline(deadline)
        clean_lease_owner = str(publication_lease_owner or "").strip()
        raw_species = str(species or "").strip()
        request = {
            "persona": persona,
            "npc_id": str(persona.npc_id or "").strip(),
            "level": max(5, min(60, int(level))),
            # An explicitly authored species remains authoritative. Planned
            # cast members may omit it; the isolated inheritance model then
            # chooses only among bounded core-bestiary candidates.
            "species": (
                normalize_species(raw_species).slug
                if raw_species
                else ""
            ),
            "rank": self._rank(rank),
            "champion_value": max(2, int(champion_value)) if self._rank(rank) == "champion" else 1,
            "combat_side": "ally" if str(combat_side).strip().lower() == "ally" else "enemy",
            "is_villain": bool(is_villain),
            "ultima_points": max(0, int(ultima_points)),
            "scene_id": str(scene_id or "").strip(),
            "scene_context": self._bounded_scene_context(scene_context),
            "preferred_template": str(preferred_template or "").strip(),
            "persona_revision": self.persona_revision(persona),
            "model": self.model,
            "model_enabled": bool(self.client is not None and self.model),
            "prompt_schema_revision": self._PROMPT_SCHEMA_REVISION,
            "blueprint_schema_revision": self._BLUEPRINT_SCHEMA_REVISION,
            "bestiary_revision": _BESTIARY_REVISION,
        }
        if request["is_villain"] and request["ultima_points"] < 1:
            request["ultima_points"] = 1
        if not request["is_villain"]:
            request["ultima_points"] = 0

        request_signature = self._request_signature(request)
        request["request_signature"] = request_signature
        request["deadline_monotonic"] = requested_deadline
        join_job_id = ""
        job_id = ""
        with self._lock:
            existing = self.world_state.npc_combat_blueprints.get(persona.name)
            if (
                existing is not None
                and existing.status == "ready"
                and (
                    (
                        existing.request_signature == request_signature
                        and existing.prompt_schema_revision
                        == self._PROMPT_SCHEMA_REVISION
                        and existing.blueprint_schema_revision
                        == self._BLUEPRINT_SCHEMA_REVISION
                    )
                    or (
                        allow_scene_agnostic_reuse
                        and self._scene_agnostic_reuse_matches(existing, request)
                    )
                )
            ):
                self._latest_signature_by_npc[persona.name] = request_signature
                return self._job_view(
                    {
                        "job_id": "",
                        "status": "ready",
                        "npc_name": persona.name,
                        "blueprint": existing,
                        "reused": True,
                    }
                )

            pending = next(
                (
                    record
                    for record in self._jobs.values()
                    if record.get("request_signature") == request_signature
                    and record.get("status") in {"queued", "running"}
                ),
                None,
            )
            if pending is not None:
                if background:
                    reused = self._job_view(pending)
                    reused["reused"] = True
                    return reused
                priority_event = pending.get("priority_event")
                if isinstance(priority_event, threading.Event):
                    priority_event.set()
                # A synchronous consumer promotes the shared job onto its own
                # outer request budget. Pure background jobs intentionally use
                # the designer's short local model budget when they eventually
                # run, so a scene-prewarm defer does not consume a player turn.
                pending["deadline_monotonic"] = requested_deadline
                pending["publication_lease_owner"] = clean_lease_owner
                join_job_id = str(pending.get("job_id") or "")
            else:
                job_id = f"npc-design-{uuid4().hex}"
                not_before_monotonic = 0.0
                if background and self.background_defer_seconds > 0:
                    has_active_background_batch = any(
                        bool(item.get("background"))
                        and item.get("status") in {"queued", "running"}
                        for item in self._jobs.values()
                    )
                    if not has_active_background_batch:
                        self._background_batch_not_before = (
                            time.monotonic() + self.background_defer_seconds
                        )
                    not_before_monotonic = self._background_batch_not_before
                record: dict[str, Any] = {
                    "job_id": job_id,
                    "status": "queued" if background else "running",
                    "npc_name": persona.name,
                    "scene_id": request["scene_id"],
                    "persona_revision": request["persona_revision"],
                    "blueprint": None,
                    "error": "",
                    "reused": False,
                    "request_signature": request_signature,
                    "priority_event": threading.Event(),
                    "completion_event": threading.Event(),
                    "background": bool(background),
                    "not_before_monotonic": not_before_monotonic,
                    "deadline_monotonic": (
                        None if background else requested_deadline
                    ),
                    "publication_lease_owner": clean_lease_owner,
                    "fallback_used": False,
                    "publication_source": "",
                }
                self._jobs[job_id] = record
                self._latest_signature_by_npc[persona.name] = request_signature
                if background:
                    future = self._executor.submit(self._run_job, job_id, request)
                    record["future"] = future
                    return self._job_view(record)

        if join_job_id:
            wait_timeout = self._foreground_wait_timeout(requested_deadline)
            try:
                result = self.wait(join_job_id, timeout=wait_timeout)
            except TimeoutError:
                result = self._publish_deterministic_fallback(
                    join_job_id,
                    request,
                    publication_lease_owner=clean_lease_owner,
                    reason=(
                        "前台等待预算将尽，已跳过仍在运行的选模请求并采用确定性继承。"
                    ),
                )
            result["reused"] = True
            return result

        # A foreground-only request deliberately runs on the caller thread. It
        # must never sit behind the single background worker.
        self._run_job(job_id, request)
        return self.poll(job_id)

    def poll(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._jobs.get(str(job_id or "").strip())
            if record is None:
                return {
                    "job_id": str(job_id or ""),
                    "status": "missing",
                    "error": "NPC设计任务不存在。",
                }
            return self._job_view(record)

    def wait(self, job_id: str, timeout: float | None = None) -> dict[str, Any]:
        with self._lock:
            record = self._jobs.get(str(job_id or "").strip())
            future: Future[Any] | None = record.get("future") if record else None
            completion_event: threading.Event | None = (
                record.get("completion_event") if record else None
            )
        if future is not None:
            try:
                future.result(timeout=timeout)
            except FutureTimeoutError as exc:
                raise TimeoutError("NPC规则档案等待预算耗尽。") from exc
        elif isinstance(completion_event, threading.Event):
            completed = completion_event.wait(timeout=timeout)
            if not completed:
                raise TimeoutError("NPC规则档案等待预算耗尽。")
        return self.poll(job_id)

    def wait_for_all(self, timeout: float | None = None) -> list[dict[str, Any]]:
        """等待调用时已经排队的NPC设计任务，并返回稳定的任务快照。"""

        with self._lock:
            job_ids = list(self._jobs)
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        results: list[dict[str, Any]] = []
        for job_id in job_ids:
            remaining = None
            if deadline is not None:
                remaining = max(0.0, deadline - time.monotonic())
            results.append(self.wait(job_id, timeout=remaining))
        return results

    def prepare_sync(self, persona: NPCPersona, **kwargs: Any) -> NPCCombatBlueprint:
        result = self.submit(persona, background=False, **kwargs)
        if result.get("status") != "ready":
            raise ValueError(str(result.get("error") or "NPC规则档案设计失败。"))
        with self._publication_lock:
            blueprint = self.world_state.npc_combat_blueprints.get(persona.name)
            if blueprint is None:
                raise ValueError("NPC规则档案没有写入私有状态。")
            expected_id = str(result.get("blueprint_id") or "")
            if expected_id and blueprint.blueprint_id != expected_id:
                raise ValueError("NPC规则档案在读取前已被更新请求取代。")
            return blueprint

    def _run_job(self, job_id: str, request: dict[str, Any]) -> None:
        with self._lock:
            record = self._jobs[job_id]
            priority_event = record.get("priority_event")
            is_background = bool(record.get("background"))
        if (
            is_background
            and isinstance(priority_event, threading.Event)
        ):
            # Scene-start prewarming is deliberately lower priority than the
            # reply which opened that scene. A later synchronous consumer can
            # promote this exact job by setting the event and still shares the
            # same single-flight result.
            remaining = max(
                0.0,
                float(record.get("not_before_monotonic") or 0.0)
                - time.monotonic(),
            )
            if remaining > 0:
                priority_event.wait(timeout=remaining)
        with self._lock:
            record = self._jobs[job_id]
            record["status"] = "running"
            request = {
                **request,
                "deadline_monotonic": record.get("deadline_monotonic"),
            }
        try:
            blueprint = self._design(request)
            with self._lock:
                lease_owner = str(
                    self._jobs[job_id].get("publication_lease_owner") or ""
                )
            self._publish_if_current(
                job_id,
                request,
                blueprint,
                publication_lease_owner=lease_owner,
                fallback_used=False,
            )
        except Exception as exc:  # pragma: no cover - exercised through failure contract tests
            with self._lock:
                record = self._jobs[job_id]
                if record.get("status") not in {"ready", "stale"}:
                    record["status"] = "failed"
                    record["error"] = str(exc)
        finally:
            with self._lock:
                completion_event = self._jobs[job_id].get("completion_event")
                if isinstance(completion_event, threading.Event):
                    completion_event.set()

    def _design(self, request: dict[str, Any]) -> NPCCombatBlueprint:
        persona: NPCPersona = request["persona"]
        candidates = self._candidate_entries(
            persona,
            requested_level=int(request["level"]),
            species=str(request["species"]),
            preferred_template=str(request["preferred_template"]),
        )
        if not candidates:
            raise ValueError("核心生物图鉴中没有可用于继承的候选模板。")
        selected = candidates[0]
        tactics: dict[str, Any] = {}
        validation_notes: list[str] = []
        if self.client is not None and self.model:
            try:
                proposal = self._model_proposal(persona, request, candidates)
                proposed_name = str(proposal.get("template_name") or "").strip()
                selected = next(
                    (entry for entry in candidates if entry.name == proposed_name),
                    selected,
                )
                tactics = self._validate_tactics(proposal.get("tactics"))
                reason = str(proposal.get("selection_reason") or "").strip()
                if reason:
                    validation_notes.append(f"模型选模理由：{reason}")
            except Exception as exc:
                validation_notes.append(f"独立选模模型不可用，已采用确定性继承：{exc}")
        if not tactics:
            tactics = self._default_tactics(selected)
        return self._compile_blueprint(
            selected,
            request=request,
            tactics=tactics,
            validation_notes=validation_notes,
        )

    def _design_deterministic(
        self,
        request: dict[str, Any],
        *,
        reason: str,
    ) -> NPCCombatBlueprint:
        persona: NPCPersona = request["persona"]
        candidates = self._candidate_entries(
            persona,
            requested_level=int(request["level"]),
            species=str(request["species"]),
            preferred_template=str(request["preferred_template"]),
        )
        if not candidates:
            raise ValueError("核心生物图鉴中没有可用于继承的候选模板。")
        selected = candidates[0]
        return self._compile_blueprint(
            selected,
            request=request,
            tactics=self._default_tactics(selected),
            validation_notes=[reason],
        )

    def _publish_deterministic_fallback(
        self,
        job_id: str,
        request: dict[str, Any],
        *,
        publication_lease_owner: str,
        reason: str,
    ) -> dict[str, Any]:
        blueprint = self._design_deterministic(request, reason=reason)
        self._publish_if_current(
            job_id,
            request,
            blueprint,
            publication_lease_owner=publication_lease_owner,
            fallback_used=True,
        )
        return self.poll(job_id)

    def _publish_if_current(
        self,
        job_id: str,
        request: dict[str, Any],
        blueprint: NPCCombatBlueprint,
        *,
        publication_lease_owner: str,
        fallback_used: bool,
    ) -> None:
        with self._publication_transaction(
            publication_lease_owner=publication_lease_owner,
            deadline=request.get("deadline_monotonic"),
        ) as publication_allowed:
            with self._lock:
                record = self._jobs[job_id]
                if record.get("status") == "ready":
                    return
                if not publication_allowed:
                    record["status"] = "stale"
                    record["error"] = (
                        "NPC规则档案完成时权威写事务仍未释放，已放弃过时发布。"
                    )
                    return
                stale_reason = self._publication_stale_reason_locked(
                    record,
                    request,
                    blueprint,
                )
                if stale_reason:
                    record["status"] = "stale"
                    record["error"] = stale_reason
                    return
                self.world_state.npc_combat_blueprints[
                    blueprint.npc_name
                ] = blueprint
                record["status"] = "ready"
                record["blueprint"] = blueprint
                record["fallback_used"] = bool(fallback_used)
                record["publication_source"] = (
                    "foreground_deterministic_fallback"
                    if fallback_used
                    else "completed_design"
                )

    def _publication_stale_reason_locked(
        self,
        record: dict[str, Any],
        request: dict[str, Any],
        blueprint: NPCCombatBlueprint,
    ) -> str:
        expected_signature = str(request.get("request_signature") or "")
        if not expected_signature:
            return "NPC规则档案请求缺少签名。"
        if str(record.get("request_signature") or "") != expected_signature:
            return "NPC规则档案任务签名在设计期间发生变化。"
        if blueprint.request_signature != expected_signature:
            return "NPC规则档案结果签名与任务不一致。"
        if self._request_signature(request) != expected_signature:
            return "NPC规则档案请求内容在设计期间发生变化。"
        if (
            self._latest_signature_by_npc.get(blueprint.npc_name)
            != expected_signature
        ):
            return "NPC已有更新的规则档案请求，旧结果不再发布。"
        current_persona = self.world_state.npc_personas.get(blueprint.npc_name)
        if current_persona is None:
            return "NPC人格档案已不存在。"
        if str(current_persona.npc_id or "") != str(request.get("npc_id") or ""):
            return "NPC权威身份在设计期间发生变化。"
        if self.persona_revision(current_persona) != blueprint.persona_revision:
            return "NPC人格档案在设计期间发生变化。"
        if blueprint.scene_id:
            current_scene = str(self.current_scene_id() or "").strip()
            if current_scene != blueprint.scene_id:
                return "NPC所属场景在设计期间已经切换。"
        return ""

    @contextmanager
    def _publication_transaction(
        self,
        *,
        publication_lease_owner: str,
        deadline: object,
    ):
        runtime = self._publication_runtime
        if runtime is None:
            with self._publication_lock:
                yield True
            return
        condition = getattr(runtime, "write_lease_condition", None)
        if condition is None:
            with self._publication_lock:
                yield not bool(getattr(runtime, "retired", False))
            return
        requested_owner = str(publication_lease_owner or "").strip()
        cutoff = self._child_deadline(
            deadline,
            timeout_seconds=self._PUBLICATION_LEASE_WAIT_SECONDS,
        )
        with condition:
            while True:
                active_owner = str(
                    getattr(runtime, "write_lease_owner", "") or ""
                ).strip()
                if not active_owner or (
                    requested_owner and active_owner == requested_owner
                ):
                    break
                remaining = cutoff - time.monotonic()
                if remaining <= 0:
                    yield False
                    return
                condition.wait(timeout=min(1.0, remaining))
            yield not bool(getattr(runtime, "retired", False))

    def _candidate_entries(
        self,
        persona: NPCPersona,
        *,
        requested_level: int,
        species: str,
        preferred_template: str,
    ) -> list[BestiaryEntry]:
        if preferred_template:
            preferred = bestiary_entry_by_name(preferred_template)
            if preferred is None:
                raise ValueError(f"核心生物图鉴中没有模板【{preferred_template}】。")
            return [preferred]
        species_zh = SPECIES_RULES[species].name if species else ""
        text = " ".join(
            [
                persona.name,
                persona.public_identity,
                persona.role_in_story,
                persona.core_drive,
                persona.combat_style,
                *persona.traits,
            ]
        )

        def score(entry: BestiaryEntry) -> tuple[int, int, str]:
            overlap = sum(
                1
                for token in (*entry.typical_traits, entry.name)
                if token and token in text
            )
            return (abs(entry.level - requested_level), -overlap, entry.name)

        if species_zh:
            pool = [
                entry
                for entry in CORE_BESTIARY_ENTRIES
                if entry.species == species_zh
            ]
            return sorted(pool, key=score)[:5]

        # Keep an unauthored species decision genuinely open without sending
        # all 56 stat blocks to the model. One nearest candidate per species
        # prevents alphabetical ties from silently collapsing the choice.
        representatives: list[BestiaryEntry] = []
        for species_name in dict.fromkeys(
            entry.species for entry in CORE_BESTIARY_ENTRIES
        ):
            members = [
                entry
                for entry in CORE_BESTIARY_ENTRIES
                if entry.species == species_name
            ]
            representatives.append(sorted(members, key=score)[0])

        def cross_species_score(entry: BestiaryEntry) -> tuple[int, int, int, str]:
            distance, negative_overlap, name = score(entry)
            # When neither the authored profile nor the deterministic matcher
            # contains any species evidence, a named social NPC is safer as a
            # humanoid than whichever species happens to sort first in Chinese.
            # Explicit species and actual concept overlap still outrank this
            # final tie-breaker.
            default_species_priority = 0 if entry.species == "人型" else 1
            return (
                distance,
                negative_overlap,
                default_species_priority,
                name,
            )

        return sorted(representatives, key=cross_species_score)[:8]

    def _model_proposal(
        self,
        persona: NPCPersona,
        request: dict[str, Any],
        candidates: list[BestiaryEntry],
    ) -> dict[str, Any]:
        model_deadline = self._child_deadline(
            request.get("deadline_monotonic"),
            timeout_seconds=self.model_timeout_seconds,
        )
        if model_deadline - time.monotonic() < self._MIN_MODEL_BUDGET_SECONDS:
            raise TimeoutError("npc_blueprint_design_deadline_budget_exhausted")
        candidate_rows = [
            {
                "name": entry.name,
                "level": entry.level,
                "species": entry.species,
                "traits": list(entry.typical_traits),
                "attacks": [attack.summary for attack in entry.attacks],
                "spells": [spell.name for spell in entry.spells],
                "rules": list(entry.traits_rules),
            }
            for entry in candidates
        ]
        prompt = {
            "npc": {
                "name": persona.name,
                "public_identity": persona.public_identity,
                "role_in_story": persona.role_in_story,
                "core_drive": persona.core_drive,
                "combat_style": persona.combat_style,
                "traits": list(persona.traits),
                "active_goal": persona.active_goal,
            },
            "request": {
                "level": request["level"],
                "species": request["species"],
                "rank": request["rank"],
                "scene_id": request["scene_id"],
            },
            "current_environment": dict(request.get("scene_context") or {}),
            "candidates": candidate_rows,
        }
        raw = self.client.create_chat_completion(
            model=self.model,
            messages=[
                ChatMessage(
                    role="system",
                    content=self._SYSTEM_PROMPT,
                    cache_breakpoint=True,
                    cache_family="npc-blueprint-designer-v1",
                ),
                ChatMessage(role="user", content=json.dumps(prompt, ensure_ascii=False)),
            ],
            temperature=0.15,
            response_format={"type": "json_object"},
            max_tokens=self.max_output_tokens,
            deadline=model_deadline,
            operation="npc_blueprint_design",
            thinking_enabled=False,
            max_recovery_retries=1,
            retry_without_response_format_on_empty=True,
        )
        return self._parse_json_object(raw)

    @staticmethod
    def _normalize_deadline(deadline: float | None) -> float | None:
        if deadline is None:
            return None
        try:
            return float(deadline)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _child_deadline(
        outer_deadline: object,
        *,
        timeout_seconds: float,
    ) -> float:
        local_deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        if outer_deadline is None:
            return local_deadline
        try:
            return min(local_deadline, float(outer_deadline))
        except (TypeError, ValueError):
            return local_deadline

    def _foreground_wait_timeout(self, deadline: float | None) -> float:
        local_timeout = self.model_timeout_seconds + 0.5
        if deadline is None:
            return local_timeout
        remaining = (
            deadline
            - time.monotonic()
            - self._FOREGROUND_FALLBACK_RESERVE_SECONDS
        )
        return max(0.0, min(local_timeout, remaining))

    def _compile_blueprint(
        self,
        entry: BestiaryEntry,
        *,
        request: dict[str, Any],
        tactics: dict[str, Any],
        validation_notes: list[str],
    ) -> NPCCombatBlueprint:
        rank = str(request["rank"])
        champion_value = int(request["champion_value"])
        target_level = max(int(entry.level), int(request["level"]))
        attributes = dict(entry.attributes)
        attribute_boosts: list[str] = []
        for threshold in (20, 40, 60):
            if not entry.level < threshold <= target_level:
                continue
            target_attribute = min(
                attributes,
                key=lambda name: (
                    attributes[name] >= 12,
                    attributes[name],
                    ("DEX", "INS", "MIG", "WLP").index(name),
                ),
            )
            before = int(attributes[target_attribute])
            attributes[target_attribute] = self._increase_die(before)
            if attributes[target_attribute] > before:
                attribute_boosts.append(target_attribute)

        source_hp_formula = entry.level * 2 + int(entry.attributes["MIG"]) * 5
        source_mp_formula = entry.level + int(entry.attributes["WLP"]) * 5
        source_initiative_formula = (
            int(entry.attributes["DEX"]) + int(entry.attributes["INS"])
        ) // 2
        soldier_hp = (
            target_level * 2
            + int(attributes["MIG"]) * 5
            + (int(entry.max_hp) - source_hp_formula)
        )
        soldier_mp = (
            target_level
            + int(attributes["WLP"]) * 5
            + (int(entry.max_mp) - source_mp_formula)
        )
        soldier_initiative = (
            (int(attributes["DEX"]) + int(attributes["INS"])) // 2
            + (int(entry.initiative) - source_initiative_formula)
        )
        source_level_damage = self._level_damage_bonus(entry.level)
        target_level_damage = self._level_damage_bonus(target_level)
        accuracy_delta = target_level // 10 - entry.level // 10
        hp_multiplier = 2 if rank == "elite" else champion_value if rank == "champion" else 1
        mp_multiplier = 2 if rank == "champion" else 1
        initiative_bonus = 2 if rank == "elite" else champion_value if rank == "champion" else 0
        attacks = []
        for index, attack in enumerate(entry.attacks):
            attack_options = attack_options_for_bestiary(entry.name, attack.name)
            attack_rules = attack_rules_for_bestiary(entry.name, attack.name)
            status_options = list(attack_options.get("status_options_on_hit") or [])
            attacks.append(
                NPCAttackProfile(
                    attack_id=f"attack-{index + 1}",
                    name=attack.name,
                    attributes=list(attack.attributes),
                    damage_bonus=(
                        int(attack.damage_bonus)
                        - source_level_damage
                        + target_level_damage
                    ),
                    damage_type=attack.damage_type,
                    accuracy_modifier=int(attack.accuracy_modifier) + accuracy_delta,
                    range="ranged" if attack.range_type == "远程" else "melee",
                    targets_magic_defense="魔防" in attack.effect,
                    multi_attack=self._multi_attack(attack.effect),
                    status_effect_on_hit=(
                        None
                        if status_options
                        else self._status_from_effect(attack.effect)
                    ),
                    damage_type_options=list(
                        attack_options.get("damage_type_options") or []
                    ),
                    random_damage_types=list(
                        attack_options.get("random_damage_types") or []
                    ),
                    status_options_on_hit=status_options,
                    conditional_damage_bonus=int(
                        attack_rules.get("conditional_damage_bonus") or 0
                    ),
                    conditional_target_statuses=list(
                        attack_rules.get("conditional_target_statuses") or []
                    ),
                    conditional_any_target_status=bool(
                        attack_rules.get("conditional_any_target_status")
                    ),
                    bonus_if_previous_guard=int(
                        attack_rules.get("bonus_if_previous_guard") or 0
                    ),
                    recover_hp_fraction=float(
                        attack_rules.get("recover_hp_fraction") or 0.0
                    ),
                    recover_mp_on_hit=int(
                        attack_rules.get("recover_mp_on_hit") or 0
                    ),
                    target_mp_loss=int(attack_rules.get("target_mp_loss") or 0),
                    target_ip_loss=int(attack_rules.get("target_ip_loss") or 0),
                    self_hp_loss_if_all_miss=int(
                        attack_rules.get("self_hp_loss_if_all_miss") or 0
                    ),
                    effects=list(attack_rules.get("effects") or []),
                    notes=[attack.effect] if attack.effect else [],
                )
            )
        if not attacks:
            attacks = [
                NPCAttackProfile(
                    attack_id="attack-1",
                    name="基础攻击",
                    attributes=["DEX", "MIG"],
                    damage_bonus=5,
                    damage_type="physical",
                )
            ]
            validation_notes.append("继承模板没有结构化攻击，已补入规则书基础攻击。")
        defenses = {
            "physical": (
                int(entry.fixed_physical_defense)
                if entry.fixed_physical_defense is not None
                else int(attributes["DEX"]) + int(entry.physical_defense_bonus)
            ),
            "magic": int(attributes["INS"]) + int(entry.magic_defense_bonus),
        }
        selected_bonus_skills: list[str] = []
        bonus_skill_state = {
            "boosted_attacks": set(),
            "defense_ranks": 0,
            "initiative_used": False,
        }
        level_skill_slots = max(0, target_level // 10 - entry.level // 10)
        soldier_hp, soldier_initiative = self._apply_inherited_bonus_skills(
            attacks,
            defenses,
            max_hp=soldier_hp,
            initiative=soldier_initiative,
            slots=level_skill_slots,
            state=bonus_skill_state,
            selected=selected_bonus_skills,
            priority=("damage", "defense", "initiative", "hp"),
        )
        max_hp = soldier_hp * hp_multiplier
        max_mp = soldier_mp * mp_multiplier
        initiative = soldier_initiative + initiative_bonus
        rank_skill_slots = (
            1
            if rank == "elite"
            else champion_value
            if rank == "champion"
            else 0
        )
        max_hp, initiative = self._apply_inherited_bonus_skills(
            attacks,
            defenses,
            max_hp=max_hp,
            initiative=initiative,
            slots=rank_skill_slots,
            state=bonus_skill_state,
            selected=selected_bonus_skills,
            priority=("defense", "damage", "initiative", "hp"),
        )
        species_slug = _SPECIES_ZH_TO_SLUG.get(entry.species, str(request["species"]))
        scaling_notes: list[str] = []
        if target_level > entry.level:
            scaling_notes.append(
                f"继承【{entry.name}】的结构并按NPC等级规则从{entry.level}级提升到{target_level}级。"
            )
            if attribute_boosts:
                scaling_notes.append(
                    "跨越20/40/60级阈值的属性提升："
                    + "、".join(attribute_boosts)
                    + "。"
                )
            if level_skill_slots:
                scaling_notes.append(
                    f"等级提升新增{level_skill_slots}个技能名额，已应用："
                    + "、".join(selected_bonus_skills[:level_skill_slots])
                    + "。"
                )
        if rank_skill_slots:
            scaling_notes.append(
                f"{rank}阶级新增{rank_skill_slots}个技能名额，已应用："
                + "、".join(selected_bonus_skills[level_skill_slots:])
                + "。"
            )
        return NPCCombatBlueprint(
            blueprint_id=f"npc-blueprint-{uuid4().hex}",
            npc_name=request["persona"].name,
            npc_id=str(request["npc_id"]),
            status="ready",
            design_mode="inherit",
            source_template=entry.name,
            source_note=entry.source_note,
            scene_id=str(request["scene_id"]),
            persona_revision=str(request["persona_revision"]),
            request_signature=str(request["request_signature"]),
            prompt_schema_revision=str(request["prompt_schema_revision"]),
            blueprint_schema_revision=str(request["blueprint_schema_revision"]),
            design_model=str(request["model"]),
            bestiary_revision=str(request["bestiary_revision"]),
            requested_species=str(request["species"]),
            preferred_template=str(request["preferred_template"]),
            requested_level=int(request["level"]),
            level=target_level,
            species=species_slug,
            rank=rank,
            champion_value=champion_value,
            combat_side=str(request["combat_side"]),
            is_villain=bool(request["is_villain"]),
            ultima_points=int(request["ultima_points"]),
            traits=list(entry.typical_traits),
            attributes=attributes,
            max_hp=max_hp,
            crisis_threshold=max_hp // 2,
            max_mp=max_mp,
            initiative=initiative,
            defenses=defenses,
            affinities=dict(entry.affinities),
            status_immunities=list(entry.status_immunities),
            attacks=attacks,
            spells=[
                NPCSpellProfile(
                    name=spell.name,
                    rules_name=self._rules_spell_name(entry.name, spell.name),
                    attributes=list(spell.attributes),
                    mp_cost=int(spell.mp_cost),
                    target=spell.target,
                    duration=spell.duration,
                    effect=spell.effect,
                )
                for spell in entry.spells
            ],
            other_actions=list(entry.other_actions),
            trait_rules=list(entry.traits_rules),
            ability_profiles=ability_profiles_for_bestiary(entry.name),
            selected_skills=[*entry.traits_rules, *selected_bonus_skills],
            tactics=tactics,
            validation_notes=[
                *validation_notes,
                *scaling_notes,
                f"按规则书继承【{entry.name}】并改皮为【{request['persona'].name}】。",
            ],
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _increase_die(die_size: int) -> int:
        if die_size < 8:
            return 8
        if die_size < 10:
            return 10
        if die_size < 12:
            return 12
        return 12

    @staticmethod
    def _level_damage_bonus(level: int) -> int:
        if level >= 60:
            return 15
        if level >= 40:
            return 10
        if level >= 20:
            return 5
        return 0

    @staticmethod
    def _apply_inherited_bonus_skills(
        attacks: list[NPCAttackProfile],
        defenses: dict[str, int],
        *,
        max_hp: int,
        initiative: int,
        slots: int,
        state: dict[str, object],
        selected: list[str],
        priority: tuple[str, ...],
    ) -> tuple[int, int]:
        boosted_attacks = state["boosted_attacks"]
        if not isinstance(boosted_attacks, set):
            boosted_attacks = set()
            state["boosted_attacks"] = boosted_attacks
        for _ in range(max(0, int(slots))):
            choice = "hp"
            for candidate in priority:
                if candidate == "damage" and len(boosted_attacks) < len(attacks):
                    choice = candidate
                    break
                if candidate == "defense" and int(state["defense_ranks"]) < 2:
                    choice = candidate
                    break
                if candidate == "initiative" and not bool(state["initiative_used"]):
                    choice = candidate
                    break
                if candidate == "hp":
                    choice = candidate
                    break
            if choice == "damage":
                attack_index = next(
                    index
                    for index in range(len(attacks))
                    if index not in boosted_attacks
                )
                attacks[attack_index].damage_bonus += 5
                boosted_attacks.add(attack_index)
                selected.append("强化伤害")
            elif choice == "defense":
                defenses["physical"] += 2
                defenses["magic"] += 1
                state["defense_ranks"] = int(state["defense_ranks"]) + 1
                selected.append("强化防御")
            elif choice == "initiative":
                initiative += 4
                state["initiative_used"] = True
                selected.append("强化先攻")
            else:
                max_hp += 10
                selected.append("强化生命")
        return max_hp, initiative

    @staticmethod
    def persona_revision(persona: NPCPersona) -> str:
        relevant = {
            key: value
            for key, value in asdict(persona).items()
            if key
            in {
                "name",
                "npc_id",
                "public_identity",
                "role_in_story",
                "core_drive",
                "combat_style",
                "traits",
                "known_skills",
                "combat_actions",
                "active_goal",
                "status",
            }
        }
        raw = json.dumps(relevant, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _request_signature(request: dict[str, Any]) -> str:
        payload = {
            key: request.get(key)
            for key in (
                "npc_id",
                "level",
                "species",
                "rank",
                "champion_value",
                "combat_side",
                "is_villain",
                "ultima_points",
                "scene_id",
                "scene_context",
                "preferred_template",
                "persona_revision",
                "model",
                "model_enabled",
                "prompt_schema_revision",
                "blueprint_schema_revision",
                "bestiary_revision",
            )
        }
        payload["system_prompt_sha256"] = hashlib.sha256(
            NPCBlueprintDesigner._SYSTEM_PROMPT.encode("utf-8")
        ).hexdigest()
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _scene_agnostic_reuse_matches(
        self,
        blueprint: NPCCombatBlueprint,
        request: dict[str, Any],
    ) -> bool:
        """Validate the deliberately scene-neutral prewarm compatibility key.

        This is narrower than a normal cache hit: only a blueprint authored
        with no scene binding may be promoted into a concrete conflict. Scene
        context is the sole omitted axis; identity, public persona facts,
        rules/prompt revisions and every combat-setting input remain exact.
        """

        return bool(
            not str(blueprint.scene_id or "").strip()
            and blueprint.npc_id == str(request["npc_id"])
            and blueprint.persona_revision == str(request["persona_revision"])
            and blueprint.requested_level == int(request["level"])
            and blueprint.requested_species == str(request["species"])
            and blueprint.rank == str(request["rank"])
            and blueprint.champion_value == int(request["champion_value"])
            and blueprint.combat_side == str(request["combat_side"])
            and blueprint.is_villain == bool(request["is_villain"])
            and blueprint.ultima_points == int(request["ultima_points"])
            and blueprint.preferred_template == str(request["preferred_template"])
            and blueprint.design_model == str(request["model"])
            and blueprint.prompt_schema_revision
            == self._PROMPT_SCHEMA_REVISION
            and blueprint.blueprint_schema_revision
            == self._BLUEPRINT_SCHEMA_REVISION
            and blueprint.bestiary_revision == _BESTIARY_REVISION
        )

    @staticmethod
    def _bounded_scene_context(
        value: dict[str, Any] | None,
    ) -> dict[str, object]:
        if not isinstance(value, dict):
            return {}

        def clean_text(key: str, limit: int = 240) -> str:
            return " ".join(str(value.get(key) or "").split())[:limit]

        visible = value.get("visible_elements")
        if not isinstance(visible, list):
            visible = []
        return {
            key: text
            for key, text in {
                "scene_name": clean_text("scene_name", 120),
                "location": clean_text("location", 120),
                "premise": clean_text("premise"),
                "current_pressure": clean_text("current_pressure"),
                "opposition_goal": clean_text("opposition_goal"),
                "npc_role_now": clean_text("npc_role_now"),
                "visible_elements": [
                    " ".join(str(item or "").split())[:120]
                    for item in visible[:4]
                    if str(item or "").strip()
                ],
            }.items()
            if text
        }

    @staticmethod
    def _rank(value: str) -> str:
        rank = str(value or "soldier").strip().lower()
        if rank not in {"soldier", "elite", "champion"}:
            raise ValueError("NPC战斗阶级必须是soldier、elite或champion。")
        return rank

    @staticmethod
    def _parse_json_object(raw: str) -> dict[str, Any]:
        text = str(raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                raise ValueError("独立NPC设计模型没有返回JSON对象。")
            value = json.loads(match.group(0))
        if not isinstance(value, dict):
            raise ValueError("独立NPC设计模型返回值不是JSON对象。")
        return value

    @staticmethod
    def _validate_tactics(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        cycle = value.get("cycle")
        if not isinstance(cycle, list):
            cycle = []
        protect_priority = value.get("protect_priority")
        if not isinstance(protect_priority, list):
            protect_priority = []
        return {
            "opening": str(value.get("opening") or "").strip(),
            "cycle": [str(item).strip() for item in cycle[:4] if str(item).strip()],
            "crisis": str(value.get("crisis") or "").strip(),
            "telegraph": str(value.get("telegraph") or "").strip(),
            "retreat": str(value.get("retreat") or "").strip(),
            "protect_policy": (
                str(value.get("protect_policy") or "always").strip().lower()
                if str(value.get("protect_policy") or "always").strip().lower()
                in {"always", "priority", "never"}
                else "always"
            ),
            "protect_priority": [
                str(item).strip()
                for item in protect_priority[:3]
                if str(item).strip()
            ],
        }

    @staticmethod
    def _default_tactics(entry: BestiaryEntry) -> dict[str, Any]:
        attack_names = [attack.name for attack in entry.attacks]
        spell_names = [spell.name for spell in entry.spells]
        cycle = [*attack_names, *spell_names][:4]
        return {
            "opening": cycle[0] if cycle else "先观察英雄的阵形，再执行最符合其目标的行动",
            "cycle": cycle or ["基础攻击"],
            "crisis": "优先触发危机效果；若无危机效果，则改变策略而不是机械重复最强招式",
            "telegraph": "强力攻击前用姿态、蓄力、环境变化或明确台词给出可利用的预兆",
            "retreat": "目标已无法实现、继续战斗失去意义或符合人格时撤退或投降",
            "protect_policy": "always",
            "protect_priority": [],
        }

    @staticmethod
    def _multi_attack(effect: str) -> int:
        match = re.search(r"多重攻击\s*[（(](\d+)[)）]", str(effect or ""))
        return max(1, min(3, int(match.group(1)))) if match else 1

    @staticmethod
    def _status_from_effect(effect: str) -> StatusEffect | None:
        text = str(effect or "")
        mapping = {
            "迟缓": StatusEffect.SLOW,
            "眩晕": StatusEffect.DAZED,
            "虚弱": StatusEffect.WEAKENED,
            "动摇": StatusEffect.SHAKEN,
            "激怒": StatusEffect.ENRAGED,
            "中毒": StatusEffect.POISONED,
        }
        return next((status for label, status in mapping.items() if label in text), None)

    @staticmethod
    def _rules_spell_name(template_name: str, spell_name: str) -> str:
        if template_name == "骷髅法师" and spell_name == "影袭":
            return "骷髅影袭"
        return spell_name

    @staticmethod
    def _job_view(record: dict[str, Any]) -> dict[str, Any]:
        blueprint = record.get("blueprint")
        return {
            "job_id": str(record.get("job_id") or ""),
            "status": str(record.get("status") or "unknown"),
            "npc_name": str(record.get("npc_name") or ""),
            "scene_id": str(record.get("scene_id") or ""),
            "blueprint_id": str(getattr(blueprint, "blueprint_id", "") or ""),
            "source_template": str(getattr(blueprint, "source_template", "") or ""),
            "error": str(record.get("error") or ""),
            "reused": bool(record.get("reused")),
            "fallback_used": bool(record.get("fallback_used")),
            "publication_source": str(record.get("publication_source") or ""),
        }


__all__ = ["NPCBlueprintDesigner"]
