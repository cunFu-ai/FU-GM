from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fu_gm.campaign_paths import safe_campaign_path_segment
from fu_gm.models import MemoryVisibility, normalize_memory_visibility


@dataclass
class TopicMemoryRecord:
    """一个可被主动召回的 Markdown 记忆文件。

    该记录不是权威状态；它只作为低延迟上下文提示。真正的 HP、命刻、
    角色卡和地图状态仍以 snapshot.json 为准。
    """

    path: Path
    relative_path: str
    memory_type: str
    visibility: MemoryVisibility
    title: str
    description: str = ""
    entities: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    last_event_at: str = ""
    lock_level: str = ""
    mtime: float = 0.0
    content: str = ""
    freshness_note: str = ""
    score: int = 0

    def format_for_prompt(self) -> str:
        lines = [
            f"[{self.relative_path}]",
            f"type={self.memory_type}; visibility={self.visibility.value}; title={self.title}",
        ]
        if self.description:
            lines.append(f"description={self.description}")
        if self.entities:
            lines.append("entities=" + "、".join(self.entities))
        if self.tags:
            lines.append("tags=" + "、".join(self.tags))
        if self.freshness_note:
            lines.append(self.freshness_note)
        if self.content:
            lines.append(self.content.strip())
        return "\n".join(lines).strip()


class TopicMemoryStore:
    """文件级长期记忆。

    设计目标：
    - 读时只扫描 frontmatter，不读取全文。
    - 每轮只唤醒少量相关文件。
    - 记忆正文作为动态上下文附件使用，不污染静态 System Prompt。
    - Markdown 便于人工审查、修订和删除。
    """

    DEFAULT_MAX_SCAN_FILES = 200
    DEFAULT_FRONTMATTER_LINES = 30

    def __init__(self, root: str | Path = "data/campaigns") -> None:
        self.root = Path(root)

    def write_topic_memory(
        self,
        campaign_id: str,
        *,
        visibility: MemoryVisibility | str,
        memory_type: str,
        title: str,
        body: str,
        description: str = "",
        entities: list[str] | None = None,
        tags: list[str] | None = None,
        filename: str = "",
        last_event_at: str = "",
        lock_level: str = "",
        extra_frontmatter: dict[str, Any] | None = None,
    ) -> Path:
        visibility = normalize_memory_visibility(visibility)
        directory = self._memory_dir(campaign_id, visibility)
        directory.mkdir(parents=True, exist_ok=True)
        file_name = self._safe_filename(filename or f"{memory_type}_{title}") + ".md"
        path = directory / file_name
        frontmatter = {
            "type": memory_type,
            "visibility": visibility.value,
            "title": title,
            "description": description or self._first_text_line(body),
            "entities": list(entities or []),
            "tags": list(tags or []),
            "last_event_at": last_event_at,
        }
        if lock_level:
            frontmatter["lock_level"] = lock_level
        for key, value in (extra_frontmatter or {}).items():
            if value not in (None, "", []):
                frontmatter[key] = value
        path.write_text(self._render_markdown(frontmatter, body), encoding="utf-8")
        self.rebuild_index(campaign_id)
        return path

    def recall(
        self,
        campaign_id: str,
        query: str,
        *,
        include_private: bool = False,
        include_table: bool = True,
        already_surfaced: set[str] | None = None,
        max_selected: int = 5,
        max_scan_files: int = DEFAULT_MAX_SCAN_FILES,
        frontmatter_lines: int = DEFAULT_FRONTMATTER_LINES,
    ) -> list[TopicMemoryRecord]:
        already_surfaced = already_surfaced or set()
        records = self.scan_frontmatter(
            campaign_id,
            include_private=include_private,
            include_table=include_table,
            already_surfaced=already_surfaced,
            max_scan_files=max_scan_files,
            frontmatter_lines=frontmatter_lines,
        )
        terms = self._query_terms(query)
        scored: list[TopicMemoryRecord] = []
        for record in records:
            score = self._score_record(record, terms, query)
            if score <= 0 and terms:
                continue
            record.score = score
            scored.append(record)
        scored.sort(key=lambda record: (record.score, record.mtime), reverse=True)
        selected = scored[: max(0, max_selected)]
        for record in selected:
            record.content = self._read_body(record.path)
            record.freshness_note = self._freshness_note(record)
        return selected

    def scan_frontmatter(
        self,
        campaign_id: str,
        *,
        include_private: bool = False,
        include_table: bool = True,
        already_surfaced: set[str] | None = None,
        max_scan_files: int = DEFAULT_MAX_SCAN_FILES,
        frontmatter_lines: int = DEFAULT_FRONTMATTER_LINES,
    ) -> list[TopicMemoryRecord]:
        already_surfaced = already_surfaced or set()
        memory_root = self._campaign_dir(campaign_id) / "memory"
        if not memory_root.exists():
            return []
        paths = [
            path
            for path in memory_root.rglob("*.md")
            if path.name != "MEMORY.md" and str(path.relative_to(memory_root)) not in already_surfaced
        ]
        paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        records: list[TopicMemoryRecord] = []
        for path in paths[: max(0, max_scan_files)]:
            frontmatter = self._read_frontmatter(path, max_lines=frontmatter_lines)
            raw_visibility = str(frontmatter.get("visibility") or self._visibility_from_path(path))
            visibility = MemoryVisibility.PRIVATE if raw_visibility == MemoryVisibility.PRIVATE.value else MemoryVisibility.PUBLIC
            path_category = self._visibility_from_path(path)
            if visibility == MemoryVisibility.PRIVATE and not include_private:
                continue
            if path_category == "table" and not include_table:
                continue
            records.append(self._record_from_frontmatter(path, memory_root, frontmatter, visibility))
        return records

    def rebuild_index(self, campaign_id: str, *, max_lines: int = 200, max_bytes: int = 25_000) -> Path:
        memory_root = self._campaign_dir(campaign_id) / "memory"
        memory_root.mkdir(parents=True, exist_ok=True)
        records = self.scan_frontmatter(
            campaign_id,
            include_private=True,
            include_table=True,
            already_surfaced=set(),
            max_scan_files=10_000,
        )
        lines = [
            "# MEMORY",
            "",
            "这个索引只用于导航；具体记忆在 public/private/table 子目录中。",
            "",
        ]
        for record in records:
            line = f"- `{record.relative_path}` [{record.visibility.value}/{record.memory_type}] {record.title}"
            if record.description:
                line += f"：{record.description}"
            lines.append(line)
            if len(lines) >= max_lines:
                break
        text = "\n".join(lines).strip() + "\n"
        encoded = text.encode("utf-8")
        if len(encoded) > max_bytes:
            text = encoded[:max_bytes].decode("utf-8", errors="ignore")
            text = text.rsplit("\n", 1)[0].strip() + "\n"
        path = memory_root / "MEMORY.md"
        path.write_text(text, encoding="utf-8")
        return path

    def _record_from_frontmatter(
        self,
        path: Path,
        memory_root: Path,
        frontmatter: dict[str, Any],
        visibility: MemoryVisibility,
    ) -> TopicMemoryRecord:
        return TopicMemoryRecord(
            path=path,
            relative_path=str(path.relative_to(memory_root)),
            memory_type=str(frontmatter.get("type") or "note"),
            visibility=visibility,
            title=str(frontmatter.get("title") or path.stem),
            description=str(frontmatter.get("description") or ""),
            entities=self._string_list(frontmatter.get("entities")),
            tags=self._string_list(frontmatter.get("tags")),
            last_event_at=str(frontmatter.get("last_event_at") or ""),
            lock_level=str(frontmatter.get("lock_level") or ""),
            mtime=path.stat().st_mtime,
        )

    def _memory_dir(self, campaign_id: str, visibility: MemoryVisibility) -> Path:
        return self._campaign_dir(campaign_id) / "memory" / visibility.value

    def _campaign_dir(self, campaign_id: str) -> Path:
        return self.root / safe_campaign_path_segment(campaign_id)

    def _render_markdown(self, frontmatter: dict[str, Any], body: str) -> str:
        lines = ["---"]
        for key, value in frontmatter.items():
            if isinstance(value, list):
                rendered = json.dumps(value, ensure_ascii=False)
            else:
                rendered = json.dumps(value, ensure_ascii=False)
            lines.append(f"{key}: {rendered}")
        lines.extend(["---", "", body.strip(), ""])
        return "\n".join(lines)

    def _read_frontmatter(self, path: Path, *, max_lines: int) -> dict[str, Any]:
        lines = path.read_text(encoding="utf-8").splitlines()[:max_lines]
        if not lines or lines[0].strip() != "---":
            return {}
        values: dict[str, Any] = {}
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if ":" not in line:
                continue
            key, raw_value = line.split(":", 1)
            values[key.strip()] = self._parse_frontmatter_value(raw_value.strip())
        return values

    def _parse_frontmatter_value(self, value: str) -> Any:
        if not value:
            return ""
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
        if value.startswith("[") and value.endswith("]"):
            return [item.strip().strip('"').strip("'") for item in value[1:-1].split(",") if item.strip()]
        return value.strip('"').strip("'")

    def _read_body(self, path: Path) -> str:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            return text
        parts = text.split("---", 2)
        if len(parts) < 3:
            return text
        return parts[2].strip()

    def _visibility_from_path(self, path: Path) -> str:
        parts = set(path.parts)
        if "private" in parts:
            return MemoryVisibility.PRIVATE.value
        if "table" in parts:
            return "table"
        return MemoryVisibility.PUBLIC.value

    def _freshness_note(self, record: TopicMemoryRecord) -> str:
        age_days = max(0, int((datetime.now(timezone.utc).timestamp() - record.mtime) // 86_400))
        if record.visibility == MemoryVisibility.PRIVATE and record.lock_level in {"draft", "seeded"}:
            return (
                f"freshness note: 这是 GM 私密{record.lock_level or 'draft'}记忆；"
                "若与已公开事实冲突，应保留公开事实并调整暗线解释。"
            )
        if age_days >= 2:
            return (
                f"freshness note: 这条记忆约 {age_days} 天未更新，可能已被后续剧情改变；"
                "使用前优先参考当前快照、最近场次摘要和公开事实。"
            )
        return ""

    def _score_record(self, record: TopicMemoryRecord, terms: list[str], query: str) -> int:
        haystack = " ".join(
            [
                record.relative_path,
                record.memory_type,
                record.title,
                record.description,
                " ".join(record.entities),
                " ".join(record.tags),
            ]
        ).lower()
        score = 0
        for term in terms:
            if term and term in haystack:
                score += 2
        for entity in record.entities:
            if entity and entity in query:
                score += 4
        if record.title and record.title in query:
            score += 5
        return score

    def _query_terms(self, query: str) -> list[str]:
        normalized = query.lower()
        normalized = re.sub(r"[，。！？、；：,.;:!?()（）\[\]【】\n\t]", " ", normalized)
        terms = [term for term in normalized.split() if term]
        # 中文短查询常常没有空格；保留原句作为兜底片段，但避免整段太长。
        compact = re.sub(r"\s+", "", normalized)
        if compact and len(compact) <= 24 and compact not in terms:
            terms.append(compact)
        return terms

    def _string_list(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    def _safe_filename(self, value: str) -> str:
        cleaned = re.sub(r"[\\/:*?\"<>|#\s]+", "_", value.strip())
        cleaned = cleaned.strip("._")
        return cleaned or "memory"

    def _first_text_line(self, body: str) -> str:
        for line in body.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                return stripped[:160]
        return ""
