from __future__ import annotations

import re
from dataclasses import asdict, is_dataclass
from copy import deepcopy
from typing import Callable

from fu_gm.components.prologue_manager import PrologueManager
from fu_gm.components.world_state import WorldState
from fu_gm.gm_guidance import build_gm_guidance
from fu_gm.models import (
    FirstActCandidate,
    GMSecretAuditEntry,
    GMSecretAuditReport,
    GMStyleProfile,
    HeroDraft,
    SecretLockLevel,
    SessionZeroParticipant,
    SessionZeroResponse,
    SessionZeroStage,
    SessionZeroState,
    SessionZeroTurn,
    WorldCreationProfile,
)
from fu_gm.optional_rules import apply_optional_rule_state, normalize_optional_rule_key
from fu_gm.skill_library import (
    CORE_CLASS_NAMES,
    compact_skill_choice_requirements,
    normalize_skill_reference_name,
    required_spell_slots,
)
from fu_gm.spellbook import spell_school_for


DEFAULT_EIGHT_PILLARS = {
    "古老的废墟和贫瘠的土地": "世界古老、辽阔且危险，古代文明留下神器、遗迹和谜团。",
    "危险中的世界": "世界被怪物、灾害和强大反派威胁，英雄迟早会直面这些阴影。",
    "社群冲突": "不同社群被旧恨、战争、信仰、阶级或魔法与技术之争撕裂。",
    "万物皆有灵魂": "万物共享灵魂之流，魔法和奇迹都源自对这股能量的触碰。",
    "魔法和技术": "魔法与技术像硬币两面，可以冲突，也可以在魔科技中融合。",
    "英雄有各种各样的身材和形象": "主角是非凡英雄，不受现实主义限制，关键是内在精神。",
    "全都是关于英雄们的": "重要事件会直接或间接围绕英雄展开，英雄的选择能改写世界。",
    "神秘、发现和成长": "故事围绕秘密、遗失力量、情感和角色成长展开。",
}

CHAPTER_ONE_INVITATION_QUESTION = (
    "第零章已经准备好了。现在进入第一章吗？"
)


def chapter_one_conversation_anchor() -> dict[str, object]:
    """Return the internal semantic focus created by an opening invitation."""

    return {
        "anchor_id": "session-zero:chapter-one-invitation",
        "kind": "chapter_one_invitation",
        "status": "awaiting_semantic_reply",
        "question": "时悠已经询问全桌是否现在进入第一章并开始首场。",
        "question_text": CHAPTER_ONE_INVITATION_QUESTION,
        "blocking": False,
        "player_visible": False,
        "accepted_action": "start_adventure",
    }

SESSION_ZERO_CONTRIBUTION_TOPICS = (
    (
        "kingdom_contributions",
        "kingdom",
        "王国、国家或政治共同体",
        "可以只说一个名称，再补一点习俗、信仰、产业、居民或生物",
        "kingdom_contributors",
    ),
    (
        "historical_event_contributions",
        "historical_event",
        "重大历史事件",
        "说一件至今仍影响世界的往事即可",
        "historical_event_contributors",
    ),
    (
        "mystery_contributions",
        "mystery",
        "世界奥秘",
        "提出一个希望队伍日后探索、答案尚未确定的问题即可",
        "mystery_contributors",
    ),
    (
        "threat_contributions",
        "threat",
        "世界性威胁",
        "直接问这个世界现在正面临哪些威胁；不要套用角色视角、故乡或某个国家仍然存在的假设",
        "threat_contributors",
    ),
)


class SessionZeroManager:
    """维护 Session 0 的结构化世界档案，并写回 WorldState。"""

    def __init__(self, world_state: WorldState) -> None:
        self.world_state = world_state
        self.state = SessionZeroState()
        self.prologue_manager = PrologueManager()
        self._hero_validator: Callable[[str], object] | None = None

    def bind_hero_validator(
        self,
        validator: Callable[[str], object] | None,
    ) -> None:
        """Use the same authority validator as final character creation."""

        self._hero_validator = validator

    def start(
        self,
        gm_style: GMStyleProfile | None = None,
        participants: list[str] | None = None,
    ) -> SessionZeroState:
        world = self.world_state.world_profile
        if not world.pillars:
            world.pillars = dict(DEFAULT_EIGHT_PILLARS)
        self.state = SessionZeroState(
            active=True,
            stage=SessionZeroStage.TONE,
            gm_style=gm_style or GMStyleProfile(),
            world=world,
        )
        if participants:
            self.configure_participants(participants)
        self.world_state.apply_world_profile(self.state.world)
        return self.state

    def configure_participants(self, participants: list[str]) -> list[SessionZeroParticipant]:
        clean_names: list[str] = []
        for name in participants:
            clean_name = name.strip()
            if clean_name and clean_name not in clean_names:
                clean_names.append(clean_name)
        self.state.participants = [SessionZeroParticipant(name=name) for name in clean_names]
        self.state.current_participant_index = 0
        self.state.polling_round = 1 if self.state.participants else 0
        return self.state.participants

    def ensure_participants(self, participants: list[str]) -> list[SessionZeroParticipant]:
        existing = {participant.name for participant in self.state.participants}
        for name in participants:
            clean_name = str(name or "").strip()
            if clean_name and clean_name not in existing:
                self.state.participants.append(SessionZeroParticipant(name=clean_name))
                existing.add(clean_name)
        if self.state.participants and not self.state.polling_round:
            self.state.polling_round = 1
        if self.state.current_participant_index >= len(self.state.participants):
            self.state.current_participant_index = 0
        return self.state.participants

    def current_participant_name(self) -> str:
        participant = self.state.current_participant()
        return participant.name if participant is not None else ""

    def record_player_input(self, speaker: str, message: str) -> None:
        self.state.transcript.append(
            SessionZeroTurn(
                speaker=speaker,
                message=message,
                stage=self.state.stage,
            )
        )
        if self._looks_like_status_query(message):
            return
        self.record_participant_contribution(speaker, message)

    def observe_table_talk(self, speaker: str, message: str) -> None:
        """Keep recent player discussion available without treating it as confirmed canon."""

        clean_speaker = str(speaker or "").strip() or "玩家"
        clean_message = str(message or "").strip()
        if not clean_message:
            return
        self.ensure_participants([clean_speaker])
        self.state.transcript.append(
            SessionZeroTurn(
                speaker=clean_speaker,
                message=clean_message,
                stage=self.state.stage,
            )
        )

    def resume_proactive_nudges_after_setup_progress(self) -> bool:
        """Clear a temporary thinking pause after meaningful setup progress."""

        if not self.state.proactive_pause:
            return False
        self.state.proactive_pause = {}
        return True

    def pause_proactive_nudges(
        self,
        player: str,
        *,
        topic: str = "",
        evidence: str = "",
    ) -> bool:
        """Suspend setup heartbeats while retaining everyone who deferred.

        The top-level fields keep old saves and audit views readable.  The
        entry list matters when several players defer in succession: the
        current reply can hand the conversation to someone who has not already
        asked for thinking time instead of cycling back to an earlier player.
        """

        clean_player = str(player or "").strip()
        clean_topic = str(topic or "").strip()
        clean_evidence = str(evidence or "").strip()
        entries = self.proactive_pause_entries()
        replacement = {
            "player": clean_player,
            "topic": clean_topic,
            "evidence": clean_evidence,
        }
        entries = [
            entry
            for entry in entries
            if not (
                str(entry.get("player") or "") == clean_player
                and str(entry.get("topic") or "") == clean_topic
            )
        ]
        entries.append(replacement)
        pause = {
            "active": True,
            "player": clean_player,
            "topic": clean_topic,
            "evidence": clean_evidence,
            "entries": entries,
        }
        changed = self.state.proactive_pause != pause
        self.state.proactive_pause = pause
        return changed

    def proactive_pause_entries(self) -> list[dict[str, str]]:
        """Return normalized temporary pauses, including legacy save shapes."""

        pause = dict(self.state.proactive_pause or {})
        raw_entries = pause.get("entries")
        if isinstance(raw_entries, list):
            entries = [
                {
                    "player": str(entry.get("player") or "").strip(),
                    "topic": str(entry.get("topic") or "").strip(),
                    "evidence": str(entry.get("evidence") or "").strip(),
                }
                for entry in raw_entries
                if isinstance(entry, dict)
                and str(entry.get("player") or "").strip()
            ]
            if entries:
                return entries
        player = str(pause.get("player") or "").strip()
        if not bool(pause.get("active")) or not player:
            return []
        return [
            {
                "player": player,
                "topic": str(pause.get("topic") or "").strip(),
                "evidence": str(pause.get("evidence") or "").strip(),
            }
        ]

    def next_proactive_participant(
        self,
        after_player: str,
        *,
        excluded_players: set[str] | None = None,
    ) -> str:
        """Choose the next willing participant in table order."""

        excluded = {
            str(item or "").strip()
            for item in (excluded_players or set())
            if str(item or "").strip()
        }
        participants = list(self.state.participants)
        if not participants:
            return ""
        names = [participant.name for participant in participants]
        try:
            start = (names.index(str(after_player or "").strip()) + 1) % len(names)
        except ValueError:
            start = 0
        for offset in range(len(participants)):
            participant = participants[(start + offset) % len(participants)]
            if (
                participant.name not in excluded
                and participant.proactive_questions_enabled
            ):
                return participant.name
        return ""

    def chapter_one_transition_status(self, *, ready: bool) -> dict[str, object]:
        """Expose the one-shot handoff posture after Session Zero is ready."""

        if not ready:
            return {"status": "not_ready", "announced": False}
        transition = dict(self.state.chapter_one_transition or {})
        posture = str(transition.get("posture") or "").strip()
        if posture not in {"supplementing", "invited"}:
            return {"status": "pending", "announced": False}
        result: dict[str, object] = {
            "status": posture,
            "announced": True,
            "speaker": str(transition.get("speaker") or ""),
            "evidence": str(transition.get("evidence") or ""),
        }
        if posture == "invited":
            stored_anchor = transition.get("conversation_anchor")
            result["conversation_anchor"] = (
                deepcopy(stored_anchor)
                if isinstance(stored_anchor, dict)
                else chapter_one_conversation_anchor()
            )
        return result

    def set_chapter_one_transition(
        self,
        posture: str,
        *,
        speaker: str = "",
        evidence: str = "",
    ) -> tuple[bool, str]:
        """Persist the GM's semantic handoff decision without starting play."""

        normalized = str(posture or "").strip()
        if normalized not in {"supplementing", "invited"}:
            raise ValueError("未知的第一章衔接姿态。")
        previous = str(
            dict(self.state.chapter_one_transition or {}).get("posture") or ""
        ).strip()
        if previous == normalized:
            return False, previous
        transition = {
            "posture": normalized,
            "speaker": str(speaker or "").strip(),
            "evidence": str(evidence or "").strip(),
        }
        if normalized == "invited":
            transition["conversation_anchor"] = (
                chapter_one_conversation_anchor()
            )
        changed = self.state.chapter_one_transition != transition
        self.state.chapter_one_transition = transition
        if normalized != "invited":
            self.state.prepared_chapter_one_session = None
        return changed, previous

    def clear_chapter_one_transition(self) -> bool:
        if (
            not self.state.chapter_one_transition
            and self.state.prepared_chapter_one_session is None
        ):
            return False
        self.state.chapter_one_transition = {}
        self.state.prepared_chapter_one_session = None
        return True

    def _looks_like_status_query(self, message: str) -> bool:
        text = str(message or "")
        if any(token in text for token in ("创建世界还缺什么", "世界创建还缺什么", "创建世界缺什么", "世界创建缺什么")):
            return True
        if any(token in text for token in ("现在是什么阶段", "当前是什么阶段", "进行到哪", "第零章状态", "当前状态")):
            return True
        return any(token in text for token in ("还缺什么", "缺哪些", "还差什么")) and any(
            scope in text for scope in ("创建世界", "世界创建", "第零章", "Session 0", "session 0")
        )

    def apply_response(self, response: SessionZeroResponse) -> None:
        if getattr(response, "action", "reply") == "silent":
            return
        self.apply_world_updates(response.world_updates)
        next_stage = response.stage
        if next_stage == SessionZeroStage.READY and self.missing_topics():
            next_stage = self.state.stage
            self.state.world.completed = False
        self.state.stage = next_stage
        self.state.transcript.append(
            SessionZeroTurn(
                speaker=self.state.gm_style.name,
                message=response.message,
                stage=next_stage,
                accepted_facts=list(response.accepted_facts),
                suggestions=list(response.suggestions),
                questions=list(response.questions),
            )
        )
        self.align_current_participant_to_stage()
        self.assign_pending_question(response.questions)
        self.world_state.apply_world_profile(self.state.world)

    def apply_world_updates(self, updates: dict) -> None:
        if not updates:
            return
        world = self.state.world
        self._apply_pending_proposal_updates(updates)
        for field_name in (
            "campaign_title",
            "continent_name",
            "world_style",
            "world_shape",
            "map_card",
            "travel_day_length",
            "magic_tech_role",
            "group_concept",
            "starting_region",
            "party_dynamic",
            "description_style",
            "violence_guideline",
            "romance_guideline",
        ):
            if updates.get(field_name):
                setattr(world, field_name, self._stringify_value(updates[field_name]))
        if updates.get("pre_session_ready") is not None:
            world.pre_session_ready = bool(updates["pre_session_ready"])
        self._apply_optional_rule_updates(updates.get("optional_rules", {}))
        self._extend_unique(world.tone_preferences, updates.get("tone_preferences", []))
        self._extend_unique(world.playstyle_themes, updates.get("playstyle_themes", []))
        self._extend_unique(world.evil_guidelines, updates.get("evil_guidelines", []))
        self._extend_unique(world.consensus_notes, updates.get("consensus_notes", []))
        self._extend_unique(world.core_themes, self._world_fact_list(updates.get("core_themes", []), "core_themes"))
        self._extend_unique(world.historical_events, self._world_fact_list(updates.get("historical_events", []), "historical_events"))
        self._extend_unique(world.villain_seeds, self._world_fact_list(updates.get("villain_seeds", []), "villain_seeds"))
        self._extend_unique(world.villain_mirrors, self._world_fact_list(updates.get("villain_mirrors", []), "villain_mirrors"))
        self._extend_unique(world.mysteries, self._world_fact_list(updates.get("mysteries", []), "mysteries"))
        self._extend_unique(world.world_threats, self._world_fact_list(updates.get("world_threats", []), "world_threats"))
        self._extend_unique(world.safety_lines, updates.get("safety_lines", []))
        self._extend_unique(world.safety_veils, updates.get("safety_veils", []))
        self._extend_unique(world.gm_secret_notes, updates.get("gm_secret_notes", []))
        self._extend_unique(world.gm_inspiration_tags, updates.get("gm_inspiration_tags", []))
        self._extend_unique(world.gm_guidance_notes, updates.get("gm_guidance_notes", []))
        self._extend_unique(world.gm_story_beats, updates.get("gm_story_beats", []))
        self._apply_first_act_updates(updates)
        self._apply_world_removals(updates.get("world_removals", {}))
        self._apply_hero_draft_updates(updates.get("hero_drafts", {}))
        self._apply_hero_draft_deletions(updates.get("hero_draft_deletions", {}))
        self._delete_hero_drafts(updates.get("delete_hero_drafts", []))
        world.open_questions = self._string_list(updates.get("open_questions", world.open_questions))
        for key, value in updates.get("pillars", {}).items():
            world.pillars[self._stringify_value(key)] = self._stringify_value(value)
        for key, value in updates.get("major_locations", {}).items():
            name = self._stringify_value(key)
            if self._is_generic_world_label(name):
                continue
            description = self._stringify_value(value)
            world.major_locations[name] = description
            self._upsert_semantic_map_location(name, description)
        map_locations = updates.get("map_locations", [])
        if isinstance(map_locations, dict):
            map_locations = [dict(value, name=key) if isinstance(value, dict) else {"name": key, "description": value} for key, value in map_locations.items()]
        for item in map_locations if isinstance(map_locations, list) else []:
            if not isinstance(item, dict):
                continue
            name = self._stringify_value(item.get("name", "")).strip()
            if not name or self._is_generic_world_label(name):
                continue
            description = self._stringify_value(item.get("description", ""))
            if description:
                world.major_locations[name] = description
            semantic = self._semantic_map_location(
                name,
                description,
                feature_type=self._stringify_value(item.get("feature_type", "")),
                terrain=self._stringify_value(item.get("terrain", "")),
                position_hint=self._stringify_value(item.get("position_hint", "")),
                relative_to=self._stringify_value(item.get("relative_to", "")),
                relative_position=self._stringify_value(item.get("relative_position", "")),
                draw_icon=item.get("draw_icon") if isinstance(item.get("draw_icon"), bool) else None,
            )
            self.world_state.upsert_map_location(
                name,
                description=description,
                terrain=semantic["terrain"],
                feature_type=semantic["feature_type"],
                position_hint=semantic["position_hint"],
                relative_to=semantic["relative_to"],
                relative_position=semantic["relative_position"],
                faction=self._stringify_value(item.get("faction", "")),
                draw_icon=semantic["draw_icon"],
            )
        for key, value in updates.get("kingdoms", {}).items():
            name = self._normalize_polity_key(key)
            if name:
                description = self._stringify_value(value)
                world.kingdoms[name] = description
                self._upsert_semantic_map_location(name, description, default_feature="country")
        for key, value in updates.get("factions", {}).items():
            world.factions[self._stringify_value(key)] = self._stringify_value(value)
        for key, value in updates.get("gm_prepared_locations", {}).items():
            world.gm_prepared_locations[self._stringify_value(key)] = self._stringify_value(value)
        for field_name in (
            "kingdom_contributors",
            "historical_event_contributors",
            "mystery_contributors",
            "threat_contributors",
        ):
            self._merge_contributor_updates(getattr(world, field_name), updates.get(field_name, {}))
        if updates.get("completed") is not None:
            world.completed = bool(updates["completed"])
        self.ensure_custom_map_card()
        self._refresh_gm_guidance(world)

    def ensure_custom_map_card(
        self,
        *,
        map_generation_requested: bool = False,
    ) -> bool:
        """Derive the internal map classification from committed geography."""

        world = self.world_state.world_profile
        if world.map_card:
            self.state.world.map_card = world.map_card
            return True
        rendered_map_exists = any(
            str(getattr(event, "kind", "") or "") == "world_map_visual"
            for event in self.world_state.memory_events
        )
        if rendered_map_exists or map_generation_requested:
            world.map_card = "自定义地图"
            self.state.world.map_card = world.map_card
            self.world_state.apply_world_profile(world)
            return True
        if not str(world.continent_name or "").strip():
            return False
        locations = [
            location
            for name, location in self.world_state.map_locations.items()
            if str(name or "").strip() and not self._is_generic_world_label(name)
        ]
        if len(locations) < 3:
            return False
        positioned = sum(
            1
            for location in locations
            if str(getattr(location, "position_hint", "") or "").strip()
            or str(getattr(location, "relative_to", "") or "").strip()
            or str(getattr(location, "relative_position", "") or "").strip()
        )
        if positioned < 2:
            return False
        # This is workflow metadata only. Every geographic fact still comes
        # from the players' committed map locations.
        world.map_card = "自定义地图"
        self.state.world.map_card = world.map_card
        self.world_state.apply_world_profile(world)
        return True

    def _apply_pending_proposal_updates(self, updates: dict) -> None:
        world = self.state.world
        proposals = updates.get("pending_proposals", [])
        if isinstance(proposals, dict):
            proposals = [proposals]
        if isinstance(proposals, list):
            existing_by_id = {
                str(item.get("id", "")).strip(): index
                for index, item in enumerate(world.pending_proposals)
                if isinstance(item, dict) and str(item.get("id", "")).strip()
            }
            for proposal in proposals:
                if not isinstance(proposal, dict):
                    continue
                clean_proposal = self._jsonable(deepcopy(proposal))
                proposal_id = str(clean_proposal.get("id", "")).strip()
                if not proposal_id:
                    proposal_id = f"proposal_{len(world.pending_proposals) + 1}"
                    clean_proposal["id"] = proposal_id
                if proposal_id in existing_by_id:
                    world.pending_proposals[existing_by_id[proposal_id]] = clean_proposal
                else:
                    world.pending_proposals.append(clean_proposal)
                    existing_by_id[proposal_id] = len(world.pending_proposals) - 1
        clear_ids = updates.get("clear_pending_proposals", [])
        if clear_ids is True:
            world.pending_proposals.clear()
            return
        if isinstance(clear_ids, str):
            clear_ids = [clear_ids]
        if isinstance(clear_ids, list):
            wanted = {str(item).strip() for item in clear_ids if str(item).strip()}
            if wanted:
                world.pending_proposals = [
                    item
                    for item in world.pending_proposals
                    if not isinstance(item, dict) or str(item.get("id", "")).strip() not in wanted
                ]

    def _upsert_semantic_map_location(
        self,
        name: str,
        description: str,
        *,
        default_feature: str = "",
    ) -> None:
        clean_name = self._stringify_value(name).strip()
        if not clean_name:
            return
        semantic = self._semantic_map_location(clean_name, self._stringify_value(description), default_feature=default_feature)
        self.world_state.upsert_map_location(
            clean_name,
            description=self._stringify_value(description),
            terrain=semantic["terrain"],
            feature_type=semantic["feature_type"],
            position_hint=semantic["position_hint"],
            relative_to=semantic["relative_to"],
            relative_position=semantic["relative_position"],
            draw_icon=semantic["draw_icon"],
        )

    def _semantic_map_location(
        self,
        name: str,
        description: str,
        *,
        feature_type: str = "",
        terrain: str = "",
        position_hint: str = "",
        relative_to: str = "",
        relative_position: str = "",
        draw_icon: bool | None = None,
        default_feature: str = "",
    ) -> dict[str, object]:
        inferred_feature, inferred_terrain, inferred_icon = self._infer_location_feature(
            name,
            description,
            default_feature=default_feature,
        )
        inferred_position = self._infer_location_position(f"{name} {description}")
        inferred_relative_to, inferred_relative_position = self._infer_location_relative_position(
            f"{name} {description}",
            name=name,
        )
        final_relative_to = relative_to or inferred_relative_to
        final_relative_position = relative_position or inferred_relative_position
        if final_relative_to and not position_hint:
            inferred_position = ""
        final_feature = feature_type or inferred_feature
        if feature_type == "region" and inferred_feature != "region":
            final_feature = inferred_feature
        final_terrain = terrain or inferred_terrain
        if terrain == "草原" and inferred_terrain != "草原":
            final_terrain = inferred_terrain
        final_icon = draw_icon if draw_icon is not None else inferred_icon
        if final_feature == "country":
            final_icon = True
        return {
            "feature_type": final_feature,
            "terrain": final_terrain,
            "position_hint": position_hint or inferred_position,
            "relative_to": final_relative_to,
            "relative_position": final_relative_position,
            "draw_icon": final_icon,
        }

    def _infer_location_feature(self, name: str, description: str, *, default_feature: str = "") -> tuple[str, str, bool]:
        text = f"{name} {description}"
        if any(token in name for token in ("驿站", "村社", "村庄", "城镇", "城市", "采掘城", "旧都", "空港")):
            return "settlement", "城镇", True
        if any(token in name for token in ("要塞", "堡垒", "城塞")):
            return "fortress", "要塞", True
        if default_feature == "country":
            return "country", "草原", True
        if any(token in text for token in ("内海", "内陆海")):
            return "inland_sea", "大海", False
        if any(token in text for token in ("群岛", "列岛", "岛链")):
            return "archipelago", "大海", False
        if any(token in text for token in ("山脉", "山岭", "群山", "雪峰", "峰群")):
            return "mountain_range", "高山", False
        if any(token in text for token in ("森林", "林海", "树海", "古林")):
            return "forest", "森林", False
        if any(token in text for token in ("海岸", "港湾", "海湾", "岸线")):
            return "coast", "海岸", False
        if any(token in text for token in ("湖", "湖泊", "湖心")):
            return "lake", "湖泊", False
        if any(token in text for token in ("驿站", "村社", "村庄", "城镇", "城市", "采掘城", "旧都", "空港")):
            return "settlement", "城镇", True
        if any(token in text for token in ("要塞", "堡垒", "城塞")):
            return "fortress", "要塞", True
        if default_feature == "country" or any(token in text for token in ("王国", "公国", "帝国", "联邦", "共和国", "城邦")):
            return "country", "草原", True
        return "region", "草原", False

    def _infer_location_position(self, text: str) -> str:
        patterns = (
            ("northwest", ("西北", "北西")),
            ("northeast", ("东北", "北东")),
            ("southwest", ("西南", "南西")),
            ("southeast", ("东南", "南东")),
            ("center", ("中央", "中心", "中部", "腹地")),
            ("north", ("北岸", "北侧", "北部", "以北", "北边")),
            ("south", ("南岸", "南侧", "南部", "以南", "南边")),
            ("west", ("西侧", "西部", "以西", "西边", "西岸")),
            ("east", ("东侧", "东部", "以东", "东边", "东岸")),
        )
        for value, tokens in patterns:
            if any(token in text for token in tokens):
                return value
        return ""

    def _infer_location_relative_position(self, text: str, *, name: str = "") -> tuple[str, str]:
        direction_tokens = {
            "north": ("北岸", "北侧", "以北", "北边"),
            "south": ("南岸", "南侧", "以南", "南边"),
            "west": ("西岸", "西侧", "以西", "西边"),
            "east": ("东岸", "东侧", "以东", "东边"),
        }
        references = ("镜线内海", "雾潮海岸", "白花碑驿站", "鸦羽山脉", "沉默森林", "潮鸢群岛")
        for reference in references:
            if reference not in text:
                continue
            if (
                re.search(rf"{re.escape(name)}[^。！？；;，,\n]*{re.escape(reference)}(?:周边|附近|周围|一带)", text)
                or re.search(rf"{re.escape(reference)}(?:周边|附近|周围|一带)[^。！？；;，,\n]*{re.escape(name)}", text)
            ):
                return reference, "center"
            for direction, tokens in direction_tokens.items():
                if any(
                    re.search(rf"{re.escape(reference)}(?:的)?{token}[^。！？；;，,\n]*{re.escape(name)}", text)
                    or re.search(rf"{re.escape(name)}[^。！？；;，,\n]*{re.escape(reference)}(?:的)?{token}", text)
                    for token in tokens
                ):
                    return reference, direction
        return "", ""

    def _refresh_gm_guidance(self, world: WorldCreationProfile) -> None:
        guidance = build_gm_guidance(world)
        world.gm_inspiration_tags = list(guidance.inspiration_tags)
        world.gm_guidance_notes = list(guidance.principles[:6])
        world.gm_story_beats = list(guidance.story_beats[:5])
        world.gm_prepared_locations = {
            seed.name: f"{seed.archetype}：{seed.brief}" for seed in guidance.location_seeds[:6]
        }

    def _apply_optional_rule_updates(self, value) -> None:
        if not value:
            return
        world = self.state.world
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    apply_optional_rule_state(world, item, enabled=True, source="session_zero")
                    continue
                if not isinstance(item, dict):
                    continue
                key = normalize_optional_rule_key(item.get("key") or item.get("label") or item.get("name") or "")
                if key:
                    apply_optional_rule_state(
                        world,
                        key,
                        enabled=bool(item.get("enabled", True)),
                        note=self._stringify_value(item.get("note", "")),
                        source=self._stringify_value(item.get("source", "session_zero")),
                    )
            return
        if not isinstance(value, dict):
            return
        for key, raw in value.items():
            normalized = normalize_optional_rule_key(key)
            if not normalized:
                continue
            if isinstance(raw, dict):
                enabled = bool(raw.get("enabled", raw.get("value", False)))
                note = self._stringify_value(raw.get("note", ""))
                source = self._stringify_value(raw.get("source", "session_zero"))
            else:
                enabled = bool(raw)
                note = ""
                source = "session_zero"
            apply_optional_rule_state(world, normalized, enabled=enabled, note=note, source=source)

    def progress_summary(self) -> dict[str, bool]:
        self.ensure_custom_map_card()
        world = self.state.world
        heroes_ready = self._hero_creation_ready(world)
        world_creation_ready = self._world_creation_ready(world)
        first_act_prerequisites_ready = (
            world_creation_ready
            and bool(world.group_concept)
            and self._safety_setup_ready(world)
            and heroes_ready
        )
        return {
            # The world's first impression is a piece of shared fiction.  The
            # map card is only renderer workflow metadata and may stay empty
            # until the rest of world creation gives the renderer enough facts.
            "world_shape": bool(world.world_shape),
            "map_card": bool(world.map_card),
            "magic_tech_role": bool(world.magic_tech_role),
            "kingdoms": bool(world.kingdoms),
            "kingdom_contributions": self._participant_contribution_ready(
                world.kingdom_contributors,
                "kingdom_contributions",
            ),
            "historical_events": bool(world.historical_events),
            "historical_event_contributions": self._participant_contribution_ready(
                world.historical_event_contributors,
                "historical_event_contributions",
            ),
            "mysteries": bool(world.mysteries),
            "mystery_contributions": self._participant_contribution_ready(
                world.mystery_contributors,
                "mystery_contributions",
            ),
            "world_threats": bool(world.world_threats),
            "threat_contributions": self._participant_contribution_ready(
                world.threat_contributors,
                "threat_contributions",
            ),
            "group_concept": bool(world.group_concept),
            "safety": self._safety_setup_ready(world),
            "heroes": heroes_ready,
            "first_act": (not first_act_prerequisites_ready)
            or bool(world.selected_first_act_id or world.selected_first_act_summary),
            "participant_polling": self.participant_polling_ready(),
        }

    def contribution_roster(self) -> list[dict[str, object]]:
        """Return per-player world-creation contribution gaps for GM pacing."""

        world = self.state.world
        roster: list[dict[str, object]] = []
        for participant in self.state.participants:
            completed_topics: list[str] = []
            missing_topics: list[dict[str, str]] = []
            for (
                topic_code,
                topic_key,
                topic_label,
                prompt_hint,
                contributor_field,
            ) in SESSION_ZERO_CONTRIBUTION_TOPICS:
                contributors = getattr(world, contributor_field, {})
                completed = (
                    participant.name in contributors
                    or topic_code in participant.answered_topics
                )
                if completed:
                    completed_topics.append(topic_code)
                    continue
                missing_topics.append(
                    {
                        "code": topic_code,
                        "key": topic_key,
                        "label": topic_label,
                        "prompt_hint": prompt_hint,
                        "contributor_field": contributor_field,
                    }
                )
            roster.append(
                {
                    "player": participant.name,
                    "proactive_questions_enabled": participant.proactive_questions_enabled,
                    "completed_count": len(completed_topics),
                    "completed_topics": completed_topics,
                    "missing_topics": missing_topics,
                }
            )
        return roster

    def session_zero_nudge_plan(
        self,
        *,
        last_player_speaker: str = "",
        prior_target_counts: dict[str, int] | None = None,
        prior_topic_counts: dict[tuple[str, str], int] | None = None,
        topic_nudge_limit: int = 2,
        preferred_player: str = "",
        preferred_topic: str = "",
        ignore_proactive_pause: bool = False,
        excluded_players: set[str] | None = None,
    ) -> dict[str, object]:
        """Choose whom to invite without asking the most active player by default."""

        pause = dict(self.state.proactive_pause or {})
        if bool(pause.get("active")) and not ignore_proactive_pause:
            return {
                "status": "player_requested_time",
                "player": str(pause.get("player") or ""),
                "topic": str(pause.get("topic") or ""),
            }

        excluded = {
            str(item or "").strip()
            for item in (excluded_players or set())
            if str(item or "").strip()
        }

        roster = self.contribution_roster()
        if not roster:
            return {"status": "no_participants"}
        incomplete = [row for row in roster if row["missing_topics"]]
        if not incomplete:
            return {"status": "contribution_round_complete"}
        eligible = [
            row
            for row in incomplete
            if bool(row["proactive_questions_enabled"])
            and str(row["player"]) not in excluded
        ]
        if not eligible:
            if any(bool(row["proactive_questions_enabled"]) for row in incomplete):
                return {"status": "no_eligible_handoff"}
            return {"status": "all_incomplete_players_opted_out"}

        topic_counts = prior_topic_counts or {}
        per_topic_limit = max(0, int(topic_nudge_limit))

        def available_topics(row: dict[str, object]) -> list[dict[str, str]]:
            return [
                item
                for item in list(row["missing_topics"])
                if per_topic_limit <= 0
                or int(
                    topic_counts.get(
                        (str(row["player"]), str(item["code"])),
                        0,
                    )
                )
                < per_topic_limit
            ]

        if preferred_player:
            preferred = next(
                (
                    row
                    for row in eligible
                    if str(row["player"]) == preferred_player
                ),
                None,
            )
            if preferred is not None:
                preferred_missing = available_topics(preferred)
                if preferred_missing:
                    topic = next(
                        (
                            item
                            for item in preferred_missing
                            if str(item["code"]) == preferred_topic
                            or str(item["key"]) == preferred_topic
                        ),
                        preferred_missing[0],
                    )
                    return self._nudge_plan_for(preferred, topic)

        counts = prior_target_counts or {}
        indexed = [
            (index, row, topic)
            for index, row in enumerate(eligible)
            for topic in available_topics(row)
        ]
        if not indexed:
            return {"status": "reminder_budget_exhausted"}
        indexed.sort(
            key=lambda item: (
                int(item[1]["completed_count"]),
                str(item[1]["player"]) == str(last_player_speaker or ""),
                int(
                    topic_counts.get(
                        (str(item[1]["player"]), str(item[2]["code"])),
                        0,
                    )
                ),
                int(counts.get(str(item[1]["player"]), 0)),
                item[0],
            )
        )
        _, target, topic = indexed[0]
        return self._nudge_plan_for(target, topic)

    def session_zero_progress_nudge_plan(
        self,
        *,
        last_player_speaker: str = "",
        prior_target_counts: dict[str, int] | None = None,
        prior_topic_counts: dict[tuple[str, str], int] | None = None,
        topic_nudge_limit: int = 2,
        preferred_player: str = "",
        preferred_topic: str = "",
        ignore_proactive_pause: bool = False,
        excluded_players: set[str] | None = None,
    ) -> dict[str, object]:
        """Choose one natural next invitation from committed Session 0 state.

        Individual world contributions are only one phase of Session 0.  Once
        that round is complete, the same heartbeat must be able to hand the
        table to shared setup, character creation, and finally the first act.
        The returned plan is structural; the language model still decides how
        to phrase the invitation from the current conversation.
        """

        pause = dict(self.state.proactive_pause or {})
        if bool(pause.get("active")) and not ignore_proactive_pause:
            return {
                "status": "player_requested_time",
                "player": str(pause.get("player") or ""),
                "topic": str(pause.get("topic") or ""),
            }

        progress = self.progress_summary()
        world = self.state.world
        foundation_topics = (
            (
                "tone",
                bool(
                    world.tone_preferences
                    or world.world_shape
                    or world.magic_tech_role
                    or world.kingdoms
                    or world.historical_events
                    or world.mysteries
                    or world.world_threats
                ),
                "故事的基调与主题",
                "先邀请全桌各说一句想要的故事感觉；承接已有回答，不提供必须三选一的固定类型。",
            ),
            (
                "safety",
                bool(progress.get("safety")),
                "本团的界限与帷幕",
                "自然、简短且不要求解释理由地邀请全桌补充界限或帷幕；没有要补充的人也可以直接说明。",
            ),
            (
                "world_shape",
                bool(progress.get("world_shape")),
                "世界的第一印象与整体形态",
                "请全桌先说这个世界给人的第一眼画面和整体形态；给具体但可修改的起点，不要要求选择预设奇幻类型。",
            ),
            (
                "magic_tech_role",
                bool(progress.get("magic_tech_role")),
                "魔法与科技的地位",
                "请全桌谈谈魔法与科技在日常生活中如何共存、冲突或彼此转化；一次只问一个角度。",
            ),
        )
        for topic, ready, topic_label, prompt_hint in foundation_topics:
            if ready:
                continue
            return {
                "status": "shared_setup_pending",
                "stage": "shared_setup",
                "target_scope": "table",
                "topic": topic,
                "topic_key": topic,
                "topic_label": topic_label,
                "prompt_hint": prompt_hint,
                "verbalize_skip_permission": topic == "safety",
            }

        contribution_plan = self.session_zero_nudge_plan(
            last_player_speaker=last_player_speaker,
            prior_target_counts=prior_target_counts,
            prior_topic_counts=prior_topic_counts,
            topic_nudge_limit=topic_nudge_limit,
            preferred_player=preferred_player,
            preferred_topic=preferred_topic,
            ignore_proactive_pause=ignore_proactive_pause,
            excluded_players=excluded_players,
        )
        if contribution_plan.get("status") != "contribution_round_complete":
            return contribution_plan

        shared_topics = (
            (
                "kingdoms",
                "kingdoms",
                "主要国家或政治共同体",
                "个人贡献轮已经结束，但世界里还没有可用的政治共同体。提出一个可改的共同起点，等玩家确认后再写入。",
            ),
            (
                "historical_events",
                "historical_events",
                "塑造当今世界的历史",
                "个人贡献轮已经结束，但还没有形成一件共同认可的重大历史。提出一个可改的起点，等玩家确认后再写入。",
            ),
            (
                "mysteries",
                "mysteries",
                "等待探索的世界奥秘",
                "个人贡献轮已经结束，但还没有形成可供冒险追寻的奥秘。提出一个可改的起点，等玩家确认后再写入。",
            ),
            (
                "world_threats",
                "world_threats",
                "正在逼近的世界威胁",
                "个人贡献轮已经结束，但世界尚缺一项真实存在的威胁。提出一个可改的起点，等玩家确认后再写入。",
            ),
            (
                "group_concept",
                "group_concept",
                "英雄们同行的理由",
                "邀请全桌从现有世界设定出发，说说英雄们为什么会一起行动；提案需要得到其他玩家确认后才成为共识。",
            ),
        )
        for progress_key, topic, topic_label, prompt_hint in shared_topics:
            if not bool(progress.get(progress_key)):
                return {
                    "status": "shared_setup_pending",
                    "stage": "shared_setup",
                    "target_scope": "table",
                    "topic": topic,
                    "topic_key": progress_key,
                    "topic_label": topic_label,
                    "prompt_hint": prompt_hint,
                    "verbalize_skip_permission": progress_key == "safety",
                }

        hero_plan = self._hero_creation_nudge_plan(
            last_player_speaker=last_player_speaker,
            prior_target_counts=prior_target_counts,
            prior_topic_counts=prior_topic_counts,
            topic_nudge_limit=topic_nudge_limit,
            preferred_player=preferred_player,
            excluded_players=excluded_players,
        )
        if hero_plan.get("status") != "character_creation_complete":
            return hero_plan

        world = self.state.world
        if not (world.selected_first_act_id or world.selected_first_act_summary):
            return {
                "status": "first_act_pending",
                "stage": "first_act",
                "target_scope": "table",
                "topic": "first_act",
                "topic_key": "first_act",
                "topic_label": "第一章从哪里开始",
                "prompt_hint": (
                    "根据已确认的世界、小队与英雄，邀请全桌提出一个具体的开场处境。"
                    "玩家提案仍需另一名玩家明确赞同，或由全桌形成其他共识后才写入。"
                ),
                "verbalize_skip_permission": False,
            }

        return {"status": "contribution_round_complete"}

    def _hero_creation_nudge_plan(
        self,
        *,
        last_player_speaker: str = "",
        prior_target_counts: dict[str, int] | None = None,
        prior_topic_counts: dict[tuple[str, str], int] | None = None,
        topic_nudge_limit: int = 2,
        preferred_player: str = "",
        excluded_players: set[str] | None = None,
    ) -> dict[str, object]:
        field_prompts = {
            "完整角色草稿": (
                "hero_concept",
                "先从一个最清楚的角色画面开始：你想扮演怎样的人？不用一次填完整张角色卡。",
            ),
            "名字": (
                "hero_name",
                "这位英雄会怎样介绍自己？先给出名字或称呼就好。",
            ),
            "身份": (
                "hero_identity",
                "用一句话说，这位英雄现在怎样看待自己？",
            ),
            "主题": (
                "hero_theme",
                "最会驱动这位英雄行动的信念、情感或直觉是什么？",
            ),
            "故乡": (
                "hero_origin",
                "这位英雄来自哪里？也可以借此给世界添一个新地点。",
            ),
            "合计 5 级的职业分配": (
                "hero_classes",
                "以这个角色概念来看，五个初始等级主要落在哪两到三个职业？",
            ),
            "四项属性骰": (
                "hero_attributes",
                "这位英雄的四项属性想走多面手、均衡还是专精？",
            ),
            "职业技能": (
                "hero_skills",
                "接下来想先定哪一项职业技能？",
            ),
            "技能附带选择": (
                "hero_skill_options",
                (
                    "只围绕choice_requirement所指的当前技能，让玩家决定一项尚缺的"
                    "习得选择；不要朗读内部字段、整张缺项清单或固定问句。"
                ),
            ),
            "授法技能对应法术": (
                "hero_spells",
                (
                    "只围绕choice_requirement所指的授法技能，让玩家决定一个尚缺的"
                    "法术；不要朗读内部字段或一次倾倒全部缺项。"
                ),
            ),
            "初始装备": (
                "hero_equipment",
                "最能代表这位英雄的武器、防具或随身装备是什么？",
            ),
            "确认角色并正式建卡": (
                "hero_confirmation",
                "这张角色草稿已经齐了；请玩家看过以后，明确是否按这版正式建卡。",
            ),
        }
        rows: list[dict[str, object]] = []
        target_counts = prior_target_counts or {}
        topic_counts = prior_topic_counts or {}
        per_topic_limit = max(0, int(topic_nudge_limit))
        excluded = {
            str(item or "").strip()
            for item in (excluded_players or set())
            if str(item or "").strip()
        }
        for index, participant in enumerate(self.state.participants):
            if (
                not participant.proactive_questions_enabled
                or participant.name in excluded
            ):
                continue
            draft_key, draft = self._draft_for_player(participant.name)
            validation = (
                None
                if draft is None
                else self._hero_validation_result(draft, draft_key=draft_key)
            )
            missing = (
                ["完整角色草稿"]
                if draft is None
                else self._hero_missing_fields(draft, validation=validation)
            )
            if not missing:
                continue
            next_field = missing[0]
            field_code, prompt_hint = field_prompts.get(
                next_field,
                field_prompts["完整角色草稿"],
            )
            validation_errors = [
                str(item).strip()
                for item in list(getattr(validation, "errors", []) or [])
                if str(item).strip()
            ]
            unresolved_choices = compact_skill_choice_requirements(
                item
                for item in list(
                    getattr(validation, "unresolved_skill_choices", []) or []
                )
                if isinstance(item, dict)
            )
            choice_requirement: dict[str, object] = {}
            if next_field == "技能附带选择":
                choice_requirement = next(
                    (
                        item
                        for item in unresolved_choices
                        if item.get("storage_field") == "skill_options"
                    ),
                    {},
                )
            elif next_field == "授法技能对应法术":
                choice_requirement = next(
                    (
                        item
                        for item in unresolved_choices
                        if item.get("storage_field") == "spells"
                    ),
                    {},
                )
            if validation_errors:
                prompt_hint = (
                    f"当前方案有一处实际规则冲突：{validation_errors[0]}"
                    "请自然说明这一处并让玩家修正，暂时不要转向后续步骤。"
                )
            if (
                next_field == "合计 5 级的职业分配"
                and draft is not None
                and draft.class_preferences
            ):
                selected_classes = "、".join(draft.class_preferences)
                prompt_hint = (
                    f"已经选了{selected_classes}；五个初始等级想怎样分配？"
                )
            topic = f"hero_creation:{field_code}"
            if (
                per_topic_limit > 0
                and int(topic_counts.get((participant.name, topic), 0))
                >= per_topic_limit
            ):
                continue
            rows.append(
                {
                    "index": index,
                    "player": participant.name,
                    "hero_name": str(getattr(draft, "hero_name", "") or ""),
                    "missing_fields": list(missing),
                    "topic": topic,
                    "field_code": field_code,
                    "prompt_hint": prompt_hint,
                    "validation_errors": validation_errors,
                    "choice_requirement": choice_requirement,
                    "allowed_values": (
                        list(choice_requirement.get("allowed_values") or [])
                        if choice_requirement
                        else []
                    ),
                    "allowed_value_count": (
                        int(choice_requirement.get("allowed_value_count") or 0)
                        if choice_requirement
                        else (len(CORE_CLASS_NAMES) if field_code == "hero_classes" else 0)
                    ),
                    "catalog_query": (
                        dict(choice_requirement.get("catalog_query") or {})
                        if choice_requirement
                        else (
                            {
                                "kind": "class",
                                "view": "shortlist",
                                "limit": 3,
                            }
                            if field_code == "hero_classes"
                            else {}
                        )
                    ),
                    "authority_note": (
                        "身份、头衔与角色画面不是职业名；只能使用allowed_values。"
                        if field_code == "hero_classes"
                        else (
                            "合法候选来自当前技能的权威规则元数据；只询问一项，措辞由GM自然组织。"
                            if choice_requirement
                            else ""
                        )
                    ),
                }
            )
        if not rows:
            any_incomplete = any(
                draft is None or bool(self._hero_missing_fields(draft))
                for _key, draft in (
                    self._draft_for_player(participant.name)
                    for participant in self.state.participants
                )
            )
            if any_incomplete:
                if any(
                    participant.proactive_questions_enabled
                    for participant in self.state.participants
                ):
                    return {"status": "reminder_budget_exhausted"}
                return {"status": "all_incomplete_players_opted_out"}
            return {"status": "character_creation_complete"}

        rows.sort(
            key=lambda row: (
                str(row["player"]) != str(preferred_player or ""),
                int(target_counts.get(str(row["player"]), 0)),
                str(row["player"]) == str(last_player_speaker or ""),
                int(row["index"]),
            )
        )
        target = rows[0]
        return {
            "status": "targeted",
            "stage": "character_creation",
            "player": str(target["player"]),
            "hero_name": str(target["hero_name"]),
            "topic": str(target["topic"]),
            "topic_key": str(target["field_code"]),
            "topic_label": "角色创建",
            "prompt_hint": str(target["prompt_hint"]),
            "missing_fields": list(target["missing_fields"]),
            "validation_errors": list(target.get("validation_errors") or []),
            "choice_requirement": dict(target.get("choice_requirement") or {}),
            "allowed_values": list(target.get("allowed_values") or []),
            "allowed_value_count": int(target.get("allowed_value_count") or 0),
            "catalog_query": dict(target.get("catalog_query") or {}),
            "authority_note": str(target.get("authority_note") or ""),
            "verbalize_skip_permission": False,
        }

    def _nudge_plan_for(
        self,
        participant: dict[str, object],
        topic: dict[str, str],
    ) -> dict[str, object]:
        target_player = str(participant["player"])
        contributor_field = str(topic.get("contributor_field") or "")
        contributor_bucket = (
            getattr(self.state.world, contributor_field, {})
            if contributor_field
            else {}
        )
        prior_contributions: list[dict[str, object]] = []
        if isinstance(contributor_bucket, dict):
            for player, raw_values in contributor_bucket.items():
                player_name = str(player or "").strip()
                if not player_name or player_name == target_player:
                    continue
                values = [
                    str(value or "").strip()[:240]
                    for value in list(raw_values or [])
                    if str(value or "").strip()
                ][:2]
                if values:
                    prior_contributions.append(
                        {
                            "player": player_name,
                            "contributions": values,
                        }
                    )
                if len(prior_contributions) >= 3:
                    break

        prompt_hint = str(topic["prompt_hint"])
        if prior_contributions:
            prompt_hint += (
                "；先承接prior_contributions中的已有内容，不要让这位玩家把同一内容"
                "重新说一遍来完成贡献。自然地邀请其补充一个不同内容、为已有内容"
                "增加不同影响，或明确跳过；单纯赞同仍是讨论，不伪装成新设定"
            )
        return {
            "status": "targeted",
            "player": target_player,
            "topic": str(topic["code"]),
            "topic_key": str(topic["key"]),
            "topic_label": str(topic["label"]),
            "prompt_hint": prompt_hint,
            "completed_count": int(participant["completed_count"]),
            "prior_contributions": prior_contributions,
            "response_contract": {
                "accepted_paths": [
                    "distinct_contribution",
                    "new_consequence_for_existing_contribution",
                    "explicit_skip",
                    "discussion_without_commitment",
                ],
                "duplicate_is_not_required": True,
            },
            "verbalize_skip_permission": False,
        }

    def set_proactive_questions_enabled(self, player: str, enabled: bool) -> bool:
        self.ensure_participants([player])
        participant = self.find_participant(player)
        if participant is None:
            return False
        changed = participant.proactive_questions_enabled != bool(enabled)
        participant.proactive_questions_enabled = bool(enabled)
        return changed

    def world_creation_ready(self) -> bool:
        return self._world_creation_ready(self.state.world)

    def hero_creation_status(self) -> dict[str, object]:
        world = self.state.world
        participants = [participant.name for participant in self.state.participants]
        if not participants:
            participants = [draft.player_name or key for key, draft in world.hero_drafts.items()]
        missing_by_player: dict[str, list[str]] = {}
        validation_errors_by_player: dict[str, list[str]] = {}
        choice_requirements_by_player: dict[str, list[dict[str, object]]] = {}
        for player in participants:
            draft_key, draft = self._draft_for_player(player)
            if draft is None:
                missing_by_player[player or "未命名玩家"] = ["完整角色草稿"]
                continue
            validation = self._hero_validation_result(
                draft,
                draft_key=draft_key,
            )
            missing = self._hero_missing_fields(draft, validation=validation)
            if missing:
                label = draft.hero_name or draft.player_name or draft_key or player
                missing_by_player[label] = missing
                validation_errors = [
                    str(item).strip()
                    for item in list(getattr(validation, "errors", []) or [])
                    if str(item).strip()
                ]
                if validation_errors:
                    validation_errors_by_player[label] = validation_errors
                unresolved = compact_skill_choice_requirements(
                    item
                    for item in list(
                        getattr(validation, "unresolved_skill_choices", []) or []
                    )
                    if isinstance(item, dict)
                )
                if unresolved:
                    choice_requirements_by_player[label] = unresolved
        if not participants and not world.hero_drafts:
            missing_by_player["玩家角色"] = ["完整角色草稿"]
        return {
            "ready": bool(world.hero_drafts) and not missing_by_player,
            "missing_by_player": missing_by_player,
            "validation_errors_by_player": validation_errors_by_player,
            "choice_requirements_by_player": choice_requirements_by_player,
        }

    def _draft_for_player(self, player: str) -> tuple[str, HeroDraft] | tuple[str, None]:
        clean = str(player or "").strip()
        world = self.state.world
        if clean in world.hero_drafts:
            return clean, world.hero_drafts[clean]
        for key, draft in world.hero_drafts.items():
            if draft.player_name == clean or draft.hero_name == clean:
                return key, draft
        return clean, None

    def missing_topics(self) -> list[str]:
        labels = {
            "world_shape": "世界第一印象或大陆形态",
            "magic_tech_role": "魔法与科技的地位",
            "kingdoms": "主要王国或国家",
            "kingdom_contributions": "每位玩家的王国/国家贡献",
            "historical_events": "重大历史事件",
            "historical_event_contributions": "每位玩家的重大历史事件贡献",
            "mysteries": "世界奥秘",
            "mystery_contributions": "每位玩家的世界奥秘贡献",
            "world_threats": "世界性威胁",
            "threat_contributions": "每位玩家的世界威胁贡献",
            "group_concept": "小队原型",
            "safety": "界限与帷幕",
            "heroes": "角色创建缺项",
            "first_act": "第一幕目标投票",
            "participant_polling": "每位玩家的 Session 0 贡献",
        }
        return [
            labels[key]
            for key, ready in self.progress_summary().items()
            if key in labels and not ready
        ]

    def _world_creation_ready(self, world: WorldCreationProfile) -> bool:
        self.ensure_custom_map_card()
        return (
            bool(world.world_shape)
            and bool(world.magic_tech_role)
            and bool(world.kingdoms)
            and self._participant_contribution_ready(world.kingdom_contributors, "kingdom_contributions")
            and bool(world.historical_events)
            and self._participant_contribution_ready(
                world.historical_event_contributors,
                "historical_event_contributions",
            )
            and bool(world.mysteries)
            and self._participant_contribution_ready(world.mystery_contributors, "mystery_contributions")
            and bool(world.world_threats)
            and self._participant_contribution_ready(world.threat_contributors, "threat_contributions")
        )

    def _participant_contribution_ready(self, contributors: dict[str, list[str]], topic: str = "") -> bool:
        if not self.state.participants:
            return True
        answered = {str(name).strip() for name in contributors if str(name).strip()}
        return all(
            participant.name in answered or (bool(topic) and topic in participant.answered_topics)
            for participant in self.state.participants
        )

    def _safety_setup_ready(self, world: WorldCreationProfile) -> bool:
        """Return whether every current player had a chance to state safety needs.

        Legacy saves only persisted the resulting lines and veils. Preserve
        those saves when nobody has the newer ``safety`` completion marker. As
        soon as one current player records a boundary or explicitly has nothing
        to add, require the rest of the current table to answer as well.
        """

        if not self.state.participants:
            return bool(world.safety_lines or world.safety_veils)
        answered = {
            participant.name
            for participant in self.state.participants
            if "safety" in participant.answered_topics
        }
        if not answered:
            return bool(world.safety_lines or world.safety_veils)
        return all(
            participant.name in answered for participant in self.state.participants
        )

    def _hero_creation_ready(self, world: WorldCreationProfile) -> bool:
        if self.state.participants:
            for participant in self.state.participants:
                _, draft = self._draft_for_player(participant.name)
                if draft is None or self._hero_missing_fields(draft):
                    return False
            return True
        return bool(world.hero_drafts) and all(
            not self._hero_missing_fields(draft) for draft in world.hero_drafts.values()
        )

    def _hero_missing_fields(
        self,
        draft: HeroDraft,
        *,
        validation: object | None = None,
    ) -> list[str]:
        if validation is None:
            validation = self._hero_validation_result(draft)
        missing: list[str] = []
        if validation is not None:
            validation_missing = list(
                getattr(validation, "missing_fields", []) or []
            )
            validation_errors = list(getattr(validation, "errors", []) or [])
            for issue in [*validation_missing, *validation_errors]:
                category = self._hero_validation_issue_category(issue)
                if category not in missing:
                    missing.append(category)
        else:
            # Standalone SessionZeroManager tests may not bind the authoritative
            # character validator.  Keep a conservative fallback for that mode.
            if not draft.hero_name:
                missing.append("名字")
            if not draft.identity:
                missing.append("身份")
            if not draft.theme:
                missing.append("主题")
            if not draft.origin:
                missing.append("故乡")
            if not draft.classes or sum(draft.classes.values()) != 5:
                missing.append("合计 5 级的职业分配")
            if len(draft.attributes) < 4:
                missing.append("四项属性骰")
            class_total = sum(draft.classes.values()) if draft.classes else 0
            skill_total = sum(draft.skills.values()) if draft.skills else 0
            if not draft.skills or (class_total == 5 and skill_total < 5):
                missing.append("职业技能")
            if self._missing_spell_slots(draft):
                missing.append("授法技能对应法术")
            if not draft.equipment:
                missing.append("初始装备")
        if not missing and not draft.confirmed:
            missing.append("确认角色并正式建卡")
        return missing

    def _hero_validation_errors(
        self,
        draft: HeroDraft,
        *,
        draft_key: str = "",
    ) -> list[str]:
        validation = self._hero_validation_result(draft, draft_key=draft_key)
        return [
            str(item).strip()
            for item in list(getattr(validation, "errors", []) or [])
            if str(item).strip()
        ]

    def _hero_validation_result(
        self,
        draft: HeroDraft,
        *,
        draft_key: str = "",
    ) -> object | None:
        if self._hero_validator is None:
            return None
        key = str(draft_key or "").strip()
        if not key:
            for candidate_key, candidate in self.state.world.hero_drafts.items():
                if candidate is draft:
                    key = str(candidate_key)
                    break
        if not key:
            return None
        try:
            return self._hero_validator(key)
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _hero_validation_issue_category(issue: str) -> str:
        text = str(issue or "")
        exact = {
            "角色名": "名字",
            "名字": "名字",
            "身份": "身份",
            "主题": "主题",
            "故乡": "故乡",
            "职业分配": "合计 5 级的职业分配",
            "四项属性骰": "四项属性骰",
            "职业技能": "职业技能",
            "起始装备": "初始装备",
        }
        if text in exact:
            return exact[text]
        if any(
            key in text
            for key in (
                "技能附带选择",
                "便携装置",
                "拟兽系仪式",
                "形意咒法",
                "装置选择",
                "属性组合",
            )
        ):
            return "技能附带选择"
        if "法术" in text or "授法" in text:
            return "授法技能对应法术"
        if "技能" in text:
            return "职业技能"
        if "属性" in text or any(key in text for key in ("DEX", "INS", "MIG", "WLP")):
            return "四项属性骰"
        if any(key in text for key in ("装备", "武器", "防具", "盾牌")):
            return "初始装备"
        if "职业" in text:
            return "合计 5 级的职业分配"
        return "完整角色草稿"

    def _missing_spell_slots(self, draft: HeroDraft) -> dict[str, int]:
        requirements = required_spell_slots(draft.skills)
        if not requirements:
            return {}
        owned: dict[str, int] = {}
        for spell in draft.spells:
            school = spell_school_for(spell)
            if school:
                owned[school] = owned.get(school, 0) + 1
        return {
            school: required - owned.get(school, 0)
            for school, required in requirements.items()
            if owned.get(school, 0) < required
        }

    def _merge_contributor_updates(self, target: dict[str, list[str]], updates: dict) -> None:
        if not isinstance(updates, dict):
            return
        for contributor, values in updates.items():
            name = self._stringify_value(contributor).strip()
            if not name:
                continue
            if isinstance(values, str):
                raw_values = [values]
            elif isinstance(values, list):
                raw_values = values
            else:
                raw_values = [values]
            bucket = target.setdefault(name, [])
            for value in raw_values:
                text = self._stringify_value(value).strip()
                if text and text not in bucket:
                    bucket.append(text)

    def _normalize_polity_key(self, key: object) -> str:
        name = self._stringify_value(key).strip(" 的了一个一座这那「」『』【】[]()（）\"'")
        if "：" in name or ":" in name:
            name = re.split(r"[：:]", name)[-1].strip()
        name = re.sub(r"^(?:我的角色|角色|英雄|玩家角色|我|我们|他|她|它|他们|她们|这个|那个)+", "", name)
        name = re.sub(r"^(?:来自|出身|属于|效忠于|逃离|守护|管理|统治|袭击|毁灭|寻找|继承)+", "", name)
        if not name or self._is_generic_world_label(name) or name.startswith("的") or len(name) > 16:
            return ""
        if any(token in name for token in ("我的角色", "角色", "大钟", "能安抚", "是", "叫", "想", "我要")):
            return ""
        if re.search(r"[，,。！？；;\s]", name):
            return ""
        if not re.search(r"(?:王国|帝国|城邦|共和国|公国|部族|联盟|同盟)$", name) and len(name) < 2:
            return ""
        return name

    @staticmethod
    def _is_generic_world_label(name: object) -> bool:
        return str(name or "").strip() in {
            "国家",
            "王国",
            "帝国",
            "城邦",
            "共和国",
            "公国",
            "部族",
            "联盟",
            "同盟",
            "政体",
            "政权",
            "势力",
            "地区",
            "区域",
            "地点",
            "神秘地点",
            "关键地点",
            "重要地点",
            "地区和历史事件",
            "地区、威胁和阵营",
            "国家和社会冲突",
            "王国或国家",
        }

    def snapshot(self) -> dict:
        self.ensure_custom_map_card()
        world = self.state.world
        return {
            "active": self.state.active,
            "stage": self.state.stage.value,
            "gm_style": deepcopy(self.state.gm_style.__dict__),
            "world": self._jsonable(deepcopy(world)),
            "participants": [
                {
                    "name": participant.name,
                    "role": participant.role,
                    "contributions": list(participant.contributions),
                    "answered_topics": list(participant.answered_topics),
                    "pending_question": participant.pending_question,
                    "proactive_questions_enabled": participant.proactive_questions_enabled,
                }
                for participant in self.state.participants
            ],
            "current_participant": self.current_participant_name(),
            "polling_round": self.state.polling_round,
            "proactive_pause": deepcopy(self.state.proactive_pause),
            "chapter_one_transition": deepcopy(
                self.state.chapter_one_transition
            ),
            "prepared_chapter_one_session": (
                {
                    "status": str(
                        self.state.prepared_chapter_one_session.quality_status
                        or ""
                    ),
                    "fingerprint": str(
                        self.state.prepared_chapter_one_session.fingerprint
                        or ""
                    )[:12],
                    "prepared_at": str(
                        self.state.prepared_chapter_one_session.prepared_at
                        or ""
                    ),
                }
                if self.state.prepared_chapter_one_session is not None
                else None
            ),
            "missing_topics": self.missing_topics(),
            "first_act_vote_result": self._jsonable(self.first_act_vote_result()),
            "gm_secret_audit": self._jsonable(self.gm_secret_audit_report(include_content=False)),
        }

    def finish_if_ready(self) -> bool:
        if self.missing_topics():
            return False
        self.state.stage = SessionZeroStage.READY
        self.state.world.completed = True
        self.world_state.apply_world_profile(self.state.world)
        return True

    def refresh_stage_from_state(self) -> SessionZeroStage:
        """Derive the workflow stage from committed state only.

        Semantic interpretation belongs to the GM agent.  This method is the
        deterministic lifecycle boundary used after a validated tool commit.
        """

        self.ensure_custom_map_card()
        world = self.state.world
        if not self._world_creation_ready(world):
            stage = SessionZeroStage.TONE
        elif not world.group_concept:
            stage = SessionZeroStage.GROUP
        elif not self._safety_setup_ready(world):
            stage = SessionZeroStage.SAFETY
        elif not self._hero_creation_ready(world):
            stage = SessionZeroStage.HEROES
        elif not (world.selected_first_act_id or world.selected_first_act_summary):
            stage = SessionZeroStage.PROLOGUE
        else:
            stage = SessionZeroStage.READY
        if stage == SessionZeroStage.PROLOGUE and not world.selected_first_act_id:
            expected_group = self.prologue_manager.prompt_for_group(world.group_concept)
            candidate_groups = {
                candidate.group_key for candidate in world.first_act_candidates
            }
            if not world.first_act_candidates or candidate_groups != {expected_group}:
                self.generate_first_act_candidates(count=6)
                world = self.state.world
        self.prologue_manager.ensure_question_state(world)
        self.state.stage = stage
        world.completed = stage == SessionZeroStage.READY
        if stage != SessionZeroStage.READY:
            self.state.chapter_one_transition = {}
            self.state.prepared_chapter_one_session = None
        self.align_current_participant_to_stage()
        self.world_state.apply_world_profile(world)
        return stage

    def generate_first_act_candidates(
        self,
        *,
        count: int = 3,
        options: list[int] | None = None,
    ) -> list[FirstActCandidate]:
        candidates = self.prologue_manager.generate_candidates(self.state.world, count=count, options=options)
        self.state.world.first_act_candidates = candidates
        self.state.world.first_act_votes.clear()
        self.state.world.selected_first_act_id = ""
        self.state.world.selected_first_act_summary = ""
        self.state.world.first_act_questions.clear()
        self.state.world.first_act_question_answers.clear()
        self.state.world.first_act_skipped_questions.clear()
        self.state.world.starting_bond_suggestions.clear()
        self.world_state.apply_world_profile(self.state.world)
        return candidates

    def record_first_act_vote(self, voter: str, candidate_id: str) -> None:
        self.prologue_manager.record_vote(self.state.world, voter, candidate_id)
        self.world_state.apply_world_profile(self.state.world)

    def confirm_first_act(self, candidate_id: str = "") -> FirstActCandidate | None:
        result = self.prologue_manager.confirm_winner(self.state.world, candidate_id)
        winner = result.winner
        if winner is not None:
            self.state.stage = SessionZeroStage.PROLOGUE
            self.world_state.apply_world_profile(self.state.world)
        return winner

    def first_act_vote_result(self):
        return self.prologue_manager.vote_result(self.state.world)

    def gm_secret_audit_report(self, *, include_content: bool = True) -> GMSecretAuditReport:
        entries: list[GMSecretAuditEntry] = []
        warnings: list[str] = []
        for secret in self.world_state.gm_secrets.values():
            lock_value = secret.lock_level.value if isinstance(secret.lock_level, SecretLockLevel) else str(secret.lock_level)
            risks: list[str] = []
            if not secret.related_entities:
                risks.append("缺少关联实体，后续检索可能不稳定。")
            if lock_value in {SecretLockLevel.SEEDED.value, SecretLockLevel.PUBLIC.value} and not secret.public_clues:
                risks.append("已埋线或已公开的暗线缺少公开线索记录。")
            if lock_value == SecretLockLevel.PUBLIC.value:
                risks.append("已公开事实不应随意改写，只能补充解释。")
            if not secret.title or not secret.content:
                risks.append("标题或内容为空。")
            if risks:
                warnings.append(f"{secret.title or secret.secret_id}：" + "；".join(risks))
            entries.append(
                GMSecretAuditEntry(
                    secret_id=secret.secret_id,
                    title=secret.title,
                    lock_level=lock_value,
                    related_entities=list(secret.related_entities),
                    public_clues=list(secret.public_clues),
                    revision_count=len(secret.revisions),
                    tags=list(secret.tags),
                    content=secret.content if include_content else "",
                    risks=risks,
                )
            )
        orphan_notes = [
            note
            for note in self.state.world.gm_secret_notes
            if not any(note in secret.content or secret.content in note for secret in self.world_state.gm_secrets.values())
        ]
        if orphan_notes:
            warnings.append(f"有 {len(orphan_notes)} 条旧式 GM 私密笔记尚未结构化为 GMSecret。")
        public_facts = [
            f"{secret.title}：{', '.join(secret.public_clues)}"
            for secret in self.world_state.gm_secrets.values()
            if (secret.lock_level == SecretLockLevel.PUBLIC or str(secret.lock_level) == SecretLockLevel.PUBLIC.value)
        ]
        summary = f"结构化暗线 {len(entries)} 条，旧式私密笔记 {len(orphan_notes)} 条，风险提示 {len(warnings)} 条。"
        return GMSecretAuditReport(
            entries=entries,
            orphan_notes=orphan_notes if include_content else [f"{len(orphan_notes)} 条旧式私密笔记"],
            public_facts=public_facts,
            warnings=warnings,
            summary=summary,
        )

    def record_participant_contribution(self, speaker: str, message: str) -> None:
        participant = self.find_participant(speaker)
        if participant is None:
            return
        topic = self.topic_for_stage(self.state.stage)
        pending_topic = self.topic_for_pending_question(participant.pending_question)
        participant.contributions.append(message)
        if topic not in participant.answered_topics:
            participant.answered_topics.append(topic)
        if pending_topic and pending_topic not in participant.answered_topics:
            participant.answered_topics.append(pending_topic)
        for inferred_topic in self.topics_from_message(message):
            if inferred_topic not in participant.answered_topics:
                participant.answered_topics.append(inferred_topic)
        participant.pending_question = ""
        self.advance_participant(topic=topic, after_speaker=speaker)

    def find_participant(self, speaker: str) -> SessionZeroParticipant | None:
        for participant in self.state.participants:
            if participant.name == speaker:
                return participant
        return None

    def advance_participant(self, *, topic: str | None = None, after_speaker: str | None = None) -> str:
        if not self.state.participants:
            return ""
        topic = topic or self.topic_for_stage(self.state.stage)
        start_index = self.state.current_participant_index
        if after_speaker:
            for index, participant in enumerate(self.state.participants):
                if participant.name == after_speaker:
                    start_index = index
                    break
        count = len(self.state.participants)
        for offset in range(1, count + 1):
            next_index = (start_index + offset) % count
            candidate = self.state.participants[next_index]
            if topic not in candidate.answered_topics:
                self.state.current_participant_index = next_index
                return candidate.name
        self.state.current_participant_index = (start_index + 1) % count
        self.state.polling_round += 1
        return self.current_participant_name()

    def align_current_participant_to_stage(self) -> None:
        if not self.state.participants:
            return
        topic = self.topic_for_stage(self.state.stage)
        current = self.state.current_participant()
        if current is not None and topic not in current.answered_topics:
            return
        for index, participant in enumerate(self.state.participants):
            if topic not in participant.answered_topics:
                self.state.current_participant_index = index
                return
        self.state.current_participant_index %= len(self.state.participants)

    def participant_polling_ready(self) -> bool:
        if not self.state.participants:
            return True
        return all(participant.contributions for participant in self.state.participants)

    def assign_pending_question(self, questions: list[str]) -> None:
        participant = self.state.current_participant()
        if participant is None:
            return
        participant.pending_question = questions[0] if questions else ""

    def topic_for_stage(self, stage: SessionZeroStage) -> str:
        return stage.value

    def topic_for_pending_question(self, question: str) -> str:
        text = str(question or "")
        if "王国" in text or "国家" in text:
            return "kingdom_contributions"
        if "历史事件" in text or "重大历史" in text:
            return "historical_event_contributions"
        if "奥秘" in text or "谜团" in text:
            return "mystery_contributions"
        if "世界性威胁" in text or "世界威胁" in text or "可怕威胁" in text:
            return "threat_contributions"
        return ""

    def topics_from_message(self, message: str) -> list[str]:
        text = str(message or "")
        topics: list[str] = []
        if "贡献" in text and any(
            token in text
            for token in (
                "王国",
                "国家",
                "公国",
                "帝国",
                "城邦",
                "联邦",
                "保护国",
                "地区",
                "区域",
                "地点",
                "群岛",
                "村社",
                "部落",
            )
        ):
            topics.append("kingdom_contributions")
        if "贡献" in text and any(token in text for token in ("历史事件", "重大历史")):
            topics.append("historical_event_contributions")
        if "贡献" in text and any(token in text for token in ("奥秘", "谜团", "谜")):
            topics.append("mystery_contributions")
        if "贡献" in text and "威胁" in text:
            topics.append("threat_contributions")
        return topics

    def _extend_unique(self, target: list[str], values: list[str]) -> None:
        for value in self._string_list(values):
            if not value:
                continue
            normalized_value = self._semantic_key(value)
            if any(self._semantic_key(existing) == normalized_value for existing in target):
                continue
            if any(self._semantic_contains(existing, value) for existing in target):
                continue
            target[:] = [existing for existing in target if not self._semantic_contains(value, existing)]
            target.append(value)

    def _semantic_key(self, value: str) -> str:
        text = str(value or "").strip()
        return re.sub(r"[。！？；;，,\s]+", "", text)

    def _semantic_contains(self, left: str, right: str) -> bool:
        left_key = self._semantic_key(left)
        right_key = self._semantic_key(right)
        if not left_key or not right_key or left_key == right_key:
            return False
        if min(len(left_key), len(right_key)) < 8:
            return False
        return right_key in left_key

    def _remove_values(self, target: list[str], values: list[str]) -> None:
        for value in values:
            if value in target:
                target.remove(value)

    def _apply_first_act_updates(self, updates: dict) -> None:
        world = self.state.world
        if "first_act_candidates" in updates:
            candidates = []
            raw_candidates = updates.get("first_act_candidates", [])
            if isinstance(raw_candidates, list):
                for index, raw in enumerate(raw_candidates, start=1):
                    if isinstance(raw, FirstActCandidate):
                        candidates.append(raw)
                    elif isinstance(raw, dict):
                        candidate_id = str(raw.get("candidate_id") or f"first_act_{index}")
                        candidates.append(
                            FirstActCandidate(
                                candidate_id=candidate_id,
                                title=str(raw.get("title", "")),
                                group_key=str(raw.get("group_key", "")),
                                option=int(raw.get("option", index) or index),
                                premise=str(raw.get("premise", "")),
                                questions=self._string_list(raw.get("questions", [])),
                                suggested_bonds=self._string_list(raw.get("suggested_bonds", [])),
                                notes=self._string_list(raw.get("notes", [])),
                                votes=self._string_list(raw.get("votes", [])),
                            )
                        )
            world.first_act_candidates = candidates
        if isinstance(updates.get("first_act_votes"), dict):
            for voter, candidate_id in updates["first_act_votes"].items():
                resolved = self.prologue_manager.resolve_candidate_id(world, str(candidate_id))
                if resolved:
                    world.first_act_votes[str(voter)] = resolved
        if updates.get("selected_first_act_id"):
            self.prologue_manager.confirm_winner(world, str(updates["selected_first_act_id"]))
        elif updates.get("selected_first_act_summary"):
            summary = str(updates["selected_first_act_summary"])
            if world.selected_first_act_id or world.selected_first_act_summary != summary:
                world.selected_first_act_id = ""
                world.first_act_questions.clear()
                world.first_act_question_answers.clear()
                world.first_act_skipped_questions.clear()
                world.starting_bond_suggestions.clear()
            world.selected_first_act_summary = summary
            self.state.stage = SessionZeroStage.PROLOGUE
        self._extend_unique(world.starting_bond_suggestions, self._string_list(updates.get("starting_bond_suggestions", [])))

    def _apply_world_removals(self, removals: dict) -> None:
        if not isinstance(removals, dict):
            return
        world = self.state.world
        for field_name in (
            "core_themes",
            "villain_seeds",
            "villain_mirrors",
            "mysteries",
            "safety_lines",
            "safety_veils",
            "gm_secret_notes",
        ):
            values = removals.get(field_name, [])
            if isinstance(values, list):
                self._remove_values(getattr(world, field_name), values)
        for field_name in ("pillars", "major_locations", "factions"):
            values = removals.get(field_name, [])
            if isinstance(values, list):
                for key in values:
                    getattr(world, field_name).pop(key, None)

    def _apply_hero_draft_updates(self, updates: dict) -> None:
        if not isinstance(updates, dict):
            return
        for raw_key, raw_patch in updates.items():
            if not isinstance(raw_patch, dict):
                continue
            key = str(raw_key).strip() or str(raw_patch.get("player_name", "")).strip()
            if not key:
                key = str(raw_patch.get("hero_name", "")).strip()
            if not key:
                continue
            if key not in self.state.world.hero_drafts and not self._hero_draft_patch_has_content(raw_patch):
                continue
            draft = self.state.world.hero_drafts.setdefault(key, HeroDraft(player_name=key))
            self._apply_hero_draft_patch(draft, raw_patch)

    def _hero_draft_patch_has_content(self, patch: dict) -> bool:
        for field_name in ("hero_name", "identity", "theme", "origin"):
            if str(patch.get(field_name, "")).strip():
                return True
        for field_name in (
            "classes",
            "attributes",
            "skills",
            "skill_options",
            "equipment_slots",
        ):
            values = patch.get(field_name, {})
            if isinstance(values, dict) and any(value not in ("", None) for value in values.values()):
                return True
        class_preferences = patch.get("class_preferences", [])
        if isinstance(class_preferences, list) and any(
            str(value).strip() for value in class_preferences
        ):
            return True
        for field_name in ("bonds", "spells", "bound_arcana", "equipment", "notes", "open_questions"):
            values = patch.get(field_name, [])
            if isinstance(values, str) and values.strip():
                return True
            if isinstance(values, list) and any(str(value).strip() for value in values):
                return True
        return False

    def _apply_hero_draft_patch(self, draft: HeroDraft, patch: dict) -> None:
        legacy_zero_level_classes = [
            str(class_name).strip()
            for class_name, level in draft.classes.items()
            if int(level or 0) <= 0 and str(class_name).strip()
        ]
        if legacy_zero_level_classes:
            draft.class_preferences = list(
                dict.fromkeys(
                    [*draft.class_preferences, *legacy_zero_level_classes]
                )
            )[:3]
            for class_name in legacy_zero_level_classes:
                draft.classes.pop(class_name, None)
        for field_name in ("player_name", "hero_name", "identity", "theme", "origin"):
            if field_name in patch and patch[field_name] is not None:
                clean_value = str(patch[field_name]).strip()
                if clean_value:
                    setattr(draft, field_name, clean_value)
        if "confirmed" in patch and bool(patch["confirmed"]):
            draft.confirmed = True
        if patch.get("replace_classes"):
            draft.classes.clear()
        if patch.get("replace_skills"):
            draft.skills.clear()
            draft.skill_options.clear()
        if patch.get("replace_equipment"):
            draft.equipment.clear()
            draft.equipment_slots.clear()
        class_preferences = patch.get("class_preferences")
        if isinstance(class_preferences, list):
            draft.class_preferences = [
                str(value).strip()
                for value in class_preferences
                if str(value).strip()
            ]
        increment_skills = bool(patch.get("increment_skills"))
        for field_name in ("classes", "attributes", "skills"):
            values = patch.get(field_name, {})
            if isinstance(values, dict):
                target = getattr(draft, field_name)
                for key, value in values.items():
                    clean_key = str(key)
                    if field_name == "skills":
                        clean_key = normalize_skill_reference_name(clean_key)
                        matching_keys = [
                            stored_key
                            for stored_key in target
                            if normalize_skill_reference_name(stored_key) == clean_key
                        ]
                        current_rank = sum(
                            int(target.get(stored_key, 0) or 0)
                            for stored_key in matching_keys
                        )
                        for stored_key in matching_keys:
                            target.pop(stored_key, None)
                    if value in ("", None):
                        target.pop(clean_key, None)
                    else:
                        parsed = self._parse_numeric_patch_value(value)
                        if field_name == "skills" and increment_skills:
                            target[clean_key] = current_rank + parsed
                        else:
                            target[clean_key] = parsed
        if draft.classes and sum(draft.classes.values()) == 5:
            draft.class_preferences.clear()
        skill_options = patch.get("skill_options")
        if isinstance(skill_options, dict):
            for skill_name, choices in skill_options.items():
                clean_skill_name = normalize_skill_reference_name(str(skill_name))
                self._remove_skill_mapping_entries(
                    draft.skill_options,
                    [clean_skill_name],
                )
                if choices in (None, ""):
                    continue
                if not isinstance(choices, list):
                    raise ValueError("技能附带选择必须使用字符串数组。")
                clean_choices = [str(choice).strip() for choice in choices if str(choice).strip()]
                if clean_choices:
                    draft.skill_options[clean_skill_name] = clean_choices
        equipment_slots = patch.get("equipment_slots")
        if isinstance(equipment_slots, dict):
            allowed_slots = {"main_hand", "off_hand", "armor", "shield"}
            for raw_slot, raw_value in equipment_slots.items():
                slot = str(raw_slot).strip()
                if slot not in allowed_slots:
                    raise ValueError(f"未知装备栏位：{slot}")
                value = str(raw_value or "").strip()
                if value:
                    draft.equipment_slots[slot] = value
                else:
                    draft.equipment_slots.pop(slot, None)
        for field_name in ("bonds", "spells", "bound_arcana", "equipment", "notes", "open_questions"):
            values = patch.get(field_name, [])
            if isinstance(values, str):
                values = [values]
            if isinstance(values, list):
                clean_values = [str(value).strip() for value in values if str(value).strip()]
                if field_name == "equipment" and patch.get("replace_equipment"):
                    # Two identical shields are two purchased items.  The
                    # generic list merger intentionally deduplicates prose,
                    # so complete equipment replacement must preserve count.
                    draft.equipment.extend(clean_values)
                else:
                    self._extend_unique(getattr(draft, field_name), clean_values)
        self._clear_hero_draft_fields(draft, patch.get("remove_fields", []))
        self._remove_values(draft.bonds, self._string_list(patch.get("remove_bonds", [])))
        self._remove_values(draft.spells, self._string_list(patch.get("remove_spells", [])))
        self._remove_values(draft.bound_arcana, self._string_list(patch.get("remove_bound_arcana", [])))
        removed_equipment = self._string_list(patch.get("remove_equipment", []))
        self._remove_values(draft.equipment, removed_equipment)
        removed_names = {str(value).strip() for value in removed_equipment}
        remaining_displays = {
            re.split(r"[（(]|=>|->|=|＝", str(value).strip(), maxsplit=1)[0].strip()
            for value in draft.equipment
            if str(value).strip()
        }
        for slot, item_name in list(draft.equipment_slots.items()):
            clean_item = str(item_name).strip()
            if clean_item in removed_names and clean_item not in remaining_displays:
                draft.equipment_slots.pop(slot, None)
        self._remove_values(draft.notes, self._string_list(patch.get("remove_notes", [])))
        for field_name, removal_name in (
            ("classes", "remove_classes"),
            ("attributes", "remove_attributes"),
        ):
            for key in self._string_list(patch.get(removal_name, [])):
                getattr(draft, field_name).pop(key, None)
        removed_skills = self._string_list(patch.get("remove_skills", []))
        self._remove_skill_mapping_entries(draft.skills, removed_skills)
        self._remove_skill_mapping_entries(draft.skill_options, removed_skills)
        self._remove_skill_mapping_entries(
            draft.skill_options,
            self._string_list(patch.get("remove_skill_options", [])),
        )

    @staticmethod
    def _remove_skill_mapping_entries(
        mapping: dict,
        requested_names: list[str],
    ) -> None:
        canonical_names = {
            normalize_skill_reference_name(str(name))
            for name in requested_names
            if str(name).strip()
        }
        if not canonical_names:
            return
        for stored_name in list(mapping):
            if normalize_skill_reference_name(str(stored_name)) in canonical_names:
                mapping.pop(stored_name, None)

    def _apply_hero_draft_deletions(self, deletions: dict) -> None:
        if not isinstance(deletions, dict):
            return
        for key, fields_to_clear in deletions.items():
            draft = self.state.world.hero_drafts.get(str(key))
            if draft is None:
                continue
            self._clear_hero_draft_fields(draft, fields_to_clear)

    def _delete_hero_drafts(self, draft_keys: list[str]) -> None:
        for key in self._string_list(draft_keys):
            self.state.world.hero_drafts.pop(key, None)

    def _parse_numeric_patch_value(self, value) -> int:
        if isinstance(value, int):
            return value
        text = str(value).strip().lower()
        if text.startswith("d") and text[1:].isdigit():
            return int(text[1:])
        return int(text)

    def _clear_hero_draft_fields(self, draft: HeroDraft, fields_to_clear) -> None:
        for field_name in self._string_list(fields_to_clear):
            if field_name in {"player_name", "hero_name", "identity", "theme", "origin"}:
                setattr(draft, field_name, "")
            elif field_name in {
                "classes",
                "attributes",
                "skills",
                "skill_options",
                "equipment_slots",
            }:
                getattr(draft, field_name).clear()
            elif field_name == "class_preferences":
                draft.class_preferences.clear()
            elif field_name in {"bonds", "spells", "bound_arcana", "equipment", "notes", "open_questions"}:
                getattr(draft, field_name).clear()
                if field_name == "equipment":
                    draft.equipment_slots.clear()
            elif field_name == "confirmed":
                draft.confirmed = False

    def _world_fact_list(self, values, field_name: str) -> list[str]:
        facts: list[str] = []
        for value in self._string_list(values):
            cleaned = self._clean_world_fact(value, field_name)
            if cleaned:
                facts.append(cleaned)
        return facts

    def _clean_world_fact(self, value: str, field_name: str) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip(" ，,。；;")
        if not text:
            return ""
        vote_markers = ("我投这个", "我投", "投这个", "投票", "第一幕我选", "我选")
        villain_markers = ("额外补一个反派种子", "补充反派种子", "反派种子")
        if field_name == "villain_seeds":
            for marker in villain_markers:
                if marker in text:
                    text = text.split(marker, 1)[1]
                    text = re.sub(r"^[：:，,。\s]+", "", text)
                    break
            if any(text.startswith(marker) for marker in vote_markers):
                return ""
            return text.strip(" ，,。；;")
        cut_positions = []
        for marker in (*vote_markers, *villain_markers):
            index = text.find(marker)
            if index >= 0:
                cut_positions.append(index)
        if cut_positions:
            text = text[: min(cut_positions)]
        return text.strip(" ，,。；;")

    def _string_list(self, values) -> list[str]:
        if isinstance(values, str):
            return [values]
        if not isinstance(values, list):
            return []
        return [self._stringify_value(value) for value in values if self._stringify_value(value)]

    def _stringify_value(self, value) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            return self._stringify_mapping(value)
        if isinstance(value, (list, tuple)):
            parts = [self._stringify_value(item) for item in value]
            return "；".join(part for part in parts if part).strip()
        return str(value).strip()

    def _stringify_mapping(self, value: dict) -> str:
        clean = {str(key): self._stringify_value(item) for key, item in value.items() if self._stringify_value(item)}
        if not clean:
            return ""
        title = (
            clean.get("name")
            or clean.get("title")
            or clean.get("subject")
            or clean.get("villain")
            or clean.get("faction")
            or clean.get("location")
        )
        description = (
            clean.get("description")
            or clean.get("summary")
            or clean.get("content")
            or clean.get("goal")
            or clean.get("secret")
            or clean.get("mystery")
            or clean.get("threat")
            or clean.get("note")
        )
        extra_parts: list[str] = []
        for key, label in (
            ("origin", "来源"),
            ("motivation", "动机"),
            ("method", "手段"),
            ("stake", "赌注"),
            ("question", "问题"),
            ("theme", "主题"),
        ):
            if clean.get(key):
                extra_parts.append(f"{label}：{clean[key]}")
        if title and description:
            return "；".join([f"{title}：{description}", *extra_parts]).strip()
        if title:
            return "；".join([title, *extra_parts]).strip()
        if description:
            return "；".join([description, *extra_parts]).strip()
        return "；".join(f"{key}：{item}" for key, item in clean.items()).strip()

    def _jsonable(self, value):
        if is_dataclass(value):
            return asdict(value)
        if isinstance(value, dict):
            return {key: self._jsonable(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._jsonable(item) for item in value]
        return value
