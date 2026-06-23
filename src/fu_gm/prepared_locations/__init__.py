from __future__ import annotations

from fu_gm.prepared_locations.core import CORE_LOCATION_SEEDS
from fu_gm.prepared_locations.epic import EPIC_LOCATION_SEEDS
from fu_gm.prepared_locations.models import LocationStoryHook, PreparedLocationSeed
from fu_gm.prepared_locations.natural import NATURAL_LOCATION_SEEDS
from fu_gm.prepared_locations.techno import TECHNO_LOCATION_SEEDS


EXPANSION_LOCATION_SEEDS: tuple[PreparedLocationSeed, ...] = (
    *EPIC_LOCATION_SEEDS,
    *NATURAL_LOCATION_SEEDS,
    *TECHNO_LOCATION_SEEDS,
)

PREPARED_LOCATION_SEEDS: tuple[PreparedLocationSeed, ...] = (
    *CORE_LOCATION_SEEDS,
    *EXPANSION_LOCATION_SEEDS,
)

PREPARED_LOCATION_BY_NAME: dict[str, PreparedLocationSeed] = {
    seed.name: seed for seed in PREPARED_LOCATION_SEEDS
}

PREPARED_LOCATION_ALIASES: dict[str, str] = {
    "边境起始王国": "奥涅里亚",
    "第七采掘城": "七号采掘器",
    "灵魂网络中枢": "灵魂中枢",
}


def prepared_location_by_name(name: str) -> PreparedLocationSeed | None:
    normalized = str(name or "").strip()
    canonical = PREPARED_LOCATION_ALIASES.get(normalized, normalized)
    return PREPARED_LOCATION_BY_NAME.get(canonical)


__all__ = [
    "CORE_LOCATION_SEEDS",
    "EPIC_LOCATION_SEEDS",
    "EXPANSION_LOCATION_SEEDS",
    "LocationStoryHook",
    "NATURAL_LOCATION_SEEDS",
    "PREPARED_LOCATION_ALIASES",
    "PREPARED_LOCATION_BY_NAME",
    "PREPARED_LOCATION_SEEDS",
    "PreparedLocationSeed",
    "TECHNO_LOCATION_SEEDS",
    "prepared_location_by_name",
]
