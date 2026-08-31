from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import threading
import time
from typing import Any

from fu_gm.components.campaign_pacing_manager import CampaignPacingManager
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.story_arc_manager import StoryArcManager
from fu_gm.models import PreparedSessionContractCache


class AdventureOpeningPrefetcher:
    """Generate Chapter One's private session contract off the player path.

    Provider work runs against detached state.  Only a fingerprint-validated
    cache envelope is written back; no gate, ledger, current plan, NPC or scene
    is created until the normal start-session transaction consumes it.
    """

    _PERSISTED_CACHE_SCHEMA_VERSION = 3
    _AUTHORITY_FINGERPRINT_SCHEMA = "chapter-one-prefetch-authority-v3"
    _NEXT_PERSISTED_CACHE_SCHEMA_VERSION = 1
    _NEXT_AUTHORITY_FINGERPRINT_SCHEMA = (
        "next-session-prefetch-authority-v1"
    )
    _WRITE_LEASE_WAIT_SECONDS = 120.0

    def __init__(self, host: Any, *, timeout_seconds: float = 65.0) -> None:
        self.host = host
        self.timeout_seconds = max(20.0, float(timeout_seconds))
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="fu-gm-opening-prefetch",
        )
        self._lock = threading.RLock()
        self._jobs: dict[str, Future[dict[str, object]]] = {}
        self._status: dict[str, dict[str, object]] = {}
        self._candidates: dict[str, PreparedSessionContractCache] = {}
        self._candidate_events: dict[str, threading.Event] = {}
        # Chapter One keeps its original maps and public behavior.  Later
        # sessions use a separate namespace so an old Session Zero job cannot
        # suppress, satisfy or overwrite a next-session preparation.
        self._next_jobs: dict[str, Future[dict[str, object]]] = {}
        self._next_status: dict[str, dict[str, object]] = {}
        self._next_candidates: dict[str, PreparedSessionContractCache] = {}
        self._next_candidate_events: dict[str, threading.Event] = {}

    def schedule(
        self,
        *,
        campaign_id: str,
        session_id: str,
        channel_id: str,
    ) -> dict[str, object]:
        clean_campaign = str(campaign_id or "").strip()
        if not clean_campaign:
            return {"status": "skipped", "reason": "missing_campaign"}
        if str(getattr(self.host, "adventure_opening_flow_mode", "legacy")) != "optimized":
            return {"status": "disabled", "flow_mode": "legacy"}
        with self._lock:
            existing = self._jobs.get(clean_campaign)
            if existing is not None and not existing.done():
                return {
                    **dict(self._status.get(clean_campaign) or {}),
                    "status": "running",
                    "reused": True,
                }
        # Never wait for a campaign transaction while holding ``_lock``.
        # Workers finish under the campaign lock and then publish status under
        # ``_lock``; reversing that order here would deadlock a concurrent
        # reschedule against a stale/failed worker.
        runtime = self.host._runtime(clean_campaign)
        cached_result: dict[str, object] | None = None
        with runtime.transaction_lock:
            cached = (
                runtime.app.session_zero_manager.state
                .prepared_chapter_one_session
            )
            if cached is not None and cached.fingerprint:
                current_fingerprint = self._current_fingerprint_locked(runtime)
                if current_fingerprint != cached.fingerprint:
                    runtime.app.session_zero_manager.state.prepared_chapter_one_session = None
                elif (
                    cached.quality_status != "model_reviewed"
                    or cached.contract.preparation_status != "ready"
                ):
                    cached_result = {
                        "status": "degraded",
                        "fingerprint": cached.fingerprint[:12],
                        "quality_status": cached.quality_status,
                        "reused": False,
                    }
                else:
                    cached_result = {
                        "status": "ready",
                        "fingerprint": cached.fingerprint[:12],
                        "quality_status": cached.quality_status,
                        "reused": True,
                    }
        if cached_result is not None:
            with self._lock:
                self._status[clean_campaign] = dict(cached_result)
            return dict(cached_result)
        with self._lock:
            # A second caller may have queued the job while this caller was
            # validating the persisted envelope.
            existing = self._jobs.get(clean_campaign)
            if existing is not None and not existing.done():
                return {
                    **dict(self._status.get(clean_campaign) or {}),
                    "status": "running",
                    "reused": True,
                }
            queued = {
                "status": "queued",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "session_id": str(session_id or ""),
                "channel_id": str(channel_id or ""),
                "reused": False,
            }
            self._candidates.pop(clean_campaign, None)
            self._candidate_events[clean_campaign] = threading.Event()
            self._status[clean_campaign] = queued
            future = self._executor.submit(
                self._run,
                clean_campaign,
                str(session_id or ""),
                str(channel_id or ""),
            )
            self._jobs[clean_campaign] = future
            return dict(queued)

    @staticmethod
    def model_available(runtime: Any) -> bool:
        concretizer = (
            runtime.app.campaign_pacing_manager.contract_planner.concretizer
        )
        return concretizer.client is not None and bool(concretizer.model)

    def wait(
        self,
        campaign_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        clean_campaign = str(campaign_id or "").strip()
        with self._lock:
            future = self._jobs.get(clean_campaign)
        if future is not None and not future.done():
            try:
                future.result(
                    timeout=(
                        self.timeout_seconds
                        if timeout_seconds is None
                        else max(0.0, float(timeout_seconds))
                    )
                )
            except TimeoutError:
                return {
                    **self.status(clean_campaign),
                    "status": "running",
                    "wait_timed_out": True,
                }
            except Exception:
                pass
        return self.status(clean_campaign)

    def status(self, campaign_id: str) -> dict[str, object]:
        with self._lock:
            return dict(self._status.get(str(campaign_id or "").strip()) or {})

    def schedule_next_session(
        self,
        *,
        campaign_id: str,
        source_session_id: str,
    ) -> dict[str, object]:
        """Queue preparation for the first not-yet-started later session.

        The caller invokes this only after an adventure end has committed.
        The worker still waits for the logical write lease to clear before it
        snapshots authority, because an agent-owned HTTP/tool transaction may
        have a wider rollback boundary than ``_end_session`` itself.
        """

        clean_campaign = str(campaign_id or "").strip()
        clean_source = str(source_session_id or "").strip()
        if not clean_campaign:
            return {"status": "skipped", "reason": "missing_campaign"}
        if not clean_source:
            return {
                "status": "skipped",
                "reason": "missing_source_session",
            }
        if str(
            getattr(self.host, "adventure_opening_flow_mode", "legacy")
            or "legacy"
        ) != "optimized":
            return {"status": "disabled", "flow_mode": "legacy"}
        with self._lock:
            existing = self._next_jobs.get(clean_campaign)
            if existing is not None and not existing.done():
                return {
                    **dict(self._next_status.get(clean_campaign) or {}),
                    "status": "running",
                    "reused": True,
                }
            queued = {
                "status": "queued",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "source_session_id": clean_source,
                "reused": False,
            }
            self._next_candidates.pop(clean_campaign, None)
            self._next_candidate_events[clean_campaign] = threading.Event()
            self._next_status[clean_campaign] = dict(queued)
            future = self._executor.submit(
                self._run_next_session,
                clean_campaign,
                clean_source,
            )
            self._next_jobs[clean_campaign] = future
            return dict(queued)

    def wait_next_session(
        self,
        campaign_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        clean_campaign = str(campaign_id or "").strip()
        with self._lock:
            future = self._next_jobs.get(clean_campaign)
        if future is not None and not future.done():
            try:
                future.result(
                    timeout=(
                        self.timeout_seconds
                        if timeout_seconds is None
                        else max(0.0, float(timeout_seconds))
                    )
                )
            except TimeoutError:
                return {
                    **self.next_session_status(clean_campaign),
                    "status": "running",
                    "wait_timed_out": True,
                }
            except Exception:
                pass
        return self.next_session_status(clean_campaign)

    def next_session_status(self, campaign_id: str) -> dict[str, object]:
        with self._lock:
            return dict(
                self._next_status.get(str(campaign_id or "").strip()) or {}
            )

    def prime_next_session_for_consumption(
        self,
        runtime: Any,
        *,
        campaign_id: str,
        wait_timeout_seconds: float = 0.0,
    ) -> dict[str, object]:
        """Validate a later-session envelope and prime only transient cache.

        This method deliberately does not adopt the contract into the live
        pacing plan.  The ordinary ``refresh_plan`` transaction remains the
        sole authority that records the plan, contract history and prepared
        NPC personas.
        """

        clean_campaign = str(campaign_id or "").strip()
        with runtime.transaction_lock:
            persisted = (
                runtime.app.story_arc_manager.state
                .prepared_next_session_contract
            )
        if persisted is None or not persisted.fingerprint:
            with self._lock:
                candidate_event = self._next_candidate_events.get(
                    clean_campaign
                )
                candidate = deepcopy(
                    self._next_candidates.get(clean_campaign)
                )
                future = self._next_jobs.get(clean_campaign)
            if (
                candidate is None
                and candidate_event is not None
                and future is not None
                and not future.done()
                and float(wait_timeout_seconds or 0.0) > 0
            ):
                candidate_event.wait(
                    timeout=max(0.0, float(wait_timeout_seconds))
                )
                with self._lock:
                    candidate = deepcopy(
                        self._next_candidates.get(clean_campaign)
                    )
        else:
            candidate = None

        with runtime.transaction_lock:
            state = runtime.app.story_arc_manager.state
            persisted = state.prepared_next_session_contract
            envelope = persisted or candidate
            target_session_number = max(2, int(state.session_count or 0) + 1)
            if envelope is None or not envelope.fingerprint:
                return {
                    "status": "miss",
                    "target_session_number": target_session_number,
                    "wait": self.next_session_status(clean_campaign),
                }
            if (
                int(envelope.schema_version or 0)
                != self._NEXT_PERSISTED_CACHE_SCHEMA_VERSION
                or int(envelope.contract.session_number or 0)
                != target_session_number
            ):
                if persisted is not None:
                    state.prepared_next_session_contract = None
                return {
                    "status": "stale",
                    "reason": "target_or_cache_schema_changed",
                    "expected_session_number": int(
                        envelope.contract.session_number or 0
                    ),
                    "actual_session_number": target_session_number,
                }
            if (
                envelope.quality_status != "model_reviewed"
                or envelope.contract.preparation_status != "ready"
            ):
                if persisted is not None:
                    state.prepared_next_session_contract = None
                return {
                    "status": "degraded",
                    "fingerprint": envelope.fingerprint[:12],
                    "quality_status": envelope.quality_status,
                    "target_session_number": target_session_number,
                }
            current_fingerprint = self._next_current_fingerprint_locked(
                runtime,
                target_session_number=target_session_number,
            )
            if (
                not current_fingerprint
                or current_fingerprint != envelope.fingerprint
            ):
                if persisted is not None:
                    state.prepared_next_session_contract = None
                return {
                    "status": "stale",
                    "reason": "authoritative_inputs_changed",
                    "expected": str(envelope.fingerprint)[:12],
                    "actual": str(current_fingerprint)[:12],
                    "target_session_number": target_session_number,
                }
            generation_fingerprint = str(
                envelope.contract.preparation_fingerprint or ""
            ).strip()
            if not generation_fingerprint:
                if persisted is not None:
                    state.prepared_next_session_contract = None
                return {
                    "status": "stale",
                    "reason": "missing_generation_fingerprint",
                    "target_session_number": target_session_number,
                }
            concretizer = (
                runtime.app.campaign_pacing_manager.contract_planner
                .concretizer
            )
            concretizer.prime_cache(
                fingerprint=generation_fingerprint,
                contract=envelope.contract,
                diagnostics=envelope.diagnostics,
            )
            return {
                "status": (
                    "persistent_hit"
                    if persisted is not None
                    else "prefetch_hit"
                ),
                "fingerprint": envelope.fingerprint[:12],
                "quality_status": envelope.quality_status,
                "prepared_at": envelope.prepared_at,
                "target_session_number": target_session_number,
                # Same-process version equality is useful telemetry only.  A
                # semantic fingerprint remains the correctness check and is
                # what permits a valid cache to survive restart.
                "source_state_version_match": bool(
                    int(envelope.source_state_version or 0)
                    == int(runtime.state_version or 0)
                ),
            }

    def consume_next_session(self, runtime: Any) -> None:
        campaign_id = str(getattr(runtime, "campaign_id", "") or "").strip()
        with runtime.transaction_lock:
            runtime.app.story_arc_manager.state.prepared_next_session_contract = (
                None
            )
        with self._lock:
            self._next_candidates.pop(campaign_id, None)
            event = self._next_candidate_events.get(campaign_id)
            if event is not None:
                event.set()

    def prime_for_consumption(
        self,
        runtime: Any,
        *,
        campaign_id: str,
        session_id: str,
        channel_id: str,
        wait_timeout_seconds: float = 65.0,
    ) -> dict[str, object]:
        """Validate and load a private envelope into the live in-memory cache."""

        clean_campaign = str(campaign_id or "").strip()
        with runtime.transaction_lock:
            persisted = (
                runtime.app.session_zero_manager.state
                .prepared_chapter_one_session
            )
        # The registry owns the campaign RLock for the complete tool handler.
        # Waiting for the worker Future here would deadlock because the worker
        # needs that same lock for persistence.  It publishes its detached
        # envelope first, so waiting only on this event remains safe.
        if persisted is None or not persisted.fingerprint:
            with self._lock:
                candidate_event = self._candidate_events.get(clean_campaign)
                candidate = deepcopy(self._candidates.get(clean_campaign))
                future = self._jobs.get(clean_campaign)
            if (
                candidate is None
                and candidate_event is not None
                and future is not None
                and not future.done()
            ):
                candidate_event.wait(
                    timeout=max(0.0, float(wait_timeout_seconds))
                )
                with self._lock:
                    candidate = deepcopy(
                        self._candidates.get(clean_campaign)
                    )
        else:
            candidate = None
        with runtime.transaction_lock:
            persisted = (
                runtime.app.session_zero_manager.state
                .prepared_chapter_one_session
            )
            envelope = persisted or candidate
            if envelope is None or not envelope.fingerprint:
                return {
                    "status": "miss",
                    "wait": self.status(clean_campaign),
                }
            if (
                envelope.quality_status != "model_reviewed"
                or envelope.contract.preparation_status != "ready"
            ):
                return {
                    "status": "degraded",
                    "fingerprint": envelope.fingerprint[:12],
                    "quality_status": envelope.quality_status,
                }
            current_fingerprint = self._current_fingerprint_locked(runtime)
            if not current_fingerprint or current_fingerprint != envelope.fingerprint:
                if persisted is not None:
                    runtime.app.session_zero_manager.state.prepared_chapter_one_session = None
                return {
                    "status": "stale",
                    "expected": str(envelope.fingerprint)[:12],
                    "actual": str(current_fingerprint)[:12],
                }
            concretizer = (
                runtime.app.campaign_pacing_manager.contract_planner.concretizer
            )
            generation_fingerprint = str(
                envelope.contract.preparation_fingerprint or ""
            ).strip()
            if not generation_fingerprint:
                if persisted is not None:
                    runtime.app.session_zero_manager.state.prepared_chapter_one_session = None
                return {
                    "status": "stale",
                    "reason": "missing_generation_fingerprint",
                    "expected": str(envelope.fingerprint)[:12],
                }
            concretizer.prime_cache(
                fingerprint=generation_fingerprint,
                contract=envelope.contract,
                diagnostics=envelope.diagnostics,
            )
            return {
                "status": (
                    "persistent_hit"
                    if (
                        persisted is not None
                        and bool(getattr(runtime, "loaded_from_disk", False))
                    )
                    else "prefetch_hit"
                ),
                "fingerprint": envelope.fingerprint[:12],
                "quality_status": envelope.quality_status,
                "prepared_at": envelope.prepared_at,
                "session_id": str(session_id or ""),
                "channel_id": str(channel_id or ""),
            }

    def consume(self, runtime: Any) -> None:
        with runtime.transaction_lock:
            runtime.app.session_zero_manager.state.prepared_chapter_one_session = None

    def _publish_candidate(
        self,
        campaign_id: str,
        envelope: PreparedSessionContractCache,
    ) -> None:
        with self._lock:
            self._candidates[campaign_id] = deepcopy(envelope)
            event = self._candidate_events.get(campaign_id)
            if event is not None:
                event.set()

    def _run(
        self,
        campaign_id: str,
        session_id: str,
        channel_id: str,
    ) -> dict[str, object]:
        started = time.monotonic()
        try:
            runtime = self.host._runtime(campaign_id)
            # ``schedule`` is called while the invitation message can still own
            # the encompassing logical write lease.  The inner tool receipts
            # may already be visible at that point, but observers and rollback
            # bookkeeping have not necessarily finished.  Snapshot only the
            # committed post-message state, matching the later-session worker.
            if not self._wait_for_runtime_write_lease(runtime):
                return self._finish_status(
                    campaign_id,
                    status="deferred",
                    started=started,
                    reason="write_lease_timeout",
                )
            with runtime.transaction_lock:
                if not self._eligible_locked(
                    runtime,
                    campaign_id=campaign_id,
                    session_id=session_id,
                    channel_id=channel_id,
                ):
                    return self._finish_status(
                        campaign_id,
                        status="stale",
                        started=started,
                        reason="invitation_no_longer_eligible",
                    )
                source_state_version = int(runtime.state_version or 0)
                source_authority_digest = (
                    self._authoritative_state_digest_locked(runtime)
                )
                detached = self._detached_manager(runtime)

            plan = detached.refresh_plan(
                conflict_active=False,
                allow_model_prep=True,
                deadline=None,
                register_session_npcs=False,
                preparation_source="prefetch",
            )
            concretizer = detached.contract_planner.concretizer
            entry = concretizer.export_cache_entry()
            model_result_available = entry is not None
            generation_fingerprint = str(
                (entry or {}).get("fingerprint")
                or plan.dramatic_contract.preparation_fingerprint
                or ""
            ).strip()
            if not generation_fingerprint:
                raise RuntimeError("后台场次准备没有形成可复用缓存。")
            fingerprint = self._persistent_fingerprint(
                generation_fingerprint=generation_fingerprint,
                authority_digest=source_authority_digest,
            )
            if entry is None:
                # A provider failure deliberately degrades to the deterministic
                # contract.  It is still safe to cache when the exact request
                # fingerprint matches; marking it as ``fallback`` keeps the
                # quality distinction visible to operators and benchmarks.
                entry = {
                    "fingerprint": generation_fingerprint,
                    "contract": deepcopy(plan.dramatic_contract),
                    "diagnostics": {
                        "last_error": str(concretizer.last_error or ""),
                    },
                }
            reviewer = concretizer.reachability_reviewer
            reviewer_status = str(
                getattr(reviewer, "last_status", "") or ""
            )
            quality_status = (
                "fallback"
                if (
                    not model_result_available
                    or str(concretizer.last_error or "").strip()
                    or str(
                        getattr(concretizer, "last_gatekeeper_repair_status", "")
                        or ""
                    )
                    == "fallback_after_llm_failure"
                    or str(getattr(reviewer, "last_error", "") or "").strip()
                    or reviewer_status.startswith("fallback_")
                )
                else "model_reviewed"
            )
            diagnostics = dict((entry or {}).get("diagnostics") or {})
            # Persist only bounded operational facts alongside the private
            # typed contract. Provider response envelopes and prompts are not
            # copied into campaign storage.
            safe_diagnostics = {
                "last_error": str(diagnostics.get("last_error") or "")[:500],
                "last_gatekeeper_repair_status": str(
                    diagnostics.get("last_gatekeeper_repair_status") or ""
                )[:80],
                "reachability_last_status": str(
                    diagnostics.get("reachability_last_status")
                    or reviewer_status
                    or ""
                )[:80],
            }
            envelope = PreparedSessionContractCache(
                schema_version=self._PERSISTED_CACHE_SCHEMA_VERSION,
                fingerprint=fingerprint,
                contract=deepcopy(plan.dramatic_contract),
                model=str(concretizer.model or ""),
                review_model=str(reviewer.model or ""),
                quality_status=quality_status,
                diagnostics=safe_diagnostics,
                prepared_at=datetime.now(timezone.utc).isoformat(),
                source_state_version=source_state_version,
            )
            self._publish_candidate(campaign_id, envelope)

            terminal: dict[str, object] | None = None
            with runtime.transaction_lock:
                if not self._eligible_locked(
                    runtime,
                    campaign_id=campaign_id,
                    session_id=session_id,
                    channel_id=channel_id,
                ):
                    terminal = {
                        "status": "stale",
                        "reason": "invitation_changed_during_prefetch",
                    }
                elif str(getattr(runtime, "write_lease_owner", "") or ""):
                    # Never autosave a snapshot while another logical message
                    # owns uncommitted state. The detached candidate remains
                    # available for an exact-fingerprint consumer.
                    terminal = {
                        "status": "candidate_ready",
                        "reason": "active_write_lease",
                        "fingerprint": fingerprint[:12],
                        "quality_status": quality_status,
                    }
                else:
                    current_fingerprint = self._current_fingerprint_locked(runtime)
                    if current_fingerprint != fingerprint:
                        terminal = {
                            "status": "stale",
                            "reason": "authoritative_inputs_changed",
                            "fingerprint": fingerprint[:12],
                            "current_fingerprint": current_fingerprint[:12],
                        }
                    else:
                        manager = runtime.app.session_zero_manager
                        previous = manager.state.prepared_chapter_one_session
                        manager.state.prepared_chapter_one_session = envelope
                        try:
                            self.host._autosave_campaign(runtime, campaign_id)
                        except Exception:
                            manager.state.prepared_chapter_one_session = previous
                            raise
            if terminal is not None:
                return self._finish_status(
                    campaign_id,
                    started=started,
                    **terminal,
                )
            return self._finish_status(
                campaign_id,
                status="ready",
                started=started,
                fingerprint=fingerprint[:12],
                quality_status=quality_status,
            )
        except Exception as exc:
            return self._finish_status(
                campaign_id,
                status="failed",
                started=started,
                error=str(exc)[:500],
            )

    def _publish_next_candidate(
        self,
        campaign_id: str,
        envelope: PreparedSessionContractCache,
    ) -> None:
        with self._lock:
            self._next_candidates[campaign_id] = deepcopy(envelope)
            event = self._next_candidate_events.get(campaign_id)
            if event is not None:
                event.set()

    def _run_next_session(
        self,
        campaign_id: str,
        source_session_id: str,
    ) -> dict[str, object]:
        started = time.monotonic()
        try:
            runtime = self.host._runtime(campaign_id)
            # Agent tool execution may have committed its inner end-session
            # files while the encompassing logical write transaction can still
            # roll back.  Do not even snapshot that provisional state until
            # its lease resolves.
            if not self._wait_for_runtime_write_lease(runtime):
                return self._finish_next_status(
                    campaign_id,
                    status="deferred",
                    started=started,
                    reason="write_lease_timeout",
                )

            with runtime.transaction_lock:
                if not self._next_eligible_locked(
                    runtime,
                    source_session_id=source_session_id,
                ):
                    return self._finish_next_status(
                        campaign_id,
                        status="stale",
                        started=started,
                        reason="completed_session_no_longer_current",
                    )
                state = runtime.app.story_arc_manager.state
                target_session_number = max(
                    2,
                    int(state.session_count or 0) + 1,
                )
                persisted = state.prepared_next_session_contract
                if (
                    persisted is not None
                    and persisted.quality_status == "model_reviewed"
                    and int(persisted.schema_version or 0)
                    == self._NEXT_PERSISTED_CACHE_SCHEMA_VERSION
                    and int(persisted.contract.session_number or 0)
                    == target_session_number
                ):
                    current = self._next_current_fingerprint_locked(
                        runtime,
                        target_session_number=target_session_number,
                    )
                    if current and current == persisted.fingerprint:
                        return self._finish_next_status(
                            campaign_id,
                            status="ready",
                            started=started,
                            fingerprint=current[:12],
                            quality_status=persisted.quality_status,
                            target_session_number=target_session_number,
                            reused=True,
                        )
                    state.prepared_next_session_contract = None
                source_state_version = int(runtime.state_version or 0)
                source_authority_digest = (
                    self._next_authoritative_state_digest_locked(runtime)
                )
                detached = self._detached_manager(runtime)

            plan = detached.refresh_plan(
                conflict_active=False,
                force_session_number=target_session_number,
                allow_model_prep=True,
                deadline=None,
                register_session_npcs=False,
                preparation_source="next_session_prefetch",
            )
            concretizer = detached.contract_planner.concretizer
            entry = concretizer.export_cache_entry()
            model_result_available = entry is not None
            generation_fingerprint = str(
                (entry or {}).get("fingerprint")
                or plan.dramatic_contract.preparation_fingerprint
                or ""
            ).strip()
            if not generation_fingerprint:
                raise RuntimeError(
                    "下一场后台准备没有形成可复用生成指纹。"
                )
            fingerprint = self._next_persistent_fingerprint(
                generation_fingerprint=generation_fingerprint,
                authority_digest=source_authority_digest,
                target_session_number=target_session_number,
            )
            if entry is None:
                entry = {
                    "fingerprint": generation_fingerprint,
                    "contract": deepcopy(plan.dramatic_contract),
                    "diagnostics": {
                        "last_error": str(concretizer.last_error or ""),
                    },
                }
            reviewer = concretizer.reachability_reviewer
            reviewer_status = str(
                getattr(reviewer, "last_status", "") or ""
            )
            quality_status = (
                "fallback"
                if (
                    not model_result_available
                    or str(concretizer.last_error or "").strip()
                    or str(
                        getattr(
                            concretizer,
                            "last_gatekeeper_repair_status",
                            "",
                        )
                        or ""
                    )
                    == "fallback_after_llm_failure"
                    or str(getattr(reviewer, "last_error", "") or "").strip()
                    or reviewer_status.startswith("fallback_")
                )
                else "model_reviewed"
            )
            diagnostics = dict((entry or {}).get("diagnostics") or {})
            safe_diagnostics = {
                "last_error": str(diagnostics.get("last_error") or "")[:500],
                "last_gatekeeper_repair_status": str(
                    diagnostics.get("last_gatekeeper_repair_status") or ""
                )[:80],
                "reachability_last_status": str(
                    diagnostics.get("reachability_last_status")
                    or reviewer_status
                    or ""
                )[:80],
                "source_session_id": str(source_session_id or "")[:120],
                "target_session_number": target_session_number,
            }
            prepared_contract = deepcopy(plan.dramatic_contract)
            if int(prepared_contract.session_number or 0) != target_session_number:
                raise RuntimeError("下一场后台契约的场次编号发生漂移。")
            envelope = PreparedSessionContractCache(
                schema_version=self._NEXT_PERSISTED_CACHE_SCHEMA_VERSION,
                fingerprint=fingerprint,
                contract=prepared_contract,
                model=str(concretizer.model or ""),
                review_model=str(reviewer.model or ""),
                quality_status=quality_status,
                diagnostics=safe_diagnostics,
                prepared_at=datetime.now(timezone.utc).isoformat(),
                source_state_version=source_state_version,
            )
            self._publish_next_candidate(campaign_id, envelope)

            terminal: dict[str, object] | None = None
            with runtime.transaction_lock:
                if not self._next_eligible_locked(
                    runtime,
                    source_session_id=source_session_id,
                    target_session_number=target_session_number,
                ):
                    terminal = {
                        "status": "stale",
                        "reason": "next_session_started_or_source_changed",
                        "target_session_number": target_session_number,
                    }
                elif str(getattr(runtime, "write_lease_owner", "") or ""):
                    # A new message acquired authority while the provider was
                    # running.  Keep the immutable candidate for an immediate
                    # exact-fingerprint consumer, but never autosave across
                    # that uncommitted transaction.
                    terminal = {
                        "status": "candidate_ready",
                        "reason": "active_write_lease",
                        "fingerprint": fingerprint[:12],
                        "quality_status": quality_status,
                        "target_session_number": target_session_number,
                    }
                else:
                    current_fingerprint = (
                        self._next_current_fingerprint_locked(
                            runtime,
                            target_session_number=target_session_number,
                        )
                    )
                    if current_fingerprint != fingerprint:
                        terminal = {
                            "status": "stale",
                            "reason": "authoritative_inputs_changed",
                            "fingerprint": fingerprint[:12],
                            "current_fingerprint": current_fingerprint[:12],
                            "target_session_number": target_session_number,
                        }
                    else:
                        state = runtime.app.story_arc_manager.state
                        previous = state.prepared_next_session_contract
                        state.prepared_next_session_contract = envelope
                        try:
                            self.host._autosave_campaign(
                                runtime,
                                campaign_id,
                            )
                        except Exception:
                            state.prepared_next_session_contract = previous
                            raise
            if terminal is not None:
                return self._finish_next_status(
                    campaign_id,
                    started=started,
                    **terminal,
                )
            return self._finish_next_status(
                campaign_id,
                status="ready",
                started=started,
                fingerprint=fingerprint[:12],
                quality_status=quality_status,
                target_session_number=target_session_number,
                reused=False,
            )
        except Exception as exc:
            return self._finish_next_status(
                campaign_id,
                status="failed",
                started=started,
                error=str(exc)[:500],
            )

    def _wait_for_runtime_write_lease(self, runtime: Any) -> bool:
        condition = getattr(runtime, "write_lease_condition", None)
        if condition is None:
            with runtime.transaction_lock:
                return not bool(
                    str(getattr(runtime, "write_lease_owner", "") or "")
                )
        deadline = time.monotonic() + self._WRITE_LEASE_WAIT_SECONDS
        with condition:
            while str(getattr(runtime, "write_lease_owner", "") or ""):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                condition.wait(timeout=min(5.0, remaining))
            return True

    @staticmethod
    def _next_eligible_locked(
        runtime: Any,
        *,
        source_session_id: str,
        target_session_number: int | None = None,
    ) -> bool:
        if bool(getattr(runtime, "retired", False)):
            return False
        app = runtime.app
        if bool(getattr(app.session_ledger, "active", False)):
            return False
        state = app.story_arc_manager.state
        if int(state.session_count or 0) < 1:
            return False
        if (
            target_session_number is not None
            and int(state.session_count or 0) + 1
            != int(target_session_number)
        ):
            return False
        clean_source = str(source_session_id or "").strip()
        processed = [
            str(item or "").strip()
            for item in list(state.processed_session_ids or [])
            if str(item or "").strip()
        ]
        if clean_source and (
            not processed or processed[-1] != clean_source
        ):
            return False
        return True

    def _next_current_fingerprint_locked(
        self,
        runtime: Any,
        *,
        target_session_number: int,
    ) -> str:
        detached = self._detached_manager(runtime)
        plan = detached.refresh_plan(
            conflict_active=False,
            force_session_number=int(target_session_number),
            allow_model_prep=False,
            deadline=None,
            register_session_npcs=False,
            preparation_source="next_fingerprint_check",
        )
        generation_fingerprint = str(
            plan.dramatic_contract.preparation_fingerprint or ""
        ).strip()
        if not generation_fingerprint:
            return ""
        return self._next_persistent_fingerprint(
            generation_fingerprint=generation_fingerprint,
            authority_digest=self._next_authoritative_state_digest_locked(
                runtime
            ),
            target_session_number=int(target_session_number),
        )

    @classmethod
    def _next_persistent_fingerprint(
        cls,
        *,
        generation_fingerprint: str,
        authority_digest: str,
        target_session_number: int,
    ) -> str:
        identity = {
            "schema": cls._NEXT_AUTHORITY_FINGERPRINT_SCHEMA,
            "target_session_number": int(target_session_number),
            "generation_fingerprint": str(
                generation_fingerprint or ""
            ).strip(),
            "authority_digest": str(authority_digest or "").strip(),
        }
        return sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @classmethod
    def _next_authoritative_state_digest_locked(cls, runtime: Any) -> str:
        """Hash durable inputs that can change the next session contract.

        Runtime counters are intentionally absent.  Generated combat cards,
        pending rule windows and audit logs are also excluded because they are
        operational products rather than session-preparation authority.
        """

        app = runtime.app
        world_state = app.world_state
        excluded_world_fields = {
            "npc_combat_blueprints",
            "transparency_audit_log",
            "decision_windows",
            "pending_check_batches",
            "check_batch_history",
        }
        world_payload = {
            str(name): cls._canonical_value(value)
            for name, value in sorted(vars(world_state).items())
            if str(name) not in excluded_world_fields
        }
        # ``StoryArcManager`` lazily derives threads, locations, pressure and
        # agenda entries from the durable world profile. A freshly restored
        # process performs that sync during runtime construction, while an
        # older in-process state may not have materialized it yet. Hash a
        # detached, deterministically synchronized copy so equivalent saves
        # have one identity on both sides of a restart.
        normalized_arc_state = deepcopy(app.story_arc_manager.state)
        normalized_story_arc = StoryArcManager(
            deepcopy(world_state),
            clock_manager=deepcopy(app.clock_manager),
            state=normalized_arc_state,
        )
        normalized_story_arc.sync_from_world_profile()
        arc_payload = cls._canonical_value(normalized_arc_state)
        if isinstance(arc_payload, dict):
            arc_payload.pop("prepared_next_session_contract", None)
            # This timestamp is only observability for the deterministic
            # derived-state refresh above; it is not preparation authority.
            arc_payload.pop("last_updated", None)
        characters = sorted(
            (
                cls._canonical_value(item)
                for item in app.character_manager.all()
            ),
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        clock_payload = {
            str(name): cls._canonical_value(value)
            for name, value in sorted(vars(app.clock_manager).items())
            if str(name) != "_current_scene_id"
        }
        session_zero = app.session_zero_manager.state
        payload = {
            "schema": cls._NEXT_AUTHORITY_FINGERPRINT_SCHEMA,
            "target_session_number": int(
                app.story_arc_manager.state.session_count or 0
            )
            + 1,
            "world": world_payload,
            "story_arc": arc_payload,
            "characters": characters,
            "clocks": clock_payload,
            "session_zero_world": cls._canonical_value(
                session_zero.world
            ),
            "campaign_participants": cls._canonical_value(
                session_zero.participants
            ),
        }
        return sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def _eligible_locked(
        self,
        runtime: Any,
        *,
        campaign_id: str,
        session_id: str,
        channel_id: str,
    ) -> bool:
        if bool(getattr(runtime, "retired", False)):
            return False
        gate = self.host.session_gates.get(campaign_id, channel_id, session_id)
        if str(getattr(gate, "status", "") or "") != "session_zero":
            return False
        readiness = self.host._adventure_readiness_snapshot(
            runtime,
            materialize_confirmed_characters=False,
        )
        transition = runtime.app.session_zero_manager.chapter_one_transition_status(
            ready=bool(readiness.get("ready"))
        )
        return bool(readiness.get("ready")) and str(
            transition.get("status") or ""
        ) == "invited"

    def _current_fingerprint_locked(self, runtime: Any) -> str:
        detached = self._detached_manager(runtime)
        plan = detached.refresh_plan(
            conflict_active=False,
            allow_model_prep=False,
            deadline=None,
            register_session_npcs=False,
            preparation_source="fingerprint_check",
        )
        generation_fingerprint = str(
            plan.dramatic_contract.preparation_fingerprint or ""
        ).strip()
        if not generation_fingerprint:
            return ""
        return self._persistent_fingerprint(
            generation_fingerprint=generation_fingerprint,
            authority_digest=self._authoritative_state_digest_locked(runtime),
        )

    @classmethod
    def _persistent_fingerprint(
        cls,
        *,
        generation_fingerprint: str,
        authority_digest: str,
    ) -> str:
        """Bind a generated result to durable authoritative campaign input.

        ``generation_fingerprint`` comes from ``SessionPrepConcretizer`` and
        already covers the exact provider request, model names, prompt hashes
        and preparation schema revision.  The second digest covers inputs that
        are authoritative for Chapter One even when the generation request
        only uses a compact projection of them.  Neither value depends on the
        process-local runtime state counter, so a valid envelope survives a
        service restart.
        """

        identity = {
            "schema": cls._AUTHORITY_FINGERPRINT_SCHEMA,
            "generation_fingerprint": str(generation_fingerprint or "").strip(),
            "authority_digest": str(authority_digest or "").strip(),
        }
        return sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @classmethod
    def _authoritative_state_digest_locked(cls, runtime: Any) -> str:
        """Hash stable Chapter One inputs, not ephemeral process counters.

        The provider request intentionally receives a compact world packet.
        Cache invalidation must be stricter: changing a safety line, any world
        creation field, a confirmed character, the first-act agreement or the
        participating roster must invalidate the persisted result even if the
        deterministic planner happens to produce the same short brief.

        Deliberately excluded are generated/operational products such as NPC
        combat blueprints, pending checks and audit logs.  Those can change in
        background workers and are not inputs to Chapter One preparation.
        """

        app = runtime.app
        session_zero = app.session_zero_manager.state
        world_state = app.world_state
        world_profile = world_state.world_profile
        characters = sorted(
            (cls._canonical_value(item) for item in app.character_manager.all()),
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        payload = {
            "schema": cls._AUTHORITY_FINGERPRINT_SCHEMA,
            # This complete dataclass includes safety_lines/safety_veils,
            # every first-act field, hero drafts and world_setting_revision.
            "world_profile": cls._canonical_value(world_profile),
            # These objects normally share identity. Keeping both makes a
            # partially migrated/legacy save fail closed if they ever diverge.
            "session_zero_world": cls._canonical_value(session_zero.world),
            "first_act": {
                "selected_id": str(world_profile.selected_first_act_id or ""),
                "selected_summary": str(
                    world_profile.selected_first_act_summary or ""
                ),
                "votes": cls._canonical_value(world_profile.first_act_votes),
                "questions": cls._canonical_value(
                    world_profile.first_act_questions
                ),
                "answers": cls._canonical_value(
                    world_profile.first_act_question_answers
                ),
                "skipped_questions": cls._canonical_value(
                    world_profile.first_act_skipped_questions
                ),
                "opening_equipment_restrictions": cls._canonical_value(
                    world_profile.first_act_opening_equipment_restrictions
                ),
            },
            "safety_boundary": {
                "lines": cls._canonical_value(world_profile.safety_lines),
                "veils": cls._canonical_value(world_profile.safety_veils),
            },
            # This is the Session 0 roster that owns Chapter One. Runtime
            # ``present_players``/``absent_players`` are deliberately not
            # included: merely receiving the consenting message marks its
            # speaker present before the tool runs, which is transport-level
            # attendance bookkeeping rather than a changed preparation input.
            "participants": cls._canonical_value(session_zero.participants),
            "characters": characters,
            # Narrative world facts can influence names, locations, chapter
            # constraints and continuity even when absent from WorldCreation.
            "world_narrative": {
                "session_pillars": cls._canonical_value(
                    world_state.session_pillars
                ),
                "map_notes": cls._canonical_value(world_state.map_notes),
                "map_locations": cls._canonical_value(
                    world_state.map_locations
                ),
                "map_routes": cls._canonical_value(world_state.map_routes),
                "npc_relationships": cls._canonical_value(
                    world_state.npc_relationships
                ),
                "memories": cls._canonical_value(world_state.memories),
                "npc_personas": cls._canonical_value(
                    world_state.npc_personas
                ),
                "subject_facts": cls._canonical_value(
                    world_state.subject_facts
                ),
                "persistent_changes": cls._canonical_value(
                    world_state.persistent_changes
                ),
                "story_items": cls._canonical_value(world_state.story_items),
                "memory_events": cls._canonical_value(
                    world_state.memory_events
                ),
                "memory_relations": cls._canonical_value(
                    world_state.memory_relations
                ),
                "gm_secrets": cls._canonical_value(world_state.gm_secrets),
                "party_sheet": cls._canonical_value(world_state.party_sheet),
                "world_sheet": cls._canonical_value(world_state.world_sheet),
                "chapter_packages": cls._canonical_value(
                    world_state.chapter_packages
                ),
                "active_chapter_package": str(
                    world_state.active_chapter_package or ""
                ),
                "iconic_elements": cls._canonical_value(
                    world_state.iconic_elements
                ),
            },
        }
        return sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @classmethod
    def _canonical_value(cls, value: Any) -> Any:
        if is_dataclass(value):
            return cls._canonical_value(asdict(value))
        if isinstance(value, Enum):
            return cls._canonical_value(value.value)
        if isinstance(value, dict):
            return {
                str(key): cls._canonical_value(item)
                for key, item in sorted(
                    value.items(),
                    key=lambda pair: str(pair[0]),
                )
            }
        if isinstance(value, (list, tuple)):
            return [cls._canonical_value(item) for item in value]
        if isinstance(value, (set, frozenset)):
            items = [cls._canonical_value(item) for item in value]
            return sorted(
                items,
                key=lambda item: json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    @staticmethod
    def _detached_manager(runtime: Any) -> CampaignPacingManager:
        live = runtime.app.campaign_pacing_manager
        world_state = deepcopy(runtime.app.world_state)
        arc_state = deepcopy(runtime.app.story_arc_manager.state)
        clock_manager = deepcopy(runtime.app.clock_manager)
        character_manager = CharacterManager()
        for character in deepcopy(runtime.app.character_manager.all()):
            character_manager.add(character)
        story_arc = StoryArcManager(
            world_state,
            clock_manager=clock_manager,
            state=arc_state,
        )
        concretizer = live.contract_planner.concretizer
        return CampaignPacingManager(
            story_arc,
            clock_manager,
            world_state,
            character_manager=character_manager,
            client=concretizer.client,
            model=concretizer.model,
            review_client=concretizer.reachability_reviewer.client,
            review_model=concretizer.reachability_reviewer.model,
            session_prep_timeout_seconds=concretizer.model_prep_max_seconds,
        )

    def _finish_status(
        self,
        campaign_id: str,
        *,
        status: str,
        started: float,
        **details: object,
    ) -> dict[str, object]:
        result = {
            "status": str(status),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            **details,
        }
        with self._lock:
            self._status[campaign_id] = dict(result)
        return result

    def _finish_next_status(
        self,
        campaign_id: str,
        *,
        status: str,
        started: float,
        **details: object,
    ) -> dict[str, object]:
        """Publish terminal state without mixing it with Chapter One jobs."""

        result = {
            "status": str(status),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            **details,
        }
        with self._lock:
            self._next_status[campaign_id] = dict(result)
        return result

    def audit_payload(self) -> dict[str, object]:
        with self._lock:
            return {
                campaign_id: dict(status)
                for campaign_id, status in self._status.items()
            }

    def next_session_audit_payload(self) -> dict[str, object]:
        with self._lock:
            return {
                campaign_id: dict(status)
                for campaign_id, status in self._next_status.items()
            }
