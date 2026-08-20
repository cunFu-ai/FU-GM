from __future__ import annotations

import argparse
import json
import re
import shutil
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


KINGDOM_TOPIC = "kingdom_contributions"
_EXPLICIT_KINGDOM_SKIP_RE = re.compile(
    r"(?:国家|王国|政治共同体).{0,12}(?:跳过|不贡献|没想法|没有想法|想不到)|"
    r"(?:跳过|不贡献|没想法|没有想法|想不到).{0,12}(?:国家|王国|政治共同体)"
)


def _explicitly_skipped_kingdom(participant: dict[str, Any]) -> bool:
    return any(
        _EXPLICIT_KINGDOM_SKIP_RE.search(str(item or ""))
        for item in list(participant.get("contributions") or [])
    )


def migrate_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    migrated = deepcopy(payload)
    session_zero = migrated.get("session_zero")
    if not isinstance(session_zero, dict):
        return migrated, []
    session_world = session_zero.get("world")
    if not isinstance(session_world, dict):
        return migrated, []

    projections: list[tuple[str, dict[str, Any]]] = [
        ("session_zero.world", session_world)
    ]
    world_state = migrated.get("world_state")
    if isinstance(world_state, dict):
        world_profile = world_state.get("world_profile")
        if isinstance(world_profile, dict):
            projections.append(("world_state.world_profile", world_profile))

    kingdom_names = {
        str(name or "").strip()
        for _projection_name, projection in projections
        for name in (
            projection.get("kingdoms")
            if isinstance(projection.get("kingdoms"), dict)
            else {}
        )
        if str(name or "").strip()
    }

    participants = {
        str(item.get("name") or "").strip(): item
        for item in list(session_zero.get("participants") or [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    changes_by_player: dict[str, dict[str, Any]] = {}
    for projection_name, projection in projections:
        contributors = projection.get("kingdom_contributors")
        if not isinstance(contributors, dict):
            continue
        cleaned: dict[str, list[str]] = {}
        for raw_player, raw_values in contributors.items():
            player = str(raw_player or "").strip()
            values = [
                str(value or "").strip()
                for value in list(raw_values or [])
                if str(value or "").strip()
            ]
            valid = [value for value in values if value in kingdom_names]
            removed = [value for value in values if value not in kingdom_names]
            if valid:
                cleaned[player] = valid
            if not removed:
                continue
            change = changes_by_player.setdefault(
                player,
                {
                    "player": player,
                    "removed_non_kingdom_values": [],
                    "kept_kingdom_values": [],
                    "projections_repaired": [],
                },
            )
            change["removed_non_kingdom_values"] = list(
                dict.fromkeys(
                    [*change["removed_non_kingdom_values"], *removed]
                )
            )
            change["kept_kingdom_values"] = list(
                dict.fromkeys([*change["kept_kingdom_values"], *valid])
            )
            change["projections_repaired"].append(projection_name)
        projection["kingdom_contributors"] = cleaned

    changes = list(changes_by_player.values())
    for change in changes:
        player = str(change["player"])
        participant = participants.get(player)
        preserved_skip = bool(
            participant is not None and _explicitly_skipped_kingdom(participant)
        )
        completion_removed = False
        if (
            participant is not None
            and not change["kept_kingdom_values"]
            and not preserved_skip
        ):
            topics = [
                str(item or "")
                for item in list(participant.get("answered_topics") or [])
            ]
            if KINGDOM_TOPIC in topics:
                participant["answered_topics"] = [
                    item for item in topics if item != KINGDOM_TOPIC
                ]
                completion_removed = True
        change["explicit_skip_preserved"] = preserved_skip
        change["completion_removed"] = completion_removed
    return migrated, changes


def candidate_files(data_root: Path) -> list[Path]:
    campaigns = data_root / "campaigns"
    return sorted(
        {
            *campaigns.glob("*/snapshot.json"),
            *campaigns.glob("*/saves/*.json"),
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="修复被地图地点错误满足的第零章国家贡献。"
    )
    parser.add_argument("data_root", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report: list[dict[str, Any]] = []
    for path in candidate_files(args.data_root):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            report.append({"file": str(path), "error": str(exc)})
            continue
        migrated, changes = migrate_payload(payload)
        if not changes:
            continue
        item: dict[str, Any] = {"file": str(path), "changes": changes}
        if args.apply:
            backup = path.with_name(
                f"{path.stem}.before-country-contribution-fix-{stamp}{path.suffix}"
            )
            shutil.copy2(path, backup)
            path.write_text(
                json.dumps(migrated, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            item["backup"] = str(backup)
        report.append(item)

    print(json.dumps({"applied": args.apply, "files": report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
