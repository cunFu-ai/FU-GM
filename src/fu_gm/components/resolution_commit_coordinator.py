from __future__ import annotations

from typing import Callable

from fu_gm.components.clock_lifecycle_coordinator import ClockLifecycleCoordinator
from fu_gm.components.narrative_memory_writer import NarrativeMemoryWriter
from fu_gm.components.scene_frame_manager import SceneFrameManager
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.topic_memory_store import TopicMemoryStore
from fu_gm.models import ActionResolution


class ResolutionCommitCoordinator:
    """Commit one authoritative resolution before any prose is rendered.

    Player turns, automatic NPC turns, and queued actions used to duplicate
    only parts of this sequence. Keeping it here guarantees that a completed
    pressure clock, narrative memory, and the current scene frame agree no
    matter which runtime entry point produced the action.
    """

    def __init__(
        self,
        *,
        clocks: ClockLifecycleCoordinator,
        memories: NarrativeMemoryWriter,
        topics_provider: Callable[[], TopicMemoryStore],
        scenes: SceneManager,
        frame_provider: Callable[[], SceneFrameManager],
        campaign_id_provider: Callable[[], str],
    ) -> None:
        self.clocks = clocks
        self.memories = memories
        self.topics_provider = topics_provider
        self.scenes = scenes
        self.frame_provider = frame_provider
        self.campaign_id_provider = campaign_id_provider

    def commit(self, resolution: ActionResolution) -> bool:
        if resolution.payload.get("check_result_provisional"):
            return False
        if resolution.payload.get("_authoritative_state_committed"):
            return False

        # A full threat clock is itself a world event. Settle it before scene
        # and memory writers inspect the payload so every projection sees the
        # same committed consequence.
        self.clocks.settle_resolution(resolution)
        self.memories.write(
            resolution,
            campaign_id=self.campaign_id_provider(),
            topics=self.topics_provider(),
        )
        # Investigation information is authoritative once the check resolves,
        # but it is not yet public: the player-facing reply has not been
        # produced.  Keep it out of the public scene ledger until ``publish``
        # confirms that the final group-chat message actually carried it.
        resolution.payload["_defer_public_information"] = True
        self.frame_provider().update_from_resolution(
            resolution,
            scene=self.scenes.current_scene,
        )
        resolution.payload["_authoritative_state_committed"] = True
        return True

    def publish(self, resolution: ActionResolution, public_reply: str) -> list[str]:
        """Publish only resolution facts that reached the final table reply."""

        if resolution.payload.get("check_result_provisional"):
            return []
        if resolution.payload.get("_public_information_published"):
            return list(resolution.payload.get("published_information") or [])
        published = self.frame_provider().publish_resolution_information(
            resolution,
            public_reply=public_reply,
        )
        resolution.payload["_public_information_published"] = True
        resolution.payload["published_information"] = list(published)
        return published
