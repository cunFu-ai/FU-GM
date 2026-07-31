from __future__ import annotations

import json
import hashlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CampaignRunCheckpoint:
    """Atomic runner-owned checkpoint at a complete session or table-event boundary."""

    FILENAME = "campaign_checkpoint.json"

    target_sessions: int
    campaign_id: str
    completed_session: int = 0
    completed: bool = False
    state: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    @staticmethod
    def directory_digest(directory: Path) -> str:
        """Return a stable digest for a checkpoint campaign directory."""

        root = Path(directory).resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"长测检查点目录不存在：{root}")
        digest = hashlib.sha256()
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            relative = path.relative_to(root).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
        return digest.hexdigest()

    def resolve_campaign_backup(self, run_root: Path) -> Path:
        """Resolve only immutable runner-owned bundles under ``.checkpoints``."""

        root = Path(run_root).resolve()
        raw = str(self.state.get("campaign_backup") or "").strip()
        if not raw:
            raise ValueError("长测检查点缺少 campaign_backup。")
        relative = Path(raw)
        if relative.is_absolute():
            raise ValueError("长测检查点的 campaign_backup 必须是相对路径。")
        backup = (root / relative).resolve()
        checkpoint_root = (root / ".checkpoints").resolve()
        try:
            backup.relative_to(checkpoint_root)
        except ValueError as exc:
            raise ValueError("长测检查点只能从 .checkpoints 恢复，不能使用可写工作副本。") from exc
        if not backup.is_dir():
            raise FileNotFoundError(f"长测检查点的战役备份不存在：{backup}")
        return backup

    def restore_campaign_copy(self, run_root: Path, destination: Path) -> Path:
        """Copy the immutable bundle to a fresh mutable campaign directory."""

        source = self.resolve_campaign_backup(run_root)
        destination = Path(destination)
        if destination.exists():
            raise FileExistsError(f"长测续跑工作副本已存在：{destination}")
        source_digest = self.directory_digest(source)
        expected_digest = str(self.state.get("campaign_backup_sha256") or "").strip()
        if expected_digest and source_digest != expected_digest:
            raise ValueError("长测检查点摘要不一致，源快照可能被修改。")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
        if self.directory_digest(destination) != source_digest:
            shutil.rmtree(destination, ignore_errors=True)
            raise OSError("长测检查点复制后摘要不一致。")
        if self.directory_digest(source) != source_digest:
            shutil.rmtree(destination, ignore_errors=True)
            raise OSError("长测检查点在恢复过程中发生变化。")
        return destination

    @classmethod
    def load_resume_source(
        cls,
        source: Path,
    ) -> tuple[Path, Path, "CampaignRunCheckpoint"]:
        """Resolve a run root or immutable bundle into one resume checkpoint.

        A long run's mutable top-level manifest always points at the latest
        snapshot.  Every immutable bundle also carries its own copy so a
        specific table-event boundary can be selected without reconstructing
        runner cursors by hand.
        """

        requested = Path(source).expanduser().resolve()
        checkpoint_path = requested if requested.is_file() else requested / cls.FILENAME
        if not checkpoint_path.is_file():
            raise FileNotFoundError(f"长测检查点不存在：{checkpoint_path}")

        checkpoint_directory = checkpoint_path.parent
        if checkpoint_directory.parent.name == ".checkpoints":
            run_root = checkpoint_directory.parent.parent.resolve()
        else:
            run_root = checkpoint_directory.resolve()

        checkpoint = cls.load(checkpoint_path)
        checkpoint.resolve_campaign_backup(run_root)
        return run_root, checkpoint_path, checkpoint

    @classmethod
    def load(cls, path: Path) -> "CampaignRunCheckpoint":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            target_sessions=int(payload["target_sessions"]),
            campaign_id=str(payload["campaign_id"]),
            completed_session=int(payload.get("completed_session") or 0),
            completed=bool(payload.get("completed")),
            state=dict(payload.get("state") or {}),
            schema_version=int(payload.get("schema_version") or 1),
        )

    def save(self, path: Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        payload = {
            "schema_version": self.schema_version,
            "target_sessions": self.target_sessions,
            "campaign_id": self.campaign_id,
            "completed_session": self.completed_session,
            "completed": self.completed,
            "state": self.state,
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        temporary.replace(destination)
