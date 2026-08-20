from __future__ import annotations

import os
import shutil
import tempfile
import time
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
    """普通写工具或整条消息的可回滚事务。"""

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
        self.scope = str(arguments.get("_gm_transaction_scope") or "tool")
        if (
            self.scope == "tool"
            and context.metadata.get("_gm_message_transaction_id")
        ):
            self.scope = "message_child"
        self.active = definition.side_effect in {"write", "write_pending"}
        self.state_changed = False
        self.runtime: Any | None = None
        self.starting_version = 0
        self.lease_owner = str(
            context.metadata.get("_gm_message_transaction_id") or f"tool-{id(self)}"
        )
        self.previous_context_lease_owner = context.metadata.get(
            "_gm_active_write_lease_owner"
        )
        self.release_lease_on_close = False
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
        with self.runtime.transaction_lock:
            try:
                self._begin_versioned_scope()
                context.metadata["_gm_active_write_lease_owner"] = self.lease_owner
                self._capture(arguments)
            except Exception:
                self._release_lease()
                self._restore_context_lease_owner()
                raise

    def _begin_versioned_scope(self) -> None:
        runtime = self.runtime
        if runtime is None:
            return
        current_version = int(getattr(runtime, "state_version", 0) or 0)
        active_owner = str(getattr(runtime, "write_lease_owner", "") or "")
        if active_owner and active_owner != self.lease_owner:
            self._mark_version_conflict(current_version, active_owner)
            raise RuntimeError("战役正在提交另一条消息，请基于最新状态重新规划。")
        if not active_owner:
            runtime.write_lease_owner = self.lease_owner
            runtime.write_lease_started_at = time.monotonic()
            self.release_lease_on_close = True

        expected_raw = self.context.metadata.get("_gm_campaign_expected_version")
        if expected_raw is None:
            expected_raw = self.context.metadata.get("_gm_campaign_observed_version")
        expected_version = (
            current_version if expected_raw is None else int(expected_raw)
        )
        if expected_version != current_version:
            self._mark_version_conflict(current_version, active_owner)
            raise RuntimeError(
                f"战役状态已从版本 {expected_version} 更新到 {current_version}，"
                "请重新读取当前局面后再提交。"
            )
        self.starting_version = current_version
        self.context.metadata["_gm_campaign_expected_version"] = current_version

    def _capture(self, arguments: dict[str, object]) -> None:
        if self.runtime is None:
            return
        app = self.runtime.app
        self.campaign_snapshot = CampaignStateTransaction.capture(
            app,
            self.context.campaign_id,
        )
        self.last_saved_path = str(getattr(self.runtime, "last_saved_path", "") or "")
        campaign_dir = app.memory_store._campaign_dir(self.context.campaign_id)
        for name in self._CAMPAIGN_FILES:
            path = campaign_dir / name
            self.file_snapshot[path] = path.read_bytes() if path.exists() else None
        self.directory_snapshot[campaign_dir / "memory"] = self._capture_directory(
            campaign_dir / "memory"
        )
        log_manager = getattr(self.runtime, "log_manager", None)
        if log_manager is not None:
            for resolver_name in ("transcript_path", "transcript_txt_path"):
                resolver = getattr(log_manager, resolver_name, None)
                if not callable(resolver):
                    continue
                transcript_file = Path(
                    resolver(
                        self.context.campaign_id,
                        self.context.session_id,
                    )
                )
                self.file_snapshot[transcript_file] = (
                    transcript_file.read_bytes()
                    if transcript_file.exists()
                    else None
                )
            if self.definition.name == "end_session":
                artifact_paths = list(
                    log_manager.finalization_artifact_paths(
                        self.context.campaign_id,
                        self.context.session_id,
                    )
                )
                artifact_paths.append(
                    log_manager.summary_enrichment_path(
                        self.context.campaign_id,
                        self.context.session_id,
                    )
                )
                for artifact_path in artifact_paths:
                    path = Path(artifact_path)
                    self.file_snapshot.setdefault(
                        path,
                        path.read_bytes() if path.exists() else None,
                    )
        if self.definition.name == "save_campaign":
            slot = str(arguments.get("slot") or "").strip()
            if slot:
                slot_path = app.memory_store._snapshot_path(
                    self.context.campaign_id,
                    slot=slot,
                )
                self.file_snapshot[slot_path] = (
                    slot_path.read_bytes() if slot_path.exists() else None
                )
        gate_path = Path(self.host.session_gates.path)
        self.gate_file_existed = gate_path.exists()
        self.gate_file_snapshot = gate_path.read_bytes() if gate_path.exists() else None
        map_tools = getattr(self.host, "gm_map_tools", None)
        if map_tools is None:
            suite = getattr(self.host, "gm_tool_suite", None)
            map_tools = getattr(suite, "maps", None)
        if (
            map_tools is not None
            and callable(getattr(map_tools, "capture_transaction_state", None))
            and callable(getattr(map_tools, "restore_transaction_state", None))
        ):
            self.service_snapshots.append(
                (
                    map_tools,
                    map_tools.capture_transaction_state(self.context.campaign_id),
                )
            )

    def _mark_version_conflict(self, current_version: int, owner: str) -> None:
        self.context.metadata["_gm_campaign_version_conflict"] = {
            "observed_version": self.context.metadata.get(
                "_gm_campaign_observed_version"
            ),
            "current_version": current_version,
            "active_owner": owner,
        }

    def set_state_changed(self, state_changed: bool) -> None:
        self.state_changed = bool(state_changed)

    def commit(self) -> None:
        if not self.active:
            return
        if self.runtime is not None:
            with self.runtime.transaction_lock:
                if self.scope in {"tool", "message"} and self.state_changed:
                    self.runtime.state_version = (
                        int(getattr(self.runtime, "state_version", 0) or 0) + 1
                    )
                    self.context.metadata["_gm_campaign_expected_version"] = (
                        self.runtime.state_version
                    )
                self._release_lease()
                self._restore_context_lease_owner()
        self.active = False

    def rollback(self) -> None:
        if not self.active:
            return
        if self.runtime is None or self.campaign_snapshot is None:
            self.active = False
            return
        with self.runtime.transaction_lock:
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
            log_manager = getattr(self.runtime, "log_manager", None)
            invalidate_transcript = getattr(
                log_manager,
                "invalidate_transcript_cache",
                None,
            )
            if callable(invalidate_transcript):
                invalidate_transcript(
                    self.context.campaign_id,
                    self.context.session_id,
                )
            gate_path = Path(self.host.session_gates.path)
            self._restore_file(
                gate_path,
                self.gate_file_snapshot if self.gate_file_existed else None,
            )
            self.runtime.last_saved_path = self.last_saved_path
            self.runtime.state_version = self.starting_version
            self.context.metadata["_gm_campaign_expected_version"] = (
                self.starting_version
            )
            self.host.current_campaign_id = self.current_campaign_id
            self._release_lease()
            self._restore_context_lease_owner()
            self.active = False

    def _release_lease(self) -> None:
        runtime = self.runtime
        if runtime is None or not self.release_lease_on_close:
            return
        if str(getattr(runtime, "write_lease_owner", "") or "") == self.lease_owner:
            runtime.write_lease_owner = ""
            runtime.write_lease_started_at = 0.0
            condition = getattr(runtime, "write_lease_condition", None)
            if condition is not None:
                condition.notify_all()
        self.release_lease_on_close = False

    def _restore_context_lease_owner(self) -> None:
        if self.previous_context_lease_owner is None:
            if (
                self.context.metadata.get("_gm_active_write_lease_owner")
                == self.lease_owner
            ):
                self.context.metadata.pop("_gm_active_write_lease_owner", None)
            return
        self.context.metadata["_gm_active_write_lease_owner"] = (
            self.previous_context_lease_owner
        )

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
    """覆盖新建、读档与删除操作的整团回滚事务。"""

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
        self.scope = str(arguments.get("_gm_transaction_scope") or "tool")
        if (
            self.scope == "tool"
            and context.metadata.get("_gm_message_transaction_id")
        ):
            self.scope = "message_child"
        self.active = definition.side_effect == "replace_state"
        self.state_changed = False
        self.lease_owner = str(
            context.metadata.get("_gm_message_transaction_id")
            or f"replace-{id(self)}"
        )
        self.previous_context_lease_owner = context.metadata.get(
            "_gm_active_write_lease_owner"
        )
        self.affected_campaign_ids: set[str] = set()
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

        self.affected_campaign_ids = self._affected_campaign_ids(
            arguments,
            context,
        )
        self._acquire_runtime_leases()
        context.metadata["_gm_active_write_lease_owner"] = self.lease_owner

        try:
            # 替换工具可能先自动保存来源战役，因此需捕获全部已加载运行时。
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
                    "state_version": int(getattr(runtime, "state_version", 0) or 0),
                }

            self._backup_root = tempfile.TemporaryDirectory(prefix="fu-gm-replace-state-")
            backup_root = Path(self._backup_root.name)
            store = host._memory_store()
            for index, campaign_id in enumerate(sorted(self.affected_campaign_ids)):
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
        except Exception:
            self._release_runtime_leases()
            self._restore_context_lease_owner()
            raise

    def set_state_changed(self, state_changed: bool) -> None:
        self.state_changed = bool(state_changed)

    def commit(self) -> None:
        if not self.active:
            return
        try:
            if self.scope in {"tool", "message"} and self.state_changed:
                with self.host._runtimes_lock:
                    runtimes = [
                        self.host.runtimes[campaign_id]
                        for campaign_id in sorted(self.affected_campaign_ids)
                        if campaign_id in self.host.runtimes
                    ]
                for runtime in runtimes:
                    with runtime.transaction_lock:
                        runtime.state_version = (
                            int(getattr(runtime, "state_version", 0) or 0) + 1
                        )
        finally:
            if self.scope in {"tool", "message"}:
                self._release_runtime_leases()
            self._restore_context_lease_owner()
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
                runtime.state_version = int(metadata["state_version"])

            self.host.runtimes.clear()
            self.host.runtimes.update(self.original_runtimes)
            gate_path = Path(self.host.session_gates.path)
            GMToolStateTransaction._restore_file(
                gate_path,
                self.gate_file_snapshot if self.gate_file_existed else None,
            )
            self.host.current_campaign_id = self.current_campaign_id
        finally:
            if self.scope in {"tool", "message"}:
                self._release_runtime_leases()
            self._restore_context_lease_owner()
            self.active = False
            self._cleanup()

    def _acquire_runtime_leases(self) -> None:
        with self.host._runtimes_lock:
            runtimes = [
                self.host.runtimes[campaign_id]
                for campaign_id in sorted(self.affected_campaign_ids)
                if campaign_id in self.host.runtimes
            ]
        acquired: list[Any] = []
        try:
            for runtime in runtimes:
                with runtime.transaction_lock:
                    active_owner = str(
                        getattr(runtime, "write_lease_owner", "") or ""
                    )
                    if active_owner and active_owner != self.lease_owner:
                        self.context.metadata["_gm_campaign_version_conflict"] = {
                            "campaign_id": runtime.campaign_id,
                            "current_version": int(
                                getattr(runtime, "state_version", 0) or 0
                            ),
                            "active_owner": active_owner,
                        }
                        raise RuntimeError(
                            f"战役《{runtime.campaign_id}》正在提交另一条消息，"
                            "请稍后基于最新状态重试。"
                        )
                    if not active_owner:
                        runtime.write_lease_owner = self.lease_owner
                        runtime.write_lease_started_at = time.monotonic()
                    acquired.append(runtime)
        except Exception:
            for runtime in acquired:
                with runtime.transaction_lock:
                    if runtime.write_lease_owner == self.lease_owner:
                        runtime.write_lease_owner = ""
                        runtime.write_lease_started_at = 0.0
                        runtime.write_lease_condition.notify_all()
            raise

    def _release_runtime_leases(self) -> None:
        with self.host._runtimes_lock:
            runtimes = [
                self.host.runtimes[campaign_id]
                for campaign_id in sorted(self.affected_campaign_ids)
                if campaign_id in self.host.runtimes
            ]
        for runtime in runtimes:
            with runtime.transaction_lock:
                if runtime.write_lease_owner != self.lease_owner:
                    continue
                runtime.write_lease_owner = ""
                runtime.write_lease_started_at = 0.0
                runtime.write_lease_condition.notify_all()

    def _restore_context_lease_owner(self) -> None:
        if self.previous_context_lease_owner is None:
            if (
                self.context.metadata.get("_gm_active_write_lease_owner")
                == self.lease_owner
            ):
                self.context.metadata.pop("_gm_active_write_lease_owner", None)
            return
        self.context.metadata["_gm_active_write_lease_owner"] = (
            self.previous_context_lease_owner
        )

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
