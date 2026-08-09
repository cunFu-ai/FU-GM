#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from fu_gm.http_server import FUGMHttpService


def _contains_marker(text: object, markers: tuple[str, ...]) -> bool:
    clean = str(text or "")
    return any(marker in clean for marker in markers)


def _scrub_list(values: list[str], markers: tuple[str, ...]) -> int:
    before = len(values)
    values[:] = [value for value in values if not _contains_marker(value, markers)]
    return before - len(values)


def _scene_frames(app) -> list[object]:
    manager = app.scene_frame_manager
    candidates = [
        *manager.history,
        *manager.suspended_frames.values(),
        manager.current_frame,
    ]
    frames: list[object] = []
    seen: set[int] = set()
    for frame in candidates:
        if frame is None or id(frame) in seen:
            continue
        seen.add(id(frame))
        frames.append(frame)
    return frames


def _scrub_superseded_fact(app, markers: tuple[str, ...]) -> dict[str, int]:
    counts = {
        "subject_facts": 0,
        "scene_facts": 0,
        "scene_beats": 0,
        "pacing_consequences": 0,
        "pacing_images": 0,
        "pacing_scalar_fields": 0,
    }
    for subject, facts in list(app.world_state.subject_facts.items()):
        counts["subject_facts"] += _scrub_list(facts, markers)
        if not facts:
            app.world_state.subject_facts.pop(subject, None)

    for frame in _scene_frames(app):
        for field_name in ("committed_consequences", "established_facts", "public_facts"):
            counts["scene_facts"] += _scrub_list(getattr(frame, field_name), markers)
        counts["scene_beats"] += _scrub_list(frame.recent_beats, markers)

    state = app.story_arc_manager.state
    progress_rows = [state.current_session_progress, *state.session_progress_history]
    for progress in progress_rows:
        counts["pacing_consequences"] += _scrub_list(
            progress.concrete_consequences,
            markers,
        )
        counts["pacing_images"] += _scrub_list(progress.public_images, markers)
        for field_name in ("last_event", "memory_consequence", "memory_image"):
            if _contains_marker(getattr(progress, field_name), markers):
                setattr(progress, field_name, "")
                counts["pacing_scalar_fields"] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(
        description="纠正剧情物件最终状态，并清除已被取代的活动场景与节奏记忆。"
    )
    parser.add_argument(
        "--data-root",
        default=str(Path.home() / ".fu-gm" / "data" / "campaigns"),
    )
    parser.add_argument("--campaign-id", default="default")
    parser.add_argument("--item-name", required=True)
    parser.add_argument("--location", required=True, help="物件动作结束后的最终地点。")
    parser.add_argument("--actor", default="", help="用于审计记录的行动角色；默认沿用当前持有者。")
    parser.add_argument(
        "--obsolete",
        action="append",
        default=[],
        help="需要从活动事实和节奏记忆中清除的旧错误文本，可重复传入。",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    service = FUGMHttpService(data_root=args.data_root, use_llm=False)
    runtime = service._runtime(args.campaign_id)
    app = runtime.app
    item = app.world_state.find_story_item(name=args.item_name)
    if item is None:
        raise SystemExit(f"没有找到剧情物件【{args.item_name}】。")

    actor = str(args.actor or item.holder or "存档纠错").strip()
    scene_location = str(
        (app.scene_manager.current_scene.location if app.scene_manager.current_scene else "")
        or item.location
        or args.location
    ).strip()
    markers = tuple(str(value).strip() for value in args.obsolete if str(value).strip())
    preview = {
        "campaign_id": args.campaign_id,
        "item_id": item.item_id,
        "item_name": item.name,
        "before": {
            "holder": item.holder,
            "location": item.location,
            "status": item.status.value,
        },
        "after": {
            "holder": "",
            "location": args.location,
            "status": "placed",
        },
        "obsolete_markers": list(markers),
    }
    if args.dry_run:
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 0

    app.world_state.commit_story_item_action(
        operation="place",
        item_name=item.name,
        actor=actor,
        scene_location=scene_location,
        public_fact="",
        source="存档纠错:剧情物件最终状态",
        item_id=item.item_id,
        to_location=args.location,
        state_note=item.current_state,
    )
    scrubbed = _scrub_superseded_fact(app, markers)
    if app.scene_manager.current_scene is not None and actor in app.scene_manager.current_scene.participants:
        app.scene_manager.record_participant_activity(
            actor,
            f"将剧情物件【{item.name}】放置于【{item.location}】",
        )
    saved_path = service._autosave_campaign(runtime, args.campaign_id)
    preview["after"] = {
        "holder": item.holder,
        "location": item.location,
        "status": item.status.value,
    }
    preview["scrubbed"] = scrubbed
    preview["saved_path"] = saved_path
    print(json.dumps(preview, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
