from __future__ import annotations

import json
import os
import shutil
import tempfile
import types
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

from fu_gm import models
from fu_gm.campaign_paths import safe_campaign_path_segment
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.clock_lifecycle_coordinator import ClockLifecycleCoordinator
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.dungeon_manager import DungeonManager
from fu_gm.components.project_manager import ProjectManager
from fu_gm.components.progression_manager import ProgressionManager
from fu_gm.components.ritual_manager import RitualManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.scene_frame_manager import SceneFrame, SceneFrameManager
from fu_gm.components.story_arc_manager import StoryArcManager
from fu_gm.components.travel_manager import TravelManager
from fu_gm.components.world_map_manager import WorldMapManager
from fu_gm.components.world_state import WorldState
from fu_gm.models import (
    Affinity,
    CampaignArcState,
    Character,
    ChapterPackage,
    Clock,
    ConflictState,
    DecisionWindow,
    DungeonDesignBrief,
    DungeonExploreMode,
    DungeonMap,
    DungeonState,
    EffectTiming,
    GMSecret,
    IconicElementState,
    MapLocation,
    MapRouteEdge,
    SemanticMapLayout,
    MemoryEvent,
    MemoryRelation,
    NPCPersona,
    NPCCombatBlueprint,
    PartySheet,
    PendingCheckBatch,
    PersistentChange,
    ProjectState,
    RitualPlan,
    SceneRecord,
    SessionZeroState,
    StoryItem,
    StatusEffect,
    TimedEffect,
    JourneyProgress,
    JourneyResult,
    TravelRouteRecord,
    TransparencyAuditEntry,
    WorldRoutePlan,
    WorldCreationProfile,
    WorldSheet,
)
from fu_gm.npc_identity import is_null_npc_target, normalize_npc_target_label


class CampaignMemoryStore:
    """本地长期记忆后端。

    这一层刻意不绑定外部向量库或图数据库；它负责保存权威状态和可审计事件。
    后续如果接 Mem0、Graphiti 或 SQLite，只需要实现相同的保存/加载边界。
    """

    SCHEMA_VERSION = 1

    def __init__(self, root: str | Path = "data/campaigns") -> None:
        self.root = Path(root)

    def save_campaign(
        self,
        campaign_id: str,
        *,
        world_state: WorldState,
        character_manager: CharacterManager,
        clock_manager: ClockManager,
        conflict_manager: ConflictManager,
        scene_manager: SceneManager | None = None,
        scene_frame_manager: SceneFrameManager | None = None,
        ritual_manager: RitualManager | None = None,
        project_manager: ProjectManager | None = None,
        story_arc_manager: StoryArcManager | None = None,
        hero_log_manager: Any | None = None,
        ally_npc_manager: Any | None = None,
        session_ledger: Any | None = None,
        session_zero_manager: Any | None = None,
        travel_manager: TravelManager | None = None,
        dungeon_manager: DungeonManager | None = None,
        world_map_manager: WorldMapManager | None = None,
        rules_engine: RulesEngine | None = None,
        progression_manager: ProgressionManager | None = None,
        slot: str | None = None,
    ) -> Path:
        campaign_dir = self._campaign_dir(campaign_id)
        campaign_dir.mkdir(parents=True, exist_ok=True)
        snapshot = self.build_snapshot(
            campaign_id,
            world_state=world_state,
            character_manager=character_manager,
            clock_manager=clock_manager,
            conflict_manager=conflict_manager,
            scene_manager=scene_manager,
            scene_frame_manager=scene_frame_manager,
            ritual_manager=ritual_manager,
            project_manager=project_manager,
            story_arc_manager=story_arc_manager,
            hero_log_manager=hero_log_manager,
            ally_npc_manager=ally_npc_manager,
            session_ledger=session_ledger,
            session_zero_manager=session_zero_manager,
            travel_manager=travel_manager,
            dungeon_manager=dungeon_manager,
            world_map_manager=world_map_manager,
            rules_engine=rules_engine,
            progression_manager=progression_manager,
        )
        snapshot_path = campaign_dir / "snapshot.json"
        self._atomic_write_text(
            snapshot_path,
            json.dumps(snapshot, ensure_ascii=False, indent=2),
        )
        return_path = snapshot_path
        if slot:
            save_dir = campaign_dir / "saves"
            save_dir.mkdir(parents=True, exist_ok=True)
            return_path = save_dir / f"{self._clean_name(slot)}.json"
            self._atomic_write_text(
                return_path,
                json.dumps(snapshot, ensure_ascii=False, indent=2),
            )

        events_path = campaign_dir / "events.jsonl"
        events_text = "\n".join(json.dumps(self._encode(event), ensure_ascii=False) for event in world_state.memory_events)
        self._atomic_write_text(
            events_path,
            events_text + ("\n" if events_text else ""),
        )
        return return_path

    def load_campaign(
        self,
        campaign_id: str,
        *,
        world_state: WorldState,
        character_manager: CharacterManager,
        clock_manager: ClockManager,
        conflict_manager: ConflictManager,
        scene_manager: SceneManager | None = None,
        scene_frame_manager: SceneFrameManager | None = None,
        ritual_manager: RitualManager | None = None,
        project_manager: ProjectManager | None = None,
        story_arc_manager: StoryArcManager | None = None,
        hero_log_manager: Any | None = None,
        ally_npc_manager: Any | None = None,
        session_ledger: Any | None = None,
        session_zero_manager: Any | None = None,
        travel_manager: TravelManager | None = None,
        dungeon_manager: DungeonManager | None = None,
        world_map_manager: WorldMapManager | None = None,
        rules_engine: RulesEngine | None = None,
        progression_manager: ProgressionManager | None = None,
        slot: str | None = None,
    ) -> dict[str, Any]:
        snapshot_path = self._snapshot_path(campaign_id, slot=slot)
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.apply_snapshot(
            snapshot,
            world_state=world_state,
            character_manager=character_manager,
            clock_manager=clock_manager,
            conflict_manager=conflict_manager,
            scene_manager=scene_manager,
            scene_frame_manager=scene_frame_manager,
            ritual_manager=ritual_manager,
            project_manager=project_manager,
            story_arc_manager=story_arc_manager,
            hero_log_manager=hero_log_manager,
            ally_npc_manager=ally_npc_manager,
            session_ledger=session_ledger,
            session_zero_manager=session_zero_manager,
            travel_manager=travel_manager,
            dungeon_manager=dungeon_manager,
            world_map_manager=world_map_manager,
            rules_engine=rules_engine,
            progression_manager=progression_manager,
        )
        return snapshot

    def read_snapshot(
        self,
        campaign_id: str,
        *,
        slot: str | None = None,
    ) -> dict[str, Any]:
        """Read a persisted snapshot without applying it to a live runtime."""

        snapshot_path = self._snapshot_path(campaign_id, slot=slot)
        return json.loads(snapshot_path.read_text(encoding="utf-8"))

    def snapshot_exists(self, campaign_id: str, *, slot: str | None = None) -> bool:
        return self._snapshot_path(campaign_id, slot=slot).exists()

    def list_campaigns(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        campaigns: list[dict[str, Any]] = []
        for campaign_dir in sorted(path for path in self.root.iterdir() if path.is_dir()):
            snapshot_path = campaign_dir / "snapshot.json"
            slots = self.list_save_slots(campaign_dir.name)
            if snapshot_path.exists() or slots:
                campaigns.append(
                    {
                        "campaign_id": campaign_dir.name,
                        "has_latest_snapshot": snapshot_path.exists(),
                        "slots": [slot["slot"] for slot in slots],
                        "updated_at": self._snapshot_saved_at(snapshot_path),
                    }
                )
        return campaigns

    def list_save_slots(self, campaign_id: str) -> list[dict[str, str]]:
        save_dir = self._campaign_dir(campaign_id) / "saves"
        if not save_dir.exists():
            return []
        slots: list[dict[str, str]] = []
        for path in sorted(save_dir.glob("*.json")):
            slots.append(
                {
                    "slot": path.stem,
                    "path": str(path),
                    "saved_at": self._snapshot_saved_at(path),
                }
            )
        return slots

    def delete_save(self, campaign_id: str, *, slot: str | None = None) -> dict[str, Any]:
        """删除最新快照或一个命名存档槽。

        不传 slot 时只删除 snapshot.json；不会删除 sessions/、events.jsonl 或命名槽。
        删除整场战役请使用 delete_campaign，避免误操作把日志一起清掉。
        """

        clean_slot = self._clean_name(slot) if slot else ""
        if slot and not clean_slot:
            raise ValueError("存档槽名称不能为空。")
        path = self._snapshot_path(campaign_id, slot=clean_slot or None)
        existed = path.exists()
        if existed:
            path.unlink()
        return {
            "campaign_id": campaign_id,
            "slot": clean_slot,
            "deleted": existed,
            "path": str(path),
        }

    def delete_campaign(self, campaign_id: str) -> dict[str, Any]:
        """删除整个战役目录，包括快照、命名存档、日志和故事记忆。"""

        campaign_dir = self._campaign_dir(campaign_id)
        existed = campaign_dir.exists()
        if existed:
            shutil.rmtree(campaign_dir)
        return {
            "campaign_id": campaign_id,
            "deleted": existed,
            "path": str(campaign_dir),
        }

    def build_snapshot(
        self,
        campaign_id: str,
        *,
        world_state: WorldState,
        character_manager: CharacterManager,
        clock_manager: ClockManager,
        conflict_manager: ConflictManager,
        scene_manager: SceneManager | None = None,
        scene_frame_manager: SceneFrameManager | None = None,
        ritual_manager: RitualManager | None = None,
        project_manager: ProjectManager | None = None,
        story_arc_manager: StoryArcManager | None = None,
        hero_log_manager: Any | None = None,
        ally_npc_manager: Any | None = None,
        session_ledger: Any | None = None,
        session_zero_manager: Any | None = None,
        travel_manager: TravelManager | None = None,
        dungeon_manager: DungeonManager | None = None,
        world_map_manager: WorldMapManager | None = None,
        rules_engine: RulesEngine | None = None,
        progression_manager: ProgressionManager | None = None,
        lossless: bool = False,
    ) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "campaign_id": campaign_id,
            "saved_at": self._now(),
            "world_state": self._world_state_to_snapshot(
                world_state,
                lossless=lossless,
            ),
            "characters": self._encode(character_manager.all()),
            "clocks": self._encode(list(clock_manager._clocks.values())),
            "archived_clocks": self._encode(clock_manager.archived()),
            "conflict_state": self._encode(conflict_manager.state),
            "scene_manager": self._scene_manager_to_snapshot(scene_manager),
            "scene_frame_manager": self._scene_frame_manager_to_snapshot(
                scene_frame_manager,
                lossless=lossless,
            ),
            "rituals": {
                "active_rituals": self._encode(list(ritual_manager.active_rituals.values())) if ritual_manager else [],
            },
            "projects": {
                "projects": self._encode(list(project_manager.projects.values())) if project_manager else [],
            },
            "story_arc": self._encode(story_arc_manager.state) if story_arc_manager else None,
            "hero_logs": hero_log_manager.to_snapshot() if hero_log_manager else {},
            "ally_npcs": ally_npc_manager.to_snapshot() if ally_npc_manager else {},
            "session_ledger": session_ledger.to_snapshot() if session_ledger else {},
            "session_zero": (
                self._encode(session_zero_manager.state)
                if session_zero_manager is not None
                else None
            ),
            "travel_runtime": {
                "last_journey": self._encode(travel_manager.last_journey)
                if travel_manager is not None
                else None,
                "history": self._encode(travel_manager.history)
                if travel_manager is not None
                else [],
                "routes": self._encode(travel_manager.routes)
                if travel_manager is not None
                else {},
                "owned_transports": self._encode(travel_manager.owned_transports)
                if travel_manager is not None
                else [],
                "active_journey": self._encode(travel_manager.active_journey)
                if travel_manager is not None
                else None,
                "interrupted_journeys": self._encode(
                    travel_manager.interrupted_journeys
                )
                if travel_manager is not None
                else [],
            },
            "dungeon_runtime": {
                "state": self._encode(dungeon_manager.state)
                if dungeon_manager is not None
                else None,
                "history": self._encode(dungeon_manager.history)
                if dungeon_manager is not None
                else [],
                "design_history": self._encode(dungeon_manager.design_history)
                if dungeon_manager is not None
                else [],
                "maps": self._encode(dungeon_manager.maps)
                if dungeon_manager is not None
                else {},
            },
            "world_map_runtime": {
                "route_plans": self._encode(world_map_manager.route_plans)
                if world_map_manager is not None
                else [],
            },
            "rules_runtime": {
                "rng_state": self._encode(rules_engine._rng.getstate())
                if rules_engine is not None
                else None,
            },
            "progression_runtime": (
                progression_manager.to_snapshot()
                if progression_manager is not None
                else {}
            ),
        }

    def apply_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        world_state: WorldState,
        character_manager: CharacterManager,
        clock_manager: ClockManager,
        conflict_manager: ConflictManager,
        scene_manager: SceneManager | None = None,
        scene_frame_manager: SceneFrameManager | None = None,
        ritual_manager: RitualManager | None = None,
        project_manager: ProjectManager | None = None,
        story_arc_manager: StoryArcManager | None = None,
        hero_log_manager: Any | None = None,
        ally_npc_manager: Any | None = None,
        session_ledger: Any | None = None,
        session_zero_manager: Any | None = None,
        travel_manager: TravelManager | None = None,
        dungeon_manager: DungeonManager | None = None,
        world_map_manager: WorldMapManager | None = None,
        rules_engine: RulesEngine | None = None,
        progression_manager: ProgressionManager | None = None,
    ) -> None:
        if snapshot.get("schema_version") != self.SCHEMA_VERSION:
            raise ValueError(f"不支持的记忆快照版本：{snapshot.get('schema_version')}")

        self._apply_world_state_snapshot(world_state, snapshot["world_state"])

        character_manager._characters = {}
        for character_data in snapshot.get("characters", []):
            character = self._decode_dataclass(Character, character_data)
            character_manager.add(character)

        clock_manager._clocks = {}
        clock_manager._archived_clocks = []
        for clock_data in snapshot.get("clocks", []):
            clock = self._decode_dataclass(Clock, clock_data)
            clock_manager.add(clock)
        for clock_data in snapshot.get("archived_clocks", []):
            clock_manager._archived_clocks.append(self._decode_dataclass(Clock, clock_data))
        ClockLifecycleCoordinator(clock_manager).reconcile_fulfilled()

        conflict_manager.state = self._decode_dataclass(ConflictState, snapshot.get("conflict_state", {}))

        coalesced_active_scenes: dict[str, list[str]] = {}
        if scene_manager is not None:
            scene_data = snapshot.get("scene_manager", {})
            current = scene_data.get("current_scene")
            scene_manager.current_scene = self._decode_dataclass(SceneRecord, current) if current else None
            scene_manager.history = [self._decode_dataclass(SceneRecord, item) for item in scene_data.get("history", [])]
            scene_manager.suspended_scenes = [
                self._decode_dataclass(SceneRecord, item)
                for item in scene_data.get("suspended_scenes", [])
                if isinstance(item, dict)
            ]
            if scene_manager.current_scene is not None:
                scene_manager.current_scene.participants = scene_manager.normalize_participants(
                    scene_manager.current_scene.participants
                )
                scene_manager.current_scene.participant_locations = {
                    str(name): str(location or "")
                    for name, location in dict(
                        scene_manager.current_scene.participant_locations or {}
                    ).items()
                    if str(name or "").strip()
                }
                scene_manager.current_scene.participant_positions = {
                    str(name): str(position or "")
                    for name, position in dict(
                        scene_manager.current_scene.participant_positions or {}
                    ).items()
                    if str(name or "").strip()
                }
            for archived_scene in scene_manager.history:
                archived_scene.participants = scene_manager.normalize_participants(
                    archived_scene.participants
                )
            for suspended_scene in scene_manager.suspended_scenes:
                suspended_scene.active = True
                suspended_scene.participants = scene_manager.normalize_participants(
                    suspended_scene.participants
                )
                suspended_scene.participant_locations = {
                    str(name): str(location or "")
                    for name, location in dict(
                        suspended_scene.participant_locations or {}
                    ).items()
                    if str(name or "").strip()
                }
                suspended_scene.participant_positions = {
                    str(name): str(position or "")
                    for name, position in dict(
                        suspended_scene.participant_positions or {}
                    ).items()
                    if str(name or "").strip()
                }
            scene_manager.free_action_round_number = max(
                1,
                int(scene_data.get("free_action_round_number", 1) or 1),
            )
            scene_manager.free_action_round_required_actors = list(
                scene_data.get("free_action_round_required_actors", []) or []
            )
            scene_manager.free_action_round_acted_actors = list(
                scene_data.get("free_action_round_acted_actors", []) or []
            )
            scene_manager.free_action_round_auto_advance_skip_names = list(
                scene_data.get("free_action_round_auto_advance_skip_names", []) or []
            )
            scene_manager.actor_locations = {
                str(name): str(location or "")
                for name, location in dict(
                    scene_data.get("actor_locations", {}) or {}
                ).items()
                if str(name or "").strip()
            }
            scene_manager.actor_positions = {
                str(name): str(position or "")
                for name, position in dict(
                    scene_data.get("actor_positions", {}) or {}
                ).items()
                if str(name or "").strip()
            }
            raw_active_scenes = [
                (scene_manager.current_scene, current),
                *zip(
                    scene_manager.suspended_scenes,
                    [
                        item
                        for item in scene_data.get("suspended_scenes", [])
                        if isinstance(item, dict)
                    ],
                ),
            ]
            for active_scene, raw_scene in raw_active_scenes:
                if active_scene is None:
                    continue
                legacy_positions = not isinstance(raw_scene, dict) or (
                    "participant_positions" not in raw_scene
                    and "actor_positions" not in scene_data
                )
                for name in active_scene.participants:
                    recorded = str(
                        active_scene.participant_locations.get(name)
                        or active_scene.location
                        or ""
                    ).strip()
                    scene_location = str(active_scene.location or recorded).strip()
                    if (
                        legacy_positions
                        and recorded
                        and scene_location
                        and not scene_manager._same_exact_location(recorded, scene_location)
                    ):
                        active_scene.participant_positions[name] = recorded
                        scene_manager.actor_positions[name] = recorded
                        recorded = scene_location
                    active_scene.participant_locations[name] = recorded
                    scene_manager.actor_locations[name] = recorded
            if scene_manager.current_scene is not None:
                for name in scene_manager.current_scene.participants:
                    location = str(
                        scene_manager.current_scene.participant_locations.get(name)
                        or scene_manager.current_scene.location
                        or ""
                    ).strip()
                    scene_manager.current_scene.participant_locations[name] = location
                    scene_manager.actor_locations[name] = location
            scene_manager._scene_counter = max(
                len(scene_manager.history)
                + len(scene_manager.suspended_scenes)
                + (1 if scene_manager.current_scene else 0),
                scene_manager._scene_counter,
            )
            coalesced_active_scenes = (
                scene_manager.coalesce_active_scenes_by_exact_location()
            )
            if scene_manager.current_scene is not None:
                clock_manager.begin_scene(scene_manager.current_scene.scene_id or scene_manager.current_scene.name)

        if scene_frame_manager is not None:
            frame_data = snapshot.get("scene_frame_manager", {})
            current_frame = frame_data.get("current_frame") if isinstance(frame_data, dict) else None
            history = frame_data.get("history", []) if isinstance(frame_data, dict) else []
            scene_frame_manager.current_frame = (
                self._decode_dataclass(SceneFrame, current_frame) if current_frame else None
            )
            suspended_frames = (
                frame_data.get("suspended_frames", {})
                if isinstance(frame_data, dict)
                else {}
            )
            scene_frame_manager.suspended_frames = {
                str(key): self._decode_dataclass(SceneFrame, value)
                for key, value in dict(suspended_frames or {}).items()
                if str(key or "").strip() and isinstance(value, dict)
            }
            scene_frame_manager.history = [
                self._decode_dataclass(SceneFrame, item)
                for item in history
                if isinstance(item, dict)
            ]
            scene_frame_manager.normalize_loaded_state()
            for primary_scene_id, duplicate_scene_ids in coalesced_active_scenes.items():
                scene_frame_manager.coalesce_suspended_frames(
                    primary_scene_id,
                    duplicate_scene_ids,
                )

        self._repair_legacy_null_npc_targets(
            world_state=world_state,
            scene_manager=scene_manager,
            scene_frame_manager=scene_frame_manager,
        )
        if scene_frame_manager is not None:
            # Identity repair can turn two formerly different-looking NPC
            # records into the same exchange. Compact once more afterwards.
            scene_frame_manager.normalize_loaded_state()

        if ritual_manager is not None:
            ritual_manager.active_rituals = {}
            for plan_data in snapshot.get("rituals", {}).get("active_rituals", []):
                plan = self._decode_dataclass(RitualPlan, plan_data)
                ritual_manager.active_rituals[plan.clock_name] = plan

        if project_manager is not None:
            project_manager.projects = {}
            for project_data in snapshot.get("projects", {}).get("projects", []):
                project = self._decode_dataclass(ProjectState, project_data)
                project_manager.projects[project.name] = project

        if story_arc_manager is not None:
            arc_data = snapshot.get("story_arc")
            if arc_data:
                story_arc_manager.state = self._decode_dataclass(CampaignArcState, arc_data)
            story_arc_manager.sync_from_world_profile()

        if hero_log_manager is not None:
            hero_log_manager.apply_snapshot(snapshot.get("hero_logs", {}))

        if ally_npc_manager is not None:
            ally_npc_manager.apply_snapshot(snapshot.get("ally_npcs", {}))

        if session_ledger is not None:
            session_ledger.apply_snapshot(snapshot.get("session_ledger", {}))

        if session_zero_manager is not None:
            session_zero_data = snapshot.get("session_zero")
            if isinstance(session_zero_data, dict):
                session_zero_manager.state = self._decode_dataclass(
                    SessionZeroState,
                    session_zero_data,
                )
                session_zero_manager.state.world = world_state.world_profile
            else:
                # Legacy snapshots persisted the shared world profile but not
                # the Session 0 workflow state. Recover only when the saved
                # scene itself proves that Session 0 was still active.
                session_zero_manager.state.world = world_state.world_profile
                current_scene = scene_manager.current_scene if scene_manager else None
                if current_scene is not None and current_scene.scene_type.value == "session_zero":
                    session_zero_manager.state.active = True
            session_zero_manager.world_state = world_state
            if session_zero_manager.state.active:
                session_zero_manager.refresh_stage_from_state()

        if travel_manager is not None:
            travel_data = snapshot.get("travel_runtime", {})
            if not isinstance(travel_data, dict):
                travel_data = {}
            last_journey = travel_data.get("last_journey")
            travel_manager.last_journey = (
                self._decode_dataclass(JourneyResult, last_journey)
                if isinstance(last_journey, dict)
                else None
            )
            travel_manager.history = [
                self._decode_dataclass(JourneyResult, item)
                for item in travel_data.get("history", [])
                if isinstance(item, dict)
            ]
            travel_manager.routes = {
                str(key): self._decode_dataclass(TravelRouteRecord, value)
                for key, value in dict(travel_data.get("routes", {}) or {}).items()
                if isinstance(value, dict)
            }
            travel_manager.owned_transports = {
                str(item)
                for item in travel_data.get("owned_transports", [])
                if str(item or "").strip()
            }
            active_journey = travel_data.get("active_journey")
            travel_manager.active_journey = (
                self._decode_dataclass(JourneyProgress, active_journey)
                if isinstance(active_journey, dict)
                else None
            )
            travel_manager.interrupted_journeys = [
                self._decode_dataclass(JourneyProgress, item)
                for item in travel_data.get("interrupted_journeys", [])
                if isinstance(item, dict)
            ]

        if dungeon_manager is not None:
            dungeon_data = snapshot.get("dungeon_runtime", {})
            if not isinstance(dungeon_data, dict):
                dungeon_data = {}
            active_state = dungeon_data.get("state")
            dungeon_manager.state = (
                self._decode_dataclass(DungeonState, active_state)
                if isinstance(active_state, dict)
                else DungeonState(name="", mode=DungeonExploreMode.SCENE)
            )
            dungeon_manager.history = [
                self._decode_dataclass(DungeonState, item)
                for item in dungeon_data.get("history", [])
                if isinstance(item, dict)
            ]
            dungeon_manager.design_history = [
                self._decode_dataclass(DungeonDesignBrief, item)
                for item in dungeon_data.get("design_history", [])
                if isinstance(item, dict)
            ]
            dungeon_manager.maps = {
                str(key): self._decode_dataclass(DungeonMap, value)
                for key, value in dict(dungeon_data.get("maps", {}) or {}).items()
                if isinstance(value, dict)
            }

        if world_map_manager is not None:
            map_data = snapshot.get("world_map_runtime", {})
            if not isinstance(map_data, dict):
                map_data = {}
            world_map_manager.route_plans = [
                self._decode_dataclass(WorldRoutePlan, item)
                for item in map_data.get("route_plans", [])
                if isinstance(item, dict)
            ]

        if rules_engine is not None:
            rules_data = snapshot.get("rules_runtime", {})
            rng_state = (
                rules_data.get("rng_state")
                if isinstance(rules_data, dict)
                else None
            )
            if rng_state is not None:
                rules_engine._rng.setstate(self._nested_tuple(rng_state))

        if progression_manager is not None:
            progression_manager.apply_snapshot(
                snapshot.get("progression_runtime", {})
            )

    @staticmethod
    def _repair_legacy_null_npc_targets(
        *,
        world_state: WorldState,
        scene_manager: SceneManager | None,
        scene_frame_manager: SceneFrameManager | None,
    ) -> None:
        """Remove model null sentinels that older builds persisted as NPCs."""

        frames = (
            [*scene_frame_manager.history, scene_frame_manager.current_frame]
            if scene_frame_manager is not None
            else []
        )
        frames = [frame for frame in frames if frame is not None]

        replacement = ""
        for frame in reversed(frames):
            candidate = normalize_npc_target_label(frame.last_npc_speaker)
            if candidate and (
                candidate in world_state.npc_personas
                or world_state.resolve_npc_name(candidate)
            ):
                replacement = world_state.resolve_npc_name(candidate) or candidate
                break

        invalid_names = [
            name
            for name in list(world_state.npc_personas)
            if is_null_npc_target(name)
        ]
        for invalid_name in invalid_names:
            if replacement and replacement in world_state.npc_personas:
                world_state.merge_npc_personas(replacement, invalid_name)
            else:
                world_state.npc_personas.pop(invalid_name, None)
                world_state.subject_facts.pop(invalid_name, None)
                world_state.npc_relationships.pop(invalid_name, None)

        # Ordinary duplicate names remain useful aliases after a merge. A null
        # transport sentinel never does and must not resolve back to this NPC.
        for persona in world_state.npc_personas.values():
            persona.aliases = [
                alias for alias in persona.aliases if not is_null_npc_target(alias)
            ]

        def rewrite_identity(record: dict[str, Any], keys: tuple[str, ...]) -> None:
            for key in keys:
                raw = str(record.get(key) or "").strip()
                if raw and is_null_npc_target(raw):
                    record[key] = replacement

        for frame in frames:
            if frame.last_npc_speaker and is_null_npc_target(frame.last_npc_speaker):
                frame.last_npc_speaker = replacement
            for record in frame.session_npc_records:
                rewrite_identity(record, ("name", "npc"))
            for record in frame.open_conditions:
                rewrite_identity(record, ("npc", "npc_name", "speaker"))
            for record in frame.settled_exchanges:
                rewrite_identity(record, ("npc", "npc_name", "speaker"))

        scenes = []
        if scene_manager is not None:
            scenes.extend(scene_manager.history)
            if scene_manager.current_scene is not None:
                scenes.append(scene_manager.current_scene)
        for scene in scenes:
            scene.participants = [
                replacement if is_null_npc_target(name) else name
                for name in scene.participants
                if replacement or not is_null_npc_target(name)
            ]
            scene.participants = list(dict.fromkeys(scene.participants))
            for record in scene.open_conditions:
                rewrite_identity(record, ("npc", "npc_name", "speaker"))

    def _world_state_to_snapshot(
        self,
        world_state: WorldState,
        *,
        lossless: bool = False,
    ) -> dict[str, Any]:
        return {
            "session_pillars": self._encode(world_state.session_pillars),
            "map_notes": self._encode(world_state.map_notes),
            "map_locations": self._encode(world_state.map_locations),
            "map_routes": self._encode(world_state.map_routes),
            "semantic_map": self._encode(world_state.semantic_map),
            "npc_relationships": self._encode(world_state.npc_relationships),
            "memories": self._encode(world_state.memories),
            "npc_personas": self._encode(world_state.npc_personas),
            "npc_combat_blueprints": self._encode(world_state.npc_combat_blueprints),
            "subject_facts": self._encode(world_state.subject_facts),
            "persistent_changes": self._encode(world_state.persistent_changes),
            "story_items": self._encode(world_state.story_items),
            "memory_events": self._encode(world_state.memory_events),
            "memory_relations": self._encode(world_state.memory_relations),
            "gm_secrets": self._encode(world_state.gm_secrets),
            "world_profile": self._encode(world_state.world_profile),
            "party_sheet": self._encode(world_state.party_sheet),
            "world_sheet": self._encode(world_state.world_sheet),
            "present_players": self._encode(world_state.present_players),
            "absent_players": self._encode(world_state.absent_players),
            "chapter_packages": self._encode(world_state.chapter_packages),
            "active_chapter_package": self._encode(world_state.active_chapter_package),
            "iconic_elements": self._encode(world_state.iconic_elements),
            "transparency_audit_log": self._encode(world_state.transparency_audit_log),
            "decision_windows": self._encode(world_state.decision_windows),
            "pending_check_batches": self._encode(world_state.pending_check_batches),
            "check_batch_history": self._encode(
                world_state.check_batch_history
                if lossless
                else world_state.check_batch_history[-100:]
            ),
        }

    def _apply_world_state_snapshot(self, world_state: WorldState, data: dict[str, Any]) -> None:
        world_state.session_pillars = list(data.get("session_pillars", []))
        world_state.map_notes = dict(data.get("map_notes", {}))
        world_state.map_locations = {
            key: self._decode_dataclass(MapLocation, value) for key, value in data.get("map_locations", {}).items()
        }
        world_state.map_routes = {
            key: self._decode_dataclass(MapRouteEdge, value) for key, value in data.get("map_routes", {}).items()
        }
        semantic_map = data.get("semantic_map")
        world_state.semantic_map = (
            self._decode_dataclass(SemanticMapLayout, semantic_map)
            if isinstance(semantic_map, dict)
            else SemanticMapLayout()
        )
        world_state.npc_relationships = {key: list(value) for key, value in data.get("npc_relationships", {}).items()}
        world_state.memories = list(data.get("memories", []))
        world_state.npc_personas = {
            key: self._decode_dataclass(NPCPersona, value) for key, value in data.get("npc_personas", {}).items()
        }
        world_state.npc_combat_blueprints = {
            key: self._decode_dataclass(NPCCombatBlueprint, value)
            for key, value in data.get("npc_combat_blueprints", {}).items()
        }
        world_state.subject_facts = {key: list(value) for key, value in data.get("subject_facts", {}).items()}
        world_state.persistent_changes = [
            self._decode_dataclass(PersistentChange, value) for value in data.get("persistent_changes", [])
        ]
        world_state.story_items = {
            key: self._decode_dataclass(StoryItem, value)
            for key, value in data.get("story_items", {}).items()
        }
        world_state.memory_events = [self._decode_dataclass(MemoryEvent, value) for value in data.get("memory_events", [])]
        world_state.memory_relations = [
            self._decode_dataclass(MemoryRelation, value) for value in data.get("memory_relations", [])
        ]
        world_state.gm_secrets = {
            key: self._decode_dataclass(GMSecret, value) for key, value in data.get("gm_secrets", {}).items()
        }
        world_state.world_profile = self._decode_dataclass(WorldCreationProfile, data.get("world_profile", {}))
        party_sheet = data.get("party_sheet")
        world_sheet = data.get("world_sheet")
        world_state.party_sheet = self._decode_dataclass(PartySheet, party_sheet) if party_sheet else None
        world_state.world_sheet = self._decode_dataclass(WorldSheet, world_sheet) if world_sheet else None
        world_state.present_players = list(data.get("present_players", []))
        world_state.absent_players = dict(data.get("absent_players", {}))
        world_state.chapter_packages = {
            key: self._decode_dataclass(ChapterPackage, value)
            for key, value in data.get("chapter_packages", {}).items()
        }
        world_state.active_chapter_package = str(data.get("active_chapter_package") or "")
        world_state.iconic_elements = {
            key: self._decode_dataclass(IconicElementState, value)
            for key, value in data.get("iconic_elements", {}).items()
        }
        world_state.transparency_audit_log = [
            self._decode_dataclass(TransparencyAuditEntry, value)
            for value in data.get("transparency_audit_log", [])
        ]
        world_state.decision_windows = {
            key: self._decode_dataclass(DecisionWindow, value)
            for key, value in data.get("decision_windows", {}).items()
        }
        world_state.pending_check_batches = {
            key: self._decode_dataclass(PendingCheckBatch, value)
            for key, value in data.get("pending_check_batches", {}).items()
        }
        world_state.check_batch_history = [
            self._decode_dataclass(PendingCheckBatch, value)
            for value in data.get("check_batch_history", [])
        ]

    def _scene_manager_to_snapshot(self, scene_manager: SceneManager | None) -> dict[str, Any]:
        if scene_manager is None:
            return {"current_scene": None, "history": []}
        return {
            "current_scene": self._encode(scene_manager.current_scene),
            "history": self._encode(scene_manager.history),
            "suspended_scenes": self._encode(scene_manager.suspended_scenes),
            "free_action_round_number": scene_manager.free_action_round_number,
            "free_action_round_required_actors": self._encode(
                scene_manager.free_action_round_required_actors
            ),
            "free_action_round_acted_actors": self._encode(
                scene_manager.free_action_round_acted_actors
            ),
            "free_action_round_auto_advance_skip_names": self._encode(
                scene_manager.free_action_round_auto_advance_skip_names
            ),
            "actor_locations": self._encode(scene_manager.actor_locations),
            "actor_positions": self._encode(scene_manager.actor_positions),
        }

    def _scene_frame_manager_to_snapshot(
        self,
        scene_frame_manager: SceneFrameManager | None,
        *,
        lossless: bool = False,
    ) -> dict[str, Any]:
        if scene_frame_manager is None:
            return {}
        return {
            "current_frame": self._encode(scene_frame_manager.current_frame),
            "suspended_frames": self._encode(scene_frame_manager.suspended_frames),
            # A short history is enough to bridge a nearby scene transition
            # without letting campaign snapshots grow with every scene.
            "history": self._encode(
                scene_frame_manager.history
                if lossless
                else scene_frame_manager.history[-4:]
            ),
        }

    def _encode(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, Enum):
            return value.value
        if is_dataclass(value):
            return {field.name: self._encode(getattr(value, field.name)) for field in fields(value)}
        if isinstance(value, dict):
            return {str(key): self._encode(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._encode(item) for item in value]
        if isinstance(value, set):
            return [self._encode(item) for item in sorted(value, key=str)]
        return value

    @classmethod
    def _nested_tuple(cls, value: Any) -> Any:
        if isinstance(value, list):
            return tuple(cls._nested_tuple(item) for item in value)
        return value

    def _decode_dataclass(self, cls, data: dict[str, Any]):
        if data is None:
            return None
        hints = self._safe_type_hints(cls)
        kwargs = {}
        for field in fields(cls):
            if field.name not in data:
                continue
            kwargs[field.name] = self._decode_value(hints.get(field.name, Any), data[field.name])
        instance = cls(**kwargs)
        if isinstance(instance, TimedEffect):
            instance.data = self._decode_timed_effect_data(instance)
        return instance

    def _decode_value(self, hint, value):
        if value is None or hint is Any:
            return value
        if isinstance(hint, str):
            return self._decode_string_hint(hint, value)
        origin = get_origin(hint)
        args = get_args(hint)
        if origin in (list, tuple):
            item_hint = args[0] if args else Any
            return [self._decode_value(item_hint, item) for item in value]
        if origin is set:
            item_hint = args[0] if args else Any
            return {self._decode_value(item_hint, item) for item in value}
        if origin is dict:
            key_hint = args[0] if args else str
            value_hint = args[1] if len(args) > 1 else Any
            return {
                self._decode_key(key_hint, key): self._decode_value(value_hint, item)
                for key, item in value.items()
            }
        if origin in (Union, types.UnionType):
            non_none = [arg for arg in args if arg is not type(None)]
            if not non_none:
                return value
            return self._decode_value(non_none[0], value)
        if isinstance(hint, type) and issubclass(hint, Enum):
            return hint(value)
        if isinstance(hint, type) and is_dataclass(hint):
            return self._decode_dataclass(hint, value)
        return value

    def _decode_key(self, hint, value):
        if isinstance(hint, str):
            if hint in vars(models):
                model_type = getattr(models, hint)
                if isinstance(model_type, type) and issubclass(model_type, Enum):
                    return model_type(value)
            if hint == "int":
                return int(value)
            return value
        if isinstance(hint, type) and issubclass(hint, Enum):
            return hint(value)
        if hint is int:
            return int(value)
        return value

    def _safe_type_hints(self, cls) -> dict[str, Any]:
        try:
            return get_type_hints(cls, vars(models))
        except TypeError:
            return dict(getattr(cls, "__annotations__", {}))

    def _decode_string_hint(self, hint: str, value):
        hint = hint.strip()
        if value is None:
            return None
        if " | " in hint:
            choices = [choice.strip() for choice in hint.split("|") if choice.strip() != "None"]
            return self._decode_string_hint(choices[0], value) if choices else value
        if hint.startswith("list[") and hint.endswith("]"):
            item_hint = hint[5:-1].strip()
            return [self._decode_string_hint(item_hint, item) for item in value]
        if hint.startswith("set[") and hint.endswith("]"):
            item_hint = hint[4:-1].strip()
            return {self._decode_string_hint(item_hint, item) for item in value}
        if hint.startswith("tuple[") and hint.endswith("]"):
            item_hints = self._split_hint_args(hint[6:-1])
            if len(item_hints) == 2 and item_hints[1] == "...":
                return tuple(self._decode_string_hint(item_hints[0], item) for item in value)
            return tuple(self._decode_string_hint(item_hints[index], item) for index, item in enumerate(value))
        if hint.startswith("dict[") and hint.endswith("]"):
            key_hint, value_hint = self._split_hint_args(hint[5:-1])
            return {
                self._decode_key(key_hint, key): self._decode_string_hint(value_hint, item)
                for key, item in value.items()
            }
        if hint in {"str", "int", "bool", "float", "Any"}:
            if hint == "int":
                return int(value)
            if hint == "bool":
                return bool(value)
            if hint == "float":
                return float(value)
            return value
        model_type = getattr(models, hint, None)
        if isinstance(model_type, type) and issubclass(model_type, Enum):
            return model_type(value)
        if isinstance(model_type, type) and is_dataclass(model_type):
            return self._decode_dataclass(model_type, value)
        return value

    def _split_hint_args(self, text: str) -> list[str]:
        parts: list[str] = []
        depth = 0
        start = 0
        for index, char in enumerate(text):
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
            elif char == "," and depth == 0:
                parts.append(text[start:index].strip())
                start = index + 1
        parts.append(text[start:].strip())
        return parts

    def _decode_timed_effect_data(self, effect: TimedEffect) -> dict[str, Any]:
        data = dict(effect.data)
        if effect.effect_type == "affinity_buff":
            data["affinity_changes"] = {
                damage_type: Affinity(affinity)
                for damage_type, affinity in data.get("affinity_changes", {}).items()
            }
        if effect.effect_type == "status_immunity":
            data["status_immunities"] = [StatusEffect(status) for status in data.get("status_immunities", [])]
        if effect.effect_type == "attribute_buff":
            data["attribute_bonus"] = {key: int(value) for key, value in data.get("attribute_bonus", {}).items()}
        if effect.effect_type == "defense_bonus":
            data["defense_bonus"] = {key: int(value) for key, value in data.get("defense_bonus", {}).items()}
        if effect.effect_type == "defense_floor":
            data["defense_floor"] = {key: int(value) for key, value in data.get("defense_floor", {}).items()}
        if effect.expires_on == EffectTiming.SCENE_END and effect.effect_type == "dungeon_state":
            data["state"] = self._decode_dataclass(DungeonState, data["state"])
        return data

    def _campaign_dir(self, campaign_id: str) -> Path:
        clean_id = self._clean_name(campaign_id)
        if not clean_id:
            raise ValueError("campaign_id 不能为空。")
        return self.root / clean_id

    def _snapshot_path(self, campaign_id: str, *, slot: str | None = None) -> Path:
        campaign_dir = self._campaign_dir(campaign_id)
        if slot:
            return campaign_dir / "saves" / f"{self._clean_name(slot)}.json"
        return campaign_dir / "snapshot.json"

    def _clean_name(self, value: str) -> str:
        return safe_campaign_path_segment(value, default="")

    def _snapshot_saved_at(self, path: Path) -> str:
        if not path.exists():
            return ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return str(data.get("saved_at") or "")
        except Exception:
            return ""

    @staticmethod
    def _atomic_write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
