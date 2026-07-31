from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Iterable

from fu_gm.components.world_state import WorldState
from fu_gm.models import MapLocation, SemanticMapLayout


class SemanticMapManager:
    """Maintain the GM-readable spatial layer of a world map.

    The semantic grid describes placement and visible terrain only. Travel
    distance, danger and route legality remain authoritative in
    ``WorldState.map_routes``.
    """

    GRID_WIDTH = 20
    GRID_HEIGHT = 12
    TERRAIN_LEGEND = {
        "~": "外海/水域",
        ".": "陆地",
        "C": "海岸",
        "F": "森林",
        "M": "山脉",
        "H": "丘陵",
        "I": "内海",
        "L": "湖泊",
        "A": "群岛",
    }
    _CELL_PATTERN = re.compile(r"^([A-Z])(\d{1,2})$")
    _POSITION_ANCHORS = {
        "north": (0.50, 0.12),
        "northeast": (0.82, 0.18),
        "east": (0.88, 0.50),
        "southeast": (0.82, 0.82),
        "south": (0.50, 0.88),
        "southwest": (0.18, 0.82),
        "west": (0.12, 0.50),
        "northwest": (0.18, 0.18),
        "center": (0.50, 0.50),
    }
    _DIRECTION_VECTORS = {
        "north": (0, -1),
        "northeast": (1, -1),
        "east": (1, 0),
        "southeast": (1, 1),
        "south": (0, 1),
        "southwest": (-1, 1),
        "west": (-1, 0),
        "northwest": (-1, -1),
        "center": (0, 0),
    }

    def view(self, world_state: WorldState) -> SemanticMapLayout:
        """Return a complete layout without mutating campaign state."""

        stored = deepcopy(getattr(world_state, "semantic_map", SemanticMapLayout()))
        if self._valid_layout(stored):
            layout = stored
        else:
            layout = self._layout_from_latest_artifact(world_state)
            if layout is None:
                layout = self._blank_layout()

        layout.grid_width = self.GRID_WIDTH
        layout.grid_height = self.GRID_HEIGHT
        layout.terrain_rows = self._normalize_terrain_rows(layout.terrain_rows)

        for name, location in world_state.map_locations.items():
            cell = self.normalize_cell(
                str(getattr(location, "semantic_cell", "") or "")
                or str(layout.location_cells.get(name) or "")
            )
            if cell:
                layout.location_cells[name] = cell
        layout.location_cells = {
            name: cell
            for name, raw_cell in layout.location_cells.items()
            if name in world_state.map_locations
            and (cell := self.normalize_cell(raw_cell))
        }
        layout.location_points = {
            name: dict(point)
            for name, point in layout.location_points.items()
            if name in world_state.map_locations and isinstance(point, dict)
        }
        layout.location_cells = self._deduplicate_location_cells(
            layout.location_cells,
            layout.location_points,
        )
        return layout

    def initialize(self, world_state: WorldState) -> SemanticMapLayout:
        """Persist a migrated or blank layout and synchronize location cells."""

        layout = self.view(world_state)
        if not layout.source:
            layout.source = "semantic_planning_grid"
        for name, cell in layout.location_cells.items():
            location = world_state.map_locations.get(name)
            if location is None:
                continue
            location.semantic_cell = cell
            location.x, location.y = self.cell_xy(cell)
        world_state.semantic_map = layout
        return layout

    def snapshot(
        self,
        world_state: WorldState,
        *,
        include_grid: bool = True,
    ) -> dict[str, object]:
        layout = self.view(world_state)
        locations: list[dict[str, object]] = []
        for location in sorted(
            world_state.map_locations.values(),
            key=lambda item: item.name,
        ):
            cell = layout.location_cells.get(location.name, "")
            point = dict(layout.location_points.get(location.name) or {})
            locations.append(
                {
                    "name": location.name,
                    "cell": cell,
                    "feature_type": str(location.feature_type or ""),
                    "terrain": str(location.terrain or ""),
                    "position_hint": str(location.position_hint or ""),
                    "relative_to": str(location.relative_to or ""),
                    "relative_position": str(location.relative_position or ""),
                    "actual_normalized_point": point,
                }
            )
        result: dict[str, object] = {
            "version": layout.version,
            "revision": layout.revision,
            "grid_size": f"{layout.grid_width}x{layout.grid_height}",
            "source": layout.source or "semantic_planning_grid",
            "terrain_legend": dict(self.TERRAIN_LEGEND),
            "locations": locations,
            "unplaced_locations": [
                item["name"] for item in locations if not item["cell"]
            ],
            "routes": [
                {
                    "route_id": route.route_id,
                    "origin": route.origin,
                    "destination": route.destination,
                    "distance_days": route.distance_days,
                }
                for route in sorted(
                    world_state.map_routes.values(),
                    key=lambda item: item.route_id,
                )
            ],
        }
        if include_grid:
            result["grid"] = self.ascii_grid(world_state, layout=layout)
        return result

    def candidates(
        self,
        world_state: WorldState,
        location_name: str,
        *,
        limit: int = 5,
    ) -> list[dict[str, object]]:
        layout = self.view(world_state)
        location = world_state.map_locations.get(location_name)
        if location is None:
            raise KeyError(location_name)

        occupied = {
            cell: name
            for name, cell in layout.location_cells.items()
            if name != location_name and cell
        }
        reference_cell = layout.location_cells.get(location.relative_to, "")
        scored: list[tuple[float, str, list[str]]] = []
        for row in range(layout.grid_height):
            for column in range(layout.grid_width):
                cell = self.cell_name(column, row)
                if cell in occupied:
                    continue
                symbol = layout.terrain_rows[row][column]
                terrain_score, terrain_reason = self._terrain_score(
                    location,
                    symbol,
                    source=layout.source,
                )
                if terrain_score <= -100:
                    continue
                score = terrain_score
                reasons = [terrain_reason] if terrain_reason else []

                position_score, position_reason = self._position_score(
                    location,
                    column,
                    row,
                    layout,
                )
                score += position_score
                if position_reason:
                    reasons.append(position_reason)

                relative_score, relative_reason = self._relative_score(
                    location,
                    cell,
                    reference_cell,
                )
                score += relative_score
                if relative_reason:
                    reasons.append(relative_reason)

                separation = self._nearest_occupied_distance(
                    column,
                    row,
                    occupied,
                )
                if separation is not None:
                    if separation < 1.5:
                        score -= 9
                    else:
                        score += min(separation, 5.0) * 0.55
                scored.append((score, cell, reasons))

        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            {
                "cell": cell,
                "terrain": self.TERRAIN_LEGEND.get(
                    self.terrain_at(layout, cell),
                    "未知",
                ),
                "score": round(score, 2),
                "reason": "；".join(dict.fromkeys(reasons))
                or "位置可用且不与现有地点重叠",
            }
            for score, cell, reasons in scored[: max(1, min(int(limit), 12))]
        ]

    def place(
        self,
        world_state: WorldState,
        placements: Iterable[dict[str, object]],
        *,
        allowed_cells: dict[str, set[str]] | None = None,
        source: str = "gm_semantic_placement",
    ) -> SemanticMapLayout:
        """Atomically validate and apply one or more model-selected cells."""

        layout = self.initialize(world_state)
        normalized: list[tuple[str, str]] = []
        selected_cells: set[str] = set()
        currently_occupied = {
            cell: name
            for name, cell in layout.location_cells.items()
            if cell
        }
        for item in placements:
            name = str(item.get("location_name") or "").strip()
            cell = self.normalize_cell(str(item.get("grid_cell") or ""))
            if not name or name not in world_state.map_locations:
                raise ValueError(f"地图里没有地点：{name or '（空）'}")
            if not cell:
                raise ValueError(
                    f"{name} 的网格坐标无效；合法格式为 A01 至 T12。"
                )
            if cell in selected_cells:
                raise ValueError(f"同一次放置不能让多个地点占用 {cell}。")
            occupant = currently_occupied.get(cell)
            if occupant and occupant != name:
                raise ValueError(f"{cell} 已由 {occupant} 占用。")
            if allowed_cells is not None and cell not in allowed_cells.get(name, set()):
                raise ValueError(f"{cell} 不在 {name} 本次读取到的合法候选中。")
            normalized.append((name, cell))
            selected_cells.add(cell)

        for name, cell in normalized:
            layout.location_cells[name] = cell
            layout.location_points.pop(name, None)
            location = world_state.map_locations[name]
            location.semantic_cell = cell
            location.x, location.y = self.cell_xy(cell)

        layout.source = source
        layout.revision = max(0, int(layout.revision)) + 1
        layout.updated_at = self._now()
        world_state.semantic_map = layout
        return layout

    def clear_location(self, world_state: WorldState, location_name: str) -> None:
        layout = self.initialize(world_state)
        layout.location_cells.pop(location_name, None)
        layout.location_points.pop(location_name, None)
        location = world_state.map_locations.get(location_name)
        if location is not None:
            location.semantic_cell = ""
        layout.revision += 1
        layout.updated_at = self._now()
        world_state.semantic_map = layout

    def apply_manifest(
        self,
        world_state: WorldState,
        manifest: dict[str, object],
        *,
        manifest_path: str = "",
    ) -> SemanticMapLayout:
        width = int(manifest.get("grid_width") or self.GRID_WIDTH)
        height = int(manifest.get("grid_height") or self.GRID_HEIGHT)
        if width != self.GRID_WIDTH or height != self.GRID_HEIGHT:
            raise ValueError(
                f"地图布局清单网格必须为 {self.GRID_WIDTH}x{self.GRID_HEIGHT}。"
            )
        rows = self._normalize_terrain_rows(
            list(manifest.get("terrain_rows") or [])
        )
        raw_locations = manifest.get("locations") or {}
        if not isinstance(raw_locations, dict):
            raw_locations = {}
        cells: dict[str, str] = {}
        points: dict[str, dict[str, Any]] = {}
        for name, raw in raw_locations.items():
            if name not in world_state.map_locations or not isinstance(raw, dict):
                continue
            cell = self.normalize_cell(str(raw.get("cell") or ""))
            if not cell:
                continue
            cells[name] = cell
            point = {
                key: raw[key]
                for key in (
                    "normalized_x",
                    "normalized_y",
                    "terrain",
                    "anchor_kind",
                )
                if key in raw
            }
            points[name] = point

        previous = self.view(world_state)
        merged_cells = self._deduplicate_location_cells(
            {**previous.location_cells, **cells},
            {**previous.location_points, **points},
        )
        layout = SemanticMapLayout(
            version=max(1, int(manifest.get("version") or 1)),
            grid_width=self.GRID_WIDTH,
            grid_height=self.GRID_HEIGHT,
            terrain_rows=rows,
            location_cells=merged_cells,
            location_points={**previous.location_points, **points},
            source="nortantis_manifest",
            manifest_path=str(manifest_path or ""),
            revision=max(0, int(previous.revision)) + 1,
            updated_at=self._now(),
        )
        for name, cell in layout.location_cells.items():
            location = world_state.map_locations.get(name)
            if location is None:
                continue
            location.semantic_cell = cell
            location.x, location.y = self.cell_xy(cell)
        world_state.semantic_map = layout
        return layout

    def normalized_position(
        self,
        world_state: WorldState,
        location: MapLocation,
    ) -> tuple[float, float] | None:
        layout = self.view(world_state)
        actual_point = layout.location_points.get(location.name) or {}
        try:
            actual_x = float(actual_point.get("normalized_x"))
            actual_y = float(actual_point.get("normalized_y"))
        except (TypeError, ValueError):
            actual_x = actual_y = -1.0
        if 0.0 <= actual_x <= 1.0 and 0.0 <= actual_y <= 1.0:
            return round(actual_x, 4), round(actual_y, 4)
        cell = layout.location_cells.get(location.name, "")
        if not cell:
            return None
        column, row = self.cell_xy(cell)
        x_ratio = column / max(1, layout.grid_width - 1)
        y_ratio = row / max(1, layout.grid_height - 1)
        return (
            round(0.10 + x_ratio * 0.80, 4),
            round(0.14 + y_ratio * 0.72, 4),
        )

    def ascii_grid(
        self,
        world_state: WorldState,
        *,
        layout: SemanticMapLayout | None = None,
    ) -> str:
        layout = layout or self.view(world_state)
        placed_names = [
            name
            for name in sorted(layout.location_cells)
            if layout.location_cells.get(name)
        ]
        markers = {
            name: self._marker(index)
            for index, name in enumerate(placed_names)
        }
        cell_markers = {
            layout.location_cells[name]: marker
            for name, marker in markers.items()
        }
        header = "    " + " ".join(
            chr(ord("A") + column)
            for column in range(layout.grid_width)
        )
        lines = [header]
        for row, terrain_row in enumerate(layout.terrain_rows):
            rendered = [
                cell_markers.get(self.cell_name(column, row), terrain_row[column])
                for column in range(layout.grid_width)
            ]
            lines.append(f"{row + 1:02d}  " + " ".join(rendered))
        lines.append(
            "地形：" + " ".join(
                f"{symbol}={label}"
                for symbol, label in self.TERRAIN_LEGEND.items()
            )
        )
        if placed_names:
            lines.append(
                "地点：" + "；".join(
                    f"{markers[name]}={name}@{layout.location_cells[name]}"
                    for name in placed_names
                )
            )
        return "\n".join(lines)

    def terrain_at(self, layout: SemanticMapLayout, cell: str) -> str:
        column, row = self.cell_xy(cell)
        return layout.terrain_rows[row][column]

    def normalize_cell(self, value: str) -> str:
        compact = str(value or "").strip().upper().replace("-", "")
        match = self._CELL_PATTERN.fullmatch(compact)
        if match is None:
            return ""
        column = ord(match.group(1)) - ord("A")
        row = int(match.group(2)) - 1
        if not (0 <= column < self.GRID_WIDTH and 0 <= row < self.GRID_HEIGHT):
            return ""
        return self.cell_name(column, row)

    def cell_name(self, column: int, row: int) -> str:
        if not (
            0 <= int(column) < self.GRID_WIDTH
            and 0 <= int(row) < self.GRID_HEIGHT
        ):
            raise ValueError("网格坐标超出地图范围。")
        return f"{chr(ord('A') + int(column))}{int(row) + 1:02d}"

    def cell_xy(self, cell: str) -> tuple[int, int]:
        normalized = self.normalize_cell(cell)
        if not normalized:
            raise ValueError(f"无效地图网格：{cell}")
        return ord(normalized[0]) - ord("A"), int(normalized[1:]) - 1

    def cell_from_normalized(self, x: float, y: float) -> str:
        column = min(
            self.GRID_WIDTH - 1,
            max(0, round(float(x) * (self.GRID_WIDTH - 1))),
        )
        row = min(
            self.GRID_HEIGHT - 1,
            max(0, round(float(y) * (self.GRID_HEIGHT - 1))),
        )
        return self.cell_name(column, row)

    def _layout_from_latest_artifact(
        self,
        world_state: WorldState,
    ) -> SemanticMapLayout | None:
        for event in reversed(world_state.memory_events):
            if str(getattr(event, "kind", "") or "") != "world_map_visual":
                continue
            payload = dict(getattr(event, "payload", {}) or {})
            manifest_path = str(payload.get("manifest_path") or "").strip()
            manifest = self._read_json(manifest_path)
            if manifest:
                return self._layout_from_manifest_data(
                    world_state,
                    manifest,
                    manifest_path,
                )
            brief_path = str(payload.get("brief_path") or "").strip()
            brief = self._read_json(brief_path)
            if brief:
                return self._layout_from_legacy_brief(world_state, brief)
        return None

    def _layout_from_manifest_data(
        self,
        world_state: WorldState,
        manifest: dict[str, Any],
        manifest_path: str,
    ) -> SemanticMapLayout:
        rows = self._normalize_terrain_rows(
            list(manifest.get("terrain_rows") or [])
        )
        cells: dict[str, str] = {}
        points: dict[str, dict[str, Any]] = {}
        raw_locations = manifest.get("locations") or {}
        if isinstance(raw_locations, dict):
            for name, raw in raw_locations.items():
                if name not in world_state.map_locations or not isinstance(raw, dict):
                    continue
                cell = self.normalize_cell(str(raw.get("cell") or ""))
                if not cell:
                    continue
                cells[name] = cell
                points[name] = dict(raw)
        cells = self._deduplicate_location_cells(cells, points)
        return SemanticMapLayout(
            terrain_rows=rows,
            location_cells=cells,
            location_points=points,
            source="nortantis_manifest",
            manifest_path=manifest_path,
            revision=int(manifest.get("revision") or 0),
            updated_at=str(manifest.get("updated_at") or ""),
        )

    def _layout_from_legacy_brief(
        self,
        world_state: WorldState,
        brief: dict[str, Any],
    ) -> SemanticMapLayout:
        cells: dict[str, str] = {}
        points: dict[str, dict[str, Any]] = {}
        for label in list(brief.get("labels") or []):
            if not isinstance(label, dict):
                continue
            name = str(label.get("text") or "").strip()
            if name not in world_state.map_locations:
                continue
            try:
                x = float(label.get("x"))
                y = float(label.get("y"))
            except (TypeError, ValueError):
                continue
            cell = self.cell_from_normalized(x, y)
            cells[name] = cell
            points[name] = {
                "normalized_x": x,
                "normalized_y": y,
                "anchor_kind": "legacy_brief",
            }
        return SemanticMapLayout(
            terrain_rows=self._default_terrain_rows(),
            location_cells=cells,
            location_points=points,
            source="legacy_brief",
        )

    def _blank_layout(self) -> SemanticMapLayout:
        return SemanticMapLayout(
            grid_width=self.GRID_WIDTH,
            grid_height=self.GRID_HEIGHT,
            terrain_rows=self._default_terrain_rows(),
            source="semantic_planning_grid",
        )

    def _valid_layout(self, layout: SemanticMapLayout) -> bool:
        return bool(
            int(getattr(layout, "grid_width", 0) or 0) == self.GRID_WIDTH
            and int(getattr(layout, "grid_height", 0) or 0) == self.GRID_HEIGHT
            and len(getattr(layout, "terrain_rows", []) or []) == self.GRID_HEIGHT
        )

    def _normalize_terrain_rows(self, rows: list[object]) -> list[str]:
        allowed = set(self.TERRAIN_LEGEND)
        normalized: list[str] = []
        for raw in rows[: self.GRID_HEIGHT]:
            row = "".join(
                char if char in allowed else "."
                for char in str(raw or "")[: self.GRID_WIDTH]
            )
            normalized.append(row.ljust(self.GRID_WIDTH, "."))
        defaults = self._default_terrain_rows()
        while len(normalized) < self.GRID_HEIGHT:
            normalized.append(defaults[len(normalized)])
        return normalized

    def _default_terrain_rows(self) -> list[str]:
        rows = ["~" * self.GRID_WIDTH]
        rows.append("~" + "C" * (self.GRID_WIDTH - 2) + "~")
        for _ in range(self.GRID_HEIGHT - 4):
            rows.append("~C" + "." * (self.GRID_WIDTH - 4) + "C~")
        rows.append("~" + "C" * (self.GRID_WIDTH - 2) + "~")
        rows.append("~" * self.GRID_WIDTH)
        return rows

    def _terrain_score(
        self,
        location: MapLocation,
        symbol: str,
        *,
        source: str,
    ) -> tuple[float, str]:
        feature = str(location.feature_type or "").strip().lower()
        text = " ".join(
            [
                feature,
                str(location.terrain or ""),
                str(location.description or ""),
                *location.tags,
            ]
        ).lower()
        actual = source == "nortantis_manifest"
        water = symbol in {"~", "I", "L", "A"}
        if feature in {"country", "settlement", "fortress"}:
            if water:
                return (-100 if actual else -30), "国家与聚落应落在陆地"
            return 8, "符合陆上国家或聚落"
        if feature == "archipelago" or "群岛" in text:
            return (
                (11, "符合群岛水域")
                if symbol in {"~", "A", "C"}
                else (-100 if actual else -5, "等待渲染器塑造群岛地形")
            )
        if feature in {"inland_sea", "lake"} or any(
            token in text for token in ("内海", "湖泊", "湖")
        ):
            if symbol in {"I", "L"}:
                return 13, "位于内陆水域"
            if symbol == "~":
                return (2 if not actual else -8), "现有地貌更接近外海"
            return (5 if not actual else -4), "位于大陆内部，适合塑造内海或湖泊"
        if feature == "coast" or "海岸" in text:
            return (
                (12, "位于海岸")
                if symbol == "C"
                else (-3 if not actual else -12, "不在现有海岸带")
            )
        if feature == "forest" or "森林" in text:
            if symbol == "F":
                return 12, "位于森林地貌"
            return (5 if not water else -100), "可在此塑造森林"
        if feature == "mountain_range" or any(
            token in text for token in ("山脉", "高山", "山地")
        ):
            if symbol == "M":
                return 12, "位于山脉地貌"
            return (5 if not water else -100), "可在此塑造山脉"
        if water:
            return (-100 if actual else -20), "普通地点不宜落入水域"
        return 6, "符合陆地地貌"

    def _position_score(
        self,
        location: MapLocation,
        column: int,
        row: int,
        layout: SemanticMapLayout,
    ) -> tuple[float, str]:
        hint = str(location.position_hint or "").strip().lower()
        anchor = self._POSITION_ANCHORS.get(hint)
        if anchor is None:
            return 0, ""
        x = column / max(1, layout.grid_width - 1)
        y = row / max(1, layout.grid_height - 1)
        distance = ((x - anchor[0]) ** 2 + (y - anchor[1]) ** 2) ** 0.5
        return max(-8.0, 12.0 - distance * 28.0), f"贴合{hint}方位"

    def _relative_score(
        self,
        location: MapLocation,
        cell: str,
        reference_cell: str,
    ) -> tuple[float, str]:
        direction = str(location.relative_position or "").strip().lower()
        expected = self._DIRECTION_VECTORS.get(direction)
        if not reference_cell or expected is None:
            return 0, ""
        column, row = self.cell_xy(cell)
        ref_column, ref_row = self.cell_xy(reference_cell)
        dx = column - ref_column
        dy = row - ref_row
        if direction == "center":
            distance = (dx * dx + dy * dy) ** 0.5
            return 12 - distance * 4, "靠近参照地点"
        ex, ey = expected
        forward = dx * ex + dy * ey
        sideways = abs(dx * ey - dy * ex)
        if forward <= 0:
            return -100, "不符合相对方位"
        return 18 + min(forward, 5) - sideways * 1.5, (
            f"位于{location.relative_to}的{direction}方向"
        )

    def _nearest_occupied_distance(
        self,
        column: int,
        row: int,
        occupied: dict[str, str],
    ) -> float | None:
        distances: list[float] = []
        for cell in occupied:
            other_column, other_row = self.cell_xy(cell)
            distances.append(
                ((column - other_column) ** 2 + (row - other_row) ** 2) ** 0.5
            )
        return min(distances) if distances else None

    def _deduplicate_location_cells(
        self,
        cells: dict[str, str],
        points: dict[str, dict[str, Any]],
    ) -> dict[str, str]:
        """Keep the readable grid one-location-per-cell.

        Nortantis can place two distinct pixel anchors inside one coarse cell.
        Their exact coordinates remain in ``location_points``; only the
        symbolic marker moves to the nearest free grid cell.
        """

        result: dict[str, str] = {}
        occupied: set[str] = set()
        for name in sorted(cells):
            cell = self.normalize_cell(cells[name])
            if not cell:
                continue
            if cell not in occupied:
                result[name] = cell
                occupied.add(cell)
                continue
            point = points.get(name) or {}
            try:
                target_x = float(point.get("normalized_x"))
                target_y = float(point.get("normalized_y"))
                target_column = round(target_x * (self.GRID_WIDTH - 1))
                target_row = round(target_y * (self.GRID_HEIGHT - 1))
            except (TypeError, ValueError):
                target_column, target_row = self.cell_xy(cell)
            free_cells = [
                self.cell_name(column, row)
                for row in range(self.GRID_HEIGHT)
                for column in range(self.GRID_WIDTH)
                if self.cell_name(column, row) not in occupied
            ]
            if not free_cells:
                result[name] = cell
                continue
            replacement = min(
                free_cells,
                key=lambda candidate: (
                    (
                        self.cell_xy(candidate)[0] - target_column
                    )
                    ** 2
                    + (
                        self.cell_xy(candidate)[1] - target_row
                    )
                    ** 2,
                    candidate,
                ),
            )
            result[name] = replacement
            occupied.add(replacement)
        return result

    @staticmethod
    def _read_json(path_text: str) -> dict[str, Any] | None:
        if not path_text:
            return None
        path = Path(path_text).expanduser()
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _marker(index: int) -> str:
        alphabet = "123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        return alphabet[index] if index < len(alphabet) else "*"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
