from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from copy import deepcopy

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
from fu_gm.skill_library import required_spell_slots
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


class SessionZeroManager:
    """维护 Session 0 的结构化世界档案，并写回 WorldState。"""

    def __init__(self, world_state: WorldState) -> None:
        self.world_state = world_state
        self.state = SessionZeroState()
        self.prologue_manager = PrologueManager()

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
        for field_name in (
            "campaign_title",
            "continent_name",
            "world_style",
            "map_card",
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
            world.major_locations[self._stringify_value(key)] = self._stringify_value(value)
        map_locations = updates.get("map_locations", [])
        if isinstance(map_locations, dict):
            map_locations = [dict(value, name=key) if isinstance(value, dict) else {"name": key, "description": value} for key, value in map_locations.items()]
        for item in map_locations if isinstance(map_locations, list) else []:
            if not isinstance(item, dict):
                continue
            name = self._stringify_value(item.get("name", "")).strip()
            if not name:
                continue
            description = self._stringify_value(item.get("description", ""))
            if description:
                world.major_locations[name] = description
            self.world_state.upsert_map_location(
                name,
                description=description,
                terrain=self._stringify_value(item.get("terrain", "")),
                feature_type=self._stringify_value(item.get("feature_type", "")),
                position_hint=self._stringify_value(item.get("position_hint", "")),
                relative_to=self._stringify_value(item.get("relative_to", "")),
                relative_position=self._stringify_value(item.get("relative_position", "")),
                faction=self._stringify_value(item.get("faction", "")),
                draw_icon=item.get("draw_icon") if isinstance(item.get("draw_icon"), bool) else None,
            )
        for key, value in updates.get("kingdoms", {}).items():
            name = self._normalize_polity_key(key)
            if name:
                world.kingdoms[name] = self._stringify_value(value)
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
        self._refresh_gm_guidance(world)

    def _refresh_gm_guidance(self, world: WorldCreationProfile) -> None:
        guidance = build_gm_guidance(world)
        world.gm_inspiration_tags = list(guidance.inspiration_tags)
        world.gm_guidance_notes = list(guidance.principles[:6])
        world.gm_story_beats = list(guidance.story_beats[:5])
        world.gm_prepared_locations = {
            seed.name: f"{seed.archetype}：{seed.brief}" for seed in guidance.location_seeds[:6]
        }

    def progress_summary(self) -> dict[str, bool]:
        world = self.state.world
        heroes_ready = self._hero_creation_ready(world)
        world_creation_ready = self._world_creation_ready(world)
        first_act_prerequisites_ready = (
            world_creation_ready
            and bool(world.group_concept)
            and bool(world.safety_lines or world.safety_veils)
            and heroes_ready
        )
        return {
            "map_card": bool(world.map_card),
            "magic_tech_role": bool(world.magic_tech_role),
            "kingdoms": bool(world.kingdoms),
            "kingdom_contributions": True,
            "historical_events": bool(world.historical_events),
            "historical_event_contributions": True,
            "mysteries": bool(world.mysteries),
            "mystery_contributions": True,
            "world_threats": bool(world.world_threats),
            "threat_contributions": True,
            "group_concept": bool(world.group_concept),
            "safety": bool(world.safety_lines or world.safety_veils),
            "heroes": heroes_ready,
            "first_act": (not first_act_prerequisites_ready) or bool(world.selected_first_act_id),
            "participant_polling": self.participant_polling_ready(),
        }

    def world_creation_ready(self) -> bool:
        return self._world_creation_ready(self.state.world)

    def hero_creation_status(self) -> dict[str, object]:
        world = self.state.world
        participants = [participant.name for participant in self.state.participants]
        if not participants:
            participants = [draft.player_name or key for key, draft in world.hero_drafts.items()]
        missing_by_player: dict[str, list[str]] = {}
        for player in participants:
            draft_key, draft = self._draft_for_player(player)
            if draft is None:
                missing_by_player[player or "未命名玩家"] = ["完整角色草稿"]
                continue
            missing = self._hero_missing_fields(draft)
            if missing:
                label = draft.hero_name or draft.player_name or draft_key or player
                missing_by_player[label] = missing
        if not participants and not world.hero_drafts:
            missing_by_player["玩家角色"] = ["完整角色草稿"]
        return {
            "ready": bool(world.hero_drafts) and not missing_by_player,
            "missing_by_player": missing_by_player,
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
            "map_card": "地图基础信息",
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
        return [labels[key] for key, ready in self.progress_summary().items() if not ready]

    def _world_creation_ready(self, world: WorldCreationProfile) -> bool:
        return (
            bool(world.map_card)
            and bool(world.magic_tech_role)
            and bool(world.kingdoms)
            and bool(world.historical_events)
            and bool(world.mysteries)
            and bool(world.world_threats)
        )

    def _participant_contribution_ready(self, contributors: dict[str, list[str]], topic: str = "") -> bool:
        if len(self.state.participants) <= 1:
            return True
        answered = {str(name).strip() for name in contributors if str(name).strip()}
        return all(
            participant.name in answered or (bool(topic) and topic in participant.answered_topics)
            for participant in self.state.participants
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

    def _hero_missing_fields(self, draft: HeroDraft) -> list[str]:
        missing: list[str] = []
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
        return missing

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
        if not name or name.startswith("的") or len(name) > 16:
            return ""
        if any(token in name for token in ("我的角色", "角色", "大钟", "能安抚", "是", "叫", "想", "我要")):
            return ""
        if re.search(r"[，,。！？；;\s]", name):
            return ""
        if not re.search(r"(?:王国|帝国|城邦|共和国|公国|部族|联盟|同盟)$", name) and len(name) < 2:
            return ""
        return name

    def snapshot(self) -> dict:
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
                }
                for participant in self.state.participants
            ],
            "current_participant": self.current_participant_name(),
            "polling_round": self.state.polling_round,
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
            world.selected_first_act_summary = str(updates["selected_first_act_summary"])
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
        for field_name in ("classes", "attributes", "skills"):
            values = patch.get(field_name, {})
            if isinstance(values, dict) and any(value not in ("", None) for value in values.values()):
                return True
        for field_name in ("bonds", "spells", "bound_arcana", "equipment", "notes", "open_questions"):
            values = patch.get(field_name, [])
            if isinstance(values, str) and values.strip():
                return True
            if isinstance(values, list) and any(str(value).strip() for value in values):
                return True
        return False

    def _apply_hero_draft_patch(self, draft: HeroDraft, patch: dict) -> None:
        for field_name in ("player_name", "hero_name", "identity", "theme", "origin"):
            if field_name in patch and patch[field_name] is not None:
                clean_value = str(patch[field_name]).strip()
                if clean_value:
                    setattr(draft, field_name, clean_value)
        if "confirmed" in patch and bool(patch["confirmed"]):
            draft.confirmed = True
        if patch.get("replace_skills"):
            draft.skills.clear()
        for field_name in ("classes", "attributes", "skills"):
            values = patch.get(field_name, {})
            if isinstance(values, dict):
                target = getattr(draft, field_name)
                for key, value in values.items():
                    if value in ("", None):
                        target.pop(str(key), None)
                    else:
                        target[str(key)] = self._parse_numeric_patch_value(value)
        for field_name in ("bonds", "spells", "bound_arcana", "equipment", "notes", "open_questions"):
            values = patch.get(field_name, [])
            if isinstance(values, str):
                values = [values]
            if isinstance(values, list):
                self._extend_unique(getattr(draft, field_name), [str(value) for value in values if str(value).strip()])
        self._clear_hero_draft_fields(draft, patch.get("remove_fields", []))
        self._remove_values(draft.bonds, self._string_list(patch.get("remove_bonds", [])))
        self._remove_values(draft.spells, self._string_list(patch.get("remove_spells", [])))
        self._remove_values(draft.bound_arcana, self._string_list(patch.get("remove_bound_arcana", [])))
        self._remove_values(draft.equipment, self._string_list(patch.get("remove_equipment", [])))
        self._remove_values(draft.notes, self._string_list(patch.get("remove_notes", [])))
        for field_name, removal_name in (
            ("classes", "remove_classes"),
            ("attributes", "remove_attributes"),
            ("skills", "remove_skills"),
        ):
            for key in self._string_list(patch.get(removal_name, [])):
                getattr(draft, field_name).pop(key, None)

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
            elif field_name in {"classes", "attributes", "skills"}:
                getattr(draft, field_name).clear()
            elif field_name in {"bonds", "spells", "bound_arcana", "equipment", "notes", "open_questions"}:
                getattr(draft, field_name).clear()
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
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        return str(value).strip()

    def _jsonable(self, value):
        if is_dataclass(value):
            return asdict(value)
        if isinstance(value, dict):
            return {key: self._jsonable(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._jsonable(item) for item in value]
        return value
