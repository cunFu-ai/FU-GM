from __future__ import annotations

import json
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MapIconSpec:
    icon_id: str
    name_zh: str
    source_path: Path
    place_kind: str = ""
    preferred_terrain: tuple[str, ...] = ()
    default_scale: float = 1.0
    aspect_ratio: float = 1.0
    placement: str = "land"
    anchor_mode: str = "ground"

    @property
    def nortantis_icon_type(self) -> str:
        # FU-GM wonder/location icons are authored as semantic map decorations, not
        # Nortantis city markers. Keeping them out of IconType.cities prevents
        # Nortantis from deleting large/transparent custom icons when their bottom
        # content bounds touch water.
        return "decorations"


class MapIconRegistry:
    """Loads enabled FU-GM map icons without keyword-based inference."""

    def __init__(self, icons: tuple[MapIconSpec, ...] = ()) -> None:
        self.icons = icons
        self._by_id = {icon.icon_id: icon for icon in icons}
        self._by_name = {icon.name_zh: icon for icon in icons}

    def __bool__(self) -> bool:
        return bool(self.icons)

    @classmethod
    def from_root(cls, root: str | Path) -> "MapIconRegistry":
        root_path = Path(root)
        if not root_path.is_dir():
            return cls()

        icons: list[MapIconSpec] = []
        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        for catalog_path in sorted(root_path.glob("*/catalog.json")):
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            if catalog.get("enabled") is not True:
                continue
            for raw_icon in catalog.get("icons", []):
                icon_id = str(raw_icon.get("icon_id", "")).strip()
                name_zh = str(raw_icon.get("name_zh", "")).strip()
                source_path = catalog_path.parent / str(raw_icon.get("file", ""))
                if not icon_id or not name_zh or not source_path.is_file():
                    raise ValueError(f"Invalid enabled map icon in {catalog_path}: {raw_icon!r}")
                if icon_id in seen_ids:
                    raise ValueError(f"Duplicate enabled map icon id: {icon_id}")
                if name_zh in seen_names:
                    raise ValueError(f"Duplicate enabled map icon name: {name_zh}")
                seen_ids.add(icon_id)
                seen_names.add(name_zh)
                icons.append(
                    MapIconSpec(
                        icon_id=icon_id,
                        name_zh=name_zh,
                        source_path=source_path,
                        place_kind=str(raw_icon.get("place_kind", "")).strip(),
                        preferred_terrain=tuple(str(item) for item in raw_icon.get("preferred_terrain", [])),
                        default_scale=max(0.1, float(raw_icon.get("default_scale", 1.0))),
                        aspect_ratio=cls._png_aspect_ratio(source_path),
                        placement=cls._placement(raw_icon),
                        anchor_mode=cls._anchor_mode(raw_icon),
                    )
                )
        return cls(tuple(icons))

    @staticmethod
    def _png_aspect_ratio(path: Path) -> float:
        with path.open("rb") as image_file:
            header = image_file.read(24)
        if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
            return 1.0
        width, height = struct.unpack(">II", header[16:24])
        return max(0.1, height / max(1, width))

    @staticmethod
    def _placement(raw_icon: dict) -> str:
        explicit = str(raw_icon.get("placement", "")).strip().lower()
        if explicit in {"land", "island", "ocean"}:
            return explicit
        place_kind = str(raw_icon.get("place_kind", "")).strip().lower()
        if place_kind == "world_wonder_island":
            return "island"
        if place_kind in {"prepared_sea", "world_wonder_undersea"}:
            return "ocean"
        return "land"

    @classmethod
    def _anchor_mode(cls, raw_icon: dict) -> str:
        explicit = str(raw_icon.get("anchor_mode", "")).strip().lower()
        if explicit in {"ground", "center"}:
            return explicit
        return "center" if cls._placement(raw_icon) in {"island", "ocean"} else "ground"

    def resolve(self, *, icon_id: str = "", semantic_name: str = "") -> MapIconSpec | None:
        """Resolve only a persisted id or an exact semantic display name."""

        normalized_id = str(icon_id or "").strip()
        if normalized_id:
            return self._by_id.get(normalized_id)
        normalized_name = str(semantic_name or "").strip()
        if normalized_name:
            return self._by_name.get(normalized_name)
        return None

    def materialize_custom_pack(
        self,
        custom_images_root: str | Path,
        *,
        group_id: str,
        encoded_width: int,
    ) -> Path:
        """Expose enabled candidates using Nortantis' custom image folder layout."""

        width = max(1, int(encoded_width))
        last_target_dir = Path(custom_images_root)
        for icon in self.icons:
            target_dir = Path(custom_images_root) / icon.nortantis_icon_type / group_id
            last_target_dir = target_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{icon.icon_id} width={width}{icon.source_path.suffix.lower()}"
            for icon_type in ("cities", "decorations"):
                stale_dir = Path(custom_images_root) / icon_type / group_id
                if not stale_dir.is_dir():
                    continue
                for stale in stale_dir.glob(f"{icon.icon_id} width=*"):
                    if stale.resolve() != target.resolve():
                        stale.unlink()
            if not target.exists() or target.read_bytes() != icon.source_path.read_bytes():
                shutil.copy2(icon.source_path, target)
        return last_target_dir
