from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from fu_gm.components.decision_window_manager import DecisionWindowManager
from fu_gm.components.world_state import WorldState
from fu_gm.gm_tool_contracts import GMToolExecutionContext


@dataclass(frozen=True)
class ResolvedToolContinuation:
    """A persisted cross-message tool obligation that has just been resumed."""

    window_id: str
    tool_name: str
    arguments: dict[str, object]
    required_field: str
    requester: str


class GMToolContinuationManager:
    """Persist small tool continuations without turning them into background jobs.

    Some foreground tools need one player-provided field before they can finish.
    The missing-field prompt and the eventual answer may be separated by other
    group messages or even a service restart.  A suppressed decision window is
    a good persistence primitive for that gap: it is transactional and saved
    with the campaign, but it does not block unrelated table conversation.
    """

    KIND = "gm_tool_continuation"

    def __init__(self, world_state: WorldState) -> None:
        self.windows = DecisionWindowManager(world_state)

    def register(
        self,
        context: GMToolExecutionContext,
        *,
        continuation_key: str,
        required_field: str,
        resume_tool: str,
        resume_arguments: dict[str, object] | None = None,
        label: str = "",
    ) -> str:
        clean_key = self._clean(continuation_key)
        clean_field = self._clean(required_field)
        clean_tool = self._clean(resume_tool)
        if not clean_key or not clean_field or not clean_tool:
            raise ValueError("工具续办必须包含稳定键、待补字段和恢复工具。")

        source_event_id = self._clean(
            context.metadata.get("source_event_id")
            or context.metadata.get("event_id")
            or context.metadata.get("message_id")
        )
        dedupe_key = ":".join(
            (
                self.KIND,
                self._clean(context.session_id),
                self._clean(context.channel_id),
                clean_key,
            )
        )
        window = self.windows.create(
            kind=self.KIND,
            owner=self._clean(context.speaker),
            scope_kind="session",
            scope_id=self._clean(context.session_id),
            blocking=False,
            action_type="resume_tool",
            resume_point=clean_tool,
            payload={
                "continuation_key": clean_key,
                "required_field": clean_field,
                "resume_tool": clean_tool,
                "resume_arguments": deepcopy(dict(resume_arguments or {})),
                "channel_id": self._clean(context.channel_id),
                "session_id": self._clean(context.session_id),
                "requester": self._clean(context.speaker),
                "source_event_id": source_event_id,
                "label": self._clean(label),
                # This is internal workflow state, not a second player-facing
                # rules prompt.  The originating tool already asked the player.
                "suppress_public_prompt": True,
            },
            dedupe_key=dedupe_key,
        )
        return window.window_id

    def resolve_for_field(
        self,
        context: GMToolExecutionContext,
        *,
        required_field: str,
        value: object,
    ) -> ResolvedToolContinuation | None:
        """Resolve the newest matching continuation in this chat session.

        A successful authoritative field write has already established that
        the current speaker may supply the value.  Matching by channel and
        session lets another participant answer a shared Session 0 question
        while preventing a private chat or another campaign surface from
        accidentally consuming the request.
        """

        clean_field = self._clean(required_field)
        if not clean_field or not self._clean(value):
            return None
        channel_id = self._clean(context.channel_id)
        session_id = self._clean(context.session_id)
        candidates = []
        for window in self.windows.pending(kind=self.KIND):
            payload = window.payload
            if self._clean(payload.get("required_field")) != clean_field:
                continue
            if self._clean(payload.get("channel_id")) != channel_id:
                continue
            if self._clean(payload.get("session_id")) != session_id:
                continue
            candidates.append(window)
        if not candidates:
            return None

        window = candidates[-1]
        responder = self._clean(context.speaker)
        if responder and responder not in window.allowed_responders:
            window.allowed_responders.append(responder)
        self.windows.resolve(
            window_id=window.window_id,
            responder=responder or window.owner,
            resolution={
                "provided_field": clean_field,
                "provided_value": self._clean(value),
                "responder": responder,
            },
        )
        return ResolvedToolContinuation(
            window_id=window.window_id,
            tool_name=self._clean(window.payload.get("resume_tool")),
            arguments=deepcopy(
                dict(window.payload.get("resume_arguments") or {})
            ),
            required_field=clean_field,
            requester=self._clean(window.payload.get("requester")),
        )

    @staticmethod
    def _clean(value: object) -> str:
        return " ".join(str(value or "").split()).strip()


__all__ = ["GMToolContinuationManager", "ResolvedToolContinuation"]
