from __future__ import annotations

import os
import tempfile
from pathlib import Path


class FileSnapshotTransaction:
    """Restore a small, known set of derived files when a larger commit fails."""

    def __init__(self, paths: list[Path]) -> None:
        self._snapshots: dict[Path, bytes | None] = {}
        for raw_path in paths:
            path = Path(raw_path)
            if path in self._snapshots:
                continue
            self._snapshots[path] = path.read_bytes() if path.exists() else None
        self._active = True

    def commit(self) -> None:
        self._active = False

    def rollback(self) -> None:
        if not self._active:
            return
        try:
            for path, payload in self._snapshots.items():
                self._restore(path, payload)
        finally:
            self._active = False

    @staticmethod
    def _restore(path: Path, payload: bytes | None) -> None:
        if payload is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".rollback",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
