from __future__ import annotations

from typing import Any

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.models import Clock, SwallowedTargetState


class NPCConditionManager:
    """Own persistent multi-step conditions created by NPC abilities.

    Swallowing is not a normal status: it restricts actions, deals damage at a
    turn boundary, owns a clock, and reacts whenever the source loses HP.  This
    component keeps those facts in one recoverable conflict transaction.
    """

    def __init__(
        self,
        characters: CharacterManager,
        clocks: ClockManager,
        conflicts: ConflictManager,
    ) -> None:
        self.characters = characters
        self.clocks = clocks
        self.conflicts = conflicts

    def swallowed(self, target: str) -> SwallowedTargetState | None:
        return self.conflicts.state.swallowed_targets.get(str(target or "").strip())

    def swallowed_by(self, source: str) -> list[SwallowedTargetState]:
        clean_source = str(source or "").strip()
        return [
            state
            for state in self.conflicts.state.swallowed_targets.values()
            if state.source == clean_source
        ]

    def capacity_for(self, source: str) -> int:
        rank = self.conflicts.state.enemy_ranks.get(str(source or "").strip())
        action_count = int(
            self.conflicts.state.enemy_action_counts.get(str(source or "").strip(), 1)
            or 1
        )
        return 2 if str(getattr(rank, "value", rank) or "") == "champion" and action_count >= 3 else 1

    def swallow(
        self,
        source: str,
        target: str,
        *,
        damage: int = 20,
        damage_type: str = "physical",
        clock_segments: int = 4,
    ) -> SwallowedTargetState:
        clean_source = str(source or "").strip()
        clean_target = str(target or "").strip()
        if not self.conflicts.state.active:
            raise ValueError("吞噬只能在冲突场景中持续结算。")
        if not self.characters.exists(clean_source) or not self.characters.exists(clean_target):
            raise ValueError("吞噬的来源与目标都必须是已建档生物。")
        existing = self.swallowed(clean_target)
        if existing is not None:
            if existing.source == clean_source:
                return existing
            raise ValueError(f"【{clean_target}】已经被【{existing.source}】吞噬。")
        if len(self.swallowed_by(clean_source)) >= self.capacity_for(clean_source):
            raise ValueError(f"【{clean_source}】已经达到可吞噬目标的上限。")

        clock_name = f"脱离【{clean_source}】的吞噬（{clean_target}）"
        if self.clocks.exists(clock_name):
            clock = self.clocks.get(clock_name)
            if clock.status in {"resolved", "abandoned", "archived"}:
                raise ValueError(f"脱困命刻【{clock_name}】已经结束。")
        else:
            self.clocks.add(
                Clock(
                    name=clock_name,
                    max_segments=max(1, int(clock_segments or 4)),
                    clock_type="objective",
                    stakes=f"填满后【{clean_target}】脱离【{clean_source}】的吞噬。",
                    gm_note="被吞噬者只能用推进目标行动影响此命刻。",
                    scope="scene",
                    scene_id="",
                    owner=clean_target,
                    source=clean_source,
                )
            )
        state = SwallowedTargetState(
            source=clean_source,
            target=clean_target,
            escape_clock=clock_name,
            damage=max(0, int(damage or 0)),
            damage_type=str(damage_type or "physical"),
            created_round=int(self.conflicts.state.round_number or 0),
        )
        self.conflicts.state.swallowed_targets[clean_target] = state
        return state

    def action_restriction_reason(
        self,
        actor: str,
        action_type: str,
        *,
        clock_name: str = "",
    ) -> str:
        state = self.swallowed(actor)
        if state is None:
            return ""
        if str(action_type or "") != "Objective":
            return (
                f"【{state.target}】正被【{state.source}】吞噬，"
                f"只能推进脱困命刻【{state.escape_clock}】。"
            )
        if str(clock_name or "").strip() != state.escape_clock:
            return f"被吞噬期间只能推进脱困命刻【{state.escape_clock}】。"
        return ""

    def advance_for_source_damage(
        self,
        source: str,
        *,
        segments: int = 1,
    ) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        for state in list(self.swallowed_by(source)):
            if not self.clocks.exists(state.escape_clock):
                self.release(state.target, reason="脱困命刻已不存在")
                continue
            clock = self.clocks.get(state.escape_clock)
            before, after = self.clocks.advance(
                state.escape_clock,
                max(0, int(segments or 0)),
            )
            released = after >= clock.max_segments
            changes.append(
                {
                    "source": state.source,
                    "target": state.target,
                    "clock_name": state.escape_clock,
                    "before": before,
                    "after": after,
                    "max_segments": clock.max_segments,
                    "released": released,
                }
            )
            if released:
                self.release(state.target, reason="陷龙花受伤，脱困命刻填满")
        return changes

    def release(self, target: str, *, reason: str = "脱困命刻完成") -> SwallowedTargetState | None:
        state = self.conflicts.state.swallowed_targets.pop(
            str(target or "").strip(),
            None,
        )
        if state is None:
            return None
        if self.clocks.exists(state.escape_clock):
            self.clocks.resolve(state.escape_clock, note=reason, archive=True)
        return state

    def release_completed(self) -> list[SwallowedTargetState]:
        released: list[SwallowedTargetState] = []
        for state in list(self.conflicts.state.swallowed_targets.values()):
            if not self.clocks.exists(state.escape_clock):
                item = self.release(state.target, reason="脱困命刻已结束")
            else:
                clock = self.clocks.get(state.escape_clock)
                item = (
                    self.release(state.target)
                    if clock.current >= clock.max_segments
                    else None
                )
            if item is not None:
                released.append(item)
        return released


__all__ = ["NPCConditionManager"]
