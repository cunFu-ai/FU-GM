from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping

from fu_gm.testing.luna_player_agent import PlayerPersona


@dataclass(frozen=True)
class NaturalSessionZeroStatus:
    """Player-safe progress projection for a natural Session 0 table."""

    stage: str
    ready: bool
    progress: dict[str, bool]
    shared_missing: tuple[str, ...]
    missing_by_player: dict[str, tuple[str, ...]]
    hero_missing_by_player: dict[str, tuple[str, ...]]
    latest_pending_proposals: tuple[dict[str, object], ...]

    @property
    def fingerprint(self) -> str:
        return json.dumps(
            {
                "stage": self.stage,
                "ready": self.ready,
                "progress": self.progress,
                "shared_missing": self.shared_missing,
                "missing_by_player": self.missing_by_player,
                "hero_missing_by_player": self.hero_missing_by_player,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def action_bar(self) -> dict[str, object]:
        return {
            "phase": "session_zero",
            "session_zero_stage": self.stage,
            "session_zero_ready": self.ready,
            "shared_missing_categories": list(self.shared_missing),
            "session_zero_missing_by_player": {
                name: list(items)
                for name, items in self.missing_by_player.items()
            },
            "hero_missing_by_player": {
                name: list(items)
                for name, items in self.hero_missing_by_player.items()
            },
            "latest_pending_proposals": [
                dict(item) for item in self.latest_pending_proposals
            ],
        }


def _public_pending_proposals(manager: object) -> tuple[dict[str, object], ...]:
    """Project a few publicly discussed proposals without internal IDs.

    Proposal churn deliberately stays out of ``NaturalSessionZeroStatus``'s
    progress fingerprint: only committed checklist progress should reset the
    stagnation detector.  The projection still gives independent FU-PL agents
    enough shared context to settle a proposal instead of endlessly restating
    it.
    """

    state = getattr(manager, "state", None)
    world = getattr(state, "world", None)
    raw_proposals = getattr(world, "pending_proposals", ())
    if not isinstance(raw_proposals, (list, tuple)):
        return ()

    projected: list[dict[str, object]] = []
    for raw in raw_proposals[-4:]:
        if not isinstance(raw, Mapping):
            continue
        categories: list[str] = []
        for operation in raw.get("world_operations") or ():
            if not isinstance(operation, Mapping):
                continue
            category = str(operation.get("category") or "").strip()
            if category:
                categories.append(category)
        proposed_updates = raw.get("proposed_updates")
        if isinstance(proposed_updates, Mapping):
            categories.extend(
                str(key).strip()
                for key, value in proposed_updates.items()
                if str(key).strip() and value not in (None, "", (), [], {})
            )
        summary = str(raw.get("summary") or "").strip()
        if not summary:
            continue
        projected.append(
            {
                "speaker": str(raw.get("speaker") or "").strip(),
                "summary": summary[:360],
                "categories": list(dict.fromkeys(categories)),
            }
        )
    return tuple(projected)


def build_natural_session_zero_status(
    manager: object,
    personas: Mapping[str, PlayerPersona],
) -> NaturalSessionZeroStatus:
    """Read committed setup state without exposing other players' private notes."""

    progress = {
        str(key): bool(value)
        for key, value in dict(manager.progress_summary()).items()
    }
    roster = list(manager.contribution_roster())
    missing_by_player: dict[str, tuple[str, ...]] = {
        name: () for name in personas
    }
    for entry in roster:
        if not isinstance(entry, Mapping):
            continue
        player = str(entry.get("player") or "").strip()
        if player not in missing_by_player:
            continue
        labels: list[str] = []
        for item in entry.get("missing_topics") or []:
            if not isinstance(item, Mapping):
                continue
            label = str(item.get("label") or item.get("code") or "").strip()
            if label:
                labels.append(label)
        missing_by_player[player] = tuple(dict.fromkeys(labels))

    hero_status = dict(manager.hero_creation_status() or {})
    raw_hero_missing = dict(hero_status.get("missing_by_player") or {})
    hero_missing_by_player: dict[str, tuple[str, ...]] = {}
    for player, persona in personas.items():
        raw = raw_hero_missing.get(player)
        if raw is None:
            raw = raw_hero_missing.get(persona.hero_name)
        hero_missing_by_player[player] = tuple(
            str(item).strip() for item in (raw or []) if str(item).strip()
        )

    missing_topics = tuple(str(item) for item in manager.missing_topics())
    stage_value = getattr(getattr(manager, "state", None), "stage", "")
    stage = str(getattr(stage_value, "value", stage_value) or "session_zero")
    return NaturalSessionZeroStatus(
        stage=stage,
        ready=not missing_topics,
        progress=progress,
        shared_missing=missing_topics,
        missing_by_player=missing_by_player,
        hero_missing_by_player=hero_missing_by_player,
        latest_pending_proposals=_public_pending_proposals(manager),
    )


class NaturalSessionZeroLoopPolicy:
    """Bounds an open-ended model table without choosing a speaker or answer."""

    def __init__(
        self,
        *,
        max_waves: int = 120,
        max_stagnant_waves: int = 20,
        initial_wave_count: int = 0,
        initial_stagnant_waves: int = 0,
        initial_inactive_waves: int = 0,
        previous_fingerprint: str = "",
        previous_coordination_fingerprint: str = "",
    ) -> None:
        self.max_waves = max(1, int(max_waves))
        self.max_stagnant_waves = max(1, int(max_stagnant_waves))
        self.wave_count = max(0, int(initial_wave_count))
        self.stagnant_waves = max(0, int(initial_stagnant_waves))
        self.inactive_waves = max(0, int(initial_inactive_waves))
        self._last_fingerprint = str(previous_fingerprint or "")
        self._last_coordination_fingerprint = str(
            previous_coordination_fingerprint or ""
        )

    @property
    def coordination_fingerprint(self) -> str:
        return self._last_coordination_fingerprint

    def observe(
        self,
        status: NaturalSessionZeroStatus,
        *,
        coordination_fingerprint: str = "",
        table_activity: bool = False,
    ) -> None:
        self.wave_count += 1
        coordination = str(coordination_fingerprint or "")
        status_changed = status.fingerprint != self._last_fingerprint
        new_handoff = bool(coordination) and (
            coordination != self._last_coordination_fingerprint
        )
        if status_changed or new_handoff:
            self.stagnant_waves = 0
            self.inactive_waves = 0
            self._last_fingerprint = status.fingerprint
            if coordination:
                self._last_coordination_fingerprint = coordination
        else:
            self.stagnant_waves += 1
            if table_activity:
                self.inactive_waves = 0
            else:
                self.inactive_waves += 1

    def progress_nudge_due(
        self,
        status: NaturalSessionZeroStatus,
        *,
        after_stagnant_waves: int,
        last_nudge_fingerprint: str = "",
        last_nudge_wave: int = 0,
        repeat_after_waves: int = 6,
    ) -> bool:
        """Return whether a cooled-down GM handoff is due.

        This deliberately observes committed Session 0 state rather than player
        wording. Fresh progress re-arms immediately after the normal stagnation
        threshold. If players ignore one handoff, the same unresolved checklist
        may be nudged again only after a longer bounded interval.
        """

        if status.ready or self.stagnant_waves < max(1, int(after_stagnant_waves)):
            return False
        if status.fingerprint != str(last_nudge_fingerprint or ""):
            return True
        prior_wave = max(0, int(last_nudge_wave))
        return bool(
            prior_wave
            and (self.wave_count + 1 - prior_wave)
            >= max(2, int(repeat_after_waves))
        )

    def failure_reason(self, status: NaturalSessionZeroStatus) -> str:
        if self.wave_count >= self.max_waves:
            return (
                f"自然第零章达到 {self.max_waves} 个公开消息周期仍未完成："
                + "、".join(status.shared_missing)
            )
        if self.inactive_waves >= self.max_stagnant_waves:
            return (
                f"自然第零章连续 {self.max_stagnant_waves} 个周期没有公开活动："
                + "、".join(status.shared_missing)
            )
        return ""
