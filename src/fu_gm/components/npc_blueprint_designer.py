from __future__ import annotations

import hashlib
import json
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from fu_gm.core_bestiary import (
    CORE_BESTIARY_ENTRIES,
    BestiaryEntry,
    bestiary_entry_by_name,
)
from fu_gm.components.bestiary_runtime_profiles import (
    ability_profiles_for_bestiary,
    attack_options_for_bestiary,
    attack_rules_for_bestiary,
)
from fu_gm.llm_client import ChatMessage
from fu_gm.models import (
    Affinity,
    NPCCombatBlueprint,
    NPCAttackProfile,
    NPCPersona,
    NPCSpellProfile,
    StatusEffect,
)
from fu_gm.npc_design_library import SPECIES_RULES, normalize_species


_SPECIES_ZH_TO_SLUG = {
    "野兽": "beast",
    "构装体": "construct",
    "恶魔": "demon",
    "元素": "elemental",
    "人型": "humanoid",
    "怪物": "monster",
    "植物": "plant",
    "不死族": "undead",
}


class NPCBlueprintDesigner:
    """Prepare private NPC combat sheets without expanding the core GM prompt.

    The optional model only chooses among a bounded set of legal bestiary
    candidates and proposes a tactical pattern.  All numbers are compiled from
    the core reference and validated locally, so an invented JSON field can
    never become authoritative state.
    """

    _SYSTEM_PROMPT = """你是《最终物语》NPC继承/改皮助手。
只能从候选模板中选择一个最贴合当前NPC概念、等级与场景职责的模板。
不要创造、修改或计算任何数值。输出一个JSON对象：
{
  "template_name": "候选模板准确名称",
  "selection_reason": "一句内部理由",
  "tactics": {
    "opening": "首轮倾向",
    "cycle": ["常规行动倾向1", "常规行动倾向2"],
    "crisis": "危机状态倾向",
    "telegraph": "强力行动前如何给玩家清晰预兆",
    "retreat": "何时撤退、投降或改变目标",
    "protect_policy": "always、priority或never",
    "protect_priority": ["优先保护的具体对象或角色特征，最多3项"]
  }
}
这些内容只给GM后台使用，不写玩家可见叙事。"""

    def __init__(
        self,
        world_state: Any,
        *,
        client: Any | None = None,
        model: str = "",
        current_scene_id: Callable[[], str] | None = None,
        max_workers: int = 2,
    ) -> None:
        self.world_state = world_state
        self.client = client
        self.model = str(model or "").strip()
        self.current_scene_id = current_scene_id or (lambda: "")
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="fu-gm-npc-design",
        )
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def submit(
        self,
        persona: NPCPersona,
        *,
        level: int,
        species: str = "",
        rank: str = "soldier",
        champion_value: int = 1,
        combat_side: str = "enemy",
        is_villain: bool = False,
        ultima_points: int = 0,
        scene_id: str = "",
        scene_context: dict[str, Any] | None = None,
        preferred_template: str = "",
        background: bool = True,
    ) -> dict[str, Any]:
        raw_species = str(species or "").strip()
        request = {
            "persona": persona,
            "level": max(5, min(60, int(level))),
            # An explicitly authored species remains authoritative. Planned
            # cast members may omit it; the isolated inheritance model then
            # chooses only among bounded core-bestiary candidates.
            "species": (
                normalize_species(raw_species).slug
                if raw_species
                else ""
            ),
            "rank": self._rank(rank),
            "champion_value": max(2, int(champion_value)) if self._rank(rank) == "champion" else 1,
            "combat_side": "ally" if str(combat_side).strip().lower() == "ally" else "enemy",
            "is_villain": bool(is_villain),
            "ultima_points": max(0, int(ultima_points)),
            "scene_id": str(scene_id or "").strip(),
            "scene_context": self._bounded_scene_context(scene_context),
            "preferred_template": str(preferred_template or "").strip(),
            "persona_revision": self.persona_revision(persona),
        }
        if request["is_villain"] and request["ultima_points"] < 1:
            request["ultima_points"] = 1
        if not request["is_villain"]:
            request["ultima_points"] = 0

        request_signature = self._request_signature(request)
        existing = self.world_state.npc_combat_blueprints.get(persona.name)
        if (
            existing is not None
            and existing.status == "ready"
            and existing.persona_revision == request["persona_revision"]
            and existing.requested_level == request["level"]
            and (
                existing.scene_id == request["scene_id"]
                or not existing.scene_id
            )
            and existing.rank == request["rank"]
            and existing.champion_value == request["champion_value"]
            and existing.combat_side == request["combat_side"]
            and existing.is_villain == request["is_villain"]
            and existing.ultima_points == request["ultima_points"]
            and (
                not request["species"]
                or existing.species == request["species"]
            )
            and (
                not request["preferred_template"]
                or existing.source_template == request["preferred_template"]
            )
        ):
            return self._job_view(
                {
                    "job_id": "",
                    "status": "ready",
                    "npc_name": persona.name,
                    "blueprint": existing,
                    "reused": True,
                }
            )

        with self._lock:
            pending = next(
                (
                    record
                    for record in self._jobs.values()
                    if record.get("request_signature") == request_signature
                    and record.get("status") in {"queued", "running"}
                ),
                None,
            )
            if pending is not None:
                reused = self._job_view(pending)
                reused["reused"] = True
                return reused

        job_id = f"npc-design-{uuid4().hex}"
        record: dict[str, Any] = {
            "job_id": job_id,
            "status": "queued" if background else "running",
            "npc_name": persona.name,
            "scene_id": request["scene_id"],
            "persona_revision": request["persona_revision"],
            "blueprint": None,
            "error": "",
            "reused": False,
            "request_signature": request_signature,
        }
        with self._lock:
            self._jobs[job_id] = record
        if background:
            future = self._executor.submit(self._run_job, job_id, request)
            with self._lock:
                record["future"] = future
            return self._job_view(record)

        self._run_job(job_id, request)
        return self.poll(job_id)

    def poll(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._jobs.get(str(job_id or "").strip())
            if record is None:
                return {
                    "job_id": str(job_id or ""),
                    "status": "missing",
                    "error": "NPC设计任务不存在。",
                }
            return self._job_view(record)

    def wait(self, job_id: str, timeout: float | None = None) -> dict[str, Any]:
        with self._lock:
            record = self._jobs.get(str(job_id or "").strip())
            future: Future[Any] | None = record.get("future") if record else None
        if future is not None:
            future.result(timeout=timeout)
        return self.poll(job_id)

    def prepare_sync(self, persona: NPCPersona, **kwargs: Any) -> NPCCombatBlueprint:
        result = self.submit(persona, background=False, **kwargs)
        if result.get("status") != "ready":
            raise ValueError(str(result.get("error") or "NPC规则档案设计失败。"))
        blueprint = self.world_state.npc_combat_blueprints.get(persona.name)
        if blueprint is None:
            raise ValueError("NPC规则档案没有写入私有状态。")
        return blueprint

    def _run_job(self, job_id: str, request: dict[str, Any]) -> None:
        with self._lock:
            record = self._jobs[job_id]
            record["status"] = "running"
        try:
            blueprint = self._design(request)
            current_persona = self.world_state.npc_personas.get(blueprint.npc_name)
            stale_reason = ""
            if current_persona is None:
                stale_reason = "NPC人格档案已不存在。"
            elif self.persona_revision(current_persona) != blueprint.persona_revision:
                stale_reason = "NPC人格档案在设计期间发生变化。"
            elif blueprint.scene_id:
                current_scene = str(self.current_scene_id() or "").strip()
                if current_scene != blueprint.scene_id:
                    stale_reason = "NPC所属场景在设计期间已经切换。"
            with self._lock:
                record = self._jobs[job_id]
                if stale_reason:
                    record["status"] = "stale"
                    record["error"] = stale_reason
                    return
                self.world_state.npc_combat_blueprints[blueprint.npc_name] = blueprint
                record["status"] = "ready"
                record["blueprint"] = blueprint
        except Exception as exc:  # pragma: no cover - exercised through failure contract tests
            with self._lock:
                record = self._jobs[job_id]
                record["status"] = "failed"
                record["error"] = str(exc)

    def _design(self, request: dict[str, Any]) -> NPCCombatBlueprint:
        persona: NPCPersona = request["persona"]
        candidates = self._candidate_entries(
            persona,
            requested_level=int(request["level"]),
            species=str(request["species"]),
            preferred_template=str(request["preferred_template"]),
        )
        if not candidates:
            raise ValueError("核心生物图鉴中没有可用于继承的候选模板。")
        selected = candidates[0]
        tactics: dict[str, Any] = {}
        validation_notes: list[str] = []
        if self.client is not None and self.model:
            try:
                proposal = self._model_proposal(persona, request, candidates)
                proposed_name = str(proposal.get("template_name") or "").strip()
                selected = next(
                    (entry for entry in candidates if entry.name == proposed_name),
                    selected,
                )
                tactics = self._validate_tactics(proposal.get("tactics"))
                reason = str(proposal.get("selection_reason") or "").strip()
                if reason:
                    validation_notes.append(f"模型选模理由：{reason}")
            except Exception as exc:
                validation_notes.append(f"独立选模模型不可用，已采用确定性继承：{exc}")
        if not tactics:
            tactics = self._default_tactics(selected)
        return self._compile_blueprint(
            selected,
            request=request,
            tactics=tactics,
            validation_notes=validation_notes,
        )

    def _candidate_entries(
        self,
        persona: NPCPersona,
        *,
        requested_level: int,
        species: str,
        preferred_template: str,
    ) -> list[BestiaryEntry]:
        if preferred_template:
            preferred = bestiary_entry_by_name(preferred_template)
            if preferred is None:
                raise ValueError(f"核心生物图鉴中没有模板【{preferred_template}】。")
            return [preferred]
        species_zh = SPECIES_RULES[species].name if species else ""
        text = " ".join(
            [
                persona.name,
                persona.public_identity,
                persona.role_in_story,
                persona.core_drive,
                persona.combat_style,
                *persona.traits,
            ]
        )

        def score(entry: BestiaryEntry) -> tuple[int, int, str]:
            overlap = sum(
                1
                for token in (*entry.typical_traits, entry.name)
                if token and token in text
            )
            return (abs(entry.level - requested_level), -overlap, entry.name)

        if species_zh:
            pool = [
                entry
                for entry in CORE_BESTIARY_ENTRIES
                if entry.species == species_zh
            ]
            return sorted(pool, key=score)[:5]

        # Keep an unauthored species decision genuinely open without sending
        # all 56 stat blocks to the model. One nearest candidate per species
        # prevents alphabetical ties from silently collapsing the choice.
        representatives: list[BestiaryEntry] = []
        for species_name in dict.fromkeys(
            entry.species for entry in CORE_BESTIARY_ENTRIES
        ):
            members = [
                entry
                for entry in CORE_BESTIARY_ENTRIES
                if entry.species == species_name
            ]
            representatives.append(sorted(members, key=score)[0])

        def cross_species_score(entry: BestiaryEntry) -> tuple[int, int, int, str]:
            distance, negative_overlap, name = score(entry)
            # When neither the authored profile nor the deterministic matcher
            # contains any species evidence, a named social NPC is safer as a
            # humanoid than whichever species happens to sort first in Chinese.
            # Explicit species and actual concept overlap still outrank this
            # final tie-breaker.
            default_species_priority = 0 if entry.species == "人型" else 1
            return (
                distance,
                negative_overlap,
                default_species_priority,
                name,
            )

        return sorted(representatives, key=cross_species_score)[:8]

    def _model_proposal(
        self,
        persona: NPCPersona,
        request: dict[str, Any],
        candidates: list[BestiaryEntry],
    ) -> dict[str, Any]:
        candidate_rows = [
            {
                "name": entry.name,
                "level": entry.level,
                "species": entry.species,
                "traits": list(entry.typical_traits),
                "attacks": [attack.summary for attack in entry.attacks],
                "spells": [spell.name for spell in entry.spells],
                "rules": list(entry.traits_rules),
            }
            for entry in candidates
        ]
        prompt = {
            "npc": {
                "name": persona.name,
                "public_identity": persona.public_identity,
                "role_in_story": persona.role_in_story,
                "core_drive": persona.core_drive,
                "combat_style": persona.combat_style,
                "traits": list(persona.traits),
                "active_goal": persona.active_goal,
            },
            "request": {
                "level": request["level"],
                "species": request["species"],
                "rank": request["rank"],
                "scene_id": request["scene_id"],
            },
            "current_environment": dict(request.get("scene_context") or {}),
            "candidates": candidate_rows,
        }
        raw = self.client.create_chat_completion(
            model=self.model,
            messages=[
                ChatMessage(
                    role="system",
                    content=self._SYSTEM_PROMPT,
                    cache_breakpoint=True,
                    cache_family="npc-blueprint-designer-v1",
                ),
                ChatMessage(role="user", content=json.dumps(prompt, ensure_ascii=False)),
            ],
            temperature=0.15,
            response_format={"type": "json_object"},
            max_tokens=900,
            operation="npc_blueprint_design",
        )
        return self._parse_json_object(raw)

    def _compile_blueprint(
        self,
        entry: BestiaryEntry,
        *,
        request: dict[str, Any],
        tactics: dict[str, Any],
        validation_notes: list[str],
    ) -> NPCCombatBlueprint:
        rank = str(request["rank"])
        champion_value = int(request["champion_value"])
        hp_multiplier = 2 if rank == "elite" else champion_value if rank == "champion" else 1
        mp_multiplier = 2 if rank == "champion" else 1
        initiative_bonus = 2 if rank == "elite" else champion_value if rank == "champion" else 0
        attacks = []
        for index, attack in enumerate(entry.attacks):
            attack_options = attack_options_for_bestiary(entry.name, attack.name)
            attack_rules = attack_rules_for_bestiary(entry.name, attack.name)
            status_options = list(attack_options.get("status_options_on_hit") or [])
            attacks.append(
                NPCAttackProfile(
                    attack_id=f"attack-{index + 1}",
                    name=attack.name,
                    attributes=list(attack.attributes),
                    damage_bonus=int(attack.damage_bonus),
                    damage_type=attack.damage_type,
                    accuracy_modifier=int(attack.accuracy_modifier),
                    range="ranged" if attack.range_type == "远程" else "melee",
                    targets_magic_defense="魔防" in attack.effect,
                    multi_attack=self._multi_attack(attack.effect),
                    status_effect_on_hit=(
                        None
                        if status_options
                        else self._status_from_effect(attack.effect)
                    ),
                    damage_type_options=list(
                        attack_options.get("damage_type_options") or []
                    ),
                    random_damage_types=list(
                        attack_options.get("random_damage_types") or []
                    ),
                    status_options_on_hit=status_options,
                    conditional_damage_bonus=int(
                        attack_rules.get("conditional_damage_bonus") or 0
                    ),
                    conditional_target_statuses=list(
                        attack_rules.get("conditional_target_statuses") or []
                    ),
                    conditional_any_target_status=bool(
                        attack_rules.get("conditional_any_target_status")
                    ),
                    bonus_if_previous_guard=int(
                        attack_rules.get("bonus_if_previous_guard") or 0
                    ),
                    recover_hp_fraction=float(
                        attack_rules.get("recover_hp_fraction") or 0.0
                    ),
                    recover_mp_on_hit=int(
                        attack_rules.get("recover_mp_on_hit") or 0
                    ),
                    target_mp_loss=int(attack_rules.get("target_mp_loss") or 0),
                    target_ip_loss=int(attack_rules.get("target_ip_loss") or 0),
                    self_hp_loss_if_all_miss=int(
                        attack_rules.get("self_hp_loss_if_all_miss") or 0
                    ),
                    effects=list(attack_rules.get("effects") or []),
                    notes=[attack.effect] if attack.effect else [],
                )
            )
        if not attacks:
            attacks = [
                NPCAttackProfile(
                    attack_id="attack-1",
                    name="基础攻击",
                    attributes=["DEX", "MIG"],
                    damage_bonus=5,
                    damage_type="physical",
                )
            ]
            validation_notes.append("继承模板没有结构化攻击，已补入规则书基础攻击。")
        defenses = {
            "physical": (
                int(entry.fixed_physical_defense)
                if entry.fixed_physical_defense is not None
                else int(entry.attributes["DEX"]) + int(entry.physical_defense_bonus)
            ),
            "magic": int(entry.attributes["INS"]) + int(entry.magic_defense_bonus),
        }
        species_slug = _SPECIES_ZH_TO_SLUG.get(entry.species, str(request["species"]))
        return NPCCombatBlueprint(
            blueprint_id=f"npc-blueprint-{uuid4().hex}",
            npc_name=request["persona"].name,
            status="ready",
            design_mode="inherit",
            source_template=entry.name,
            source_note=entry.source_note,
            scene_id=str(request["scene_id"]),
            persona_revision=str(request["persona_revision"]),
            requested_level=int(request["level"]),
            level=entry.level,
            species=species_slug,
            rank=rank,
            champion_value=champion_value,
            combat_side=str(request["combat_side"]),
            is_villain=bool(request["is_villain"]),
            ultima_points=int(request["ultima_points"]),
            traits=list(entry.typical_traits),
            attributes=dict(entry.attributes),
            max_hp=int(entry.max_hp) * hp_multiplier,
            crisis_threshold=(int(entry.max_hp) * hp_multiplier) // 2,
            max_mp=int(entry.max_mp) * mp_multiplier,
            initiative=int(entry.initiative) + initiative_bonus,
            defenses=defenses,
            affinities=dict(entry.affinities),
            status_immunities=list(entry.status_immunities),
            attacks=attacks,
            spells=[
                NPCSpellProfile(
                    name=spell.name,
                    rules_name=self._rules_spell_name(entry.name, spell.name),
                    attributes=list(spell.attributes),
                    mp_cost=int(spell.mp_cost),
                    target=spell.target,
                    duration=spell.duration,
                    effect=spell.effect,
                )
                for spell in entry.spells
            ],
            other_actions=list(entry.other_actions),
            trait_rules=list(entry.traits_rules),
            ability_profiles=ability_profiles_for_bestiary(entry.name),
            selected_skills=list(entry.traits_rules),
            tactics=tactics,
            validation_notes=[
                *validation_notes,
                (
                    f"按规则书继承【{entry.name}】并改皮为【{request['persona'].name}】；"
                    f"实际等级沿用模板的{entry.level}级。"
                ),
            ],
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def persona_revision(persona: NPCPersona) -> str:
        relevant = {
            key: value
            for key, value in asdict(persona).items()
            if key
            in {
                "name",
                "public_identity",
                "role_in_story",
                "core_drive",
                "combat_style",
                "traits",
                "known_skills",
                "combat_actions",
                "active_goal",
                "status",
            }
        }
        raw = json.dumps(relevant, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _request_signature(request: dict[str, Any]) -> str:
        payload = {
            key: request.get(key)
            for key in (
                "level",
                "species",
                "rank",
                "champion_value",
                "combat_side",
                "is_villain",
                "ultima_points",
                "scene_id",
                "scene_context",
                "preferred_template",
                "persona_revision",
            )
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _bounded_scene_context(
        value: dict[str, Any] | None,
    ) -> dict[str, object]:
        if not isinstance(value, dict):
            return {}

        def clean_text(key: str, limit: int = 240) -> str:
            return " ".join(str(value.get(key) or "").split())[:limit]

        visible = value.get("visible_elements")
        if not isinstance(visible, list):
            visible = []
        return {
            key: text
            for key, text in {
                "scene_name": clean_text("scene_name", 120),
                "location": clean_text("location", 120),
                "premise": clean_text("premise"),
                "current_pressure": clean_text("current_pressure"),
                "opposition_goal": clean_text("opposition_goal"),
                "npc_role_now": clean_text("npc_role_now"),
                "visible_elements": [
                    " ".join(str(item or "").split())[:120]
                    for item in visible[:4]
                    if str(item or "").strip()
                ],
            }.items()
            if text
        }

    @staticmethod
    def _rank(value: str) -> str:
        rank = str(value or "soldier").strip().lower()
        if rank not in {"soldier", "elite", "champion"}:
            raise ValueError("NPC战斗阶级必须是soldier、elite或champion。")
        return rank

    @staticmethod
    def _parse_json_object(raw: str) -> dict[str, Any]:
        text = str(raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                raise ValueError("独立NPC设计模型没有返回JSON对象。")
            value = json.loads(match.group(0))
        if not isinstance(value, dict):
            raise ValueError("独立NPC设计模型返回值不是JSON对象。")
        return value

    @staticmethod
    def _validate_tactics(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        cycle = value.get("cycle")
        if not isinstance(cycle, list):
            cycle = []
        protect_priority = value.get("protect_priority")
        if not isinstance(protect_priority, list):
            protect_priority = []
        return {
            "opening": str(value.get("opening") or "").strip(),
            "cycle": [str(item).strip() for item in cycle[:4] if str(item).strip()],
            "crisis": str(value.get("crisis") or "").strip(),
            "telegraph": str(value.get("telegraph") or "").strip(),
            "retreat": str(value.get("retreat") or "").strip(),
            "protect_policy": (
                str(value.get("protect_policy") or "always").strip().lower()
                if str(value.get("protect_policy") or "always").strip().lower()
                in {"always", "priority", "never"}
                else "always"
            ),
            "protect_priority": [
                str(item).strip()
                for item in protect_priority[:3]
                if str(item).strip()
            ],
        }

    @staticmethod
    def _default_tactics(entry: BestiaryEntry) -> dict[str, Any]:
        attack_names = [attack.name for attack in entry.attacks]
        spell_names = [spell.name for spell in entry.spells]
        cycle = [*attack_names, *spell_names][:4]
        return {
            "opening": cycle[0] if cycle else "先观察英雄的阵形，再执行最符合其目标的行动",
            "cycle": cycle or ["基础攻击"],
            "crisis": "优先触发危机效果；若无危机效果，则改变策略而不是机械重复最强招式",
            "telegraph": "强力攻击前用姿态、蓄力、环境变化或明确台词给出可利用的预兆",
            "retreat": "目标已无法实现、继续战斗失去意义或符合人格时撤退或投降",
            "protect_policy": "always",
            "protect_priority": [],
        }

    @staticmethod
    def _multi_attack(effect: str) -> int:
        match = re.search(r"多重攻击\s*[（(](\d+)[)）]", str(effect or ""))
        return max(1, min(3, int(match.group(1)))) if match else 1

    @staticmethod
    def _status_from_effect(effect: str) -> StatusEffect | None:
        text = str(effect or "")
        mapping = {
            "迟缓": StatusEffect.SLOW,
            "眩晕": StatusEffect.DAZED,
            "虚弱": StatusEffect.WEAKENED,
            "动摇": StatusEffect.SHAKEN,
            "激怒": StatusEffect.ENRAGED,
            "中毒": StatusEffect.POISONED,
        }
        return next((status for label, status in mapping.items() if label in text), None)

    @staticmethod
    def _rules_spell_name(template_name: str, spell_name: str) -> str:
        if template_name == "骷髅法师" and spell_name == "影袭":
            return "骷髅影袭"
        return spell_name

    @staticmethod
    def _job_view(record: dict[str, Any]) -> dict[str, Any]:
        blueprint = record.get("blueprint")
        return {
            "job_id": str(record.get("job_id") or ""),
            "status": str(record.get("status") or "unknown"),
            "npc_name": str(record.get("npc_name") or ""),
            "scene_id": str(record.get("scene_id") or ""),
            "blueprint_id": str(getattr(blueprint, "blueprint_id", "") or ""),
            "source_template": str(getattr(blueprint, "source_template", "") or ""),
            "error": str(record.get("error") or ""),
            "reused": bool(record.get("reused")),
        }


__all__ = ["NPCBlueprintDesigner"]
