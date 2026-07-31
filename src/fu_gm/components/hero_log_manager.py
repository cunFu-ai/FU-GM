from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.sheet_exporter import ATTRIBUTE_LABELS, DAMAGE_TYPE_LABELS, RANGE_LABELS
from fu_gm.models import ChapterBeat, ChapterRunRecord, ChapterSettlement, HeroLogEntry, RareItemApproval, RareItemDesign


class HeroLogManager:
    """Per-hero audit trail for chapter play, rewards and approvals.

    This intentionally stays small and file-snapshot friendly. It is not the
    story summary; it is the authoritative bookkeeping layer a GM can inspect.
    """

    def __init__(self) -> None:
        self.entries: list[HeroLogEntry] = []
        self.chapter_runs: list[ChapterRunRecord] = []
        self.rare_item_approvals: list[RareItemApproval] = []

    def record_chapter_settlement(
        self,
        settlement: ChapterSettlement,
        *,
        campaign_id: str = "",
        session_id: str = "",
        gm_name: str = "",
        character_manager: CharacterManager | None = None,
        story_flags: list[str] | None = None,
    ) -> list[HeroLogEntry]:
        xp_by_hero = {
            gain.character_name: gain.amount
            for gain in settlement.experience_report.gains
        }
        share = self._reward_share(settlement.reward.zenit, settlement.participating_pcs)
        created: list[HeroLogEntry] = []
        for hero_name in settlement.participating_pcs:
            character = character_manager.get(hero_name) if character_manager and character_manager.exists(hero_name) else None
            entry = HeroLogEntry(
                hero_name=hero_name,
                chapter_title=settlement.chapter_title,
                campaign_id=campaign_id,
                session_id=session_id,
                gm_name=gm_name,
                created_at=self._now(),
                starting_level=character.level if character else 0,
                ending_level=character.level if character else 0,
                xp_awarded=xp_by_hero.get(hero_name, settlement.experience_report.total_xp),
                zenit_awarded=share,
                rare_items=list(settlement.reward.rare_items),
                rewards=[settlement.reward.summary] if settlement.reward.summary else [],
                story_flags=list(story_flags or settlement.world_changes),
                notes=[settlement.summary] if settlement.summary else [],
            )
            self.entries.append(entry)
            created.append(entry)
        self.chapter_runs.append(
            ChapterRunRecord(
                chapter_title=settlement.chapter_title,
                session_id=session_id,
                campaign_id=campaign_id,
                status="settled",
                participants=list(settlement.participating_pcs),
                rewards=[settlement.reward.summary] if settlement.reward.summary else [],
                summary=settlement.summary,
            )
        )
        return created

    def start_chapter_run(
        self,
        *,
        chapter_title: str,
        participants: list[str] | None = None,
        campaign_id: str = "",
        session_id: str = "",
        gm_name: str = "",
        synopsis: str = "",
        intro_prompt: str = "",
        conclusion_prompt: str = "",
        shared_creation_slots: list[str] | None = None,
        iconic_elements: list[str] | None = None,
        timebox_minutes: int = 180,
    ) -> ChapterRunRecord:
        """Create an official-campaign style run scaffold for dashboard audit."""

        slots = list(shared_creation_slots or [])
        beats = [
            ChapterBeat("前情与章节目标", "recap", "pending", 10, synopsis),
            ChapterBeat("开场场景", "intro", "pending", 20, intro_prompt),
            ChapterBeat("本桌共创变量", "shared_creation", "pending", 20, "、".join(slots)),
            ChapterBeat("主要场景与冲突", "scene", "pending", max(60, int(timebox_minutes) - 70), ""),
            ChapterBeat("结尾与章节奖励", "conclusion", "pending", 20, conclusion_prompt),
        ]
        record = ChapterRunRecord(
            chapter_title=chapter_title,
            campaign_id=campaign_id,
            session_id=session_id,
            status="running",
            gm_name=gm_name,
            synopsis=synopsis,
            intro_prompt=intro_prompt,
            conclusion_prompt=conclusion_prompt,
            timebox_minutes=max(30, int(timebox_minutes or 180)),
            participants=list(participants or []),
            beats=beats,
            shared_creation_slots=slots,
            iconic_elements=list(iconic_elements or []),
            warnings=self._chapter_run_warnings(iconic_elements or []),
            summary=self._chapter_run_summary(chapter_title, synopsis, slots),
        )
        self.chapter_runs.append(record)
        return record

    def record_chapter_beat(
        self,
        chapter_title: str,
        *,
        title: str,
        beat_type: str = "scene",
        status: str = "done",
        expected_minutes: int = 0,
        summary: str = "",
        notes: list[str] | None = None,
    ) -> ChapterBeat:
        record = self._latest_chapter_run(chapter_title)
        beat = ChapterBeat(
            title=title,
            beat_type=beat_type,
            status=status,
            expected_minutes=max(0, int(expected_minutes or 0)),
            summary=summary,
            notes=list(notes or []),
        )
        if record is None:
            record = self.start_chapter_run(chapter_title=chapter_title)
        record.beats.append(beat)
        if summary:
            record.gm_scenes.append(summary)
        return beat

    def chapter_start_audit(
        self,
        *,
        participating_pcs: list[str],
        temporary_bonds: list[str] | None = None,
        fabula_points: int = 3,
    ) -> dict[str, Any]:
        return {
            "participants": list(participating_pcs),
            "fabula_reset": f"章节开始时每名 PC 的物语点应为 {fabula_points} 点；上一章未花完的不累积。",
            "temporary_bonds": list(temporary_bonds or []),
            "downtime_prompt": "章节间可恢复 HP/MP、购买/出售物品、补充物资点，并提交造物使项目或稀有物品审批。",
        }

    def record_manual_entry(
        self,
        *,
        hero_name: str,
        chapter_title: str,
        campaign_id: str = "",
        session_id: str = "",
        player_name: str = "",
        gm_name: str = "",
        rewards: list[str] | None = None,
        story_flags: list[str] | None = None,
        notes: list[str] | None = None,
    ) -> HeroLogEntry:
        entry = HeroLogEntry(
            hero_name=hero_name,
            chapter_title=chapter_title,
            campaign_id=campaign_id,
            session_id=session_id,
            player_name=player_name,
            gm_name=gm_name,
            created_at=self._now(),
            rewards=list(rewards or []),
            story_flags=list(story_flags or []),
            notes=list(notes or []),
        )
        self.entries.append(entry)
        return entry

    def request_rare_item_approval(
        self,
        *,
        item_name: str,
        requester: str = "",
        item_type: str = "",
        source: str = "",
        price: int = 0,
        effects: list[str] | None = None,
        notes: list[str] | None = None,
    ) -> RareItemApproval:
        approval = RareItemApproval(
            request_id=f"rare-{len(self.rare_item_approvals) + 1}",
            item_name=item_name,
            requester=requester,
            item_type=item_type,
            source=source,
            price=price,
            effects=list(effects or []),
            notes=list(notes or []),
            created_at=self._now(),
        )
        self.rare_item_approvals.append(approval)
        return approval

    def request_rare_item_design_approval(
        self,
        design: RareItemDesign,
        *,
        requester: str = "",
        source: str = "",
        notes: list[str] | None = None,
    ) -> RareItemApproval:
        """Create an approval request from the core rare-item design guide."""

        quality_text = [
            f"{quality.name}：{quality.description}"
            for quality in design.qualities
        ]
        design_notes = [
            f"基础模板：{design.base_item}",
            f"参考价格：{design.price}Z",
        ]
        if design.required_ability:
            design_notes.append(f"装备权限：{design.required_ability}")
        if design.item_type == "weapon" or getattr(design.item_type, "value", design.item_type) == "weapon":
            attrs = (
                "+".join(ATTRIBUTE_LABELS.get(attribute, attribute) for attribute in design.accuracy_attributes)
                if design.accuracy_attributes
                else "继承基础武器"
            )
            damage_type = DAMAGE_TYPE_LABELS.get(design.damage_type, design.damage_type)
            hands = {1: "单手", 2: "双手"}.get(design.hands, "继承手数")
            range_type = RANGE_LABELS.get(design.range_type, design.range_type or "继承射程")
            design_notes.append(
                f"武器参数：命中【{attrs}】"
                f"{'+' + str(design.accuracy_modifier) if design.accuracy_modifier else ''}；"
                f"伤害【高值+{design.damage_bonus}】{damage_type}；"
                f"{hands}；{range_type}。"
            )
        approval = self.request_rare_item_approval(
            item_name=design.name,
            requester=requester,
            item_type=getattr(design.item_type, "value", str(design.item_type)),
            source=source or "稀有物品设计器",
            price=design.price,
            effects=[*quality_text],
            notes=[*design_notes, *design.notes, *(notes or [])],
        )
        return approval

    def approve_rare_item(self, request_id: str, *, note: str = "") -> RareItemApproval:
        approval = self._approval(request_id)
        approval.status = "approved"
        approval.approved_at = self._now()
        if note:
            approval.notes.append(note)
        return approval

    def reject_rare_item(self, request_id: str, *, note: str = "") -> RareItemApproval:
        approval = self._approval(request_id)
        approval.status = "rejected"
        approval.approved_at = self._now()
        if note:
            approval.notes.append(note)
        return approval

    def has_participated(self, hero_name: str, chapter_title: str) -> bool:
        return any(
            entry.hero_name == hero_name and entry.chapter_title == chapter_title
            for entry in self.entries
        )

    def audit_payload(self, *, limit: int = 20) -> dict[str, Any]:
        return {
            "entries": [asdict(entry) for entry in self.entries[-limit:]],
            "chapter_runs": [asdict(run) for run in self.chapter_runs[-limit:]],
            "rare_item_approvals": [asdict(item) for item in self.rare_item_approvals[-limit:]],
            "warnings": self._warnings(),
            "usage_note": "英雄日志记录每个 PC 的章节参与、奖励、稀有物品审批和长期旗标；不是给玩家朗读的叙事摘要。",
        }

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "entries": [asdict(entry) for entry in self.entries],
            "chapter_runs": [asdict(run) for run in self.chapter_runs],
            "rare_item_approvals": [asdict(item) for item in self.rare_item_approvals],
        }

    def apply_snapshot(self, snapshot: dict[str, Any] | None) -> None:
        snapshot = snapshot or {}
        self.entries = [HeroLogEntry(**item) for item in snapshot.get("entries", [])]
        self.chapter_runs = [self._chapter_run_from_snapshot(item) for item in snapshot.get("chapter_runs", [])]
        self.rare_item_approvals = [
            RareItemApproval(**item) for item in snapshot.get("rare_item_approvals", [])
        ]

    def _approval(self, request_id: str) -> RareItemApproval:
        for approval in self.rare_item_approvals:
            if approval.request_id == request_id:
                return approval
        raise KeyError(f"未知稀有物品审批：{request_id}")

    def _reward_share(self, total_zenit: int, pcs: list[str]) -> int:
        if not pcs:
            return 0
        return max(0, int(total_zenit) // len(pcs))

    def _warnings(self) -> list[str]:
        warnings: list[str] = []
        seen: set[tuple[str, str]] = set()
        for entry in self.entries:
            key = (entry.hero_name, entry.chapter_title)
            if key in seen:
                warnings.append(f"{entry.hero_name} 已有章节【{entry.chapter_title}】记录；官方战役模式下通常不应重复领取奖励。")
            seen.add(key)
        if any(item.status == "pending" for item in self.rare_item_approvals):
            warnings.append("仍有待审批的稀有物品或制作成果。")
        return warnings

    def _latest_chapter_run(self, chapter_title: str) -> ChapterRunRecord | None:
        for record in reversed(self.chapter_runs):
            if record.chapter_title == chapter_title:
                return record
        return None

    def _chapter_run_from_snapshot(self, item: dict[str, Any]) -> ChapterRunRecord:
        data = dict(item)
        data["beats"] = [
            beat if isinstance(beat, ChapterBeat) else ChapterBeat(**beat)
            for beat in data.get("beats", [])
        ]
        return ChapterRunRecord(**data)

    def _chapter_run_warnings(self, iconic_elements: list[str]) -> list[str]:
        if iconic_elements:
            return ["标志性元素只能按章节允许的方式互动；不要让单桌结果永久改写它们。"]
        return []

    def _chapter_run_summary(self, chapter_title: str, synopsis: str, slots: list[str]) -> str:
        pieces = [f"章节【{chapter_title}】已建立运行脚手架。"]
        if synopsis:
            pieces.append(synopsis)
        if slots:
            pieces.append("本桌共创变量：" + "、".join(slots))
        return " ".join(pieces)

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
