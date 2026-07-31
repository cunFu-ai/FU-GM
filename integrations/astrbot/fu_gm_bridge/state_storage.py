from __future__ import annotations

import json
import os
from pathlib import Path
import uuid
from typing import Any, Mapping


def write_json_atomic(path: Path, value: Any) -> None:
    """Persist bridge state without exposing a partial JSON file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def write_json_map_atomic(path: Path, values: Mapping[str, str]) -> None:
    """Persist a string binding map atomically."""

    write_json_atomic(
        path,
        {str(key): str(value) for key, value in values.items()},
    )
