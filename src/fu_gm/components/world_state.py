from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fu_gm.models import (
    GMSecret,
    GMSecretRevision,
    MapLocation,
    MapRouteEdge,
    MapRouteSegment,
    MemoryEvent,
    MemoryRecallResult,
    MemoryRelation,
    MemoryVisibility,
    NPCPersona,
    PartySheet,
    PersistentChange,
    PersistentChangeType,
    SecretLockLevel,
    WorldCreationProfile,
    WorldSheet,
)
from fu_gm.gm_guidance import build_gm_guidance


class WorldState:
    def __init__(self) -> None:
        self.session_pillars: list[str] = []
        self.map_notes: dict[str, str] = {}
        self.map_locations: dict[str, MapLocation] = {}
        self.map_routes: dict[str, MapRouteEdge] = {}
        self.npc_relationships: dict[str, list[str]] = {}
        self.memories: list[str] = []
        self.npc_personas: dict[str, NPCPersona] = {}
        self.subject_facts: dict[str, list[str]] = {}
        self.persistent_changes: list[PersistentChange] = []
        self.memory_events: list[MemoryEvent] = []
        self.memory_relations: list[MemoryRelation] = []
        self.gm_secrets: dict[str, GMSecret] = {}
        self.world_profile = WorldCreationProfile()
        self.party_sheet: PartySheet | None = None
        self.world_sheet: WorldSheet | None = None
        self.present_players: list[str] = []
        self.absent_players: dict[str, str] = {}

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
            visibility=MemoryVisibility(visibility),
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
            visibility=MemoryVisibility(visibility),
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
        self.add_memory(f"已接受物语改写：{fact}")
        self.record_memory_event(
            f"已接受物语改写：{fact}",
            kind="story_change",
            visibility=MemoryVisibility.PUBLIC,
            tags=["story_change"],
        )

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
            "投这个",
            "额外补一个",
            "额外补充",
            "另外补一个",
            "顺便补一个",
            "我的角色",
            "接下来",
            "下一步",
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
                "投票",
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
        public_identity: str = "",
        role_in_story: str = "",
        core_drive: str = "",
        manner: str = "",
        speech_style: str = "",
        combat_style: str = "",
        first_scene: str = "",
        goals: list[str] | None = None,
        taboos: list[str] | None = None,
        secrets: list[str] | None = None,
        custom_prompt: str = "",
    ) -> NPCPersona:
        if name in self.npc_personas:
            persona = self.npc_personas[name]
            if public_identity:
                persona.public_identity = persona.public_identity or public_identity
            if role_in_story:
                persona.role_in_story = persona.role_in_story or role_in_story
            if core_drive:
                persona.core_drive = persona.core_drive or core_drive
            if manner:
                persona.manner = persona.manner or manner
            if speech_style:
                persona.speech_style = persona.speech_style or speech_style
            if combat_style:
                persona.combat_style = persona.combat_style or combat_style
            if first_scene:
                persona.first_scene = persona.first_scene or first_scene
            if custom_prompt:
                persona.custom_prompt = persona.custom_prompt or custom_prompt
            for value in goals or []:
                if value not in persona.goals:
                    persona.goals.append(value)
            for value in taboos or []:
                if value not in persona.taboos:
                    persona.taboos.append(value)
            for value in secrets or []:
                if value not in persona.secrets:
                    persona.secrets.append(value)
            return persona

        persona = NPCPersona(
            name=name,
            public_identity=public_identity or name,
            role_in_story=role_in_story,
            core_drive=core_drive,
            manner=manner,
            speech_style=speech_style,
            combat_style=combat_style,
            first_scene=first_scene,
            goals=list(goals or []),
            taboos=list(taboos or []),
            secrets=list(secrets or []),
            custom_prompt=custom_prompt,
        )
        self.npc_personas[name] = persona
        return persona

    def remember_npc_event(self, name: str, note: str) -> None:
        persona = self.ensure_npc_persona(name)
        if note not in persona.memories:
            persona.memories.append(note)

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

    def render_npc_prompt(self, name: str) -> str:
        persona = self.ensure_npc_persona(name)
        goals = "；".join(persona.goals) if persona.goals else "尚未明确记录"
        taboos = "；".join(persona.taboos) if persona.taboos else "尚未明确记录"
        secrets = "；".join(persona.secrets) if persona.secrets else "尚未明确记录"
        memories = "；".join(persona.memories[-6:]) if persona.memories else "尚无关键近期记忆"
        subject_facts = "；".join(self.subject_facts.get(name, [])[-6:]) if self.subject_facts.get(name) else "尚无结构化已知事实"
        custom_prompt = f"\n额外人设提示：{persona.custom_prompt}" if persona.custom_prompt else ""
        return (
            f"NPC名称：{persona.name}\n"
            f"公开身份：{persona.public_identity or persona.name}\n"
            f"剧情定位：{persona.role_in_story or '未定义'}\n"
            f"核心驱动力：{persona.core_drive or '未定义'}\n"
            f"行为风格：{persona.manner or '未定义'}\n"
            f"说话风格：{persona.speech_style or '未定义'}\n"
            f"战斗风格：{persona.combat_style or '未定义'}\n"
            f"首次出场场景：{persona.first_scene or '未记录'}\n"
            f"当前目标：{goals}\n"
            f"行为禁忌：{taboos}\n"
            f"隐藏秘密：{secrets}\n"
            f"结构化已知事实：{subject_facts}\n"
            f"近期记忆：{memories}"
            f"{custom_prompt}"
        )
