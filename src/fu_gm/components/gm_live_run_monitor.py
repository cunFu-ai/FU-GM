from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from contextvars import ContextVar, Token
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_text(value: object, *, limit: int = 1_048_576) -> tuple[str, bool, int]:
    text = str(value or "")
    original_chars = len(text)
    if original_chars <= limit:
        return text, False, original_chars
    marker = (
        "\n\n[实时观察器：单项文本超过 1 MiB，"
        f"已保留前 {limit} 个字符；原始长度 {original_chars}。]"
    )
    return text[:limit] + marker, True, original_chars


@dataclass
class _LiveRun:
    run_id: str
    campaign_id: str
    session_id: str
    channel_id: str
    conversation_turn_id: str
    message_id: str
    speaker: str
    source_kind: str
    is_private: bool
    model: str
    timeout_seconds: float
    max_iterations: int
    started_at: str
    started_monotonic: float
    owner_thread_id: int
    phase: str = "accepted"
    iteration: int = 0
    status: str = "active"
    terminal_reason: str = ""
    phase_started_monotonic: float = 0.0
    last_event_monotonic: float = 0.0
    ended_monotonic: float = 0.0
    superseded: bool = False
    superseded_by: str = ""
    events: deque[dict[str, object]] = field(default_factory=deque)


@dataclass(frozen=True)
class _LiveRunBinding:
    monitor: "GMLiveRunMonitor"
    run_id: str


_CURRENT_LIVE_RUN: ContextVar[_LiveRunBinding | None] = ContextVar(
    "fu_gm_current_live_run",
    default=None,
)


class GMLiveRunMonitor:
    """Thread-safe, process-local observability for active GM turns.

    The monitor owns diagnostic copies only.  It never reads campaign state,
    participates in a game transaction, or persists model output to a save.
    This makes its lock independent from ``CampaignRuntime.transaction_lock``
    and lets the Dashboard remain responsive while a model request is blocked.
    """

    def __init__(
        self,
        *,
        completed_limit: int = 50,
        events_per_run: int = 500,
        stuck_grace_seconds: float = 5.0,
    ) -> None:
        self.completed_limit = max(1, int(completed_limit))
        self.events_per_run = max(20, int(events_per_run))
        self.stuck_grace_seconds = max(0.0, float(stuck_grace_seconds))
        self._lock = threading.RLock()
        self._runs: dict[str, _LiveRun] = {}
        self._active_ids: set[str] = set()
        self._completed_ids: deque[str] = deque()
        self._sequence = 0

    def start_run(
        self,
        *,
        campaign_id: str,
        session_id: str,
        channel_id: str,
        conversation_turn_id: str = "",
        message_id: str = "",
        speaker: str = "",
        source_kind: str = "player_message",
        is_private: bool = False,
        model: str = "",
        timeout_seconds: float = 0.0,
        max_iterations: int = 0,
        message: str = "",
    ) -> str:
        now = time.monotonic()
        run_id = uuid.uuid4().hex
        run = _LiveRun(
            run_id=run_id,
            campaign_id=str(campaign_id or "default"),
            session_id=str(session_id or "default"),
            channel_id=str(channel_id or ""),
            conversation_turn_id=str(conversation_turn_id or ""),
            message_id=str(message_id or ""),
            speaker=str(speaker or ""),
            source_kind=str(source_kind or "player_message"),
            is_private=bool(is_private),
            model=str(model or ""),
            timeout_seconds=max(0.0, float(timeout_seconds or 0.0)),
            max_iterations=max(0, int(max_iterations or 0)),
            started_at=_utc_now(),
            started_monotonic=now,
            owner_thread_id=threading.get_ident(),
            phase_started_monotonic=now,
            last_event_monotonic=now,
            events=deque(maxlen=self.events_per_run),
        )
        with self._lock:
            self._runs[run_id] = run
            self._active_ids.add(run_id)
            self._append_event_locked(
                run,
                kind="run_started",
                phase="accepted",
                summary="已接收消息，开始主持事务。",
                public_details={
                    "source_kind": run.source_kind,
                    "model": run.model,
                    "timeout_seconds": run.timeout_seconds,
                    "max_iterations": run.max_iterations,
                    "message_chars": len(str(message or "")),
                },
                private_details={"message": str(message or "")},
            )
        return run_id

    def event(
        self,
        run_id: str,
        *,
        kind: str,
        summary: str = "",
        phase: str = "",
        iteration: int | None = None,
        attempt: int = 0,
        public_details: dict[str, object] | None = None,
        private_details: dict[str, object] | None = None,
    ) -> None:
        with self._lock:
            run = self._runs.get(str(run_id or ""))
            if run is None:
                return
            clean_phase = str(phase or "").strip()
            if clean_phase and clean_phase != run.phase:
                run.phase = clean_phase
                run.phase_started_monotonic = time.monotonic()
            if iteration is not None:
                run.iteration = max(0, int(iteration))
            self._append_event_locked(
                run,
                kind=kind,
                phase=clean_phase or run.phase,
                summary=summary,
                attempt=attempt,
                public_details=public_details,
                private_details=private_details,
            )

    def mark_superseded(
        self,
        *,
        campaign_id: str,
        session_id: str,
        channel_id: str,
        newer_message_id: str = "",
    ) -> int:
        if not str(channel_id or "").strip():
            return 0
        marked = 0
        with self._lock:
            for run_id in tuple(self._active_ids):
                run = self._runs.get(run_id)
                if run is None or (
                    run.campaign_id != str(campaign_id or "")
                    or run.session_id != str(session_id or "")
                    or run.channel_id != str(channel_id or "")
                ):
                    continue
                if newer_message_id and run.message_id == newer_message_id:
                    continue
                if run.superseded:
                    continue
                run.superseded = True
                run.superseded_by = str(newer_message_id or "")
                marked += 1
                self._append_event_locked(
                    run,
                    kind="run_superseded",
                    phase=run.phase,
                    summary="频道收到更新消息；本轮将在下一个安全点终止或回滚。",
                    public_details={"superseded": True},
                    private_details={
                        "newer_message_id": str(newer_message_id or "")
                    },
                )
        return marked

    def finish_run(
        self,
        run_id: str,
        *,
        terminal_reason: str,
        status: str = "completed",
        summary: str = "",
        public_details: dict[str, object] | None = None,
        private_details: dict[str, object] | None = None,
    ) -> None:
        with self._lock:
            run = self._runs.get(str(run_id or ""))
            if run is None or run.status != "active":
                return
            run.status = str(status or "completed")
            run.terminal_reason = str(terminal_reason or run.status)
            run.ended_monotonic = time.monotonic()
            terminal_phase = (
                "stale"
                if run.status == "stale"
                else "failed"
                if run.status in {"failed", "exception"}
                else "completed"
            )
            run.phase = terminal_phase
            run.phase_started_monotonic = run.ended_monotonic
            self._append_event_locked(
                run,
                kind="run_finished",
                phase=terminal_phase,
                summary=summary or f"主持事务结束：{run.terminal_reason}",
                public_details={
                    "status": run.status,
                    "terminal_reason": run.terminal_reason,
                    **dict(public_details or {}),
                },
                private_details=private_details,
            )
            self._active_ids.discard(run.run_id)
            self._completed_ids.append(run.run_id)
            while len(self._completed_ids) > self.completed_limit:
                expired = self._completed_ids.popleft()
                if expired not in self._active_ids:
                    self._runs.pop(expired, None)

    def snapshot(
        self,
        *,
        campaign_id: str = "",
        session_id: str = "",
        channel_id: str = "",
        include_private: bool = False,
        limit: int = 20,
        after_sequence: int = 0,
    ) -> dict[str, object]:
        with self._lock:
            now = time.monotonic()
            thread_ids = {
                int(thread.ident)
                for thread in threading.enumerate()
                if thread.ident is not None and thread.is_alive()
            }
            active = [
                self._serialize_run_locked(
                    self._runs[run_id],
                    now=now,
                    thread_ids=thread_ids,
                    include_private=include_private,
                    after_sequence=after_sequence,
                )
                for run_id in sorted(
                    self._active_ids,
                    key=lambda item: self._runs[item].started_monotonic,
                )
                if self._matches_scope(
                    self._runs[run_id],
                    campaign_id=campaign_id,
                    session_id=session_id,
                    channel_id=channel_id,
                )
            ]
            recent_ids = list(self._completed_ids)[-max(1, int(limit)) :]
            recent = [
                self._serialize_run_locked(
                    self._runs[run_id],
                    now=now,
                    thread_ids=thread_ids,
                    include_private=include_private,
                    after_sequence=after_sequence,
                )
                for run_id in reversed(recent_ids)
                if run_id in self._runs
                and self._matches_scope(
                    self._runs[run_id],
                    campaign_id=campaign_id,
                    session_id=session_id,
                    channel_id=channel_id,
                )
            ]
            return {
                "ok": True,
                "server_time": _utc_now(),
                "next_poll_ms": 750,
                "latest_sequence": self._sequence,
                "active_count": len(active),
                "active_runs": active,
                "recent_runs": recent,
                "streaming": False,
                "streaming_note": (
                    "当前供应商请求为非流式；返回前只能显示等待状态，"
                    "返回后会一次性显示完整 assistant 正文。"
                ),
                "private_included": bool(include_private),
            }

    def _append_event_locked(
        self,
        run: _LiveRun,
        *,
        kind: str,
        phase: str,
        summary: str,
        attempt: int = 0,
        public_details: dict[str, object] | None = None,
        private_details: dict[str, object] | None = None,
    ) -> None:
        now = time.monotonic()
        self._sequence += 1
        private = deepcopy(dict(private_details or {}))
        for key, value in tuple(private.items()):
            if isinstance(value, str):
                bounded, truncated, original_chars = _bounded_text(value)
                private[key] = bounded
                if truncated:
                    private[f"{key}_truncated"] = True
                    private[f"{key}_original_chars"] = original_chars
        run.events.append(
            {
                "sequence": self._sequence,
                "run_sequence": (
                    int(run.events[-1]["run_sequence"]) + 1
                    if run.events
                    else 1
                ),
                "at": _utc_now(),
                "offset_ms": max(
                    0,
                    int((now - run.started_monotonic) * 1000),
                ),
                "kind": str(kind or "event"),
                "phase": str(phase or run.phase),
                "iteration": run.iteration,
                "attempt": max(0, int(attempt or 0)),
                "summary": str(summary or ""),
                "public_details": deepcopy(dict(public_details or {})),
                "private_details": private,
            }
        )
        run.last_event_monotonic = now

    def _serialize_run_locked(
        self,
        run: _LiveRun,
        *,
        now: float,
        thread_ids: set[int],
        include_private: bool,
        after_sequence: int,
    ) -> dict[str, object]:
        end = run.ended_monotonic or now
        elapsed_ms = max(0, int((end - run.started_monotonic) * 1000))
        phase_elapsed_ms = max(
            0,
            int(((run.ended_monotonic or now) - run.phase_started_monotonic) * 1000),
        )
        last_event_age_ms = max(
            0,
            int((now - run.last_event_monotonic) * 1000),
        )
        timeout_ms = max(0, int(run.timeout_seconds * 1000))
        remaining_ms = max(0, timeout_ms - elapsed_ms) if timeout_ms else 0
        thread_alive = run.owner_thread_id in thread_ids
        health = run.status
        health_reason = ""
        if run.status == "active":
            if (
                not thread_alive
                or (
                    timeout_ms
                    and elapsed_ms
                    > timeout_ms + int(self.stuck_grace_seconds * 1000)
                )
            ):
                health = "suspected_stuck"
                health_reason = (
                    "worker_gone" if not thread_alive else "deadline_overrun"
                )
            elif run.superseded:
                health = "superseded"
                health_reason = "newer_channel_message"
            elif run.phase in {
                "requesting_model",
                "provider_attempt",
                "provider_recovery",
            }:
                health = "waiting_provider"
            elif timeout_ms and elapsed_ms >= int(timeout_ms * 0.75):
                health = "slow"
                health_reason = "approaching_deadline"
            else:
                health = "running"
        events: list[dict[str, object]] = []
        for event in run.events:
            if int(event.get("sequence") or 0) <= max(0, int(after_sequence or 0)):
                continue
            serialized = {
                key: deepcopy(value)
                for key, value in event.items()
                if key != "private_details"
            }
            details = dict(serialized.pop("public_details", {}) or {})
            if include_private:
                details.update(deepcopy(dict(event.get("private_details") or {})))
            serialized["details"] = details
            events.append(serialized)
        payload: dict[str, object] = {
            "run_id": run.run_id,
            "campaign_id": run.campaign_id,
            "session_id": run.session_id,
            "channel_id": run.channel_id,
            "conversation_turn_id": run.conversation_turn_id,
            "source_kind": run.source_kind,
            "is_private": run.is_private,
            "model": run.model,
            "started_at": run.started_at,
            "status": run.status,
            "health": health,
            "health_reason": health_reason,
            "phase": run.phase,
            "iteration": run.iteration,
            "terminal_reason": run.terminal_reason,
            "superseded": run.superseded,
            "elapsed_ms": elapsed_ms,
            "phase_elapsed_ms": phase_elapsed_ms,
            "last_event_age_ms": last_event_age_ms,
            "timeout_seconds": run.timeout_seconds,
            "deadline_remaining_ms": remaining_ms,
            "max_iterations": run.max_iterations,
            "thread_alive": thread_alive,
            "events": events,
        }
        if include_private:
            payload.update(
                {
                    "message_id": run.message_id,
                    "speaker": (
                        "匿名玩家" if run.is_private else run.speaker
                    ),
                    "superseded_by": run.superseded_by,
                }
            )
        return payload

    @staticmethod
    def _matches_scope(
        run: _LiveRun,
        *,
        campaign_id: str,
        session_id: str,
        channel_id: str,
    ) -> bool:
        return bool(
            (not campaign_id or run.campaign_id == str(campaign_id))
            and (not session_id or run.session_id == str(session_id))
            and (not channel_id or run.channel_id == str(channel_id))
        )


def bind_live_run(monitor: GMLiveRunMonitor, run_id: str) -> Token:
    return _CURRENT_LIVE_RUN.set(
        _LiveRunBinding(monitor=monitor, run_id=str(run_id or ""))
    )


def reset_live_run(token: Token) -> None:
    try:
        _CURRENT_LIVE_RUN.reset(token)
    except Exception:
        pass


def emit_live_run_event(
    kind: str,
    *,
    summary: str = "",
    phase: str = "",
    iteration: int | None = None,
    attempt: int = 0,
    public_details: dict[str, object] | None = None,
    private_details: dict[str, object] | None = None,
) -> None:
    """Publish one best-effort event without affecting the GM transaction."""

    binding = _CURRENT_LIVE_RUN.get()
    if binding is None:
        return
    try:
        binding.monitor.event(
            binding.run_id,
            kind=kind,
            summary=summary,
            phase=phase,
            iteration=iteration,
            attempt=attempt,
            public_details=public_details,
            private_details=private_details,
        )
    except Exception:
        # Observability is explicitly non-authoritative.  A broken Dashboard
        # must never alter a roll, tool receipt, transaction or public reply.
        return
