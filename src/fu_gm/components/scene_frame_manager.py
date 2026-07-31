from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Iterable

from fu_gm.components.adventure_event_manager import AdventureEventManager
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.npc_response_contract import (
    is_current_action_permission_bargain,
    is_nonfinal_promise_result,
)
from fu_gm.components.npc_deferred_commitment_manager import NPCDeferredCommitmentManager
from fu_gm.components.npc_response_window_manager import NPCResponseWindowManager
from fu_gm.components.session_scene_navigator import SessionSceneNavigator
from fu_gm.components.session_ledger import SessionLedger
from fu_gm.components.world_state import WorldState
from fu_gm.models import Action, ActionResolution, ActionType, SceneRecord, SessionDramaticContract


@dataclass
class SceneFrame:
    """GM-facing prep for the current scene.

    This is not a plot script. It stores a stable situation, clue web and pressure
    so different player approaches can reveal consistent information.
    """

    scene_key: str
    scene_name: str
    source_scene_id: str = ""
    location: str = ""
    premise: str = ""
    stakes: str = ""
    current_pressure: str = ""
    session_title: str = ""
    dramatic_question: str = ""
    signature_image: str = ""
    opposition_goal: str = ""
    dilemma: str = ""
    reversal: str = ""
    climax_type: str = ""
    closure_requirement: str = ""
    irreversible_change: str = ""
    ending_echo: str = ""
    contract_situation_facts: list[str] = field(default_factory=list)
    session_opportunity_key: str = ""
    session_opportunity_title: str = ""
    session_opportunity_role: str = ""
    session_opportunity_purpose: str = ""
    session_opportunity_situation: str = ""
    required_opening_elements: list[str] = field(default_factory=list)
    required_opening_npc_names: list[str] = field(default_factory=list)
    session_scene_opportunities: list[str] = field(default_factory=list)
    session_clue_routes: list[str] = field(default_factory=list)
    session_npc_roles: list[str] = field(default_factory=list)
    session_npc_records: list[dict[str, str]] = field(default_factory=list)
    fantastic_details: list[str] = field(default_factory=list)
    escalation_ladder: list[str] = field(default_factory=list)
    possible_payoffs: list[str] = field(default_factory=list)
    visible_elements: list[str] = field(default_factory=list)
    npc_functions: list[str] = field(default_factory=list)
    clue_pool: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    possible_reveals: list[str] = field(default_factory=list)
    unresolved_requests: list[str] = field(default_factory=list)
    pending_npc_questions: list[dict[str, str]] = field(default_factory=list)
    # Versioned transcript fingerprints for semantic save migrations. Empty
    # results are meaningful too, so they must be persisted just like records.
    history_reconciliation_markers: dict[str, str] = field(default_factory=dict)
    committed_consequences: list[str] = field(default_factory=list)
    established_facts: list[str] = field(default_factory=list)
    public_facts: list[str] = field(default_factory=list)
    revealed_clues: list[str] = field(default_factory=list)
    recent_beats: list[str] = field(default_factory=list)
    investigation_cards: list[dict[str, str]] = field(default_factory=list)
    open_conditions: list[dict[str, str]] = field(default_factory=list)
    settled_exchanges: list[dict[str, str]] = field(default_factory=list)
    deferred_npc_commitments: list[dict[str, str]] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    clarity_notes: list[str] = field(default_factory=list)
    opening_guidance: list[str] = field(default_factory=list)
    npc_response_guidance: list[str] = field(default_factory=list)
    investigation_guidance: list[str] = field(default_factory=list)
    failure_guidance: list[str] = field(default_factory=list)
    telegraphed_threats: list[str] = field(default_factory=list)
    danger_candidates: list[str] = field(default_factory=list)
    discovery_candidates: list[str] = field(default_factory=list)
    special_mechanism_candidates: list[str] = field(default_factory=list)
    story_outline: list[str] = field(default_factory=list)
    last_npc_speaker: str = ""
    last_updated: str = ""


class SceneFrameManager:
    """Builds and updates a cache-safe scene frame without extra LLM calls."""

    def __init__(self, *, session_ledger: SessionLedger | None = None) -> None:
        self.current_frame: SceneFrame | None = None
        self.history: list[SceneFrame] = []
        self.suspended_frames: dict[str, SceneFrame] = {}
        self.scene_navigator = SessionSceneNavigator()
        self.session_ledger = session_ledger
        self.npc_deferred_commitment_manager = NPCDeferredCommitmentManager()

    def suspend_current_frame(self) -> SceneFrame | None:
        """Park the current GM situation while another party branch is focused."""

        frame = self.current_frame
        if frame is None:
            return None
        key = str(frame.source_scene_id or frame.scene_key or "").strip()
        if key:
            self.suspended_frames[key] = frame
        self.current_frame = None
        return frame

    def restore_suspended_frame(self, scene: SceneRecord) -> SceneFrame | None:
        """Restore a parked frame without treating it as a new scene."""

        key = str(scene.scene_id or "").strip()
        frame = self.suspended_frames.pop(key, None)
        if frame is None:
            return None
        self.current_frame = frame
        self._touch(frame)
        return frame

    def coalesce_suspended_frames(
        self,
        primary_scene_id: str,
        duplicate_scene_ids: Iterable[str],
    ) -> SceneFrame | None:
        """Merge frames for legacy duplicate branches at one exact location.

        The focused frame remains authoritative when two private prep fields
        disagree.  Facts and lifecycle records are additive because losing an
        already revealed clue or an unanswered NPC question would be worse
        than retaining both versions for later GM reconciliation.
        """

        clean_primary_id = str(primary_scene_id or "").strip()
        duplicate_ids = {
            str(scene_id or "").strip()
            for scene_id in duplicate_scene_ids
            if str(scene_id or "").strip()
        }
        if not clean_primary_id or not duplicate_ids:
            return self.current_frame

        primary = self.current_frame
        if primary is None or str(primary.source_scene_id or "").strip() != clean_primary_id:
            primary = self.suspended_frames.pop(clean_primary_id, None)
            if primary is not None:
                self.current_frame = primary
        if primary is None:
            # A legacy save may have a SceneRecord without a corresponding
            # current frame. Promote one duplicate rather than discarding its
            # remembered table facts; ensure_frame will refresh its identity.
            for duplicate_id in tuple(duplicate_ids):
                primary = self.suspended_frames.pop(duplicate_id, None)
                if primary is not None:
                    duplicate_ids.remove(duplicate_id)
                    self.current_frame = primary
                    break
        if primary is None:
            return None

        for duplicate_id in duplicate_ids:
            duplicate = self.suspended_frames.pop(duplicate_id, None)
            if duplicate is None or duplicate is primary:
                continue
            self._merge_frame_state(primary, duplicate)
        primary.source_scene_id = clean_primary_id
        self.normalize_loaded_state()
        self._touch(primary)
        return primary

    @staticmethod
    def _merge_frame_state(primary: SceneFrame, duplicate: SceneFrame) -> None:
        """Merge one same-location frame without overwriting current prep."""

        identity_fields = {
            "scene_key",
            "scene_name",
            "source_scene_id",
            "location",
            "last_updated",
        }
        for frame_field in fields(SceneFrame):
            name = frame_field.name
            if name in identity_fields:
                continue
            current_value = getattr(primary, name)
            incoming_value = getattr(duplicate, name)
            if isinstance(current_value, list) and isinstance(incoming_value, list):
                for item in incoming_value:
                    if item not in current_value:
                        current_value.append(item)
                continue
            if isinstance(current_value, dict) and isinstance(incoming_value, dict):
                for key, value in incoming_value.items():
                    current_value.setdefault(key, value)
                continue
            if not current_value and incoming_value:
                setattr(primary, name, incoming_value)

    def archive_scene(self, scene_id: str) -> SceneFrame | None:
        """Archive the frame owned by an ended scene, focused or parked."""

        clean_id = str(scene_id or "").strip()
        if not clean_id:
            return None
        frame: SceneFrame | None = None
        if (
            self.current_frame is not None
            and str(self.current_frame.source_scene_id or "").strip() == clean_id
        ):
            frame = self.current_frame
            self.current_frame = None
        else:
            frame = self.suspended_frames.pop(clean_id, None)
        if frame is not None and not any(
            existing.scene_key == frame.scene_key for existing in self.history
        ):
            self.history.append(frame)
        return frame

    def ensure_frame(
        self,
        *,
        scene: SceneRecord | None,
        recent_chat: str,
        world_state: WorldState,
        character_manager: CharacterManager,
        contract: SessionDramaticContract | None = None,
    ) -> SceneFrame:
        location = self._location(scene, recent_chat, world_state)
        scene_name = self._scene_name(scene, recent_chat, location)
        source_scene_id = str(scene.scene_id if scene else "").strip()
        scene_key = self._scene_key(scene_name, location, source_scene_id)
        if self.current_frame and self.current_frame.scene_key == scene_key:
            self._refresh_dynamic_bits(self.current_frame, recent_chat, world_state, character_manager)
            self._apply_contract(self.current_frame, contract)
            self._sync_scene_opportunity(scene, self.current_frame)
            return self.current_frame

        previous_frame = self.current_frame
        same_source_scene = bool(
            previous_frame
            and source_scene_id
            and previous_frame.source_scene_id == source_scene_id
        )
        if previous_frame and not same_source_scene:
            self.history.append(previous_frame)
        next_frame = self._build_frame(
            scene_key=scene_key,
            scene_name=scene_name,
            location=location,
            scene=scene,
            recent_chat=recent_chat,
            world_state=world_state,
            character_manager=character_manager,
            contract=contract,
        )
        if previous_frame:
            self.inherit_transition_continuity(
                previous_frame,
                next_frame,
                scene=scene,
                same_source_scene=same_source_scene,
            )
        self.current_frame = next_frame
        self._sync_scene_opportunity(scene, self.current_frame)
        return self.current_frame

    def apply_contract_to_current(self, contract: SessionDramaticContract | None) -> None:
        if self.current_frame is None or contract is None:
            return
        self._apply_contract(self.current_frame, contract)
        self._touch(self.current_frame)

    def synchronize_current_location(self, location: str) -> bool:
        """Keep routing context aligned after a resolved player-led move.

        A formal scene record may be opened a little later, but the very next
        group message must already know where the characters physically are.
        This updates only the live camera location; ``ensure_frame`` will
        construct the new scene frame when the next turn begins.
        """

        frame = self.current_frame
        clean_location = " ".join(str(location or "").split()).strip()
        if frame is None or not clean_location or frame.location == clean_location:
            return False
        frame.location = clean_location
        self._touch(frame)
        return True

    def _inherit_location_continuity(self, previous: SceneFrame, current: SceneFrame) -> None:
        """Carry player-visible facts across new scenes at the same place."""

        for fact in previous.public_facts:
            self._append_unique(current.public_facts, fact, limit=12)
        for fact in previous.established_facts:
            self._append_unique(current.established_facts, fact, limit=10)
        for fact in previous.committed_consequences:
            self._append_unique(current.committed_consequences, fact, limit=6)
        for clue in previous.revealed_clues:
            self._append_unique(current.revealed_clues, clue, limit=12)
        for beat in previous.recent_beats[-3:]:
            self._append_unique(current.recent_beats, beat, limit=4)
        for question in previous.pending_npc_questions:
            if str(question.get("status") or "open") != "open":
                continue
            if not any(
                str(existing.get("question_id") or "")
                == str(question.get("question_id") or "")
                for existing in current.pending_npc_questions
            ):
                current.pending_npc_questions.append(dict(question))
        known_condition_ids = {
            str(item.get("condition_id") or "").strip()
            for item in current.open_conditions
            if str(item.get("condition_id") or "").strip()
        }
        for condition in previous.open_conditions:
            condition_id = str(condition.get("condition_id") or "").strip()
            if condition_id and condition_id not in known_condition_ids:
                current.open_conditions.append(dict(condition))
                known_condition_ids.add(condition_id)
        known_exchange_ids = {
            str(item.get("exchange_id") or "").strip()
            for item in current.settled_exchanges
            if str(item.get("exchange_id") or "").strip()
        }
        for exchange in previous.settled_exchanges:
            exchange_id = str(exchange.get("exchange_id") or "").strip()
            if exchange_id and exchange_id not in known_exchange_ids:
                current.settled_exchanges.append(dict(exchange))
                known_exchange_ids.add(exchange_id)
        known_commitment_ids = {
            str(item.get("commitment_id") or "").strip()
            for item in current.deferred_npc_commitments
            if str(item.get("commitment_id") or "").strip()
        }
        for commitment in previous.deferred_npc_commitments:
            commitment_id = str(commitment.get("commitment_id") or "").strip()
            if commitment_id and commitment_id not in known_commitment_ids:
                current.deferred_npc_commitments.append(dict(commitment))
                known_commitment_ids.add(commitment_id)
        if previous.last_npc_speaker:
            current.last_npc_speaker = previous.last_npc_speaker
        self._touch(current)

    def inherit_transition_continuity(
        self,
        previous: SceneFrame | None,
        current: SceneFrame | None,
        *,
        scene: SceneRecord | None = None,
        same_source_scene: bool = False,
    ) -> bool:
        """Carry one physical situation across a formal scene boundary.

        ``end_scene`` archives and clears the focused frame before the next
        scene is built.  A transition tool therefore has to retain the old
        frame explicitly and invoke this bridge after opening the next scene;
        otherwise public bargains disappear merely because the camera moved
        from one room of a location to another.
        """

        if previous is None or current is None:
            return False
        if not (
            same_source_scene
            or self._same_physical_location(previous.location, current.location)
        ):
            return False
        self._inherit_location_continuity(previous, current)
        for condition in current.open_conditions:
            self._sync_scene_condition(scene, condition)
        return True

    @staticmethod
    def _same_physical_location(left: str, right: str) -> bool:
        def normalize(value: str) -> str:
            return re.sub(r"[\s，,。；;：:]+", "", str(value or "")).strip()

        def parent(value: str) -> str:
            # A scene may move from an area's entrance to one of its rooms.
            # Only explicit location delimiters establish that relationship;
            # loose substring matching would wrongly merge nearby places.
            return normalize(re.split(r"[·•／/＞>]", str(value or ""), maxsplit=1)[0])

        left_normalized = normalize(left)
        right_normalized = normalize(right)
        if not left_normalized or not right_normalized:
            return False
        if left_normalized == right_normalized:
            return True
        left_parent = parent(left)
        right_parent = parent(right)
        return bool(
            left_parent
            and left_parent == right_parent
            and (left_parent != left_normalized or right_parent != right_normalized)
        )

    def update_from_resolution(
        self,
        resolution: ActionResolution,
        *,
        scene: SceneRecord | None = None,
    ) -> None:
        frame = self.current_frame
        if not frame:
            return
        pending_clock_facts = list(
            resolution.payload.get("_pending_clock_public_facts") or []
        )
        for change in [
            *list(resolution.payload.get("auto_clock_changes") or []),
            *([resolution.payload["clock_change"]] if resolution.payload.get("clock_change") else []),
        ]:
            before = int(getattr(change, "before", 0) or 0)
            after = int(getattr(change, "after", 0) or 0)
            maximum = int(getattr(change, "max_segments", 0) or 0)
            if maximum <= 0 or before >= maximum or after < maximum:
                continue
            consequence = str(
                getattr(change, "completion_consequence", "")
                or getattr(change, "stakes", "")
                or ""
            ).strip()
            consequence = re.sub(r"^(?:若|当)?(?:命刻)?填满(?:后|时)?[，,:：]?\s*", "", consequence)
            clock_type = str(getattr(change, "clock_type", "") or "").strip()
            if consequence:
                fact = consequence.rstrip("。") + "。"
            elif clock_type == "objective":
                fact = "目标已经达成。"
            elif clock_type == "ritual":
                fact = "仪式准备完成。"
            else:
                fact = "威胁已经兑现。"
            if fact not in pending_clock_facts:
                pending_clock_facts.append(fact)
            # The GM must know the committed consequence before composing the
            # reply that reveals it. Player-facing fact ledgers remain deferred
            # until ``publish_resolution_information`` confirms delivery.
            self._append_unique(frame.committed_consequences, fact, limit=6)
            frame.current_pressure = fact
        if pending_clock_facts:
            # A rules consequence is authoritative once the transaction
            # commits, but it becomes player knowledge only after the final
            # group-chat reply actually contains it.
            resolution.payload["_pending_clock_public_facts"] = pending_clock_facts
        committed_source = resolution.payload.get("committed_source_action")
        action = committed_source if isinstance(committed_source, Action) else resolution.action
        for item in action.parameters.get("committed_public_facts") or []:
            fact = " ".join(str(item or "").split()).strip()
            if not fact:
                continue
            self._append_unique(frame.established_facts, fact, limit=10)
            self._append_unique(frame.public_facts, fact, limit=12)
            self._append_unique(frame.committed_consequences, fact, limit=6)
        if action.action_type == ActionType.ACCEPT_STORY_CHANGE:
            fact = str(resolution.payload.get("fact") or "").strip()
            if fact:
                self._append_unique(frame.established_facts, fact, limit=10)
                self._append_unique(frame.public_facts, fact, limit=12)
                self._append_unique(frame.clue_pool, f"玩家花费物语点确认：{fact}", limit=8)
            followup = str(resolution.payload.get("followup_intent") or "").strip()
            if followup:
                self._append_unique(frame.unresolved_requests, followup, limit=6)
                self._append_unique(frame.open_questions, followup, limit=8)
        elif action.action_type == ActionType.NARRATE and action.parameters.get("establish_fact"):
            summary = self._clean_persistent_fact(
                resolution.payload.get("summary") or action.parameters.get("summary") or ""
            )
            if summary and not action.parameters.get("scene_open_request"):
                self._append_unique(frame.established_facts, summary, limit=10)
                self._append_unique(frame.public_facts, summary, limit=12)
                if action.parameters.get("material_change"):
                    self._append_unique(frame.committed_consequences, summary, limit=6)
            for fact in action.parameters.get("public_facts") or []:
                clean = self._clean_persistent_fact(fact)
                if not clean:
                    continue
                self._append_unique(frame.established_facts, clean, limit=10)
                self._append_unique(frame.public_facts, clean, limit=12)
                if action.parameters.get("material_change"):
                    self._append_unique(frame.committed_consequences, clean, limit=6)
            if action.parameters.get("npc_answer_generated"):
                self._record_npc_answer_state(frame, action, summary=summary, scene=scene)
        elif action.action_type == ActionType.NARRATE and action.parameters.get("npc_answer_generated"):
            summary = self._clean_persistent_fact(
                resolution.payload.get("summary") or action.parameters.get("summary") or ""
            )
            self._record_npc_answer_state(frame, action, summary=summary, scene=scene)
        elif action.action_type in {ActionType.INVESTIGATE, ActionType.REQUEST_ROLL, ActionType.OBJECTIVE}:
            # Runtime commits happen before expression.  Do not let a clue
            # become "public" merely because it exists in the rules payload;
            # ``publish_resolution_information`` records it after the final
            # player-facing reply has demonstrably included it.  Direct manager
            # callers retain the old immediate behavior for small isolated
            # tools/tests that have no separate rendering phase.
            if not resolution.payload.get("_defer_public_information"):
                self._publish_information_items(
                    frame,
                    action,
                    resolution.payload.get("information") or [],
                )
            roll = resolution.payload.get("roll")
            if roll is not None and not getattr(roll, "success", True):
                self._append_unique(
                    frame.clarity_notes,
                    "最近一次检定失败；表达时应说明失败如何发生、代价是什么，且不要让关键线索凭空消失。",
                    limit=6,
                )
            if resolution.payload.get("clock_change") or resolution.payload.get("clock_progress"):
                self._append_unique(
                    frame.telegraphed_threats,
                    "当前命刻已进入镜头焦点；每次行动后可以表现其剩余压力，但只有完整行动轮结束时才自动改变进度。",
                    limit=6,
                )
        self._touch(frame)

    def publish_resolution_information(
        self,
        resolution: ActionResolution,
        *,
        public_reply: str,
    ) -> list[str]:
        """Move delivered check facts into the table-visible scene ledger.

        The caller supplies the final message after every prose/sanitizer stage.
        Matching is intentionally exact after whitespace normalization.  A
        semantic paraphrase is not enough to silently grant players a more
        detailed canonical clue than the words they actually received.
        """

        frame = self.current_frame
        if frame is None:
            return []
        committed_source = resolution.payload.get("committed_source_action")
        action = committed_source if isinstance(committed_source, Action) else resolution.action
        information_action = action.action_type in {
            ActionType.INVESTIGATE,
            ActionType.REQUEST_ROLL,
            ActionType.OBJECTIVE,
        }
        reply = " ".join(str(public_reply or "").split())
        delivered: list[str] = []
        if information_action:
            delivered.extend(
                " ".join(str(item or "").split()).strip()
                for item in (resolution.payload.get("information") or [])
                if " ".join(str(item or "").split()).strip()
                and " ".join(str(item or "").split()).strip() in reply
            )
            if delivered:
                self._publish_information_items(frame, action, delivered)

        normalized_reply = self._normalize_public_match(reply)
        for raw_fact in resolution.payload.get("_pending_clock_public_facts") or []:
            fact = " ".join(str(raw_fact or "").split()).strip()
            if not fact or self._normalize_public_match(fact) not in normalized_reply:
                continue
            self._append_unique(frame.committed_consequences, fact, limit=6)
            self._append_unique(frame.established_facts, fact, limit=10)
            self._append_unique(frame.public_facts, fact, limit=12)
            frame.current_pressure = fact
            if fact not in delivered:
                delivered.append(fact)
        if delivered:
            self._touch(frame)
        return delivered

    @staticmethod
    def _normalize_public_match(value: object) -> str:
        return re.sub(
            r"[^0-9A-Za-z\u4e00-\u9fff]+",
            "",
            str(value or ""),
        ).lower()

    def _publish_information_items(
        self,
        frame: SceneFrame,
        action: Action,
        items: object,
    ) -> None:
        published = False
        for item in items if isinstance(items, (list, tuple)) else []:
            text = " ".join(str(item or "").split()).strip()
            if not text:
                continue
            self._append_unique(frame.established_facts, text, limit=10)
            self._append_unique(frame.public_facts, text, limit=12)
            self._append_unique(frame.revealed_clues, text, limit=12)
            published = True
        if published:
            self._resolve_matching_request(frame, action)

    def latest_open_condition(self, *, npc: str = "") -> dict[str, str] | None:
        frame = self.current_frame
        if frame is None:
            return None
        clean_npc = str(npc or "").strip()
        for condition in reversed(frame.open_conditions):
            if str(condition.get("status") or "open") != "open":
                continue
            if clean_npc and str(condition.get("npc") or "").strip() != clean_npc:
                continue
            return condition
        return None

    def resolve_condition(
        self,
        condition_id: str,
        *,
        scene: SceneRecord | None = None,
        actor: str = "",
        public_evidence: str = "",
    ) -> dict[str, str] | None:
        clean_id = str(condition_id or "").strip()
        frame = self.current_frame
        if not clean_id or frame is None:
            return None
        resolved: dict[str, str] | None = None
        for condition in frame.open_conditions:
            if str(condition.get("condition_id") or "") != clean_id:
                continue
            if not self.condition_is_available_to_actor(condition, actor):
                return None
            condition["player_fulfillment"] = "fulfilled"
            condition["status"] = "resolved"
            resolved = condition
            break
        if resolved is None:
            return None

        # Several conversations may expose alternative ways to earn the same
        # promise. Once one route succeeds, the NPC must not reopen another
        # sibling route and demand the same price again.
        promise_key = str(resolved.get("promise_key") or "").strip()
        resolved_ids: set[str] = {clean_id}
        if promise_key:
            for condition in frame.open_conditions:
                if str(condition.get("npc") or "").strip() != str(resolved.get("npc") or "").strip():
                    continue
                if str(condition.get("promise_key") or "").strip() != promise_key:
                    continue
                condition["player_fulfillment"] = "fulfilled"
                condition["status"] = "resolved"
                resolved_ids.add(str(condition.get("condition_id") or ""))
        if scene is not None:
            for condition in frame.open_conditions:
                if str(condition.get("condition_id") or "") in resolved_ids:
                    self._sync_scene_condition(scene, condition)

        promised_result = str(resolved.get("promised_result") or "").strip()
        npc = str(resolved.get("npc") or "").strip()
        if promised_result:
            fact = " ".join(str(public_evidence or "").split()).strip()
            if not fact:
                fact = f"{npc}已经兑现承诺：{promised_result.rstrip('。')}。" if npc else f"承诺已经兑现：{promised_result.rstrip('。')}。"
            self._append_unique(frame.established_facts, fact, limit=10)
            self._append_unique(frame.public_facts, fact, limit=12)
        self._clear_pressure_resolved_by_condition(frame, resolved)
        if self.session_ledger is not None:
            self.session_ledger.record_fulfilled_promise(resolved)
        self._touch(frame)
        return resolved

    def mark_condition_fulfilled(
        self,
        condition_id: str,
        *,
        scene: SceneRecord | None = None,
        actor: str = "",
        public_evidence: str = "",
    ) -> dict[str, str] | None:
        """Record that the heroes paid the price while the NPC still owes.

        A fulfilled player obligation is not the same event as delivery of
        the promised concession.  Keeping ``status`` open until the payoff is
        public lets the tool agent require the owning NPC's response without
        asking the player to repeat the action or falsely claiming the gate,
        item or permission has already been delivered.
        """

        clean_id = str(condition_id or "").strip()
        clean_actor = " ".join(str(actor or "").split()).strip()
        frame = self.current_frame
        if not clean_id or frame is None:
            return None
        fulfilled: dict[str, str] | None = None
        for condition in frame.open_conditions:
            if str(condition.get("condition_id") or "").strip() != clean_id:
                continue
            if str(condition.get("status") or "open") != "open":
                return None
            if not self.condition_is_available_to_actor(condition, clean_actor):
                return None
            condition["player_fulfillment"] = "fulfilled"
            if clean_actor:
                condition["fulfilled_by"] = clean_actor
            evidence = " ".join(str(public_evidence or "").split()).strip()
            if evidence:
                condition["fulfillment_evidence"] = evidence[:500]
            fulfilled = condition
            break
        if fulfilled is None:
            return None
        self._sync_scene_condition(scene, fulfilled)
        self._touch(frame)
        return fulfilled

    def supersede_open_conditions(
        self,
        npc: str,
        *,
        reason: str,
        scene: SceneRecord | None = None,
    ) -> list[dict[str, str]]:
        """Retire an NPC's old bargain after an explicit public revision.

        Superseded conditions are not fulfilled and must not create a promised
        payoff. They remain in the audit trail so a later NPC call cannot revive
        them as if the table had never heard the revision.
        """

        frame = self.current_frame
        clean_npc = " ".join(str(npc or "").split()).strip()
        clean_reason = " ".join(str(reason or "").split()).strip()
        if frame is None or not clean_npc:
            return []
        changed: list[dict[str, str]] = []
        changed_ids: set[str] = set()
        for condition in frame.open_conditions:
            if str(condition.get("status") or "open") != "open":
                continue
            if not self._same_contract_npc(
                self._compact_contract_text(clean_npc),
                self._compact_contract_text(condition.get("npc")),
            ):
                continue
            condition["status"] = "superseded"
            condition["superseded_by"] = clean_reason[:300]
            changed.append(condition)
            changed_ids.add(str(condition.get("condition_id") or ""))
        if scene is not None and changed_ids:
            for condition in scene.open_conditions:
                if str(condition.get("condition_id") or "") not in changed_ids:
                    continue
                condition["status"] = "superseded"
                condition["superseded_by"] = clean_reason[:300]
        if changed:
            self._touch(frame)
        return changed

    def record_condition(
        self,
        *,
        npc: str,
        condition: str,
        promised_result: str = "",
        promise_kind: str = "",
        promise_subject: str = "",
        required_actor: str = "",
        scene: SceneRecord | None = None,
        replace_existing: bool = False,
    ) -> dict[str, str] | None:
        """Persist an NPC condition that has already been made public."""

        frame = self.current_frame
        clean_npc = " ".join(str(npc or "").split()).strip()
        clean_condition = " ".join(str(condition or "").split()).strip()
        supplied_result = " ".join(str(promised_result or "").split()).strip()
        clean_kind = str(promise_kind or "").strip().lower()
        clean_subject = " ".join(str(promise_subject or "").split()).strip()
        clean_required_actor = " ".join(str(required_actor or "").split()).strip()
        if frame is None or not clean_npc or not clean_condition:
            return None
        clean_result = self._promised_result(clean_condition, supplied_result)
        if not clean_result:
            # Pressure, advice and threats are not finite bargains. Keeping
            # them in the open-condition lifecycle makes later real offers
            # impossible to record and teaches the GM to wait for a promise
            # that was never made.
            return None
        if is_nonfinal_promise_result(clean_result):
            # A process such as "continue reviewing" is not a concession. It
            # must not occupy the one open-condition slot for this NPC or the
            # table will be forced to fulfil a promise with no payoff.
            return None
        clean_subject = clean_subject or self._promise_subject(clean_result or clean_condition)
        promise_key = self._promise_key(
            clean_result or clean_condition,
            kind=clean_kind,
            subject=clean_subject,
        )

        if self.session_ledger is not None:
            fulfilled = self.session_ledger.find_fulfilled_promise(
                npc=clean_npc,
                promise_key=promise_key,
                promise_subject=clean_subject,
                promised_result=clean_result,
            )
            if fulfilled is not None:
                resolved = dict(fulfilled)
                resolved["status"] = "resolved"
                if not any(
                    str(item.get("condition_id") or "")
                    == str(resolved.get("condition_id") or "")
                    and str(item.get("npc") or "") == clean_npc
                    for item in frame.open_conditions
                ):
                    frame.open_conditions.append(resolved)
                self._sync_scene_condition(scene, resolved)
                fact = f"{clean_npc}已经兑现承诺：{clean_result.rstrip('。')}。"
                self._append_unique(frame.established_facts, fact, limit=10)
                self._append_unique(frame.public_facts, fact, limit=12)
                self._touch(frame)
                return resolved

        # Never reopen an already fulfilled bargain merely because a later
        # model call paraphrased its condition.
        for existing in reversed(frame.open_conditions):
            if str(existing.get("npc") or "").strip() != clean_npc:
                continue
            existing_key = str(existing.get("promise_key") or "").strip()
            existing_subject = str(existing.get("promise_subject") or "").strip()
            same_subject = bool(clean_subject and existing_subject == clean_subject)
            if (
                str(existing.get("status") or "open") != "open"
                and ((promise_key and existing_key == promise_key) or same_subject)
            ):
                return existing
        open_for_npc = next(
            (
                existing
                for existing in reversed(frame.open_conditions)
                if str(existing.get("status") or "open") == "open"
                and str(existing.get("npc") or "").strip() == clean_npc
            ),
            None,
        )
        if open_for_npc is not None:
            # One finite bargain at a time. A paraphrase may clarify the same
            # bargain, but an NPC cannot stack an endless ladder of gates while
            # the first promise is still pending.
            if replace_existing:
                open_for_npc["condition"] = clean_condition
                open_for_npc["promised_result"] = clean_result
                open_for_npc["promise_key"] = promise_key
                open_for_npc["promise_kind"] = clean_kind
                open_for_npc["promise_subject"] = clean_subject
                if clean_required_actor:
                    open_for_npc["required_actor"] = clean_required_actor
                self._sync_scene_condition(scene, open_for_npc)
                self._touch(frame)
                return open_for_npc
            if clean_result and not str(open_for_npc.get("promised_result") or "").strip():
                open_for_npc["promised_result"] = clean_result
            if promise_key and not str(open_for_npc.get("promise_key") or "").strip():
                open_for_npc["promise_key"] = promise_key
            if clean_kind and not str(open_for_npc.get("promise_kind") or "").strip():
                open_for_npc["promise_kind"] = clean_kind
            if clean_subject and not str(open_for_npc.get("promise_subject") or "").strip():
                open_for_npc["promise_subject"] = clean_subject
            if clean_required_actor and not str(open_for_npc.get("required_actor") or "").strip():
                open_for_npc["required_actor"] = clean_required_actor
                self._sync_scene_condition(scene, open_for_npc)
                self._touch(frame)
            return open_for_npc
        for existing in reversed(frame.open_conditions):
            if (
                str(existing.get("status") or "open") == "open"
                and str(existing.get("npc") or "").strip() == clean_npc
                and (
                    str(existing.get("condition") or "").strip() == clean_condition
                    or (
                        promise_key
                        and str(existing.get("promise_key") or "").strip() == promise_key
                    )
                )
            ):
                if clean_result and not str(existing.get("promised_result") or "").strip():
                    existing["promised_result"] = clean_result
                if promise_key and not str(existing.get("promise_key") or "").strip():
                    existing["promise_key"] = promise_key
                return existing
        recorded = {
            "condition_id": f"{frame.scene_key}-condition-{len(frame.open_conditions) + 1}",
            "npc": clean_npc,
            "condition": clean_condition,
            "promised_result": clean_result,
            "promise_key": promise_key,
            "promise_kind": clean_kind,
            "promise_subject": clean_subject,
            "required_actor": clean_required_actor,
            "player_fulfillment": "pending",
            "status": "open",
        }
        frame.open_conditions.append(recorded)
        if scene is not None and not any(
            str(item.get("condition_id") or "") == recorded["condition_id"]
            for item in scene.open_conditions
        ):
            scene.open_conditions.append(dict(recorded))
        self._touch(frame)
        return recorded

    @staticmethod
    def condition_is_available_to_actor(
        condition: dict[str, str],
        actor: str = "",
    ) -> bool:
        """Return whether this hero may satisfy an actor-bound condition.

        Most scene conditions are group-wide.  A direct identity or agency
        challenge is different: an NPC can require one named hero to answer
        for themselves, and a teammate must not be able to settle it by
        speaking first.  Empty ``required_actor`` preserves legacy/group
        conditions unchanged.
        """

        required = " ".join(str(condition.get("required_actor") or "").split()).strip()
        current = " ".join(str(actor or "").split()).strip()
        return not required or bool(current and current == required)

    @staticmethod
    def _sync_scene_condition(
        scene: SceneRecord | None,
        condition: dict[str, str],
    ) -> None:
        if scene is None:
            return
        condition_id = str(condition.get("condition_id") or "")
        for index, existing in enumerate(scene.open_conditions):
            if str(existing.get("condition_id") or "") == condition_id:
                scene.open_conditions[index] = dict(condition)
                return
        scene.open_conditions.append(dict(condition))

    @staticmethod
    def _promised_result(condition: str, supplied: str) -> str:
        """Extract the NPC's future concession, not the whole spoken answer."""

        # A structured speech plan has already separated the price from the
        # concession.  Prefer that explicit field before attempting to parse a
        # natural-language condition.  Parsing the condition first used to
        # turn ``只要签字，或把遗物放进盒中，就算满足`` into the nonsensical
        # promise ``或把遗物放进盒中，就算满足`` and discarded the real
        # concession supplied by the NPC planner.
        supplied_clean = str(supplied or "").strip(" ：:，,。；;‘’'\"")
        if supplied_clean and not SceneFrameManager._is_threat_not_concession(supplied_clean):
            return supplied_clean[:160]

        candidates = [str(condition or "").strip()]
        patterns = (
            r"(?:只要|如果|若|一旦|等到)[^。；;]{1,120}?[，,](?:我|我们|这边|守望会)?(?:就|便|会|才|立刻|马上|随后)?(?P<result>[^。；;]{2,100})",
            r"(?:完成|做到|带回|交出|证明)[^。；;]{1,100}?(?:后|之后|以后)[，,]?(?P<result>[^。；;]{2,100})",
        )
        for candidate in candidates:
            for pattern in patterns:
                match = re.search(pattern, candidate)
                if not match:
                    continue
                result = match.group("result").strip(" ：:，,。；;‘’'\"")
                result = re.sub(r"^(?:我|我们|这边|守望会)(?:就|便|会|才|立刻|马上|随后)?", "", result).strip()
                if result and not SceneFrameManager._is_threat_not_concession(result):
                    return result[:100]
        return ""

    @staticmethod
    def _is_threat_not_concession(value: str) -> bool:
        """Do not persist an NPC's threatened punishment as their promise."""

        text = str(value or "").strip()
        if not text:
            return False
        punitive = bool(
            re.search(
                r"(?:并入|列入|转入|上报|通报|扣押|逮捕|拘留|封锁|封控|惩处|处罚|逐间登记|强制登记|攻击|处决)",
                text,
            )
        )
        withdrawal = bool(
            re.search(
                r"(?:不再|不会|免于|取消|解除|停止|暂停|终止|撤销|撤回|暂缓|放弃)"
                r".{0,12}(?:上报|扣押|封锁|处罚|登记|攻击)",
                text,
            )
        )
        return punitive and not withdrawal

    @staticmethod
    def _promise_key(value: str, *, kind: str = "", subject: str = "") -> str:
        clean = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(value or "")).lower()
        if not clean:
            return ""
        normalized_kind = str(kind or "").strip().lower()
        normalized_subject = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(subject or "")).lower()
        if normalized_kind and normalized_kind != "none":
            return f"{normalized_kind}:{normalized_subject or clean[:32]}"
        if re.search(r"(?:开门|放行|开放.{0,8}(?:门|路|通道)|打开.{0,8}(?:门|路|通道)|让.{0,8}(?:进去|进入|通过))", clean):
            return "access_granted"
        if re.search(r"(?:带路|领路|护送|派.{0,8}(?:向导|巡守|人).{0,6}带路)", clean):
            return "escort_or_guide"
        if re.search(r"(?:交出|给出|提供|归还).{0,20}(?:钥匙|物品|证据|名册|地图|通行证)", clean):
            return "provide_item:" + clean[-24:]
        return clean[:64]

    @staticmethod
    def _promise_subject(value: str) -> str:
        """Best-effort legacy normalization for plans without structured keys."""

        clean = " ".join(str(value or "").split()).strip()
        if not clean:
            return ""
        explicit = re.search(
            r"(?P<subject>[\u4e00-\u9fffA-Za-z0-9]{2,18}(?:后院|旧路|通道|侧门|大门|闸门|房间|档案|账册|名册|地图|钥匙|证据|真相|消息|情报))",
            clean,
        )
        if explicit:
            subject = explicit.group("subject")
            subject = re.sub(r"^(?:进入|开放|打开|带去|带到|说明|说清|交出|提供|关于|有关)", "", subject)
            return subject[-18:]
        return ""

    def _record_npc_condition(
        self,
        frame: SceneFrame,
        action,
        *,
        scene: SceneRecord | None,
    ) -> dict[str, str] | None:
        fulfillment = str(action.parameters.get("condition_fulfillment") or "none").strip().lower()
        if fulfillment not in {"", "none"}:
            # This speech evaluates an existing bargain. Even an incomplete
            # answer must update that bargain rather than opening a duplicate.
            return None
        plan = action.parameters.get("npc_speech_plan")
        if isinstance(plan, dict) and is_current_action_permission_bargain(
            str(action.parameters.get("npc_player_message") or ""),
            plan,
        ):
            # Permission for an action already declared this turn must never
            # become a second, self-referential scene bargain.
            return None
        if isinstance(plan, dict) and str(plan.get("proposal_outcome") or "none") in {
            "accepted",
            "rejected",
        }:
            # This answer closes the proposal currently on the table. It is not
            # another price the players must pay later.
            return None
        prepared_offered = bool(action.parameters.get("prepared_bargain_offered"))
        retroactively_fulfilled = bool(
            action.parameters.get("new_condition_retroactively_fulfilled")
        )
        if not isinstance(plan, dict) and not prepared_offered and not retroactively_fulfilled:
            return None
        plan = dict(plan or {})
        if not prepared_offered and not retroactively_fulfilled:
            speech_act = str(plan.get("speech_act") or "").strip()
            if speech_act not in {"", "condition"}:
                return None
            if str(plan.get("condition_outcome") or "none").strip() != "none":
                return None
        text = " ".join(
            str(
                action.parameters.get("new_condition_public_condition")
                or action.parameters.get("prepared_bargain_public_condition")
                or plan.get("condition")
                or ""
            ).split()
        ).strip()
        npc = str(action.parameters.get("npc_answer_target") or frame.last_npc_speaker or "").strip()
        if not text or not npc:
            return None
        promised_result = " ".join(
            str(
                action.parameters.get("new_condition_promised_result")
                or action.parameters.get("prepared_bargain_promised_result")
                or plan.get("promised_result")
                or ""
            ).split()
        ).strip()
        promised_result = self._canonical_prepared_promised_result(
            frame,
            npc=npc,
            condition=text,
            supplied_result=promised_result,
        )
        if not promised_result:
            promised_result = self._promised_result(
                text,
                " ".join(str(plan.get("direct_answer") or "").split()).strip(),
            )
            promised_result = self._canonical_prepared_promised_result(
                frame,
                npc=npc,
                condition=text,
                supplied_result=promised_result,
            )
        if not promised_result:
            # A request without a finite concession is guidance or pressure,
            # not a bargain the scene lifecycle should keep reopening.
            return None
        return self.record_condition(
            npc=npc,
            condition=text,
            promised_result=promised_result,
            promise_kind=str(
                action.parameters.get("prepared_bargain_promise_kind")
                or plan.get("promise_kind")
                or ""
            ),
            promise_subject=str(
                action.parameters.get("prepared_bargain_promise_subject")
                or plan.get("promise_subject")
                or ""
            ),
            # A public bargain applies to the party unless an upstream semantic
            # contract explicitly supplies a responsible actor.  Inferring this
            # from words such as "姓名" or "关系" used to bind unrelated
            # conditions to whichever player happened to speak first.
            required_actor=str(
                action.parameters.get("prepared_bargain_required_actor") or ""
            ).strip(),
            scene=scene,
            replace_existing=bool(action.parameters.get("prepared_bargain_bound")),
        )

    @classmethod
    def _canonical_prepared_promised_result(
        cls,
        frame: SceneFrame,
        *,
        npc: str,
        condition: str,
        supplied_result: str,
    ) -> str:
        """Recover a prepared concession when voice output repeats its price.

        The prepared result is only authoritative for the same NPC and a
        recognizably identical bargain. A genuinely new public bargain remains
        untouched, so scene play can still revise unrevealed preparation.
        """

        supplied = " ".join(str(supplied_result or "").split()).strip()
        clean_npc = cls._compact_contract_text(npc)
        clean_condition = cls._compact_contract_text(condition)
        if not clean_npc or not clean_condition:
            return supplied
        for record in frame.session_npc_records:
            prepared_npc = cls._compact_contract_text(record.get("name"))
            if not cls._same_contract_npc(clean_npc, prepared_npc):
                continue
            demand = cls._compact_contract_text(record.get("concrete_demand"))
            acceptance = cls._compact_contract_text(record.get("acceptance_rule"))
            canonical = " ".join(str(record.get("promised_result") or "").split()).strip()
            if not canonical or not cls._same_prepared_bargain(clean_condition, demand, acceptance):
                continue
            clean_supplied = cls._compact_contract_text(supplied)
            if not clean_supplied:
                return canonical
            price = f"{demand}{acceptance}"
            if clean_supplied in price:
                return canonical
        return supplied

    @staticmethod
    def _compact_contract_text(value: object) -> str:
        return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(value or "")).lower()

    @staticmethod
    def _same_contract_npc(left: str, right: str) -> bool:
        if not left or not right:
            return False
        if left == right or (min(len(left), len(right)) >= 3 and (left in right or right in left)):
            return True
        roles = (
            "使者",
            "旅人",
            "会长",
            "守门人",
            "巡守",
            "监察官",
            "钟匠",
            "掌柜",
            "向导",
        )
        shared_role = next(
            (role for role in roles if left.endswith(role) and right.endswith(role)),
            "",
        )
        if not shared_role:
            return False
        left_core = left[: -len(shared_role)]
        right_core = right[: -len(shared_role)]
        longest = SequenceMatcher(None, left_core, right_core).find_longest_match(
            0,
            len(left_core),
            0,
            len(right_core),
        )
        return longest.size >= 4

    @staticmethod
    def _bare_contract_role_alias_matches(left: str, right: str) -> bool:
        """Match a role-only nickname only when the caller checks uniqueness."""

        if not left or not right:
            return False
        roles = (
            "使者",
            "旅人",
            "会长",
            "守门人",
            "巡守",
            "监察官",
            "钟匠",
            "掌柜",
            "向导",
        )
        return any(
            (left == role and right.endswith(role))
            or (right == role and left.endswith(role))
            for role in roles
        )

    @staticmethod
    def _same_prepared_bargain(condition: str, demand: str, acceptance: str) -> bool:
        anchors = [item for item in (demand, acceptance) if len(item) >= 6]
        if not anchors:
            return False
        return any(anchor in condition or condition in anchor for anchor in anchors)

    def _record_npc_answer_state(
        self,
        frame: SceneFrame,
        action: Action,
        *,
        summary: str,
        scene: SceneRecord | None,
    ) -> None:
        """Persist the public answer and any finite bargain it contains.

        NPC voice renderers may also mark an answer as an established scene
        fact.  That generic flag must not bypass the NPC-specific lifecycle,
        otherwise the spoken condition disappears before the next player can
        act on it.
        """

        clean_summary = self._clean_persistent_fact(summary)
        if clean_summary:
            self._append_unique(frame.established_facts, clean_summary, limit=10)
            self._append_unique(frame.public_facts, clean_summary, limit=12)
            self._resolve_matching_request(frame, action)
        target = str(action.parameters.get("npc_answer_target") or "").strip()
        addressed_actor = str(
            action.parameters.get("npc_answer_addressed_actor")
            or action.parameters.get("actor")
            or ""
        ).strip()
        if target:
            frame.last_npc_speaker = target
        if isinstance(plan := action.parameters.get("npc_speech_plan"), dict):
            settlement = self._record_settlement_from_plan(
                npc=target,
                player_offer=str(action.parameters.get("npc_player_message") or ""),
                npc_response=clean_summary,
                plan=plan,
            )
            self._attach_settlement_to_action(action, settlement)
            self.npc_deferred_commitment_manager.update_from_public_answer(
                frame,
                npc=target,
                public_statement=clean_summary,
                speech_plan=plan,
            )
        for item in action.parameters.get("multi_npc_speech_plans") or []:
            if not isinstance(item, dict) or not isinstance(item.get("plan"), dict):
                continue
            settlement = self._record_settlement_from_plan(
                npc=str(item.get("npc") or ""),
                player_offer=str(item.get("player_message") or ""),
                npc_response=str(item.get("reply") or ""),
                plan=dict(item["plan"]),
            )
            self._attach_settlement_to_action(action, settlement)
            self.npc_deferred_commitment_manager.update_from_public_answer(
                frame,
                npc=str(item.get("npc") or ""),
                public_statement=str(item.get("reply") or ""),
                speech_plan=dict(item["plan"]),
            )
        self._record_npc_condition(frame, action, scene=scene)
        resolved_condition_id = str(action.parameters.get("resolved_scene_condition_id") or "").strip()
        if resolved_condition_id:
            self.resolve_condition(
                resolved_condition_id,
                scene=scene,
                actor=addressed_actor,
            )

    def _record_settlement_from_plan(
        self,
        *,
        npc: str,
        player_offer: str,
        npc_response: str,
        plan: dict[str, object],
    ) -> dict[str, str] | None:
        outcome = str(plan.get("proposal_outcome") or "none").strip().lower()
        if outcome not in {"accepted", "rejected"}:
            return None
        return self.record_settled_exchange(
            npc=npc,
            player_offer=player_offer,
            npc_response=npc_response,
            outcome=outcome,
            settled_terms=str(plan.get("settled_terms") or ""),
        )

    @staticmethod
    def _attach_settlement_to_action(
        action: Action,
        settlement: dict[str, str] | None,
    ) -> None:
        """Expose a completed public bargain to downstream episode tracking."""

        if not settlement:
            return
        action.parameters["settled_exchange_id"] = settlement.get("exchange_id", "")
        action.parameters["settled_exchange_outcome"] = settlement.get("outcome", "")
        action.parameters["settled_exchange_terms"] = settlement.get("settled_terms", "")
        action.parameters["settled_exchange_player_performance"] = settlement.get(
            "player_performance",
            "pending",
        )
        action.parameters["settled_exchange_npc"] = settlement.get("npc", "")

    def record_gm_beat(self, text: str) -> None:
        """Keep a proactive beat available for pronoun resolution and continuity."""

        frame = self.current_frame
        clean = " ".join(str(text or "").split()).strip()
        if not frame or not clean:
            return
        # A beat is already public, but it is not automatically an
        # investigation clue. Keep it in a dedicated continuity buffer so a
        # later successful check cannot reward players by replaying old prose.
        self._append_unique(frame.recent_beats, clean[:400], limit=4)
        speaker = self._mentioned_npc_from_frame(frame, clean)
        if speaker:
            frame.last_npc_speaker = speaker
        self._touch(frame)

    def record_settled_exchange(
        self,
        *,
        npc: str,
        player_offer: str,
        npc_response: str,
        outcome: str,
        settled_terms: str,
    ) -> dict[str, str] | None:
        """Persist an NPC's final answer to a player proposal.

        A settled exchange is not an open condition. It records that an NPC has
        accepted or rejected a particular arrangement so later proactive beats
        cannot silently reopen the same negotiation.
        """

        frame = self.current_frame
        clean_npc = " ".join(str(npc or "").split()).strip()
        clean_offer = " ".join(str(player_offer or "").split()).strip()
        clean_response = " ".join(str(npc_response or "").split()).strip()
        clean_terms = " ".join(str(settled_terms or "").split()).strip()
        clean_outcome = str(outcome or "").strip().lower()
        if (
            frame is None
            or not clean_npc
            or not clean_offer
            or not clean_terms
            or clean_outcome not in {"accepted", "rejected"}
        ):
            return None
        for existing in reversed(frame.settled_exchanges):
            if not self._same_settled_exchange(
                existing,
                npc=clean_npc,
                outcome=clean_outcome,
                settled_terms=clean_terms,
            ):
                continue
            existing["npc"] = clean_npc
            existing["settled_terms"] = clean_terms[:300]
            conditional_terms = self._conditional_exchange_terms(clean_terms)
            if conditional_terms["condition"]:
                existing["condition"] = conditional_terms["condition"]
            else:
                existing.setdefault("condition", "")
            if conditional_terms["promised_result"]:
                existing["promised_result"] = conditional_terms["promised_result"]
            else:
                existing.setdefault("promised_result", "")
            if clean_response:
                existing["npc_response"] = clean_response[:500]
            existing["player_offer"] = clean_offer[:500]
            if (
                not conditional_terms["condition"]
                or self._player_offer_performed(clean_offer)
            ):
                existing["player_performance"] = "complete"
            else:
                existing.setdefault("player_performance", "pending")
            self._touch(frame)
            return existing
        conditional_terms = self._conditional_exchange_terms(clean_terms)
        recorded = {
            "exchange_id": f"{frame.scene_key}-exchange-{len(frame.settled_exchanges) + 1}",
            "npc": clean_npc,
            "outcome": clean_outcome,
            "settled_terms": clean_terms[:300],
            "player_offer": clean_offer[:500],
            "npc_response": clean_response[:500],
            # Some NPC conversations settle a finite bargain without using the
            # structured ``condition`` field.  Preserve its price and payoff
            # separately so the next player action can actually complete it
            # instead of relying on the NPC model to remember a prose clause.
            "condition": conditional_terms["condition"],
            "promised_result": conditional_terms["promised_result"],
            "player_performance": (
                "complete"
                if (
                    not conditional_terms["condition"]
                    or self._player_offer_performed(clean_offer)
                )
                else "pending"
            ),
        }
        frame.settled_exchanges.append(recorded)
        if len(frame.settled_exchanges) > 12:
            frame.settled_exchanges[:] = frame.settled_exchanges[-12:]
        self._touch(frame)
        return recorded

    def pending_settled_exchanges(
        self,
        frame: SceneFrame | None = None,
    ) -> list[dict[str, str]]:
        """Return accepted bargains whose player-side cost is still unpaid.

        Agreement and performance are separate fictional events.  Keeping this
        distinction explicit prevents an accepted price from being mistaken
        for a completed scene payoff or an earned session ending.
        """

        active = frame or self.current_frame
        if active is None:
            return []
        return [
            item
            for item in active.settled_exchanges
            if str(item.get("outcome") or "").strip().lower() == "accepted"
            and bool(self.settled_exchange_condition(item))
            and str(item.get("player_performance") or "pending").strip().lower()
            != "complete"
        ]

    def complete_pending_exchange_from_player_action(
        self,
        *,
        npc: str,
        player_message: str,
        frame: SceneFrame | None = None,
    ) -> dict[str, str] | None:
        """Commit a player's actual performance of an already accepted price.

        This is intentionally narrower than proposal recognition: a statement
        of willingness does not count.  The message must describe an immediate
        delivery/sacrifice, and it must match the accepted bargain's subject or
        explicitly refer back to that agreement.
        """

        active = frame or self.current_frame
        message = " ".join(str(player_message or "").split()).strip()
        clean_npc = self._compact_contract_text(npc)
        if active is None or not clean_npc or not message:
            return None
        pending = self.pending_settled_exchanges(active)
        # A few pre-lifecycle saves explicitly marked an accepted exchange as
        # pending without storing a separate condition.  They must not block
        # scene closure, but when the player actually performs the recorded
        # offer we should still recognise and close that legacy record.
        legacy_pending = [
            item
            for item in active.settled_exchanges
            if str(item.get("outcome") or "").strip().lower() == "accepted"
            and str(item.get("player_performance") or "").strip().lower()
            == "pending"
            and not self.settled_exchange_condition(item)
        ]
        fulfilment_pool = [*pending]
        for item in legacy_pending:
            if item not in fulfilment_pool:
                fulfilment_pool.append(item)
        candidates = [
            item
            for item in fulfilment_pool
            if self._same_contract_npc(
                clean_npc,
                self._compact_contract_text(item.get("npc")),
            )
        ]
        if not candidates:
            # The scene-state API is also used outside the dialogue resolver.
            # A bare role such as “会长” is safe only when this scene has one
            # pending agreement for that role; with several, leave it unresolved
            # instead of silently paying the wrong NPC's bargain.
            role_alias_candidates = [
                item
                for item in fulfilment_pool
                if self._bare_contract_role_alias_matches(
                    clean_npc,
                    self._compact_contract_text(item.get("npc")),
                )
            ]
            if len(role_alias_candidates) == 1:
                candidates = role_alias_candidates
        if not candidates:
            return None
        message_topics = self._settlement_topics(self._compact_contract_text(message))
        explicit_reference = bool(
            re.search(
                r"按约|履约|照约|照说好的|就按说好的|这(?:段|份|件)|拿走吧|收下吧",
                message,
            )
        )
        for exchange in reversed(candidates):
            settled_topics = self._settlement_topics(
                self._compact_contract_text(exchange.get("settled_terms"))
            )
            condition = self.settled_exchange_condition(exchange)
            delivered = self._player_offer_performed(message)
            established = self._settled_exchange_condition_performed(
                message,
                condition=condition,
                settled_terms=str(exchange.get("settled_terms") or ""),
                message_topics=message_topics,
                settled_topics=settled_topics,
                explicit_reference=explicit_reference,
            )
            if not delivered and not established:
                continue
            if not message_topics.intersection(settled_topics) and not (
                len(candidates) == 1 and explicit_reference
            ) and not established:
                continue
            exchange["player_performance"] = "complete"
            exchange["player_fulfillment"] = message[:500]
            self._touch(active)
            return exchange
        return None

    @classmethod
    def settled_exchange_condition(cls, exchange: dict[str, str]) -> str:
        """Return the player-side price of a conditional accepted exchange."""

        stored = " ".join(str(exchange.get("condition") or "").split()).strip()
        if stored and not cls._is_npc_self_condition(stored):
            return stored
        inferred = cls._conditional_exchange_terms(
            str(exchange.get("settled_terms") or "")
        )["condition"]
        return "" if cls._is_npc_self_condition(inferred) else inferred

    @classmethod
    def settled_exchange_promised_result(cls, exchange: dict[str, str]) -> str:
        """Return the finite payoff promised by a conditional exchange."""

        stored = " ".join(
            str(exchange.get("promised_result") or "").split()
        ).strip()
        if stored:
            return stored
        return cls._conditional_exchange_terms(
            str(exchange.get("settled_terms") or "")
        )["promised_result"]

    @classmethod
    def _settled_exchange_condition_performed(
        cls,
        message: str,
        *,
        condition: str,
        settled_terms: str,
        message_topics: set[str],
        settled_topics: set[str],
        explicit_reference: bool,
    ) -> bool:
        """Recognise a stated explanation or proof that pays a known price.

        Delivery verbs cover hand-offs well, but a real table also settles
        bargains by *establishing a fact*: explaining a reaction, proving an
        alibi, or naming the cause of an omen.  A bare promise still does not
        count.  This narrow fallback requires both an asserted conclusion and
        a condition that explicitly asks for an explanation/proof.
        """

        clean_message = " ".join(str(message or "").split()).strip()
        clean_condition = " ".join(str(condition or "").split()).strip()
        clean_terms = " ".join(str(settled_terms or "").split()).strip()
        if not clean_message or not (clean_condition or clean_terms):
            return False
        requested_explanation = bool(
            re.search(
                r"(?:查明|弄清|找出|确认|说明|解释|证明|回答|答得上|交代|辨认)",
                f"{clean_condition} {clean_terms}",
            )
        )
        if not requested_explanation:
            return False
        # Intentions such as “我准备查明” must never satisfy a condition.
        if re.search(
            r"(?:想|准备|打算|会|将要|之后|稍后).{0,24}"
            r"(?:查明|弄清|找出|确认|说明|解释|证明|回答|交代)",
            clean_message,
        ) and not re.search(
            r"(?:这|那)(?:就|正)是.{0,36}(?:原因|来源|证明|答案)|"
            r"(?:已经|已|终于|当场).{0,24}(?:查明|弄清|找出|确认|说明|解释|证明|回答|交代)",
            clean_message,
        ):
            return False
        asserted_conclusion = bool(
            re.search(
                r"(?:这|那)(?:就|正)是.{0,48}(?:原因|来源|证明|答案)|"
                r"(?:已经|已|终于|当场).{0,24}(?:查明|弄清|找出|确认|说明|解释|证明|回答|交代)|"
                r"(?:原因|来源|真相|答案).{0,16}(?:是|为|在于)",
                clean_message,
            )
        )
        if not asserted_conclusion:
            return False
        return bool(message_topics.intersection(settled_topics) or explicit_reference)

    @staticmethod
    def _conditional_exchange_terms(settled_terms: str) -> dict[str, str]:
        """Extract a finite price/payoff pair from an accepted prose bargain.

        This is intentionally conservative.  Ordinary accepted proposals stay
        ordinary settlements; only explicit “first X, then Y” wording gains a
        fulfilment lifecycle.
        """

        text = " ".join(str(settled_terms or "").split()).strip("。；; ")
        if not text:
            return {"condition": "", "promised_result": ""}
        patterns = (
            r"(?:接受|同意|答应)?先(?P<condition>[^；。]{2,120}?)(?:再|然后|之后|才)"
            r"(?P<payout>[^；。]{2,120})",
            r"(?:若|如果|只要|一旦)(?P<condition>[^；。]{2,120}?)[，,]"
            r"(?:我|我们|守望会|对方)?(?:就|便|会|才)(?P<payout>[^；。]{2,120})",
            r"(?P<condition>(?:英雄|玩家|队伍|你们?|我(?:们)?)[^，,；。]{0,100}?"
            r"(?:交出|交付|递交|献出|支付|销毁|放弃|带来|取回|查明|证明|说出)"
            r"[^，,；。]{0,80})[，,；;]"
            r"(?P<payout>[^；。]{0,60}?(?:提供|交给|给予|开放|放行|允许|释放|归还|交还|带路|退开)"
            r"[^；。]{0,80})",
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if match is None:
                continue
            condition = match.group("condition").strip("，,：: ")
            payout = match.group("payout").strip("，,：: ")
            payout = re.sub(r"^(?:决定|判断|考虑)(?:是否)?", "", payout).strip()
            # ``settled_terms`` is the NPC's public answer. A clause such as
            # “如果我想撤回，我会亲口说停下” records the NPC's own agency,
            # not an unpaid price owed by the players. Such clauses must never
            # block scene or session closure.
            if SceneFrameManager._is_npc_self_condition(condition):
                continue
            if condition and payout:
                return {
                    "condition": condition[:220],
                    "promised_result": payout[:160],
                }
        return {"condition": "", "promised_result": ""}

    @staticmethod
    def _is_npc_self_condition(condition: str) -> bool:
        clean = " ".join(str(condition or "").split()).strip("，,：: ")
        return bool(re.match(r"^(?:我|我们)(?!方)", clean))

    def normalize_loaded_state(self) -> None:
        """Migrate scene-frame fields that used older, noisier lifecycles."""

        for frame in [*self.history, *self.suspended_frames.values(), self.current_frame]:
            if frame is None:
                continue
            compacted: list[dict[str, str]] = []
            for item in frame.settled_exchanges:
                clean = {key: str(value or "").strip() for key, value in dict(item).items()}
                conditional_terms = self._conditional_exchange_terms(
                    clean.get("settled_terms", "")
                )
                if self._is_npc_self_condition(clean.get("condition", "")):
                    clean["condition"] = ""
                    clean["promised_result"] = ""
                if not clean.get("condition"):
                    clean["condition"] = conditional_terms["condition"]
                if not clean.get("promised_result"):
                    clean["promised_result"] = conditional_terms["promised_result"]
                if not clean.get("condition"):
                    clean["player_performance"] = "complete"
                elif clean.get("player_performance") not in {"complete", "pending"}:
                    clean["player_performance"] = (
                        "complete"
                        if self._player_offer_performed(clean.get("player_offer", ""))
                        else "pending"
                    )
                match = next(
                    (
                        existing
                        for existing in reversed(compacted)
                        if self._same_settled_exchange(
                            existing,
                            npc=clean.get("npc", ""),
                            outcome=clean.get("outcome", ""),
                            settled_terms=clean.get("settled_terms", ""),
                        )
                    ),
                    None,
                )
                if match is None:
                    compacted.append(clean)
                    continue
                match["settled_terms"] = clean.get("settled_terms", "")[:300]
                if clean.get("npc"):
                    match["npc"] = clean["npc"]
                if clean.get("player_offer"):
                    match["player_offer"] = clean["player_offer"][:500]
                if clean.get("npc_response"):
                    match["npc_response"] = clean["npc_response"][:500]
                if clean.get("condition"):
                    match["condition"] = clean["condition"][:220]
                else:
                    match.setdefault("condition", "")
                if clean.get("promised_result"):
                    match["promised_result"] = clean["promised_result"][:160]
                else:
                    match.setdefault("promised_result", "")
                if clean.get("player_performance") == "complete":
                    match["player_performance"] = "complete"
                else:
                    match.setdefault("player_performance", "pending")
            frame.settled_exchanges[:] = compacted[-12:]
            frame.deferred_npc_commitments[:] = [
                {
                    key: str(value or "").strip()
                    for key, value in dict(item).items()
                }
                for item in frame.deferred_npc_commitments[-8:]
                if isinstance(item, dict)
                and str(item.get("npc") or "").strip()
                and str(item.get("action") or "").strip()
                and str(item.get("promised_result") or "").strip()
                and str(item.get("status") or "pending").strip()
                in {"pending", "resolved", "cancelled", "superseded"}
            ]
            pending_questions: list[dict[str, str]] = []
            for item in frame.pending_npc_questions:
                clean = {key: str(value or "").strip() for key, value in dict(item).items()}
                if not clean.get("npc") or self._looks_like_non_npc_speaker(clean.get("npc")):
                    continue
                if clean.get("kind") not in {"identity_check", "player_response"}:
                    continue
                if clean.get("status") not in {"open", "resolved", "superseded"}:
                    clean["status"] = "open"
                if not clean.get("question_id"):
                    clean["question_id"] = f"{frame.scene_key}-npc-question-{len(pending_questions) + 1}"
                pending_questions.append(clean)
            self._supersede_same_source_resolved_questions(pending_questions)
            frame.pending_npc_questions[:] = self._bounded_pending_npc_questions(
                pending_questions
            )
            frame.established_facts[:] = self._compact_fact_list(
                frame.established_facts,
                limit=10,
            )
            frame.public_facts[:] = self._compact_fact_list(
                frame.public_facts,
                limit=12,
            )
            frame.visible_elements[:] = [
                item
                for item in frame.visible_elements
                if not self._looks_like_backstage_visual_instruction(item)
            ][-10:]
            frame.open_questions[:] = [
                item
                for item in frame.open_questions
                if not self._looks_like_transient_player_request(item)
            ][-8:]
            if self._looks_like_non_npc_speaker(frame.last_npc_speaker):
                frame.last_npc_speaker = ""
            for condition in frame.open_conditions:
                if str(condition.get("status") or "open") == "resolved":
                    self._clear_pressure_resolved_by_condition(frame, condition)

    @staticmethod
    def _looks_like_backstage_visual_instruction(value: object) -> bool:
        clean = " ".join(str(value or "").split()).strip()
        return bool(
            re.search(
                r"^(?:把地点设定|让[“\"]|标志物从|开场扰动)|"
                r"(?:变成可触碰、可破坏或可利用|而非背景说明|"
                r"通过一种具体材质、声响、气味或运转方式进入现场|"
                r"必须先改变一个人、物件或空间位置，再由GM交出行动权)",
                clean,
            )
        )

    @classmethod
    def _clear_pressure_resolved_by_condition(
        cls,
        frame: SceneFrame,
        condition: dict[str, str],
    ) -> None:
        pressure = " ".join(str(frame.current_pressure or "").split()).strip()
        if not pressure or not re.search(
            r"尚未|还未|仍在等待|仍未|没有|未决定|等(?:待)?(?:答复|回应|决定)",
            pressure,
        ):
            return
        promise_key = str(condition.get("promise_key") or "").strip()
        promised_result = " ".join(
            str(condition.get("promised_result") or "").split()
        ).strip()
        condition_text = " ".join(str(condition.get("condition") or "").split()).strip()
        topic_patterns = {
            "access_granted": r"放行|通行|旧路|路线|入口|开门|钥匙",
            "disclosure": r"回答|答复|情报|秘密|真相|名字|身份|说明",
            "aid_granted": r"帮助|协助|援助|护送|治疗|庇护",
        }
        explicit_pattern = topic_patterns.get(promise_key, "")
        if explicit_pattern and re.search(explicit_pattern, pressure):
            frame.current_pressure = ""
            return
        pressure_topics = cls._settlement_topics(cls._compact_contract_text(pressure))
        resolved_topics = cls._settlement_topics(
            cls._compact_contract_text(f"{condition_text} {promised_result}")
        )
        if pressure_topics & resolved_topics:
            frame.current_pressure = ""

    @staticmethod
    def _looks_like_non_npc_speaker(value: object) -> bool:
        clean = "".join(str(value or "").split())
        if not clean:
            return False
        if clean in {"了", "的", "他", "她", "它", "我", "你", "我们", "众人", "有人"}:
            return True
        return bool(
            re.search(
                r"(?:方向|方位|入口|出口|门缝|地面|墙根|路面|动静|现场|局面|痕迹)$",
                clean,
            )
        )

    @classmethod
    def _same_settled_exchange(
        cls,
        existing: dict[str, str],
        *,
        npc: str,
        outcome: str,
        settled_terms: str,
    ) -> bool:
        if str(existing.get("outcome") or "").strip() != str(outcome or "").strip():
            return False
        if not cls._same_contract_npc(
            cls._compact_contract_text(existing.get("npc")),
            cls._compact_contract_text(npc),
        ):
            return False
        left = cls._compact_contract_text(existing.get("settled_terms"))
        right = cls._compact_contract_text(settled_terms)
        if not left or not right:
            return False
        if left == right:
            return True
        canonical_left = cls._canonical_settlement_terms(left)
        canonical_right = cls._canonical_settlement_terms(right)
        if canonical_left == canonical_right:
            return True
        if min(len(canonical_left), len(canonical_right)) >= 5 and (
            canonical_left in canonical_right or canonical_right in canonical_left
        ):
            return True
        if min(len(left), len(right)) < 14:
            return False
        left_pairs = {left[index : index + 2] for index in range(len(left) - 1)}
        right_pairs = {right[index : index + 2] for index in range(len(right) - 1)}
        containment = len(left_pairs & right_pairs) / max(
            1,
            min(len(left_pairs), len(right_pairs)),
        )
        similarity = SequenceMatcher(None, left, right).ratio()
        if similarity >= 0.4 and containment >= 0.3:
            return True
        shared_topics = cls._settlement_topics(left) & cls._settlement_topics(right)
        return bool(shared_topics) and similarity >= 0.28 and containment >= 0.22

    @staticmethod
    def _canonical_settlement_terms(text: str) -> str:
        """Normalize common promise verbs before comparing an agreed result."""

        clean = str(text or "")
        replacements = (
            (r"(?:开放|开启|打开|准许通行|允许通行|允许通过|同意借路)", "放行"),
            (r"(?:北边|北面)", "北侧"),
            (r"(?:南边|南面)", "南侧"),
            (r"(?:东边|东面)", "东侧"),
            (r"(?:西边|西面)", "西侧"),
        )
        for pattern, replacement in replacements:
            clean = re.sub(pattern, replacement, clean)
        return clean

    @staticmethod
    def _settlement_topics(text: str) -> set[str]:
        """Return broad bargain topics used only to stabilize paraphrase dedupe."""

        patterns = {
            "information_scope": (
                r"去路|来路|方向|路线|路径|说出|说明|解释|原因|来源|反应|触发|"
                r"复述|听取|补全|范围|程度|一小段|这一段|名字|终点|情报|信息"
            ),
            "passage": r"开门|放行|通行|借路|钥匙|闸门|入口",
            "proof": r"证据|证明|担保|誓约|证词|凭证",
            "payment": r"金币|报酬|付款|价格|货物|物资|赎金",
            "custody": r"交人|带走|扣留|释放|看守|护送",
            "memory": r"记忆|回忆|旧识|名字|姓名|梦境|情感|思绪",
        }
        return {name for name, pattern in patterns.items() if re.search(pattern, text)}

    @staticmethod
    def _player_offer_performed(text: str) -> bool:
        """Whether the offer text describes consideration already delivered."""

        clean = " ".join(str(text or "").split()).strip()
        if not clean:
            return False
        delivery = (
            r"转告|转给|告诉|交给|递给|复述给|说给|交付|说出|交出|献出|"
            r"让[^，,。；;！？?]{0,12}(?:拿走|取走)|放弃|舍弃|抹去|删去"
        )
        positive = re.sub(
            r"(?:不|没有|并未|暂不|先不)(?:再|会|要|把|将)?[^，,。；;！？?]{0,24}"
            rf"(?:{delivery})",
            "",
            clean,
        )
        if re.search(
            rf"(?:愿意|可以|会|打算|准备|之后|稍后|以后)[^。；;！？?]{{0,32}}(?:{delivery})",
            positive,
        ) and not re.search(r"现在就|当场|直接|已经|这就|按约|履约", positive):
            return False
        return bool(
            re.search(
                r"(?:现在就|当场|直接|已经|这就).{0,24}"
                r"(?:把|将)?[^。；;！？?]{0,100}"
                rf"(?:{delivery})",
                positive,
            )
            or re.search(
                rf"(?:{delivery}).{{0,30}}"
                r"(?:完毕|完成|清楚|听见|收下)",
                positive,
            )
            or re.search(
                rf"(?:按约|履约|照约|照说好的|就按说好的).{{0,40}}(?:{delivery})",
                positive,
            )
            or re.search(
                rf"(?:把|将)[^。；;！？?]{{1,100}}(?:{delivery})",
                positive,
            )
            or re.search(r"(?:拿走|取走|收下)(?:这段|这份|它|吧)", positive)
        )

    def repeated_settled_exchange(
        self,
        *,
        npc: str,
        player_message: str,
    ) -> tuple[dict[str, str], str] | None:
        """Find a settled bargain that the current message merely reopens."""

        frame = self.current_frame
        message = " ".join(str(player_message or "").split()).strip()
        clean_npc = self._compact_contract_text(npc)
        if frame is None or not clean_npc or not message:
            return None
        if re.search(r"(?:兑现|按约|履约|该你|现在.{0,6}(?:开门|放行|交出|给出))", message):
            return None
        asks_terms_again = bool(
            re.search(
                r"(?:什么程度|说到什么程度|才算|条件(?:是|为|呢|吗)|按什么范围|"
                r"怎样才|怎么才|是否.{0,10}(?:成立|谈妥|接受)|再确认.{0,16}(?:条件|范围))",
                message,
            )
        )
        repeats_delivery = self._player_offer_performed(message)
        if not asks_terms_again and not repeats_delivery:
            return None
        message_topics = self._settlement_topics(self._compact_contract_text(message))
        for exchange in reversed(frame.settled_exchanges):
            if not self._same_contract_npc(
                clean_npc,
                self._compact_contract_text(exchange.get("npc")),
            ):
                continue
            settled_topics = self._settlement_topics(
                self._compact_contract_text(exchange.get("settled_terms"))
            )
            if not message_topics.intersection(settled_topics):
                continue
            if asks_terms_again:
                return exchange, "terms"
            if (
                repeats_delivery
                and str(exchange.get("player_performance") or "pending") == "complete"
            ):
                return exchange, "delivery"
        return None

    @staticmethod
    def _looks_like_transient_player_request(text: str) -> bool:
        """Separate immediate player actions from durable fictional questions."""

        clean = " ".join(str(text or "").split()).strip()
        if not clean:
            return False
        if re.search(r"(?:谁来|谁先|谁负责|我倾向|我建议|大家觉得|你们觉得)", clean):
            return True
        return bool(
            re.search(
                r"(?:^|[，,。；;])(?:我|我们)[^。！？]{0,30}"
                r"(?:现在|这就|直接|转向|转身|点头|询问|追问|调查|检查|观察|复述|告诉|请)",
                clean,
            )
        )

    def record_public_fact(self, text: str) -> None:
        """Record a fact already spoken by the GM without treating it as a clue."""

        frame = self.current_frame
        clean = self._clean_persistent_fact(text)
        if not frame or not clean:
            return
        self._append_unique(frame.established_facts, clean[:500], limit=10)
        self._append_unique(frame.public_facts, clean[:500], limit=12)
        self._touch(frame)

    def record_npc_answer(
        self,
        target: str,
        text: str,
        *,
        addressed_actor: str = "",
    ) -> None:
        """Persist a player-visible NPC answer even when recovery generated it.

        Recovery answers are added after the normal action resolution, so they
        would otherwise be absent from the scene frame and could be repeated on
        the next request.
        """

        frame = self.current_frame
        clean = self._clean_persistent_fact(text)
        if not frame or not clean:
            return
        self._append_unique(frame.established_facts, clean[:400], limit=10)
        self._append_unique(frame.public_facts, clean[:400], limit=12)
        if target:
            frame.last_npc_speaker = str(target)
        self._touch(frame)

    def latest_pending_npc_question(self) -> dict[str, str] | None:
        """Return the newest semantic NPC request that still awaits a hero."""

        frame = self.current_frame
        if frame is None:
            return None
        return next(
            (
                dict(question)
                for question in reversed(frame.pending_npc_questions)
                if str(question.get("status") or "open") == "open"
            ),
            None,
        )

    def resolve_pending_npc_question(
        self,
        *,
        actor: str,
        player_message: str,
        npc_response: str = "",
    ) -> bool:
        """Close an identity check only when its addressed hero actually answers."""

        frame = self.current_frame
        clean_actor = " ".join(str(actor or "").split()).strip()
        clean_message = " ".join(str(player_message or "").split()).strip()
        if frame is None or not clean_actor or not clean_message:
            return False
        for question in reversed(frame.pending_npc_questions):
            if str(question.get("status") or "open") != "open":
                continue
            addressed = str(question.get("addressed_actor") or "").strip()
            if addressed and addressed != clean_actor:
                continue
            if str(question.get("kind") or "") != "identity_check":
                continue
            if not self._answers_identity_question(
                clean_message,
                question,
                actor=clean_actor,
                npc_response=npc_response,
            ):
                continue
            question["status"] = "resolved"
            question["resolved_by"] = clean_actor
            self._touch(frame)
            return True
        return False

    def touch_current_state(self) -> None:
        """Mark semantic side-state mutations for persistence and dashboards."""

        if self.current_frame is not None:
            self._touch(self.current_frame)

    @staticmethod
    def _bounded_pending_npc_questions(
        records: list[dict[str, str]],
        *,
        limit: int = 24,
    ) -> list[dict[str, str]]:
        """Bound closed history without ever discarding an open obligation."""

        cleaned = [dict(item) for item in records if isinstance(item, dict)]
        if len(cleaned) <= limit:
            return cleaned
        open_records = [
            item
            for item in cleaned
            if str(item.get("status") or "open").strip().lower() == "open"
        ]
        closed_records = [item for item in cleaned if item not in open_records]
        keep_closed = max(0, limit - len(open_records))
        retained_closed = closed_records[-keep_closed:] if keep_closed else []
        return [*retained_closed, *open_records]

    @classmethod
    def _supersede_same_source_resolved_questions(
        cls,
        records: list[dict[str, str]],
    ) -> None:
        """Close legacy duplicate obligations derived from one NPC utterance.

        This migration deliberately compares provenance, not loose topic words:
        both records must belong to the same NPC, kind and addressed actor, and
        their stored source utterance must be identical or one complete excerpt
        of the other. Different questions about the same topic remain open.
        """

        resolved = [
            item
            for item in records
            if str(item.get("status") or "").strip().lower() == "resolved"
        ]
        for candidate in records:
            if str(candidate.get("status") or "open").strip().lower() != "open":
                continue
            for settled in resolved:
                if any(
                    str(candidate.get(field_name) or "").strip()
                    != str(settled.get(field_name) or "").strip()
                    for field_name in ("npc", "kind", "addressed_actor")
                ):
                    continue
                candidate_evidence = cls._question_source_fingerprint(
                    candidate.get("speaker_evidence", "")
                )
                settled_evidence = cls._question_source_fingerprint(
                    settled.get("speaker_evidence", "")
                )
                if not candidate_evidence or not settled_evidence:
                    continue
                if min(len(candidate_evidence), len(settled_evidence)) < 8:
                    continue
                if (
                    candidate_evidence not in settled_evidence
                    and settled_evidence not in candidate_evidence
                ):
                    continue
                candidate["status"] = "superseded"
                candidate["superseded_by"] = str(
                    settled.get("question_id") or "resolved_same_source"
                ).strip()
                break

    @staticmethod
    def _question_source_fingerprint(value: str) -> str:
        return re.sub(
            r"[\s，,。！？!?；;：:\"'“”‘’（）()【】\[\]]+",
            "",
            str(value or ""),
        ).strip()

    @staticmethod
    def _identity_question_summary(text: str) -> tuple[str, list[str]]:
        clean = " ".join(str(text or "").split()).strip()
        asks_name = bool(re.search(r"(?:你的)?(?:姓名|名字|身份)|报上(?:姓名|名字|身份)", clean))
        asks_relation = bool(
            re.search(r"(?:你(?:和|与).{1,24}?(?:的)?关系|你们.{0,16}关系)", clean)
        )
        asks_agency = bool(
            re.search(
                r"(?:是否|是不是|能否|可否).{0,24}?(?:代表|替).{0,16}?(?:答话|作答)|"
                r"(?:代表|替).{0,16}?(?:答话|作答)",
                clean,
            )
        )
        asks_truth = bool(re.search(r"(?:是否属实|是不是属实|是否为真|是不是真的)", clean))
        parts: list[str] = []
        if asks_name:
            parts.append("自己的姓名")
        if asks_relation:
            relation = re.search(r"你(?:和|与)(?P<target>[^，,。；;！？?]{1,24}?)(?:的)?关系", clean)
            target = relation.group("target").strip() if relation else "在场那位的关系"
            parts.append(f"与{target}的关系")
        if asks_agency:
            parts.append("是否代为答话")
        if asks_truth:
            parts.append("刚才说法是否属实")
        if len(parts) < 2:
            return "", []
        return "、".join(parts), parts

    @staticmethod
    def _answers_identity_question(
        message: str,
        question: dict[str, str],
        *,
        actor: str = "",
        npc_response: str = "",
    ) -> bool:
        required = {
            item.strip()
            for item in str(question.get("required_parts") or "").split(",")
            if item.strip()
        }
        required_aliases = {
            "name": "自己的姓名",
            "relation": "与对方的关系",
            "agency": "是否代为答话",
            "truth": "刚才说法是否属实",
        }
        required = {required_aliases.get(item, item) for item in required}
        clean = " ".join(str(message or "").split()).strip()
        if not clean:
            return False
        clean_actor = " ".join(str(actor or "").split()).strip()
        clean_npc_response = " ".join(str(npc_response or "").split()).strip()
        answered_name = bool(
            re.search(r"(?:我叫|我是|我的姓名(?:是|为)|名字(?:是|叫))", clean)
            or (
                clean_actor
                and clean_actor in clean
                and re.search(r"(?:把|将)?我的名字(?:记上|记下|写上|登记)", clean)
            )
        )
        answered_relation = bool(
            re.search(r"(?:同行|同来|同路|护送|同伴|关系|保护|我(?:和|与))", clean)
        )
        answered_agency = bool(
            re.search(r"(?:只代表自己|不替.{0,16}(?:答话|作答|作主)|不代.{0,16}(?:答话|作答|作主)|代表.{0,16}自己)", clean)
            or re.search(
                r"(?:个人|你|他|她).{0,12}(?:拒绝|回答|答复|表态)"
                r".{0,12}(?:不能|不等于|无法).{0,12}(?:代表|替).{0,12}(?:作答|答话|作主|表态)",
                clean_npc_response,
            )
        )
        answered_truth = bool(re.search(r"(?:属实|是真的|为真|没有从正面|确实没有)", clean))
        checks = {
            "自己的姓名": answered_name,
            "是否代为答话": answered_agency,
            "刚才说法是否属实": answered_truth,
        }
        if any("的关系" in item for item in required):
            checks.update({item: answered_relation for item in required if "的关系" in item})
        return bool(required) and all(checks.get(item, False) for item in required)

    @staticmethod
    def _clean_persistent_fact(value: object) -> str:
        """Remove UI-only clock progress from a durable public fact."""

        clean = " ".join(str(value or "").split()).strip()
        if not clean:
            return ""
        clean = re.sub(
            r"\s*【[^】]{1,80}】\s*\d+\s*/\s*\d+"
            r"(?:\s*[。！!]\s*[^。！？!?]{0,100}[。！？!?]?)?\s*$",
            "",
            clean,
        ).strip()
        return clean

    @classmethod
    def _compact_fact_list(cls, values: Iterable[object], *, limit: int) -> list[str]:
        compacted: list[str] = []
        for raw in values:
            clean = cls._clean_persistent_fact(raw)
            if not clean:
                continue
            normalized = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", clean).lower()
            duplicate_index = next(
                (
                    index
                    for index, existing in enumerate(compacted)
                    if normalized
                    and SequenceMatcher(
                        None,
                        re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", existing).lower(),
                        normalized,
                    ).ratio()
                    >= 0.94
                ),
                None,
            )
            if duplicate_index is not None:
                compacted[duplicate_index] = clean
            else:
                compacted.append(clean)
        return compacted[-max(1, int(limit)) :]

    def format_for_prompt(self, *, include_private: bool = True) -> str:
        frame = self.current_frame
        if not frame:
            return ""
        lines = [
            "当前场景框架（后台使用，不是剧本，不能原样念给玩家）：",
            f"场景：{frame.scene_name}",
        ]
        if frame.location:
            lines.append(f"地点：{frame.location}")
        for label, value in (
            ("前提", frame.premise),
            ("赌注", frame.stakes),
            ("当前压力", frame.current_pressure),
        ):
            if value:
                lines.append(f"{label}：{value}")
        self._extend_list(lines, "可见元素", frame.visible_elements, limit=5)
        self._extend_list(lines, "NPC功能位", frame.npc_functions, limit=5)
        self._extend_list(lines, "线索池", frame.clue_pool, limit=6)
        self._extend_list(lines, "已公开事实", frame.public_facts, limit=6)
        self._extend_list(lines, "已揭示线索", frame.revealed_clues, limit=6)
        self._extend_list(lines, "最近GM节拍（只保持连续性，不得复述）", frame.recent_beats, limit=3)
        self._extend_list(lines, "未回应请求", frame.unresolved_requests, limit=4)
        self._extend_list(
            lines,
            "NPC正在等候的明确答复",
            [
                f"{item.get('npc')}正在等{item.get('addressed_actor') or '答话者'}答清{item.get('summary')}"
                for item in frame.pending_npc_questions
                if str(item.get("status") or "open") == "open"
            ],
            limit=3,
        )
        self._extend_list(
            lines,
            "玩家尚待完成的NPC公开条件",
            [
                f"{item.get('npc')}：{item.get('condition')}"
                for item in frame.open_conditions
                if str(item.get("status") or "open") == "open"
                and str(item.get("player_fulfillment") or "pending") != "fulfilled"
            ],
            limit=4,
        )
        self._extend_list(
            lines,
            "玩家已经履约、NPC本轮必须兑现的承诺",
            [
                f"{item.get('npc')}应兑现：{item.get('promised_result')}"
                for item in frame.open_conditions
                if str(item.get("status") or "open") == "open"
                and str(item.get("player_fulfillment") or "pending") == "fulfilled"
            ],
            limit=4,
        )
        self._extend_list(
            lines,
            "已兑现NPC承诺（不得再次要求同一条件）",
            [
                f"{item.get('npc')}：{item.get('promised_result') or item.get('condition')}"
                for item in frame.open_conditions
                if str(item.get("status") or "open") != "open"
            ],
            limit=4,
        )
        self._extend_list(
            lines,
            "条款已接受、但玩家尚未实际履行（不得重谈；下一步可履行、明确拒绝或承担违约后果）",
            [
                f"{item.get('npc')}接受：{item.get('settled_terms')}"
                for item in self.pending_settled_exchanges(frame)
            ],
            limit=6,
        )
        self._extend_list(
            lines,
            "已完成或已拒绝的交涉（不得重新索要、反悔或要求重复表态）",
            [
                f"{item.get('npc')}已{('接受并完成' if item.get('outcome') == 'accepted' else '拒绝')}："
                f"{item.get('settled_terms')}"
                for item in frame.settled_exchanges
                if str(item.get("outcome") or "").strip().lower() == "rejected"
                or str(item.get("player_performance") or "pending").strip().lower()
                == "complete"
            ],
            limit=6,
        )
        self._extend_list(
            lines,
            "NPC已经公开答应、尚待履行的短期行动（优先兑现，不得遗忘或用新障碍替代）",
            [
                (
                    f"{item.get('npc')}答应{item.get('action')}；"
                    f"触发：{item.get('trigger')}；"
                    f"状态：{item.get('trigger_status') or 'waiting'}；"
                    f"现场兑现者：{item.get('trigger_responder') or item.get('npc')}；"
                    f"应兑现：{item.get('promised_result')}"
                )
                for item in self.npc_deferred_commitment_manager.pending(frame)
            ],
            limit=4,
        )
        self._extend_list(lines, "已兑现不可软化后果", frame.committed_consequences, limit=6)
        self._extend_list(lines, "开放问题", frame.open_questions, limit=4)
        self._extend_list(lines, "已确立事实", frame.established_facts, limit=5)
        self._extend_list(lines, "清晰度提醒", frame.clarity_notes, limit=3)
        self._extend_list(lines, "开场引导", frame.opening_guidance, limit=3)
        self._extend_list(lines, "NPC回应原则", frame.npc_response_guidance, limit=3)
        self._extend_list(lines, "调查结果原则", frame.investigation_guidance, limit=4)
        self._extend_list(lines, "失败处理原则", frame.failure_guidance, limit=4)
        self._extend_list(lines, "已电报威胁", frame.telegraphed_threats, limit=3)
        self._extend_list(lines, "危险候选", frame.danger_candidates, limit=3)
        self._extend_list(lines, "发现候选", frame.discovery_candidates, limit=3)
        self._extend_list(lines, "特殊机制候选", frame.special_mechanism_candidates, limit=3)
        if include_private:
            for label, value in (
                ("本场标题", frame.session_title),
                ("本场核心问题", frame.dramatic_question),
                ("标志画面", frame.signature_image),
                ("对立方目标", frame.opposition_goal),
                ("两难", frame.dilemma),
                ("可变转折", frame.reversal),
                ("高潮形态", frame.climax_type),
                ("收束要求", frame.closure_requirement),
            ):
                if value:
                    lines.append(f"{label}：{value}")
            self._extend_list(lines, "契约局面事实", frame.contract_situation_facts, limit=4)
            self._extend_list(lines, "可选场景局面（可换序、合并或丢弃）", frame.session_scene_opportunities, limit=5)
            self._extend_list(lines, "关键结论的独立线索路径", frame.session_clue_routes, limit=4)
            self._extend_list(lines, "本场NPC目标", frame.session_npc_roles, limit=4)
            self._extend_list(lines, "奇幻现场细节", frame.fantastic_details, limit=4)
            self._extend_list(lines, "升级阶梯", frame.escalation_ladder, limit=3)
            self._extend_list(lines, "可兑现结果", frame.possible_payoffs, limit=3)
            self._extend_list(lines, "秘密/真相", frame.secrets, limit=5)
            self._extend_list(lines, "可揭示内容", frame.possible_reveals, limit=5)
            self._extend_list(
                lines,
                "预备调查卡（按玩家实际方法移动，不得整批公开）",
                [
                    f"{card.get('subject')}｜普通：{card.get('ordinary')}｜高结果：{card.get('high')}"
                    for card in frame.investigation_cards
                ],
                limit=6,
            )
        self._extend_list(lines, "非固定流程", frame.story_outline, limit=5)
        lines.append(
            "使用原则：准备局势、秘密和线索，不准备固定剧情；玩家从任意合理路径调查时，都从线索池和秘密中给出一致答案。"
            "如果玩家误解了公开信息，要直接澄清；检定失败也要给出受阻原因、代价或替代线索，不要把故事卡死。"
        )
        return "；".join(line for line in lines if line).strip("；")

    def audit_payload(self, *, include_private: bool = False) -> dict[str, object]:
        frame = self.current_frame
        if not frame:
            return {"active": False, "history_count": len(self.history)}
        data = asdict(frame)
        if not include_private:
            data.pop("secrets", None)
            data.pop("possible_reveals", None)
            data.pop("investigation_cards", None)
            data["session_npc_records"] = [
                {
                    "name": str(item.get("name") or ""),
                    "public_role": str(item.get("public_role") or ""),
                }
                for item in frame.session_npc_records
                if str(item.get("name") or "").strip()
            ]
        data["active"] = True
        data["history_count"] = len(self.history)
        data["usage_note"] = "场景框架是后台筹备：帮助 GM 保持线索一致，不是固定剧情。"
        return data

    def expression_packet(
        self,
        *,
        active_clocks: Iterable[str] = (),
        include_private: bool = False,
    ) -> dict[str, object]:
        """Return the small, structured packet used by the scene renderer."""

        frame = self.current_frame
        if frame is None:
            return {"active_clocks": list(active_clocks)}
        visible_front = list(frame.visible_elements[:5])
        heroes = [
            item for item in frame.visible_elements if str(item).startswith("在场英雄：")
        ]
        packet: dict[str, object] = {
            "scene_name": frame.scene_name,
            "location": frame.location,
            "premise": frame.premise,
            "mission_anchor": frame.stakes,
            "current_pressure": frame.current_pressure,
            "visible_elements": list(dict.fromkeys([*visible_front, *heroes])),
            "npc_functions": list(frame.npc_functions[:4]),
            "public_facts": list(frame.public_facts[-8:]),
            "revealed_clues": list(frame.revealed_clues[:5]),
            "recent_beats": list(frame.recent_beats[-3:]),
            "unresolved_requests": list(frame.unresolved_requests[:3]),
            "pending_npc_questions": [
                dict(item)
                for item in frame.pending_npc_questions
                if str(item.get("status") or "open") == "open"
            ][-3:],
            "open_conditions": [
                dict(item)
                for item in frame.open_conditions
                if str(item.get("status") or "open") == "open"
            ][:4],
            "resolved_conditions": [
                dict(item)
                for item in frame.open_conditions
                if str(item.get("status") or "open") != "open"
            ][-4:],
            "settled_exchanges": [dict(item) for item in frame.settled_exchanges[-6:]],
            "pending_npc_commitments": [
                dict(item)
                for item in self.npc_deferred_commitment_manager.pending(frame)
            ][-4:],
            "committed_consequences": list(frame.committed_consequences[-6:]),
            "active_clocks": list(active_clocks),
        }
        if include_private:
            opening_prepared_npcs = self._opening_prepared_npc_records(frame)
            opening_image_mode = (
                "establish"
                if frame.session_opportunity_role == "strong_start" or not self.history
                else "evolve"
            )
            packet.update(
                {
                    "private_truths": list(frame.secrets[:4]),
                    "possible_reveals": list(frame.possible_reveals[:4]),
                    "session_title": frame.session_title,
                    "dramatic_question": frame.dramatic_question,
                    "signature_image": frame.signature_image,
                    "opening_image_mode": opening_image_mode,
                    "required_opening_image": (
                        frame.signature_image if opening_image_mode == "establish" else ""
                    ),
                    "signature_image_reference": (
                        frame.signature_image if opening_image_mode == "evolve" else ""
                    ),
                    "required_opening_elements": list(frame.required_opening_elements),
                    "selected_scene_title": frame.session_opportunity_title,
                    "selected_scene_role": frame.session_opportunity_role,
                    "selected_scene_purpose": frame.session_opportunity_purpose,
                    "selected_scene_situation": frame.session_opportunity_situation,
                    "opposition_goal": frame.opposition_goal,
                    "dilemma": frame.dilemma,
                    "reversal": frame.reversal,
                    "climax_type": frame.climax_type,
                    "closure_requirement": frame.closure_requirement,
                    "irreversible_change": frame.irreversible_change,
                    "ending_echo": frame.ending_echo,
                    "prepared_npcs": [dict(item) for item in frame.session_npc_records[:4]],
                    # The full list is a GM reference library. Opening
                    # expression receives this narrower list so an offstage
                    # antagonist is not mistaken for part of the first shot.
                    "opening_prepared_npcs": opening_prepared_npcs,
                    "required_opening_npc_names": list(frame.required_opening_npc_names),
                    "escalation_ladder": list(frame.escalation_ladder[:3]),
                    "possible_payoffs": list(frame.possible_payoffs[:3]),
                }
            )
        return packet

    @classmethod
    def _opening_prepared_npc_records(cls, frame: SceneFrame) -> list[dict[str, str]]:
        matched_names = {
            cls._compact_contract_text(cls._required_npc_name(frame, required) or required)
            for required in frame.required_opening_npc_names
            if str(required or "").strip()
        }
        if not matched_names:
            return []
        return [
            dict(record)
            for record in frame.session_npc_records
            if cls._compact_contract_text(record.get("name")) in matched_names
        ][:4]

    def routing_context(self) -> dict[str, object]:
        """Return only the scene facts needed by the semantic message router."""

        frame = self.current_frame
        if frame is None:
            return {"open_conditions": []}
        generic_roles = {"知情者", "受压者", "对立者", "守门者"}
        known_npcs = [str(frame.last_npc_speaker or "").strip()]
        known_npcs.extend(
            str(entry or "").split("：", 1)[0].strip()
            for entry in frame.npc_functions
            if str(entry or "").split("：", 1)[0].strip() not in generic_roles
        )
        known_npcs.extend(
            str(item.get("npc") or "").strip()
            for item in frame.open_conditions
            if isinstance(item, dict)
        )
        known_npcs.extend(
            str(item.get("npc") or "").strip()
            for item in frame.pending_npc_questions
            if isinstance(item, dict)
            and str(item.get("status") or "open").strip().lower() == "open"
        )
        pending_npc_questions: list[dict[str, object]] = []
        for item in frame.pending_npc_questions:
            if not isinstance(item, dict):
                continue
            if str(item.get("status") or "open").strip().lower() != "open":
                continue
            if str(item.get("kind") or "") == "player_response":
                question = NPCResponseWindowManager.public_question(item)
                pending_npc_questions.append(
                    {
                        "question_id": question["question_id"],
                        "npc": question["npc"],
                        "addressed_actor": question["addressed_actor"],
                        "summary": question["summary"],
                        "remaining_items": question["remaining_items"],
                    }
                )
                continue
            required_parts = self._routing_question_parts(item.get("required_parts"))
            answered_parts = set(self._routing_question_parts(item.get("answered_parts")))
            pending_npc_questions.append(
                {
                    "question_id": str(item.get("question_id") or ""),
                    "npc": str(item.get("npc") or ""),
                    "addressed_actor": str(item.get("addressed_actor") or ""),
                    "summary": str(item.get("summary") or ""),
                    "remaining_parts": [
                        part for part in required_parts if part not in answered_parts
                    ],
                }
            )
        return {
            "scene_key": frame.scene_key,
            "scene_name": frame.scene_name,
            "location": frame.location,
            "known_npcs": list(dict.fromkeys(name for name in known_npcs if name))[:16],
            "current_pressure": frame.current_pressure,
            "recent_public_facts": list(frame.public_facts[-4:]),
            "committed_consequences": list(frame.committed_consequences[-3:]),
            "settled_exchanges": [dict(item) for item in frame.settled_exchanges[-4:]],
            "pending_npc_commitments": [
                dict(item)
                for item in self.npc_deferred_commitment_manager.pending(frame)
            ][-4:],
            "pending_npc_questions": pending_npc_questions[-4:],
            "open_conditions": [
                {
                    "condition_id": str(item.get("condition_id") or ""),
                    "npc": str(item.get("npc") or ""),
                    "condition": str(item.get("condition") or ""),
                    "promised_result": str(item.get("promised_result") or ""),
                    "required_actor": str(item.get("required_actor") or ""),
                    "player_fulfillment": str(
                        item.get("player_fulfillment") or "pending"
                    ),
                    "fulfillment_evidence": str(
                        item.get("fulfillment_evidence") or ""
                    ),
                    "fulfilled_by": str(item.get("fulfilled_by") or ""),
                    "status": str(item.get("status") or "open"),
                }
                for item in frame.open_conditions
                if str(item.get("status") or "open") == "open"
            ][:4],
        }

    @staticmethod
    def _routing_question_parts(value: object) -> list[str]:
        """Decode persisted response parts before they reach the semantic router."""

        if isinstance(value, list):
            raw_parts = value
        else:
            text = str(value or "").strip()
            if not text:
                return []
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                parsed = []
            raw_parts = parsed if isinstance(parsed, list) else []
        return list(
            dict.fromkeys(
                " ".join(str(part or "").split()).strip()
                for part in raw_parts
                if " ".join(str(part or "").split()).strip()
            )
        )

    def _build_frame(
        self,
        *,
        scene_key: str,
        scene_name: str,
        location: str,
        scene: SceneRecord | None,
        recent_chat: str,
        world_state: WorldState,
        character_manager: CharacterManager,
        contract: SessionDramaticContract | None,
    ) -> SceneFrame:
        profile = world_state.world_profile
        premise = scene.summary if scene and scene.summary else self._premise_from_chat(recent_chat, location)
        stakes = scene.objective if scene and scene.objective else self._stakes_from_world(profile, recent_chat)
        pressure = self._pressure_from_world(profile, recent_chat, location, world_state)
        visible = self._visible_elements(location, recent_chat, world_state, character_manager)
        npcs = self._npc_functions(location, recent_chat, world_state)
        clues = self._clue_pool(location, recent_chat, world_state)
        secrets = self._secrets(world_state, location, recent_chat)
        reveals = self._possible_reveals(world_state, location, recent_chat)
        palette = self._gm_palette(location, world_state)
        outline = [
            "当前只准备局面，不规定玩家必须依次调查、交涉、战斗或收束。",
            "从玩家真正触及的人物、地点和手段中选择可用素材；未使用场景可以丢弃。",
            "每次实质行动都让人物、环境、资源、关系或压力至少一项发生可见变化。",
            "关键结论保留多条独立线索路径，失败改变代价或路线，不让故事停住。",
            "当本场核心问题得到答案或不可逆改变后，先兑现结果，再给短片尾钩子。",
        ]
        ledger_conditions = self._session_fulfilled_conditions()
        scene_conditions = [dict(item) for item in (scene.open_conditions if scene else [])]
        known_condition_ids = {
            str(item.get("condition_id") or "").strip()
            for item in scene_conditions
            if str(item.get("condition_id") or "").strip()
        }
        for condition in ledger_conditions:
            condition_id = str(condition.get("condition_id") or "").strip()
            if condition_id and condition_id in known_condition_ids:
                continue
            scene_conditions.append(condition)
            if condition_id:
                known_condition_ids.add(condition_id)
        initial_public_facts = self._initial_public_facts(location, world_state)
        for condition in ledger_conditions:
            npc = str(condition.get("npc") or "").strip()
            result = str(condition.get("promised_result") or "").strip()
            if result:
                self._append_unique(
                    initial_public_facts,
                    f"{npc}已经兑现承诺：{result.rstrip('。')}。",
                    limit=12,
                )
        frame = SceneFrame(
            scene_key=scene_key,
            scene_name=scene_name,
            source_scene_id=str(scene.scene_id if scene else "").strip(),
            location=location,
            premise=premise,
            stakes=stakes,
            current_pressure=pressure,
            session_opportunity_key=str(
                getattr(scene, "session_opportunity_key", "") or ""
            ).strip(),
            session_opportunity_title=str(
                getattr(scene, "session_opportunity_title", "") or ""
            ).strip(),
            session_opportunity_role=str(
                getattr(scene, "session_opportunity_role", "") or ""
            ).strip(),
            session_opportunity_purpose=str(
                getattr(scene, "session_opportunity_purpose", "") or ""
            ).strip(),
            session_opportunity_situation=str(
                getattr(scene, "session_opportunity_situation", "") or ""
            ).strip(),
            visible_elements=visible,
            npc_functions=npcs,
            clue_pool=clues,
            secrets=secrets,
            possible_reveals=reveals,
            public_facts=initial_public_facts,
            open_questions=self._open_questions(world_state, location, recent_chat),
            danger_candidates=palette["danger"],
            discovery_candidates=palette["discovery"],
            special_mechanism_candidates=palette["special_mechanisms"],
            story_outline=outline,
            open_conditions=scene_conditions,
            opening_guidance=[
                "先描述地点中的动态变化，而不是列出可互动清单。",
                "开场最后交给英雄一个具体当下：谁在等答复、什么正在逼近、哪个物件已经异常。",
            ],
            npc_response_guidance=[
                "NPC 被玩家询问时必须给出可行动回应：同意、拒绝、条件、代价、犹豫或反问。",
                "NPC 的回答要受其动机、公开事实和秘密约束；不能只复述玩家动作。",
                "若 NPC 不愿说真相，也要给出可理解的回避理由或可争取条件。",
            ],
            investigation_guidance=[
                "普通成功给可观察事实或可追线索；不要直接公开全部秘密。",
                "高结果或大成功给额外角度、隐藏关联或下一步明确方向。",
                "调查对象名称要具体，避免对玩家说“当前目标”“当前线索”。",
                "线索答案可随玩家路径呈现，但不能推翻已经公开的事实。",
            ],
            failure_guidance=[
                "失败不是空白：说明噪音、压力、遮挡、误导或 NPC 阻拦如何让行动受阻。",
                "除非行动本身与危险相关，普通调查失败不应凭空推进无关威胁命刻。",
                "关键线索不要因一次失败彻底消失；可以给模糊线索、代价、延迟或换一条路径。",
            ],
        )
        self._apply_contract(frame, contract)
        self._touch(frame)
        return frame

    def _session_fulfilled_conditions(self) -> list[dict[str, str]]:
        if self.session_ledger is None or not self.session_ledger.active:
            return []
        return [dict(item) for item in self.session_ledger.fulfilled_promises]

    def _apply_contract(
        self,
        frame: SceneFrame,
        contract: SessionDramaticContract | None,
    ) -> None:
        """Attach the current table-session situation without making it canon.

        Contract fields remain backstage until play establishes them.  This
        keeps the GM focused on a local dramatic question while preserving the
        freedom to relocate unrevealed clues or discard a planned reversal.
        """

        if contract is None or not str(contract.title or "").strip():
            return
        frame.session_title = str(contract.title or "").strip()
        frame.dramatic_question = str(contract.dramatic_question or "").strip()
        frame.signature_image = str(contract.signature_image or "").strip()
        frame.opposition_goal = str(contract.opposition_goal or "").strip()
        frame.dilemma = str(contract.dilemma or "").strip()
        frame.reversal = str(contract.reversal or "").strip()
        frame.climax_type = str(contract.climax_type or "").strip()
        frame.closure_requirement = str(contract.closure_requirement or "").strip()
        frame.irreversible_change = str(contract.irreversible_change or "").strip()
        frame.ending_echo = str(contract.ending_echo or "").strip()
        frame.contract_situation_facts = self._dedupe(contract.situation_facts, limit=6)
        frame.session_scene_opportunities = self._dedupe(
            [
                f"{item.title}｜局面：{item.situation}｜作用：{item.purpose}｜压力：{item.pressure}"
                for item in contract.potential_scenes
            ],
            limit=5,
        )
        frame.session_clue_routes = self._dedupe(
            [
                f"{item.approach}｜引子：{item.visible_lead}｜成功：{item.success_reveal}｜受阻：{item.fallback}"
                for item in contract.clue_routes
            ],
            limit=5,
        )
        frame.session_npc_roles = self._dedupe(
            [
                f"{item.name}｜身份：{item.public_role}｜现在想要：{item.goal_now}｜筹码：{item.leverage}｜"
                f"权限范围：{item.authority_scope}｜明确要求：{item.concrete_demand}｜"
                f"接受标准：{item.acceptance_rule}｜兑现结果：{item.promised_result}｜"
                f"公开起步方向：{item.public_lead}｜可行路径：{'；'.join(item.fulfillment_routes)}｜"
                f"受阻后行动：{item.refusal_move or item.if_blocked}｜说话线索：{item.voice_cue}｜"
                f"私密动机：{item.private_secret}"
                for item in contract.important_npcs
            ],
            limit=4,
        )
        frame.session_npc_records = [
            {
                "name": str(item.name or "").strip(),
                "persona_id": str(item.persona_id or "").strip(),
                "public_role": str(item.public_role or "").strip(),
                "goal_now": str(item.goal_now or "").strip(),
                "leverage": str(item.leverage or "").strip(),
                "authority_scope": str(item.authority_scope or "").strip(),
                "concrete_demand": str(item.concrete_demand or "").strip(),
                "acceptance_rule": str(item.acceptance_rule or "").strip(),
                "promised_result": str(item.promised_result or "").strip(),
                "public_lead": str(item.public_lead or "").strip(),
                "fulfillment_routes": list(item.fulfillment_routes),
                "refusal_move": str(item.refusal_move or item.if_blocked or "").strip(),
                "voice_cue": str(item.voice_cue or "").strip(),
                "private_secret": str(item.private_secret or "").strip(),
                "if_helped": str(item.if_helped or "").strip(),
                "if_blocked": str(item.if_blocked or "").strip(),
            }
            for item in contract.important_npcs
            if str(item.name or "").strip()
        ][:4]
        frame.fantastic_details = self._dedupe(contract.fantastic_details, limit=5)
        self._select_session_opportunity(frame, contract)
        for route in contract.clue_routes:
            self._append_unique(frame.clue_pool, route.visible_lead, limit=12)
            self._append_unique(frame.possible_reveals, route.success_reveal, limit=10)
        for role in contract.important_npcs:
            self._append_unique(
                frame.npc_functions,
                f"{role.name}：{role.public_role}；当前目标：{role.goal_now}；"
                f"权限范围：{role.authority_scope}；明确要求：{role.concrete_demand}；"
                f"接受标准：{role.acceptance_rule}；兑现结果：{role.promised_result}；"
                f"公开起步方向：{role.public_lead}；可行路径：{'；'.join(role.fulfillment_routes)}；"
                f"受阻后行动：{role.refusal_move or role.if_blocked}",
                limit=10,
            )
        # These are GM-facing transformation prompts, not objects that already
        # exist in the fiction. The expressor receives them separately and may
        # turn them into concrete sensory details during the opening.
        frame.escalation_ladder = self._dedupe(contract.escalation_ladder, limit=5)
        frame.possible_payoffs = self._dedupe(contract.possible_payoffs, limit=5)
        if contract.opening_disruption and not frame.current_pressure:
            frame.current_pressure = str(contract.opening_disruption).strip()
        if frame.dramatic_question and (
            not frame.stakes or frame.stakes == "确认当前场景的目标、代价和下一步方向。"
        ):
            frame.stakes = frame.dramatic_question
        self._touch(frame)

    def _select_session_opportunity(
        self,
        frame: SceneFrame,
        contract: SessionDramaticContract,
    ) -> None:
        if not contract.potential_scenes:
            return
        if frame.session_opportunity_key:
            selected = next(
                (
                    item
                    for item in contract.potential_scenes
                    if str(item.scene_key or "").strip() == frame.session_opportunity_key
                ),
                None,
            )
            if selected is not None:
                self._apply_selected_opportunity(frame, selected)
            return
        used = {
            item.session_opportunity_key
            for item in self.history
            if item.session_title == contract.title and item.session_opportunity_key
        }
        scene_text = f"{frame.scene_name} {frame.location} {frame.premise}"
        selected = self.scene_navigator.select(
            contract,
            used_keys=used,
            scene_text=scene_text,
            recent_context=" ".join(frame.recent_beats[-4:]),
            location_anchor=frame.location,
        )
        if selected is not None:
            self._apply_selected_opportunity(frame, selected)

    @staticmethod
    def _sync_scene_opportunity(scene: SceneRecord | None, frame: SceneFrame) -> None:
        """Backfill the durable scene record from its selected prep opportunity.

        Scene creation and contract preparation can finish in either order.  The
        first selected opportunity therefore becomes the scene's stable identity,
        while an explicitly different identity is never overwritten here.
        """

        if scene is None or not frame.session_opportunity_key:
            return
        scene_key = str(scene.session_opportunity_key or "").strip()
        if scene_key and scene_key != frame.session_opportunity_key:
            return
        for field_name in (
            "session_opportunity_key",
            "session_opportunity_role",
            "session_opportunity_title",
            "session_opportunity_purpose",
            "session_opportunity_situation",
        ):
            if not str(getattr(scene, field_name, "") or "").strip():
                setattr(scene, field_name, str(getattr(frame, field_name, "") or "").strip())

    def _apply_selected_opportunity(self, frame: SceneFrame, selected) -> None:
        frame.session_opportunity_key = selected.scene_key
        frame.session_opportunity_title = selected.title
        frame.session_opportunity_role = str(selected.scene_role or "").strip()
        frame.session_opportunity_purpose = str(selected.purpose or "").strip()
        frame.session_opportunity_situation = str(selected.situation or "").strip()
        frame.required_opening_elements = self._dedupe(
            selected.required_elements,
            limit=6,
        )
        frame.required_opening_npc_names = self._dedupe(
            selected.required_npc_names,
            limit=4,
        )
        # Older saved contracts may carry role labels only in required_elements.
        # Resolve those labels against the prepared cast without making every
        # optional session NPC mandatory in the opening shot.
        remaining_elements: list[str] = []
        for element in frame.required_opening_elements:
            matched_name = self._required_npc_name(frame, element)
            if matched_name:
                self._append_unique(frame.required_opening_npc_names, matched_name, limit=4)
            else:
                remaining_elements.append(element)
        frame.required_opening_elements = remaining_elements
        for name in frame.required_opening_npc_names:
            if any(
                self._compact_contract_text(record.get("name"))
                == self._compact_contract_text(name)
                for record in frame.session_npc_records
            ):
                continue
            frame.session_npc_records.append(
                {
                    "name": name,
                    "persona_id": "",
                    "public_role": name,
                    "goal_now": "",
                    "leverage": "",
                    "authority_scope": "",
                    "concrete_demand": "",
                    "acceptance_rule": "",
                    "promised_result": "",
                    "public_lead": "",
                    "fulfillment_routes": [],
                    "refusal_move": "",
                    "voice_cue": "",
                    "private_secret": "",
                    "if_helped": "",
                    "if_blocked": "",
                }
            )
            self._append_unique(frame.npc_functions, f"{name}：本场开场已在场", limit=10)
        if selected.situation:
            frame.current_pressure = frame.current_pressure or selected.situation
        for entry in selected.entry_points:
            self._append_unique(frame.discovery_candidates, entry, limit=8)
        for change in selected.possible_changes:
            self._append_unique(frame.possible_payoffs, change, limit=8)

    @staticmethod
    def _required_npc_name(frame: SceneFrame, required: str) -> str:
        raw_required = str(required or "").strip()
        normalized = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", raw_required)
        role_terms = (
            "会长",
            "监察官",
            "守门人",
            "巡守长",
            "钟匠",
            "掌柜",
            "祭司",
            "书记官",
            "旅人",
            "队长",
            "领主",
        )
        for record in frame.session_npc_records:
            name = str(record.get("name") or "").strip()
            labels = [
                re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(record.get(key) or ""))
                for key in ("name", "public_role")
            ]
            if any(label and (label in normalized or normalized in label) for label in labels):
                return name
            if any(term in normalized and any(term in label for label in labels) for term in role_terms):
                return name
        if (
            1 < len(raw_required) <= 24
            and any(normalized.endswith(term) for term in role_terms)
            and not re.search(r"(?:旁边|附近|身后|手中|携带|留下|发出|写着|刻着)", raw_required)
        ):
            return re.sub(r"^(?:一名|一位|那名|那位|这名|这位)", "", raw_required).strip()
        return ""

    @staticmethod
    def _mentioned_npc_from_frame(frame: SceneFrame, text: str) -> str:
        candidates: list[str] = []
        if frame.last_npc_speaker:
            candidates.append(frame.last_npc_speaker)
        for entry in frame.npc_functions:
            label = re.split(r"[：:]", str(entry or ""), maxsplit=1)[0].strip()
            if label:
                candidates.append(label)
        for name in sorted(set(candidates), key=len, reverse=True):
            if name in text:
                return name
        subject_match = re.search(
            r"(?:^|[。！？；;\n])\s*(?P<name>[\u4e00-\u9fffA-Za-z0-9·]{2,18}?)"
            r"(?:把|将|说道|说|问道|问|答道|回答|喊道|喊|低声|抬手|递出|推开|点头|摇头)",
            text,
        )
        if subject_match:
            subject = subject_match.group("name").strip()
            if subject not in {"英雄们", "玩家们", "队伍众人", "镜头之中"}:
                return subject
        return ""

    def _refresh_dynamic_bits(
        self,
        frame: SceneFrame,
        recent_chat: str,
        world_state: WorldState,
        character_manager: CharacterManager,
    ) -> None:
        for item in self._visible_elements(frame.location, recent_chat, world_state, character_manager):
            self._append_unique(frame.visible_elements, item, limit=8)
        for item in self._clue_pool(frame.location, recent_chat, world_state):
            self._append_unique(frame.clue_pool, item, limit=8)
        if not (frame.danger_candidates or frame.discovery_candidates or frame.special_mechanism_candidates):
            palette = self._gm_palette(frame.location, world_state)
            for item in palette["danger"]:
                self._append_unique(frame.danger_candidates, item, limit=5)
            for item in palette["discovery"]:
                self._append_unique(frame.discovery_candidates, item, limit=5)
            for item in palette["special_mechanisms"]:
                self._append_unique(frame.special_mechanism_candidates, item, limit=5)
        request = self._player_request(recent_chat)
        if request:
            self._append_unique(frame.unresolved_requests, request, limit=6)
        self._touch(frame)

    def _scene_name(self, scene: SceneRecord | None, recent_chat: str, location: str) -> str:
        if scene:
            return scene.name
        if location:
            return location
        text = str(recent_chat or "")
        if "第一章" in text:
            return "第一章开场"
        return "当前场景"

    def _location(self, scene: SceneRecord | None, recent_chat: str, world_state: WorldState) -> str:
        if scene and scene.location:
            return scene.location
        text = str(recent_chat or "")
        known_locations = [
            *world_state.world_profile.major_locations.keys(),
            *world_state.world_profile.kingdoms.keys(),
            world_state.world_profile.starting_region,
        ]
        for name in sorted((item for item in known_locations if item), key=len, reverse=True):
            if name in text:
                return name
        return ""

    def _scene_key(self, scene_name: str, location: str, scene_id: str = "") -> str:
        return f"{scene_id}|{scene_name}|{location}".strip("|")

    def _premise_from_chat(self, recent_chat: str, location: str) -> str:
        if location:
            return f"镜头聚焦【{location}】，先展示现场压力与可互动对象。"
        text = self._strip_speaker(str(recent_chat or ""))
        return text[:120] or "当前场景刚刚展开。"

    def _stakes_from_world(self, profile, recent_chat: str) -> str:
        if profile.selected_first_act_summary:
            return profile.selected_first_act_summary
        if profile.group_concept:
            return f"让小队目标【{profile.group_concept}】在当前场景中变得具体。"
        if "护送" in recent_chat:
            return "护送对象、遗物或路线安全。"
        return "确认当前场景的目标、代价和下一步方向。"

    def _pressure_from_world(self, profile, recent_chat: str, location: str, world_state: WorldState) -> str:
        text = str(recent_chat or "")
        local_text = " ".join([text, location, *world_state.subject_facts.get(location, [])])
        for name, detail in profile.factions.items():
            if name and name in local_text:
                return f"{name} 的行动正在给现场施压：{detail}"
        for threat in profile.world_threats:
            if threat and self._is_locally_relevant(threat, local_text, profile):
                return threat
        for seed in profile.villain_seeds:
            if seed and self._is_locally_relevant(seed, local_text, profile):
                return seed
        group_concept = str(profile.group_concept or "")
        if location and "驿站" in location and "护送" in group_concept:
            destination_match = re.search(r"带到([^，,。；;]+)", group_concept)
            destination = destination_match.group(1).strip() if destination_match else "下一处安全地点"
            return f"被护送者仍在等待一条能前往{destination}的安全路线，驿站的守门人尚未决定是否放行。"
        return ""

    def _visible_elements(
        self,
        location: str,
        recent_chat: str,
        world_state: WorldState,
        character_manager: CharacterManager,
    ) -> list[str]:
        items = []
        if location:
            items.append(f"地点：{location}")
            profile = world_state.world_profile
            location_detail = profile.major_locations.get(location) or profile.kingdoms.get(location)
            if location_detail:
                items.append(f"现场：{location_detail}")
        group_concept = str(world_state.world_profile.group_concept or "")
        if "旅人" in group_concept and (not location or location in group_concept or "驿站" in location):
            items.append("现场人物：小队护送的失名旅人正在这里等待去路。")
        for character in character_manager.all():
            if "pc" in character.traits:
                items.append(f"在场英雄：{character.name}")
        for name in self._mentioned_names(recent_chat, world_state.world_profile.factions.keys()):
            items.append(f"可见势力痕迹：{name}")
        for name in self._mentioned_names(recent_chat, world_state.world_profile.major_locations.keys()):
            items.append(f"相关地点：{name}")
        return self._dedupe(items, limit=8)

    def _npc_functions(self, location: str, recent_chat: str, world_state: WorldState) -> list[str]:
        items: list[str] = []
        local_context = " ".join(
            [
                str(location or ""),
                str(recent_chat or ""),
                *world_state.subject_facts.get(location, []),
            ]
        )
        for persona in world_state.npc_personas.values():
            persona_markers = [persona.name, persona.public_identity, *persona.aliases]
            if location:
                if persona.first_scene:
                    if location not in persona.first_scene and persona.first_scene not in location:
                        continue
                elif not any(
                    marker and marker in local_context
                    for marker in persona_markers
                ):
                    continue
            elif not any(marker and marker in local_context for marker in persona_markers):
                continue
            label = persona.public_identity or persona.name
            role = persona.role_in_story or "可互动 NPC"
            drive = f"；动机：{persona.core_drive}" if persona.core_drive else ""
            items.append(f"{label}：{role}{drive}")
        text = str(recent_chat or "")
        group_concept = str(world_state.world_profile.group_concept or "")
        if "钟" in text or "晶炉" in text:
            items.append("知情者：钟匠、晶炉维护者或听见异常钟声的人。")
        if "旅人" in text or "病人" in text or "旅人" in group_concept:
            items.append("受压者：被护送的旅人，掌握记忆收购相关片段。")
        if "财团" in text:
            items.append("对立者：财团收购队、代理人或留下痕迹的巡逻者。")
        if "守望会" in text or "白花碑" in text or "白花碑" in location:
            items.append("守门者：白花守望会，能提供旧路、规矩或代价。")
        return self._dedupe(items, limit=7)

    def _clue_pool(self, location: str, recent_chat: str, world_state: WorldState) -> list[str]:
        profile = world_state.world_profile
        clues: list[str] = []
        for subject in (location, *self._entities_from_text(recent_chat, world_state)):
            for note in world_state.subject_facts.get(subject, [])[-3:]:
                clues.append(f"{subject}：{note}")
        for name, detail in profile.factions.items():
            if name and name in " ".join([recent_chat, *world_state.subject_facts.get(location, [])]):
                clues.append(f"{name} 的公开线索：{detail}")
        local_context = " ".join([location, recent_chat, *clues])
        for mystery in profile.mysteries:
            if self._is_locally_relevant(mystery, local_context, profile):
                clues.append(f"可指向谜团：{mystery}")
        for threat in profile.world_threats:
            if self._is_locally_relevant(threat, local_context, profile):
                clues.append(f"可指向威胁：{threat}")
        return self._dedupe(clues, limit=8)

    def _secrets(self, world_state: WorldState, location: str, recent_chat: str) -> list[str]:
        profile = world_state.world_profile
        context = " ".join([location, recent_chat])
        local_personas = [
            persona
            for persona in world_state.npc_personas.values()
            if (location and location in persona.first_scene) or persona.name in context
        ]
        context = " ".join([context, *(persona.name for persona in local_personas)])
        secrets = [secret for secret in profile.gm_secret_notes if self._is_locally_relevant(secret, context, profile)]
        secrets.extend(persona.secrets[-1] for persona in local_personas if persona.secrets)
        return self._dedupe(secrets, limit=6)

    def _possible_reveals(self, world_state: WorldState, location: str, recent_chat: str) -> list[str]:
        profile = world_state.world_profile
        context = " ".join([location, recent_chat, *world_state.subject_facts.get(location, [])])
        reveals = [
            item
            for item in [*profile.mysteries, *profile.world_threats, *profile.villain_seeds]
            if self._is_locally_relevant(item, context, profile)
        ]
        return self._dedupe(reveals, limit=8)

    def _gm_palette(self, location: str, world_state: WorldState) -> dict[str, list[str]]:
        region = location or world_state.world_profile.starting_region
        empty = {"danger": [], "discovery": [], "special_mechanisms": []}
        if not region:
            return empty
        palette = AdventureEventManager(world_state).gm_palette_for_region(region)
        return {
            "danger": self._format_templates(palette.get("danger", []), limit=3),
            "discovery": self._format_templates(palette.get("discovery", []), limit=3),
            "special_mechanisms": self._format_templates(palette.get("special_mechanisms", []), limit=3),
        }

    def _format_templates(self, templates: Iterable[object], *, limit: int) -> list[str]:
        items: list[str] = []
        for template in templates:
            name = str(getattr(template, "name", "") or "").strip()
            description = str(getattr(template, "description", "") or "").strip()
            hint = str(getattr(template, "mechanical_hint", "") or "").strip()
            if not (name or description or hint):
                continue
            text = name
            if description:
                text = f"{text}：{description}" if text else description
            if hint:
                text = f"{text}（{hint}）"
            items.append(text)
            if len(items) >= limit:
                break
        return items

    def _initial_public_facts(self, location: str, world_state: WorldState) -> list[str]:
        facts: list[str] = []
        if location:
            facts.extend(world_state.subject_facts.get(location, [])[-4:])
        # Consensus notes preserve table decisions, proposals and facilitation
        # context. They are not automatically observable evidence in every scene.
        return self._dedupe(facts, limit=8)

    def _open_questions(self, world_state: WorldState, location: str, recent_chat: str) -> list[str]:
        profile = world_state.world_profile
        context = " ".join([location, recent_chat, *world_state.subject_facts.get(location, [])])
        questions = [
            item
            for item in [*profile.open_questions[-5:], *profile.mysteries]
            if self._is_locally_relevant(item, context, profile)
        ]
        return self._dedupe(questions, limit=8)

    def _player_request(self, recent_chat: str) -> str:
        lines = [line.strip() for line in str(recent_chat or "").splitlines() if line.strip()]
        text = self._strip_speaker(lines[-1] if lines else "")
        if any(token in text for token in ("想", "请", "询问", "寻找", "调查", "判断", "提议", "？", "?")):
            return text[:140]
        return ""

    def _resolve_matching_request(self, frame: SceneFrame, action) -> None:
        target = str(
            action.parameters.get("target")
            or action.parameters.get("subject")
            or action.parameters.get("scene_object")
            or ""
        ).strip()
        if not frame.unresolved_requests:
            return
        resolved = [
            request
            for request in frame.unresolved_requests
            if target and self._compact_contract_text(target) in self._compact_contract_text(request)
        ]
        if not resolved:
            # The answer belongs to the request that just produced it. Older
            # code popped the first request, leaving the current one alive and
            # turning every answered NPC question into a permanent scene hook.
            resolved = [frame.unresolved_requests[-1]]
        resolved_set = set(resolved)
        frame.unresolved_requests = [
            request for request in frame.unresolved_requests if request not in resolved_set
        ]
        frame.open_questions = [
            question for question in frame.open_questions if question not in resolved_set
        ]

    def _is_locally_relevant(self, item: str, context: str, profile) -> bool:
        text = str(item or "").strip()
        local = str(context or "")
        if not text or not local:
            return False
        named_entities = [
            *profile.major_locations.keys(),
            *profile.kingdoms.keys(),
            *profile.factions.keys(),
        ]
        for name in named_entities:
            if name and name in text and name in local:
                return True
        # Arbitrary two-character overlap is far too permissive in Chinese and
        # used to pull unrelated campaign mysteries into local investigations.
        # A faction marker is strong enough alone; otherwise require two
        # independent scene concepts before treating a global seed as local.
        strong_markers = ("财团", "守望会", "司教团", "王国", "公国", "帝国", "教会", "钟匠")
        if any(marker in text and marker in local for marker in strong_markers):
            return True
        scene_concepts = (
            "旅人",
            "失忆",
            "记忆",
            "灰晶",
            "遗物",
            "风铃",
            "钟声",
            "名字",
            "灵魂",
            "旧路",
            "驿站",
            "内海",
            "海岸",
            "病人",
            "采掘",
            "归潮祭",
        )
        shared = [concept for concept in scene_concepts if concept in text and concept in local]
        return len(shared) >= 2

    def _entities_from_text(self, text: str, world_state: WorldState) -> list[str]:
        names = [
            *world_state.world_profile.major_locations.keys(),
            *world_state.world_profile.kingdoms.keys(),
            *world_state.world_profile.factions.keys(),
            *world_state.npc_personas.keys(),
        ]
        return [name for name in names if name and name in text]

    def _mentioned_names(self, text: str, names: Iterable[str]) -> list[str]:
        return [name for name in names if name and name in text]

    def _strip_speaker(self, text: str) -> str:
        return re.split(r"[：:]", text, maxsplit=1)[-1].strip()

    def _append_unique(self, target: list[str], value: str, *, limit: int) -> None:
        value = str(value or "").strip()
        if not value:
            return
        if value in target:
            target.remove(value)
        target.append(value)
        del target[:-limit]

    def _dedupe(self, items: Iterable[str], *, limit: int) -> list[str]:
        result: list[str] = []
        for item in items:
            text = str(item or "").strip()
            if text and text not in result:
                result.append(text)
            if len(result) >= limit:
                break
        return result

    def _extend_list(self, lines: list[str], label: str, values: list[str], *, limit: int) -> None:
        selected = [str(item).strip() for item in values if str(item).strip()][:limit]
        if selected:
            lines.append(f"{label}：" + " / ".join(selected))

    def _touch(self, frame: SceneFrame) -> None:
        frame.last_updated = datetime.now(timezone.utc).isoformat()
