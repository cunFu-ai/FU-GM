from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from fu_gm.components.world_state import WorldState
from fu_gm.components.topic_memory_store import TopicMemoryStore
from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.llm_utils import extract_json_object
from fu_gm.models import MemoryVisibility, SessionTranscriptEntry, StorySessionSummary
from fu_gm.prompt_cache import build_cache_friendly_messages


class StorySummarizer(Protocol):
    """把一场跑团的完整记录整理成可长期召回的故事记忆。"""

    def summarize(
        self,
        entries: list[SessionTranscriptEntry],
        *,
        campaign_id: str,
        session_id: str,
        title: str = "",
        world_state: WorldState | None = None,
    ) -> StorySessionSummary:
        ...


class HeuristicStorySummarizer:
    """离线兜底整理器。

    它不试图写漂亮故事，只保证在没有 LLM 或测试环境中也能生成稳定的长期记忆。
    """

    def summarize(
        self,
        entries: list[SessionTranscriptEntry],
        *,
        campaign_id: str,
        session_id: str,
        title: str = "",
        world_state: WorldState | None = None,
    ) -> StorySessionSummary:
        public_entries = [entry for entry in entries if not self._is_private_role(entry.role)]
        title = title or f"跑团记录 {session_id}"
        timeline = [f"{entry.speaker}：{entry.content}" for entry in public_entries[-8:]]
        public_summary = "；".join(timeline) if timeline else "本场跑团没有公开对话记录。"
        short_memory = f"{title}：{public_summary[:220]}"
        entities = self._extract_entities(public_entries, world_state)
        spotlight_characters = self._speaker_names(public_entries)
        important_npcs = [entity for entity in entities if entity not in spotlight_characters][:8]
        locations = self._lines_with_keywords(public_entries, ("地下城", "遗迹", "村", "城", "港", "森林", "房", "区域"), limit=6)
        rewards = self._lines_with_keywords(public_entries, ("奖励", "宝箱", "获得", "金币", "银爪", "装备", "物资"), limit=6)
        unresolved_threads = self._lines_with_keywords(public_entries, ("？", "?", "线索", "悬念", "下次", "目标", "未解决"), limit=6)
        private_notes = [
            f"{entry.speaker}：{entry.content}" for entry in entries if self._is_private_role(entry.role)
        ]
        return StorySessionSummary(
            campaign_id=campaign_id,
            session_id=session_id,
            title=title,
            created_at=self._now(),
            public_summary=public_summary,
            short_memory=short_memory,
            timeline=timeline,
            spotlight_characters=spotlight_characters,
            important_npcs=important_npcs,
            locations=locations,
            rewards=rewards,
            unresolved_threads=unresolved_threads,
            entities=entities,
            tags=["story", "session_summary", "heuristic"],
            private_notes=private_notes,
        )

    def _extract_entities(self, entries: list[SessionTranscriptEntry], world_state: WorldState | None) -> list[str]:
        text = "\n".join(entry.content for entry in entries)
        if world_state is None:
            return []
        return world_state.extract_entities(text)

    def _speaker_names(self, entries: list[SessionTranscriptEntry]) -> list[str]:
        names: list[str] = []
        for entry in entries:
            if entry.speaker and entry.speaker not in names and entry.speaker not in {"GM", "系统", "旁白"}:
                names.append(entry.speaker)
        return names[:8]

    def _lines_with_keywords(
        self,
        entries: list[SessionTranscriptEntry],
        keywords: tuple[str, ...],
        *,
        limit: int,
    ) -> list[str]:
        lines: list[str] = []
        for entry in entries:
            text = entry.content.strip()
            if not text:
                continue
            if any(keyword in text for keyword in keywords):
                line = f"{entry.speaker}：{text[:120]}"
                if line not in lines:
                    lines.append(line)
            if len(lines) >= limit:
                break
        return lines

    def _is_private_role(self, role: str) -> bool:
        return role in {"gm_private", "system_private", "private"}

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()


class LLMStorySummarizer:
    """调用真实 LLM 整理跑团日志，失败时回退到启发式整理器。"""

    def __init__(
        self,
        client: OpenAICompatibleClient,
        model: str,
        *,
        fallback: StorySummarizer | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.fallback = fallback or HeuristicStorySummarizer()

    def summarize(
        self,
        entries: list[SessionTranscriptEntry],
        *,
        campaign_id: str,
        session_id: str,
        title: str = "",
        world_state: WorldState | None = None,
    ) -> StorySessionSummary:
        try:
            content = self.client.create_chat_completion(
                model=self.model,
                messages=build_cache_friendly_messages(
                    static_system_prompt=self._system_prompt(),
                    user_content=self._user_prompt(entries, title=title, world_state=world_state),
                ),
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            data = extract_json_object(content)
            return self._summary_from_payload(
                data,
                campaign_id=campaign_id,
                session_id=session_id,
                title=title,
                entries=entries,
                world_state=world_state,
            )
        except Exception:
            return self.fallback.summarize(
                entries,
                campaign_id=campaign_id,
                session_id=session_id,
                title=title,
                world_state=world_state,
            )

    def _summary_from_payload(
        self,
        data: dict[str, Any],
        *,
        campaign_id: str,
        session_id: str,
        title: str,
        entries: list[SessionTranscriptEntry],
        world_state: WorldState | None,
    ) -> StorySessionSummary:
        public_text = "\n".join(
            entry.content for entry in entries if entry.role not in {"gm_private", "private", "system_private"}
        )
        fallback = self.fallback.summarize(
            entries,
            campaign_id=campaign_id,
            session_id=session_id,
            title=title,
            world_state=world_state,
        )
        entities = self._string_list(data.get("entities"))
        if world_state is not None:
            for entity in world_state.extract_entities(public_text):
                if entity not in entities:
                    entities.append(entity)
        for entity in fallback.entities:
            if entity not in entities:
                entities.append(entity)
        public_summary = str(data.get("public_summary") or "").strip() or fallback.public_summary
        short_memory = str(data.get("short_memory") or "").strip() or public_summary[:220] or fallback.short_memory
        return StorySessionSummary(
            campaign_id=campaign_id,
            session_id=session_id,
            title=str(data.get("title") or title or f"跑团记录 {session_id}"),
            created_at=datetime.now(timezone.utc).isoformat(),
            public_summary=public_summary,
            short_memory=short_memory,
            timeline=self._string_list(data.get("timeline")) or fallback.timeline,
            spotlight_characters=self._string_list(data.get("spotlight_characters")) or fallback.spotlight_characters,
            important_npcs=self._string_list(data.get("important_npcs")) or fallback.important_npcs,
            locations=self._string_list(data.get("locations")) or fallback.locations,
            rewards=self._string_list(data.get("rewards")) or fallback.rewards,
            unresolved_threads=self._string_list(data.get("unresolved_threads")) or fallback.unresolved_threads,
            private_notes=self._string_list(data.get("private_notes")) or fallback.private_notes,
            entities=entities,
            tags=["story", "session_summary", "llm", *self._string_list(data.get("tags"))],
        )

    def _system_prompt(self) -> str:
        return (
            "你是《最终物语》AI GM 的后台跑团日志整理器。你的任务不是继续扮演，而是把一场跑团整理成可长期检索的故事记忆。\n"
            "规则：\n"
            "1. 输出严格 JSON 对象，不要 Markdown。\n"
            "2. public_summary 与 short_memory 只能包含玩家已经看见或公开发生的内容，供水群闲聊时召回。\n"
            "3. role 为 gm_private/private/system_private 的内容不得写入 public_summary、short_memory、timeline、entities、locations 或 unresolved_threads。\n"
            "4. 如果私密内容对 GM 有用，只能写入 private_notes。\n"
            "5. 摘要要像战役日志：记录谁做了什么、地点如何改变、获得了什么、留下什么悬念。\n"
            "6. 如果公开记录中出现了人物、地点、奖励、目标或未解线索，尽量写入对应数组，不要只给空数组。"
        )

    def _user_prompt(
        self,
        entries: list[SessionTranscriptEntry],
        *,
        title: str,
        world_state: WorldState | None,
    ) -> str:
        known_public = world_state.retrieve_relevant_memory("", include_private=False, limit=12) if world_state else []
        compacted_transcript = self._compact_transcript(entries)
        return json.dumps(
            {
                "required_schema": {
                    "title": "本场标题",
                    "public_summary": "300-800字公开故事总结",
                    "short_memory": "50-120字，给水群闲聊快速召回",
                    "timeline": ["按发生顺序列出公开关键事件"],
                    "spotlight_characters": ["本场高光角色"],
                    "important_npcs": ["公开登场的重要NPC"],
                    "locations": ["公开涉及地点"],
                    "rewards": ["公开获得的奖励/资产/情报"],
                    "unresolved_threads": ["公开留下的悬念或下一步目标"],
                    "private_notes": ["仅GM可见的后台备注"],
                    "entities": ["可用于检索的公开实体名"],
                    "tags": ["story"],
                },
                "title_hint": title,
                "known_public_memory": known_public,
                "transcript_compaction": "若原始记录很长，此处保留开场少量消息与最近关键消息；请不要编造未给出的事件。",
                "transcript": compacted_transcript,
            },
            ensure_ascii=False,
        )

    def _compact_transcript(
        self,
        entries: list[SessionTranscriptEntry],
        *,
        head: int = 24,
        tail: int = 120,
        max_content_chars: int = 1200,
    ) -> list[dict[str, Any]]:
        if len(entries) <= head + tail:
            selected = entries
        else:
            selected = [*entries[:head], *entries[-tail:]]
        compacted: list[dict[str, Any]] = []
        skipped_inserted = False
        for index, entry in enumerate(selected):
            if len(entries) > head + tail and index == head and not skipped_inserted:
                compacted.append(
                    {
                        "role": "system_private",
                        "speaker": "日志裁剪器",
                        "content": f"中间 {len(entries) - head - tail} 条记录因长度被省略；请只基于可见记录总结。",
                    }
                )
                skipped_inserted = True
            data = asdict(entry)
            content = str(data.get("content") or "")
            if len(content) > max_content_chars:
                data["content"] = content[:max_content_chars] + "……[已截断]"
            compacted.append(data)
        return compacted

    def _string_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if str(item).strip()]


class SessionLogManager:
    """跑团日志归档与故事摘要入口。

    这层是 AstrBot/HTTP/CLI 共用的后台能力：消息入口只负责把群消息写进来，
    每场结束时调用 finalize_session 即可得到完整 transcript 与可召回故事记忆。
    """

    def __init__(
        self,
        root: str | Path = "data/campaigns",
        *,
        summarizer: StorySummarizer | None = None,
    ) -> None:
        self.root = Path(root)
        self.summarizer = summarizer or HeuristicStorySummarizer()
        self.topic_memory_store = TopicMemoryStore(self.root)

    def append_message(
        self,
        campaign_id: str,
        session_id: str,
        *,
        speaker: str,
        content: str,
        role: str = "user",
        channel_id: str = "",
        message_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> SessionTranscriptEntry:
        entry = SessionTranscriptEntry(
            campaign_id=campaign_id,
            session_id=session_id,
            created_at=self._now(),
            role=role,
            speaker=speaker,
            content=content,
            channel_id=channel_id,
            message_id=message_id,
            metadata=dict(metadata or {}),
        )
        path = self.transcript_path(campaign_id, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
        self._append_transcript_text_entry(entry)
        return entry

    def append_turn(
        self,
        campaign_id: str,
        session_id: str,
        *,
        speaker: str,
        message: str,
        gm_reply: str,
        channel_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.append_message(
            campaign_id,
            session_id,
            speaker=speaker,
            content=message,
            role="user",
            channel_id=channel_id,
            metadata=metadata,
        )
        self.append_message(
            campaign_id,
            session_id,
            speaker="AI GM",
            content=gm_reply,
            role="assistant",
            channel_id=channel_id,
            metadata=metadata,
        )

    def load_transcript(self, campaign_id: str, session_id: str) -> list[SessionTranscriptEntry]:
        path = self.transcript_path(campaign_id, session_id)
        if not path.exists():
            return []
        entries: list[SessionTranscriptEntry] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            entries.append(SessionTranscriptEntry(**data))
        return entries

    def live_context_lines(
        self,
        campaign_id: str,
        session_id: str,
        *,
        limit: int = 24,
        include_system: bool = False,
    ) -> list[str]:
        entries = self.load_transcript(campaign_id, session_id)
        visible = [
            entry
            for entry in entries
            if entry.role not in {"gm_private", "private", "system_private"}
            and (include_system or entry.role != "system")
            and str(entry.content or "").strip()
        ]
        lines: list[str] = []
        for entry in visible[-limit:]:
            content = " ".join(str(entry.content or "").split())
            if len(content) > 360:
                content = content[:360] + "..."
            lines.append(f"{entry.speaker}（{entry.role}）：{content}")
        return lines

    def format_live_context(
        self,
        campaign_id: str,
        session_id: str,
        *,
        limit: int = 24,
        include_system: bool = False,
    ) -> str:
        lines = self.live_context_lines(
            campaign_id,
            session_id,
            limit=limit,
            include_system=include_system,
        )
        if not lines:
            return ""
        return "当前场次实时公开记录（尚未收团也可作为公开上下文使用）：\n" + "\n".join(
            f"- {line}" for line in lines
        )

    def finalize_session(
        self,
        campaign_id: str,
        session_id: str,
        *,
        world_state: WorldState,
        title: str = "",
    ) -> StorySessionSummary:
        entries = self.load_transcript(campaign_id, session_id)
        transcript_txt_path = self.export_transcript_text(campaign_id, session_id, entries=entries)
        summary = self.summarizer.summarize(
            entries,
            campaign_id=campaign_id,
            session_id=session_id,
            title=title,
            world_state=world_state,
        )
        summary.transcript_path = str(self.transcript_path(campaign_id, session_id))
        summary.transcript_txt_path = str(transcript_txt_path)
        summary.summary_path = str(self.summary_path(campaign_id, session_id))
        summary.memory_path = str(self.memory_path(campaign_id, session_id))
        self._write_summary(summary)
        self._write_memory_markdown(summary)
        self._record_world_memory(world_state, summary)
        self._write_topic_memories(summary)
        return summary

    def recall_story_memories(self, campaign_id: str, query: str, *, limit: int = 5) -> list[str]:
        summaries = self.load_story_summaries(campaign_id)
        terms = [term.lower() for term in query.split() if term.strip()]
        scored: list[tuple[int, StorySessionSummary]] = []
        for summary in summaries:
            text = " ".join(
                [
                    summary.title,
                    summary.short_memory,
                    summary.public_summary,
                    " ".join(summary.timeline),
                    " ".join(summary.entities),
                ]
            ).lower()
            score = sum(1 for term in terms if term in text)
            if score > 0 or not terms:
                scored.append((score, summary))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [self._format_recall(summary) for _score, summary in scored[:limit]]

    def load_story_summaries(self, campaign_id: str) -> list[StorySessionSummary]:
        campaign_dir = self._campaign_dir(campaign_id) / "sessions"
        if not campaign_dir.exists():
            return []
        summaries: list[StorySessionSummary] = []
        for path in sorted(campaign_dir.glob("*/story_summary.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            summaries.append(StorySessionSummary(**data))
        return summaries

    def transcript_path(self, campaign_id: str, session_id: str) -> Path:
        return self._session_dir(campaign_id, session_id) / "transcript.jsonl"

    def transcript_txt_path(self, campaign_id: str, session_id: str) -> Path:
        return self._session_dir(campaign_id, session_id) / "transcript.txt"

    def summary_path(self, campaign_id: str, session_id: str) -> Path:
        return self._session_dir(campaign_id, session_id) / "story_summary.json"

    def memory_path(self, campaign_id: str, session_id: str) -> Path:
        return self._session_dir(campaign_id, session_id) / "story_memory.md"

    def export_transcript_text(
        self,
        campaign_id: str,
        session_id: str,
        *,
        entries: list[SessionTranscriptEntry] | None = None,
    ) -> Path:
        entries = list(entries) if entries is not None else self.load_transcript(campaign_id, session_id)
        path = self.transcript_txt_path(campaign_id, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._format_transcript_text(campaign_id, session_id, entries), encoding="utf-8")
        return path

    def _write_summary(self, summary: StorySessionSummary) -> None:
        path = self.summary_path(summary.campaign_id, summary.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(summary), ensure_ascii=False, indent=2), encoding="utf-8")

    def _append_transcript_text_entry(self, entry: SessionTranscriptEntry) -> None:
        path = self.transcript_txt_path(entry.campaign_id, entry.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(self._transcript_text_header(entry.campaign_id, entry.session_id), encoding="utf-8")
        with path.open("a", encoding="utf-8") as file:
            file.write(self._format_transcript_text_entry(entry))

    def _format_transcript_text(
        self,
        campaign_id: str,
        session_id: str,
        entries: list[SessionTranscriptEntry],
    ) -> str:
        parts = [self._transcript_text_header(campaign_id, session_id)]
        parts.extend(self._format_transcript_text_entry(entry) for entry in entries)
        return "".join(parts).rstrip() + "\n"

    def _transcript_text_header(self, campaign_id: str, session_id: str) -> str:
        return "\n".join(
            [
                "# 跑团完整记录",
                "",
                f"战役：{campaign_id}",
                f"场次：{session_id}",
                "",
                "---",
                "",
            ]
        )

    def _format_transcript_text_entry(self, entry: SessionTranscriptEntry) -> str:
        content = str(entry.content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not content:
            content = "(空消息)"
        header = f"[{entry.created_at}] {entry.role} · {entry.speaker}".strip()
        return f"{header}\n{content}\n\n"

    def _write_memory_markdown(self, summary: StorySessionSummary) -> None:
        lines = [
            f"# {summary.title}",
            "",
            f"- 战役：{summary.campaign_id}",
            f"- 场次：{summary.session_id}",
            f"- 整理时间：{summary.created_at}",
            "",
            "## 水群可召回短记忆",
            summary.short_memory,
            "",
            "## 公开故事总结",
            summary.public_summary,
        ]
        if summary.timeline:
            lines.extend(["", "## 时间线", *[f"- {item}" for item in summary.timeline]])
        if summary.unresolved_threads:
            lines.extend(["", "## 未解决线索", *[f"- {item}" for item in summary.unresolved_threads]])
        path = self.memory_path(summary.campaign_id, summary.session_id)
        path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")

    def _record_world_memory(self, world_state: WorldState, summary: StorySessionSummary) -> None:
        world_state.record_memory_event(
            summary.short_memory or summary.public_summary,
            kind="session_story_summary",
            visibility=MemoryVisibility.PUBLIC,
            entities=summary.entities,
            tags=summary.tags,
            source=summary.summary_path,
            payload={
                "campaign_id": summary.campaign_id,
                "session_id": summary.session_id,
                "title": summary.title,
                "timeline": summary.timeline,
                "unresolved_threads": summary.unresolved_threads,
                "summary_path": summary.summary_path,
                "memory_path": summary.memory_path,
            },
        )
        if summary.private_notes:
            world_state.record_memory_event(
                f"本场 GM 私密整理：{'; '.join(summary.private_notes)}",
                kind="session_private_notes",
                visibility=MemoryVisibility.PRIVATE,
                entities=summary.entities,
                tags=["story", "session_summary", "private"],
                source=summary.summary_path,
            )

    def _write_topic_memories(self, summary: StorySessionSummary) -> None:
        public_sections = [
            f"# {summary.title}",
            "",
            "## 短记忆",
            summary.short_memory or summary.public_summary,
            "",
            "## 公开故事总结",
            summary.public_summary,
        ]
        if summary.timeline:
            public_sections.extend(["", "## 时间线", *[f"- {item}" for item in summary.timeline]])
        if summary.spotlight_characters:
            public_sections.extend(["", "## 高光角色", *[f"- {item}" for item in summary.spotlight_characters]])
        if summary.important_npcs:
            public_sections.extend(["", "## 重要 NPC", *[f"- {item}" for item in summary.important_npcs]])
        if summary.locations:
            public_sections.extend(["", "## 地点", *[f"- {item}" for item in summary.locations]])
        if summary.rewards:
            public_sections.extend(["", "## 奖励与资产", *[f"- {item}" for item in summary.rewards]])
        if summary.unresolved_threads:
            public_sections.extend(["", "## 未解决线索", *[f"- {item}" for item in summary.unresolved_threads]])

        self.topic_memory_store.write_topic_memory(
            summary.campaign_id,
            visibility=MemoryVisibility.PUBLIC,
            memory_type="session_summary",
            title=summary.title,
            description=summary.short_memory or summary.public_summary[:160],
            body="\n".join(public_sections),
            entities=summary.entities,
            tags=[*summary.tags, "session", summary.session_id],
            filename=f"session_{summary.session_id}",
            last_event_at=summary.created_at,
            extra_frontmatter={
                "source_summary_path": summary.summary_path,
                "source_transcript_path": summary.transcript_path,
            },
        )

        if not summary.private_notes:
            return
        private_body = "\n".join(
            [
                f"# {summary.title} GM 私密备注",
                "",
                "这些内容只允许进入 GM 决策侧，不得进入水群、玩家摘要或表达层。",
                "",
                *[f"- {item}" for item in summary.private_notes],
            ]
        )
        self.topic_memory_store.write_topic_memory(
            summary.campaign_id,
            visibility=MemoryVisibility.PRIVATE,
            memory_type="session_private_notes",
            title=f"{summary.title} GM 私密备注",
            description=summary.private_notes[0][:160] if summary.private_notes else "本场后台暗线、NPC动机或未公开后果。",
            body=private_body,
            entities=summary.entities,
            tags=["story", "session_summary", "private", summary.session_id],
            filename=f"session_{summary.session_id}_private",
            last_event_at=summary.created_at,
            lock_level="draft",
            extra_frontmatter={
                "source_summary_path": summary.summary_path,
                "source_transcript_path": summary.transcript_path,
            },
        )

    def _format_recall(self, summary: StorySessionSummary) -> str:
        return f"{summary.title}（{summary.session_id}）：{summary.short_memory or summary.public_summary}"

    def _campaign_dir(self, campaign_id: str) -> Path:
        return self.root / self._safe_name(campaign_id)

    def _session_dir(self, campaign_id: str, session_id: str) -> Path:
        return self._campaign_dir(campaign_id) / "sessions" / self._safe_name(session_id)

    def _safe_name(self, value: str) -> str:
        return "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value.strip()) or "default"

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
