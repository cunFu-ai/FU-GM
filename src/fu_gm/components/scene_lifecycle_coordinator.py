from __future__ import annotations

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.decision_window_manager import DecisionWindowManager
from fu_gm.components.session_episode_tracker import SessionEpisodeTracker
from fu_gm.components.skill_trigger_manager import SkillTriggerManager
from fu_gm.components.world_state import WorldState
from fu_gm.models import DecisionWindowStatus, SceneRecord, SceneType


class SceneLifecycleCoordinator:
    """Own all start/end side effects for standard and conflict scenes."""

    def __init__(
        self,
        *,
        clocks: ClockManager,
        decisions: DecisionWindowManager,
        conflict: ConflictManager,
        characters: CharacterManager,
        world_state: WorldState,
        skills: SkillTriggerManager,
        episodes: SessionEpisodeTracker,
        rituals: object | None = None,
    ) -> None:
        self.clocks = clocks
        self.decisions = decisions
        self.conflict = conflict
        self.characters = characters
        self.world_state = world_state
        self.skills = skills
        self.episodes = episodes
        self.rituals = rituals

    def start(self, scene: SceneRecord) -> None:
        scene_id = scene.scene_id or scene.name
        self.clocks.begin_scene(scene_id)
        self.episodes.scene_started(scene)
        scene.recovered_fallen_pcs = self._recover_fallen_pcs(scene)
        for character in self._characters_in_scene(scene):
            self.skills.emit("scene_start", character, scene_id=scene_id)
        if scene.scene_type in {SceneType.SESSION_ZERO, SceneType.GM}:
            return
        known_pc_names = self._known_player_character_names()
        for raw_name in scene.participants:
            name = str(raw_name or "").strip()
            if not name:
                continue
            if name in known_pc_names:
                continue
            self.world_state.ensure_npc_persona(
                name,
                profile_status="placeholder",
                public_identity=name,
                role_in_story="当前场景中的非玩家角色",
                first_scene=scene.name,
                current_location=scene.location,
                last_seen_scene=scene_id,
            )

    def enter(self, scene: SceneRecord, participants: list[str]) -> None:
        """Apply per-character next-scene recovery on an existing branch."""

        recovered = self._recover_fallen_pcs(
            scene,
            participants=participants,
        )
        scene.recovered_fallen_pcs = list(
            dict.fromkeys([*scene.recovered_fallen_pcs, *recovered])
        )

    def _recover_fallen_pcs(
        self,
        scene: SceneRecord,
        *,
        participants: list[str] | None = None,
    ) -> list[str]:
        """Wake defeated PCs at crisis HP when their next scene begins."""

        recovered: list[str] = []
        participant_names = {
            str(item or "").strip()
            for item in (
                scene.participants if participants is None else participants
            )
            if str(item or "").strip()
        }
        for name in sorted(participant_names):
            if name not in self.conflict.state.fallen_pcs:
                continue
            if name in self.conflict.state.sacrifices or not self.characters.exists(name):
                continue
            character = self.characters.get(name)
            crisis = (
                character.crisis_threshold
                if character.crisis_threshold > 0
                else character.max_hp // 2
            )
            target_hp = max(1, crisis)
            self.characters.modify_resource(
                name,
                "hp",
                target_hp - character.hp,
            )
            self.conflict.state.fallen_pcs.pop(name, None)
            self.conflict.state.defeated_combatants.discard(name)
            recovered.append(name)
            self.conflict.record_log(
                name,
                "pc_recovered_next_scene",
                f"{name}在新的场景开始时恢复意识，当前生命值为危机值 {character.hp}。",
            )
        return recovered

    def focus(self, scene: SceneRecord) -> None:
        """Restore runtime scope when the camera returns to an active branch.

        Focusing is not a new scene start: it must not re-emit skill triggers,
        duplicate episode telemetry or recreate NPCs.
        """

        self.clocks.begin_scene(scene.scene_id or scene.name)
        self.episodes.scene_focused(scene)

    def _known_player_character_names(self) -> set[str]:
        names = {
            character.name
            for character in self.characters.all()
            if "pc" in character.traits and str(character.name or "").strip()
        }
        party_sheet = getattr(self.world_state, "party_sheet", None)
        for member in list(getattr(party_sheet, "members", []) or []):
            hero_name = str(getattr(member, "hero_name", "") or "").strip()
            if hero_name:
                names.add(hero_name)
        profile = getattr(self.world_state, "world_profile", None)
        for key, draft in dict(getattr(profile, "hero_drafts", {}) or {}).items():
            hero_name = str(getattr(draft, "hero_name", "") or key or "").strip()
            if hero_name:
                names.add(hero_name)
        return names

    def _characters_in_scene(self, scene: SceneRecord):
        participants = {
            str(item or "").strip()
            for item in scene.participants
            if str(item or "").strip()
        }
        if not participants:
            return self.characters.all()
        return [
            character
            for character in self.characters.all()
            if character.name in participants
        ]

    def _expire_windows_for_scene(self, scene: SceneRecord) -> None:
        scene_ids = {
            str(scene.scene_id or "").strip(),
            str(scene.name or "").strip(),
        }
        scene_ids.discard("")
        for scope_kind in ("scene", "conflict"):
            for scope_id in scene_ids:
                self.decisions.cancel_matching(
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    reason="scene_ended",
                    status=DecisionWindowStatus.EXPIRED,
                )

        # Older snapshots used the ambiguous ``current`` scope. Restrict its
        # cleanup to this scene's participants so another split-party branch
        # does not lose an unresolved choice.
        participants = {
            str(item or "").strip()
            for item in scene.participants
            if str(item or "").strip()
        }
        for scope_kind in ("scene", "conflict"):
            if not participants:
                self.decisions.cancel_matching(
                    scope_kind=scope_kind,
                    scope_id="current",
                    reason="scene_ended",
                    status=DecisionWindowStatus.EXPIRED,
                )
                continue
            for owner in participants:
                self.decisions.cancel_matching(
                    owner=owner,
                    scope_kind=scope_kind,
                    scope_id="current",
                    reason="scene_ended",
                    status=DecisionWindowStatus.EXPIRED,
                )

    def end(self, scene: SceneRecord) -> None:
        scene_id = scene.scene_id or scene.name
        self._expire_windows_for_scene(scene)
        if self.rituals is not None:
            self.rituals.cancel_scene(
                scene_id,
                reason="场景结束，尚未完成的仪式准备中断。",
            )
        self.clocks.end_scene(scene_id)
        for character in self._characters_in_scene(scene):
            self.skills.emit("scene_end", character, scene_id=scene_id)
        self.episodes.scene_ended(scene)
        participants = list(scene.participants)
        conflict_scene = str(self.conflict.state.scene_name or "").strip()
        if self.conflict.state.active and conflict_scene in {
            str(scene.scene_id or "").strip(),
            str(scene.name or "").strip(),
        }:
            self.conflict.end_scene(participants)
        else:
            self.conflict.clear_scene_effects(participants)
