from __future__ import annotations

import json
import shutil
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

from fu_gm import models
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.project_manager import ProjectManager
from fu_gm.components.ritual_manager import RitualManager
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.story_arc_manager import StoryArcManager
from fu_gm.components.world_state import WorldState
from fu_gm.models import (
    Affinity,
    CampaignArcState,
    Character,
    Clock,
    ConflictState,
    DungeonState,
    EffectTiming,
    GMSecret,
    MapLocation,
    MapRouteEdge,
    MemoryEvent,
    MemoryRelation,
    NPCPersona,
    PartySheet,
    PersistentChange,
    ProjectState,
    RitualPlan,
    SceneRecord,
    StatusEffect,
    TimedEffect,
    WorldCreationProfile,
    WorldSheet,
)


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
        ritual_manager: RitualManager | None = None,
        project_manager: ProjectManager | None = None,
        story_arc_manager: StoryArcManager | None = None,
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
            ritual_manager=ritual_manager,
            project_manager=project_manager,
            story_arc_manager=story_arc_manager,
        )
        snapshot_path = campaign_dir / "snapshot.json"
        snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        return_path = snapshot_path
        if slot:
            save_dir = campaign_dir / "saves"
            save_dir.mkdir(parents=True, exist_ok=True)
            return_path = save_dir / f"{self._clean_name(slot)}.json"
            return_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

        events_path = campaign_dir / "events.jsonl"
        events_text = "\n".join(json.dumps(self._encode(event), ensure_ascii=False) for event in world_state.memory_events)
        events_path.write_text(events_text + ("\n" if events_text else ""), encoding="utf-8")
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
        ritual_manager: RitualManager | None = None,
        project_manager: ProjectManager | None = None,
        story_arc_manager: StoryArcManager | None = None,
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
            ritual_manager=ritual_manager,
            project_manager=project_manager,
            story_arc_manager=story_arc_manager,
        )
        return snapshot

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
        ritual_manager: RitualManager | None = None,
        project_manager: ProjectManager | None = None,
        story_arc_manager: StoryArcManager | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "campaign_id": campaign_id,
            "saved_at": self._now(),
            "world_state": self._world_state_to_snapshot(world_state),
            "characters": self._encode(character_manager.all()),
            "clocks": self._encode(list(clock_manager._clocks.values())),
            "conflict_state": self._encode(conflict_manager.state),
            "scene_manager": self._scene_manager_to_snapshot(scene_manager),
            "rituals": {
                "active_rituals": self._encode(list(ritual_manager.active_rituals.values())) if ritual_manager else [],
            },
            "projects": {
                "projects": self._encode(list(project_manager.projects.values())) if project_manager else [],
            },
            "story_arc": self._encode(story_arc_manager.state) if story_arc_manager else None,
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
        ritual_manager: RitualManager | None = None,
        project_manager: ProjectManager | None = None,
        story_arc_manager: StoryArcManager | None = None,
    ) -> None:
        if snapshot.get("schema_version") != self.SCHEMA_VERSION:
            raise ValueError(f"不支持的记忆快照版本：{snapshot.get('schema_version')}")

        self._apply_world_state_snapshot(world_state, snapshot["world_state"])

        character_manager._characters = {}
        for character_data in snapshot.get("characters", []):
            character = self._decode_dataclass(Character, character_data)
            character_manager.add(character)

        clock_manager._clocks = {}
        for clock_data in snapshot.get("clocks", []):
            clock = self._decode_dataclass(Clock, clock_data)
            clock_manager.add(clock)

        conflict_manager.state = self._decode_dataclass(ConflictState, snapshot.get("conflict_state", {}))

        if scene_manager is not None:
            scene_data = snapshot.get("scene_manager", {})
            current = scene_data.get("current_scene")
            scene_manager.current_scene = self._decode_dataclass(SceneRecord, current) if current else None
            scene_manager.history = [self._decode_dataclass(SceneRecord, item) for item in scene_data.get("history", [])]

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

    def _world_state_to_snapshot(self, world_state: WorldState) -> dict[str, Any]:
        return {
            "session_pillars": self._encode(world_state.session_pillars),
            "map_notes": self._encode(world_state.map_notes),
            "map_locations": self._encode(world_state.map_locations),
            "map_routes": self._encode(world_state.map_routes),
            "npc_relationships": self._encode(world_state.npc_relationships),
            "memories": self._encode(world_state.memories),
            "npc_personas": self._encode(world_state.npc_personas),
            "subject_facts": self._encode(world_state.subject_facts),
            "persistent_changes": self._encode(world_state.persistent_changes),
            "memory_events": self._encode(world_state.memory_events),
            "memory_relations": self._encode(world_state.memory_relations),
            "gm_secrets": self._encode(world_state.gm_secrets),
            "world_profile": self._encode(world_state.world_profile),
            "party_sheet": self._encode(world_state.party_sheet),
            "world_sheet": self._encode(world_state.world_sheet),
            "present_players": self._encode(world_state.present_players),
            "absent_players": self._encode(world_state.absent_players),
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
        world_state.npc_relationships = {key: list(value) for key, value in data.get("npc_relationships", {}).items()}
        world_state.memories = list(data.get("memories", []))
        world_state.npc_personas = {
            key: self._decode_dataclass(NPCPersona, value) for key, value in data.get("npc_personas", {}).items()
        }
        world_state.subject_facts = {key: list(value) for key, value in data.get("subject_facts", {}).items()}
        world_state.persistent_changes = [
            self._decode_dataclass(PersistentChange, value) for value in data.get("persistent_changes", [])
        ]
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

    def _scene_manager_to_snapshot(self, scene_manager: SceneManager | None) -> dict[str, Any]:
        if scene_manager is None:
            return {"current_scene": None, "history": []}
        return {
            "current_scene": self._encode(scene_manager.current_scene),
            "history": self._encode(scene_manager.history),
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
        if origin is Union or str(origin) == "types.UnionType":
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
        return value.strip().replace("/", "_").replace("\\", "_").replace(" ", "_")

    def _snapshot_saved_at(self, path: Path) -> str:
        if not path.exists():
            return ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return str(data.get("saved_at") or "")
        except Exception:
            return ""

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
