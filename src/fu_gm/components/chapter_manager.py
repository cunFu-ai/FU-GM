from __future__ import annotations

from fu_gm.components.economy_manager import EconomyManager
from fu_gm.components.progression_manager import ProgressionManager
from fu_gm.components.world_state import WorldState
from fu_gm.models import ChapterSettlement


class ChapterManager:
    """章节结算：经验、奖励、升级提示和世界变化摘要。"""

    def __init__(
        self,
        progression_manager: ProgressionManager,
        economy_manager: EconomyManager,
        world_state: WorldState,
    ) -> None:
        self.progression_manager = progression_manager
        self.economy_manager = economy_manager
        self.world_state = world_state
        self.history: list[ChapterSettlement] = []

    def settle_chapter(
        self,
        *,
        chapter_title: str,
        participating_pcs: list[str],
        party_level: int,
        ultima_spent: int = 0,
        fabula_spent: int = 0,
        difficulty: str = "normal",
        rare_item: str = "",
        world_change_limit: int = 8,
    ) -> ChapterSettlement:
        xp_report = self.progression_manager.award_session_experience(
            participating_pcs=participating_pcs,
            ultima_spent=ultima_spent,
            fabula_spent=fabula_spent,
        )
        reward = self.economy_manager.award_session_treasure(
            participating_pcs,
            party_level=party_level,
            difficulty=difficulty,
            rare_item=rare_item,
        )
        level_up_available = [
            gain.character_name for gain in xp_report.gains if self.progression_manager.can_level_up(gain.character_name)
        ]
        world_changes = self.world_change_summary(limit=world_change_limit)
        summary = self._build_summary(
            chapter_title=chapter_title,
            xp_summary=xp_report.summary,
            reward_summary=reward.summary,
            level_up_available=level_up_available,
            world_changes=world_changes,
        )
        settlement = ChapterSettlement(
            chapter_title=chapter_title,
            participating_pcs=list(participating_pcs),
            experience_report=xp_report,
            reward=reward,
            world_changes=world_changes,
            level_up_available=level_up_available,
            summary=summary,
        )
        self.history.append(settlement)
        self.world_state.record_memory_event(
            summary,
            kind="chapter_settlement",
            entities=participating_pcs,
            tags=["chapter", "settlement", difficulty],
            payload={
                "chapter_title": chapter_title,
                "xp": xp_report.total_xp,
                "reward_zenit": reward.zenit,
                "level_up_available": level_up_available,
            },
        )
        return settlement

    def world_change_summary(self, *, limit: int = 8) -> list[str]:
        changes: list[str] = []
        for change in reversed(self.world_state.persistent_changes):
            changes.append(
                f"{change.name}：{change.description}"
                + (f"（地点：{change.location}）" if change.location else "")
                + (f"（持有者：{change.owner}）" if change.owner else "")
            )
            if len(changes) >= limit:
                return list(reversed(changes))
        for event in reversed(self.world_state.memory_events):
            if event.kind in {"story_change", "ritual", "project_completed", "treasure", "scene_end"}:
                changes.append(event.summary)
            if len(changes) >= limit:
                break
        return list(reversed(changes))

    def _build_summary(
        self,
        *,
        chapter_title: str,
        xp_summary: str,
        reward_summary: str,
        level_up_available: list[str],
        world_changes: list[str],
    ) -> str:
        level_text = "可升级：" + "、".join(level_up_available) if level_up_available else "暂无角色达到升级条件。"
        changes_text = "；".join(world_changes) if world_changes else "本章暂无新的长期世界变化。"
        return f"章节【{chapter_title}】结算。{xp_summary} {reward_summary} {level_text} 世界变化：{changes_text}"
