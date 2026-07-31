from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass
class CampaignStateSnapshot:
    campaign: dict[str, object]
    travel: dict[str, object]
    route_plans: list[object]
    runtime_ephemeral: dict[str, object]
    rng_state: object = None
    check_transaction: dict[str, object] | None = None
    check_pending: object = None
    check_candidate: object = None


class CampaignStateTransaction:
    """Capture and restore every mutable domain touched by one GM tool call."""

    @staticmethod
    def capture(app: Any, campaign_id: str) -> CampaignStateSnapshot:
        travel = getattr(app, "travel_manager", None)
        world_map = getattr(app, "world_map_manager", None)
        interceptor = getattr(app, "interceptor", None)
        check_manager = getattr(interceptor, "check_transaction_manager", None)
        rules_engine = getattr(interceptor, "rules_engine", None)
        rng = getattr(rules_engine, "_rng", None)
        creation_manager = getattr(app, "character_creation_manager", None)
        creation_rules = getattr(creation_manager, "rules_engine", None)
        creation_rng = getattr(creation_rules, "_rng", None)
        campaign = app.memory_store.build_snapshot(
                campaign_id,
                world_state=app.world_state,
                character_manager=app.character_manager,
                clock_manager=app.clock_manager,
                conflict_manager=app.conflict_manager,
                scene_manager=app.scene_manager,
                scene_frame_manager=app.scene_frame_manager,
                ritual_manager=app.ritual_manager,
                project_manager=app.project_manager,
                story_arc_manager=app.story_arc_manager,
                hero_log_manager=app.hero_log_manager,
                ally_npc_manager=app.ally_npc_manager,
                session_ledger=app.session_ledger,
                session_zero_manager=app.session_zero_manager,
                travel_manager=getattr(app, "travel_manager", None),
                dungeon_manager=getattr(app, "dungeon_manager", None),
                world_map_manager=getattr(app, "world_map_manager", None),
                rules_engine=rules_engine,
                progression_manager=getattr(app, "progression_manager", None),
                lossless=True,
            )
        # ``saved_at`` describes serialization time, not campaign state. Keeping
        # wall-clock time in a transaction snapshot makes two pure reads appear
        # different whenever they straddle a second boundary.
        campaign["saved_at"] = ""
        return CampaignStateSnapshot(
            campaign=campaign,
            travel={
                "last_journey": deepcopy(getattr(travel, "last_journey", None)),
                "history": deepcopy(getattr(travel, "history", [])),
                "routes": deepcopy(getattr(travel, "routes", {})),
                "owned_transports": deepcopy(getattr(travel, "owned_transports", set())),
                "active_journey": deepcopy(getattr(travel, "active_journey", None)),
            },
            route_plans=deepcopy(getattr(world_map, "route_plans", [])),
            runtime_ephemeral={
                "_surfaced_topic_memory_paths": deepcopy(
                    getattr(app, "_surfaced_topic_memory_paths", set())
                ),
                "_world_map_generation_status": deepcopy(
                    getattr(app, "_world_map_generation_status", {})
                ),
                "recent_pipeline_spans": deepcopy(
                    getattr(app, "recent_pipeline_spans", [])
                ),
                "last_resolved_check_event_id": str(
                    getattr(app, "last_resolved_check_event_id", "") or ""
                ),
                "last_gm_beat_diagnostics": deepcopy(
                    getattr(app, "last_gm_beat_diagnostics", [])
                ),
                "last_gm_beat_fidelity_diagnostics": deepcopy(
                    getattr(app, "last_gm_beat_fidelity_diagnostics", [])
                ),
                "_advancing_check_batches": bool(
                    getattr(interceptor, "_advancing_check_batches", False)
                ),
                "character_creation_hero_profiles": deepcopy(
                    getattr(creation_manager, "hero_profiles", {})
                ),
                "character_creation_rng_state": (
                    creation_rng.getstate()
                    if creation_rng is not None
                    and hasattr(creation_rng, "getstate")
                    else None
                ),
            },
            rng_state=(rng.getstate() if rng is not None and hasattr(rng, "getstate") else None),
            check_transaction=(
                deepcopy(check_manager.snapshot())
                if check_manager is not None and hasattr(check_manager, "snapshot")
                else None
            ),
            check_pending=deepcopy(getattr(check_manager, "pending", None)),
            check_candidate=deepcopy(getattr(check_manager, "candidate", None)),
        )

    @staticmethod
    def restore(app: Any, snapshot: CampaignStateSnapshot) -> None:
        app.memory_store.apply_snapshot(
            snapshot.campaign,
            world_state=app.world_state,
            character_manager=app.character_manager,
            clock_manager=app.clock_manager,
            conflict_manager=app.conflict_manager,
            scene_manager=app.scene_manager,
            scene_frame_manager=app.scene_frame_manager,
            ritual_manager=app.ritual_manager,
            project_manager=app.project_manager,
            story_arc_manager=app.story_arc_manager,
            hero_log_manager=app.hero_log_manager,
            ally_npc_manager=app.ally_npc_manager,
            session_ledger=app.session_ledger,
            session_zero_manager=app.session_zero_manager,
            travel_manager=getattr(app, "travel_manager", None),
            dungeon_manager=getattr(app, "dungeon_manager", None),
            world_map_manager=getattr(app, "world_map_manager", None),
            rules_engine=getattr(getattr(app, "interceptor", None), "rules_engine", None),
            progression_manager=getattr(app, "progression_manager", None),
        )

        travel = getattr(app, "travel_manager", None)
        if travel is not None:
            travel.last_journey = deepcopy(snapshot.travel.get("last_journey"))
            travel.history = deepcopy(snapshot.travel.get("history") or [])
            travel.routes = deepcopy(snapshot.travel.get("routes") or {})
            travel.owned_transports = deepcopy(
                snapshot.travel.get("owned_transports") or set()
            )
            travel.active_journey = deepcopy(
                snapshot.travel.get("active_journey")
            )
        world_map = getattr(app, "world_map_manager", None)
        if world_map is not None:
            world_map.route_plans = deepcopy(snapshot.route_plans)
        for name, value in snapshot.runtime_ephemeral.items():
            if name in {
                "_advancing_check_batches",
                "character_creation_hero_profiles",
                "character_creation_rng_state",
            }:
                continue
            if hasattr(app, name):
                setattr(app, name, deepcopy(value))

        creation_manager = getattr(app, "character_creation_manager", None)
        if creation_manager is not None:
            creation_manager.hero_profiles = deepcopy(
                snapshot.runtime_ephemeral.get(
                    "character_creation_hero_profiles",
                    {},
                )
            )
            creation_rules = getattr(creation_manager, "rules_engine", None)
            creation_rng = getattr(creation_rules, "_rng", None)
            creation_rng_state = snapshot.runtime_ephemeral.get(
                "character_creation_rng_state"
            )
            if (
                creation_rng_state is not None
                and creation_rng is not None
                and hasattr(creation_rng, "setstate")
            ):
                creation_rng.setstate(creation_rng_state)

        interceptor = getattr(app, "interceptor", None)
        if interceptor is not None:
            interceptor._advancing_check_batches = bool(
                snapshot.runtime_ephemeral.get(
                    "_advancing_check_batches",
                    False,
                )
            )
        check_manager = getattr(interceptor, "check_transaction_manager", None)
        if check_manager is not None:
            if snapshot.check_transaction is not None and hasattr(check_manager, "restore"):
                check_manager.restore(deepcopy(snapshot.check_transaction))
            check_manager.pending = deepcopy(snapshot.check_pending)
            check_manager.candidate = deepcopy(snapshot.check_candidate)
            interceptor.pending_check_transactions = check_manager.pending

        rules_engine = getattr(interceptor, "rules_engine", None)
        rng = getattr(rules_engine, "_rng", None)
        if snapshot.rng_state is not None and rng is not None and hasattr(rng, "setstate"):
            rng.setstate(snapshot.rng_state)
