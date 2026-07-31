from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from fu_gm.components.clock_manager import ClockManager
from fu_gm.models import ActionResolution


class ClockLifecycleCoordinator:
    """Close countdown clocks once their announced consequence has happened.

    Objective clocks and pressure clocks resolve when they become full.
    Ritual clocks are different: filling one only makes the final casting
    action available. Keeping already resolved clocks active invites later
    narration to downgrade an accomplished fact back into an approaching one.
    """

    PRESSURE_TYPES = {"threat", "villain", "dungeon", "boss"}
    IMMEDIATE_TYPES = PRESSURE_TYPES | {"objective"}

    def __init__(self, clocks: ClockManager) -> None:
        self.clocks = clocks

    def settle_resolution(self, resolution: ActionResolution) -> list[dict[str, str]]:
        changes = [
            *list(resolution.payload.get("auto_clock_changes") or []),
            *(
                [resolution.payload["clock_change"]]
                if resolution.payload.get("clock_change")
                else []
            ),
        ]
        return self.settle_changes(changes, payload=resolution.payload)

    def settle_changes(
        self,
        changes: Iterable[Any],
        *,
        payload: dict[str, object] | None = None,
    ) -> list[dict[str, str]]:
        """Settle completed finite clocks for every execution path.

        Structured rule actions and natural-language GM tools both advance the
        same clocks.  Accepting raw committed changes here prevents the latter
        path from leaving a full threat active merely because it did not build
        an ``ActionResolution`` object.
        """

        settled: list[dict[str, str]] = []
        seen: set[str] = set()
        for change in changes:
            name = str(getattr(change, "clock_name", "") or "").strip()
            clock_type = str(getattr(change, "clock_type", "") or "").strip()
            after = int(getattr(change, "after", 0) or 0)
            maximum = int(getattr(change, "max_segments", 0) or 0)
            if not name or name in seen or clock_type not in self.IMMEDIATE_TYPES:
                continue
            if maximum <= 0 or after < maximum or not self.clocks.exists(name):
                continue
            clock = self.clocks.get(name)
            consequence = str(
                getattr(change, "completion_consequence", "")
                or clock.completion_consequence
                or getattr(change, "stakes", "")
                or clock.stakes
                or (
                    "目标已经达成"
                    if clock_type == "objective"
                    else f"命刻【{name}】的后果已经发生"
                )
            ).strip()
            self.clocks.resolve(name, note=consequence, archive=True)
            seen.add(name)
            settled.append(
                {
                    "clock_name": name,
                    "clock_type": clock_type,
                    "consequence": consequence,
                    "status": "resolved",
                }
            )
        if settled and payload is not None:
            pressure = [
                item for item in settled if item["clock_type"] in self.PRESSURE_TYPES
            ]
            objectives = [
                item for item in settled if item["clock_type"] == "objective"
            ]
            if pressure:
                payload["settled_pressure_clocks"] = pressure
                # A fulfilled pressure clock is not another warning. Its
                # announced consequence has changed the current problem and
                # must advance the episode into a response/turning-point beat.
                payload["pressure_clock_fulfilled"] = True
                payload["local_question_changed"] = True
                payload["session_reversal"] = True
            if objectives:
                payload["settled_objective_clocks"] = objectives
            payload["world_consequence_required"] = True
            existing = list(
                payload.get("committed_world_consequences") or []
            )
            for item in settled:
                consequence = str(item.get("consequence") or "").strip()
                if consequence and consequence not in existing:
                    existing.append(consequence)
            payload["committed_world_consequences"] = existing
        return settled

    def reconcile_fulfilled(
        self,
        *,
        payload: dict[str, object] | None = None,
    ) -> list[dict[str, str]]:
        """Settle terminal clocks restored by an older save.

        Earlier execution paths could persist a non-ritual clock at maximum
        progress without archiving it.  There is no fresh ``ClockChange`` when
        that campaign is loaded, so waiting for the next action would leave a
        consequence permanently poised to happen a second time.  Reconstruct
        the terminal change from authoritative clock state and pass it through
        the same lifecycle used by live actions.
        """

        recovered: list[Any] = []
        for clock in list(self.clocks.all()):
            clock_type = str(clock.clock_type or "").strip()
            if (
                clock_type not in self.IMMEDIATE_TYPES
                or clock.current < clock.max_segments
                or clock.status in {"resolved", "abandoned", "archived"}
            ):
                continue
            recovered.append(
                _RecoveredClockChange(
                    clock_name=clock.name,
                    clock_type=clock_type,
                    after=clock.current,
                    max_segments=clock.max_segments,
                    stakes=clock.stakes,
                    completion_consequence=clock.completion_consequence,
                )
            )
        return self.settle_changes(recovered, payload=payload)

    def settle_local_resolution(
        self,
        state_change: dict[str, object] | None,
        *,
        scene_id: str = "",
        note: str = "",
    ) -> list[dict[str, str]]:
        """Retire finite clocks once the episode's local question is over.

        A scene clock is a tool for the current situation, not a permanent
        world fact. If a consequence beat explicitly resolves that situation,
        unfinished local clocks represent routes that were averted or made
        irrelevant. Session clocks deliberately survive scene changes and are
        retired only by an explicit tool decision or at session end; otherwise
        resolving one room could silently erase pressure spanning several
        scenes. Campaign clocks likewise remain untouched.
        """

        change = dict(state_change or {})
        if not (
            change.get("material_change")
            and change.get("local_question_resolved")
            and str(change.get("commitment_level") or "").strip().lower()
            == "consequence"
        ):
            return []

        current_scene_id = str(scene_id or "").strip()
        resolution_note = str(
            note
            or change.get("public_fact")
            or "本场核心问题已经解决，这个命刻不再继续。"
        ).strip()
        settled: list[dict[str, str]] = []
        for clock in list(self.clocks.all()):
            scope = str(clock.scope or "").strip().lower()
            if scope != "scene":
                continue
            if current_scene_id and clock.scene_id and clock.scene_id != current_scene_id:
                continue

            completed = clock.current >= clock.max_segments or clock.status == "fulfilled"
            if completed:
                self.clocks.resolve(clock.name, note=resolution_note, archive=True)
                status = "resolved"
            else:
                self.clocks.abandon(clock.name, note=resolution_note)
                status = "abandoned"
            settled.append(
                {
                    "clock_name": clock.name,
                    "clock_type": str(clock.clock_type or ""),
                    "status": status,
                    "consequence": resolution_note,
                }
            )
        return settled


class _RecoveredClockChange:
    """Minimal attribute carrier accepted by ``settle_changes``."""

    def __init__(
        self,
        *,
        clock_name: str,
        clock_type: str,
        after: int,
        max_segments: int,
        stakes: str,
        completion_consequence: str,
    ) -> None:
        self.clock_name = clock_name
        self.clock_type = clock_type
        self.after = after
        self.max_segments = max_segments
        self.stakes = stakes
        self.completion_consequence = completion_consequence
