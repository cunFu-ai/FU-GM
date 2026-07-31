from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fu_gm.components.campaign_state_transaction import (
    CampaignStateSnapshot,
    CampaignStateTransaction,
)
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
)


class GMToolStateTransaction:
    """Rollback envelope for one ordinary mutating GM tool.

    Campaign replacement tools have their own filesystem semantics and are
    deliberately excluded. Every ordinary mutating domain tool shares this
    envelope, including pending Session 0 proposals, clocks, NPCs, scenes and
    rules turns.
    """

    _CAMPAIGN_FILES = ("snapshot.json", "events.jsonl")

    def __init__(
        self,
        host: Any,
        definition: GMToolDefinition,
        arguments: dict[str, object],
        context: GMToolExecutionContext,
    ) -> None:
        self.host = host
        self.definition = definition
        self.context = context
        self.active = definition.side_effect in {"write", "write_pending"}
        self.runtime: Any | None = None
        self.campaign_snapshot: CampaignStateSnapshot | None = None
        self.file_snapshot: dict[Path, bytes | None] = {}
        self.directory_snapshot: dict[
            Path,
            tuple[bool, dict[Path, bytes], set[Path]],
        ] = {}
        self.gate_file_snapshot: bytes | None = None
        self.gate_file_existed = False
        self.current_campaign_id = str(getattr(host, "current_campaign_id", "") or "")
        self.last_saved_path = ""
        self.service_snapshots: list[tuple[Any, object]] = []
        if not self.active:
            return

        self.runtime = host._runtime(context.campaign_id)
        app = self.runtime.app
        self.campaign_snapshot = CampaignStateTransaction.capture(
            app,
            context.campaign_id,
        )
        self.last_saved_path = str(getattr(self.runtime, "last_saved_path", "") or "")
        campaign_dir = app.memory_store._campaign_dir(context.campaign_id)
        for name in self._CAMPAIGN_FILES:
            path = campaign_dir / name
            self.file_snapshot[path] = path.read_bytes() if path.exists() else None
        self.directory_snapshot[campaign_dir / "memory"] = (
            self._capture_directory(campaign_dir / "memory")
        )
        if definition.name == "save_campaign":
            slot = str(arguments.get("slot") or "").strip()
            if slot:
                slot_path = app.memory_store._snapshot_path(
                    context.campaign_id,
                    slot=slot,
                )
                self.file_snapshot[slot_path] = (
                    slot_path.read_bytes()
                    if slot_path.exists()
                    else None
                )
        gate_path = Path(host.session_gates.path)
        self.gate_file_existed = gate_path.exists()
        self.gate_file_snapshot = gate_path.read_bytes() if gate_path.exists() else None
        map_tools = getattr(host, "gm_map_tools", None)
        if map_tools is None:
            suite = getattr(host, "gm_tool_suite", None)
            map_tools = getattr(suite, "maps", None)
        if (
            map_tools is not None
            and callable(getattr(map_tools, "capture_transaction_state", None))
            and callable(getattr(map_tools, "restore_transaction_state", None))
        ):
            self.service_snapshots.append(
                (
                    map_tools,
                    map_tools.capture_transaction_state(context.campaign_id),
                )
            )

    def commit(self) -> None:
        self.active = False

    def rollback(self) -> None:
        if not self.active:
            return
        if self.runtime is None or self.campaign_snapshot is None:
            self.active = False
            return
        CampaignStateTransaction.restore(self.runtime.app, self.campaign_snapshot)
        for service, snapshot in reversed(self.service_snapshots):
            service.restore_transaction_state(
                self.context.campaign_id,
                snapshot,
            )
        for path, payload in self.file_snapshot.items():
            self._restore_file(path, payload)
        for root, snapshot in self.directory_snapshot.items():
            self._restore_directory(root, snapshot)
        gate_path = Path(self.host.session_gates.path)
        self._restore_file(
            gate_path,
            self.gate_file_snapshot if self.gate_file_existed else None,
        )
        self.runtime.last_saved_path = self.last_saved_path
        self.host.current_campaign_id = self.current_campaign_id
        self.active = False

    @staticmethod
    def _restore_file(path: Path, payload: bytes | None) -> None:
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

    @staticmethod
    def _capture_directory(
        root: Path,
    ) -> tuple[bool, dict[Path, bytes], set[Path]]:
        existed = root.is_dir()
        files: dict[Path, bytes] = {}
        directories: set[Path] = set()
        if not existed:
            return False, files, directories
        resolved_root = root.resolve(strict=False)
        for entry in root.rglob("*"):
            resolved = entry.resolve(strict=False)
            if not resolved.is_relative_to(resolved_root):
                continue
            relative = resolved.relative_to(resolved_root)
            if entry.is_file():
                files[relative] = entry.read_bytes()
            elif entry.is_dir():
                directories.add(relative)
        return True, files, directories

    @classmethod
    def _restore_directory(
        cls,
        root: Path,
        snapshot: tuple[bool, dict[Path, bytes], set[Path]],
    ) -> None:
        existed, files, directories = snapshot
        if not existed:
            if root.exists():
                shutil.rmtree(root)
            return

        root.mkdir(parents=True, exist_ok=True)
        resolved_root = root.resolve(strict=False)
        current_files: set[Path] = set()
        current_directories: set[Path] = set()
        for entry in root.rglob("*"):
            resolved = entry.resolve(strict=False)
            if not resolved.is_relative_to(resolved_root):
                continue
            relative = resolved.relative_to(resolved_root)
            if entry.is_file():
                current_files.add(relative)
            elif entry.is_dir():
                current_directories.add(relative)

        for relative in current_files - set(files):
            (root / relative).unlink(missing_ok=True)
        for relative, payload in files.items():
            cls._restore_file(root / relative, payload)
        for relative in sorted(
            current_directories - directories,
            key=lambda candidate: len(candidate.parts),
            reverse=True,
        ):
            try:
                (root / relative).rmdir()
            except OSError:
                pass


class GMToolStateTransactionFactory:
    def __init__(self, host: Any) -> None:
        self.host = host

    def __call__(
        self,
        definition: GMToolDefinition,
        arguments: dict[str, object],
        context: GMToolExecutionContext,
    ) -> GMToolStateTransaction | "GMReplaceStateTransaction" | None:
        if definition.side_effect in {"write", "write_pending"}:
            return GMToolStateTransaction(self.host, definition, arguments, context)
        if definition.side_effect == "replace_state":
            return GMReplaceStateTransaction(self.host, definition, arguments, context)
        return None


class GMReplaceStateTransaction:
    """Rollback envelope for campaign create/load/delete operations.

    These tools can replace an entire in-memory runtime, switch the current
    campaign, or remove a campaign directory. A normal state snapshot is not
    enough: rollback must restore both the runtime registry and every affected
    campaign directory, including logs and named save slots.
    """

    def __init__(
        self,
        host: Any,
        definition: GMToolDefinition,
        arguments: dict[str, object],
        context: GMToolExecutionContext,
    ) -> None:
        self.host = host
        self.definition = definition
        self.context = context
        self.active = definition.side_effect == "replace_state"
        self.current_campaign_id = str(getattr(host, "current_campaign_id", "") or "")
        self.original_runtimes = dict(getattr(host, "runtimes", {}))
        self.runtime_snapshots: dict[str, CampaignStateSnapshot] = {}
        self.runtime_metadata: dict[str, dict[str, object]] = {}
        self.directory_snapshots: dict[Path, Path | None] = {}
        self.gate_file_snapshot: bytes | None = None
        self.gate_file_existed = False
        self._backup_root: tempfile.TemporaryDirectory[str] | None = None
        if not self.active:
            return

        # Capture every loaded runtime. Replacement handlers are rare and may
        # autosave the source campaign before touching the requested target.
        for campaign_id, runtime in self.original_runtimes.items():
            self.runtime_snapshots[campaign_id] = CampaignStateTransaction.capture(
                runtime.app,
                campaign_id,
            )
            self.runtime_metadata[campaign_id] = {
                "loaded_from_disk": bool(getattr(runtime, "loaded_from_disk", False)),
                "last_saved_path": str(getattr(runtime, "last_saved_path", "") or ""),
                "last_loaded_slot": str(getattr(runtime, "last_loaded_slot", "") or ""),
                "retired": bool(getattr(runtime, "retired", False)),
            }

        self._backup_root = tempfile.TemporaryDirectory(prefix="fu-gm-replace-state-")
        backup_root = Path(self._backup_root.name)
        store = host._memory_store()
        affected_ids = self._affected_campaign_ids(arguments, context)
        for index, campaign_id in enumerate(sorted(affected_ids)):
            campaign_dir = store._campaign_dir(campaign_id)
            if campaign_dir.exists():
                backup_dir = backup_root / f"campaign-{index}"
                shutil.copytree(campaign_dir, backup_dir)
                self.directory_snapshots[campaign_dir] = backup_dir
            else:
                self.directory_snapshots[campaign_dir] = None

        gate_path = Path(host.session_gates.path)
        self.gate_file_existed = gate_path.exists()
        self.gate_file_snapshot = gate_path.read_bytes() if gate_path.exists() else None

    def commit(self) -> None:
        if not self.active:
            return
        self.active = False
        self._cleanup()

    def rollback(self) -> None:
        if not self.active:
            return
        try:
            for campaign_dir, backup_dir in self.directory_snapshots.items():
                self._restore_directory(campaign_dir, backup_dir)

            for campaign_id, snapshot in self.runtime_snapshots.items():
                runtime = self.original_runtimes[campaign_id]
                CampaignStateTransaction.restore(runtime.app, snapshot)
                metadata = self.runtime_metadata[campaign_id]
                runtime.loaded_from_disk = bool(metadata["loaded_from_disk"])
                runtime.last_saved_path = str(metadata["last_saved_path"])
                runtime.last_loaded_slot = str(metadata["last_loaded_slot"])
                runtime.retired = bool(metadata["retired"])

            self.host.runtimes.clear()
            self.host.runtimes.update(self.original_runtimes)
            gate_path = Path(self.host.session_gates.path)
            GMToolStateTransaction._restore_file(
                gate_path,
                self.gate_file_snapshot if self.gate_file_existed else None,
            )
            self.host.current_campaign_id = self.current_campaign_id
        finally:
            self.active = False
            self._cleanup()

    def _affected_campaign_ids(
        self,
        arguments: dict[str, object],
        context: GMToolExecutionContext,
    ) -> set[str]:
        result = {
            str(context.campaign_id or "").strip(),
            str(arguments.get("campaign_id") or "").strip(),
            str(self.host._current_campaign_id() or "").strip(),
        }
        return {campaign_id for campaign_id in result if campaign_id}

    @classmethod
    def _restore_directory(cls, path: Path, backup: Path | None) -> None:
        if backup is None:
            cls._remove_path(path)
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        stage_root = Path(
            tempfile.mkdtemp(prefix=f".{path.name}.restore-", dir=path.parent)
        )
        displaced_root = Path(
            tempfile.mkdtemp(prefix=f".{path.name}.discard-", dir=path.parent)
        )
        staged = stage_root / path.name
        displaced = displaced_root / path.name
        try:
            shutil.copytree(backup, staged)
            if path.exists():
                os.replace(path, displaced)
            try:
                os.replace(staged, path)
            except Exception:
                if displaced.exists() and not path.exists():
                    os.replace(displaced, path)
                raise
        finally:
            shutil.rmtree(stage_root, ignore_errors=True)
            shutil.rmtree(displaced_root, ignore_errors=True)

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    def _cleanup(self) -> None:
        if self._backup_root is not None:
            self._backup_root.cleanup()
            self._backup_root = None
