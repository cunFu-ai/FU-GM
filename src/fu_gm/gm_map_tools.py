from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from fu_gm.components.semantic_map_manager import SemanticMapManager
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolReceipt,
    GMToolRegistry,
)


class MapToolHost(Protocol):
    def _runtime(self, campaign_id: str, *, auto_load: bool = True) -> Any: ...

    def _autosave_campaign(self, runtime: Any, campaign_id: str) -> str: ...


class GMMapToolService:
    """Player-facing world-map capabilities for the autonomous GM.

    WorldState remains the authority for geography. These tools only render or
    retrieve a visual artifact from that state; they never infer new locations.
    """

    _VISUAL_KIND = "world_map_visual"
    _POSITIONS = (
        "north",
        "northeast",
        "east",
        "southeast",
        "south",
        "southwest",
        "west",
        "northwest",
        "center",
    )
    _FEATURE_TYPES = (
        "settlement",
        "country",
        "mountain_range",
        "forest",
        "archipelago",
        "inland_sea",
        "lake",
        "coast",
        "region",
        "landmark",
        "fortress",
    )

    def __init__(self, host: MapToolHost) -> None:
        self.host = host
        self.semantic_maps = SemanticMapManager()
        self._placement_contexts: dict[str, dict[str, object]] = {}
        self._pending_redraw: dict[tuple[str, str, str], bool] = {}

    def capture_transaction_state(self, campaign_id: str) -> dict[str, object]:
        """Capture process-local map state that campaign snapshots do not own."""

        app = self.host._runtime(campaign_id).app
        artifact_directories = self._capture_artifact_directories(app)
        return {
            "placement_contexts": deepcopy(self._placement_contexts),
            "pending_redraw": deepcopy(self._pending_redraw),
            "generation_status": deepcopy(
                getattr(app, "_world_map_generation_status", {})
            ),
            "artifact_directories": artifact_directories,
        }

    def restore_transaction_state(
        self,
        campaign_id: str,
        snapshot: object,
    ) -> None:
        if not isinstance(snapshot, dict):
            raise TypeError("地图工具事务快照格式无效。")
        app = self.host._runtime(campaign_id).app
        self._remove_new_artifacts(snapshot.get("artifact_directories"))
        self._placement_contexts = deepcopy(
            dict(snapshot.get("placement_contexts") or {})
        )
        self._pending_redraw = deepcopy(
            dict(snapshot.get("pending_redraw") or {})
        )
        app._world_map_generation_status = deepcopy(
            dict(snapshot.get("generation_status") or {})
        )

    @classmethod
    def _capture_artifact_directories(
        cls,
        app: Any,
    ) -> list[dict[str, object]]:
        snapshots: list[dict[str, object]] = []
        for root in cls._artifact_roots(app):
            existed = root.is_dir()
            files, directories = cls._directory_entries(root)
            snapshots.append(
                {
                    "root": str(root),
                    "existed": existed,
                    "files": sorted(str(path) for path in files),
                    "directories": sorted(str(path) for path in directories),
                }
            )
        return snapshots

    @classmethod
    def _artifact_roots(cls, app: Any) -> list[Path]:
        manager = getattr(app, "world_map_image_manager", None)
        candidates: list[object] = []
        if manager is not None:
            renderer = getattr(manager, "renderer", None)
            candidates.extend(
                [
                    getattr(renderer, "output_dir", None),
                    getattr(getattr(renderer, "config", None), "output_dir", None),
                    getattr(getattr(manager, "config", None), "output_dir", None),
                ]
            )
        roots: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            if not candidate:
                continue
            try:
                root = Path(candidate).expanduser().resolve(strict=False)
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
            key = str(root)
            if key in seen:
                continue
            seen.add(key)
            roots.append(root)
        return roots

    @staticmethod
    def _directory_entries(root: Path) -> tuple[set[Path], set[Path]]:
        if not root.is_dir():
            return set(), set()
        files: set[Path] = set()
        directories: set[Path] = set()
        try:
            entries = list(root.rglob("*"))
        except OSError:
            return files, directories
        for entry in entries:
            try:
                resolved = entry.resolve(strict=False)
                if not resolved.is_relative_to(root):
                    continue
                if entry.is_file():
                    files.add(resolved)
                elif entry.is_dir():
                    directories.add(resolved)
            except (OSError, RuntimeError):
                continue
        return files, directories

    @classmethod
    def _remove_new_artifacts(cls, raw_snapshots: object) -> None:
        if not isinstance(raw_snapshots, list):
            return
        for raw in raw_snapshots:
            if not isinstance(raw, dict):
                continue
            try:
                root = Path(str(raw.get("root") or "")).expanduser().resolve(
                    strict=False
                )
            except (OSError, RuntimeError, ValueError):
                continue
            if not str(root) or not root.is_dir():
                continue
            previous_files = {
                Path(str(path)).expanduser().resolve(strict=False)
                for path in list(raw.get("files") or [])
                if str(path or "").strip()
            }
            previous_directories = {
                Path(str(path)).expanduser().resolve(strict=False)
                for path in list(raw.get("directories") or [])
                if str(path or "").strip()
            }
            current_files, current_directories = cls._directory_entries(root)
            for path in sorted(current_files - previous_files, reverse=True):
                if path.is_relative_to(root):
                    path.unlink(missing_ok=True)
            for path in sorted(
                current_directories - previous_directories,
                key=lambda candidate: len(candidate.parts),
                reverse=True,
            ):
                if not path.is_relative_to(root):
                    continue
                try:
                    path.rmdir()
                except OSError:
                    pass
            if not bool(raw.get("existed")):
                try:
                    root.rmdir()
                except OSError:
                    pass

    def register_tools(self, registry: GMToolRegistry) -> None:
        registry.register(
            GMToolDefinition(
                name="get_world_map_status",
                description=(
                    "查看当前战役是否已有与最新世界设定一致的地图，并在已有地图时把图片交给玩家。"
                    "只询问状态或要求查看现有地图时使用；没有现成地图且玩家明确要求绘制时，"
                    "改用generate_world_map_preview。地图还没有名字时，本工具会要求玩家先命名，"
                    "不会把带有“未命名大陆”的旧图发出去。"
                ),
                handler=self.get_status,
            )
        )
        registry.register(
            GMToolDefinition(
                name="inspect_semantic_map",
                description=(
                    "读取时悠可理解的20x12世界地图网格、地形图例、现有地点准确格位、"
                    "尚未放置的地点和路线。只读，不修改地图。玩家询问地点相对位置，"
                    "或你需要先理解整张地图时使用。新增或移动地点应改用"
                    "find_map_location_candidates，它也会返回完整网格。"
                ),
                handler=self.inspect_semantic_map,
            )
        )
        registry.register(
            GMToolDefinition(
                name="find_map_location_candidates",
                description=(
                    "在放置或移动地图地点前必须先调用。回执会向你展示完整语义网格、"
                    "所有已放置地点，以及当前一个目标地点结合地形、绝对方位、相对方位和"
                    "间距算出的合法候选格。多个地点会按相对依赖逐个处理，每放好一个就"
                    "重新读取更新后的地图。看完后从候选中选择，并立即调用"
                    "place_world_map_locations；不能自行编造网格坐标。"
                ),
                handler=self.find_map_location_candidates,
                parameters=(
                    GMToolParameter(
                        "location_names",
                        "array",
                        "要放置或移动的准确地点名；省略时读取所有尚未放置的地点。",
                        schema_details={
                            "items": {"type": "string", "minLength": 1},
                            "uniqueItems": True,
                            "maxItems": 12,
                        },
                    ),
                    GMToolParameter(
                        "candidate_limit",
                        "integer",
                        "每个地点返回的候选数，默认5，范围1至8。",
                        schema_details={"minimum": 1, "maximum": 8},
                    ),
                    GMToolParameter(
                        "redraw_after_placement",
                        "boolean",
                        "完成落点后是否立即重绘。玩家本句要求画图或重画时设为true。",
                    ),
                ),
                side_effect="write_pending",
            )
        )
        registry.register(
            GMToolDefinition(
                name="place_world_map_locations",
                description=(
                    "把地点放进刚刚读取过的语义地图。必须原样提交"
                    "find_map_location_candidates回执中的placement_context_id，"
                    "且每个grid_cell只能从对应地点的候选中选择。规则层会拒绝过期、"
                    "未读图、重叠或越界的坐标。"
                ),
                handler=self.place_world_map_locations,
                parameters=(
                    GMToolParameter(
                        "placement_context_id",
                        "string",
                        "刚才候选工具返回的上下文凭证。",
                        required=True,
                    ),
                    GMToolParameter(
                        "placements",
                        "array",
                        "每个待放置地点及你从其合法候选中选择的格位。",
                        required=True,
                        schema_details={
                            "minItems": 1,
                            "maxItems": 12,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "location_name": {
                                        "type": "string",
                                        "minLength": 1,
                                    },
                                    "grid_cell": {
                                        "type": "string",
                                        "pattern": "^[A-Ta-t](?:0?[1-9]|1[0-2])$",
                                    },
                                },
                                "required": ["location_name", "grid_cell"],
                                "additionalProperties": False,
                            },
                        },
                    ),
                ),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )
        registry.register(
            GMToolDefinition(
                name="generate_world_map_preview",
                description=(
                    "根据已经提交的世界地点、方位和地形立即绘制世界地图预览，并把图片交给玩家。"
                    "只有玩家明确要求现在画、生成或重画地图时调用；仅新增地点或讨论构图时不要调用。"
                    "同一句还包含新的世界设定时，先在同一call_tools中调用"
                    "commit_session_zero_update，再调用本工具，不能只记录设定后声称地图已经画好。"
                    "若回执要求map_name，先询问地图名称；玩家回答后把名称写入continent_name，"
                    "再重新调用本工具。"
                ),
                handler=self.generate_preview,
                parameters=(
                    GMToolParameter(
                        "redraw",
                        "boolean",
                        "已有且设定未变化的地图是否仍要重新绘制；仅在玩家明确要求重画时设为true。",
                    ),
                ),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )
        registry.register(
            GMToolDefinition(
                name="edit_world_map",
                description=(
                    "按照玩家已经明确说出的地图修改，命名地图，或更新一个地点的说明、类型、"
                    "地形、图标与绝对/相对方位；随后默认重绘并把新地图交给玩家。"
                    "例如“把托伦王国放到赤砂帝国西边”应填写location_name=托伦王国、"
                    "relative_to=赤砂帝国、relative_position=west。"
                    "不得从讨论、建议或未确认方案中擅自修改。已有地点必须使用"
                    "state_summary.map_locations里的准确名称；明确新增地点时才把"
                    "create_if_missing设为true并提供feature_type。地图名称回答也由本工具写入。"
                ),
                handler=self.edit_world_map,
                parameters=(
                    GMToolParameter(
                        "map_name",
                        "string",
                        "玩家明确指定的新地图或大陆名称；没有命名或改名时省略。",
                    ),
                    GMToolParameter(
                        "location_name",
                        "string",
                        "要新增或修改的一个地图地点准确名称；只改地图名称时省略。",
                    ),
                    GMToolParameter(
                        "create_if_missing",
                        "boolean",
                        "仅当玩家明确要求新增这个地点时设为true；修改已有地点时省略或设为false。",
                    ),
                    GMToolParameter(
                        "description",
                        "string",
                        "玩家本句明确补充或替换的地点说明；没有时省略。",
                    ),
                    GMToolParameter(
                        "feature_type",
                        "string",
                        "地图引擎地点类型；新增地点时必填，更新类型时按玩家语义填写。",
                        enum=self._FEATURE_TYPES,
                    ),
                    GMToolParameter(
                        "terrain",
                        "string",
                        "玩家明确指定的地形；没有时省略。",
                    ),
                    GMToolParameter(
                        "position_hint",
                        "string",
                        "地点在整张地图上的绝对方位；设置后会清除旧的相对方位。",
                        enum=self._POSITIONS,
                    ),
                    GMToolParameter(
                        "relative_to",
                        "string",
                        "玩家明确指定的参照地点准确名称；必须与relative_position一起使用。",
                    ),
                    GMToolParameter(
                        "relative_position",
                        "string",
                        "相对参照地点的方向；设置后会清除旧的绝对方位。",
                        enum=self._POSITIONS,
                    ),
                    GMToolParameter(
                        "draw_icon",
                        "boolean",
                        "玩家明确要求显示或隐藏该地点图标时填写。",
                    ),
                    GMToolParameter(
                        "redraw",
                        "boolean",
                        "是否立即重绘；默认true，只有玩家明确说暂不重画时才设为false。",
                    ),
                ),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )

    def state_summary(self, context: GMToolExecutionContext) -> dict[str, object]:
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        artifact = self._artifact(app)
        runtime_status = dict(app.world_map_generation_status())
        map_name = self._map_name(app)
        semantic = self.semantic_maps.snapshot(
            app.world_state,
            include_grid=False,
        )
        semantic_cell_by_name = {
            str(item["name"]): str(item["cell"])
            for item in semantic["locations"]
        }
        return {
            "has_map_foundation": bool(app._has_world_map_foundation()),
            "map_name": map_name,
            "needs_map_name": not bool(map_name),
            "map_locations": [
                {
                    "name": location.name,
                    "feature_type": str(location.feature_type or ""),
                    "terrain": str(location.terrain or ""),
                    "position_hint": str(location.position_hint or ""),
                    "relative_to": str(location.relative_to or ""),
                    "relative_position": str(location.relative_position or ""),
                    "semantic_cell": str(
                        semantic_cell_by_name.get(location.name, "")
                    ),
                    "draw_icon": location.draw_icon,
                }
                for location in sorted(
                    app.world_state.map_locations.values(),
                    key=lambda item: item.name,
                )
            ],
            "status": self._effective_status(runtime_status, artifact),
            "current_map_available": bool(artifact.get("current")),
            "stale_map_available": bool(artifact.get("available") and not artifact.get("current")),
            "semantic_layout": {
                "grid_size": semantic["grid_size"],
                "revision": semantic["revision"],
                "source": semantic["source"],
                "placed_count": sum(
                    1 for item in semantic["locations"] if item["cell"]
                ),
                "unplaced_locations": semantic["unplaced_locations"],
                "inspection_required_before_placement": True,
            },
        }

    def inspect_semantic_map(
        self,
        context: GMToolExecutionContext,
        _arguments: dict[str, object],
    ) -> GMToolReceipt:
        runtime = self.host._runtime(context.campaign_id)
        snapshot = self.semantic_maps.snapshot(
            runtime.app.world_state,
            include_grid=True,
        )
        return GMToolReceipt.success(
            "inspect_semantic_map",
            result={
                **snapshot,
                "rules_truth": (
                    "网格只表示地貌与相对位置；旅行日、危险与通路仍以routes为准。"
                ),
            },
        )

    def find_map_location_candidates(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        runtime = self.host._runtime(context.campaign_id)
        world_state = runtime.app.world_state
        snapshot = self.semantic_maps.snapshot(
            world_state,
            include_grid=True,
        )
        raw_names = arguments.get("location_names") or []
        if raw_names and not isinstance(raw_names, list):
            return GMToolReceipt.failure(
                "find_map_location_candidates",
                "INVALID_LOCATION_NAMES",
                "location_names必须是地点名称数组。",
                "使用state_summary.map_locations中的准确名称后重试。",
            )
        requested_names: list[str] = []
        for raw_name in raw_names:
            resolved = self._resolve_location_name(world_state, self._text(raw_name))
            if not resolved:
                return GMToolReceipt.failure(
                    "find_map_location_candidates",
                    "MAP_LOCATION_NOT_FOUND",
                    f"地图里没有名为“{self._text(raw_name)}”的地点。",
                    "先把玩家已经确认的新地点写入世界状态，或改用准确名称。",
                    result={
                        "available_locations": sorted(world_state.map_locations)
                    },
                )
            if resolved not in requested_names:
                requested_names.append(resolved)
        if not requested_names:
            requested_names = [
                str(name)
                for name in snapshot["unplaced_locations"]
            ]
        if not requested_names:
            return GMToolReceipt.success(
                "find_map_location_candidates",
                result={
                    **snapshot,
                    "status": "nothing_to_place",
                    "allowed_followup_tools": [],
                },
            )

        placed_names = {
            str(item["name"])
            for item in snapshot["locations"]
            if item["cell"]
        }
        requested_set = set(requested_names)
        active_name = next(
            (
                name
                for name in requested_names
                if not world_state.map_locations[name].relative_to
                or world_state.map_locations[name].relative_to in placed_names
                or world_state.map_locations[name].relative_to not in requested_set
            ),
            requested_names[0],
        )
        deferred_names = [
            name for name in requested_names if name != active_name
        ]
        limit = max(1, min(int(arguments.get("candidate_limit") or 5), 8))
        rows = self.semantic_maps.candidates(
            world_state,
            active_name,
            limit=limit,
        )
        if not rows:
            return GMToolReceipt.failure(
                "find_map_location_candidates",
                "NO_VALID_MAP_CELL",
                f"当前地图上找不到适合{active_name}的空闲位置。",
                "调整该地点的方位或地形约束后重新读取候选。",
            )
        candidates = {active_name: rows}

        placement_context_id = str(uuid4())
        pending_key = self._placement_key(context)
        redraw = bool(
            arguments.get(
                "redraw_after_placement",
                self._pending_redraw.get(pending_key, False),
            )
        )
        layout = self.semantic_maps.view(world_state)
        self._placement_contexts[placement_context_id] = {
            "campaign_id": context.campaign_id,
            "session_id": context.session_id,
            "speaker": context.speaker,
            "revision": layout.revision,
            "location_names": [active_name],
            "deferred_names": deferred_names,
            "allowed_cells": {
                name: {str(item["cell"]) for item in rows}
                for name, rows in candidates.items()
            },
            "redraw": redraw,
        }
        self._prune_placement_contexts()
        return GMToolReceipt.success(
            "find_map_location_candidates",
            result={
                **snapshot,
                "status": "candidates_ready",
                "placement_context_id": placement_context_id,
                "candidates": candidates,
                "current_location": active_name,
                "deferred_locations": deferred_names,
                "redraw_after_placement": redraw,
                "allowed_followup_tools": ["place_world_map_locations"],
                "required_followup_tools": ["place_world_map_locations"],
            },
            state_changed=True,
        )

    def place_world_map_locations(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        placement_context_id = self._text(
            arguments.get("placement_context_id")
        )
        placement_context = self._placement_contexts.get(placement_context_id)
        if placement_context is None:
            return GMToolReceipt.failure(
                "place_world_map_locations",
                "MAP_NOT_INSPECTED",
                "没有找到这次放置所依据的地图读取记录，或记录已经过期。",
                "重新调用find_map_location_candidates，看完当前网格和候选后再提交。",
                result={
                    "allowed_followup_tools": [
                        "find_map_location_candidates"
                    ],
                    "required_followup_tools": [
                        "find_map_location_candidates"
                    ],
                },
            )
        if (
            placement_context["campaign_id"] != context.campaign_id
            or placement_context["session_id"] != context.session_id
            or placement_context["speaker"] != context.speaker
        ):
            return GMToolReceipt.failure(
                "place_world_map_locations",
                "MAP_PLACEMENT_CONTEXT_MISMATCH",
                "这份候选不属于当前团、场次或请求者。",
                "为当前请求重新读取地图候选。",
            )

        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        world_state = app.world_state
        layout = self.semantic_maps.view(world_state)
        if int(placement_context["revision"]) != int(layout.revision):
            self._placement_contexts.pop(placement_context_id, None)
            return GMToolReceipt.failure(
                "place_world_map_locations",
                "STALE_MAP_PLACEMENT",
                "地图在你读取之后已经发生变化。",
                "重新调用find_map_location_candidates读取最新网格。",
                result={
                    "allowed_followup_tools": [
                        "find_map_location_candidates"
                    ],
                    "required_followup_tools": [
                        "find_map_location_candidates"
                    ],
                },
            )
        placements = arguments.get("placements") or []
        if not isinstance(placements, list):
            return GMToolReceipt.failure(
                "place_world_map_locations",
                "INVALID_PLACEMENTS",
                "placements必须是地点与格位对象组成的数组。",
                "按工具schema重新提交。",
            )
        submitted_names = {
            self._text(item.get("location_name"))
            for item in placements
            if isinstance(item, dict)
        }
        expected_names = set(placement_context["location_names"])
        if submitted_names != expected_names:
            return GMToolReceipt.failure(
                "place_world_map_locations",
                "INCOMPLETE_MAP_PLACEMENT",
                "本次需要为所有已读取目标各选择一个候选格。",
                "placements必须恰好包含：" + "、".join(sorted(expected_names)),
                result={
                    "expected_locations": sorted(expected_names),
                    "submitted_locations": sorted(
                        name for name in submitted_names if name
                    ),
                },
            )
        try:
            placed_layout = self.semantic_maps.place(
                world_state,
                [
                    dict(item)
                    for item in placements
                    if isinstance(item, dict)
                ],
                allowed_cells={
                    str(name): set(cells)
                    for name, cells in dict(
                        placement_context["allowed_cells"]
                    ).items()
                },
            )
        except (KeyError, TypeError, ValueError) as exc:
            return GMToolReceipt.failure(
                "place_world_map_locations",
                "INVALID_MAP_PLACEMENT",
                str(exc),
                "只从刚才每个地点自己的候选格中选择，不能交换、重叠或自造坐标。",
            )

        placed = {
            name: placed_layout.location_cells[name]
            for name in sorted(expected_names)
        }
        world_state.record_memory_event(
            "地图地点落位：" + "；".join(
                f"{name}@{cell}" for name, cell in placed.items()
            ),
            kind="world_map_placement",
            entities=sorted(expected_names),
            tags=["map", "placement"],
            source="GMMapToolService",
            payload={
                "placements": dict(placed),
                "semantic_revision": placed_layout.revision,
            },
        )
        saved_path = self.host._autosave_campaign(
            runtime,
            context.campaign_id,
        )
        self._placement_contexts.pop(placement_context_id, None)
        pending_key = self._placement_key(context)
        redraw = bool(placement_context.get("redraw", False))
        deferred_names = list(
            placement_context.get("deferred_names") or []
        )
        result: dict[str, object] = {
            "status": "placed",
            "placements": placed,
            "semantic_revision": placed_layout.revision,
            "saved_path": saved_path,
            "redraw": redraw,
            "reply_media": [],
        }

        remaining_unplaced = [
            name
            for name in deferred_names
            if not self.semantic_maps.view(world_state).location_cells.get(name)
        ]
        if remaining_unplaced:
            self._pending_redraw[pending_key] = redraw
            result.update(
                {
                    "status": "needs_placement",
                    "unplaced_locations": remaining_unplaced,
                    "allowed_followup_tools": [
                        "find_map_location_candidates"
                    ],
                    "required_followup_tools": [
                        "find_map_location_candidates"
                    ],
                }
            )
            return GMToolReceipt.success(
                "place_world_map_locations",
                result=result,
                state_changed=True,
            )

        self._pending_redraw.pop(pending_key, None)
        if redraw and self._map_name(app) and app._has_world_map_foundation():
            before_artifact = self._artifact(app)
            status = dict(
                app.ensure_world_map_for_adventure(
                    max_attempts=2,
                    force=True,
                )
            )
            artifact = self._artifact(app)
            generated_now = bool(
                artifact.get("current")
                and artifact.get("event_id")
                and artifact.get("event_id")
                != before_artifact.get("event_id")
            )
            result.update(status)
            result["status"] = self._effective_status(
                status,
                artifact,
                generated_now=generated_now,
            )
            result["artifact"] = artifact
            result["reply_media"] = (
                self._reply_media(artifact)
                if artifact.get("current")
                else []
            )
            if generated_now:
                result["saved_path"] = self.host._autosave_campaign(
                    runtime,
                    context.campaign_id,
                )
        elif redraw and not self._map_name(app):
            result.update(
                {
                    "status": "needs_name",
                    "required_field": "map_name",
                    "resume_tool": "generate_world_map_preview",
                }
            )

        if result["status"] in {"generated", "ready"}:
            reply = "地点已经放好，新地图在这里。"
        elif result["status"] == "needs_name":
            reply = "地点已经放好。这张地图还没有名字，你想叫它什么？"
        elif redraw:
            reply = "地点已经放好，但这次地图没有画成。"
        else:
            reply = "地点已经放到地图上了。"
        return GMToolReceipt.success(
            "place_world_map_locations",
            result=result,
            state_changed=True,
            public_reply=reply,
            lock_public_reply=bool(result["reply_media"]),
        )

    def edit_world_map(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        world_state = app.world_state
        map_name = self._text(arguments.get("map_name"))
        requested_name = self._text(arguments.get("location_name"))
        create_if_missing = bool(arguments.get("create_if_missing", False))
        position_hint = self._text(arguments.get("position_hint"))
        relative_to = self._text(arguments.get("relative_to"))
        relative_position = self._text(arguments.get("relative_position"))

        if bool(relative_to) != bool(relative_position):
            return GMToolReceipt.failure(
                "edit_world_map",
                "INCOMPLETE_RELATIVE_POSITION",
                "相对方位需要同时给出参照地点和方向。",
                "同时提交relative_to与relative_position，或改用position_hint。",
                result={"available_locations": sorted(world_state.map_locations)},
            )
        if position_hint and relative_to:
            return GMToolReceipt.failure(
                "edit_world_map",
                "AMBIGUOUS_POSITION",
                "同一次地点修改不能同时使用绝对方位和相对方位。",
                "根据玩家原话保留更具体的一种方位后重试。",
            )

        meaningful_location_fields = {
            "description",
            "feature_type",
            "terrain",
            "position_hint",
            "relative_to",
            "relative_position",
            "draw_icon",
        }
        has_location_change = bool(
            requested_name
            and (
                create_if_missing
                or any(key in arguments for key in meaningful_location_fields)
            )
        )
        if not map_name and not has_location_change:
            return GMToolReceipt.failure(
                "edit_world_map",
                "NO_MAP_EDIT",
                "没有提交地图名称或地点修改。",
                "从玩家原话中提交map_name，或location_name及其明确修改字段。",
            )

        changed: list[str] = []
        requires_placement = False
        if map_name:
            self._set_map_name(app, map_name)
            changed.append(f"地图命名为{map_name}")

        location = None
        if requested_name:
            resolved_name = self._resolve_location_name(world_state, requested_name)
            if not resolved_name and not create_if_missing:
                return GMToolReceipt.failure(
                    "edit_world_map",
                    "MAP_LOCATION_NOT_FOUND",
                    f"地图里没有名为“{requested_name}”的地点。",
                    "使用available_locations中的准确名称；只有玩家明确新增时才设create_if_missing=true。",
                    result={"available_locations": sorted(world_state.map_locations)},
                )
            if not resolved_name:
                feature_type = self._text(arguments.get("feature_type"))
                if not feature_type:
                    return GMToolReceipt.failure(
                        "edit_world_map",
                        "NEW_LOCATION_TYPE_REQUIRED",
                        "新增地图地点必须说明地图引擎类型。",
                        "根据玩家语义补充feature_type后重试。",
                    )
                location = app.world_map_manager.add_location(
                    requested_name,
                    description=self._text(arguments.get("description")),
                    terrain=self._text(arguments.get("terrain")),
                    feature_type=feature_type,
                    draw_icon=(
                        bool(arguments["draw_icon"])
                        if "draw_icon" in arguments
                        else None
                    ),
                )
                resolved_name = requested_name
                changed.append(f"新增{resolved_name}")
                requires_placement = True
            else:
                location = world_state.map_locations[resolved_name]

            if relative_to:
                resolved_reference = self._resolve_location_name(world_state, relative_to)
                if not resolved_reference:
                    return GMToolReceipt.failure(
                        "edit_world_map",
                        "MAP_REFERENCE_NOT_FOUND",
                        f"地图里没有名为“{relative_to}”的参照地点。",
                        "使用available_locations中的准确名称后重试。",
                        result={"available_locations": sorted(world_state.map_locations)},
                    )
                if resolved_reference == resolved_name:
                    return GMToolReceipt.failure(
                        "edit_world_map",
                        "SELF_RELATIVE_POSITION",
                        "地点不能以自身作为相对方位参照。",
                        "改用另一个具名地点或position_hint。",
                    )
                location.relative_to = resolved_reference
                location.relative_position = relative_position
                location.position_hint = ""
                requires_placement = True
                changed.append(
                    f"{resolved_name}位于{resolved_reference}的{self._position_label(relative_position)}"
                )
            elif position_hint:
                location.position_hint = position_hint
                location.relative_to = ""
                location.relative_position = ""
                requires_placement = True
                changed.append(f"{resolved_name}位于地图{self._position_label(position_hint)}")

            description = self._text(arguments.get("description"))
            if description:
                location.description = description
                world_state.map_notes[resolved_name] = description
                world_state.world_profile.major_locations[resolved_name] = description
                if world_state.world_sheet is not None:
                    world_state.world_sheet.major_locations[resolved_name] = description
                changed.append(f"更新{resolved_name}说明")
            feature_type = self._text(arguments.get("feature_type"))
            if feature_type:
                location.feature_type = feature_type
                if feature_type == "country":
                    world_state.world_profile.kingdoms.setdefault(
                        resolved_name,
                        description or location.description,
                    )
                changed.append(f"更新{resolved_name}类型")
            terrain = self._text(arguments.get("terrain"))
            if terrain:
                location.terrain = terrain
                changed.append(f"更新{resolved_name}地形")
            if "draw_icon" in arguments:
                location.draw_icon = bool(arguments["draw_icon"])
                changed.append(f"更新{resolved_name}图标")

        redraw = bool(arguments.get("redraw", True))
        if requires_placement and location is not None:
            self.semantic_maps.clear_location(
                world_state,
                str(location.name),
            )
            self._pending_redraw[self._placement_key(context)] = redraw

        world_state.record_memory_event(
            "地图调整：" + "；".join(dict.fromkeys(changed)),
            kind="world_map_edit",
            entities=[requested_name] if requested_name else [],
            tags=["map", "edit"],
            source="GMMapToolService",
            payload={
                "map_name": map_name,
                "location_name": str(getattr(location, "name", "") or ""),
                "position_hint": str(getattr(location, "position_hint", "") or ""),
                "relative_to": str(getattr(location, "relative_to", "") or ""),
                "relative_position": str(
                    getattr(location, "relative_position", "") or ""
                ),
            },
        )
        saved_path = self.host._autosave_campaign(runtime, context.campaign_id)

        result: dict[str, object] = {
            "changes": list(dict.fromkeys(changed)),
            "saved_path": saved_path,
            "redraw": redraw,
            "reply_media": [],
        }
        if not self._map_name(app):
            result.update(
                {
                    "status": "needs_name",
                    "required_field": "map_name",
                    "resume_tool": "edit_world_map",
                }
            )
            return GMToolReceipt.success(
                "edit_world_map",
                result=result,
                state_changed=True,
                public_reply="位置先改好了。这张地图还没有名字，你想叫它什么？",
                lock_public_reply=True,
            )

        semantic = self.semantic_maps.snapshot(
            world_state,
            include_grid=False,
        )
        if semantic["unplaced_locations"]:
            self._pending_redraw[self._placement_key(context)] = redraw
            result.update(
                {
                    "status": "needs_placement",
                    "unplaced_locations": semantic["unplaced_locations"],
                    "allowed_followup_tools": [
                        "find_map_location_candidates"
                    ],
                    "required_followup_tools": [
                        "find_map_location_candidates"
                    ],
                }
            )
            return GMToolReceipt.success(
                "edit_world_map",
                result=result,
                state_changed=True,
            )

        if redraw and app._has_world_map_foundation():
            before_artifact = self._artifact(app)
            status = dict(
                app.ensure_world_map_for_adventure(
                    max_attempts=2,
                    force=True,
                )
            )
            artifact = self._artifact(app)
            generated_now = bool(
                artifact.get("current")
                and artifact.get("event_id")
                and artifact.get("event_id") != before_artifact.get("event_id")
            )
            result.update(status)
            result["status"] = self._effective_status(
                status,
                artifact,
                generated_now=generated_now,
            )
            result["artifact"] = artifact
            result["reply_media"] = (
                self._reply_media(artifact) if artifact.get("current") else []
            )
            if generated_now:
                result["saved_path"] = self.host._autosave_campaign(
                    runtime,
                    context.campaign_id,
                )

        reply = self._map_edit_reply(changed, str(result.get("status") or ""), redraw)
        return GMToolReceipt.success(
            "edit_world_map",
            result=result,
            state_changed=True,
            public_reply=reply,
            lock_public_reply=True,
        )

    def get_status(
        self,
        context: GMToolExecutionContext,
        _arguments: dict[str, object],
    ) -> GMToolReceipt:
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        map_name = self._map_name(app)
        if not map_name:
            return self._missing_map_name_receipt("get_world_map_status")
        artifact = self._artifact(app)
        status = self._effective_status(
            dict(app.world_map_generation_status()),
            artifact,
        )
        result = {
            "status": status,
            "has_map_foundation": bool(app._has_world_map_foundation()),
            "artifact": artifact,
            "reply_media": self._reply_media(artifact) if artifact.get("current") else [],
        }
        if artifact.get("current"):
            reply = "现有地图在这里。"
        elif artifact.get("available"):
            reply = "现有地图已经落后于最新设定，需要重新绘制。"
        elif status == "generating":
            reply = "地图还在绘制中。"
        elif status == "failed":
            reply = "上一轮地图没有画成。"
        else:
            reply = "当前还没有生成地图。"
        return GMToolReceipt.success(
            "get_world_map_status",
            result=result,
            public_reply=reply,
            lock_public_reply=True,
        )

    def generate_preview(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        if not app._has_world_map_foundation():
            return GMToolReceipt.success(
                "generate_world_map_preview",
                result={
                    "status": "deferred",
                    "has_map_foundation": False,
                    "reply_media": [],
                },
                public_reply="现在还没有足够的地理设定可以成图；至少先确定一个大陆、国家或主要地点。",
                lock_public_reply=True,
            )
        if not self._map_name(app):
            return self._missing_map_name_receipt("generate_world_map_preview")

        redraw = bool(arguments.get("redraw", False))
        self.semantic_maps.initialize(app.world_state)
        semantic = self.semantic_maps.snapshot(
            app.world_state,
            include_grid=False,
        )
        if semantic["unplaced_locations"]:
            self._pending_redraw[self._placement_key(context)] = True
            saved_path = self.host._autosave_campaign(
                runtime,
                context.campaign_id,
            )
            return GMToolReceipt.success(
                "generate_world_map_preview",
                result={
                    "status": "needs_placement",
                    "unplaced_locations": semantic["unplaced_locations"],
                    "saved_path": saved_path,
                    "reply_media": [],
                    "allowed_followup_tools": [
                        "find_map_location_candidates"
                    ],
                    "required_followup_tools": [
                        "find_map_location_candidates"
                    ],
                },
                state_changed=True,
            )
        before_artifact = self._artifact(app)
        status = dict(
            app.ensure_world_map_for_adventure(
                max_attempts=2,
                force=redraw,
            )
        )
        artifact = self._artifact(app)
        generated_now = bool(
            artifact.get("current")
            and artifact.get("event_id")
            and artifact.get("event_id") != before_artifact.get("event_id")
        )
        effective_status = self._effective_status(
            status,
            artifact,
            generated_now=generated_now,
        )
        result = {
            **status,
            "status": effective_status,
            "redraw": redraw,
            "artifact": artifact,
            "reply_media": self._reply_media(artifact) if artifact.get("current") else [],
        }

        generated = generated_now
        if generated:
            result["saved_path"] = self.host._autosave_campaign(
                runtime,
                context.campaign_id,
            )
            reply = "地图画好了。"
        elif effective_status == "ready" and artifact.get("current"):
            reply = "地图已经有一版与当前设定一致的版本，我把它发出来了。"
        elif effective_status == "deferred":
            reply = "现在还没有足够的地理设定可以成图；至少先确定一个大陆、国家或主要地点。"
        elif effective_status == "unavailable":
            reply = "地图绘制器当前不可用，这次没有生成图片。"
        elif effective_status == "generating":
            reply = "地图正在绘制中，完成后才能发出来。"
        else:
            reply = "这次地图没有画成，我没有把它当作已经完成。"

        return GMToolReceipt.success(
            "generate_world_map_preview",
            result=result,
            state_changed=generated,
            public_reply=reply,
            lock_public_reply=True,
        )

    def _artifact(self, app: Any) -> dict[str, object]:
        event = next(
            (
                item
                for item in reversed(app.world_state.memory_events)
                if str(getattr(item, "kind", "") or "") == self._VISUAL_KIND
            ),
            None,
        )
        if event is None:
            return {
                "available": False,
                "current": False,
                "output_path": "",
                "thumbnail_path": "",
                "remote_url": "",
                "event_id": "",
            }
        payload = dict(getattr(event, "payload", {}) or {})
        output_path = str(payload.get("output_path") or "").strip()
        thumbnail_path = str(payload.get("thumbnail_path") or "").strip()
        remote_url = str(payload.get("remote_url") or "").strip()
        local_candidate = thumbnail_path or output_path
        local_exists = bool(
            local_candidate
            and Path(local_candidate).expanduser().is_file()
        )
        available = bool(local_exists or remote_url)
        manager = getattr(app, "world_map_image_manager", None)
        current = False
        if available and manager is not None:
            try:
                current = bool(manager.has_current_map(app.world_state))
            except Exception:
                current = False
        return {
            "available": available,
            "current": current,
            "output_path": output_path,
            "thumbnail_path": thumbnail_path,
            "remote_url": remote_url,
            "renderer": str(payload.get("renderer") or payload.get("model") or ""),
            "settings_path": str(payload.get("settings_path") or ""),
            "manifest_path": str(payload.get("manifest_path") or ""),
            "map_seed": payload.get("map_seed"),
            "terrain_seed": payload.get("terrain_seed"),
            "event_id": str(getattr(event, "event_id", "") or ""),
        }

    @staticmethod
    def _map_name(app: Any) -> str:
        world_state = app.world_state
        world_sheet = getattr(world_state, "world_sheet", None)
        candidates = [
            str(getattr(world_sheet, "continent_name", "") or "").strip(),
            str(
                getattr(
                    getattr(world_state, "world_profile", None),
                    "continent_name",
                    "",
                )
                or ""
            ).strip(),
        ]
        placeholders = {
            "未命名大陆",
            "未命名世界",
            "未命名地图",
            "暂未命名",
            "待定",
        }
        return next(
            (name for name in candidates if name and name not in placeholders),
            "",
        )

    @staticmethod
    def _missing_map_name_receipt(tool_name: str) -> GMToolReceipt:
        return GMToolReceipt.success(
            tool_name,
            result={
                "status": "needs_name",
                "required_field": "continent_name",
                "resume_tool": "generate_world_map_preview",
                "reply_media": [],
            },
            public_reply="这张地图还没有名字。你想叫它什么？",
            lock_public_reply=True,
        )

    @staticmethod
    def _effective_status(
        runtime_status: dict[str, object],
        artifact: dict[str, object],
        *,
        generated_now: bool = False,
    ) -> str:
        status = str(runtime_status.get("status") or "idle").strip().lower()
        if artifact.get("current"):
            return "generated" if generated_now else "ready"
        if artifact.get("available"):
            return "stale"
        return status

    @staticmethod
    def _reply_media(artifact: dict[str, object]) -> list[dict[str, object]]:
        if not artifact.get("available"):
            return []
        return [
            {
                "type": "image",
                "path": str(
                    artifact.get("thumbnail_path")
                    or artifact.get("output_path")
                    or ""
                ),
                "url": str(artifact.get("remote_url") or ""),
                "alt": "世界地图",
            }
        ]

    @staticmethod
    def _text(value: object) -> str:
        return " ".join(str(value or "").split()).strip()

    @staticmethod
    def _placement_key(
        context: GMToolExecutionContext,
    ) -> tuple[str, str, str]:
        return (
            str(context.campaign_id or ""),
            str(context.session_id or ""),
            str(context.speaker or ""),
        )

    def _prune_placement_contexts(self) -> None:
        while len(self._placement_contexts) > 64:
            oldest = next(iter(self._placement_contexts))
            self._placement_contexts.pop(oldest, None)

    @classmethod
    def _resolve_location_name(cls, world_state: Any, requested: str) -> str:
        clean = cls._text(requested)
        if clean in world_state.map_locations:
            return clean
        folded = clean.casefold()
        matches = [
            name
            for name in world_state.map_locations
            if cls._text(name).casefold() == folded
        ]
        return matches[0] if len(matches) == 1 else ""

    @staticmethod
    def _position_label(position: str) -> str:
        return {
            "north": "北侧",
            "northeast": "东北侧",
            "east": "东侧",
            "southeast": "东南侧",
            "south": "南侧",
            "southwest": "西南侧",
            "west": "西侧",
            "northwest": "西北侧",
            "center": "中央",
        }.get(position, position)

    @staticmethod
    def _set_map_name(app: Any, map_name: str) -> None:
        world_state = app.world_state
        world_state.world_profile.continent_name = map_name
        if world_state.world_sheet is not None:
            world_state.world_sheet.continent_name = map_name
        session_zero = getattr(app, "session_zero_manager", None)
        session_world = getattr(getattr(session_zero, "state", None), "world", None)
        if session_world is not None:
            session_world.continent_name = map_name

    @staticmethod
    def _map_edit_reply(changes: list[str], status: str, redraw: bool) -> str:
        summary = "；".join(dict.fromkeys(changes))
        if status in {"generated", "ready"}:
            return f"{summary}。新地图在这里。"
        if redraw and status in {"unavailable", "failed"}:
            return f"{summary}。修改已经保存，但这次地图没有画成。"
        if redraw and status == "generating":
            return f"{summary}。地图正在重画。"
        return f"{summary}。"
