from __future__ import annotations

from datetime import datetime, timezone

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.topic_memory_store import TopicMemoryStore
from fu_gm.components.world_state import WorldState
from fu_gm.models import ActionResolution, ActionType, MemoryVisibility


class NarrativeMemoryWriter:
    """Persist authoritative soft narration without owning rules state.

    HP, MP, clocks, equipment, and other hard state are committed by rule
    components before this writer runs.  This boundary only turns accepted
    narrative facts and GM-private notes into recallable topic memories.
    """

    def __init__(
        self,
        *,
        topics: TopicMemoryStore,
        world: WorldState,
        characters: CharacterManager,
        scenes: SceneManager,
    ) -> None:
        self.topics = topics
        self.world = world
        self.characters = characters
        self.scenes = scenes

    def write(
        self,
        resolution: ActionResolution,
        *,
        campaign_id: str,
        topics: TopicMemoryStore | None = None,
    ) -> None:
        if resolution.action.action_type != ActionType.NARRATE:
            return
        if not resolution.payload.get("narrative_authority"):
            return

        params = resolution.action.parameters
        summary = str(resolution.payload.get("summary") or "").strip()
        public_facts = self._string_list(
            params.get("public_facts") or params.get("world_facts") or params.get("facts")
        )
        private_notes = self._string_list(
            params.get("gm_private_notes") or params.get("private_notes")
        )
        subject_facts = self._dict_list(params.get("subject_facts"))
        npc_updates = self._dict_list(params.get("npc_updates"))
        relations = self._dict_list(params.get("relations"))
        persistent_changes = [
            str(item)
            for item in resolution.payload.get("persistent_changes", [])
            if str(item).strip()
        ]
        world_profile_updates = [
            str(item)
            for item in resolution.payload.get("world_profile_updates", [])
            if str(item).strip()
        ]

        public_lines: list[str] = []
        has_public_update = any(
            (public_facts, subject_facts, npc_updates, relations, persistent_changes, world_profile_updates)
        )
        if summary and has_public_update:
            public_lines.extend(["## 场景摘要", summary])
        if public_facts:
            public_lines.extend(["", "## 公开事实", *[f"- {fact}" for fact in public_facts]])

        public_subject_lines = []
        for item in subject_facts:
            subject = str(item.get("subject") or item.get("name") or "").strip()
            note = str(
                item.get("note") or item.get("fact") or item.get("description") or ""
            ).strip()
            if subject and note:
                public_subject_lines.append(f"- {subject}：{note}")
        if public_subject_lines:
            public_lines.extend(["", "## 对象事实", *public_subject_lines])

        public_npc_lines: list[str] = []
        private_npc_lines: list[str] = []
        for item in npc_updates:
            name = str(item.get("name") or item.get("npc") or "").strip()
            if not name:
                continue
            note = str(item.get("note") or item.get("memory") or item.get("event") or "").strip()
            public_identity = str(item.get("public_identity") or "").strip()
            role = str(item.get("role_in_story") or "").strip()
            if note or public_identity or role:
                public_npc_lines.append(
                    f"- {name}：" + "；".join(part for part in (public_identity, role, note) if part)
                )
            secret_parts: list[str] = []
            for key in ("core_drive", "secrets", "taboos", "custom_prompt"):
                value = item.get(key)
                if isinstance(value, list):
                    secret_parts.extend(str(part) for part in value if str(part).strip())
                elif str(value or "").strip():
                    secret_parts.append(str(value).strip())
            if secret_parts:
                private_npc_lines.append(f"- {name}：" + "；".join(secret_parts))
        if public_npc_lines:
            public_lines.extend(["", "## NPC 公开更新", *public_npc_lines])

        public_relation_lines: list[str] = []
        private_relation_lines: list[str] = []
        for item in relations:
            source = str(item.get("source") or "").strip()
            relation = str(item.get("relation") or item.get("type") or "").strip()
            target = str(item.get("target") or "").strip()
            if not source or not relation or not target:
                continue
            line = f"- {source} --{relation}--> {target}"
            visibility = str(item.get("visibility") or MemoryVisibility.PUBLIC.value)
            if visibility == MemoryVisibility.PRIVATE.value:
                private_relation_lines.append(line)
            else:
                public_relation_lines.append(line)
        if public_relation_lines:
            public_lines.extend(["", "## 公开关系", *public_relation_lines])
        if persistent_changes:
            public_lines.extend(["", "## 非数值持久变化", *[f"- {item}" for item in persistent_changes]])
        if world_profile_updates:
            public_lines.extend(["", "## 世界观补全", *[f"- {item}" for item in world_profile_updates]])

        private_lines: list[str] = []
        if private_notes:
            private_lines.extend(["## GM 私密暗线", *[f"- {note}" for note in private_notes]])
        if private_npc_lines:
            private_lines.extend(["", "## NPC 私密更新", *private_npc_lines])
        if private_relation_lines:
            private_lines.extend(["", "## 私密关系", *private_relation_lines])
        if not public_lines and not private_lines:
            return

        now = datetime.now(timezone.utc)
        scene = self.scenes.current_scene
        scene_title = scene.name if scene else "软叙事"
        timestamp = now.strftime("%Y%m%d_%H%M%S_%f")
        tags = ["narrate", "llm_soft_writeback"]
        if scene:
            tags.append(scene.scene_type.value)
        all_text = "\n".join(
            [summary, *public_facts, *world_profile_updates, *private_notes, *public_lines, *private_lines]
        )
        entities = self.world.extract_entities(
            all_text,
            extra_entities=[character.name for character in self.characters.all()],
        )

        target_topics = topics or self.topics
        if public_lines:
            target_topics.write_topic_memory(
                campaign_id,
                visibility=MemoryVisibility.PUBLIC,
                memory_type="narrative_writeback",
                title=f"{scene_title}：LLM 软叙事写回",
                description=summary or self._first_nonempty_line(public_lines),
                body="\n".join(public_lines),
                entities=entities,
                tags=tags,
                filename=f"narrate_{timestamp}",
                last_event_at=now.isoformat(),
                extra_frontmatter={"scene": scene_title},
            )
        if private_lines:
            target_topics.write_topic_memory(
                campaign_id,
                visibility=MemoryVisibility.PRIVATE,
                memory_type="narrative_private_writeback",
                title=f"{scene_title}：GM 私密软叙事写回",
                description=self._first_nonempty_line(private_lines),
                body="\n".join(private_lines),
                entities=entities,
                tags=[*tags, "private"],
                filename=f"narrate_{timestamp}_private",
                last_event_at=now.isoformat(),
                lock_level="draft",
                extra_frontmatter={"scene": scene_title},
            )

    @staticmethod
    def _first_nonempty_line(lines: list[str]) -> str:
        for line in lines:
            stripped = line.strip().lstrip("#- ").strip()
            if stripped:
                return stripped[:160]
        return ""

    @staticmethod
    def _string_list(value: object) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    @staticmethod
    def _dict_list(value: object) -> list[dict]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]
