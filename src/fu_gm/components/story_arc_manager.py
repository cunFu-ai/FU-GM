from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.world_state import WorldState
from fu_gm.models import (
    CampaignArcState,
    LocationReturnState,
    NextSessionAgenda,
    RevealCandidate,
    StoryArcPhase,
    StorySessionSummary,
    StoryThread,
    VillainPressureTrack,
    WorldCreationProfile,
)


class StoryArcManager:
    """长期战役节奏器。

    它不负责替玩家写死剧情，而是把第零章和每场总结沉淀成可审计的后台状态：
    还有哪些线索没解决、哪些反派目标在推进、哪些真相适合中期揭示、哪些地点值得回访。
    """

    def __init__(
        self,
        world_state: WorldState,
        clock_manager: ClockManager | None = None,
        state: CampaignArcState | None = None,
    ) -> None:
        self.world_state = world_state
        self.clock_manager = clock_manager
        self.state = state or CampaignArcState()

    def sync_from_world_profile(self, world: WorldCreationProfile | None = None) -> CampaignArcState:
        world = world or self.world_state.world_profile
        if world.selected_first_act_summary:
            item = self._normalize_story_item(world.selected_first_act_summary, fallback_title="第一幕开局")
            self._ensure_thread(
                "first_act",
                item["key"],
                thread_type="first_act",
                summary=item["summary"],
                priority=3,
                source="world.selected_first_act_summary",
                title=item["title"],
            )
        for seed in world.villain_seeds:
            item = self._normalize_story_item(seed, fallback_title="反派种子")
            thread = self._ensure_thread(
                "villain",
                item["key"],
                thread_type="villain_seed",
                summary=item["summary"],
                priority=3,
                source="world.villain_seeds",
                title=item["title"],
            )
            self._ensure_pressure(
                "villain",
                item["key"],
                villain=item["title"],
                goal=item["summary"],
                segments=8,
                related_threads=[thread.thread_id],
                source="world.villain_seeds",
            )
        for threat in world.world_threats:
            item = self._normalize_story_item(threat, fallback_title="世界威胁")
            thread = self._ensure_thread(
                "threat",
                item["key"],
                thread_type="world_threat",
                summary=item["summary"],
                priority=3,
                source="world.world_threats",
                title=item["title"],
            )
            self._ensure_pressure(
                "threat",
                item["key"],
                villain=self._threat_pressure_actor(item["summary"]),
                goal=item["summary"],
                segments=10,
                related_threads=[thread.thread_id],
                source="world.world_threats",
            )
        for mystery in world.mysteries:
            item = self._normalize_story_item(mystery, fallback_title="世界奥秘")
            thread = self._ensure_thread(
                "mystery",
                item["key"],
                thread_type="mystery",
                summary=item["summary"],
                priority=2,
                source="world.mysteries",
                title=item["title"],
            )
            self._ensure_reveal(
                "mystery",
                item["key"],
                title=item["title"],
                secret=item["summary"],
                related_entities=self._known_entities_in_text(item["summary"]),
                source="world.mysteries",
                thread=thread,
            )
        for mirror in world.villain_mirrors:
            item = self._normalize_story_item(mirror, fallback_title="反派映照")
            self._ensure_thread(
                "mirror",
                item["key"],
                thread_type="villain_mirror",
                summary=item["summary"],
                priority=2,
                source="world.villain_mirrors",
                title=item["title"],
            )
        for note in world.gm_secret_notes:
            item = self._normalize_story_item(note, fallback_title="GM暗线")
            self._ensure_reveal(
                "secret_note",
                item["key"],
                title=item["title"],
                secret=item["summary"],
                related_entities=self._known_entities_in_text(item["summary"]),
                source="world.gm_secret_notes",
            )
        for name, brief in world.major_locations.items():
            self._ensure_location(name, status="public", source="world.major_locations", next_prompt=brief)
        if world.starting_region:
            self._ensure_location(
                world.starting_region,
                status="public",
                source="world.starting_region",
                next_prompt="作为起点地区，优先让它随英雄行动发生变化。",
            )
        for name, brief in world.gm_prepared_locations.items():
            self._ensure_location(
                name,
                status="backstage_candidate",
                source="world.gm_prepared_locations",
                next_prompt=brief,
            )
        for draft in world.hero_drafts.values():
            if not draft.hero_name and not draft.identity and not draft.theme:
                continue
            title = " / ".join(part for part in [draft.hero_name, draft.identity, draft.theme] if part)
            self._ensure_thread(
                "hero",
                title,
                thread_type="hero_theme",
                summary="；".join([*draft.notes[:2], *draft.open_questions[:2]]),
                entities=[item for item in [draft.hero_name, draft.player_name] if item],
                related_tags=[tag for tag in [draft.identity, draft.theme, draft.origin] if tag],
                priority=2,
                source="world.hero_drafts",
            )
        self._dedupe_state()
        self._refresh_phase()
        self._refresh_agenda()
        self.state.last_updated = self._now()
        return self.state

    @staticmethod
    def _threat_pressure_actor(summary: str) -> str:
        """Use the faction named in a threat instead of a category label."""

        text = " ".join(str(summary or "").split()).strip()
        suffixes = (
            "财团",
            "司教团",
            "教团",
            "守望会",
            "行会",
            "王室",
            "帝国",
            "王国",
            "公国",
            "联盟",
            "军团",
            "教会",
            "协会",
            "公司",
        )
        pattern = "|".join(re.escape(item) for item in suffixes)
        match = re.search(rf"([\u4e00-\u9fffA-Za-z0-9·]{{1,16}}?(?:{pattern}))", text)
        return match.group(1) if match else "世界威胁"

    def update_from_session_summary(self, summary: StorySessionSummary) -> CampaignArcState:
        self.sync_from_world_profile()
        first_seen = summary.session_id not in self.state.processed_session_ids
        if first_seen:
            self.state.processed_session_ids.append(summary.session_id)
            self.state.session_count += 1
        self._refresh_phase()

        text_blob = self._summary_text(summary)
        for unresolved in summary.unresolved_threads:
            thread = self._ensure_thread(
                "session_thread",
                unresolved,
                thread_type="unresolved_thread",
                summary=unresolved,
                priority=2,
                source=f"session:{summary.session_id}",
            )
            self._append_unique(thread.public_clues, summary.short_memory or summary.title)
            thread.status = "active"

        for thread in self.state.threads:
            if self._text_mentions(text_blob, thread.title) or any(
                self._text_mentions(text_blob, entity) for entity in thread.entities
            ):
                thread.progress = min(12, thread.progress + (1 if first_seen else 0))
                if thread.status == "seeded":
                    thread.status = "active"
                clue = summary.short_memory or summary.public_summary[:120]
                self._append_unique(thread.public_clues, clue)
            if self._looks_resolved(text_blob, thread.title):
                thread.status = "resolved"

        for location in summary.locations:
            state = self._ensure_location(location, status="public", source=f"session:{summary.session_id}")
            state.last_seen = summary.session_id
            for item in summary.timeline:
                if self._text_mentions(item, location):
                    self._append_unique(state.changes, item)
            if not state.changes and summary.short_memory:
                self._append_unique(state.changes, summary.short_memory)

        for reveal in self.state.reveals:
            if reveal.status == "revealed":
                continue
            if self._text_mentions(text_blob, reveal.title) or any(
                self._text_mentions(text_blob, entity) for entity in reveal.related_entities
            ):
                self._append_unique(reveal.public_clues, summary.short_memory or summary.title)
                if len(reveal.public_clues) >= reveal.required_clues and self.state.phase in {
                    StoryArcPhase.MIDPOINT,
                    StoryArcPhase.CRISIS,
                    StoryArcPhase.FINALE,
                }:
                    reveal.status = "ready"

        for note in summary.private_notes:
            self._ensure_reveal(
                "session_secret",
                note,
                title=self._short_title(note),
                secret=note,
                related_entities=summary.entities,
                source=f"session:{summary.session_id}:private",
            )

        self._update_pressure_from_summary(summary, text_blob, first_seen=first_seen)
        self._dedupe_state()
        self._refresh_phase()
        self._refresh_agenda()
        self.state.last_updated = self._now()
        return self.state

    def advance_villain_pressure(self, track_id: str, *, amount: int = 1, reason: str = "") -> VillainPressureTrack:
        track = self._pressure_by_id(track_id)
        if track is None:
            raise ValueError(f"未知反派压力轨：{track_id}")
        track.current = max(0, min(track.segments, track.current + amount))
        if reason:
            track.last_action = reason
        track.stage = "danger" if track.current >= track.segments - 1 else "active"
        self._refresh_agenda()
        self.state.last_updated = self._now()
        return track

    def mark_reveal(self, reveal_id: str, *, clue: str = "", revealed: bool = False) -> RevealCandidate:
        reveal = self._reveal_by_id(reveal_id)
        if reveal is None:
            raise ValueError(f"未知揭示候选：{reveal_id}")
        if clue:
            self._append_unique(reveal.public_clues, clue)
        if revealed:
            reveal.status = "revealed"
        elif len(reveal.public_clues) >= reveal.required_clues:
            reveal.status = "ready"
        self._refresh_agenda()
        self.state.last_updated = self._now()
        return reveal

    def prompt_summary(self) -> dict[str, Any]:
        self.sync_from_world_profile()
        return {
            "phase": self.state.phase.value,
            "session_count": self.state.session_count,
            "active_threads": [
                {
                    "title": thread.title,
                    "type": thread.thread_type,
                    "status": thread.status,
                    "progress": thread.progress,
                    "public_clues": list(thread.public_clues[-3:]),
                }
                for thread in self._active_threads()[:5]
            ],
            "villain_pressure": [
                {
                    "villain": track.villain,
                    "goal": track.goal,
                    "stage": track.stage,
                    "current": track.current,
                    "segments": track.segments,
                    "visible_consequence": track.visible_consequence,
                    "last_action": track.last_action,
                }
                for track in self._active_pressure()[:4]
            ],
            "reveal_candidates": [
                {
                    "title": reveal.title,
                    "status": reveal.status,
                    "clue_count": len(reveal.public_clues),
                    "required_clues": reveal.required_clues,
                    "best_phase": reveal.best_phase,
                }
                for reveal in self._pending_reveals()[:4]
            ],
            "return_locations": [
                {
                    "location": location.location,
                    "status": location.status,
                    "last_seen": location.last_seen,
                    "next_prompt": location.next_prompt,
                }
                for location in self.state.locations[:5]
            ],
            "agenda": asdict(self.state.agenda),
        }

    def audit_payload(self, *, include_private: bool = False) -> dict[str, Any]:
        self.sync_from_world_profile()
        payload = asdict(self.state)
        payload["phase"] = self.state.phase.value
        if not include_private:
            for reveal in payload.get("reveals", []):
                reveal.pop("secret", None)
            for thread in payload.get("threads", []):
                thread.pop("private_notes", None)
        payload["usage_note"] = (
            "长期故事节奏是 GM 后台导演台：用来追踪压力、伏笔、地点变化和下一场议程；"
            "未公开真相不能直接告诉玩家。"
        )
        return payload

    def _ensure_thread(
        self,
        prefix: str,
        key: str,
        *,
        thread_type: str,
        summary: str = "",
        entities: list[str] | None = None,
        related_tags: list[str] | None = None,
        priority: int = 1,
        source: str = "",
        title: str = "",
    ) -> StoryThread:
        thread_id = self._stable_id(prefix, key)
        existing = self._thread_by_id(thread_id)
        if existing is not None:
            if title:
                existing.title = self._clean_text(title)
            if summary and not existing.summary:
                existing.summary = summary
            existing.priority = max(existing.priority, priority)
            for entity in entities or []:
                self._append_unique(existing.entities, entity)
            for tag in related_tags or []:
                self._append_unique(existing.related_tags, tag)
            return existing
        thread = StoryThread(
            thread_id=thread_id,
            title=self._clean_text(title or key),
            thread_type=thread_type,
            summary=summary,
            entities=list(dict.fromkeys(entities or [])),
            related_tags=list(dict.fromkeys(related_tags or [])),
            priority=priority,
            source=source,
        )
        self.state.threads.append(thread)
        return thread

    def _ensure_pressure(
        self,
        prefix: str,
        key: str,
        *,
        villain: str,
        goal: str,
        segments: int,
        related_threads: list[str],
        source: str,
    ) -> VillainPressureTrack:
        track_id = self._stable_id(prefix, key)
        existing = self._pressure_by_id(track_id)
        if existing is not None:
            for thread_id in related_threads:
                self._append_unique(existing.related_threads, thread_id)
            return existing
        track = VillainPressureTrack(
            track_id=track_id,
            villain=villain,
            goal=goal,
            clock_name=f"{villain}：{self._short_title(goal)}",
            segments=segments,
            visible_consequence="若长期无人阻止，这个目标会以可见后果推进。",
            related_threads=related_threads,
            source=source,
        )
        self.state.villain_pressure.append(track)
        return track

    def _ensure_reveal(
        self,
        prefix: str,
        key: str,
        *,
        title: str,
        secret: str,
        related_entities: list[str],
        source: str,
        thread: StoryThread | None = None,
    ) -> RevealCandidate:
        reveal_id = self._stable_id(prefix, key)
        existing = self._reveal_by_id(reveal_id)
        if existing is not None:
            for entity in related_entities:
                self._append_unique(existing.related_entities, entity)
            return existing
        reveal = RevealCandidate(
            reveal_id=reveal_id,
            title=title or self._short_title(secret),
            secret=secret,
            related_entities=list(dict.fromkeys(related_entities)),
            source=source,
            best_phase="midpoint" if thread is None or thread.thread_type != "world_threat" else "crisis",
        )
        self.state.reveals.append(reveal)
        return reveal

    def _ensure_location(
        self,
        location: str,
        *,
        status: str,
        source: str,
        next_prompt: str = "",
    ) -> LocationReturnState:
        clean = self._clean_text(location)
        for state in self.state.locations:
            if state.location == clean:
                if status == "public":
                    state.status = "public"
                if next_prompt and not state.next_prompt:
                    state.next_prompt = next_prompt
                return state
        state = LocationReturnState(location=clean, status=status, source=source, next_prompt=next_prompt)
        self.state.locations.append(state)
        return state

    def _update_pressure_from_summary(
        self,
        summary: StorySessionSummary,
        text_blob: str,
        *,
        first_seen: bool,
    ) -> None:
        for track in self.state.villain_pressure:
            if self._text_mentions(text_blob, track.villain) or self._text_mentions(text_blob, track.goal):
                track.stage = "active"
                track.last_action = summary.short_memory or summary.title
                if first_seen and any(self._text_mentions(item, track.villain) for item in summary.unresolved_threads):
                    track.current = min(track.segments, track.current + 1)
            if track.current >= track.segments:
                track.stage = "fulfilled"
            elif track.current >= track.segments - 1:
                track.stage = "danger"

    def _refresh_phase(self) -> None:
        count = self.state.session_count
        target = max(1, int(self.state.pacing_profile.target_sessions or 35))
        ratio = count / target
        if ratio < 0.18:
            self.state.phase = StoryArcPhase.OPENING
        elif ratio < 0.45:
            self.state.phase = StoryArcPhase.RISING
        elif ratio < 0.68:
            self.state.phase = StoryArcPhase.MIDPOINT
        elif ratio < 0.86:
            self.state.phase = StoryArcPhase.CRISIS
        else:
            self.state.phase = StoryArcPhase.FINALE

    def _refresh_agenda(self) -> None:
        active_threads = self._active_threads()
        pressure = self._active_pressure()
        pending_reveals = self._pending_reveals()
        public_locations = [loc for loc in self.state.locations if loc.status == "public"]
        phase = self.state.phase

        focus: list[str] = []
        if active_threads:
            focus.append(f"推进线索：{active_threads[0].title}")
        if pressure:
            focus.append(f"让压力可见：{pressure[0].villain} 正在追求 {pressure[0].goal}")
        if pending_reveals:
            focus.append(f"铺垫真相：{pending_reveals[0].title}")
        if public_locations:
            focus.append(f"回访地点：{public_locations[0].location}")

        questions = self._phase_questions(phase, active_threads, pending_reveals, public_locations)
        pressure_moves = [
            f"{track.villain} 推进“{self._short_title(track.goal)}”，并留下玩家能察觉的后果。"
            for track in pressure[:3]
        ]
        scene_closure = self._scene_closure_directives(phase, active_threads, pressure, pending_reveals, public_locations)
        campaign_pacing = self._campaign_pacing_directives(phase, active_threads, pressure, pending_reveals)
        director_moves = self._director_moves(phase, active_threads, pressure, pending_reveals, public_locations)
        suggested_scene_type = "standard"
        if pressure and pressure[0].current >= max(1, pressure[0].segments - 1):
            suggested_scene_type = "conflict"
        elif phase in {StoryArcPhase.MIDPOINT, StoryArcPhase.CRISIS} and pending_reveals:
            suggested_scene_type = "gm_scene"
        elif phase == StoryArcPhase.OPENING and not active_threads:
            suggested_scene_type = "interlude"

        self.state.agenda = NextSessionAgenda(
            opening_image=self._opening_image(public_locations, active_threads),
            recommended_focus=focus[:5],
            questions=questions[:5],
            suggested_scene_type=suggested_scene_type,
            pressure_moves=pressure_moves,
            scene_closure=scene_closure,
            campaign_pacing=campaign_pacing,
            director_moves=director_moves,
            warnings=[
                "先用问题和场景后果引导玩家选择，不要把长期节奏当作预设剧情。",
                "未公开真相只能化成线索、梦境、NPC 失态或主持人短镜头，不能直接剧透。",
            ],
        )

    def _scene_closure_directives(
        self,
        phase: StoryArcPhase,
        threads: list[StoryThread],
        pressure: list[VillainPressureTrack],
        reveals: list[RevealCandidate],
        locations: list[LocationReturnState],
    ) -> list[str]:
        directives: list[str] = []
        if pressure and pressure[0].current >= max(1, pressure[0].segments - 1):
            directives.append("当前压力接近爆发：用冲突、倒计时命刻或明确代价收束场景，不要继续闲聊拖延。")
        if reveals and reveals[0].status == "ready" and phase in {StoryArcPhase.MIDPOINT, StoryArcPhase.CRISIS, StoryArcPhase.FINALE}:
            directives.append(f"真相“{reveals[0].title}”已适合揭示：让玩家主动拼出答案，或用短主持人镜头收尾。")
        if threads and threads[0].progress >= 3:
            directives.append(f"线索“{threads[0].title}”已推进多次：给出阶段性答案、选择或后果，然后结束当前镜头。")
        if locations:
            directives.append(f"若当前场景停滞，让“{locations[0].location}”出现一个变化，逼近一次选择。")
        if not directives:
            directives.append("当局面已解决、目标已明确或镜头要换地点时，主动用一两句总结收束场景。")
        return directives[:4]

    def _campaign_pacing_directives(
        self,
        phase: StoryArcPhase,
        threads: list[StoryThread],
        pressure: list[VillainPressureTrack],
        reveals: list[RevealCandidate],
    ) -> list[str]:
        if phase == StoryArcPhase.OPENING:
            return [
                "优先让英雄动机、小队关系和世界问题同屏出现。",
                "每场至少留下一个可追踪线索或可选择地点。",
            ]
        if phase == StoryArcPhase.RISING:
            return [
                "让反派计划开始产生公开后果，并让玩家能选择追哪条线。",
                "用回访地点或 NPC 求助展示英雄行动造成的改变。",
            ]
        if phase == StoryArcPhase.MIDPOINT:
            return [
                "准备一次足以颠覆力量平衡的揭示，但必须由公开线索支撑。",
                "揭示之后立刻给玩家一个新的行动方向或代价选择。",
            ]
        if phase == StoryArcPhase.CRISIS:
            return [
                "把反派胜利条件变成清晰可见的倒计时或命刻。",
                "推动英雄主题进入高代价选择，而不是单纯增加战斗数量。",
            ]
        return [
            "收束主要支线，把最终战场绑定世界威胁、英雄羁绊和反派镜像。",
            "每个终局场景都应改变世界状态，直到胜利、牺牲或尾声落定。",
        ]

    def _director_moves(
        self,
        phase: StoryArcPhase,
        threads: list[StoryThread],
        pressure: list[VillainPressureTrack],
        reveals: list[RevealCandidate],
        locations: list[LocationReturnState],
    ) -> list[str]:
        moves: list[str] = []
        if pressure:
            track = pressure[0]
            moves.append(f"让 {track.villain} 的目标“{self._short_title(track.goal)}”在场景背景中留下可见痕迹。")
        if reveals:
            reveal = reveals[0]
            if reveal.status == "ready":
                moves.append(f"安排一个能让玩家确认“{reveal.title}”的证据，而不是直接公告答案。")
            else:
                moves.append(f"为“{reveal.title}”再放一枚公开线索。")
        if threads:
            moves.append(f"把“{threads[0].title}”转化成玩家现在可以做的一个目标。")
        if locations:
            moves.append(f"让“{locations[0].location}”因为英雄曾经的行动出现新状态。")
        if not moves:
            moves.append("用一个可互动细节开场，再根据玩家选择决定是否进入检定、命刻或冲突。")
        return moves[:4]

    def _phase_questions(
        self,
        phase: StoryArcPhase,
        threads: list[StoryThread],
        reveals: list[RevealCandidate],
        locations: list[LocationReturnState],
    ) -> list[str]:
        questions: list[str] = []
        if phase == StoryArcPhase.OPENING:
            questions.append("玩家最想保护或改变的东西是什么？把它放进下一个场景。")
            questions.append("哪个地点能用一两个画面展示这个世界的希望与问题？")
        elif phase == StoryArcPhase.RISING:
            questions.append("这次选择会让哪条反派压力线推进，或让玩家看见什么代价？")
            questions.append("哪个 NPC 可以用妥协、误解或求助来复杂化当前目标？")
        elif phase == StoryArcPhase.MIDPOINT:
            questions.append("哪一个真相已经有足够线索，可以揭示一部分来改变力量平衡？")
            questions.append("哪个旧地点现在应该以新的面貌回访？")
        elif phase == StoryArcPhase.CRISIS:
            questions.append("反派的胜利条件是什么？玩家还有哪条高代价路线可以阻止它？")
            questions.append("哪个英雄主题适合被推到必须选择的时刻？")
        else:
            questions.append("终局战场如何同时承载世界威胁、英雄羁绊和反派镜像？")
            questions.append("哪些支线必须收束，哪些可以作为尾声余韵留下？")
        if threads:
            questions.append(f"围绕“{threads[0].title}”，现在应该问玩家一个什么选择题？")
        if reveals:
            questions.append(f"“{reveals[0].title}”还能通过哪条公开线索被玩家主动拼出来？")
        if locations:
            questions.append(f"回到“{locations[0].location}”时，那里因为英雄行动发生了什么变化？")
        return questions

    def _opening_image(self, locations: list[LocationReturnState], threads: list[StoryThread]) -> str:
        if locations and threads:
            return f"镜头从{locations[0].location}切入，让“{threads[0].title}”以一个可互动细节出现。"
        if locations:
            return f"镜头从{locations[0].location}的一个变化开始。"
        if threads:
            return f"镜头从“{threads[0].title}”留下的新线索开始。"
        return "用一两个画面建立当前场景的时间、地点、在场角色和可互动元素。"

    def _active_threads(self) -> list[StoryThread]:
        return sorted(
            [thread for thread in self.state.threads if thread.status not in {"resolved", "retired"}],
            key=lambda item: (item.priority, item.progress, item.title),
            reverse=True,
        )

    def _active_pressure(self) -> list[VillainPressureTrack]:
        return sorted(
            [track for track in self.state.villain_pressure if track.stage != "fulfilled"],
            key=lambda item: (item.current, item.segments, item.villain),
            reverse=True,
        )

    def _pending_reveals(self) -> list[RevealCandidate]:
        return sorted(
            [reveal for reveal in self.state.reveals if reveal.status != "revealed"],
            key=lambda item: (item.status == "ready", len(item.public_clues), item.title),
            reverse=True,
        )

    def _thread_by_id(self, thread_id: str) -> StoryThread | None:
        return next((thread for thread in self.state.threads if thread.thread_id == thread_id), None)

    def _pressure_by_id(self, track_id: str) -> VillainPressureTrack | None:
        return next((track for track in self.state.villain_pressure if track.track_id == track_id), None)

    def _reveal_by_id(self, reveal_id: str) -> RevealCandidate | None:
        return next((reveal for reveal in self.state.reveals if reveal.reveal_id == reveal_id), None)

    def _known_entities_in_text(self, text: str) -> list[str]:
        world = self.world_state.world_profile
        candidates = [
            *world.major_locations.keys(),
            *world.kingdoms.keys(),
            *world.factions.keys(),
            *[draft.hero_name for draft in world.hero_drafts.values() if draft.hero_name],
        ]
        return [candidate for candidate in candidates if candidate and self._text_mentions(text, candidate)]

    def _summary_text(self, summary: StorySessionSummary) -> str:
        return "\n".join(
            [
                summary.title,
                summary.public_summary,
                summary.short_memory,
                "\n".join(summary.timeline),
                "\n".join(summary.important_npcs),
                "\n".join(summary.locations),
                "\n".join(summary.unresolved_threads),
                "\n".join(summary.private_notes),
                "\n".join(summary.entities),
            ]
        )

    def _looks_resolved(self, text: str, title: str) -> bool:
        if not self._text_mentions(text, title):
            return False
        return any(keyword in text for keyword in ["解决", "平息", "击败", "公开", "真相大白", "完成", "收束"])

    def _stable_id(self, prefix: str, value: str) -> str:
        normalized = self._canonical_key(value) or prefix
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10]
        return f"{prefix}_{digest}"

    def _short_title(self, text: str, *, limit: int = 18) -> str:
        clean = self._clean_text(text)
        if len(clean) <= limit:
            return clean
        return clean[:limit].rstrip("，。；、 ") + "..."

    def _clean_text(self, text: str) -> str:
        return " ".join(str(text or "").split()).strip()

    def _normalize_story_item(self, raw: object, *, fallback_title: str = "") -> dict[str, str]:
        if isinstance(raw, dict):
            data = raw
        else:
            text = self._clean_text(str(raw or ""))
            data = self._parse_json_text(text) or {"description": text}
        raw_title = data.get("name") or data.get("title") or data.get("villain") or data.get("subject")
        title = self._clean_text(raw_title or fallback_title)
        summary = self._clean_text(
            data.get("description")
            or data.get("summary")
            or data.get("goal")
            or data.get("secret")
            or data.get("content")
            or str(raw or "")
        )
        if not title or not raw_title:
            title = self._short_title(summary) or fallback_title
        key = self._canonical_key({"title": title, "summary": summary})
        return {"title": title, "summary": summary, "key": key}

    def _parse_json_text(self, text: str) -> dict[str, Any] | None:
        clean = self._clean_text(text)
        if not (clean.startswith("{") and clean.endswith("}")):
            return None
        try:
            parsed = json.loads(clean)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _canonical_key(self, value: object) -> str:
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        text = self._clean_text(str(value or ""))
        parsed = self._parse_json_text(text)
        if parsed is not None:
            return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return text

    def _dedupe_state(self) -> None:
        self.state.threads = self._dedupe_threads(self.state.threads)
        self.state.villain_pressure = self._dedupe_pressure(self.state.villain_pressure)
        self.state.reveals = self._dedupe_reveals(self.state.reveals)

    def _dedupe_threads(self, threads: list[StoryThread]) -> list[StoryThread]:
        merged: dict[tuple[str, str], StoryThread] = {}
        for thread in threads:
            raw = thread.title if self._parse_json_text(thread.title) else thread.summary if self._parse_json_text(thread.summary) else {
                "title": thread.title,
                "summary": thread.summary,
            }
            item = self._normalize_story_item(raw, fallback_title=thread.title)
            if item["title"] and item["title"] != thread.title:
                thread.title = item["title"]
            if item["summary"] and item["summary"] != thread.summary:
                thread.summary = item["summary"]
            key = (thread.thread_type, item["key"])
            existing = merged.get(key)
            if existing is None:
                merged[key] = thread
                continue
            existing.priority = max(existing.priority, thread.priority)
            existing.progress = max(existing.progress, thread.progress)
            if existing.status == "seeded" and thread.status != "seeded":
                existing.status = thread.status
            for attr in ("entities", "related_tags", "public_clues", "private_notes"):
                for item in getattr(thread, attr):
                    self._append_unique(getattr(existing, attr), item)
        return list(merged.values())

    def _dedupe_pressure(self, tracks: list[VillainPressureTrack]) -> list[VillainPressureTrack]:
        merged: dict[tuple[str, str], VillainPressureTrack] = {}
        for track in tracks:
            raw = track.villain if self._parse_json_text(track.villain) else track.goal if self._parse_json_text(track.goal) else {
                "title": track.villain,
                "summary": track.goal,
            }
            item = self._normalize_story_item(raw, fallback_title=track.villain)
            if item["title"] and item["title"] != track.villain:
                track.villain = item["title"]
            if item["summary"] and item["summary"] != track.goal:
                track.goal = item["summary"]
            key = (self._canonical_key(track.villain), self._canonical_key(track.goal))
            existing = merged.get(key)
            if existing is None:
                merged[key] = track
                continue
            existing.current = max(existing.current, track.current)
            existing.segments = max(existing.segments, track.segments)
            if track.last_action and not existing.last_action:
                existing.last_action = track.last_action
            for thread_id in track.related_threads:
                self._append_unique(existing.related_threads, thread_id)
        return list(merged.values())

    def _dedupe_reveals(self, reveals: list[RevealCandidate]) -> list[RevealCandidate]:
        merged: dict[tuple[str, str], RevealCandidate] = {}
        for reveal in reveals:
            raw = reveal.title if self._parse_json_text(reveal.title) else reveal.secret if self._parse_json_text(reveal.secret) else {
                "title": reveal.title,
                "summary": reveal.secret,
            }
            item = self._normalize_story_item(raw, fallback_title=reveal.title)
            if item["title"] and item["title"] != reveal.title:
                reveal.title = item["title"]
            if item["summary"] and item["summary"] != reveal.secret:
                reveal.secret = item["summary"]
            key = (self._canonical_key(reveal.title), self._canonical_key(reveal.secret))
            existing = merged.get(key)
            if existing is None:
                merged[key] = reveal
                continue
            for attr in ("related_entities", "public_clues"):
                for item in getattr(reveal, attr):
                    self._append_unique(getattr(existing, attr), item)
            if existing.status == "seeded" and reveal.status != "seeded":
                existing.status = reveal.status
        return list(merged.values())

    def _text_mentions(self, text: str, needle: str) -> bool:
        needle = self._clean_text(needle)
        if not needle:
            return False
        if needle in text:
            return True
        candidates = [
            self._short_title(needle, limit=10).replace("...", ""),
            needle.split("？", 1)[0],
            needle.split("?", 1)[0],
            needle[:6],
            needle[:4],
        ]
        return any(candidate and len(candidate) >= 3 and candidate in text for candidate in candidates)

    def _append_unique(self, items: list[str], value: str) -> None:
        value = self._clean_text(value)
        if value and value not in items:
            items.append(value)

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
