from __future__ import annotations

import re
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fu_gm.models import (
    ChapterPackage,
    DecisionWindow,
    GMSecret,
    GMSecretRevision,
    IconicElementState,
    MapLocation,
    MapRouteEdge,
    MapRouteSegment,
    SemanticMapLayout,
    MemoryEvent,
    MemoryRecallResult,
    MemoryRelation,
    MemoryVisibility,
    NPCPersona,
    NPCCombatBlueprint,
    PartySheet,
    PendingCheckBatch,
    PersistentChange,
    PersistentChangeType,
    SecretLockLevel,
    StoryItem,
    StoryItemEvent,
    StoryItemStatus,
    TransparencyAuditEntry,
    WorldCreationProfile,
    WorldSheet,
    normalize_memory_visibility,
)
from fu_gm.gm_guidance import build_gm_guidance
from fu_gm.optional_rules import apply_optional_rule_state, normalize_optional_rule_key
from fu_gm.optional_rules import optional_rule_rows


class WorldState:
    def __init__(self) -> None:
        self.session_pillars: list[str] = []
        self.map_notes: dict[str, str] = {}
        self.map_locations: dict[str, MapLocation] = {}
        self.map_routes: dict[str, MapRouteEdge] = {}
        self.semantic_map = SemanticMapLayout()
        self.npc_relationships: dict[str, list[str]] = {}
        self.memories: list[str] = []
        self.npc_personas: dict[str, NPCPersona] = {}
        # Prepared rules cards are private GM state.  They are intentionally
        # separate from personas so an NPC may exist socially without carrying
        # combat numbers, and so background design never bloats chat context.
        self.npc_combat_blueprints: dict[str, NPCCombatBlueprint] = {}
        self.subject_facts: dict[str, list[str]] = {}
        self.persistent_changes: list[PersistentChange] = []
        self.story_items: dict[str, StoryItem] = {}
        self.memory_events: list[MemoryEvent] = []
        self.memory_relations: list[MemoryRelation] = []
        self.gm_secrets: dict[str, GMSecret] = {}
        self.world_profile = WorldCreationProfile()
        self.party_sheet: PartySheet | None = None
        self.world_sheet: WorldSheet | None = None
        self.present_players: list[str] = []
        self.absent_players: dict[str, str] = {}
        self.chapter_packages: dict[str, ChapterPackage] = {}
        self.active_chapter_package: str = ""
        self.iconic_elements: dict[str, IconicElementState] = {}
        self.transparency_audit_log: list[TransparencyAuditEntry] = []
        self.decision_windows: dict[str, DecisionWindow] = {}
        self.pending_check_batches: dict[str, PendingCheckBatch] = {}
        self.check_batch_history: list[PendingCheckBatch] = []

    def add_memory(self, memory: str) -> None:
        self.memories.append(memory)

    def mark_player_present(self, player_name: str) -> None:
        """记录玩家当前在桌边。

        这里不自动写入公开记忆，避免每次普通发言都污染长期剧情记忆。
        离席/回归这类明确桌面状态变化由 HTTP 层写入事件。
        """

        player_name = player_name.strip()
        if not player_name or player_name == "AI GM":
            return
        if player_name not in self.present_players:
            self.present_players.append(player_name)
        self.absent_players.pop(player_name, None)

    def mark_player_absent(self, player_name: str, reason: str = "") -> None:
        player_name = player_name.strip()
        if not player_name or player_name == "AI GM":
            return
        if player_name not in self.present_players:
            self.present_players.append(player_name)
        self.absent_players[player_name] = reason.strip()

    def attendance_snapshot(self) -> dict[str, list[str] | dict[str, str]]:
        active = [player for player in self.present_players if player not in self.absent_players]
        return {
            "present_players": list(self.present_players),
            "active_players": active,
            "absent_players": dict(self.absent_players),
        }

    def format_attendance(self) -> list[str]:
        snapshot = self.attendance_snapshot()
        active = snapshot["active_players"]
        absent = snapshot["absent_players"]
        lines: list[str] = []
        if active:
            lines.append("当前在场玩家：" + "、".join(active))
        if absent:
            absent_text = "、".join(
                f"{player}（{reason or '临时离席'}）" for player, reason in absent.items()
            )
            lines.append("当前离席玩家：" + absent_text)
            lines.append("离席玩家对应角色不得被 AI GM 擅自决定重大行动；需要暂停、存档或征得代管同意。")
        return lines

    def record_memory_event(
        self,
        summary: str,
        *,
        kind: str = "note",
        visibility: MemoryVisibility | str = MemoryVisibility.PUBLIC,
        entities: list[str] | None = None,
        tags: list[str] | None = None,
        source: str = "",
        payload: dict | None = None,
    ) -> MemoryEvent:
        event = MemoryEvent(
            event_id=str(uuid4()),
            created_at=self._now(),
            kind=kind,
            summary=summary,
            visibility=normalize_memory_visibility(visibility),
            entities=list(entities or []),
            tags=list(tags or []),
            source=source,
            payload=dict(payload or {}),
        )
        self.memory_events.append(event)
        if event.visibility == MemoryVisibility.PUBLIC:
            self._add_memory_once(summary)
        return event

    def record_relation(
        self,
        source: str,
        relation: str,
        target: str,
        *,
        visibility: MemoryVisibility | str = MemoryVisibility.PUBLIC,
        evidence: str = "",
        tags: list[str] | None = None,
    ) -> MemoryRelation:
        candidate = MemoryRelation(
            source=source,
            relation=relation,
            target=target,
            visibility=normalize_memory_visibility(visibility),
            evidence=evidence,
            tags=list(tags or []),
        )
        for existing in self.memory_relations:
            if (
                existing.source == candidate.source
                and existing.relation == candidate.relation
                and existing.target == candidate.target
                and existing.visibility == candidate.visibility
            ):
                if evidence and not existing.evidence:
                    existing.evidence = evidence
                for tag in candidate.tags:
                    if tag not in existing.tags:
                        existing.tags.append(tag)
                return existing
        self.memory_relations.append(candidate)
        if candidate.visibility == MemoryVisibility.PUBLIC:
            self.remember_subject_fact(source, f"{relation} -> {target}")
        return candidate

    def upsert_gm_secret(
        self,
        secret_id: str,
        *,
        title: str,
        content: str,
        lock_level: SecretLockLevel | str = SecretLockLevel.DRAFT,
        related_entities: list[str] | None = None,
        public_clues: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> GMSecret:
        now = self._now()
        level = SecretLockLevel(lock_level)
        if secret_id in self.gm_secrets:
            secret = self.gm_secrets[secret_id]
            secret.title = title or secret.title
            secret.content = content or secret.content
            secret.lock_level = level
            secret.updated_at = now
            for entity in related_entities or []:
                if entity not in secret.related_entities:
                    secret.related_entities.append(entity)
            for clue in public_clues or []:
                if clue not in secret.public_clues:
                    secret.public_clues.append(clue)
            for tag in tags or []:
                if tag not in secret.tags:
                    secret.tags.append(tag)
            return secret

        secret = GMSecret(
            secret_id=secret_id,
            title=title,
            content=content,
            lock_level=level,
            created_at=now,
            updated_at=now,
            related_entities=list(related_entities or []),
            public_clues=list(public_clues or []),
            tags=list(tags or []),
        )
        self.gm_secrets[secret_id] = secret
        if title and content:
            self._append_gm_secret_note_once(f"{title}：{content}")
        return secret

    def revise_gm_secret(
        self,
        secret_id: str,
        *,
        new_content: str,
        reason: str = "",
        preserve_clues: list[str] | None = None,
        allow_public_revision: bool = False,
    ) -> GMSecret:
        secret = self.gm_secrets[secret_id]
        if secret.lock_level == SecretLockLevel.PUBLIC and not allow_public_revision:
            raise ValueError("该暗线已经成为公开事实，不能由 LLM 擅自修改。")
        now = self._now()
        revision = GMSecretRevision(
            revised_at=now,
            previous_content=secret.content,
            new_content=new_content,
            reason=reason,
            preserve_clues=list(preserve_clues or []),
        )
        secret.revisions.append(revision)
        secret.content = new_content
        secret.updated_at = now
        for clue in preserve_clues or []:
            if clue not in secret.public_clues:
                secret.public_clues.append(clue)
        self.record_memory_event(
            f"GM 私密暗线修订：{secret.title}。理由：{reason or '未注明'}",
            kind="secret_revision",
            visibility=MemoryVisibility.PRIVATE,
            entities=secret.related_entities,
            tags=["gm_secret", *secret.tags],
            payload={"secret_id": secret.secret_id, "preserve_clues": list(preserve_clues or [])},
        )
        return secret

    def set_gm_secret_lock(self, secret_id: str, lock_level: SecretLockLevel | str) -> GMSecret:
        secret = self.gm_secrets[secret_id]
        secret.lock_level = SecretLockLevel(lock_level)
        secret.updated_at = self._now()
        return secret

    def retrieve_relevant_memory(
        self,
        query: str,
        *,
        include_private: bool = False,
        limit: int = 8,
        extra_entities: list[str] | None = None,
    ) -> list[str]:
        terms = self._query_terms(query, extra_entities=extra_entities)
        scored: list[tuple[int, str]] = []

        def visible(visibility: MemoryVisibility) -> bool:
            return include_private or visibility == MemoryVisibility.PUBLIC

        def add_candidate(text: str, *, visibility: MemoryVisibility = MemoryVisibility.PUBLIC) -> None:
            if not text or not visible(visibility):
                return
            lowered = text.lower()
            score = sum(1 for term in terms if term in lowered)
            if score > 0 or not terms:
                scored.append((score, text))

        for memory in self.memories:
            add_candidate(memory)
        for subject, facts in self.subject_facts.items():
            for fact in facts:
                add_candidate(f"{subject}: {fact}")
        for event in self.memory_events:
            add_candidate(f"{event.kind}: {event.summary}", visibility=event.visibility)
        for relation in self.memory_relations:
            add_candidate(
                f"{relation.source} --{relation.relation}--> {relation.target}"
                + (f"（证据：{relation.evidence}）" if relation.evidence else ""),
                visibility=relation.visibility,
            )
        for persona in self.npc_personas.values():
            add_candidate(f"{persona.name}: {persona.public_identity}；{persona.core_drive}；{';'.join(persona.memories)}")
            if include_private:
                add_candidate(f"{persona.name} 的秘密：{';'.join(persona.secrets)}", visibility=MemoryVisibility.PRIVATE)
        for secret in self.gm_secrets.values():
            related = "；".join(secret.related_entities)
            add_candidate(
                f"GM暗线【{secret.title}】：{secret.content}；关联：{related}；线索：{';'.join(secret.public_clues)}",
                visibility=MemoryVisibility.PRIVATE,
            )

        scored.sort(key=lambda item: item[0], reverse=True)
        deduped: list[str] = []
        for _score, text in scored:
            if text not in deduped:
                deduped.append(text)
            if len(deduped) >= limit:
                break
        return deduped

    def recall_context(
        self,
        query: str,
        *,
        include_private: bool = True,
        limit: int = 8,
        extra_entities: list[str] | None = None,
    ) -> MemoryRecallResult:
        entities = self.extract_entities(query, extra_entities=extra_entities)
        public_memory = self.retrieve_relevant_memory(
            query,
            include_private=False,
            limit=limit,
            extra_entities=entities,
        )
        private_memory: list[str] = []
        if include_private:
            private_candidates = self.retrieve_relevant_memory(
                query,
                include_private=True,
                limit=limit * 2,
                extra_entities=entities,
            )
            private_memory = [memory for memory in private_candidates if memory not in public_memory][:limit]
        return MemoryRecallResult(
            query=query,
            entities=entities,
            public_memory=public_memory,
            private_memory=private_memory,
            summary=f"识别实体：{', '.join(entities) if entities else '无'}；公开记忆 {len(public_memory)} 条；私密记忆 {len(private_memory)} 条。",
        )

    def extract_entities(self, text: str, *, extra_entities: list[str] | None = None) -> list[str]:
        candidates = self.known_entity_names(extra_entities=extra_entities)
        found = [entity for entity in candidates if entity and entity in text]
        found.sort(key=len, reverse=True)
        deduped: list[str] = []
        for entity in found:
            if entity not in deduped:
                deduped.append(entity)
        return deduped

    def known_entity_names(self, *, extra_entities: list[str] | None = None) -> list[str]:
        names: set[str] = set(extra_entities or [])
        names.update(self.map_notes)
        names.update(self.map_locations)
        names.update(route.route_id for route in self.map_routes.values())
        names.update(self.npc_relationships)
        names.update(self.npc_personas)
        names.update(self.subject_facts)
        if self.party_sheet is not None:
            names.update(member.hero_name for member in self.party_sheet.members)
            names.update(member.player_name for member in self.party_sheet.members)
        if self.world_sheet is not None:
            names.update(self.world_sheet.major_locations)
            names.update(self.world_sheet.factions)
            for values in (
                self.world_sheet.villain_seeds,
                self.world_sheet.villain_mirrors,
                self.world_sheet.mysteries,
                self.world_sheet.created_assets,
            ):
                names.update(values)
        for event in self.memory_events:
            names.update(event.entities)
        for relation in self.memory_relations:
            names.add(relation.source)
            names.add(relation.target)
        for secret in self.gm_secrets.values():
            names.add(secret.title)
            names.update(secret.related_entities)
            names.update(secret.public_clues)
        for change in self.persistent_changes:
            names.add(change.name)
            if change.owner:
                names.add(change.owner)
            if change.location:
                names.add(change.location)
        return sorted((name for name in names if name), key=len, reverse=True)

    def apply_story_fact(self, fact: str) -> None:
        violation = self.iconic_protection_violation(fact)
        if violation:
            self.record_transparency_audit(
                "iconic_element_protection",
                False,
                violation,
                severity="warning",
                source="WorldState.apply_story_fact",
            )
            raise ValueError(violation)
        self.add_memory(f"已接受物语改写：{fact}")
        self.record_memory_event(
            f"已接受物语改写：{fact}",
            kind="story_change",
            visibility=MemoryVisibility.PUBLIC,
            tags=["story_change"],
        )

    def register_chapter_package(
        self,
        package: ChapterPackage,
        *,
        activate: bool = True,
    ) -> ChapterPackage:
        title = str(package.chapter_title or "").strip()
        if not title:
            raise ValueError("章节包必须有标题。")
        self.chapter_packages[title] = package
        if activate:
            self.active_chapter_package = title
            package.status = "active"
        for element in package.iconic_elements:
            self.register_iconic_element(
                element,
                element_type="chapter",
                description=f"章节【{title}】的标志性元素。",
                source=title,
            )
        self.record_memory_event(
            f"章节包【{title}】已登记：{package.synopsis or '未填写概要'}",
            kind="chapter_package",
            visibility=MemoryVisibility.PRIVATE,
            entities=[title, *package.iconic_elements],
            tags=["chapter", "package"],
            source="WorldState",
        )
        return package

    def active_chapter(self) -> ChapterPackage | None:
        if not self.active_chapter_package:
            return None
        return self.chapter_packages.get(self.active_chapter_package)

    def register_iconic_element(
        self,
        name: str,
        *,
        element_type: str = "generic",
        description: str = "",
        protection_level: str = "protected",
        allowed_interactions: list[str] | None = None,
        restrictions: list[str] | None = None,
        source: str = "",
        notes: list[str] | None = None,
    ) -> IconicElementState:
        name = str(name or "").strip()
        if not name:
            raise ValueError("标志性元素名称不能为空。")
        current = self.iconic_elements.get(name)
        if current is None:
            current = IconicElementState(
                name=name,
                element_type=element_type,
                description=description,
                protection_level=protection_level,
                allowed_interactions=list(allowed_interactions or []),
                restrictions=list(restrictions or []),
                source=source,
                notes=list(notes or []),
            )
            self.iconic_elements[name] = current
        else:
            current.element_type = element_type or current.element_type
            current.description = description or current.description
            current.protection_level = protection_level or current.protection_level
            current.source = source or current.source
            for item in allowed_interactions or []:
                if item not in current.allowed_interactions:
                    current.allowed_interactions.append(item)
            for item in restrictions or []:
                if item not in current.restrictions:
                    current.restrictions.append(item)
            for item in notes or []:
                if item not in current.notes:
                    current.notes.append(item)
        return current

    def iconic_protection_violation(self, text: str) -> str:
        raw = str(text or "")
        if not raw or not self.iconic_elements:
            return ""
        destructive_markers = (
            "摧毁",
            "毁掉",
            "杀死",
            "死亡",
            "打碎",
            "破坏",
            "消失",
            "不存在",
            "改成",
            "其实是",
            "亲属",
            "父亲",
            "母亲",
            "姐姐",
            "哥哥",
            "恋人",
            "属于我",
            "被我拥有",
        )
        for name, element in self.iconic_elements.items():
            if name not in raw or element.protection_level in {"none", "loose"}:
                continue
            if any(marker in raw for marker in destructive_markers):
                return (
                    f"物语改写触碰标志性元素【{name}】。该元素受章节/战役保护，"
                    "不能由普通物语点或叙事写回直接改写、摧毁、改归属或建立重大亲缘关系；"
                    "需要 GM 明确确认。"
                )
        return ""

    def record_transparency_audit(
        self,
        check_name: str,
        passed: bool,
        message: str,
        *,
        severity: str = "info",
        source: str = "",
    ) -> TransparencyAuditEntry:
        entry = TransparencyAuditEntry(
            check_name=check_name,
            passed=bool(passed),
            message=str(message or "").strip(),
            severity=severity,
            source=source,
        )
        self.transparency_audit_log.append(entry)
        del self.transparency_audit_log[:-50]
        return entry

    def chapter_package_prompt(self) -> str:
        package = self.active_chapter()
        if package is None:
            return ""
        lines = [
            f"当前章节包【{package.chapter_title}】（后台使用，不要原样念给玩家）：",
        ]
        if package.synopsis:
            lines.append(f"概要：{package.synopsis}")
        if package.intro_prompt:
            lines.append(f"开场：{package.intro_prompt}")
        if package.shared_creation_slots:
            lines.append("本桌共创占位：" + " / ".join(package.shared_creation_slots[:6]))
        if package.iconic_elements:
            lines.append("标志性元素：" + " / ".join(package.iconic_elements[:6]))
        if package.scenes:
            scene_bits = [
                f"{scene.title}（{scene.purpose or scene.when_to_use or '场景'}）"
                for scene in package.scenes[:6]
            ]
            lines.append("场景候选：" + " / ".join(scene_bits))
        if package.conclusion_prompt:
            lines.append(f"结尾条件：{package.conclusion_prompt}")
        lines.append("原则：固定引子、结尾、标志性元素和场景目标；细节用玩家回答填入占位，不让 PL 代替 GM 主导剧情。")
        return "；".join(lines)

    def iconic_elements_prompt(self) -> str:
        if not self.iconic_elements:
            return ""
        bits = []
        for element in list(self.iconic_elements.values())[:8]:
            restrictions = "、".join(element.restrictions[:3]) if element.restrictions else "不可被普通写回摧毁或改归属"
            bits.append(f"{element.name}（{element.element_type}）：{element.description or restrictions}")
        return (
            "标志性元素保护（后台规则）："
            + " / ".join(bits)
            + "。玩家可互动、调查、请求、围绕它行动，但不能用普通叙事写回直接改变其核心身份、存亡、归属或章节功能。"
        )

    def chapter_audit_payload(self, *, include_private: bool = False, limit: int = 20) -> dict[str, Any]:
        package = self.active_chapter()
        payload: dict[str, Any] = {
            "active": package is not None,
            "active_chapter_package": self.active_chapter_package,
            "registered_packages": list(self.chapter_packages.keys()),
            "iconic_elements": [asdict(element) for element in self.iconic_elements.values()],
            "transparency_audit_log": [asdict(entry) for entry in self.transparency_audit_log[-limit:]],
        }
        if package is not None:
            package_payload = asdict(package)
            if not include_private:
                package_payload.pop("gm_notes", None)
                package_payload.pop("adversary_notes", None)
            payload["package"] = package_payload
        return payload

    def apply_world_profile(self, profile: WorldCreationProfile) -> None:
        self._refresh_gm_guidance(profile)
        self.world_profile = profile
        if profile.pillars:
            self.session_pillars = [f"{name}: {detail}" for name, detail in profile.pillars.items()]
        for location, detail in profile.major_locations.items():
            self.upsert_map_location(location, description=detail)
        for faction, detail in profile.factions.items():
            facts = self.npc_relationships.setdefault(faction, [])
            if detail not in facts:
                facts.append(detail)
        if profile.campaign_title:
            self._add_memory_once(f"Session 0 战役标题：{profile.campaign_title}")
        if profile.continent_name:
            self._add_memory_once(f"Session 0 大陆名称：{profile.continent_name}")
        if profile.magic_tech_role:
            self._add_memory_once(f"Session 0 魔法与科技：{profile.magic_tech_role}")
        if profile.group_concept:
            self._add_memory_once(f"Session 0 小队原型：{profile.group_concept}")
        for kingdom, detail in profile.kingdoms.items():
            self._add_memory_once(f"Session 0 国家【{kingdom}】：{detail}")
        for event in profile.historical_events:
            self._add_memory_once(f"Session 0 历史事件：{event}")
        for threat in profile.world_threats:
            self._add_memory_once(f"Session 0 世界威胁：{threat}")
        if profile.selected_first_act_summary:
            self._add_memory_once(f"Session 0 第一幕：{profile.selected_first_act_summary}")
        for row in optional_rule_rows(profile):
            if row["enabled"]:
                self._add_memory_once(f"Session 0 可选规则已启用：{row['label']}")

    def apply_world_profile_updates(
        self,
        updates: dict[str, Any],
        *,
        source: str = "live_worldbuilding",
    ) -> list[str]:
        if not isinstance(updates, dict):
            return []
        profile = self.world_profile
        changes: list[str] = []

        scalar_fields = {
            "campaign_title",
            "continent_name",
            "world_style",
            "map_card",
            "magic_tech_role",
            "group_concept",
            "starting_region",
            "selected_first_act_summary",
        }
        dict_fields = {
            "major_locations",
            "kingdoms",
            "factions",
            "pillars",
        }
        list_fields = {
            "tone_preferences",
            "playstyle_themes",
            "core_themes",
            "historical_events",
            "villain_seeds",
            "villain_mirrors",
            "mysteries",
            "world_threats",
            "gm_secret_notes",
            "starting_bond_suggestions",
            "open_questions",
        }
        aliases = {
            "locations": "major_locations",
            "location": "major_locations",
            "threats": "world_threats",
            "villains": "villain_seeds",
            "mystery": "mysteries",
            "faction": "factions",
        }

        normalized: dict[str, Any] = {}
        for key, value in updates.items():
            normalized[aliases.get(str(key), str(key))] = value
        audit: dict[str, Any] = {"source": source, "accepted": [], "rejected": []}

        for field_name in scalar_fields:
            raw_value = str(normalized.get(field_name) or "").strip()
            value, reason = self._clean_world_profile_text(field_name, raw_value)
            if raw_value and not value:
                self._audit_world_profile_rejection(audit, field_name, "", raw_value, reason)
                continue
            if not value:
                continue
            if getattr(profile, field_name) != value:
                setattr(profile, field_name, value)
                changes.append(f"{field_name}: {value}")
                self._audit_world_profile_acceptance(audit, field_name, "", value)

        for field_name in dict_fields:
            target = getattr(profile, field_name)
            for key, value in self._normalize_mapping_updates(normalized.get(field_name)).items():
                raw_key = key
                raw_value = value
                key, key_reason = self._clean_world_profile_key(field_name, key)
                value, value_reason = self._clean_world_profile_text(field_name, value)
                if not key:
                    self._audit_world_profile_rejection(audit, field_name, raw_key, raw_value, key_reason)
                    continue
                if raw_value and not value:
                    self._audit_world_profile_rejection(audit, field_name, key, raw_value, value_reason)
                    continue
                if not key or not value:
                    continue
                if target.get(key) != value:
                    target[key] = value
                    changes.append(f"{field_name}.{key}: {value}")
                    self._audit_world_profile_acceptance(audit, field_name, key, value)

        map_locations = normalized.get("map_locations", [])
        if isinstance(map_locations, dict):
            map_locations = [
                dict(value, name=key) if isinstance(value, dict) else {"name": key, "description": value}
                for key, value in map_locations.items()
            ]
        for item in map_locations if isinstance(map_locations, list) else []:
            if not isinstance(item, dict):
                continue
            raw_name = str(item.get("name") or "").strip()
            name, name_reason = self._clean_world_profile_key("map_locations", raw_name)
            if not name:
                self._audit_world_profile_rejection(audit, "map_locations", raw_name, item, name_reason)
                continue
            description, description_reason = self._clean_world_profile_text(
                "map_locations",
                str(item.get("description") or "").strip(),
            )
            if item.get("description") and not description:
                self._audit_world_profile_rejection(audit, "map_locations", name, item.get("description"), description_reason)
                continue
            self.upsert_map_location(
                name,
                description=description,
                terrain=str(item.get("terrain") or "").strip(),
                feature_type=str(item.get("feature_type") or "").strip(),
                position_hint=str(item.get("position_hint") or "").strip(),
                relative_to=str(item.get("relative_to") or "").strip(),
                relative_position=str(item.get("relative_position") or "").strip(),
                faction=str(item.get("faction") or "").strip(),
                draw_icon=item.get("draw_icon") if isinstance(item.get("draw_icon"), bool) else None,
            )
            if description:
                profile.major_locations[name] = description
            changes.append(f"map_locations.{name}: {description or '已登记'}")
            self._audit_world_profile_acceptance(audit, "map_locations", name, description or "已登记")

        optional_rules = normalized.get("optional_rules")
        if isinstance(optional_rules, list):
            optional_rules = {
                str(item.get("key") or item.get("label") or item.get("name") or ""): item
                for item in optional_rules
                if isinstance(item, dict)
            }
        if isinstance(optional_rules, dict):
            for raw_key, raw_value in optional_rules.items():
                key = normalize_optional_rule_key(str(raw_key))
                if not key:
                    continue
                if isinstance(raw_value, dict):
                    enabled = bool(raw_value.get("enabled", raw_value.get("value", False)))
                    note = str(raw_value.get("note") or "").strip()
                    rule_source = str(raw_value.get("source") or source).strip()
                else:
                    enabled = bool(raw_value)
                    note = ""
                    rule_source = source
                current = profile.optional_rules.get(key)
                if current is None or current.enabled != enabled or current.note != note:
                    state = apply_optional_rule_state(
                        profile,
                        key,
                        enabled=enabled,
                        note=note,
                        source=rule_source,
                    )
                    text = f"optional_rules.{key}: {'启用' if state.enabled else '关闭'}"
                    changes.append(text)
                    self._audit_world_profile_acceptance(audit, "optional_rules", key, text)

        for field_name in list_fields:
            target = getattr(profile, field_name)
            for raw_value in self._normalize_sequence_updates(normalized.get(field_name)):
                value, reason = self._clean_world_profile_text(field_name, raw_value)
                if raw_value and not value:
                    self._audit_world_profile_rejection(audit, field_name, "", raw_value, reason)
                    continue
                if value and value not in target:
                    target.append(value)
                    changes.append(f"{field_name}: {value}")
                    self._audit_world_profile_acceptance(audit, field_name, "", value)

        if audit["accepted"] or audit["rejected"]:
            self.record_memory_event(
                f"世界观入库审计：接受 {len(audit['accepted'])} 条，拒收 {len(audit['rejected'])} 条。",
                kind="world_profile_update_audit",
                visibility=MemoryVisibility.PRIVATE,
                tags=["world_profile", "audit"],
                source=source,
                payload=audit,
            )

        if not changes:
            return []

        self.apply_world_profile(profile)
        for change in changes:
            self.record_memory_event(
                f"世界观补全：{change}",
                kind="world_profile_update",
                visibility=MemoryVisibility.PUBLIC,
                entities=self.extract_entities(change),
                tags=["world_profile", "live_worldbuilding"],
                source=source,
            )
        return changes

    def world_profile_update_audit(self, *, limit: int = 20, include_private: bool = True) -> list[MemoryEvent]:
        events = [
            event
            for event in self.memory_events
            if event.kind == "world_profile_update_audit"
            and (include_private or event.visibility == MemoryVisibility.PUBLIC)
        ]
        return events[-max(1, limit) :]

    def _normalize_mapping_updates(self, value: Any) -> dict[str, str]:
        if isinstance(value, dict):
            return {str(key).strip(): str(item).strip() for key, item in value.items()}
        if isinstance(value, list):
            result: dict[str, str] = {}
            for item in value:
                if isinstance(item, dict):
                    name = str(item.get("name") or item.get("title") or item.get("key") or "").strip()
                    description = str(
                        item.get("description")
                        or item.get("detail")
                        or item.get("note")
                        or item.get("value")
                        or ""
                    ).strip()
                    if name and description:
                        result[name] = description
                elif str(item or "").strip():
                    text = str(item).strip()
                    result[text] = text
            return result
        if str(value or "").strip():
            text = str(value).strip()
            return {text: text}
        return {}

    def _normalize_sequence_updates(self, value: Any) -> list[str]:
        if isinstance(value, list):
            result: list[str] = []
            for item in value:
                if isinstance(item, dict):
                    text = str(
                        item.get("name")
                        or item.get("title")
                        or item.get("description")
                        or item.get("note")
                        or item.get("value")
                        or ""
                    ).strip()
                else:
                    text = str(item or "").strip()
                if text:
                    result.append(text)
            return result
        if str(value or "").strip():
            return [str(value).strip()]
        return []

    def _clean_world_profile_key(self, field_name: str, key: Any) -> tuple[str, str]:
        text = str(key or "").strip(" \t\r\n:：,，;；。.!！?？【】[]")
        text = self._strip_world_profile_meta_tail(text)
        if not text:
            return "", "empty_key"
        if text.startswith(("的", "了", "我", "我的", "玩家", "角色")):
            return "", "looks_like_sentence_fragment"
        if self._contains_world_profile_meta(text):
            return "", "contains_table_talk"
        max_len = 32 if field_name in {"major_locations", "map_locations"} else 24
        if len(text) > max_len:
            return "", "key_too_long"
        return text, ""

    def _clean_world_profile_text(self, field_name: str, value: Any) -> tuple[str, str]:
        text = str(value or "").strip()
        if not text:
            return "", "empty_value"
        text = self._strip_world_profile_meta_tail(text)
        text = text.strip(" \t\r\n,，;；。")
        if not text:
            return "", "only_table_talk"
        if text.startswith(("我的角色", "我投", "投这个", "请给", "下一步")):
            return "", "table_talk_not_world_fact"
        if field_name in {"mysteries", "historical_events", "world_threats", "villain_seeds"}:
            text = re.sub(r"^(?:我补充一个|额外补一个|另外补一个)?(?:反派种子|世界细节|地点细节)[：:，,]\s*", "", text).strip()
        return text, ""

    def _strip_world_profile_meta_tail(self, text: str) -> str:
        text = str(text or "").strip()
        if not text:
            return ""
        meta_markers = (
            "我投这个",
            "我投",
            "我也投",
            "投这个",
            "额外补一个",
            "额外补充",
            "另外补一个",
            "顺便补一个",
            "我的角色",
            "接下来",
            "下一步",
            "第一幕我提议",
            "第一章我提议",
            "我希望第一幕",
            "我希望第一章",
        )
        positions = [text.find(marker) for marker in meta_markers if text.find(marker) >= 0]
        if positions:
            text = text[: min(positions)]
        return text.strip(" \t\r\n,，;；。")

    def _contains_world_profile_meta(self, text: str) -> bool:
        return any(
            marker in str(text or "")
            for marker in (
                "我投",
                "我也投",
                "投票",
                "第一幕我提议",
                "第一章我提议",
                "我希望第一幕",
                "我希望第一章",
                "我的角色",
                "创建角色",
                "技能选择",
                "下一位",
                "请给一个",
            )
        )

    def _audit_world_profile_acceptance(self, audit: dict[str, Any], field_name: str, key: str, value: Any) -> None:
        audit["accepted"].append({"field": field_name, "key": key, "value": value})

    def _audit_world_profile_rejection(
        self,
        audit: dict[str, Any],
        field_name: str,
        key: Any,
        value: Any,
        reason: str,
    ) -> None:
        audit["rejected"].append(
            {
                "field": field_name,
                "key": str(key or ""),
                "value": str(value or ""),
                "reason": reason or "rejected",
            }
        )

    def _refresh_gm_guidance(self, profile: WorldCreationProfile) -> None:
        guidance = build_gm_guidance(profile)
        profile.gm_inspiration_tags = list(guidance.inspiration_tags)
        profile.gm_guidance_notes = list(guidance.principles[:6])
        profile.gm_story_beats = list(guidance.story_beats[:5])
        profile.gm_prepared_locations = {
            seed.name: f"{seed.archetype}：{seed.brief}" for seed in guidance.location_seeds[:6]
        }

    def _add_memory_once(self, memory: str) -> None:
        if memory not in self.memories:
            self.add_memory(memory)

    def _append_gm_secret_note_once(self, note: str) -> None:
        if note not in self.world_profile.gm_secret_notes:
            self.world_profile.gm_secret_notes.append(note)

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def _query_terms(self, query: str, *, extra_entities: list[str] | None = None) -> list[str]:
        separators = "，。！？、；：,.;:!?()（）[]【】\n\t"
        normalized = query.lower()
        for separator in separators:
            normalized = normalized.replace(separator, " ")
        terms = [term for term in normalized.split() if term]
        for entity in self.extract_entities(query, extra_entities=extra_entities):
            lowered = entity.lower()
            if lowered not in terms:
                terms.append(lowered)
        for entity in extra_entities or []:
            lowered = entity.lower()
            if lowered not in terms:
                terms.append(lowered)
        return terms

    def apply_party_sheet(self, party_sheet: PartySheet) -> None:
        self.party_sheet = party_sheet
        if party_sheet.group_concept:
            self._add_memory_once(f"小队表原型：{party_sheet.group_concept}")
        if party_sheet.shared_goal:
            self._add_memory_once(f"小队共同目标：{party_sheet.shared_goal}")

    def apply_world_sheet(self, world_sheet: WorldSheet) -> None:
        self.world_sheet = world_sheet
        if world_sheet.campaign_title:
            self._add_memory_once(f"世界表战役标题：{world_sheet.campaign_title}")
        if world_sheet.continent_name:
            self._add_memory_once(f"世界表大陆名称：{world_sheet.continent_name}")
        for location, detail in world_sheet.major_locations.items():
            self.upsert_map_location(location, description=detail)

    def upsert_map_location(
        self,
        name: str,
        *,
        x: int | None = None,
        y: int | None = None,
        description: str = "",
        terrain: str = "",
        feature_type: str = "",
        position_hint: str = "",
        relative_to: str = "",
        relative_position: str = "",
        semantic_cell: str = "",
        draw_icon: bool | None = None,
        icon_id: str = "",
        threat_level=None,
        route_type=None,
        faction: str = "",
        discovered: bool | None = None,
        tags: list[str] | None = None,
        notes: list[str] | None = None,
    ) -> MapLocation:
        from fu_gm.models import TravelRouteType, TravelThreatLevel

        if not name:
            raise ValueError("地点名称不能为空。")
        location = self.map_locations.get(name)
        if location is None:
            location = MapLocation(name=name)
            self.map_locations[name] = location
        if x is not None:
            location.x = x
        if y is not None:
            location.y = y
        if description:
            location.description = description
            self.map_notes[name] = description
        elif name not in self.map_notes:
            self.map_notes[name] = location.description
        if terrain:
            location.terrain = terrain
        if feature_type:
            location.feature_type = feature_type
        if position_hint:
            location.position_hint = position_hint
        if relative_to:
            location.relative_to = relative_to
        if relative_position:
            location.relative_position = relative_position
        if semantic_cell:
            location.semantic_cell = semantic_cell
        if draw_icon is not None:
            location.draw_icon = draw_icon
        if icon_id:
            location.icon_id = icon_id
        if threat_level is not None:
            location.threat_level = TravelThreatLevel(threat_level)
        if route_type is not None:
            location.route_type = TravelRouteType(route_type)
        if faction:
            location.faction = faction
        if discovered is not None:
            location.discovered = discovered
        for tag in tags or []:
            if tag not in location.tags:
                location.tags.append(tag)
        for note in notes or []:
            if note not in location.notes:
                location.notes.append(note)
        if self.world_sheet is not None and location.discovered:
            self.world_sheet.major_locations[location.name] = self.map_notes.get(location.name, location.description)
        return location

    def discover_map_location(
        self,
        name: str,
        *,
        x: int | None = None,
        y: int | None = None,
        description: str = "",
        terrain: str = "",
        threat_level=None,
        route_type=None,
        source: str = "",
        tags: list[str] | None = None,
    ) -> MapLocation:
        location = self.upsert_map_location(
            name,
            x=x,
            y=y,
            description=description,
            terrain=terrain,
            threat_level=threat_level,
            route_type=route_type,
            discovered=True,
            tags=tags,
        )
        self.record_memory_event(
            f"地图发现：{self.format_map_location(location)}",
            kind="map_discovery",
            visibility=MemoryVisibility.PUBLIC,
            entities=[location.name],
            tags=["map", *(tags or [])],
            source=source,
        )
        return location

    def upsert_map_route(
        self,
        *,
        origin: str,
        destination: str,
        route_id: str = "",
        distance_days: int | None = None,
        default_threat_level=None,
        route_type=None,
        terrain: str = "",
        description: str = "",
        bidirectional: bool = True,
        discovered: bool = True,
        segments: list[MapRouteSegment | dict] | None = None,
        tags: list[str] | None = None,
        notes: list[str] | None = None,
    ) -> MapRouteEdge:
        from fu_gm.models import TravelRouteType, TravelThreatLevel

        origin = origin.strip()
        destination = destination.strip()
        if not origin or not destination:
            raise ValueError("路线起点和终点不能为空。")
        route_id = route_id.strip() or self.map_route_key(origin, destination)
        normalized_segments = self._normalize_route_segments(
            segments or [],
            fallback_region=destination,
            fallback_threat=TravelThreatLevel(default_threat_level) if default_threat_level else TravelThreatLevel.MEDIUM,
        )
        if distance_days is None:
            distance_days = sum(segment.distance_days for segment in normalized_segments) if normalized_segments else 1
        distance_days = max(1, int(distance_days))
        edge = self.map_routes.get(route_id)
        if edge is None:
            edge = MapRouteEdge(route_id=route_id, origin=origin, destination=destination)
            self.map_routes[route_id] = edge
        edge.origin = origin
        edge.destination = destination
        edge.distance_days = distance_days
        if default_threat_level is not None:
            edge.default_threat_level = TravelThreatLevel(default_threat_level)
        edge.route_type = TravelRouteType(route_type) if route_type is not None else edge.route_type
        if terrain:
            edge.terrain = terrain
        if description:
            edge.description = description
        edge.bidirectional = bool(bidirectional)
        edge.discovered = bool(discovered)
        edge.segments = normalized_segments
        for tag in tags or []:
            if tag not in edge.tags:
                edge.tags.append(tag)
        for note in notes or []:
            if note not in edge.notes:
                edge.notes.append(note)
        self.record_memory_event(
            f"地图路线登记：{self.format_map_route(edge)}",
            kind="map_route",
            visibility=MemoryVisibility.PUBLIC,
            entities=[origin, destination],
            tags=["map", "route", *(tags or [])],
            source="WorldState",
            payload={"route_id": edge.route_id, "distance_days": edge.distance_days},
        )
        return edge

    def find_map_route(self, origin: str, destination: str, *, route_id: str = "", allow_reverse: bool = True) -> MapRouteEdge | None:
        if route_id:
            edge = self.map_routes.get(route_id)
            if edge is None:
                return None
            if edge.origin == origin and edge.destination == destination:
                return edge
            if allow_reverse and edge.bidirectional and edge.origin == destination and edge.destination == origin:
                return self._reversed_route(edge)
            return None
        for edge in self.map_routes.values():
            if edge.origin == origin and edge.destination == destination:
                return edge
            if allow_reverse and edge.bidirectional and edge.origin == destination and edge.destination == origin:
                return self._reversed_route(edge)
        return None

    def map_route_key(self, origin: str, destination: str) -> str:
        return f"{origin}->{destination}"

    def format_map_route(self, route: MapRouteEdge) -> str:
        segment_text = "；".join(
            f"{segment.region} {segment.distance_days}日/{segment.threat_level.value}"
            for segment in route.segments
        )
        if not segment_text:
            segment_text = f"默认威胁：{route.default_threat_level.value}"
        return (
            f"{route.route_id}：{route.origin} -> {route.destination}，"
            f"{route.distance_days} 个徒步旅行日单位，路线类型：{route.route_type.value}，{segment_text}"
        )

    def format_map_location(self, location: MapLocation) -> str:
        faction = f"，势力：{location.faction}" if location.faction else ""
        return (
            f"{location.name}({location.x}, {location.y})：{location.description or '尚无详细描述'}"
            f"；地形：{location.terrain}；威胁：{location.threat_level.value}{faction}"
        )

    def _normalize_route_segments(
        self,
        segments: list[MapRouteSegment | dict],
        *,
        fallback_region: str,
        fallback_threat,
    ) -> list[MapRouteSegment]:
        from fu_gm.models import TravelThreatLevel

        normalized: list[MapRouteSegment] = []
        for raw in segments:
            if isinstance(raw, MapRouteSegment):
                segment = raw
            elif isinstance(raw, dict):
                segment = MapRouteSegment(
                    region=str(raw.get("region") or fallback_region),
                    distance_days=int(raw.get("distance_days") or raw.get("days") or 1),
                    threat_level=TravelThreatLevel(raw.get("threat_level") or fallback_threat),
                    terrain=str(raw.get("terrain") or ""),
                    description=str(raw.get("description") or ""),
                )
            else:
                continue
            if segment.distance_days <= 0:
                continue
            segment.threat_level = TravelThreatLevel(segment.threat_level)
            normalized.append(segment)
        return normalized

    def _reversed_route(self, route: MapRouteEdge) -> MapRouteEdge:
        return replace(
            route,
            origin=route.destination,
            destination=route.origin,
            segments=list(reversed(route.segments)),
            route_id=f"{route.route_id}:reverse",
        )

    def ensure_npc_persona(
        self,
        name: str,
        *,
        profile_status: str = "",
        entity_kind: str = "",
        aliases: list[str] | None = None,
        public_identity: str = "",
        role_in_story: str = "",
        core_drive: str = "",
        manner: str = "",
        speech_style: str = "",
        combat_style: str = "",
        traits: list[str] | None = None,
        npc_rank: str = "",
        leverage: str = "",
        authority_scope: str = "",
        knowledge_scope: str = "",
        refusal_move: str = "",
        known_skills: list[str] | None = None,
        combat_actions: list[str] | None = None,
        first_scene: str = "",
        goals: list[str] | None = None,
        taboos: list[str] | None = None,
        secrets: list[str] | None = None,
        custom_prompt: str = "",
        current_location: str = "",
        current_mood: str = "",
        current_stance: str = "",
        active_goal: str = "",
        last_seen_scene: str = "",
        voice_examples: list[str] | None = None,
    ) -> NPCPersona:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("NPC 名称不能为空。")
        canonical_name = self.resolve_npc_name(clean_name) or clean_name
        if canonical_name in self.npc_personas:
            persona = self.npc_personas[canonical_name]
            if not persona.npc_id:
                persona.npc_id = f"npc-{uuid4().hex}"
            current_profile_status = str(
                getattr(persona, "profile_status", "established") or "established"
            ).strip().lower()
            requested_profile_status = str(profile_status or "").strip().lower()
            if requested_profile_status not in {"placeholder", "established"}:
                requested_profile_status = ""
            upgrading_placeholder = bool(
                current_profile_status == "placeholder"
                and requested_profile_status == "established"
            )

            def fill_profile_field(field_name: str, value: str) -> None:
                if value and (
                    upgrading_placeholder or not getattr(persona, field_name)
                ):
                    setattr(persona, field_name, value)

            clean_entity_kind = str(entity_kind or "").strip().lower()
            if clean_entity_kind in {"individual", "collective"}:
                # Only an explicit caller may promote a legacy/default profile
                # to a collective.  Empty defaults never downgrade either kind.
                persona.entity_kind = clean_entity_kind
            for value in aliases or []:
                alias = str(value or "").strip()
                if alias and alias != persona.name and alias not in persona.aliases:
                    persona.aliases.append(alias)
            fill_profile_field("public_identity", public_identity)
            fill_profile_field("role_in_story", role_in_story)
            fill_profile_field("core_drive", core_drive)
            fill_profile_field("manner", manner)
            fill_profile_field("speech_style", speech_style)
            fill_profile_field("combat_style", combat_style)
            for value in traits or []:
                clean = str(value or "").strip()
                if clean and clean not in persona.traits:
                    persona.traits.append(clean)
            if npc_rank:
                # The legacy default is "minor", so an existing profile must
                # still be able to become supporting/elite/villain/boss when
                # later authoritative information establishes that rank.  A
                # default "minor" must never silently downgrade a higher rank.
                if (
                    upgrading_placeholder
                    or not persona.npc_rank
                    or persona.npc_rank == "minor"
                    or npc_rank != "minor"
                ):
                    persona.npc_rank = npc_rank
            fill_profile_field("leverage", leverage)
            fill_profile_field("authority_scope", authority_scope)
            fill_profile_field("knowledge_scope", knowledge_scope)
            fill_profile_field("refusal_move", refusal_move)
            for value in known_skills or []:
                clean = str(value or "").strip()
                if clean and clean not in persona.known_skills:
                    persona.known_skills.append(clean)
            for value in combat_actions or []:
                clean = str(value or "").strip()
                if clean and clean not in persona.combat_actions:
                    persona.combat_actions.append(clean)
            if first_scene:
                fill_profile_field("first_scene", first_scene)
            if custom_prompt:
                fill_profile_field("custom_prompt", custom_prompt)
            for value in goals or []:
                if value not in persona.goals:
                    persona.goals.append(value)
            for value in taboos or []:
                if value not in persona.taboos:
                    persona.taboos.append(value)
            for value in secrets or []:
                if value not in persona.secrets:
                    persona.secrets.append(value)
            for value in voice_examples or []:
                example = str(value or "").strip()
                if example and example not in persona.voice_examples:
                    persona.voice_examples.append(example)
            if current_location:
                persona.current_location = current_location
            if current_mood:
                persona.current_mood = current_mood
            if current_stance:
                persona.current_stance = current_stance
            if active_goal:
                persona.active_goal = active_goal
            if last_seen_scene:
                persona.last_seen_scene = last_seen_scene
            if requested_profile_status == "established":
                persona.profile_status = "established"
            elif not getattr(persona, "profile_status", ""):
                persona.profile_status = current_profile_status
            return persona

        normalized_profile_status = str(profile_status or "established").strip().lower()
        if normalized_profile_status not in {"placeholder", "established"}:
            normalized_profile_status = "established"
        persona = NPCPersona(
            name=clean_name,
            npc_id=f"npc-{uuid4().hex}",
            profile_status=normalized_profile_status,
            entity_kind=(
                str(entity_kind or "individual").strip().lower()
                if str(entity_kind or "individual").strip().lower()
                in {"individual", "collective"}
                else "individual"
            ),
            aliases=[str(value).strip() for value in aliases or [] if str(value).strip() and str(value).strip() != clean_name],
            public_identity=public_identity or clean_name,
            role_in_story=role_in_story,
            core_drive=core_drive,
            manner=manner,
            speech_style=speech_style,
            combat_style=combat_style,
            traits=[
                str(value).strip()
                for value in traits or []
                if str(value).strip()
            ][:4],
            npc_rank=npc_rank or "minor",
            leverage=leverage,
            authority_scope=authority_scope,
            knowledge_scope=knowledge_scope,
            refusal_move=refusal_move,
            known_skills=[str(value).strip() for value in known_skills or [] if str(value).strip()],
            combat_actions=[str(value).strip() for value in combat_actions or [] if str(value).strip()],
            first_scene=first_scene,
            goals=list(goals or []),
            taboos=list(taboos or []),
            secrets=list(secrets or []),
            custom_prompt=custom_prompt,
            current_location=current_location,
            current_mood=current_mood,
            current_stance=current_stance,
            active_goal=active_goal,
            last_seen_scene=last_seen_scene,
            voice_examples=[str(value).strip() for value in voice_examples or [] if str(value).strip()],
        )
        self.npc_personas[clean_name] = persona
        return persona

    def resolve_npc_name(self, name: str) -> str:
        clean_name = str(name or "").strip()
        if not clean_name:
            return ""
        if clean_name in self.npc_personas:
            return clean_name
        for canonical_name, persona in self.npc_personas.items():
            if clean_name == persona.public_identity or clean_name in persona.aliases:
                return canonical_name
        return ""

    def merge_npc_personas(self, primary_name: str, duplicate_name: str) -> NPCPersona:
        """Merge a proven alias persona without discarding persistent memory.

        Identity decisions belong to the GM tool agent. This method is
        deliberately mechanical: a typed tool call must already have
        established that both keys describe the same fictional person.
        """

        primary_key = (
            primary_name if primary_name in self.npc_personas else self.resolve_npc_name(primary_name)
        )
        duplicate_key = (
            duplicate_name if duplicate_name in self.npc_personas else self.resolve_npc_name(duplicate_name)
        )
        if not primary_key or primary_key not in self.npc_personas:
            raise KeyError(f"找不到主 NPC 人格：{primary_name}")
        if not duplicate_key or duplicate_key not in self.npc_personas:
            raise KeyError(f"找不到待合并 NPC 人格：{duplicate_name}")
        if primary_key == duplicate_key:
            return self.npc_personas[primary_key]

        primary = self.npc_personas[primary_key]
        duplicate = self.npc_personas[duplicate_key]
        primary_status = str(
            getattr(primary, "profile_status", "established") or "established"
        )
        duplicate_status = str(
            getattr(duplicate, "profile_status", "established") or "established"
        )
        primary_was_placeholder = (
            primary_status == "placeholder" and duplicate_status == "established"
        )
        if duplicate.entity_kind == "collective":
            primary.entity_kind = "collective"

        def append_unique(bucket: list[Any], values: list[Any]) -> None:
            for value in values:
                if value not in (None, "") and value not in bucket:
                    bucket.append(value)

        append_unique(
            primary.aliases,
            [duplicate_key, duplicate.name, duplicate.public_identity, *duplicate.aliases],
        )
        primary.aliases = [
            alias for alias in primary.aliases if alias and alias != primary.name
        ]
        for field_name in (
            "goals",
            "taboos",
            "secrets",
            "memories",
            "completed_goals",
            "voice_examples",
            "known_skills",
            "combat_actions",
        ):
            append_unique(getattr(primary, field_name), list(getattr(duplicate, field_name)))

        existing_records = {
            (
                str(record.get("note") or ""),
                str(record.get("scene_id") or ""),
                str(record.get("source") or ""),
            )
            for record in primary.memory_records
        }
        for record in duplicate.memory_records:
            key = (
                str(record.get("note") or ""),
                str(record.get("scene_id") or ""),
                str(record.get("source") or ""),
            )
            if key not in existing_records:
                primary.memory_records.append(dict(record))
                existing_records.add(key)

        for key, value in duplicate.relationships.items():
            primary.relationships.setdefault(key, value)
        if duplicate.active_goal and duplicate.active_goal not in primary.goals:
            primary.goals.append(duplicate.active_goal)
        for field_name in (
            "public_identity",
            "role_in_story",
            "core_drive",
            "manner",
            "speech_style",
            "combat_style",
            "npc_rank",
            "leverage",
            "authority_scope",
            "knowledge_scope",
            "refusal_move",
            "first_scene",
            "current_location",
            "current_mood",
            "current_stance",
            "active_goal",
            "last_seen_scene",
            "status",
        ):
            if (
                primary_was_placeholder or not getattr(primary, field_name)
            ) and getattr(duplicate, field_name):
                setattr(primary, field_name, getattr(duplicate, field_name))
        if duplicate_status == "established":
            primary.profile_status = "established"
        if duplicate.custom_prompt:
            if not primary.custom_prompt:
                primary.custom_prompt = duplicate.custom_prompt
            elif duplicate.custom_prompt not in primary.custom_prompt:
                primary.custom_prompt += "\n" + duplicate.custom_prompt

        duplicate_facts = self.subject_facts.pop(duplicate_key, [])
        append_unique(self.subject_facts.setdefault(primary_key, []), duplicate_facts)
        duplicate_relationships = self.npc_relationships.pop(duplicate_key, [])
        append_unique(
            self.npc_relationships.setdefault(primary_key, []),
            duplicate_relationships,
        )

        aliases = {
            duplicate_key,
            duplicate.name,
            duplicate.public_identity,
            *duplicate.aliases,
        }
        for event in self.memory_events:
            event.entities = [primary_key if entity in aliases else entity for entity in event.entities]
            event.entities = list(dict.fromkeys(event.entities))
        for relation in self.memory_relations:
            if relation.source in aliases:
                relation.source = primary_key
            if relation.target in aliases:
                relation.target = primary_key
        unique_relations: list[MemoryRelation] = []
        seen_relations: set[tuple[str, str, str, MemoryVisibility]] = set()
        for relation in self.memory_relations:
            key = (relation.source, relation.relation, relation.target, relation.visibility)
            if key in seen_relations:
                continue
            seen_relations.add(key)
            unique_relations.append(relation)
        self.memory_relations = unique_relations
        for secret in self.gm_secrets.values():
            secret.related_entities = list(
                dict.fromkeys(
                    primary_key if entity in aliases else entity
                    for entity in secret.related_entities
                )
            )

        del self.npc_personas[duplicate_key]
        return primary

    def update_npc_state(
        self,
        name: str,
        *,
        location: str = "",
        mood: str = "",
        stance: str = "",
        active_goal: str = "",
        completed_goal: str = "",
        relationship_target: str = "",
        relationship: str = "",
        scene: str = "",
        status: str = "",
    ) -> NPCPersona:
        persona = self.ensure_npc_persona(name)
        if location:
            persona.current_location = location
        if mood:
            persona.current_mood = mood
        if stance:
            persona.current_stance = stance
        if active_goal:
            persona.active_goal = active_goal
            if active_goal not in persona.goals:
                persona.goals.append(active_goal)
        if completed_goal:
            if completed_goal not in persona.completed_goals:
                persona.completed_goals.append(completed_goal)
            if persona.active_goal == completed_goal:
                persona.active_goal = ""
        if relationship_target and relationship:
            persona.relationships[relationship_target] = relationship
        if scene:
            persona.last_seen_scene = scene
        if status:
            persona.status = status
        return persona

    def remember_npc_event(
        self,
        name: str,
        note: str,
        *,
        scene_id: str = "",
        source: str = "",
        salience: int = 1,
        witnessed: bool = True,
        supersedes_prior_terms: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        persona = self.ensure_npc_persona(name)
        clean_note = str(note or "").strip()
        if not clean_note:
            return
        if clean_note not in persona.memories:
            persona.memories.append(clean_note)
        record = {
            "note": clean_note,
            "scene_id": str(scene_id or "").strip(),
            "source": str(source or "").strip(),
            "salience": max(0, min(5, int(salience))),
            "witnessed": bool(witnessed),
            "supersedes_prior_terms": bool(supersedes_prior_terms),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        if metadata:
            record["metadata"] = dict(metadata)
        if not any(existing.get("note") == clean_note for existing in persona.memory_records):
            persona.memory_records.append(record)
        persona.memories = persona.memories[-200:]
        persona.memory_records = persona.memory_records[-200:]

    def latest_npc_public_statement(
        self,
        name: str,
        *,
        scene_id: str = "",
    ) -> dict[str, Any]:
        """Return the latest witnessed statement made by one NPC.

        Prefer the current scene, but retain cross-scene continuity when the NPC
        has not spoken locally yet. Older snapshots did not store the explicit
        supersession flag, so callers may additionally inspect the statement's
        wording through ``NPCContinuityPolicy``.
        """

        history = self.npc_public_statement_history(name, scene_id=scene_id, limit=1)
        return history[-1] if history else {}

    def npc_public_statement_history(
        self,
        name: str,
        *,
        scene_id: str = "",
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        """Return witnessed NPC statements in chronological order.

        A current-scene query intentionally does not fall through to an older
        scene. New scene prep may legitimately begin from a changed situation,
        while every statement inside one scene remains available for continuity
        and legacy-snapshot repair.
        """

        canonical = self.resolve_npc_name(name)
        if not canonical:
            return []
        persona = self.npc_personas[canonical]
        clean_scene_id = str(scene_id or "").strip()
        public_sources = {"gm_scene_beat", "direct_dialogue"}
        result: list[dict[str, Any]] = []
        for record in persona.memory_records:
            if not isinstance(record, dict) or not bool(record.get("witnessed", True)):
                continue
            source = str(record.get("source") or "").strip()
            if source not in public_sources:
                continue
            record_scene = str(record.get("scene_id") or "").strip()
            if clean_scene_id and record_scene != clean_scene_id:
                continue
            note = " ".join(str(record.get("note") or "").split()).strip()
            statement = self._npc_statement_from_memory_note(note)
            if not statement:
                continue
            result.append(
                {
                    "statement": statement,
                    "scene_id": record_scene,
                    "source": source,
                    "supersedes_prior_terms": bool(record.get("supersedes_prior_terms", False)),
                    "recorded_at": str(record.get("recorded_at") or ""),
                }
            )
        return result[-max(1, int(limit)) :]

    @staticmethod
    def _npc_statement_from_memory_note(note: str) -> str:
        clean = " ".join(str(note or "").split()).strip()
        if "我公开说过：" in clean:
            return clean.split("我公开说过：", 1)[1].strip()
        if "；我的答复：" in clean:
            return clean.split("；我的答复：", 1)[1].strip()
        return ""

    def relevant_npc_memories(self, name: str, query: str = "", *, limit: int = 6) -> list[str]:
        canonical = self.resolve_npc_name(name)
        if not canonical:
            return []
        persona = self.npc_personas[canonical]
        if not persona.memory_records:
            return persona.memories[-limit:]
        terms = {
            term
            for term in re.split(r"[\s，。！？；、：,.!?;:\[\]【】（）()]+", str(query or ""))
            if len(term) >= 2
        }
        scored: list[tuple[int, int, str]] = []
        for index, record in enumerate(persona.memory_records):
            if not bool(record.get("witnessed", True)):
                continue
            note = str(record.get("note") or "").strip()
            if not note:
                continue
            salience = int(record.get("salience", 1) or 1)
            relevance = sum(3 for term in terms if term in note)
            scene_bonus = 2 if persona.current_location and persona.current_location in note else 0
            scored.append((salience * 10 + relevance + scene_bonus, index, note))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [note for _, _, note in scored[: max(1, limit)]]

    def remember_subject_fact(self, subject: str, note: str) -> None:
        facts = self.subject_facts.setdefault(subject, [])
        if note not in facts:
            facts.append(note)

    def record_persistent_change(self, change: PersistentChange) -> PersistentChange:
        """记录仪式或项目造成的长期改变，并同步到世界表。"""

        for existing in self.persistent_changes:
            if self._same_persistent_change(existing, change):
                return existing

        self.persistent_changes.append(change)
        summary = self.format_persistent_change(change)
        self._add_memory_once(f"持久化变化：{summary}")
        if change.owner:
            self.remember_subject_fact(change.owner, summary)
        if change.location:
            current_note = self.map_notes.get(change.location, "")
            addition = f"设施/变化：{change.name}。{change.description}".strip()
            if addition not in current_note:
                self.map_notes[change.location] = f"{current_note} {addition}".strip()
        self._sync_world_sheet_persistent_change(change, summary)
        return change

    def record_world_fact(
        self,
        *,
        name: str,
        description: str,
        source: str,
        location: str = "",
        tags: list[str] | None = None,
    ) -> PersistentChange:
        return self.record_persistent_change(
            PersistentChange(
                change_type=PersistentChangeType.WORLD_FACT,
                name=name,
                description=description,
                source=source,
                location=location,
                tags=list(tags or []),
            )
        )

    def record_location_facility(
        self,
        *,
        name: str,
        description: str,
        source: str,
        location: str,
        tags: list[str] | None = None,
    ) -> PersistentChange:
        return self.record_persistent_change(
            PersistentChange(
                change_type=PersistentChangeType.FACILITY,
                name=name,
                description=description,
                source=source,
                location=location,
                tags=list(tags or []),
            )
        )

    def record_created_asset(
        self,
        *,
        change_type: PersistentChangeType,
        name: str,
        description: str,
        source: str,
        owner: str,
        location: str = "",
        tags: list[str] | None = None,
    ) -> PersistentChange:
        if change_type not in {PersistentChangeType.EQUIPMENT, PersistentChangeType.CONSUMABLE, PersistentChangeType.TRANSPORT}:
            raise ValueError("created asset 只能是装备、一次性道具或交通工具。")
        return self.record_persistent_change(
            PersistentChange(
                change_type=change_type,
                name=name,
                description=description,
                source=source,
                owner=owner,
                location=location,
                tags=list(tags or []),
            )
        )

    def find_story_item(self, *, item_id: str = "", name: str = "") -> StoryItem | None:
        clean_id = str(item_id or "").strip()
        if clean_id:
            return self.story_items.get(clean_id)
        key = self._story_item_name_key(name)
        if not key:
            return None
        for item in self.story_items.values():
            if self._story_item_name_key(item.name) == key:
                return item
        return None

    def validate_story_item_action(
        self,
        *,
        operation: str,
        item_name: str,
        actor: str,
        scene_location: str,
        item_id: str = "",
        to_holder: str = "",
        to_location: str = "",
        state_note: str = "",
    ) -> None:
        """Validate a story-item transition without changing world state."""

        self._validated_story_item_action(
            operation=operation,
            item_name=item_name,
            item_id=item_id,
            actor=actor,
            scene_location=scene_location,
            to_holder=to_holder,
            to_location=to_location,
            state_note=state_note,
        )["item"]

    def _validated_story_item_action(
        self,
        *,
        operation: str,
        item_name: str,
        actor: str,
        scene_location: str,
        item_id: str = "",
        to_holder: str = "",
        to_location: str = "",
        state_note: str = "",
    ) -> dict[str, Any]:
        """Return normalized transition inputs after every non-mutating check."""

        action = str(operation or "").strip().lower()
        if action not in {"acquire", "transfer", "place", "operate", "destroy", "consume"}:
            raise ValueError(f"不支持的剧情物件操作：{operation}")
        name = " ".join(str(item_name or "").split()).strip()
        owner = " ".join(str(actor or "").split()).strip()
        location = " ".join(str(scene_location or "").split()).strip()
        if not name or not owner:
            raise ValueError("剧情物件操作需要物件名与行动者。")

        item = self.find_story_item(item_id=item_id, name=name)
        if item is None and action not in {"acquire", "place"}:
            raise ValueError(
                f"剧情物件【{name}】尚未登记，首次操作必须是取得或直接放置到最终地点。"
            )
        if item is not None and item.status in {StoryItemStatus.DESTROYED, StoryItemStatus.CONSUMED}:
            raise ValueError(
                f"剧情物件【{item.name}】已经{self._story_item_status_label(item.status)}，不能再次操作。"
            )

        destination_holder = " ".join(str(to_holder or "").split()).strip()
        destination_location = " ".join(str(to_location or "").split()).strip()
        if (
            destination_location
            and location
            and not self._story_locations_overlap(destination_location, location)
        ):
            destination_location = f"{location}·{destination_location}"
        resolved_state = " ".join(str(state_note or "").split()).strip()

        if action == "acquire" and item is not None:
            if item.holder == owner and item.status == StoryItemStatus.CARRIED:
                raise ValueError(f"【{owner}】已经持有剧情物件【{item.name}】。")
            if item.holder and item.holder != owner:
                raise ValueError(f"剧情物件【{item.name}】当前由【{item.holder}】持有。")
            if item.location and location and not self._story_locations_overlap(item.location, location):
                raise ValueError(f"剧情物件【{item.name}】当前位于【{item.location}】，不在本场景。")
        elif action == "transfer":
            if item is None:  # Kept explicit so validation remains active under ``python -O``.
                raise ValueError(f"剧情物件【{name}】尚未登记，不能转交。")
            if item.holder != owner:
                raise ValueError(f"只有当前持有者【{item.holder or '无'}】能转交剧情物件【{item.name}】。")
            if not destination_holder:
                raise ValueError("转交剧情物件时必须指定新持有者。")
        elif action == "place" and item is not None:
            if item.holder and item.holder != owner:
                raise ValueError(f"只有当前持有者【{item.holder or '无'}】能放下剧情物件【{item.name}】。")
            if (
                not item.holder
                and item.location
                and location
                and not self._story_locations_overlap(item.location, location)
            ):
                raise ValueError(f"剧情物件【{item.name}】当前位于【{item.location}】，不在本场景。")
        elif action == "operate":
            if item is None:
                raise ValueError(f"剧情物件【{name}】尚未登记，不能操作。")
            if item.holder and item.holder != owner:
                raise ValueError(f"剧情物件【{item.name}】当前由【{item.holder}】持有。")
            if (
                not item.holder
                and item.location
                and location
                and not self._story_locations_overlap(item.location, location)
            ):
                raise ValueError(f"剧情物件【{item.name}】当前位于【{item.location}】，不在本场景。")
            if not resolved_state:
                raise ValueError("操作剧情物件时必须记录操作后的当前状态。")
        elif action in {"destroy", "consume"}:
            if item is None:
                raise ValueError(f"剧情物件【{name}】尚未登记，不能结算终结状态。")
            if item.holder and item.holder != owner:
                raise ValueError(f"剧情物件【{item.name}】当前由【{item.holder}】持有。")
            if (
                not item.holder
                and item.location
                and location
                and not self._story_locations_overlap(item.location, location)
            ):
                raise ValueError(f"剧情物件【{item.name}】当前位于【{item.location}】，不在本场景。")

        return {
            "action": action,
            "name": name,
            "owner": owner,
            "location": location,
            "item": item,
            "destination_holder": destination_holder,
            "destination_location": destination_location,
            "resolved_state": resolved_state,
        }

    def commit_story_item_action(
        self,
        *,
        operation: str,
        item_name: str,
        actor: str,
        scene_location: str,
        public_fact: str,
        source: str,
        item_id: str = "",
        description: str = "",
        to_holder: str = "",
        to_location: str = "",
        state_note: str = "",
        tags: list[str] | None = None,
    ) -> StoryItem:
        """Commit custody, operation state or terminal state for one story item."""

        validated = self._validated_story_item_action(
            operation=operation,
            item_name=item_name,
            item_id=item_id,
            actor=actor,
            scene_location=scene_location,
            to_holder=to_holder,
            to_location=to_location,
            state_note=state_note,
        )
        action = validated["action"]
        name = validated["name"]
        owner = validated["owner"]
        location = validated["location"]
        item = validated["item"]
        destination_holder = validated["destination_holder"]
        destination_location = validated["destination_location"]
        resolved_state = validated["resolved_state"]
        if item is None:
            resolved_id = str(item_id or "").strip() or f"story-item-{uuid4()}"
            item = StoryItem(
                item_id=resolved_id,
                name=name,
                description=str(description or "").strip(),
                location=location,
                tags=list(dict.fromkeys(str(tag).strip() for tag in (tags or []) if str(tag).strip())),
            )
            self.story_items[resolved_id] = item

        from_holder = item.holder
        from_location = item.location
        from_state = item.current_state

        if action == "acquire":
            item.holder = owner
            item.location = location
            item.status = StoryItemStatus.CARRIED
        elif action == "transfer":
            item.holder = destination_holder
            item.location = destination_location or location
            item.status = StoryItemStatus.CARRIED
        elif action == "place":
            item.holder = ""
            item.location = destination_location or location
            item.status = StoryItemStatus.PLACED
        elif action == "operate":
            item.current_state = resolved_state
        else:
            item.holder = ""
            item.location = destination_location or location
            item.status = StoryItemStatus.DESTROYED if action == "destroy" else StoryItemStatus.CONSUMED
            item.current_state = "已销毁" if action == "destroy" else "已消耗"

        if description and not item.description:
            item.description = str(description).strip()
        if resolved_state and action != "operate":
            item.current_state = resolved_state
        for tag in tags or []:
            clean = str(tag or "").strip()
            if clean and clean not in item.tags:
                item.tags.append(clean)
        item.history.append(
            StoryItemEvent(
                operation=action,
                actor=owner,
                changed_at=self._now(),
                from_holder=from_holder,
                to_holder=item.holder,
                from_location=from_location,
                to_location=item.location,
                from_state=from_state,
                to_state=item.current_state,
                public_fact=str(public_fact or "").strip(),
                source=str(source or "").strip(),
            )
        )
        clean_public_fact = str(public_fact or "").strip()
        if clean_public_fact:
            self.remember_subject_fact(item.name, clean_public_fact)
        custody_fact = f"持有剧情物件【{item.name}】"
        for subject, facts in list(self.subject_facts.items()):
            self.subject_facts[subject] = [fact for fact in facts if fact != custody_fact]
            if not self.subject_facts[subject]:
                self.subject_facts.pop(subject, None)
        if item.holder:
            self.remember_subject_fact(item.holder, custody_fact)
        return item

    def sync_carried_story_item_locations(
        self,
        holder_locations: dict[str, str],
        *,
        source: str = "SceneManager",
    ) -> list[str]:
        """Keep carried story items co-located with their authoritative holder."""

        normalized = {
            " ".join(str(holder or "").split()).strip():
            " ".join(str(location or "").split()).strip()
            for holder, location in holder_locations.items()
            if str(holder or "").strip() and str(location or "").strip()
        }
        changed: list[str] = []
        for item in self.story_items.values():
            holder = " ".join(str(item.holder or "").split()).strip()
            destination = normalized.get(holder, "")
            if (
                not destination
                or item.status != StoryItemStatus.CARRIED
                or item.location == destination
            ):
                continue
            previous = item.location
            item.location = destination
            item.history.append(
                StoryItemEvent(
                    operation="carry_move",
                    actor=holder,
                    changed_at=self._now(),
                    from_holder=holder,
                    to_holder=holder,
                    from_location=previous,
                    to_location=destination,
                    from_state=item.current_state,
                    to_state=item.current_state,
                    source=str(source or "SceneManager").strip(),
                )
            )
            changed.append(item.item_id)
        return changed

    @staticmethod
    def _story_item_name_key(value: str) -> str:
        return re.sub(r"[\s【】《》\[\]（）()，,。.!！?？·:：'\"]+", "", str(value or "")).casefold()

    @staticmethod
    def _story_locations_overlap(left: str, right: str) -> bool:
        lhs = "".join(str(left or "").split()).strip("·/ ")
        rhs = "".join(str(right or "").split()).strip("·/ ")
        return bool(lhs and rhs and (lhs == rhs or lhs.startswith(rhs + "·") or rhs.startswith(lhs + "·")))

    @staticmethod
    def _story_item_status_label(status: StoryItemStatus) -> str:
        return {
            StoryItemStatus.DESTROYED: "被销毁",
            StoryItemStatus.CONSUMED: "被消耗",
        }.get(status, status.value)

    def format_persistent_change(self, change: PersistentChange) -> str:
        if change.change_type == PersistentChangeType.EQUIPMENT:
            owner = change.owner or "未指定持有者"
            return f"{owner} 获得装备【{change.name}】：{change.description}"
        if change.change_type == PersistentChangeType.CONSUMABLE:
            owner = change.owner or "未指定持有者"
            return f"{owner} 获得一次性道具【{change.name}】：{change.description}"
        if change.change_type == PersistentChangeType.TRANSPORT:
            owner = change.owner or "小队"
            return f"{owner} 获得交通工具【{change.name}】：{change.description}"
        if change.change_type == PersistentChangeType.FACILITY:
            location = change.location or "未指定地点"
            return f"{location} 出现设施【{change.name}】：{change.description}"
        location_text = f"（{change.location}）" if change.location else ""
        return f"{change.name}{location_text}：{change.description}"

    def _same_persistent_change(self, left: PersistentChange, right: PersistentChange) -> bool:
        return (
            left.change_type == right.change_type
            and left.name == right.name
            and left.owner == right.owner
            and left.location == right.location
            and left.source == right.source
        )

    def _sync_world_sheet_persistent_change(self, change: PersistentChange, summary: str) -> None:
        if self.world_sheet is None:
            return
        if summary not in self.world_sheet.persistent_changes:
            self.world_sheet.persistent_changes.append(summary)
        if change.change_type in {PersistentChangeType.EQUIPMENT, PersistentChangeType.CONSUMABLE, PersistentChangeType.TRANSPORT}:
            if summary not in self.world_sheet.created_assets:
                self.world_sheet.created_assets.append(summary)
        if change.change_type == PersistentChangeType.FACILITY:
            location = change.location or "未指定地点"
            facilities = self.world_sheet.location_facilities.setdefault(location, [])
            facility_summary = f"{change.name}：{change.description}"
            if facility_summary not in facilities:
                facilities.append(facility_summary)

    def render_npc_prompt(self, name: str, *, scene_context: str = "", include_secrets: bool = True) -> str:
        canonical = self.resolve_npc_name(name)
        if not canonical:
            raise KeyError(f"找不到 NPC 人格：{name}")
        persona = self.npc_personas[canonical]
        goals = "；".join(persona.goals) if persona.goals else "尚未明确记录"
        taboos = "；".join(persona.taboos) if persona.taboos else "尚未明确记录"
        secrets = "；".join(persona.secrets) if include_secrets and persona.secrets else "不向当前调用提供"
        relevant_memories = self.relevant_npc_memories(name, scene_context, limit=6)
        memories = "；".join(relevant_memories) if relevant_memories else "尚无关键近期记忆"
        subject_facts = (
            "；".join(self.subject_facts.get(persona.name, [])[-6:])
            if self.subject_facts.get(persona.name)
            else "尚无结构化已知事实"
        )
        custom_prompt = f"\n额外人设提示：{persona.custom_prompt}" if persona.custom_prompt else ""
        return (
            f"NPC稳定ID：{persona.npc_id}\n"
            f"发言主体类型：{'集体角色' if persona.entity_kind == 'collective' else '单体人物'}\n"
            f"NPC名称：{persona.name}\n"
            f"别名：{'、'.join(persona.aliases) if persona.aliases else '无'}\n"
            f"公开身份：{persona.public_identity or persona.name}\n"
            f"剧情定位：{persona.role_in_story or '未定义'}\n"
            f"核心驱动力：{persona.core_drive or '未定义'}\n"
            f"行为风格：{persona.manner or '未定义'}\n"
            f"说话风格：{persona.speech_style or '未定义'}\n"
            f"战斗风格：{persona.combat_style or '未定义'}\n"
            f"NPC阶级：{persona.npc_rank or 'minor'}\n"
            f"当前筹码：{persona.leverage or '未明确'}\n"
            f"权限范围：{persona.authority_scope or '仅能决定自身行动'}\n"
            f"知识范围：{persona.knowledge_scope or '只知道亲历与当前可见信息'}\n"
            f"受阻动作：{persona.refusal_move or '按自身目标作出具体回应'}\n"
            f"已知技能：{'；'.join(persona.known_skills) if persona.known_skills else '无'}\n"
            f"战斗行动：{'；'.join(persona.combat_actions) if persona.combat_actions else '无'}\n"
            f"首次出场场景：{persona.first_scene or '未记录'}\n"
            f"当前位置：{persona.current_location or '未记录'}\n"
            f"当前情绪：{persona.current_mood or '未记录'}\n"
            f"当前立场：{persona.current_stance or '未记录'}\n"
            f"当前首要目标：{persona.active_goal or '未明确'}\n"
            f"当前目标：{goals}\n"
            f"行为禁忌：{taboos}\n"
            f"隐藏秘密：{secrets}\n"
            f"结构化已知事实：{subject_facts}\n"
            f"近期记忆：{memories}"
            f"{custom_prompt}"
        )

    def npc_audit_payload(self, *, include_private: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for persona in list(self.npc_personas.values())[-max(1, limit):]:
            row: dict[str, Any] = {
                "npc_id": persona.npc_id,
                "name": persona.name,
                "entity_kind": persona.entity_kind,
                "aliases": list(persona.aliases),
                "public_identity": persona.public_identity,
                "role_in_story": persona.role_in_story,
                "manner": persona.manner,
                "speech_style": persona.speech_style,
                "combat_style": persona.combat_style,
                "npc_rank": persona.npc_rank,
                "first_scene": persona.first_scene,
                "current_location": persona.current_location,
                "current_mood": persona.current_mood,
                "current_stance": persona.current_stance,
                "last_seen_scene": persona.last_seen_scene,
                "status": persona.status,
                "relationships": dict(persona.relationships),
                "voice_examples": list(persona.voice_examples),
                "memory_count": len(persona.memory_records or persona.memories),
            }
            if include_private:
                row.update(
                    {
                        "core_drive": persona.core_drive,
                        "goals": list(persona.goals),
                        "active_goal": persona.active_goal,
                        "leverage": persona.leverage,
                        "authority_scope": persona.authority_scope,
                        "knowledge_scope": persona.knowledge_scope,
                        "refusal_move": persona.refusal_move,
                        "known_skills": list(persona.known_skills),
                        "combat_actions": list(persona.combat_actions),
                        "completed_goals": list(persona.completed_goals),
                        "taboos": list(persona.taboos),
                        "secrets": list(persona.secrets),
                        "custom_prompt": persona.custom_prompt,
                        "recent_memories": self.relevant_npc_memories(persona.name, limit=8),
                        "memory_records": list(persona.memory_records[-8:]),
                    }
                )
            rows.append(row)
        return rows
