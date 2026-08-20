from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
    snapshot_version_at_write: int = 0
    verified_at: str = ""
    superseded_by: str = ""
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
        snapshot_version_at_write: int = 0,
        verified_at: str = "",
        superseded_by: str = "",
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
            "snapshot_version_at_write": max(
                0,
                int(snapshot_version_at_write or 0),
            ),
            "verified_at": verified_at,
        }
        if lock_level:
            frontmatter["lock_level"] = lock_level
        if superseded_by:
            frontmatter["superseded_by"] = superseded_by
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
        include_superseded: bool = False,
    ) -> list[TopicMemoryRecord]:
        already_surfaced = already_surfaced or set()
        records = self.scan_frontmatter(
            campaign_id,
            include_private=include_private,
            include_table=include_table,
            already_surfaced=already_surfaced,
            max_scan_files=max_scan_files,
            frontmatter_lines=frontmatter_lines,
            include_superseded=include_superseded,
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
        include_superseded: bool = False,
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
            if frontmatter.get("superseded_by") and not include_superseded:
                continue
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
            snapshot_version_at_write=self._safe_int(
                frontmatter.get("snapshot_version_at_write")
            ),
            verified_at=str(frontmatter.get("verified_at") or ""),
            superseded_by=str(frontmatter.get("superseded_by") or ""),
            mtime=path.stat().st_mtime,
        )

    def verify_memory(
        self,
        campaign_id: str,
        relative_path: str,
        *,
        snapshot_version: int = 0,
    ) -> bool:
        """标记一条派生记忆已与当前权威快照核对。"""

        path = self._resolve_memory_path(campaign_id, relative_path)
        if path is None:
            return False
        updates: dict[str, Any] = {
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
        if snapshot_version:
            updates["snapshot_version_at_write"] = max(
                0,
                int(snapshot_version),
            )
        self._update_frontmatter(path, updates)
        self.rebuild_index(campaign_id)
        return True

    def supersede_memory(
        self,
        campaign_id: str,
        relative_path: str,
        *,
        superseded_by: str,
    ) -> bool:
        """保留旧文件供审计，但从默认召回集合中移除。"""

        replacement = str(superseded_by or "").strip()
        path = self._resolve_memory_path(campaign_id, relative_path)
        if path is None or not replacement:
            return False
        self._update_frontmatter(
            path,
            {
                "superseded_by": replacement,
                "superseded_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        self.rebuild_index(campaign_id)
        return True

    def consolidate_if_due(
        self,
        campaign_id: str,
        *,
        completed_session_count: int = 0,
        force: bool = False,
        interval_sessions: int = 5,
        interval_hours: int = 24,
    ) -> dict[str, object]:
        """定期做保守的文件级去重，不让派生记忆无限累积。

        这里只合并正文完全相同的重复项，不尝试凭词法规则判断剧情真假。
        语义冲突仍留给后台模型或人工核验，避免整理器篡改公开事实。
        """

        memory_root = self._campaign_dir(campaign_id) / "memory"
        memory_root.mkdir(parents=True, exist_ok=True)
        maintenance_path = memory_root / ".maintenance.json"
        previous: dict[str, Any] = {}
        if maintenance_path.exists():
            try:
                previous = json.loads(maintenance_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                previous = {}
        now = datetime.now(timezone.utc)
        previous_count = self._safe_int(previous.get("completed_session_count"))
        previous_at = self._parse_datetime(previous.get("consolidated_at"))
        session_due = (
            completed_session_count > 0
            and completed_session_count - previous_count >= max(1, interval_sessions)
        )
        interval = timedelta(hours=max(1, interval_hours))
        time_due = bool(previous_at is not None and now - previous_at >= interval)
        if previous_at is None:
            cutoff = now.timestamp() - interval.total_seconds()
            time_due = any(
                path.name != "MEMORY.md" and path.stat().st_mtime <= cutoff
                for path in memory_root.rglob("*.md")
            )
        if previous_at is None and completed_session_count >= max(
            1,
            interval_sessions,
        ):
            session_due = True
        if not force and not session_due and not time_due:
            return {
                "ran": False,
                "reason": "not_due",
                "superseded": 0,
            }

        records = self.scan_frontmatter(
            campaign_id,
            include_private=True,
            include_table=True,
            include_superseded=False,
            max_scan_files=10_000,
        )
        by_fingerprint: dict[str, list[TopicMemoryRecord]] = {}
        by_identity: dict[
            tuple[str, str, str, tuple[str, ...]],
            list[tuple[TopicMemoryRecord, str]],
        ] = {}
        for record in records:
            body = self._read_body(record.path)
            normalized = re.sub(r"\s+", " ", body).strip()
            fingerprint = hashlib.sha256(
                "\n".join(
                    (
                        record.visibility.value,
                        record.memory_type,
                        record.title.strip(),
                        "|".join(sorted(record.entities)),
                        normalized,
                    )
                ).encode("utf-8")
            ).hexdigest()
            by_fingerprint.setdefault(fingerprint, []).append(record)
            identity = (
                record.visibility.value,
                record.memory_type,
                record.title.strip(),
                tuple(sorted(record.entities)),
            )
            by_identity.setdefault(identity, []).append((record, fingerprint))

        superseded = 0
        for duplicates in by_fingerprint.values():
            if len(duplicates) < 2:
                continue
            duplicates.sort(key=lambda item: item.mtime, reverse=True)
            replacement = duplicates[0].relative_path
            for stale in duplicates[1:]:
                self._update_frontmatter(
                    stale.path,
                    {
                        "superseded_by": replacement,
                        "superseded_at": now.isoformat(),
                    },
                )
                superseded += 1

        conflict_candidates: list[dict[str, object]] = []
        for identity, candidates in by_identity.items():
            distinct_fingerprints = {fingerprint for _, fingerprint in candidates}
            if len(candidates) < 2 or len(distinct_fingerprints) < 2:
                continue
            conflict_candidates.append(
                {
                    "visibility": identity[0],
                    "memory_type": identity[1],
                    "title": identity[2],
                    "entities": list(identity[3]),
                    "records": [
                        record.relative_path
                        for record, _ in sorted(
                            candidates,
                            key=lambda item: item[0].mtime,
                            reverse=True,
                        )
                    ],
                    "requires_semantic_review": True,
                }
            )

        maintenance = {
            "consolidated_at": now.isoformat(),
            "completed_session_count": max(0, int(completed_session_count)),
            "active_records_scanned": len(records),
            "superseded_exact_duplicates": superseded,
            "conflict_candidate_count": len(conflict_candidates),
            "conflict_candidates": conflict_candidates[:100],
        }
        maintenance_path.write_text(
            json.dumps(maintenance, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.rebuild_index(campaign_id)
        return {"ran": True, **maintenance, "superseded": superseded}

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

    def _resolve_memory_path(
        self,
        campaign_id: str,
        relative_path: str,
    ) -> Path | None:
        memory_root = (self._campaign_dir(campaign_id) / "memory").resolve()
        candidate = (memory_root / str(relative_path or "")).resolve()
        try:
            candidate.relative_to(memory_root)
        except ValueError:
            return None
        if not candidate.is_file() or candidate.suffix.lower() != ".md":
            return None
        return candidate

    def _update_frontmatter(self, path: Path, updates: dict[str, Any]) -> None:
        frontmatter = self._read_frontmatter(path, max_lines=200)
        body = self._read_body(path)
        frontmatter.update(updates)
        path.write_text(
            self._render_markdown(frontmatter, body),
            encoding="utf-8",
        )

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        clean = str(value or "").strip()
        if not clean:
            return None
        try:
            parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

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
