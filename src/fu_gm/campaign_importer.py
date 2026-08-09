from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.llm_utils import extract_json_object
from fu_gm.prompt_cache import build_cache_friendly_messages
from fu_gm.models import (
    MemoryVisibility,
    NPCPersona,
    PartyMemberEntry,
    PartySheet,
    SecretLockLevel,
    WorldSheet,
    normalize_memory_visibility,
)
from fu_gm.safety_parser import extract_safety_declarations


IMPORT_SYSTEM_PROMPT = """你是《最终物语》AI GM 的迁移存档整理器。
你的任务是把用户粘贴的旧群聊记录整理成可导入 FU-GM 存档的结构化事实。

硬性原则：
1. 只提取聊天记录中明确出现、玩家确认、或多轮上下文强烈支持的事实；不要补设定。
2. 若旧 AI 回复和玩家/规则修正冲突，优先相信玩家更正和后出现的明确规则。
3. 忽略旧错误模板中这类错误内容：LV1 起始、40 点属性、17 个种族、42 个职业、职业完全自由无固定列表。
4. 《最终物语》起始角色通常为 5 级；可选职业固定为：奥灵使、拟兽使、暗刃骑士、元素使、熵术士、怒焰斗士、守护者、博学家、游说家、浪客、神射手、御魂使、造物使、旅人、武器大师。
5. 属性草稿若出现，使用敏捷/洞察/力量/意志，骰等级用 6/8/10/12 数字表示；不确定就留空并写入 open_questions。
6. 界限与帷幕要谨慎提取：界限是完全不出现，帷幕是淡出/不细描。
7. GM 暗线可以写入 gm_secret_notes 或 gm_secrets，但不要把私密暗线混进 public_memories。
8. 输出必须是单个 JSON 对象，不要 Markdown。

JSON 结构：
{
  "summary": "本次迁移摘要",
  "confidence": 0.0,
  "world_updates": {
    "campaign_title": "",
    "continent_name": "",
    "tone_preferences": [],
    "playstyle_themes": [],
    "party_dynamic": "",
    "description_style": "",
    "violence_guideline": "",
    "evil_guidelines": [],
    "romance_guideline": "",
    "consensus_notes": [],
    "pre_session_ready": false,
    "world_style": "",
    "world_shape": "",
    "map_card": "",
    "travel_day_length": "",
    "magic_tech_role": "",
    "pillars": {},
    "core_themes": [],
    "group_concept": "",
    "starting_region": "",
    "major_locations": {},
    "kingdoms": {},
    "kingdom_contributors": {},
    "historical_events": [],
    "historical_event_contributors": {},
    "factions": {},
    "villain_seeds": [],
    "villain_mirrors": [],
    "mysteries": [],
    "mystery_contributors": {},
    "world_threats": [],
    "threat_contributors": {},
    "safety_lines": [],
    "safety_veils": [],
    "hero_drafts": {
      "玩家或角色键": {
        "player_name": "",
        "hero_name": "",
        "identity": "",
        "theme": "",
        "origin": "",
        "classes": {},
        "attributes": {},
        "skills": {},
        "skill_options": {},
        "spells": [],
        "bound_arcana": [],
        "equipment": [],
        "bonds": [],
        "notes": [],
        "open_questions": [],
        "confirmed": false
      }
    },
    "gm_secret_notes": [],
    "selected_first_act_summary": "",
    "starting_bond_suggestions": [],
    "open_questions": [],
    "completed": false
  },
  "subject_facts": {"实体名": ["事实"]},
  "public_memories": [],
  "private_memories": [],
  "memory_events": [
    {"summary": "", "kind": "migration_fact", "visibility": "public", "entities": [], "tags": []}
  ],
  "gm_secrets": [
    {"secret_id": "", "title": "", "content": "", "lock_level": "draft", "related_entities": [], "public_clues": [], "tags": []}
  ],
  "npc_personas": [
    {"name": "", "public_identity": "", "role_in_story": "", "core_drive": "", "goals": [], "secrets": []}
  ],
  "world_sheet": {},
  "party_sheet": {},
  "warnings": [],
  "unresolved_questions": []
}
"""


@dataclass
class ChatLogImportResult:
    import_payload: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    fallback_used: bool = False
    source: str = "llm"

    def to_dict(self) -> dict[str, Any]:
        return {
            "import_payload": self.import_payload,
            "warnings": list(self.warnings),
            "fallback_used": self.fallback_used,
            "source": self.source,
        }


class CampaignChatLogImporter:
    def __init__(
        self,
        *,
        client: OpenAICompatibleClient | None = None,
        model: str = "",
        gm_name: str = "时悠",
    ) -> None:
        self.client = client
        self.model = model
        self.gm_name = gm_name

    def extract(
        self,
        *,
        chat_log: str,
        campaign_id: str,
        existing_context: dict[str, Any] | None = None,
    ) -> ChatLogImportResult:
        chat_log = str(chat_log or "").strip()
        if not chat_log:
            raise ValueError("导入聊天记录不能为空。")

        if self._looks_like_json(chat_log):
            try:
                return ChatLogImportResult(
                    import_payload=self.normalize_payload(extract_json_object(chat_log)),
                    source="json",
                )
            except Exception:
                pass

        warnings: list[str] = []
        if self.client is not None and self.model:
            try:
                return ChatLogImportResult(
                    import_payload=self.normalize_payload(
                        self._extract_with_llm(
                            chat_log=chat_log,
                            campaign_id=campaign_id,
                            existing_context=existing_context or {},
                        )
                    ),
                    source="llm",
                )
            except Exception as exc:
                warnings.append(f"LLM 导入整理失败，已改用本地保守提取：{exc}")

        return ChatLogImportResult(
            import_payload=self.normalize_payload(self._heuristic_extract(chat_log)),
            warnings=warnings,
            fallback_used=True,
            source="heuristic",
        )

    def normalize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            payload = {}
        if "world_profile" in payload and "world_updates" not in payload:
            payload["world_updates"] = payload.get("world_profile")
        payload.setdefault("world_updates", {})
        world_updates = payload["world_updates"] if isinstance(payload["world_updates"], dict) else {}
        payload["world_updates"] = world_updates

        for field_name in (
            "tone_preferences",
            "playstyle_themes",
            "evil_guidelines",
            "consensus_notes",
            "core_themes",
            "historical_events",
            "villain_seeds",
            "villain_mirrors",
            "mysteries",
            "world_threats",
            "safety_lines",
            "safety_veils",
            "gm_secret_notes",
            "starting_bond_suggestions",
            "open_questions",
        ):
            world_updates[field_name] = self._string_list(world_updates.get(field_name, []))

        for field_name in (
            "pillars",
            "major_locations",
            "kingdoms",
            "factions",
        ):
            world_updates[field_name] = self._string_dict(world_updates.get(field_name, {}))

        for field_name in (
            "kingdom_contributors",
            "historical_event_contributors",
            "mystery_contributors",
            "threat_contributors",
        ):
            world_updates[field_name] = self._string_list_dict(world_updates.get(field_name, {}))

        if not isinstance(world_updates.get("hero_drafts"), dict):
            world_updates["hero_drafts"] = {}
        world_updates["hero_drafts"] = {
            str(key): self._normalize_hero_draft_patch(value)
            for key, value in world_updates["hero_drafts"].items()
            if isinstance(value, dict)
        }

        for scalar_name in (
            "campaign_title",
            "continent_name",
            "party_dynamic",
            "description_style",
            "violence_guideline",
            "romance_guideline",
            "world_style",
            "world_shape",
            "map_card",
            "travel_day_length",
            "magic_tech_role",
            "group_concept",
            "starting_region",
            "selected_first_act_summary",
        ):
            if scalar_name in world_updates:
                world_updates[scalar_name] = str(world_updates.get(scalar_name) or "").strip()

        if "pre_session_ready" in world_updates:
            world_updates["pre_session_ready"] = bool(world_updates["pre_session_ready"])
        if "completed" in world_updates:
            world_updates["completed"] = bool(world_updates["completed"])

        payload["subject_facts"] = {
            str(subject): self._string_list(facts)
            for subject, facts in (payload.get("subject_facts") or {}).items()
            if str(subject).strip()
        } if isinstance(payload.get("subject_facts"), dict) else {}

        for field_name in ("public_memories", "private_memories", "warnings", "unresolved_questions"):
            payload[field_name] = self._string_list(payload.get(field_name, []))

        payload["memory_events"] = [
            self._normalize_memory_event(item)
            for item in payload.get("memory_events", [])
            if isinstance(item, dict) and str(item.get("summary", "")).strip()
        ] if isinstance(payload.get("memory_events"), list) else []
        payload["gm_secrets"] = [
            self._normalize_gm_secret(item)
            for item in payload.get("gm_secrets", [])
            if isinstance(item, dict) and str(item.get("title") or item.get("content") or "").strip()
        ] if isinstance(payload.get("gm_secrets"), list) else []
        payload["npc_personas"] = [
            item for item in payload.get("npc_personas", []) if isinstance(item, dict) and str(item.get("name", "")).strip()
        ] if isinstance(payload.get("npc_personas"), list) else []
        payload["world_sheet"] = payload.get("world_sheet") if isinstance(payload.get("world_sheet"), dict) else {}
        payload["party_sheet"] = payload.get("party_sheet") if isinstance(payload.get("party_sheet"), dict) else {}
        payload["summary"] = str(payload.get("summary") or "").strip()
        try:
            payload["confidence"] = max(0.0, min(1.0, float(payload.get("confidence", 0.0))))
        except (TypeError, ValueError):
            payload["confidence"] = 0.0
        return payload

    def apply_to_app(self, app, import_payload: dict[str, Any], *, source: str = "migration_import") -> dict[str, Any]:
        payload = self.normalize_payload(import_payload)
        world_updates = payload.get("world_updates", {})
        world = app.world_state.world_profile
        app.session_zero_manager.state.world = world
        app.session_zero_manager.apply_world_updates(world_updates)
        self._append_unique(app.world_state.world_profile.open_questions, payload.get("unresolved_questions", []))

        for note in payload.get("public_memories", []):
            self._append_unique(app.world_state.memories, [note])
        for note in payload.get("private_memories", []):
            self._record_event_once(
                app,
                summary=note,
                kind="migration_private_note",
                visibility=MemoryVisibility.PRIVATE,
                tags=["migration", "private"],
                source=source,
            )
        for subject, facts in payload.get("subject_facts", {}).items():
            for fact in facts:
                app.world_state.remember_subject_fact(subject, fact)
        for event in payload.get("memory_events", []):
            self._record_event_once(
                app,
                summary=event["summary"],
                kind=event.get("kind") or "migration_fact",
                visibility=event.get("visibility") or MemoryVisibility.PUBLIC,
                entities=event.get("entities", []),
                tags=["migration", *event.get("tags", [])],
                source=source,
                payload=event.get("payload", {}),
            )
        for secret_note in payload.get("world_updates", {}).get("gm_secret_notes", []):
            self._append_unique(app.world_state.world_profile.gm_secret_notes, [secret_note])
        for secret in payload.get("gm_secrets", []):
            app.world_state.upsert_gm_secret(
                secret["secret_id"],
                title=secret["title"],
                content=secret["content"],
                lock_level=secret["lock_level"],
                related_entities=secret["related_entities"],
                public_clues=secret["public_clues"],
                tags=["migration", *secret["tags"]],
            )
        for persona_data in payload.get("npc_personas", []):
            app.world_state.npc_personas[persona_data["name"]] = self._merge_npc_persona(
                app.world_state.npc_personas.get(persona_data["name"]),
                persona_data,
            )
        self._sync_world_and_party_sheets(app, payload)
        app.world_state.apply_world_profile(app.world_state.world_profile)

        imported_counts = self.import_counts(payload)
        summary = payload.get("summary") or "聊天记录迁移导入完成。"
        self._record_event_once(
            app,
            summary=f"迁移导入：{summary}",
            kind="migration_import",
            visibility=MemoryVisibility.PRIVATE,
            tags=["migration"],
            source=source,
            payload={"counts": imported_counts, "confidence": payload.get("confidence", 0.0)},
        )
        return imported_counts

    def import_counts(self, payload: dict[str, Any]) -> dict[str, int]:
        payload = self.normalize_payload(payload)
        world_updates = payload.get("world_updates", {})
        return {
            "hero_drafts": len(world_updates.get("hero_drafts", {})),
            "safety_lines": len(world_updates.get("safety_lines", [])),
            "safety_veils": len(world_updates.get("safety_veils", [])),
            "locations": len(world_updates.get("major_locations", {})),
            "factions": len(world_updates.get("factions", {})),
            "subject_fact_subjects": len(payload.get("subject_facts", {})),
            "memory_events": len(payload.get("memory_events", [])),
            "gm_secrets": len(payload.get("gm_secrets", [])),
            "npc_personas": len(payload.get("npc_personas", [])),
        }

    def _extract_with_llm(self, *, chat_log: str, campaign_id: str, existing_context: dict[str, Any]) -> dict[str, Any]:
        context = json.dumps(existing_context, ensure_ascii=False, indent=2)
        content = self.client.create_chat_completion(
            model=self.model,
            messages=build_cache_friendly_messages(
                static_system_prompt=IMPORT_SYSTEM_PROMPT,
                user_content=(
                        f"目标 campaign_id：{campaign_id}\n"
                        f"当前存档摘要 JSON：\n{context}\n\n"
                        "请整理下面的旧群聊记录，输出导入 JSON：\n"
                        "<chat_log>\n"
                        f"{chat_log}\n"
                        "</chat_log>"
                ),
                cache_family="campaign-import",
            ),
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        return extract_json_object(content)

    def _heuristic_extract(self, chat_log: str) -> dict[str, Any]:
        player_text = self._player_authored_text(chat_log)
        world_updates: dict[str, Any] = {
            "safety_lines": [],
            "safety_veils": [],
            "hero_drafts": {},
            "open_questions": [],
        }
        for kind, item in extract_safety_declarations(player_text):
            target = "safety_lines" if kind == "line" else "safety_veils"
            if item not in world_updates[target]:
                world_updates[target].append(item)

        self._extract_line_value(player_text, world_updates, "campaign_title", ("战役标题", "团名", "标题"))
        self._extract_line_value(player_text, world_updates, "world_style", ("世界风格", "世界基调", "世界观"))
        self._extract_line_value(player_text, world_updates, "group_concept", ("小队原型", "队伍主题", "队伍概念"))
        self._extract_line_value(player_text, world_updates, "starting_region", ("起始区域", "起点", "故乡"))

        hero_names = self._extract_hero_names(player_text)
        for name in hero_names:
            world_updates["hero_drafts"][name] = {
                "hero_name": name,
                "notes": ["从迁移聊天记录中识别到的角色草稿；具体规则字段需继续确认。"],
                "open_questions": ["请确认该角色的身份、主题、故乡、职业等级、属性和技能。"],
            }

        return {
            "summary": "本地保守提取完成；建议用 LLM 预览补全更多结构化事实。",
            "confidence": 0.35 if hero_names or world_updates["safety_lines"] or world_updates["safety_veils"] else 0.15,
            "world_updates": world_updates,
            "public_memories": [],
            "private_memories": [],
            "memory_events": [
                {
                    "summary": "已从旧聊天记录执行一次迁移导入整理。",
                    "kind": "migration_fact",
                    "visibility": "private",
                    "entities": hero_names,
                    "tags": ["migration"],
                }
            ],
            "warnings": ["当前未调用 LLM，仅执行保守提取；请预览后再导入。"],
            "unresolved_questions": ["旧聊天记录中未被结构化识别的细节需要 GM 或玩家复核。"],
        }

    def _sync_world_and_party_sheets(self, app, payload: dict[str, Any]) -> None:
        profile = app.world_state.world_profile
        world_sheet_data = payload.get("world_sheet", {})
        world_sheet = app.world_state.world_sheet or WorldSheet()
        self._set_scalar(world_sheet, "campaign_title", world_sheet_data.get("campaign_title") or profile.campaign_title)
        self._set_scalar(world_sheet, "continent_name", world_sheet_data.get("continent_name") or profile.continent_name)
        self._set_scalar(world_sheet, "world_style", world_sheet_data.get("world_style") or profile.world_style)
        self._set_scalar(world_sheet, "starting_region", world_sheet_data.get("starting_region") or profile.starting_region)
        self._set_scalar(
            world_sheet,
            "selected_first_act",
            world_sheet_data.get("selected_first_act") or profile.selected_first_act_summary,
        )
        world_sheet.pillars.update(profile.pillars)
        world_sheet.major_locations.update(profile.major_locations)
        world_sheet.factions.update(profile.factions)
        self._append_unique(world_sheet.core_themes, profile.core_themes)
        self._append_unique(world_sheet.villain_seeds, profile.villain_seeds)
        self._append_unique(world_sheet.villain_mirrors, profile.villain_mirrors)
        self._append_unique(world_sheet.mysteries, profile.mysteries)
        self._append_unique(world_sheet.starting_bond_suggestions, profile.starting_bond_suggestions)
        self._append_unique(world_sheet.safety_lines, profile.safety_lines)
        self._append_unique(world_sheet.safety_veils, profile.safety_veils)
        for key, value in self._string_dict(world_sheet_data.get("major_locations", {})).items():
            world_sheet.major_locations[key] = value
        for key, value in self._string_dict(world_sheet_data.get("factions", {})).items():
            world_sheet.factions[key] = value
        app.world_state.apply_world_sheet(world_sheet)

        party_data = payload.get("party_sheet", {})
        party_sheet = app.world_state.party_sheet or PartySheet()
        self._set_scalar(party_sheet, "group_concept", party_data.get("group_concept") or profile.group_concept)
        self._set_scalar(party_sheet, "shared_goal", party_data.get("shared_goal"))
        self._set_scalar(party_sheet, "starting_region", party_data.get("starting_region") or profile.starting_region)
        self._append_unique(party_sheet.party_notes, self._string_list(party_data.get("party_notes", [])))
        self._append_unique(party_sheet.open_questions, self._string_list(party_data.get("open_questions", [])))
        for member in party_data.get("members", []) if isinstance(party_data.get("members"), list) else []:
            if isinstance(member, dict):
                self._upsert_party_member(party_sheet, member)
        for key, draft in profile.hero_drafts.items():
            if draft.hero_name:
                self._upsert_party_member(
                    party_sheet,
                    {
                        "player_name": draft.player_name or key,
                        "hero_name": draft.hero_name,
                        "identity": draft.identity,
                        "theme": draft.theme,
                        "origin": draft.origin,
                        "classes": draft.classes,
                        "skills": draft.skills,
                        "equipment": draft.equipment,
                        "bonds": draft.bonds,
                    },
                )
        app.world_state.apply_party_sheet(party_sheet)

    def _upsert_party_member(self, party_sheet: PartySheet, data: dict[str, Any]) -> None:
        hero_name = str(data.get("hero_name") or data.get("name") or "").strip()
        player_name = str(data.get("player_name") or "").strip()
        if not hero_name and not player_name:
            return
        existing = next(
            (
                member
                for member in party_sheet.members
                if (hero_name and member.hero_name == hero_name) or (player_name and member.player_name == player_name)
            ),
            None,
        )
        if existing is None:
            existing = PartyMemberEntry(player_name=player_name, hero_name=hero_name, identity="", theme="", origin="", classes={})
            party_sheet.members.append(existing)
        self._set_scalar(existing, "player_name", player_name)
        self._set_scalar(existing, "hero_name", hero_name)
        for field_name in ("identity", "theme", "origin"):
            self._set_scalar(existing, field_name, data.get(field_name))
        if isinstance(data.get("classes"), dict):
            existing.classes.update({str(key): self._int_value(value, default=0) for key, value in data["classes"].items()})
        if isinstance(data.get("skills"), dict):
            existing.skills.update({str(key): self._int_value(value, default=0) for key, value in data["skills"].items()})
        if isinstance(data.get("skill_options"), dict):
            existing.skill_options.update(
                {
                    str(key): self._string_list(value)
                    for key, value in data["skill_options"].items()
                    if str(key).strip()
                }
            )
        self._append_unique(existing.equipment, self._string_list(data.get("equipment", [])))
        self._append_unique(existing.bonds, self._string_list(data.get("bonds", [])))
        if "zenit" in data:
            existing.zenit = self._int_value(data.get("zenit"), default=existing.zenit)

    def _normalize_hero_draft_patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for field_name in ("player_name", "hero_name", "identity", "theme", "origin"):
            if field_name in patch:
                normalized[field_name] = str(patch.get(field_name) or "").strip()
        for field_name in ("classes", "attributes", "skills"):
            if isinstance(patch.get(field_name), dict):
                normalized[field_name] = {
                    str(key): self._int_value(value, default=0)
                    for key, value in patch[field_name].items()
                    if str(key).strip()
                }
        if isinstance(patch.get("skill_options"), dict):
            normalized["skill_options"] = {
                str(key): self._string_list(value)
                for key, value in patch["skill_options"].items()
                if str(key).strip()
            }
        for field_name in ("bonds", "spells", "bound_arcana", "equipment", "notes", "open_questions"):
            normalized[field_name] = self._string_list(patch.get(field_name, []))
        if "confirmed" in patch:
            normalized["confirmed"] = bool(patch["confirmed"])
        return normalized

    def _normalize_memory_event(self, item: dict[str, Any]) -> dict[str, Any]:
        visibility = str(item.get("visibility") or "public").strip().lower()
        if visibility not in {"public", "private"}:
            visibility = "public"
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        return {
            "summary": str(item.get("summary") or "").strip(),
            "kind": str(item.get("kind") or "migration_fact").strip() or "migration_fact",
            "visibility": visibility,
            "entities": self._string_list(item.get("entities", [])),
            "tags": self._string_list(item.get("tags", [])),
            "payload": payload,
        }

    def _normalize_gm_secret(self, item: dict[str, Any]) -> dict[str, Any]:
        secret_id = str(item.get("secret_id") or item.get("title") or f"migration_secret_{self._now_compact()}").strip()
        lock_level = str(item.get("lock_level") or SecretLockLevel.DRAFT.value).strip().lower()
        if lock_level not in {level.value for level in SecretLockLevel}:
            lock_level = SecretLockLevel.DRAFT.value
        return {
            "secret_id": self._safe_id(secret_id),
            "title": str(item.get("title") or secret_id).strip(),
            "content": str(item.get("content") or "").strip(),
            "lock_level": lock_level,
            "related_entities": self._string_list(item.get("related_entities", [])),
            "public_clues": self._string_list(item.get("public_clues", [])),
            "tags": self._string_list(item.get("tags", [])),
        }

    def _merge_npc_persona(self, existing: NPCPersona | None, data: dict[str, Any]) -> NPCPersona:
        persona = existing or NPCPersona(name=str(data.get("name") or "").strip())
        imported_profile_status = str(
            data.get("profile_status") or ""
        ).strip().lower()
        if imported_profile_status in {"placeholder", "established"}:
            persona.profile_status = imported_profile_status
        imported_kind = str(data.get("entity_kind") or "").strip().lower()
        if imported_kind in {"individual", "collective"}:
            persona.entity_kind = imported_kind
        for field_name in (
            "npc_id",
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
            "custom_prompt",
        ):
            value = str(data.get(field_name) or "").strip()
            if value and not getattr(persona, field_name):
                setattr(persona, field_name, value)
        for field_name in (
            "aliases",
            "goals",
            "taboos",
            "secrets",
            "memories",
            "completed_goals",
            "voice_examples",
            "known_skills",
            "combat_actions",
        ):
            self._append_unique(getattr(persona, field_name), self._string_list(data.get(field_name, [])))
        for field_name in (
            "current_location",
            "current_mood",
            "current_stance",
            "active_goal",
            "last_seen_scene",
            "status",
        ):
            value = str(data.get(field_name) or "").strip()
            if value:
                setattr(persona, field_name, value)
        relationships = data.get("relationships")
        if isinstance(relationships, dict):
            persona.relationships.update(
                {
                    str(target).strip(): str(relation).strip()
                    for target, relation in relationships.items()
                    if str(target).strip() and str(relation).strip()
                }
            )
        for record in data.get("memory_records", []) if isinstance(data.get("memory_records"), list) else []:
            if isinstance(record, dict) and not any(
                existing.get("note") == record.get("note") for existing in persona.memory_records
            ):
                persona.memory_records.append(dict(record))
        return persona

    def _record_event_once(
        self,
        app,
        *,
        summary: str,
        kind: str,
        visibility: MemoryVisibility | str = MemoryVisibility.PUBLIC,
        entities: list[str] | None = None,
        tags: list[str] | None = None,
        source: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        visibility_value = normalize_memory_visibility(visibility)
        for event in app.world_state.memory_events:
            if event.summary == summary and event.kind == kind and event.visibility == visibility_value:
                return
        app.world_state.record_memory_event(
            summary,
            kind=kind,
            visibility=visibility_value,
            entities=list(entities or []),
            tags=list(tags or []),
            source=source,
            payload=dict(payload or {}),
        )

    def _extract_line_value(self, text: str, target: dict[str, Any], field_name: str, labels: tuple[str, ...]) -> None:
        for label in labels:
            match = re.search(rf"{re.escape(label)}[：:]\s*([^\n。；;]+)", text)
            if match:
                target[field_name] = match.group(1).strip(" 《》\"'")
                return

    def _player_authored_text(self, text: str) -> str:
        """Keep speaker-authored lines and ignore the GM's prompts in pasted chat logs."""
        lines = str(text or "").splitlines()
        header_pattern = re.compile(r"^([^:：\n]{1,48})[:：]\s*\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s*$")
        kept: list[str] = []
        current_is_player = True
        found_header = False
        for line in lines:
            stripped = line.strip()
            match = header_pattern.match(stripped)
            if match:
                found_header = True
                speaker = match.group(1).strip()
                lowered = speaker.lower()
                current_is_player = (
                    speaker != self.gm_name
                    and speaker not in {"系统", "旁白", "GM", "gm"}
                    and not lowered.startswith(("bot", "assistant"))
                )
                continue
            if current_is_player and stripped:
                kept.append(line)
        return "\n".join(kept).strip() if found_header and kept else str(text or "")

    def _extract_hero_names(self, text: str) -> list[str]:
        names: list[str] = []
        for marker in ("艾丽妮", "诺艾尔"):
            if marker in text:
                names.append(marker)
        patterns = [
            r"(?:角色名|人物名|PC名|玩家角色名|名字)\s*(?:叫|是|为|叫做)?\s*(?P<name>[\u4e00-\u9fffA-Za-z·]{2,8})",
            r"(?:我的角色|我(?:的)?人物|我(?:的)?PC)\s*(?:叫|名叫|名字是|名字叫)\s*(?P<name>[\u4e00-\u9fffA-Za-z·]{2,8})",
            r"(?:角色|人物|PC|玩家角色)[：:]\s*(?P<name>[\u4e00-\u9fffA-Za-z·]{2,8})(?=$|[，,。；;\s])",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                name = self._clean_hero_name(match.group("name"))
                if self._looks_like_hero_name(name):
                    names.append(name)
        deduped: list[str] = []
        for name in names:
            if name not in deduped:
                deduped.append(name)
        return deduped

    def _clean_hero_name(self, value: str) -> str:
        return str(value or "").strip(" ：:，,。.;；!?！？《》\"'“”‘’（）()[]【】")

    def _looks_like_hero_name(self, value: str) -> bool:
        name = self._clean_hero_name(value)
        if not (2 <= len(name) <= 8):
            return False
        if not re.fullmatch(r"[\u4e00-\u9fffA-Za-z·]+", name):
            return False
        blocked_fragments = (
            "角色",
            "人物",
            "玩家",
            "创建",
            "规则",
            "属性",
            "职业",
            "技能",
            "主题",
            "特点",
            "故乡",
            "身份",
            "冒险者",
            "学者",
            "盗贼",
            "术士",
            "灵魂",
            "机械",
            "藤蔓",
            "成长",
            "种族",
        )
        if any(fragment in name for fragment in blocked_fragments):
            return False
        return not name.startswith(("是", "在", "会", "被", "但", "和", "与", "或", "也许", "一个", "这些"))

    def _append_unique(self, target: list[str], values: list[str]) -> None:
        for value in values:
            value = str(value).strip()
            if value and value not in target:
                target.append(value)

    def _string_list(self, values: Any) -> list[str]:
        if values is None:
            return []
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            return []
        return [str(value).strip() for value in values if str(value).strip()]

    def _string_dict(self, values: Any) -> dict[str, str]:
        if not isinstance(values, dict):
            return {}
        return {str(key).strip(): str(value).strip() for key, value in values.items() if str(key).strip() and str(value).strip()}

    def _string_list_dict(self, values: Any) -> dict[str, list[str]]:
        if not isinstance(values, dict):
            return {}
        result: dict[str, list[str]] = {}
        for key, value in values.items():
            name = str(key).strip()
            items = self._string_list(value)
            if name and items:
                result[name] = items
        return result

    def _int_value(self, value: Any, *, default: int = 0) -> int:
        if isinstance(value, int):
            return value
        text = str(value).strip().lower()
        if text.startswith("d") and text[1:].isdigit():
            return int(text[1:])
        try:
            return int(text)
        except (TypeError, ValueError):
            return default

    def _set_scalar(self, target: Any, field_name: str, value: Any) -> None:
        text = str(value or "").strip()
        if text:
            setattr(target, field_name, text)

    def _looks_like_json(self, text: str) -> bool:
        stripped = text.strip()
        return stripped.startswith("{") and stripped.endswith("}")

    def _safe_id(self, value: str) -> str:
        cleaned = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value.strip())
        return cleaned or f"migration_secret_{self._now_compact()}"

    def _now_compact(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def import_payload_preview(payload: dict[str, Any]) -> dict[str, Any]:
    importer = CampaignChatLogImporter()
    normalized = importer.normalize_payload(payload)
    return {
        "summary": normalized.get("summary", ""),
        "confidence": normalized.get("confidence", 0.0),
        "counts": importer.import_counts(normalized),
        "warnings": normalized.get("warnings", []),
        "unresolved_questions": normalized.get("unresolved_questions", []),
        "world_updates": normalized.get("world_updates", {}),
    }
