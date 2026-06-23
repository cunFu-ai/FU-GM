from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.dungeon_manager import DungeonManager
from fu_gm.components.economy_manager import EconomyManager
from fu_gm.components.gadget_manager import TinkererGadgetManager
from fu_gm.components.project_manager import ProjectManager
from fu_gm.components.ritual_manager import RitualManager
from fu_gm.components.rules_engine import RulesEngine, resolve_affinity
from fu_gm.components.trigger_manager import TriggerManager
from fu_gm.components.world_state import WorldState
from fu_gm.models import (
    Action,
    ActionResolution,
    ActionType,
    Affinity,
    Clock,
    ClockChange,
    ConflictEvent,
    EffectTiming,
    EnemyRank,
    InventoryUseResult,
    MemoryVisibility,
    PersistentChangeType,
    ProjectUse,
    ResourceChange,
    RitualDiscipline,
    RitualPotency,
    RitualScope,
    SpellEffectType,
    SpellTarget,
    StatusEffect,
    TimedEffect,
    TinkererGadgetResult,
)
from fu_gm.skill_library import (
    CLASS_SKILL_REFERENCES,
    normalize_skill_reference_name,
    skill_implementation_coverage,
    skill_rank,
)
from fu_gm.spellbook import get_spell_definition


class ActionInterceptor:
    """在叙事前执行硬规则的中间拦截层。"""

    CLASS_NAMES = {reference.class_name for reference in CLASS_SKILL_REFERENCES if reference.class_name}

    ARCANUM_ALIASES = {
        "锻造": "锻造",
        "锻造的阿卡纳": "锻造",
        "熔炉": "锻造",
        "熔炉奥灵": "锻造",
        "forge": "锻造",
        "霜": "霜",
        "霜的阿卡纳": "霜",
        "寒霜": "霜",
        "寒霜奥灵": "霜",
        "frost": "霜",
        "门": "门",
        "门的阿卡纳": "门",
        "门径": "门",
        "门径奥灵": "门",
        "gate": "门",
        "魔典": "魔典",
        "魔典的阿卡纳": "魔典",
        "魔典奥灵": "魔典",
        "grimoire": "魔典",
        "橡树": "橡树",
        "橡树的阿卡纳": "橡树",
        "橡树奥灵": "橡树",
        "oak": "橡树",
        "天空": "天空",
        "天空的阿卡纳": "天空",
        "天空奥灵": "天空",
        "sky": "天空",
        "剑": "剑",
        "剑的阿卡纳": "剑",
        "剑之奥灵": "剑",
        "sword": "剑",
        "塔": "塔",
        "塔的阿卡纳": "塔",
        "高塔": "塔",
        "高塔奥灵": "塔",
        "tower": "塔",
        "轮": "轮",
        "轮的阿卡纳": "轮",
        "轮之奥灵": "轮",
        "wheel": "轮",
    }

    ARCANUM_DISPLAY_NAMES = {
        "锻造": "熔炉奥灵",
        "霜": "寒霜奥灵",
        "门": "门径奥灵",
        "魔典": "魔典奥灵",
        "橡树": "橡树奥灵",
        "天空": "天空奥灵",
        "剑": "剑之奥灵",
        "塔": "高塔奥灵",
        "轮": "轮之奥灵",
    }

    def __init__(
        self,
        rules_engine: RulesEngine,
        character_manager: CharacterManager,
        clock_manager: ClockManager,
        conflict_manager: ConflictManager,
        world_state: WorldState,
        ritual_manager: RitualManager | None = None,
        project_manager: ProjectManager | None = None,
        gadget_manager: TinkererGadgetManager | None = None,
        economy_manager: EconomyManager | None = None,
        dungeon_manager: DungeonManager | None = None,
        trigger_manager: TriggerManager | None = None,
    ) -> None:
        self.rules_engine = rules_engine
        self.character_manager = character_manager
        self.clock_manager = clock_manager
        self.conflict_manager = conflict_manager
        self.world_state = world_state
        self.ritual_manager = ritual_manager
        self.project_manager = project_manager
        self.gadget_manager = gadget_manager or TinkererGadgetManager(
            rules_engine,
            character_manager,
            conflict_manager,
        )
        self.economy_manager = economy_manager or EconomyManager(character_manager, world_state, rules_engine)
        self.dungeon_manager = dungeon_manager
        self.trigger_manager = trigger_manager or TriggerManager(character_manager)
        self.pending_rolls: dict[str, object] = {}
        self.pending_advantages: dict[str, int] = {}

    def _int_parameter(
        self,
        parameters: dict[str, Any],
        key: str,
        default: int = 0,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        raw_value = parameters.get(key, default)
        if raw_value is None or raw_value == "":
            value = default
        else:
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                value = default
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _first_int_parameter(
        self,
        parameters: dict[str, Any],
        keys: list[str],
        default: int = 0,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        for key in keys:
            raw_value = parameters.get(key)
            if raw_value is not None and raw_value != "":
                return self._int_parameter(parameters, key, default, minimum=minimum, maximum=maximum)
        value = default
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _coerce_misrouted_class_skill_action(self, action: Action) -> Action:
        skill_name = normalize_skill_reference_name(str(action.parameters.get("skill_name") or ""))
        if skill_name not in self.CLASS_NAMES:
            return action

        text = " ".join(
            str(action.parameters.get(key) or "")
            for key in ("summary", "reasoning", "in_mind_reply", "description", "intent")
        )
        status = str(action.parameters.get("status_effect") or "")
        has_hinder_signal = bool(status) or any(
            token in text
            for token in ("妨碍", "干扰", "佯攻", "分散注意", "动摇", "迟缓", "虚弱", "眩晕", "中毒", "施加异常")
        )
        if not has_hinder_signal:
            return action

        parameters = dict(action.parameters)
        parameters.pop("skill_name", None)
        parameters.setdefault("attributes", ["INS", "WLP"])
        parameters.setdefault("target_number", 10)
        if not parameters.get("status_effect"):
            if "迟缓" in text:
                parameters["status_effect"] = "slow"
            elif "虚弱" in text:
                parameters["status_effect"] = "weakened"
            elif "眩晕" in text:
                parameters["status_effect"] = "dazed"
            elif "中毒" in text:
                parameters["status_effect"] = "poisoned"
            else:
                parameters["status_effect"] = "shaken"
        parameters["reasoning"] = (
            parameters.get("reasoning")
            or f"玩家以{skill_name}的风格执行基础妨碍行动；职业名不视为技能名。"
        )
        return Action(ActionType.HINDER, parameters)

    def resolve(self, action: Action) -> ActionResolution:
        if action.action_type == ActionType.ATTACK:
            return self._finalize_resolution(self._resolve_attack(action))
        if action.action_type == ActionType.SPELL:
            return self._finalize_resolution(self._resolve_spell(action))
        if action.action_type == ActionType.GUARD:
            return self._finalize_resolution(self._resolve_guard(action))
        if action.action_type == ActionType.EQUIP:
            return self._finalize_resolution(self._resolve_equip(action))
        if action.action_type == ActionType.HINDER:
            return self._finalize_resolution(self._resolve_hinder(action))
        if action.action_type == ActionType.INVESTIGATE:
            return self._finalize_resolution(self._resolve_investigate(action))
        if action.action_type == ActionType.OBJECTIVE:
            return self._finalize_resolution(self._resolve_objective(action))
        if action.action_type == ActionType.SKILL:
            coerced = self._coerce_misrouted_class_skill_action(action)
            if coerced is not action:
                return self._finalize_resolution(self._resolve_hinder(coerced))
            return self._finalize_resolution(self._resolve_skill(action))
        if action.action_type == ActionType.USE_INVENTORY:
            return self._finalize_resolution(self._resolve_use_inventory(action))
        if action.action_type == ActionType.TINKERER_GADGET:
            return self._finalize_resolution(self._resolve_tinkerer_gadget(action))
        if action.action_type == ActionType.SHOP:
            return self._finalize_resolution(self._resolve_shop(action))
        if action.action_type == ActionType.OPEN_CHEST:
            return self._finalize_resolution(self._resolve_open_chest(action))
        if action.action_type == ActionType.AWARD_REWARD:
            return self._finalize_resolution(self._resolve_award_reward(action))
        if action.action_type == ActionType.EXPLORE_DUNGEON:
            return self._finalize_resolution(self._resolve_explore_dungeon(action))
        if action.action_type == ActionType.NEXT_TURN:
            return self._finalize_resolution(self._resolve_next_turn(action))
        if action.action_type == ActionType.PLAN_RITUAL:
            return self._finalize_resolution(self._resolve_plan_ritual(action))
        if action.action_type == ActionType.CONTRIBUTE_RITUAL:
            return self._finalize_resolution(self._resolve_contribute_ritual(action))
        if action.action_type == ActionType.CAST_RITUAL:
            return self._finalize_resolution(self._resolve_cast_ritual(action))
        if action.action_type == ActionType.START_PROJECT:
            return self._finalize_resolution(self._resolve_start_project(action))
        if action.action_type == ActionType.HIRE_PROJECT_HELPERS:
            return self._finalize_resolution(self._resolve_hire_project_helpers(action))
        if action.action_type == ActionType.WORK_PROJECT:
            return self._finalize_resolution(self._resolve_work_project(action))
        if action.action_type == ActionType.NPCACT:
            return self._finalize_resolution(self._resolve_npc_act(action))
        if action.action_type == ActionType.NARRATE:
            return self._finalize_resolution(self._resolve_narrate(action))
        if action.action_type == ActionType.REQUEST_ROLL:
            return self._finalize_resolution(self._resolve_roll(action))
        if action.action_type == ActionType.MODIFY_RESOURCE:
            return self._finalize_resolution(self._resolve_resource(action))
        if action.action_type == ActionType.ADVANCE_CLOCK:
            return self._finalize_resolution(self._resolve_clock(action))
        if action.action_type == ActionType.INVOKE_TRAIT:
            return self._finalize_resolution(self._resolve_invoke_trait(action))
        if action.action_type == ActionType.INVOKE_BOND:
            return self._finalize_resolution(self._resolve_invoke_bond(action))
        if action.action_type == ActionType.TRIGGER_OPPORTUNITY:
            return self._finalize_resolution(self._resolve_opportunity(action))
        if action.action_type == ActionType.ACCEPT_STORY_CHANGE:
            return self._finalize_resolution(self._resolve_story_change(action))
        if action.action_type == ActionType.START_CONFLICT:
            return self._finalize_resolution(self._resolve_start_conflict(action))
        if action.action_type == ActionType.MANAGE_BOND:
            return self._finalize_resolution(self._resolve_manage_bond(action))
        if action.action_type == ActionType.SELL_ITEM:
            return self._finalize_resolution(self._resolve_sell_item(action))
        if action.action_type == ActionType.PLAYER_VS_PLAYER:
            return self._finalize_resolution(self._resolve_player_vs_player(action))
        if action.action_type == ActionType.ABSENT_PLAYER:
            return self._finalize_resolution(self._resolve_absent_player(action))
        return self._finalize_resolution(ActionResolution(action=action, rules_text="该动作不需要执行硬规则。", payload={}))

    def _finalize_resolution(self, resolution: ActionResolution) -> ActionResolution:
        if not self.conflict_manager.state.active:
            return resolution
        resolution.payload.setdefault("turn_board", self.conflict_manager.format_turn_board())
        resolution.payload.setdefault("combat_log", self.conflict_manager.format_combat_log())
        action = resolution.action
        loggable_actions = {
            ActionType.ATTACK,
            ActionType.SPELL,
            ActionType.GUARD,
            ActionType.EQUIP,
            ActionType.HINDER,
            ActionType.INVESTIGATE,
            ActionType.OBJECTIVE,
            ActionType.SKILL,
            ActionType.USE_INVENTORY,
            ActionType.TINKERER_GADGET,
            ActionType.NPCACT,
            ActionType.NARRATE,
            ActionType.REQUEST_ROLL,
            ActionType.MODIFY_RESOURCE,
            ActionType.ADVANCE_CLOCK,
            ActionType.INVOKE_TRAIT,
            ActionType.INVOKE_BOND,
            ActionType.TRIGGER_OPPORTUNITY,
            ActionType.START_CONFLICT,
            ActionType.MANAGE_BOND,
            ActionType.SELL_ITEM,
            ActionType.PLAYER_VS_PLAYER,
            ActionType.ABSENT_PLAYER,
        }
        if action.action_type in loggable_actions and resolution.rules_text:
            actor = action.parameters.get("actor") or action.parameters.get("target") or self.conflict_manager.state.current_actor() or "system"
            event_type = action.parameters.get("npc_action_type") if action.action_type == ActionType.NPCACT else action.action_type.value
            self.conflict_manager.record_log(str(actor), str(event_type), resolution.rules_text)
            resolution.payload["combat_log"] = self.conflict_manager.format_combat_log()
            resolution.payload["turn_board"] = self.conflict_manager.format_turn_board()
        return resolution

    def _target_name(self, action: Action, default: str = "当前目标") -> str:
        """从 LLM 动作中宽松提取目标名，兼容场景物件与线索目标。"""

        raw_target = (
            action.parameters.get("target")
            or action.parameters.get("target_name")
            or action.parameters.get("subject")
            or action.parameters.get("scene_object")
        )
        if isinstance(raw_target, list):
            raw_target = raw_target[0] if raw_target else None
        text = str(raw_target or "").strip()
        return text or default

    def _resolve_scene_target_skill(
        self,
        action: Action,
        skill_name: str,
        actor,
        target_name: str,
        *,
        summary: str | None = None,
    ) -> ActionResolution:
        """当技能被用于机关、入口、歌声等场景目标时，记录叙事效果而不做角色数值结算。"""

        note = summary or f"{actor.name} 以【{skill_name}】影响场景目标【{target_name}】。"
        self.world_state.add_memory(note)
        self.world_state.remember_subject_fact(target_name, f"被 {actor.name} 的【{skill_name}】影响。")
        return ActionResolution(
            action=action,
            rules_text=f"{actor.name} 使用【{skill_name}】影响了【{target_name}】；该目标不是已建档角色，已作为场景效果记录。",
            payload={"skill_name": skill_name, "scene_object": target_name, "scene_skill": True},
        )

    def _resolve_narrate(self, action: Action) -> ActionResolution:
        """LLM 的软叙事通道。

        这里不做任何骰子、伤害、资源或命刻结算；只把 LLM 已决定的非数值事实写入世界记忆。
        需要硬规则的内容仍应走 Attack/RequestRoll/AdvanceClock/ModifyResource 等专用动作。
        """

        params = action.parameters
        summary = str(
            params.get("summary")
            or params.get("narration")
            or params.get("scene_text")
            or params.get("in_mind_reply")
            or "场景继续推进。"
        ).strip()
        if summary:
            self.world_state.record_memory_event(
                summary,
                kind="llm_narration",
                visibility=MemoryVisibility.PUBLIC,
                tags=["narrative_authority"],
                source="narrate",
            )

        public_facts: list[str] = []
        for fact in self._string_list(params.get("public_facts") or params.get("world_facts") or params.get("facts")):
            self.world_state.record_memory_event(
                fact,
                kind="llm_public_fact",
                visibility=MemoryVisibility.PUBLIC,
                tags=["narrative_authority"],
                source="narrate",
            )
            public_facts.append(fact)

        private_notes: list[str] = []
        for note in self._string_list(params.get("gm_private_notes") or params.get("private_notes")):
            self.world_state.record_memory_event(
                note,
                kind="llm_private_note",
                visibility=MemoryVisibility.PRIVATE,
                tags=["narrative_authority"],
                source="narrate",
            )
            private_notes.append(note)

        subject_facts: list[dict[str, str]] = []
        for item in self._dict_list(params.get("subject_facts")):
            subject = str(item.get("subject") or item.get("name") or "").strip()
            note = str(item.get("note") or item.get("fact") or item.get("description") or "").strip()
            if not subject or not note:
                continue
            self.world_state.remember_subject_fact(subject, note)
            subject_facts.append({"subject": subject, "note": note})

        npc_updates: list[dict[str, str]] = []
        for item in self._dict_list(params.get("npc_updates")):
            name = str(item.get("name") or item.get("npc") or "").strip()
            if not name:
                continue
            if any(
                item.get(key)
                for key in (
                    "public_identity",
                    "role_in_story",
                    "core_drive",
                    "manner",
                    "speech_style",
                    "combat_style",
                    "goals",
                    "taboos",
                    "secrets",
                    "custom_prompt",
                )
            ):
                self.world_state.ensure_npc_persona(
                    name,
                    public_identity=str(item.get("public_identity") or name),
                    role_in_story=str(item.get("role_in_story") or "由 LLM 叙事中登场的 NPC"),
                    core_drive=str(item.get("core_drive") or "根据当前故事目标行动"),
                    manner=str(item.get("manner") or ""),
                    speech_style=str(item.get("speech_style") or ""),
                    combat_style=str(item.get("combat_style") or ""),
                    first_scene=str(item.get("first_scene") or params.get("scene") or ""),
                    goals=self._string_list(item.get("goals")),
                    taboos=self._string_list(item.get("taboos")),
                    secrets=self._string_list(item.get("secrets")),
                    custom_prompt=str(item.get("custom_prompt") or ""),
                )
            note = str(item.get("note") or item.get("memory") or item.get("event") or "").strip()
            if note:
                self.world_state.remember_npc_event(name, note)
            npc_updates.append({"name": name, "note": note})

        relations: list[dict[str, str]] = []
        for item in self._dict_list(params.get("relations")):
            source = str(item.get("source") or "").strip()
            relation = str(item.get("relation") or item.get("type") or "").strip()
            target = str(item.get("target") or "").strip()
            if not source or not relation or not target:
                continue
            self.world_state.record_relation(
                source,
                relation,
                target,
                visibility=item.get("visibility", MemoryVisibility.PUBLIC),
                evidence=str(item.get("evidence") or summary),
            )
            relations.append({"source": source, "relation": relation, "target": target})

        persistent_changes: list[str] = []
        skipped_changes: list[str] = []
        for item in self._dict_list(params.get("persistent_changes")):
            try:
                change_type = self._persistent_change_type(
                    item.get("change_type") or item.get("type"),
                    fallback=PersistentChangeType.WORLD_FACT,
                )
                if change_type is None:
                    continue
                name = str(item.get("name") or item.get("title") or "").strip()
                description = str(item.get("description") or item.get("effect") or item.get("fact") or "").strip()
                if not name or not description:
                    continue
                if change_type == PersistentChangeType.FACILITY:
                    change = self.world_state.record_location_facility(
                        name=name,
                        description=description,
                        source=str(item.get("source") or "LLM 叙事裁量"),
                        location=str(item.get("location") or params.get("location") or ""),
                        tags=self._string_list(item.get("tags")),
                    )
                elif change_type in {
                    PersistentChangeType.EQUIPMENT,
                    PersistentChangeType.CONSUMABLE,
                    PersistentChangeType.TRANSPORT,
                }:
                    change = self.world_state.record_created_asset(
                        change_type=change_type,
                        name=name,
                        description=description,
                        source=str(item.get("source") or "LLM 叙事裁量"),
                        owner=str(item.get("owner") or "小队"),
                        location=str(item.get("location") or params.get("location") or ""),
                        tags=self._string_list(item.get("tags")),
                    )
                else:
                    change = self.world_state.record_world_fact(
                        name=name,
                        description=description,
                        source=str(item.get("source") or "LLM 叙事裁量"),
                        location=str(item.get("location") or params.get("location") or ""),
                        tags=self._string_list(item.get("tags")),
                    )
                persistent_changes.append(self.world_state.format_persistent_change(change))
            except Exception as exc:
                skipped_changes.append(str(exc))

        world_profile_updates: list[str] = []
        raw_profile_updates = params.get("world_profile_updates") or params.get("world_updates")
        if isinstance(raw_profile_updates, dict):
            world_profile_updates = self.world_state.apply_world_profile_updates(
                raw_profile_updates,
                source="narrate",
            )

        rules_text = "LLM 叙事裁量已记录；未执行任何硬数值结算。"
        if public_facts or private_notes or npc_updates or subject_facts or relations or persistent_changes or world_profile_updates:
            rules_text += (
                f" 写入公开事实 {len(public_facts)} 条、私密笔记 {len(private_notes)} 条、"
                f"NPC 更新 {len(npc_updates)} 条、对象事实 {len(subject_facts)} 条、"
                f"关系 {len(relations)} 条、持久变化 {len(persistent_changes)} 条、"
                f"世界观补全 {len(world_profile_updates)} 条。"
            )
        return ActionResolution(
            action=action,
            rules_text=rules_text,
            payload={
                "narrative_authority": True,
                "summary": summary,
                "public_facts": public_facts,
                "gm_private_notes_count": len(private_notes),
                "npc_updates": npc_updates,
                "subject_facts": subject_facts,
                "relations": relations,
                "persistent_changes": persistent_changes,
                "world_profile_updates": world_profile_updates,
                "skipped_changes": skipped_changes,
            },
        )

    def _resolve_plan_ritual(self, action: Action) -> ActionResolution:
        manager = self._require_ritual_manager()
        caster = action.parameters.get("caster") or action.parameters.get("actor")
        ritual_name = self._ritual_name(action.parameters["name"])
        plan = manager.plan_ritual(
            caster=caster,
            name=ritual_name,
            discipline=self._ritual_discipline(action.parameters.get("discipline", "ritualism")),
            potency=self._ritual_potency(action.parameters.get("potency", "minor")),
            scope=self._ritual_scope(action.parameters.get("scope", "individual")),
            effect=action.parameters.get("effect", ""),
            attributes=action.parameters.get("attributes"),
            rare_material=action.parameters.get("rare_material", ""),
            forbidden_tags=action.parameters.get("forbidden_tags", []),
            enforce_permission=action.parameters.get("enforce_permission", True),
        )
        clock_change = None
        should_track_clock = action.parameters.get("track_clock", True)
        if should_track_clock or action.parameters.get("start_conflict_clock", False) or action.parameters.get("conflict_ritual", False):
            manager.start_conflict_ritual(plan)
            clock = self.clock_manager.get(plan.clock_name)
            clock_change = ClockChange(
                clock_name=clock.name,
                before=0,
                after=clock.current,
                delta=0,
                max_segments=clock.max_segments,
                reason="创建冲突仪式命刻。",
            )
        self.world_state.add_memory(
            f"仪式计划：{plan.caster} 准备【{plan.name}】，消耗 {plan.mp_cost} MP，DL {plan.target_number}。"
        )
        rules_text = (
            f"{plan.caster} 计划仪式【{plan.name}】：{self._ritual_potency_text(plan.potency)}效力、"
            f"{self._ritual_scope_text(plan.scope)}范围，需要 {plan.mp_cost} MP，DL {plan.target_number}。"
        )
        payload = {"ritual_plan": plan}
        if clock_change is not None:
            payload["clock_change"] = clock_change
            rules_text += f" 已创建命刻【{plan.clock_name}】{plan.clock_segments} 格。"
        return ActionResolution(action=action, rules_text=rules_text, payload=payload)

    def _resolve_contribute_ritual(self, action: Action) -> ActionResolution:
        manager = self._require_ritual_manager()
        clock_name = self._ritual_clock_name(action.parameters.get("clock_name") or action.parameters.get("name", ""))
        actor = action.parameters.get("actor") or action.parameters.get("caster")
        explicit_target_number = "target_number" in action.parameters
        outcome, change = manager.contribute_to_ritual(
            clock_name,
            actor=actor,
            attributes=action.parameters.get("attributes"),
            target_number=self._int_parameter(action.parameters, "target_number", 10, minimum=0)
            if explicit_target_number
            else None,
            modifier=self._int_parameter(action.parameters, "modifier", 0),
            direction=action.parameters.get("direction", 1),
            spend_critical_opportunity=bool(action.parameters.get("spend_critical_opportunity_on_clock", False)),
            reason=action.parameters.get("reasoning", "推进仪式命刻"),
        )
        self.world_state.add_memory(
            f"{actor} 推进仪式【{clock_name}】：{outcome.total} vs {outcome.target_number}，命刻 {change.after}/{change.max_segments}。"
        )
        return ActionResolution(
            action=action,
            rules_text=(
                f"{actor} 尝试推进仪式【{clock_name}】：{outcome.total} vs {outcome.target_number}，"
                f"命刻 {change.before}/{change.max_segments} -> {change.after}/{change.max_segments}。"
            ),
            payload={"roll": outcome, "clock_change": change},
        )

    def _resolve_cast_ritual(self, action: Action) -> ActionResolution:
        manager = self._require_ritual_manager()
        if action.parameters.get("clock_name"):
            plan_or_clock_name = self._ritual_clock_name(action.parameters["clock_name"])
        elif action.parameters.get("name") and self._ritual_clock_name(action.parameters["name"]) in manager.active_rituals:
            plan_or_clock_name = self._ritual_clock_name(action.parameters["name"])
        else:
            caster = action.parameters.get("caster") or action.parameters.get("actor")
            plan_or_clock_name = manager.plan_ritual(
                caster=caster,
                name=self._ritual_name(action.parameters["name"]),
                discipline=self._ritual_discipline(action.parameters.get("discipline", "ritualism")),
                potency=self._ritual_potency(action.parameters.get("potency", "minor")),
                scope=self._ritual_scope(action.parameters.get("scope", "individual")),
                effect=action.parameters.get("effect", ""),
                attributes=action.parameters.get("attributes"),
                rare_material=action.parameters.get("rare_material", ""),
                forbidden_tags=action.parameters.get("forbidden_tags", []),
                enforce_permission=action.parameters.get("enforce_permission", True),
            )
        if action.parameters.get("require_completed_clock", False):
            plan = manager._resolve_plan(plan_or_clock_name)
            if self.clock_manager.exists(plan.clock_name):
                clock = self.clock_manager.get(plan.clock_name)
                if clock.current < clock.max_segments:
                    remaining = clock.max_segments - clock.current
                    return ActionResolution(
                        action=action,
                        rules_text=(
                            f"仪式【{plan.name}】还不能完成：命刻【{plan.clock_name}】当前 "
                            f"{clock.current}/{clock.max_segments}，还差 {remaining} 格。"
                            "这不是行动失败；需要继续推进仪式命刻。"
                        ),
                        payload={"ritual_plan": plan, "clock": clock, "ritual_waiting": True},
                    )
        result = manager.cast_ritual(
            plan_or_clock_name,
            catastrophe=action.parameters.get(
                "catastrophe",
                "仪式失控，GM 应让效果以危险、代价或威胁命刻的方式扭曲。",
            ),
            require_completed_clock=action.parameters.get("require_completed_clock", False),
        )
        persistence = None
        if result.success:
            persistence = self._persist_ritual_result(action, result)
        self.world_state.add_memory(result.summary)
        payload = {
            "ritual_result": result,
            "ritual_plan": result.plan,
            "roll": result.roll,
            "resource_change": result.mp_change,
        }
        if persistence is not None:
            payload["persistence"] = persistence
        return ActionResolution(
            action=action,
            rules_text=result.summary,
            payload=payload,
        )

    def _resolve_start_project(self, action: Action) -> ActionResolution:
        manager = self._require_project_manager()
        inventor = action.parameters.get("inventor") or action.parameters.get("actor")
        project = manager.start_project(
            inventor=inventor,
            name=action.parameters["name"],
            potency=self._ritual_potency(action.parameters.get("potency", "minor")),
            scope=self._ritual_scope(action.parameters.get("scope", "individual")),
            use=self._project_use(action.parameters.get("use", "consumable")),
            effect=action.parameters.get("effect", ""),
            output_type=self._persistent_change_type(
                action.parameters.get("output_type"),
                fallback=None,
            ),
            owner=action.parameters.get("owner", ""),
            location=action.parameters.get("location", ""),
            flaw=action.parameters.get("flaw", ""),
            special_materials=action.parameters.get("special_materials", []),
            material_credit=action.parameters.get("material_credit", 0),
            enforce_permission=action.parameters.get("enforce_permission", True),
        )
        self.world_state.add_memory(
            f"项目启动：{inventor} 开始制作【{project.name}】，成本 {project.material_cost}Z，进度 {project.required_progress}。"
        )
        return ActionResolution(
            action=action,
            rules_text=(
                f"{inventor} 启动项目【{project.name}】：总成本 {project.material_cost}Z，"
                f"需要进度 {project.required_progress}，当前 {project.current_progress}/{project.required_progress}。"
            ),
            payload={"project": project},
        )

    def _resolve_hire_project_helpers(self, action: Action) -> ActionResolution:
        manager = self._require_project_manager()
        project_name = action.parameters["project_name"]
        payer = action.parameters.get("payer") or action.parameters.get("actor")
        count = action.parameters.get("count", 1)
        change = manager.hire_helpers(project_name, payer=payer, count=count)
        self.world_state.add_memory(f"项目【{project_name}】雇佣帮手 {count} 名。")
        return ActionResolution(
            action=action,
            rules_text=f"{payer} 为项目【{project_name}】雇佣 {count} 名帮手，支付 {abs(change.amount)}Z。",
            payload={"resource_change": change, "project": manager.projects[project_name]},
        )

    def _resolve_work_project(self, action: Action) -> ActionResolution:
        manager = self._require_project_manager()
        project_name = action.parameters["project_name"]
        workers = action.parameters.get("workers") or [action.parameters.get("actor")]
        workers = [worker for worker in workers if worker]
        result = manager.work_on_project(project_name, workers, days=action.parameters.get("days", 1))
        self.world_state.add_memory(result.summary)
        persistence = self._persist_project_result(result.project) if result.completed else None
        payload = {"project_progress": result, "project": result.project}
        if persistence is not None:
            payload["persistence"] = persistence
        return ActionResolution(
            action=action,
            rules_text=result.summary,
            payload=payload,
        )

    def _resolve_use_inventory(self, action: Action) -> ActionResolution:
        actor = action.parameters.get("actor") or action.parameters.get("user")
        item_name = action.parameters.get("item_name") or action.parameters.get("item") or "治疗剂"
        result = self.gadget_manager.use_inventory_item(
            actor,
            item_name,
            target_name=action.parameters.get("target") or action.parameters.get("target_name"),
            damage_type=action.parameters.get("damage_type", "fire"),
            status_effect=action.parameters.get("status_effect"),
        )
        self.world_state.add_memory(result.summary)
        return ActionResolution(action=action, rules_text=result.summary, payload={"inventory_result": result})

    def _resolve_tinkerer_gadget(self, action: Action) -> ActionResolution:
        actor = action.parameters.get("actor") or action.parameters.get("user")
        gadget_type = str(action.parameters.get("gadget_type") or action.parameters.get("type") or "").lower()
        mode = str(action.parameters.get("mode") or action.parameters.get("subtype") or action.parameters.get("infusion_name") or "")

        if gadget_type in {"alchemy", "炼金术", "调合"} or mode in {"alchemy", "炼金术", "调合"}:
            result = self.gadget_manager.use_alchemy(
                actor,
                tier=action.parameters.get("tier", "basic"),
                target_roll=action.parameters.get("target_roll"),
                effect_roll=action.parameters.get("effect_roll"),
                targets=action.parameters.get("targets"),
            )
            self.world_state.add_memory(result.summary)
            return ActionResolution(action=action, rules_text=result.summary, payload={"gadget_result": result})

        if gadget_type in {"infusion", "灌注术", "灌注"} or mode in self.gadget_manager.INFUSIONS:
            infusion_name = action.parameters.get("infusion_name") or action.parameters.get("mode") or "焦火"
            result = self.gadget_manager.prepare_infusion(actor, infusion_name)
            self.world_state.add_memory(result.summary)
            return ActionResolution(action=action, rules_text=result.summary, payload={"gadget_result": result})

        if gadget_type in {"magitech", "魔科技", "magictech"} or any(token in mode for token in ["魔法加农炮", "魔加农", "篡夺", "天球"]):
            if any(token in mode for token in ["篡夺", "override"]):
                target_name = self._target_name(action, "当前构装体")
                if not self.character_manager.exists(target_name):
                    self.world_state.add_memory(f"{actor} 尝试以魔科技篡夺影响场景装置【{target_name}】。")
                    self.world_state.remember_subject_fact(target_name, f"被 {actor} 尝试魔科技篡夺。")
                    return ActionResolution(
                        action=action,
                        rules_text=f"{actor} 尝试对【{target_name}】进行魔科技篡夺；该目标不是已建档构装体，已作为场景装置交互记录。",
                        payload={"gadget_result": None, "scene_object": target_name, "scene_gadget": True},
                    )
                result = self.gadget_manager.magitech_override(
                    actor,
                    target_name,
                    action.parameters.get("forced_action") or action.parameters.get("command") or "指定行动",
                )
                self.world_state.add_memory(result.summary)
                return ActionResolution(action=action, rules_text=result.summary, payload={"gadget_result": result})
            if any(token in mode for token in ["天球", "magisphere"]):
                return self._resolve_magisphere(action, actor)
            result = self.gadget_manager.create_magicannon(actor, action.parameters.get("damage_type", "physical"))
            self.world_state.add_memory(result.summary)
            return ActionResolution(action=action, rules_text=result.summary, payload={"gadget_result": result})

        return ActionResolution(
            action=action,
            rules_text="造物使便携装置动作已识别，但缺少 gadget_type 或 mode，未执行数值结算。",
            payload={"gadget_failed": True},
        )

    def _resolve_magisphere(self, action: Action, actor: str) -> ActionResolution:
        ip_change = self.gadget_manager.spend_ip(actor, 2, "创建魔科天球。")
        spell_action = Action(
            action_type=ActionType.SPELL,
            parameters={
                **action.parameters,
                "actor": actor,
                "spell_name": action.parameters.get("spell_name") or action.parameters.get("spell") or "落雷",
                "target": action.parameters.get("target", actor),
            },
        )
        nested = self._resolve_spell(spell_action)
        result = TinkererGadgetResult(
            actor=actor,
            gadget_type="魔科技",
            mode="魔科天球",
            ip_change=ip_change,
            nested_resolution=nested,
            summary=f"{actor} 消耗 2 IP 制造魔科天球，并立即释放【{spell_action.parameters['spell_name']}】。",
        )
        self.world_state.add_memory(result.summary)
        return ActionResolution(
            action=action,
            rules_text=f"{result.summary} {nested.rules_text}",
            payload={"gadget_result": result, "nested_resolution": nested},
        )

    def _resolve_shop(self, action: Action) -> ActionResolution:
        actor = action.parameters.get("actor") or action.parameters.get("buyer")
        mode = str(action.parameters.get("mode") or action.parameters.get("shop_action") or "buy").lower()
        if mode in {"lodging", "inn", "rest_service", "旅馆", "住宿", "休息服务"}:
            transaction = self.economy_manager.buy_lodging(
                actor,
                settlement_size=action.parameters.get("settlement_size", "town"),
                party_size=action.parameters.get("party_size", 1),
            )
        elif mode in {"travel_service", "hire_transport", "rent_transport", "雇佣旅行服务", "旅行服务", "租交通"}:
            transaction = self.economy_manager.pay_travel_service(
                actor,
                action.parameters.get("transport") or action.parameters.get("item_name") or "陆地旅行服务",
                days=action.parameters.get("days", 1),
                party_size=action.parameters.get("party_size", 1),
            )
        elif mode in {"buy_transport", "transport", "vehicle", "mount", "购买交通", "购买载具", "购买坐骑"}:
            transaction = self.economy_manager.buy_transport(
                actor,
                action.parameters.get("transport") or action.parameters.get("item_name") or "地面载具",
                owner=action.parameters.get("owner", "小队"),
            )
        elif mode in {"restock", "补充", "补充库存", "inventory"} or action.parameters.get("item_name") in {"库存点", "IP", "ip"}:
            quantity = action.parameters.get("quantity")
            if quantity is None:
                character = self.character_manager.get(actor)
                maximum = character.max_inventory_points or 6
                quantity = max(0, maximum - character.inventory_points)
            transaction = self.economy_manager.restock_inventory(actor, quantity)
        else:
            transaction = self.economy_manager.buy_item(
                actor,
                action.parameters.get("item_name") or action.parameters.get("item") or "治疗剂",
                quantity=action.parameters.get("quantity", 1),
                equip=action.parameters.get("equip", False),
            )
        self.world_state.add_memory(transaction.summary)
        return ActionResolution(action=action, rules_text=transaction.summary, payload={"shop_transaction": transaction})

    def _resolve_open_chest(self, action: Action) -> ActionResolution:
        opener = action.parameters.get("actor") or action.parameters.get("opener") or self._default_story_change_target()
        reward = self.economy_manager.open_chest(
            opener,
            action.parameters.get("chest_name") or action.parameters.get("name") or "宝箱",
            rarity=action.parameters.get("rarity", "standard"),
            fixed_item=action.parameters.get("fixed_item", ""),
            fixed_zenit=action.parameters.get("fixed_zenit"),
        )
        return ActionResolution(action=action, rules_text=reward.summary, payload={"chest_reward": reward})

    def _resolve_award_reward(self, action: Action) -> ActionResolution:
        recipients = action.parameters.get("recipients") or [
            character.name for character in self.character_manager.all() if "pc" in character.traits
        ]
        party_level = action.parameters.get(
            "party_level",
            max((self.character_manager.get(name).level for name in recipients if self.character_manager.exists(name)), default=5),
        )
        reward = self.economy_manager.award_session_treasure(
            recipients,
            party_level=party_level,
            difficulty=action.parameters.get("difficulty", "normal"),
            rare_item=action.parameters.get("rare_item", ""),
        )
        return ActionResolution(action=action, rules_text=reward.summary, payload={"session_reward": reward})

    def _resolve_explore_dungeon(self, action: Action) -> ActionResolution:
        manager = self._require_dungeon_manager()
        actor = action.parameters.get("actor") or action.parameters.get("explorer") or self._default_story_change_target()
        result = manager.explore_area(
            action.parameters.get("area_name") or action.parameters.get("area"),
            actor=actor,
            action=action.parameters.get("mode")
            or action.parameters.get("exploration_action")
            or action.parameters.get("dungeon_action")
            or "enter",
            success=action.parameters.get("success"),
            collect_treasure=action.parameters.get("collect_treasure", False),
            trigger_trap=action.parameters.get("trigger_trap", False),
            danger_segments=self._int_parameter(action.parameters, "danger_segments", 1, minimum=1, maximum=6),
            clear_area=action.parameters.get("clear_area"),
            note=action.parameters.get("note", ""),
        )
        self.world_state.record_memory_event(
            result.summary,
            kind="dungeon_exploration",
            entities=[entity for entity in [actor, result.dungeon_name, result.area_name] if entity],
            tags=["dungeon", result.area_type.value, result.action],
        )
        payload = {"dungeon_exploration": result}
        rules_text = result.summary
        if (
            result.treasure_collected
            and action.parameters.get("award_treasure", True)
            and actor
            and self.character_manager.exists(actor)
        ):
            fixed_item = action.parameters.get("fixed_item")
            if fixed_item is None:
                fixed_item = result.reward_item
                if not fixed_item:
                    fixed_item = "" if "或" in result.treasure else result.treasure
            fixed_zenit = action.parameters.get("fixed_zenit")
            if fixed_zenit is None:
                fixed_zenit = result.reward_zenit
            reward = self.economy_manager.open_chest(
                actor,
                action.parameters.get("chest_name") or f"{result.area_name}的宝箱",
                rarity=action.parameters.get("rarity", result.reward_rarity or "standard"),
                fixed_item=fixed_item,
                fixed_zenit=fixed_zenit,
            )
            payload["chest_reward"] = reward
            rules_text = f"{rules_text} {reward.summary}"
        return ActionResolution(action=action, rules_text=rules_text, payload=payload)

    def _resolve_next_turn(self, action: Action) -> ActionResolution:
        previous_actor = self.conflict_manager.state.current_actor()
        next_actor = self.conflict_manager.next_turn()
        if next_actor is None:
            return ActionResolution(
                action=action,
                rules_text="当前没有激活的冲突轮转。",
                payload={"previous_actor": previous_actor, "next_actor": None},
            )
        phase = self.conflict_manager.format_phase()
        return ActionResolution(
            action=action,
            rules_text=f"回合从 {previous_actor or '无'} 推进到 {next_actor}。{phase}",
            payload={
                "previous_actor": previous_actor,
                "next_actor": next_actor,
                "round_number": self.conflict_manager.state.round_number,
                "phase": phase,
                "queued_turns": list(self.conflict_manager.state.queued_turns),
                "bonus_turn": self.conflict_manager.state.current_bonus_actor is not None,
            },
        )

    def _persist_ritual_result(self, action: Action, result) -> object:
        persistence_type = self._persistent_change_type(
            action.parameters.get("persistence_type") or action.parameters.get("output_type"),
            fallback=PersistentChangeType.WORLD_FACT,
        )
        if persistence_type not in {PersistentChangeType.WORLD_FACT, PersistentChangeType.FACILITY}:
            persistence_type = PersistentChangeType.WORLD_FACT
        location = action.parameters.get("location", "")
        name = action.parameters.get("subject") or result.plan.name
        source = f"仪式：{result.plan.name}"
        if persistence_type == PersistentChangeType.FACILITY:
            return self.world_state.record_location_facility(
                name=name,
                description=result.plan.effect,
                source=source,
                location=location or self._default_location(),
                tags=["ritual", result.plan.discipline.value],
            )
        return self.world_state.record_world_fact(
            name=name,
            description=result.plan.effect,
            source=source,
            location=location,
            tags=["ritual", result.plan.discipline.value],
        )

    def _persist_project_result(self, project) -> object | None:
        if project.persisted:
            return None
        source = f"项目：{project.name}"
        owner = project.owner or project.inventor
        location = project.location or self._default_location()
        if project.output_type == PersistentChangeType.EQUIPMENT:
            change = self.world_state.record_created_asset(
                change_type=PersistentChangeType.EQUIPMENT,
                name=project.name,
                description=project.effect,
                source=source,
                owner=owner,
                location=project.location,
                tags=["project", project.potency.value, project.scope.value],
            )
            self._add_created_item_to_character(owner, project.name)
        elif project.output_type == PersistentChangeType.CONSUMABLE:
            item_name = f"{project.name}（一次性）"
            change = self.world_state.record_created_asset(
                change_type=PersistentChangeType.CONSUMABLE,
                name=project.name,
                description=project.effect,
                source=source,
                owner=owner,
                location=project.location,
                tags=["project", "consumable", project.potency.value, project.scope.value],
            )
            self._add_created_item_to_character(owner, item_name)
        elif project.output_type == PersistentChangeType.FACILITY:
            change = self.world_state.record_location_facility(
                name=project.name,
                description=project.effect,
                source=source,
                location=location,
                tags=["project", "facility", project.potency.value, project.scope.value],
            )
        else:
            change = self.world_state.record_world_fact(
                name=project.name,
                description=project.effect,
                source=source,
                location=project.location,
                tags=["project", project.potency.value, project.scope.value],
            )
        project.persisted = True
        project.created_asset_id = self.world_state.format_persistent_change(change)
        return change

    def _add_created_item_to_character(self, owner: str, item_name: str) -> None:
        if not owner or not self.character_manager.exists(owner):
            return
        character = self.character_manager.get(owner)
        if item_name not in character.equipment:
            character.equipment.append(item_name)

    def _default_location(self) -> str:
        if self.world_state.world_sheet and self.world_state.world_sheet.starting_region:
            return self.world_state.world_sheet.starting_region
        if self.world_state.world_profile.starting_region:
            return self.world_state.world_profile.starting_region
        return "当前场景"

    def _require_ritual_manager(self) -> RitualManager:
        if self.ritual_manager is None:
            raise ValueError("当前拦截器未配置 RitualManager，不能结算仪式动作。")
        return self.ritual_manager

    def _require_project_manager(self) -> ProjectManager:
        if self.project_manager is None:
            raise ValueError("当前拦截器未配置 ProjectManager，不能结算项目动作。")
        return self.project_manager

    def _require_dungeon_manager(self) -> DungeonManager:
        if self.dungeon_manager is None:
            raise ValueError("当前拦截器未配置 DungeonManager，不能结算地下城探索动作。")
        return self.dungeon_manager

    def _remember_roll(self, outcome) -> None:
        self.pending_rolls[outcome.actor] = outcome

    def _consume_advantage_bonus(self, actor_name: str) -> int:
        return self.pending_advantages.pop(actor_name, 0)

    def _declared_teamwork_bonus(self, action: Action, leader) -> tuple[int, dict[str, object]]:
        raw_supporters = action.parameters.get("teamwork_supporters", action.parameters.get("supporters", []))
        if isinstance(raw_supporters, str):
            supporter_names = [name.strip() for name in re.split(r"[、,，/]+", raw_supporters) if name.strip()]
        else:
            supporter_names = [str(name).strip() for name in raw_supporters if str(name).strip()]
        supporter_names = [name for name in supporter_names if name and name != leader.name and self.character_manager.exists(name)]
        if not supporter_names:
            return 0, {}
        already_consumed_raw = action.parameters.get("teamwork_turns_already_consumed", [])
        if isinstance(already_consumed_raw, str):
            already_consumed = {name.strip() for name in re.split(r"[、,，/]+", already_consumed_raw) if name.strip()}
        else:
            already_consumed = {str(name).strip() for name in already_consumed_raw if str(name).strip()}
        highest_bond = 0
        valid_supporters = []
        consumed_now: list[str] = []
        rejected_supporters: list[str] = []
        for supporter_name in supporter_names:
            supporter = self.character_manager.get(supporter_name)
            if self.conflict_manager.state.active and supporter_name not in already_consumed:
                if self.conflict_manager.register_team_assist(
                    supporter_name,
                    leader.name,
                    reason="declared_teamwork",
                ):
                    consumed_now.append(supporter_name)
                else:
                    rejected_supporters.append(supporter_name)
                    continue
            highest_bond = max(highest_bond, supporter.bond_strength_with(leader.name))
            valid_supporters.append(supporter_name)
        if not valid_supporters:
            return 0, {}
        bonus = len(valid_supporters) + highest_bond
        return bonus, {
            "supporters": valid_supporters,
            "support_bonus": len(valid_supporters),
            "highest_bond_bonus": highest_bond,
            "total_bonus": bonus,
            "turns_consumed": bool(self.conflict_manager.state.active),
            "turns_consumed_now": consumed_now,
            "turns_already_consumed": sorted(already_consumed & set(valid_supporters)),
            "rejected_supporters": rejected_supporters,
        }

    def _apply_declared_invocations(self, action: Action, outcome, actor) -> tuple[object, list[str], dict[str, object]]:
        notes: list[str] = []
        payload: dict[str, object] = {}
        invoked_roll = outcome
        trait_name = (
            action.parameters.get("invoke_trait")
            or action.parameters.get("trait_name")
            or action.parameters.get("invoke_trait_name")
        )
        if trait_name:
            if invoked_roll.fumble:
                raise ValueError("大失败不能援用特质重掷。")
            reroll_indices = action.parameters.get("reroll_indices", action.parameters.get("reroll_dice"))
            if reroll_indices is None:
                values = [rolled for _, rolled in invoked_roll.dice]
                lowest = min(range(len(values)), key=lambda index: values[index]) if values else 0
                reroll_indices = [lowest]
            resource_change = self._spend_invocation_resource(actor.name, str(trait_name), is_trait=True)
            before_roll = invoked_roll
            invoked_roll = self.rules_engine.reroll_outcome(invoked_roll, reroll_indices)
            payload["trait_invocation"] = {
                "trait_name": str(trait_name),
                "before_roll": before_roll,
                "after_roll": invoked_roll,
                "resource_change": resource_change,
            }
            notes.append(f"{actor.name} 援用特质【{trait_name}】重掷，结算值变为 {invoked_roll.total}。")

        bond_target = action.parameters.get("invoke_bond_target") or action.parameters.get("bond_target")
        if bond_target:
            if invoked_roll.fumble:
                raise ValueError("大失败自动失败，不能靠羁绊加值改写。")
            bond_strength = actor.bond_strength_with(str(bond_target))
            if bond_strength <= 0:
                raise ValueError(f"{actor.name} 对【{bond_target}】没有可援用的羁绊。")
            before, after = self.character_manager.modify_resource(actor.name, "fabula_points", -1)
            if before <= after:
                raise ValueError(f"{actor.name} 没有足够物语点援用羁绊。")
            before_roll = invoked_roll
            invoked_roll = self.rules_engine.apply_bond_bonus(invoked_roll, bond_strength)
            payload["bond_invocation"] = {
                "bond_target": str(bond_target),
                "bond_strength": bond_strength,
                "before_roll": before_roll,
                "after_roll": invoked_roll,
                "resource_change": ResourceChange(
                    actor.name,
                    "fabula_points",
                    after - before,
                    before,
                    after,
                    "援用羁绊为检定提供加值。",
                ),
            }
            notes.append(f"{actor.name} 援用对【{bond_target}】的羁绊，结算值 +{bond_strength} 至 {invoked_roll.total}。")
        return invoked_roll, notes, payload

    def _spend_invocation_resource(self, actor_name: str, trait_name: str, *, is_trait: bool) -> ResourceChange | ConflictEvent:
        actor = self.character_manager.get(actor_name)
        if "pc" in actor.traits:
            before, after = self.character_manager.modify_resource(actor_name, "fabula_points", -1)
            if before <= after:
                raise ValueError(f"{actor_name} 没有足够物语点援用特质。")
            return ResourceChange(
                actor_name,
                "fabula_points",
                after - before,
                before,
                after,
                f"援用特质【{trait_name}】重掷。",
            )
        if self.conflict_manager.is_villain(actor_name):
            return self.conflict_manager.spend_ultima_for_trait_invocation(actor_name)
        raise ValueError("只有玩家角色或反派可以援用特质。")

    def _resolve_attack(self, action: Action) -> ActionResolution:
        infusion_result = None
        if action.parameters.get("infusion") or action.parameters.get("infusion_name"):
            action, infusion_result = self._with_attack_infusion(action)
        if self._uses_attack_window(action):
            resolution = self._resolve_attack_window(action)
            return self._attach_gadget_result(resolution, infusion_result)
        actor = self.character_manager.get(action.parameters["actor"])
        actual_target, cover_text = self._resolve_attack_target(action)
        damage_type = self._attack_damage_type(actor, action)
        defense_type = self._attack_defense_type(actor, action)
        attack_action = Action(
            action_type=ActionType.REQUEST_ROLL,
            parameters={
                **action.parameters,
                "attributes": action.parameters.get("attributes", actor.weapon_accuracy_attributes),
                "modifier": self._int_parameter(action.parameters, "modifier", 0)
                + actor.weapon_accuracy_modifier
                + self._weapon_mastery_bonus(
                    actor,
                    action.parameters.get("is_melee", actor.weapon_range != "ranged"),
                )
                + actor.equipment_accuracy_bonus,
                "target": actual_target.name,
                "target_number": action.parameters.get(
                    "target_number",
                    self.character_manager.effective_defense(actual_target.name, defense_type),
                ),
                "damage_type": damage_type,
                "ignore_resist": action.parameters.get("ignore_resist", False)
                or self._attack_ignores_resist(actor, damage_type),
                "ignore_all_affinities": action.parameters.get("ignore_all_affinities", False)
                or actor.equipment_ignore_all_affinities,
                "weapon_damage": action.parameters.get("weapon_damage", actor.weapon_damage)
                + self._hero_damage_bonus(
                    actor,
                    is_spell=False,
                    is_melee=action.parameters.get("is_melee", actor.weapon_range != "ranged"),
                )
                + actor.equipment_attack_damage_bonus,
            },
        )
        resolution = self._resolve_roll(attack_action)
        resolution.action = action
        if cover_text:
            resolution.rules_text = f"{cover_text} {resolution.rules_text}"
            resolution.payload["cover_text"] = cover_text
        return self._attach_gadget_result(resolution, infusion_result)

    def _resolve_attack_window(self, action: Action) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        is_melee = action.parameters.get("is_melee", actor.weapon_range != "ranged")
        target_names = self._attack_target_names(action)
        actual_targets = []
        cover_texts = []
        for target_name in target_names:
            target = self.character_manager.get(target_name)
            if is_melee:
                guardian = self.character_manager.guardian_for(target.name)
                if guardian is not None:
                    cover_texts.append(f"{guardian.name} 挡在 {target.name} 身前，替同伴承受了这次近战攻击。")
                    target = guardian
            actual_targets.append(target)
        if not actual_targets:
            raise ValueError("攻击至少需要一个目标。")

        teamwork_bonus, teamwork_payload = self._declared_teamwork_bonus(action, actor)
        advantage_bonus = self._consume_advantage_bonus(actor.name)
        modifier = self._int_parameter(action.parameters, "modifier", 0) + actor.weapon_accuracy_modifier + self._weapon_mastery_bonus(
            actor, is_melee
        )
        modifier += actor.equipment_accuracy_bonus + teamwork_bonus + advantage_bonus
        defense_type = self._attack_defense_type(actor, action)
        first_target_number = action.parameters.get(
            "target_number",
            self.character_manager.effective_defense(actual_targets[0].name, defense_type),
        )
        shared_roll = self.rules_engine.roll_check(
            actor=actor,
            attributes=action.parameters.get("attributes", actor.weapon_accuracy_attributes),
            target_number=first_target_number,
            modifier=modifier,
            target="、".join(target.name for target in actual_targets),
            reason=action.parameters.get("reasoning", ""),
        )
        invocation_notes: list[str] = []
        invocation_payload: dict[str, object] = {}
        if action.parameters.get("invoke_trait") or action.parameters.get("trait_name") or action.parameters.get("invoke_bond_target") or action.parameters.get("bond_target"):
            shared_roll, invocation_notes, invocation_payload = self._apply_declared_invocations(action, shared_roll, actor)
        self._remember_roll(shared_roll)
        payload: dict[str, object] = {
            "roll": shared_roll,
            "rolls": [],
            "multi_target": len(actual_targets) > 1,
            "reaction_events": [],
            "available_reactions": self._available_attack_reactions(action, shared_roll, actual_targets, is_melee),
        }
        if teamwork_payload:
            payload["conflict_teamwork"] = teamwork_payload
        if advantage_bonus:
            payload["advantage_bonus"] = advantage_bonus
        if invocation_payload:
            payload.update(invocation_payload)
        rules_parts = []
        if advantage_bonus:
            rules_parts.append(f"机会【优势】提供 +{advantage_bonus} 修正")
        if teamwork_payload:
            rules_parts.append(f"团队合作提供 +{teamwork_payload['total_bonus']} 修正")
        rules_parts.extend(invocation_notes)

        if shared_roll.critical_success:
            rules_parts.append("触发大成功，获得 1 次机会。")
            trigger_results = self.trigger_manager.on_critical_success(actor.name)
            self._append_trigger_results(payload, trigger_results)
            rules_parts.extend(result.summary for result in trigger_results)
        if shared_roll.fumble:
            before, after = self.character_manager.modify_resource(actor.name, "fabula_points", 1)
            payload["fabula_gain"] = ResourceChange(
                target=actor.name,
                resource="fabula_points",
                amount=1,
                before=before,
                after=after,
                reason="大失败获得 1 点物语点。",
            )
            rules_parts.append("触发大失败，对手获得 1 次机会，且掷骰角色获得 1 点物语点。")
            trigger_results = self.trigger_manager.on_fumble(actor.name)
            self._append_trigger_results(payload, trigger_results)
            rules_parts.extend(result.summary for result in trigger_results)

        crossfire_cancelled, crossfire_events = self._apply_crossfire_reactions(action, shared_roll, is_melee)
        payload["reaction_events"].extend(crossfire_events)
        if crossfire_cancelled:
            rules_parts.append("干涉火力使本次远程攻击自动失败。")

        outcomes = []
        conflict_events = []
        damage_type = self._attack_damage_type(actor, action)
        ignore_resist = action.parameters.get("ignore_resist", False) or self._attack_ignores_resist(actor, damage_type)
        ignore_all_affinities = action.parameters.get("ignore_all_affinities", False) or actor.equipment_ignore_all_affinities
        weapon_damage = action.parameters.get("weapon_damage", actor.weapon_damage) + self._hero_damage_bonus(
            actor,
            is_spell=False,
            is_melee=is_melee,
        ) + actor.equipment_attack_damage_bonus
        for target in actual_targets:
            target_number = action.parameters.get(
                "target_number",
                self.character_manager.effective_defense(target.name, defense_type),
            )
            success = (
                shared_roll.critical_success
                or (shared_roll.total >= target_number and not shared_roll.fumble)
            ) and not crossfire_cancelled
            outcome = replace(
                shared_roll,
                target=target.name,
                target_number=target_number,
                success=success,
                margin=shared_roll.total - target_number,
                damage=0,
                damage_type=damage_type,
                hp_after=target.hp,
            )
            if success:
                next_damage_bonus = self._consume_next_damage_bonus(target.name)
                incoming_damage_bonus = self._incoming_damage_bonus(target.name)
                damage, affinity = self.rules_engine.compute_damage(
                    high_roll=outcome.high_roll,
                    weapon_damage=weapon_damage + next_damage_bonus + incoming_damage_bonus,
                    damage_type=damage_type,
                    target=target,
                    ignore_resist=ignore_resist,
                    ignore_all_affinities=ignore_all_affinities,
                )
                if damage >= 0:
                    _, after = self.character_manager.apply_damage(target.name, damage)
                else:
                    _, after = self.character_manager.modify_resource(target.name, "hp", -damage)
                outcome.damage = abs(damage)
                outcome.applied_affinity = affinity
                outcome.hp_after = after
                if next_damage_bonus:
                    payload.setdefault("next_damage_bonuses", {})[target.name] = next_damage_bonus
                if incoming_damage_bonus:
                    payload.setdefault("incoming_damage_bonuses", {})[target.name] = incoming_damage_bonus
                payload.setdefault("target_statuses", {})[target.name] = self.character_manager.format_status(
                    self.character_manager.get(target.name)
                )
                self._remember_damage_outcome(actor.name, target.name, outcome)
                self._apply_on_hit_status(action, target.name, payload)
                hit_trigger_results = self.trigger_manager.after_hit(
                    actor.name,
                    target.name,
                    is_spell=False,
                    is_critical=outcome.critical_success,
                    target_was_zero_hp=after == 0,
                )
                if hit_trigger_results:
                    after = self.character_manager.get(target.name).hp
                    outcome.hp_after = after
                    payload.setdefault("target_statuses", {})[target.name] = self.character_manager.format_status(
                        self.character_manager.get(target.name)
                    )
                    self._append_trigger_results(payload, hit_trigger_results)
                    rules_parts.extend(result.summary for result in hit_trigger_results)
                if after == 0:
                    zero_hp_trigger_results = self.trigger_manager.before_zero_hp(target.name)
                    if zero_hp_trigger_results:
                        after = self.character_manager.get(target.name).hp
                        outcome.hp_after = after
                        payload.setdefault("target_statuses", {})[target.name] = self.character_manager.format_status(
                            self.character_manager.get(target.name)
                        )
                        self._append_trigger_results(payload, zero_hp_trigger_results)
                        rules_parts.extend(result.summary for result in zero_hp_trigger_results)
                    if after == 0 and self.conflict_manager.prevent_zero_hp_once(target.name):
                        outcome.hp_after = 1
                        event = self.conflict_event_survive_once(target.name)
                    elif after == 0:
                        event = self.conflict_manager.resolve_zero_hp(
                            target=target.name,
                            pc_choice=action.parameters.get("pc_zero_hp_choice", "give_up_resistance"),
                            pc_consequence=action.parameters.get("pc_consequence", "被俘虏并失去重要装备"),
                            villain_mode=action.parameters.get("villain_zero_hp_mode", "auto"),
                            allow_escalation=action.parameters.get("allow_escalation", True),
                            sacrifice_benefits_bond=action.parameters.get("sacrifice_benefits_bond"),
                            sacrifice_betters_world=action.parameters.get("sacrifice_betters_world"),
                        )
                        if event.hp_after is not None:
                            outcome.hp_after = event.hp_after
                    else:
                        event = None
                    if event is not None:
                        conflict_events.append(event)
            outcomes.append(outcome)
            rules_parts.append(
                f"{target.name}: {outcome.total} vs {target_number}，"
                f"{'命中' if outcome.success else '未命中'}"
                + (f"，造成 {outcome.damage} 点{self._damage_type_text(outcome.damage_type)}伤害" if outcome.success else "")
            )

        payload["rolls"] = outcomes
        payload["roll"] = outcomes[0]
        if conflict_events:
            payload["conflict_events"] = conflict_events
            payload["conflict_event"] = conflict_events[0]

        counter_events = self._apply_counter_reactions(action, shared_roll, actual_targets, is_melee)
        payload["reaction_events"].extend(counter_events)
        for event in counter_events:
            if event.get("rules_text"):
                rules_parts.append(event["rules_text"])

        rules_text = f"多目标攻击检定 {shared_roll.total}: " if len(actual_targets) > 1 else f"攻击检定 {shared_roll.total}: "
        if cover_texts:
            rules_text = " ".join(cover_texts) + " " + rules_text
            payload["cover_texts"] = cover_texts
        return ActionResolution(action=action, rules_text=rules_text + "；".join(rules_parts) + "。", payload=payload)

    def _resolve_equip(self, action: Action) -> ActionResolution:
        actor_name = action.parameters["actor"]
        raw_items = action.parameters.get("items", action.parameters.get("item_names", action.parameters.get("item_name", [])))
        if isinstance(raw_items, str):
            item_names = [item.strip() for item in re.split(r"[、,，/]+", raw_items) if item.strip()]
        elif isinstance(raw_items, dict):
            item_names = [self._equipment_item_name_from_value(raw_items)]
        else:
            item_names = [self._equipment_item_name_from_value(item) for item in raw_items]
            item_names = [item for item in item_names if item]
        allow_armor = bool(action.parameters.get("allow_armor", False))
        if self.conflict_manager.state.active:
            allow_armor = False
        equipped = self.economy_manager.equip_items(actor_name, item_names, allow_armor=allow_armor)
        actor = self.character_manager.get(actor_name)
        rules_text = (
            f"{actor_name} 执行装备行动，装备：{('、'.join(equipped) if equipped else '无变更')}。"
            f" 当前主手【{actor.equipped_main_hand}】、副手【{actor.equipped_off_hand or '空'}】、"
            f"盾牌【{actor.equipped_shield or '无'}】、饰品【{actor.equipped_accessory or '无'}】。"
        )
        return ActionResolution(
            action=action,
            rules_text=rules_text,
            payload={"equipped_items": equipped, "actor_status": self.character_manager.format_status(actor)},
        )

    def _equipment_item_name_from_value(self, value: Any) -> str:
        if isinstance(value, dict):
            for key in ("item_name", "name", "item", "weapon", "equipment"):
                candidate = value.get(key)
                if candidate:
                    return str(candidate).strip()
            return ""
        return str(value).strip()

    def _resolve_start_conflict(self, action: Action) -> ActionResolution:
        scene_name = str(action.parameters.get("scene_name") or action.parameters.get("name") or "冲突场景")
        if self.conflict_manager.state.active:
            current_scene = self.conflict_manager.state.scene_name or "当前冲突"
            turn_order = list(self.conflict_manager.state.turn_order)
            return ActionResolution(
                action=action,
                rules_text=(
                    f"冲突场景【{current_scene}】已经在进行中；保留当前先攻与回合顺序，"
                    f"不重新初始化冲突。当前顺序：{' -> '.join(turn_order)}。"
                ),
                payload={
                    "conflict_already_active": True,
                    "scene_name": current_scene,
                    "turn_order": turn_order,
                    "current_actor": self.conflict_manager.state.current_actor(),
                },
            )
        pcs = self._string_sequence(action.parameters.get("pcs")) or [
            character.name for character in self.character_manager.all() if "pc" in character.traits
        ]
        enemies = self._string_sequence(action.parameters.get("enemies")) or [
            character.name for character in self.character_manager.all() if "enemy" in character.traits or "villain" in character.traits
        ]
        for enemy_name in enemies:
            if self.character_manager.exists(enemy_name):
                enemy = self.character_manager.get(enemy_name)
                if enemy_name not in self.conflict_manager.state.enemy_ranks and ("enemy" in enemy.traits or "villain" in enemy.traits):
                    rank = EnemyRank.VILLAIN if "villain" in enemy.traits else EnemyRank.SOLDIER
                    self.conflict_manager.register_enemy(enemy_name, rank, ultima_points=self._int_parameter(action.parameters, "ultima_points", 0, minimum=0))
        leader_name = str(action.parameters.get("leader") or (pcs[0] if pcs else ""))
        leader = self.character_manager.get(leader_name)
        support_names = [name for name in (self._string_sequence(action.parameters.get("supporters")) or pcs[1:]) if name != leader_name]
        supporters = [self.character_manager.get(name) for name in support_names if self.character_manager.exists(name)]
        enemy_characters = [self.character_manager.get(name) for name in enemies if self.character_manager.exists(name)]
        target_number = self.rules_engine.initiative_target(enemy_characters)
        initiative = self.rules_engine.roll_team_check(
            leader=leader,
            supporters=supporters,
            attributes=["DEX", "INS"],
            target_number=target_number,
            leader_modifier=leader.initiative,
        )
        players_first = bool(initiative.success)
        turn_order = self.conflict_manager.start_scene_from_initiative(scene_name, pcs, enemies, players_first=players_first)
        appearance_events = []
        for enemy_name in enemies:
            if self.conflict_manager.is_villain(enemy_name):
                appearance_events.append(self.conflict_manager.award_villain_appearance_fabula(enemy_name))
        rules_text = (
            f"先攻团队检定：{leader_name} {initiative.final_total} vs {target_number}，"
            f"{'玩家方先行动' if players_first else '敌方先行动'}。回合顺序：{' -> '.join(turn_order)}。"
        )
        return ActionResolution(
            action=action,
            rules_text=rules_text,
            payload={
                "initiative": initiative,
                "turn_order": turn_order,
                "players_first": players_first,
                "villain_appearance_events": appearance_events,
            },
        )

    def _resolve_invoke_trait(self, action: Action) -> ActionResolution:
        actor_name = action.parameters["actor"]
        actor = self.character_manager.get(actor_name)
        outcome = self.pending_rolls.get(actor_name)
        if outcome is None:
            raise ValueError(f"{actor_name} 没有可援用特质的待处理检定。")
        if action.parameters.get("skip_if_pending_roll_success") and outcome.success:
            return ActionResolution(
                action=action,
                rules_text=f"{actor_name} 上一次检定已经成功；按玩家声明，本次援用特质不触发，不消耗物语点。",
                payload={"skipped_invocation": True, "roll": outcome, "actor_status": self.character_manager.format_status(actor)},
            )
        if outcome.fumble:
            raise ValueError("大失败不能援用特质重掷。")
        trait_name = str(action.parameters.get("trait_name") or action.parameters.get("trait") or "未命名特质")
        resource = self._spend_invocation_resource(actor_name, trait_name, is_trait=True)
        rerolled = self.rules_engine.reroll_outcome(outcome, action.parameters.get("reroll_indices", action.parameters.get("reroll_dice")))
        self.pending_rolls[actor_name] = rerolled
        return ActionResolution(
            action=action,
            rules_text=f"{actor_name} 援用特质【{trait_name}】重掷，检定从 {outcome.total} 变为 {rerolled.total}。",
            payload={"before_roll": outcome, "roll": rerolled, "resource_change": resource, "actor_status": self.character_manager.format_status(actor)},
        )

    def _resolve_invoke_bond(self, action: Action) -> ActionResolution:
        actor_name = action.parameters["actor"]
        actor = self.character_manager.get(actor_name)
        outcome = self.pending_rolls.get(actor_name)
        if outcome is None:
            raise ValueError(f"{actor_name} 没有可援用羁绊的待处理检定。")
        if outcome.fumble:
            raise ValueError("大失败自动失败，不能靠羁绊加值改写。")
        bond_target = str(action.parameters.get("bond_target") or action.parameters.get("target") or "")
        strength = actor.bond_strength_with(bond_target)
        if strength <= 0:
            raise ValueError(f"{actor_name} 对【{bond_target}】没有可援用的羁绊。")
        before, after = self.character_manager.modify_resource(actor_name, "fabula_points", -1)
        if before <= after:
            raise ValueError(f"{actor_name} 没有足够物语点援用羁绊。")
        adjusted = self.rules_engine.apply_bond_bonus(outcome, strength)
        self.pending_rolls[actor_name] = adjusted
        change = ResourceChange(actor_name, "fabula_points", after - before, before, after, "援用羁绊。")
        return ActionResolution(
            action=action,
            rules_text=f"{actor_name} 援用对【{bond_target}】的羁绊，检定 +{strength}，从 {outcome.total} 变为 {adjusted.total}。",
            payload={"before_roll": outcome, "roll": adjusted, "resource_change": change, "bond_strength": strength},
        )

    def _resolve_opportunity(self, action: Action) -> ActionResolution:
        effect = str(action.parameters.get("effect") or action.parameters.get("opportunity") or "").strip()
        normalized = self._normalize_opportunity_effect(effect)
        actor = str(action.parameters.get("actor") or "system")
        payload: dict[str, object] = {"effect": normalized}
        if normalized == "progress":
            clock_name = action.parameters["clock_name"]
            self._ensure_clock_exists(action, clock_name, default_clock_type=str(action.parameters.get("clock_type") or "objective"))
            direction = -1 if action.parameters.get("erase") or action.parameters.get("clock_direction") == -1 else 1
            delta = self._int_parameter(action.parameters, "delta", 2, minimum=0) * direction
            before, after = self.clock_manager.advance(clock_name, delta)
            clock = self.clock_manager.get(clock_name)
            change = ClockChange(clock.name, before, after, delta, clock.max_segments, "机会效果：进展。")
            payload["clock_change"] = change
            return ActionResolution(action, f"机会【进展】：命刻 [{clock.name}] {'推进' if delta >= 0 else '擦除'} {abs(delta)} 格。", payload)
        if normalized == "bond":
            bond = self.character_manager.manage_bond(
                actor,
                str(action.parameters["target"]),
                self._string_sequence(action.parameters.get("emotions")) or [str(action.parameters.get("emotion") or "信赖")],
                mode="upsert",
            )
            payload["bond"] = bond
            return ActionResolution(action, f"机会【纽带】：{actor} 对【{bond.target}】的羁绊现在为强度 {bond.strength}。", payload)
        if normalized == "suffer":
            target_name = str(action.parameters["target"])
            status = self._status_effect(action.parameters.get("status_effect") or StatusEffect.SHAKEN.value)
            applied = self.conflict_manager.apply_status(target_name, status)
            payload["status_applied"] = applied
            payload["status"] = status
            return ActionResolution(action, f"机会【受苦】：{target_name} 被施加 {self._status_name(status)}。", payload)
        if normalized == "advantage":
            target_name = str(action.parameters.get("target") or action.parameters.get("advantage_target") or actor)
            self.pending_advantages[target_name] = self.pending_advantages.get(target_name, 0) + 4
            payload["target"] = target_name
            payload["advantage_bonus"] = 4
            return ActionResolution(action, f"机会【优势】：{target_name} 的下一次相关检定获得 +4 修正。", payload)
        text = str(action.parameters.get("fact") or action.parameters.get("information") or action.parameters.get("description") or "")
        if text:
            self.world_state.add_memory(f"机会【{effect or normalized}】：{text}")
        payload["text"] = text
        return ActionResolution(action, f"机会【{effect or normalized}】已记录。", payload)

    def _normalize_opportunity_effect(self, effect: str) -> str:
        aliases = {
            "揭示": "reveal",
            "进展": "progress",
            "纽带": "bond",
            "情报": "information",
            "青睐": "favor",
            "审视": "scan",
            "失态": "misstep",
            "失物": "lost_item",
            "受苦": "suffer",
            "优势": "advantage",
            "转折": "twist",
            "转折!": "twist",
        }
        return aliases.get(effect, effect.lower() or "information")

    def _resolve_manage_bond(self, action: Action) -> ActionResolution:
        actor = action.parameters["actor"]
        mode = str(action.parameters.get("mode") or "upsert")
        bond = self.character_manager.manage_bond(
            actor,
            str(action.parameters.get("target") or action.parameters.get("bond_target") or ""),
            self._string_sequence(action.parameters.get("emotions")) or ([str(action.parameters["emotion"])] if action.parameters.get("emotion") else []),
            mode=mode,
            replace=bool(action.parameters.get("replace", False)),
        )
        if bond is None:
            return ActionResolution(action, f"{actor} 抹除了对【{action.parameters.get('target')}】的羁绊。", {"bond_removed": True})
        return ActionResolution(action, f"{actor} 的羁绊【{bond.target}】现在为强度 {bond.strength}（{'、'.join(bond.emotions)}）。", {"bond": bond})

    def _resolve_sell_item(self, action: Action) -> ActionResolution:
        transaction = self.economy_manager.sell_item(
            str(action.parameters["actor"]),
            str(action.parameters["item_name"]),
            quantity=self._int_parameter(action.parameters, "quantity", 1, minimum=1),
            price_ratio=float(action.parameters.get("price_ratio", 0.5) or 0.5),
        )
        return ActionResolution(action, transaction.summary, {"transaction": transaction})

    def _resolve_player_vs_player(self, action: Action) -> ActionResolution:
        consent = bool(action.parameters.get("consent_confirmed", False))
        if not consent:
            return ActionResolution(
                action,
                "玩家对玩家冲突需要先暂停，确认所有相关玩家同意目标、边界和处理方式；本次不进行硬结算。",
                {"requires_consent": True},
            )
        left = self.character_manager.get(str(action.parameters["actor"]))
        right = self.character_manager.get(str(action.parameters["target"]))
        opposed = self.rules_engine.roll_opposed_check(
            left,
            right,
            action.parameters.get("attributes", ["WLP", "WLP"]),
            left_modifier=self._int_parameter(action.parameters, "left_modifier", 0),
            right_modifier=self._int_parameter(action.parameters, "right_modifier", 0),
        )
        return ActionResolution(action, f"PVP 对抗检定：胜者为 {opposed.winner}。", {"opposed_check": opposed})

    def _resolve_absent_player(self, action: Action) -> ActionResolution:
        actor = str(action.parameters.get("actor") or action.parameters.get("character") or "")
        mode = str(action.parameters.get("mode") or "fade_out")
        note = str(action.parameters.get("note") or "")
        if actor and self.character_manager.exists(actor):
            self.world_state.remember_subject_fact(actor, f"本场缺席处理：{mode}。{note}".strip())
        return ActionResolution(
            action,
            f"缺席玩家处理：{actor or '未指定角色'} 采用 {mode}。{note}",
            {"actor": actor, "mode": mode, "note": note},
        )

    def _string_sequence(self, value) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [piece.strip() for piece in re.split(r"[、,，/]+", value) if piece.strip()]
        return [str(item).strip() for item in value if str(item).strip()]

    def _resolve_npc_act(self, action: Action) -> ActionResolution:
        subaction = self._infer_npc_subaction(action)
        actor_name = action.parameters["actor"]
        if subaction == "Attack":
            resolution = self._resolve_attack(
                Action(
                    action_type=ActionType.ATTACK,
                    parameters={
                        "actor": actor_name,
                        "target": action.parameters.get("target")
                        or (action.parameters.get("targets") or [None])[0],
                        "targets": action.parameters.get("targets"),
                        "attributes": action.parameters.get("attributes", ["DEX", "MIG"]),
                        "damage_type": action.parameters.get(
                            "damage_type",
                            self.character_manager.effective_weapon_damage_type(actor_name),
                        ),
                        "infusion_name": action.parameters.get("infusion_name"),
                        "weapon_damage": action.parameters.get("weapon_damage", self.character_manager.get(actor_name).weapon_damage),
                        "reasoning": action.parameters.get("reasoning", ""),
                        "in_mind_reply": action.parameters.get("in_mind_reply", ""),
                        "is_melee": action.parameters.get("is_melee", True),
                        "reactions": action.parameters.get("reactions", []),
                    },
                )
            )
            resolution.action = action
            return resolution
        if subaction == "Spell":
            spell_name = action.parameters.get("spell_name")
            resolution = self._resolve_spell(
                Action(
                    action_type=ActionType.SPELL,
                    parameters={
                        "actor": actor_name,
                        "target": action.parameters.get("target", actor_name),
                        "spell_name": spell_name,
                        "attributes": action.parameters.get("attributes", ["INS", "WLP"]),
                        "mp_cost": action.parameters.get("mp_cost", 5),
                        "fixed_damage": action.parameters.get("fixed_damage", 5),
                        "damage_type": action.parameters.get("damage_type", "arcane"),
                        "reasoning": action.parameters.get("reasoning", ""),
                        "in_mind_reply": action.parameters.get("in_mind_reply", ""),
                    },
                )
            )
            resolution.action = action
            return resolution
        if subaction == "Guard":
            resolution = self._resolve_guard(
                Action(
                    action_type=ActionType.GUARD,
                    parameters={
                        "actor": actor_name,
                        "guarded_target": action.parameters.get("guarded_target"),
                        "in_mind_reply": action.parameters.get("in_mind_reply", ""),
                    },
                )
            )
            resolution.action = action
            return resolution
        if subaction == "Hinder":
            resolution = self._resolve_hinder(
                Action(
                    action_type=ActionType.HINDER,
                    parameters={
                        "actor": actor_name,
                        "target": self._target_name(action, "当前威胁"),
                        "attributes": action.parameters.get("attributes", ["INS", "WLP"]),
                        "status_effect": action.parameters.get("status_effect", "shaken"),
                        "target_number": action.parameters.get("target_number", 10),
                        "reasoning": action.parameters.get("reasoning", ""),
                        "in_mind_reply": action.parameters.get("in_mind_reply", ""),
                    },
                )
            )
            resolution.action = action
            return resolution
        if subaction == "Investigate":
            resolution = self._resolve_investigate(
                Action(
                    action_type=ActionType.INVESTIGATE,
                    parameters={
                        "actor": actor_name,
                        "target": self._target_name(action, "当前线索"),
                        "attributes": action.parameters.get("attributes", ["INS", "INS"]),
                        "reasoning": action.parameters.get("reasoning", ""),
                        "in_mind_reply": action.parameters.get("in_mind_reply", ""),
                    },
                )
            )
            resolution.action = action
            return resolution
        if subaction == "Objective":
            clock_name = action.parameters.get("clock_name") or action.parameters.get("target") or "当前目标命刻"
            resolution = self._resolve_objective(
                Action(
                    action_type=ActionType.OBJECTIVE,
                    parameters={
                        "actor": actor_name,
                        "target": action.parameters.get("target", clock_name),
                        "attributes": action.parameters.get("attributes", ["DEX", "INS"]),
                        "clock_name": clock_name,
                        "target_number": action.parameters.get("target_number", 10),
                        "threat_clock_name": action.parameters.get("threat_clock_name"),
                        "reasoning": action.parameters.get("reasoning", ""),
                        "in_mind_reply": action.parameters.get("in_mind_reply", ""),
                    },
                )
            )
            resolution.action = action
            return resolution
        if subaction == "Skill":
            resolution = self._resolve_skill(
                Action(
                    action_type=ActionType.SKILL,
                    parameters={
                        **action.parameters,
                        "actor": actor_name,
                    },
                )
            )
            resolution.action = action
            return resolution
        if subaction == "UltimaRecover":
            event = self.conflict_manager.spend_ultima_to_recover(actor_name)
            return ActionResolution(
                action=action,
                rules_text=event.summary,
                payload={"conflict_event": event},
            )
        if subaction == "Narrate":
            return ActionResolution(
                action=action,
                rules_text=action.parameters.get("summary", f"{actor_name} 暂时没有执行明确动作。"),
                payload={},
            )
        return ActionResolution(
            action=action,
            rules_text=f"{actor_name} 的 NPCAct 子动作 {subaction} 未识别。",
            payload={},
        )

    def _infer_npc_subaction(self, action: Action) -> str:
        explicit = action.parameters.get("npc_action_type") or action.parameters.get("subaction") or action.parameters.get("action")
        if explicit:
            return str(explicit)
        if action.parameters.get("clock_name"):
            return "Objective"
        if action.parameters.get("spell_name"):
            return "Spell"
        if action.parameters.get("status_effect"):
            return "Hinder"
        if action.parameters.get("guarded_target"):
            return "Guard"
        if action.parameters.get("target") or action.parameters.get("targets"):
            return "Attack"
        return "Narrate"

    def _resolve_spell(self, action: Action) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        spell_name = action.parameters.get("spell_name")
        if spell_name:
            try:
                return self._resolve_spell_from_definition(action, spell_name)
            except ValueError:
                target_name = (
                    action.parameters.get("target")
                    or action.parameters.get("subject")
                    or action.parameters.get("scene_object")
                    or "当前魔法目标"
                )
                return self._resolve_scene_object_spell(action, actor, target_name, spell_name)

        target_name = (
            action.parameters.get("target")
            or action.parameters.get("subject")
            or action.parameters.get("scene_object")
            or "当前魔法目标"
        )
        if not self.character_manager.exists(target_name):
            return self._resolve_scene_object_spell(action, actor, target_name, spell_name)
        target = self.character_manager.get(target_name)

        mp_cost = abs(action.parameters.get("mp_cost", 0))
        if actor.mp < mp_cost:
            return ActionResolution(
                action=action,
                rules_text=f"{actor.name} 的 MP 不足，无法施放该法术。",
                payload={
                    "spell_failed": True,
                    "resource_change": ResourceChange(
                        target=actor.name,
                        resource="mp",
                        amount=0,
                        before=actor.mp,
                        after=actor.mp,
                        reason="MP 不足，法术未能发动。",
                    ),
                },
            )

        before_mp, after_mp = self.character_manager.modify_resource(actor.name, "mp", -mp_cost)
        mp_change = ResourceChange(
            target=actor.name,
            resource="mp",
            amount=-mp_cost,
            before=before_mp,
            after=after_mp,
            reason="施放法术消耗 MP。",
        )

        spell_action = Action(
            action_type=ActionType.REQUEST_ROLL,
            parameters={
                **action.parameters,
                "attributes": action.parameters.get("attributes", ["INS", "WLP"]),
                "target_number": action.parameters.get(
                    "target_number",
                    self.character_manager.effective_defense(target.name, "magic"),
                ),
                "modifier": self._int_parameter(action.parameters, "modifier", 0) + actor.equipment_spell_bonus,
                "weapon_damage": action.parameters.get("fixed_damage", 0)
                + self._hero_damage_bonus(actor, is_spell=True)
                + actor.equipment_spell_damage_bonus,
                "damage_type": action.parameters.get("damage_type", "arcane"),
                "ignore_resist": action.parameters.get("ignore_resist", False)
                or self._attack_ignores_resist(actor, action.parameters.get("damage_type", "arcane")),
                "ignore_all_affinities": action.parameters.get("ignore_all_affinities", False)
                or actor.equipment_ignore_all_affinities,
            },
        )
        resolution = self._resolve_roll(spell_action)
        resolution.action = action
        resolution.payload["resource_change"] = mp_change
        return resolution

    def _resolve_spell_from_definition(self, action: Action, spell_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        definition = get_spell_definition(spell_name)
        target_names = self._spell_target_names(action, definition, actor.name)
        missing_targets = [name for name in target_names if not self.character_manager.exists(name)]
        if missing_targets:
            if len(target_names) == 1:
                target_name = missing_targets[0]
                return self._resolve_scene_object_spell(
                    action,
                    actor,
                    target_name,
                    definition.name,
                    default_mp_cost=definition.mp_cost,
                )
            return self._resolve_scene_object_spell(
                action,
                actor,
                "、".join(missing_targets),
                definition.name,
                default_mp_cost=definition.mp_cost * len(target_names),
            )
        target = self.character_manager.get(target_names[0])

        default_mp_cost = definition.mp_cost * len(target_names) if self._is_multi_target_spell(definition) else definition.mp_cost
        mp_cost = abs(action.parameters.get("mp_cost", default_mp_cost))
        if actor.mp < mp_cost:
            if skill_rank(actor.skills, "生命秘法") > 0:
                hp_cost = 10 + mp_cost
                if actor.hp - hp_cost > 0:
                    before_hp, after_hp = self.character_manager.modify_resource(actor.name, "hp", -hp_cost)
                    action.parameters["_vismagus_hp_payment"] = True
                    mp_change = ResourceChange(
                        target=actor.name,
                        resource="hp",
                        amount=after_hp - before_hp,
                        before=before_hp,
                        after=after_hp,
                        reason=f"【生命秘法】代替【{definition.name}】的 MP 消耗。",
                    )
                else:
                    return ActionResolution(
                        action=action,
                        rules_text=f"{actor.name} 的 MP 不足，且【生命秘法】会使 HP 降至 0，无法施放【{definition.name}】。",
                        payload={
                            "spell_failed": True,
                            "spell_name": definition.name,
                            "resource_change": ResourceChange(
                                target=actor.name,
                                resource="hp",
                                amount=0,
                                before=actor.hp,
                                after=actor.hp,
                                reason="生命秘法不能让施法者降至 0 HP。",
                            ),
                        },
                    )
            else:
                return ActionResolution(
                    action=action,
                    rules_text=f"{actor.name} 的 MP 不足，无法施放【{definition.name}】。",
                    payload={
                        "spell_failed": True,
                        "spell_name": definition.name,
                        "resource_change": ResourceChange(
                            target=actor.name,
                            resource="mp",
                            amount=0,
                            before=actor.mp,
                            after=actor.mp,
                            reason="MP 不足，法术未能发动。",
                        ),
                    },
                )
        else:
            before_mp, after_mp = self.character_manager.modify_resource(actor.name, "mp", -mp_cost)
            mp_change = ResourceChange(
                target=actor.name,
                resource="mp",
                amount=-mp_cost,
                before=before_mp,
                after=after_mp,
                reason=f"施放【{definition.name}】消耗 MP。",
            )

        if definition.effect_type == SpellEffectType.DAMAGE:
            if len(target_names) > 1:
                resolution = self._resolve_spell_damage_multi(action, definition, target_names)
            else:
                resolution = self._resolve_spell_damage(action, definition, target.name)
            resolution.action = action
            resolution.payload["resource_change"] = mp_change
            resolution.payload["spell_name"] = definition.name
            return resolution

        if definition.effect_type == SpellEffectType.MP_DAMAGE:
            return self._resolve_spell_mp_damage(action, definition, target.name, mp_change)

        if definition.effect_type == SpellEffectType.HEAL:
            if len(target_names) > 1:
                return self._resolve_spell_heal_multi(action, definition, target_names, mp_change)
            return self._resolve_spell_heal(action, definition, target.name, mp_change)

        if definition.effect_type == SpellEffectType.STATUS_CLEAR:
            if len(target_names) > 1:
                return self._resolve_spell_status_clear_multi(action, definition, target_names, mp_change)
            return self._resolve_spell_status_clear(action, definition, target.name, mp_change)

        if definition.effect_type == SpellEffectType.STATUS_APPLY:
            if len(target_names) > 1:
                return self._resolve_spell_status_apply_multi(action, definition, target_names, mp_change)
            return self._resolve_spell_status_apply(action, definition, target.name, mp_change)

        if definition.effect_type == SpellEffectType.DISPEL:
            return self._resolve_spell_dispel(action, definition, target.name, mp_change)

        if definition.name == "终焉降临":
            target_name = target.name
            amount = 20 + target.level // 2
            before, after = self.character_manager.apply_damage(target_name, amount)
            zero_hp_event = None
            if after == 0:
                zero_hp_event = self.conflict_manager.resolve_zero_hp(target_name)
            return ActionResolution(
                action=action,
                rules_text=f"{actor.name} 施放【终焉降临】，{target_name} 失去 {before - after} 点 HP。",
                payload={
                    "resource_change": mp_change,
                    "spell_name": definition.name,
                    "fixed_hp_loss": ResourceChange(
                        target=target_name,
                        resource="hp",
                        amount=after - before,
                        before=before,
                        after=after,
                        reason="【终焉降临】使目标失去生命值。",
                    ),
                    "zero_hp_event": zero_hp_event,
                },
            )

        if definition.name == "时空静滞":
            for target_name in target_names:
                self.conflict_manager.penalize_next_turn(target_name, 1)
            return ActionResolution(
                action=action,
                rules_text=f"{actor.name} 施放【时空静滞】，{'、'.join(target_names)} 在下个回合少执行 1 次行动。",
                payload={
                    "resource_change": mp_change,
                    "spell_name": definition.name,
                    "action_penalty_targets": target_names,
                },
            )

        if definition.effect_type == SpellEffectType.NARRATIVE:
            target_label = "、".join(target_names)
            memory = f"{actor.name} 施放【{definition.name}】影响 {target_label}：{definition.description}"
            self.world_state.add_memory(memory)
            for target_name in target_names:
                self.world_state.remember_subject_fact(target_name, memory)
            return ActionResolution(
                action=action,
                rules_text=(
                    f"{actor.name} 施放【{definition.name}】。{definition.description} "
                    "此法术包含特殊时机或条件效果，已记录为叙事/裁量效果；若后续涉及检定、命刻、顺势攻击或资源变化，请接续调用对应硬规则。"
                ),
                payload={
                    "resource_change": mp_change,
                    "spell_name": definition.name,
                    "spell_target": target_label,
                    "narrative_spell": True,
                },
            )

        timed_effects = [
            self._register_spell_effect(actor.name, target_name, action, definition)
            for target_name in target_names
        ]
        rules_text = " ".join(
            self._spell_effect_rules_text(actor.name, target_name, definition, effect)
            for target_name, effect in zip(target_names, timed_effects)
        )
        payload: dict[str, object] = {
            "resource_change": mp_change,
            "spell_name": definition.name,
            "spell_effect": timed_effects[0] if len(timed_effects) == 1 else timed_effects,
            "spell_target": target_names[0] if len(target_names) == 1 else target_names,
        }
        if definition.effect_type == SpellEffectType.WEAPON_ENCHANT and actor.name in target_names:
            opportunity_target = action.parameters.get("opportunity_target") or action.parameters.get("attack_target")
            if opportunity_target:
                attack_resolution = self._resolve_attack(
                    Action(
                        action_type=ActionType.ATTACK,
                        parameters={
                            "actor": actor.name,
                            "target": opportunity_target,
                            "is_melee": action.parameters.get("is_melee", True),
                            "reasoning": f"【{definition.name}】施放后的顺势攻击。",
                        },
                    )
                )
                payload["opportunity_attack"] = attack_resolution
                rules_text += " " + attack_resolution.rules_text
            else:
                payload["opportunity_attack_available"] = True
                rules_text += " 若施法者正装备这件武器，可指定 opportunity_target 立即进行一次顺势攻击。"
        return ActionResolution(
            action=action,
            rules_text=rules_text,
            payload=payload,
        )

    def _spell_target_names(self, action: Action, definition, actor_name: str) -> list[str]:
        if definition.target == SpellTarget.SELF:
            return [actor_name]
        raw_targets = (
            action.parameters.get("targets")
            or action.parameters.get("target_names")
            or action.parameters.get("target")
            or action.parameters.get("subject")
            or action.parameters.get("scene_object")
            or actor_name
        )
        if isinstance(raw_targets, str):
            names = [piece.strip() for piece in raw_targets.replace("，", ",").split(",") if piece.strip()]
        elif isinstance(raw_targets, list):
            names = [str(name).strip() for name in raw_targets if str(name).strip()]
        else:
            names = [str(raw_targets).strip()]
        if not names:
            names = [actor_name]
        if self._is_multi_target_spell(definition):
            return names[:3]
        return names[:1]

    def _is_multi_target_spell(self, definition) -> bool:
        return definition.target == SpellTarget.UP_TO_THREE_CREATURES

    def _resolve_scene_object_spell(
        self,
        action: Action,
        actor,
        target_name: str,
        spell_name: str | None = None,
        default_mp_cost: int = 0,
    ) -> ActionResolution:
        mp_cost = abs(int(action.parameters.get("mp_cost", default_mp_cost) or 0))
        display_name = spell_name or action.parameters.get("spell_name") or "临场魔法"
        if actor.mp < mp_cost:
            return ActionResolution(
                action=action,
                rules_text=f"{actor.name} 的 MP 不足，无法对【{target_name}】施展【{display_name}】。",
                payload={
                    "spell_failed": True,
                    "spell_name": display_name,
                    "scene_object": target_name,
                    "resource_change": ResourceChange(
                        target=actor.name,
                        resource="mp",
                        amount=0,
                        before=actor.mp,
                        after=actor.mp,
                        reason="MP 不足，法术未能发动。",
                    ),
                },
            )

        before_mp, after_mp = self.character_manager.modify_resource(actor.name, "mp", -mp_cost)
        mp_change = ResourceChange(
            target=actor.name,
            resource="mp",
            amount=-mp_cost,
            before=before_mp,
            after=after_mp,
            reason=f"对场景目标施展【{display_name}】消耗 MP。",
        )
        target_number = self._int_parameter(action.parameters, "target_number", 10, minimum=0)
        outcome = self.rules_engine.roll_check(
            actor=actor,
            attributes=action.parameters.get("attributes", ["INS", "WLP"]),
            target_number=target_number,
            modifier=self._int_parameter(action.parameters, "modifier", 0) + actor.equipment_spell_bonus,
            target=target_name,
            reason=action.parameters.get("reasoning", ""),
        )
        intention = (
            action.parameters.get("effect")
            or action.parameters.get("intended_effect")
            or action.parameters.get("reasoning")
            or "影响场景局势"
        )
        result_text = "成功" if outcome.success else "失败"
        memory = f"{actor.name} 对场景目标 {target_name} 施展【{display_name}】：{result_text}，意图为{intention}。"
        self.world_state.add_memory(memory)
        self.world_state.remember_subject_fact(target_name, memory)
        rules_text = f"魔法检定 {outcome.total}: {result_text}。"
        if outcome.success:
            rules_text += f" 【{target_name}】被魔法影响；若这是复杂目标，建议转入目标命刻或写入世界状态。"
        else:
            rules_text += f" 【{target_name}】没有被顺利影响，GM 应给出代价、延迟或威胁推进。"
        return ActionResolution(
            action=action,
            rules_text=rules_text,
            payload={
                "roll": outcome,
                "resource_change": mp_change,
                "spell_name": display_name,
                "scene_object": target_name,
                "ad_hoc_scene_spell": True,
            },
        )

    def _resolve_guard(self, action: Action) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        guarded_target = action.parameters.get("guarded_target")
        self.conflict_manager.apply_guard(actor.name, guarded_target=guarded_target)
        if guarded_target:
            rules_text = f"{actor.name} 进入防御姿态，并掩护 {guarded_target} 免受近战攻击。"
        else:
            rules_text = f"{actor.name} 进入防御姿态，本轮对所有伤害获得抵抗，对抗检定 +2。"
        return ActionResolution(
            action=action,
            rules_text=rules_text,
            payload={"guarding": True, "guarded_target": guarded_target},
        )

    def _resolve_spell_damage(self, action: Action, definition, target_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        spell_action = Action(
            action_type=ActionType.REQUEST_ROLL,
            parameters={
                **action.parameters,
                "target": target_name,
                "attributes": list(definition.attributes),
                "target_number": action.parameters.get(
                    "target_number",
                    self.character_manager.effective_defense(target_name, definition.defense_type),
                ),
                "weapon_damage": definition.fixed_damage
                + self._hero_damage_bonus(actor, is_spell=True)
                + actor.equipment_spell_damage_bonus,
                "damage_type": self._selected_damage_type(action, definition) or definition.damage_type,
                "modifier": self._int_parameter(action.parameters, "modifier", 0) + actor.equipment_spell_bonus,
                "ignore_resist": definition.ignore_resist
                or self._attack_ignores_resist(actor, self._selected_damage_type(action, definition) or definition.damage_type),
                "ignore_all_affinities": action.parameters.get("ignore_all_affinities", False)
                or actor.equipment_ignore_all_affinities,
                "spell_name": definition.name,
            },
        )
        resolution = self._resolve_roll(spell_action)
        if (
            resolution.payload["roll"].success
            and definition.drain_to is not None
            and self.character_manager.get(target_name).hp > 0
        ):
            recovered = resolution.payload["roll"].damage // 2
            before, after = self.character_manager.modify_resource(action.parameters["actor"], definition.drain_to, recovered)
            resolution.payload["drain_change"] = ResourceChange(
                target=action.parameters["actor"],
                resource=definition.drain_to,
                amount=after - before,
                before=before,
                after=after,
                reason=f"【{definition.name}】从目标处吸收资源。",
            )
            resolution.rules_text += f" {action.parameters['actor']} 恢复 {after - before} 点 {definition.drain_to.upper()}。"
        if resolution.payload["roll"].success and resolution.payload["roll"].critical_success and definition.status_effect is not None:
            applied = self.conflict_manager.apply_status(target_name, definition.status_effect)
            resolution.payload["hinder_status"] = definition.status_effect
            resolution.payload["status_applied"] = applied
            if applied:
                resolution.rules_text += f" {target_name} 陷入{self._status_name(definition.status_effect)}。"
        return resolution

    def _resolve_spell_damage_multi(self, action: Action, definition, target_names: list[str]) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        target_numbers = {
            target_name: self.character_manager.effective_defense(target_name, definition.defense_type)
            for target_name in target_names
        }
        roll = self.rules_engine.roll_check(
            actor=actor,
            attributes=list(definition.attributes),
            target_number=min(target_numbers.values()),
            modifier=self._int_parameter(action.parameters, "modifier", 0) + actor.equipment_spell_bonus,
            target="、".join(target_names),
            reason=action.parameters.get("reasoning", ""),
        )
        hit_targets = []
        if roll.critical_success:
            hit_targets = list(target_names)
        elif not roll.fumble:
            hit_targets = [name for name in target_names if roll.total >= target_numbers[name]]
        roll.success = bool(hit_targets)

        payload: dict[str, object] = {
            "roll": roll,
            "spell_name": definition.name,
            "target_numbers": target_numbers,
            "hit_targets": hit_targets,
            "damage_results": [],
        }
        rules_text = f"施法检定 {roll.total}: 命中 {len(hit_targets)}/{len(target_names)} 个目标。"
        if roll.critical_success:
            rules_text += " 触发大成功，获得 1 次机会。"
            trigger_results = self.trigger_manager.on_critical_success(actor.name)
            rules_text += self._trigger_rules_text(trigger_results)
            self._append_trigger_results(payload, trigger_results)
        if roll.fumble:
            before, after = self.character_manager.modify_resource(actor.name, "fabula_points", 1)
            payload["fabula_gain"] = ResourceChange(
                target=actor.name,
                resource="fabula_points",
                amount=1,
                before=before,
                after=after,
                reason="大失败获得 1 点物语点。",
            )
            rules_text += " 触发大失败，对手获得 1 次机会，且掷骰角色获得 1 点物语点。"
            trigger_results = self.trigger_manager.on_fumble(actor.name)
            rules_text += self._trigger_rules_text(trigger_results)
            self._append_trigger_results(payload, trigger_results)

        total_damage = 0
        damage_type = self._selected_damage_type(action, definition) or definition.damage_type
        for target_name in hit_targets:
            target = self.character_manager.get(target_name)
            next_damage_bonus = self._consume_next_damage_bonus(target.name)
            incoming_damage_bonus = self._incoming_damage_bonus(target.name)
            damage, affinity = self.rules_engine.compute_damage(
                high_roll=roll.high_roll,
                weapon_damage=definition.fixed_damage
                + self._hero_damage_bonus(actor, is_spell=True)
                + actor.equipment_spell_damage_bonus
                + next_damage_bonus
                + incoming_damage_bonus,
                damage_type=damage_type,
                target=target,
                ignore_resist=definition.ignore_resist or self._attack_ignores_resist(actor, damage_type),
                ignore_all_affinities=action.parameters.get("ignore_all_affinities", False)
                or actor.equipment_ignore_all_affinities,
            )
            if damage >= 0:
                before_hp, after_hp = self.character_manager.apply_damage(target.name, damage)
            else:
                before_hp, after_hp = self.character_manager.modify_resource(target.name, "hp", -damage)
            dealt = before_hp - after_hp if damage >= 0 else after_hp - before_hp
            total_damage += max(0, dealt)
            payload["damage_results"].append(
                {
                    "target": target.name,
                    "damage": abs(damage),
                    "damage_type": damage_type,
                    "affinity": affinity,
                    "hp_after": after_hp,
                }
            )
            self._remember_damage_outcome(actor.name, target.name, roll)
            rules_text += f" {target.name} 伤害 {damage} ({affinity.value})。"
            if roll.critical_success and definition.status_effect is not None:
                applied = self.conflict_manager.apply_status(target.name, definition.status_effect)
                if applied:
                    rules_text += f" {target.name} 陷入{self._status_name(definition.status_effect)}。"
        roll.damage = total_damage
        roll.damage_type = damage_type
        return ActionResolution(action=action, rules_text=rules_text, payload=payload)

    def _resolve_spell_mp_damage(
        self,
        action: Action,
        definition,
        target_name: str,
        mp_change: ResourceChange,
    ) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        target = self.character_manager.get(target_name)
        roll = self.rules_engine.roll_check(
            actor=actor,
            attributes=list(definition.attributes),
            target_number=action.parameters.get(
                "target_number",
                self.character_manager.effective_defense(target_name, definition.defense_type),
            ),
            modifier=self._int_parameter(action.parameters, "modifier", 0) + actor.equipment_spell_bonus,
            target=target_name,
            reason=action.parameters.get("reasoning", ""),
        )
        resource_loss = None
        resource_gain = None
        rules_text = f"检定 {roll.total} vs {roll.target_number}: {'成功' if roll.success else '失败'}。"
        payload: dict[str, object] = {"roll": roll, "resource_change": mp_change, "spell_name": definition.name}
        if roll.critical_success:
            rules_text += " 触发大成功，获得 1 次机会。"
            trigger_results = self.trigger_manager.on_critical_success(actor.name)
            self._append_trigger_results(payload, trigger_results)
            rules_text += self._trigger_rules_text(trigger_results)
        if roll.fumble:
            before, after = self.character_manager.modify_resource(actor.name, "fabula_points", 1)
            payload["fabula_gain"] = ResourceChange(
                target=actor.name,
                resource="fabula_points",
                amount=1,
                before=before,
                after=after,
                reason="大失败获得 1 点物语点。",
            )
            rules_text += " 触发大失败，对手获得 1 次机会，且掷骰角色获得 1 点物语点。"
            trigger_results = self.trigger_manager.on_fumble(actor.name)
            self._append_trigger_results(payload, trigger_results)
            rules_text += self._trigger_rules_text(trigger_results)
        if roll.success:
            loss_amount = max(0, roll.high_roll + definition.fixed_damage)
            before, after = self.character_manager.modify_resource(target_name, "mp", -loss_amount)
            roll.damage = before - after
            roll.damage_type = definition.damage_type
            resource_loss = ResourceChange(
                target=target_name,
                resource="mp",
                amount=-(before - after),
                before=before,
                after=after,
                reason=f"【{definition.name}】抽取了目标的 MP。",
            )
            rules_text += f" {target_name} 失去 {before - after} 点 MP。"
            if definition.drain_to and before > after and after > 0:
                recovered = (before - after) // 2
                gain_before, gain_after = self.character_manager.modify_resource(actor.name, definition.drain_to, recovered)
                resource_gain = ResourceChange(
                    target=actor.name,
                    resource=definition.drain_to,
                    amount=gain_after - gain_before,
                    before=gain_before,
                    after=gain_after,
                    reason=f"【{definition.name}】从目标处吸收资源。",
                )
                rules_text += f" {actor.name} 恢复 {gain_after - gain_before} 点 {definition.drain_to.upper()}。"
        payload["target_resource_change"] = resource_loss
        if resource_gain is not None:
            payload["drain_change"] = resource_gain
        return ActionResolution(action=action, rules_text=rules_text, payload=payload)

    def _resolve_spell_heal(
        self,
        action: Action,
        definition,
        target_name: str,
        mp_change: ResourceChange,
    ) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        amount = self._spell_heal_amount(action, definition, actor)
        if action.parameters.get("_vismagus_hp_payment") and target_name == actor.name:
            amount = 0
        before, after = self.character_manager.modify_resource(target_name, "hp", amount)
        change = ResourceChange(
            target=target_name,
            resource="hp",
            amount=after - before,
            before=before,
            after=after,
            reason=f"【{definition.name}】恢复生命值。",
        )
        return ActionResolution(
            action=action,
            rules_text=f"{target_name} 受到【{definition.name}】影响，规则恢复量 {amount} 点 HP；HP {before}->{after}，实际恢复 {after - before} 点。",
            payload={
                "resource_change": mp_change,
                "spell_name": definition.name,
                "healing_change": change,
                "spell_fixed_effect": {
                    "kind": "heal",
                    "base_amount": amount,
                    "actual_amount": after - before,
                    "targets": [target_name],
                },
            },
        )

    def _resolve_spell_heal_multi(
        self,
        action: Action,
        definition,
        target_names: list[str],
        mp_change: ResourceChange,
    ) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        amount = self._spell_heal_amount(action, definition, actor)
        changes = []
        for target_name in target_names:
            if action.parameters.get("_vismagus_hp_payment") and target_name == actor.name:
                effective_amount = 0
            else:
                effective_amount = amount
            before, after = self.character_manager.modify_resource(target_name, "hp", effective_amount)
            changes.append(
                ResourceChange(
                    target=target_name,
                    resource="hp",
                    amount=after - before,
                    before=before,
                    after=after,
                    reason=f"【{definition.name}】恢复生命值。",
                )
            )
        restored = "、".join(f"{change.target} 实际+{change.amount}" for change in changes)
        return ActionResolution(
            action=action,
            rules_text=f"{'、'.join(target_names)} 受到【{definition.name}】影响，规则恢复量每目标 {amount} 点 HP：{restored} HP。",
            payload={
                "resource_change": mp_change,
                "spell_name": definition.name,
                "healing_changes": changes,
                "spell_fixed_effect": {
                    "kind": "heal",
                    "base_amount": amount,
                    "actual_amounts": {change.target: change.amount for change in changes},
                    "targets": list(target_names),
                },
            },
        )

    def _spell_heal_amount(self, action: Action, definition, actor) -> int:
        if definition.name == "治愈术":
            if actor.level >= 40:
                amount = 60
            elif actor.level >= 20:
                amount = 50
            else:
                amount = definition.fixed_damage
        else:
            amount = definition.fixed_damage
        return amount + actor.equipment_healing_bonus

    def _resolve_spell_status_clear(
        self,
        action: Action,
        definition,
        target_name: str,
        mp_change: ResourceChange,
    ) -> ActionResolution:
        cleared = self.conflict_manager.clear_statuses(target_name) if definition.clear_all_statuses else False
        return ActionResolution(
            action=action,
            rules_text=f"{target_name} 受到【{definition.name}】影响，{'解除全部异常状态' if cleared else '原本没有异常状态'}。",
            payload={
                "resource_change": mp_change,
                "spell_name": definition.name,
                "statuses_cleared": cleared,
            },
        )

    def _resolve_spell_status_clear_multi(
        self,
        action: Action,
        definition,
        target_names: list[str],
        mp_change: ResourceChange,
    ) -> ActionResolution:
        cleared_targets = []
        for target_name in target_names:
            if self.conflict_manager.clear_statuses(target_name) if definition.clear_all_statuses else False:
                cleared_targets.append(target_name)
        return ActionResolution(
            action=action,
            rules_text=(
                f"{'、'.join(target_names)} 受到【{definition.name}】影响，"
                f"解除异常：{('、'.join(cleared_targets) if cleared_targets else '无')}。"
            ),
            payload={
                "resource_change": mp_change,
                "spell_name": definition.name,
                "statuses_cleared_targets": cleared_targets,
            },
        )

    def _resolve_spell_status_apply(
        self,
        action: Action,
        definition,
        target_name: str,
        mp_change: ResourceChange,
    ) -> ActionResolution:
        status = self._selected_status(action, definition)
        resolution = self._resolve_roll(
            Action(
                action_type=ActionType.REQUEST_ROLL,
                parameters={
                    **action.parameters,
                    "target": target_name,
                    "attributes": list(definition.attributes),
                    "target_number": action.parameters.get(
                        "target_number",
                        self.character_manager.effective_defense(target_name, definition.defense_type),
                    ),
                    "modifier": self._int_parameter(action.parameters, "modifier", 0)
                    + self.character_manager.get(action.parameters["actor"]).equipment_spell_bonus,
                    "non_damage": True,
                },
            )
        )
        resolution.action = action
        resolution.payload["resource_change"] = mp_change
        resolution.payload["spell_name"] = definition.name
        resolution.payload["hinder_status"] = status
        if resolution.payload["roll"].success:
            applied = self.conflict_manager.apply_status(target_name, status)
            resolution.payload["status_applied"] = applied
            if applied:
                resolution.rules_text += f" {target_name} 陷入{self._status_name(status)}。"
        return resolution

    def _resolve_spell_status_apply_multi(
        self,
        action: Action,
        definition,
        target_names: list[str],
        mp_change: ResourceChange,
    ) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        status = self._selected_status(action, definition)
        target_numbers = {
            target_name: self.character_manager.effective_defense(target_name, definition.defense_type)
            for target_name in target_names
        }
        roll = self.rules_engine.roll_check(
            actor=actor,
            attributes=list(definition.attributes),
            target_number=min(target_numbers.values()),
            modifier=self._int_parameter(action.parameters, "modifier", 0) + actor.equipment_spell_bonus,
            target="、".join(target_names),
            reason=action.parameters.get("reasoning", ""),
        )
        hit_targets = []
        if roll.critical_success:
            hit_targets = list(target_names)
        elif not roll.fumble:
            hit_targets = [name for name in target_names if roll.total >= target_numbers[name]]
        roll.success = bool(hit_targets)
        applied_targets = []
        for target_name in hit_targets:
            if self.conflict_manager.apply_status(target_name, status):
                applied_targets.append(target_name)
        rules_text = (
            f"施法检定 {roll.total}: 命中 {len(hit_targets)}/{len(target_names)} 个目标。"
            f" {self._status_name(status)}影响：{('、'.join(applied_targets) if applied_targets else '无新增目标')}。"
        )
        payload: dict[str, object] = {
            "roll": roll,
            "resource_change": mp_change,
            "spell_name": definition.name,
            "hinder_status": status,
            "target_numbers": target_numbers,
            "hit_targets": hit_targets,
            "status_applied_targets": applied_targets,
        }
        if roll.critical_success:
            rules_text += " 触发大成功，获得 1 次机会。"
            trigger_results = self.trigger_manager.on_critical_success(actor.name)
            rules_text += self._trigger_rules_text(trigger_results)
            self._append_trigger_results(payload, trigger_results)
        if roll.fumble:
            before, after = self.character_manager.modify_resource(actor.name, "fabula_points", 1)
            payload["fabula_gain"] = ResourceChange(
                target=actor.name,
                resource="fabula_points",
                amount=1,
                before=before,
                after=after,
                reason="大失败获得 1 点物语点。",
            )
            rules_text += " 触发大失败，对手获得 1 次机会，且掷骰角色获得 1 点物语点。"
            trigger_results = self.trigger_manager.on_fumble(actor.name)
            rules_text += self._trigger_rules_text(trigger_results)
            self._append_trigger_results(payload, trigger_results)
        return ActionResolution(action=action, rules_text=rules_text, payload=payload)

    def _resolve_spell_dispel(
        self,
        action: Action,
        definition,
        target_name: str,
        mp_change: ResourceChange,
    ) -> ActionResolution:
        removed_sources = self.conflict_manager.clear_spell_effects_on_target(target_name)
        if removed_sources:
            rules_text = f"{target_name} 身上的法术被驱散：{'、'.join(removed_sources)}。"
        else:
            rules_text = f"{target_name} 身上没有可被驱散的持续法术。"
        return ActionResolution(
            action=action,
            rules_text=rules_text,
            payload={
                "resource_change": mp_change,
                "spell_name": definition.name,
                "dispelled_effects": removed_sources,
            },
        )

    def _resolve_hinder(self, action: Action) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        target_name = (
            action.parameters.get("target")
            or action.parameters.get("subject")
            or action.parameters.get("scene_object")
            or "当前威胁"
        )
        target = self.character_manager.get(target_name) if self.character_manager.exists(target_name) else None
        raw_status = action.parameters.get("status_effect", StatusEffect.DAZED.value)
        try:
            status = StatusEffect(raw_status)
        except ValueError:
            status = StatusEffect.DAZED
        resolution = self._resolve_roll(
            Action(
                action_type=ActionType.REQUEST_ROLL,
                parameters={
                    **action.parameters,
                    "target": target_name,
                    "attributes": action.parameters.get("attributes", ["INS", "WLP"]),
                    "target_number": action.parameters.get("target_number", 10),
                    "non_damage": True,
                },
            )
        )
        resolution.action = action
        resolution.payload["hinder_status"] = status
        resolution.payload.pop("target_status", None)
        if resolution.payload["roll"].success:
            if target is None:
                resolution.payload["scene_object"] = target_name
                resolution.rules_text += f" {actor.name} 成功牵制或干扰了【{target_name}】。"
                self.world_state.add_memory(f"{actor.name} 干扰场景目标 {target_name}：{self._status_name(status)}式压制。")
                self.world_state.remember_subject_fact(target_name, f"被 {actor.name} 干扰或牵制。")
                return resolution
            applied = self.conflict_manager.apply_status(target.name, status)
            resolution.payload["status_applied"] = applied
            if applied:
                resolution.rules_text += f" {target.name} 陷入了{self._status_name(status)}。"
                if actor.name in self.world_state.npc_personas:
                    self.world_state.remember_npc_event(actor.name, f"成功让 {target.name} 陷入{self._status_name(status)}。")
            else:
                resolution.rules_text += f" {target.name} 已经处于{self._status_name(status)}。"
        return resolution

    def _resolve_investigate(self, action: Action) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        target_name = (
            action.parameters.get("target")
            or action.parameters.get("subject")
            or action.parameters.get("scene_object")
            or "当前线索"
        )
        if not self.character_manager.exists(target_name):
            return self._resolve_scene_object_investigation(action, actor, target_name)
        target = self.character_manager.get(target_name)
        outcome = self.rules_engine.roll_check(
            actor=actor,
            attributes=action.parameters.get("attributes", ["INS", "INS"]),
            target_number=7,
            modifier=self._int_parameter(action.parameters, "modifier", 0),
            target=target.name,
            reason=action.parameters.get("reasoning", ""),
        )
        information = []
        if outcome.total >= 7:
            species = self._species_text(target)
            information.append(f"等级/物种：{target.level}级，{species}")
            information.append(f"最大 HP/MP：{target.max_hp}，{target.max_mp}")
        if outcome.total >= 10:
            attributes_text = "，".join(f"{name} d{size}" for name, size in target.attributes.items())
            traits_text = "、".join(target.traits) or "未记录特质"
            affinities_text = self._affinities_text(target)
            information.append(f"属性骰：{attributes_text}")
            information.append(f"特质：{traits_text}")
            information.append(
                f"物防/魔防：{self.character_manager.effective_defense(target.name, 'physical')}/"
                f"{self.character_manager.effective_defense(target.name, 'magic')}"
            )
            information.append(f"相性：{affinities_text}")
        if outcome.total >= 13:
            attack_attributes = getattr(target, "weapon_accuracy_attributes", ["DEX", "MIG"])
            attack_modifier = getattr(target, "weapon_accuracy_modifier", 0)
            attack_modifier_text = f"+{attack_modifier}" if attack_modifier >= 0 else str(attack_modifier)
            basic_attack = (
                f"{target.equipped_main_hand or '基础攻击'} "
                f"{'/'.join(attack_attributes)} {attack_modifier_text}，"
                f"【HR+{target.weapon_damage}】{self._damage_type_text(target.weapon_type)}"
            )
            abilities_text = "、".join(target.abilities) or "未记录特殊技能"
            spells_text = "、".join(target.spells) or "未记录法术"
            information.append(f"基础攻击：{basic_attack}")
            information.append(f"技能：{abilities_text}")
            information.append(f"法术：{spells_text}")

        if information:
            joined = "；".join(information)
            self.world_state.add_memory(f"{actor.name} 调查 {target.name}：{joined}")
            self.world_state.remember_subject_fact(target.name, joined)
            if actor.name in self.world_state.npc_personas:
                self.world_state.remember_npc_event(actor.name, f"侦知 {target.name}：{joined}")

        rules_text = f"调查检定 {outcome.total}: {'成功' if outcome.total >= 7 else '失败'}。"
        if information:
            rules_text += " 获取了敌人的关键信息。"
        return ActionResolution(
            action=action,
            rules_text=rules_text,
            payload={"roll": outcome, "information": information},
        )

    def _resolve_scene_object_investigation(self, action: Action, actor, target_name: str) -> ActionResolution:
        target_number = self._int_parameter(action.parameters, "target_number", 7, minimum=0)
        outcome = self.rules_engine.roll_check(
            actor=actor,
            attributes=action.parameters.get("attributes", ["INS", "INS"]),
            target_number=target_number,
            modifier=self._int_parameter(action.parameters, "modifier", 0),
            target=target_name,
            reason=action.parameters.get("reasoning", ""),
        )
        information: list[str] = []
        provided_clues = action.parameters.get("clues") or action.parameters.get("information") or []
        if isinstance(provided_clues, str):
            provided_clues = [provided_clues]
        if outcome.total >= 7:
            information.append(f"{target_name} 是场景物件或线索目标，不是已建档敌人；可用调查、目标行动、开箱或叙事交互继续处理。")
        if outcome.total >= 10:
            if provided_clues:
                information.append("线索：" + "；".join(str(item) for item in provided_clues[:2] if str(item).strip()))
            else:
                information.append(f"{target_name} 与当前场景目标有关，适合建立命刻或触发一个明确抉择。")
        if outcome.total >= 13:
            detail = str(action.parameters.get("discovered_detail") or "").strip()
            information.append(detail or f"你发现了 {target_name} 的可利用细节：它可以被安全接触，但最好先说明代价或风险。")

        if information:
            joined = "；".join(information)
            self.world_state.add_memory(f"{actor.name} 调查场景物件 {target_name}：{joined}")
            self.world_state.remember_subject_fact(target_name, joined)

        rules_text = f"调查检定 {outcome.total}: {'成功' if outcome.total >= target_number else '失败'}。"
        if information:
            rules_text += " 获取了场景线索。"
        return ActionResolution(
            action=action,
            rules_text=rules_text,
            payload={"roll": outcome, "information": information, "scene_object": target_name},
        )

    def _resolve_objective(self, action: Action) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        clock_name = action.parameters.get("clock_name") or action.parameters.get("target") or "当前目标命刻"
        target_number = self._int_parameter(action.parameters, "target_number", 10, minimum=0)
        if target_number <= 0:
            target_number = 10
        resolution = self._resolve_roll(
            Action(
                action_type=ActionType.REQUEST_ROLL,
                parameters={
                    **action.parameters,
                    "target": action.parameters.get("target", clock_name),
                    "clock_name": clock_name,
                    "target_number": target_number,
                    "non_damage": True,
                },
            )
        )
        resolution.action = action
        if action.parameters.get("cooperative_progress"):
            resolution.payload["cooperative_progress"] = True
        clock_change = resolution.payload.get("clock_change")
        if clock_change is not None and actor.name in self.world_state.npc_personas:
            self.world_state.remember_npc_event(
                actor.name,
                f"将命刻【{clock_change.clock_name}】推进到 {clock_change.after}/{clock_change.max_segments}。",
            )
        return resolution

    def _resolve_skill(self, action: Action) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        skill_name = self._normalized_skill_name(action.parameters["skill_name"])
        if not self._actor_has_skill(actor, skill_name):
            raise ValueError(f"{actor.name} 尚未拥有技能【{skill_name}】。")

        handlers = {
            "暗影击": self._resolve_shadow_strike,
            "薄情者": self._resolve_heartbreaker,
            "挑衅": self._resolve_provoke,
            "谴责": self._resolve_condemn,
            "鼓舞": self._resolve_encourage,
            "窃取时间": self._resolve_stolen_time,
            "窃取灵魂": self._resolve_soul_steal,
            "回见了您呐": self._resolve_see_you_later,
            "碎骨": self._resolve_bone_crusher,
            "威慑射击": self._resolve_warning_shot,
            "破防打击": self._resolve_breach,
            "挺身守护": self._resolve_protect,
            "快速评估": self._resolve_quick_assessment,
            "意外盟友": self._resolve_unexpected_ally,
            "卸甲真言": self._resolve_disarming_rhetoric,
            "我算到了": self._resolve_predictable,
            "消失": self._resolve_vanish,
            "希望": self._resolve_hope,
            "火山": self._resolve_volcano,
            "彗星": self._resolve_comet,
            "弹幕射击": self._resolve_barrage,
            "利刃风暴": self._resolve_bladestorm,
            "契约与召唤": self._resolve_bind_and_summon,
        }
        if skill_name in handlers:
            return handlers[skill_name](action, skill_name)
        return self._resolve_pending_skill(action, skill_name)

    def _resolve_bind_and_summon(self, action: Action, skill_name: str) -> ActionResolution:
        mode = str(action.parameters.get("mode", "summon")).lower()
        if mode in {"dismiss", "release", "解除", "解除阿卡纳", "遣散", "遣散奥灵", "释放", "解放"} or action.parameters.get("dismiss"):
            return self._resolve_dismiss_arcanum(action, skill_name)
        return self._resolve_summon_arcanum(action, skill_name)

    def _resolve_summon_arcanum(self, action: Action, skill_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        arcanum = self._normalize_arcanum_name(
            action.parameters.get("arcanum")
            or action.parameters.get("arcanum_name")
            or action.parameters.get("target")
            or "锻造"
        )
        if actor.active_arcanum:
            return ActionResolution(
                action=action,
                rules_text=f"{actor.name} 已经与【{self._arcanum_display(actor.active_arcanum)}】融合，必须先遣散当前奥灵。",
                payload={"skill_name": skill_name, "skill_failed": True, "active_arcanum": actor.active_arcanum},
            )

        bound_arcana = {self._normalize_arcanum_name(name) for name in actor.bound_arcana}
        if bound_arcana and arcanum not in bound_arcana:
            return ActionResolution(
                action=action,
                rules_text=f"{actor.name} 尚未与【{self._arcanum_display(arcanum)}】结契，无法召唤。",
                payload={"skill_name": skill_name, "skill_failed": True, "requested_arcanum": arcanum},
            )
        if not actor.bound_arcana:
            if not (action.parameters.get("initial_contract") or action.parameters.get("allow_auto_bind")):
                return ActionResolution(
                    action=action,
                    rules_text=f"{actor.name} 尚未记录任何已结契奥灵。请先在角色创建或剧情中记录 bound_arcana，再召唤【{self._arcanum_display(arcanum)}】。",
                    payload={"skill_name": skill_name, "skill_failed": True, "requested_arcanum": arcanum},
                )
            actor.bound_arcana.append(arcanum)

        mp_cost = 40
        emergency_rank = skill_rank(actor.skills, "险境召唤")
        if actor.in_crisis and emergency_rank > 0:
            mp_cost = max(0, mp_cost - emergency_rank * 5)
        mp_change = self._spend_mp_or_fail(action, actor.name, mp_cost, f"召唤【{self._arcanum_display(arcanum)}】。")
        if isinstance(mp_change, ActionResolution):
            return mp_change

        actor.active_arcanum = arcanum
        effect_key = self._arcanum_effect_key(arcanum)
        self.conflict_manager.register_effect(
            TimedEffect(
                owner=actor.name,
                effect_type="arcanum_link",
                expires_on=EffectTiming.SCENE_END,
                target=actor.name,
                source=f"奥灵：{self._arcanum_display(arcanum)}",
                effect_key=effect_key,
                data={"arcanum": arcanum},
                note="奥灵融合持续到场景结束、角色失去意识、离开场景或主动遣散。",
            )
        )
        effect_notes = self._register_arcanum_link_effects(actor.name, arcanum, action, effect_key)

        healing_change = None
        regen_rank = skill_rank(actor.skills, "奥灵疗愈")
        if regen_rank > 0:
            before, after = self.character_manager.modify_resource(actor.name, "hp", regen_rank * 5)
            healing_change = ResourceChange(actor.name, "hp", after - before, before, after, "奥灵疗愈。")

        rules_text = (
            f"{actor.name} 消耗 {abs(mp_change.amount)} MP 召唤【{self._arcanum_display(arcanum)}】，"
            f"获得融合增益：{'；'.join(effect_notes) if effect_notes else '叙事性融合增益'}。"
        )
        if healing_change and healing_change.amount > 0:
            rules_text += f" 【奥灵疗愈】恢复 {healing_change.amount} HP。"

        return ActionResolution(
            action=action,
            rules_text=rules_text,
            payload={
                "skill_name": skill_name,
                "arcanum": arcanum,
                "arcanum_display": self._arcanum_display(arcanum),
                "resource_change": mp_change,
                "healing_change": healing_change,
                "link_effects": effect_notes,
            },
        )

    def _resolve_dismiss_arcanum(self, action: Action, skill_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        if not actor.active_arcanum:
            return ActionResolution(
                action=action,
                rules_text=f"{actor.name} 当前没有正在融合的奥灵。",
                payload={"skill_name": skill_name, "skill_failed": True},
            )
        arcanum = self._normalize_arcanum_name(
            action.parameters.get("arcanum")
            or action.parameters.get("arcanum_name")
            or actor.active_arcanum
        )
        if arcanum != actor.active_arcanum:
            return ActionResolution(
                action=action,
                rules_text=f"{actor.name} 当前融合的是【{self._arcanum_display(actor.active_arcanum)}】，不是【{self._arcanum_display(arcanum)}】。",
                payload={"skill_name": skill_name, "skill_failed": True, "active_arcanum": actor.active_arcanum},
            )

        removed_effect_key = self._arcanum_effect_key(arcanum)
        self.conflict_manager.clear_effects(actor.name, effect_key=removed_effect_key)
        option = str(action.parameters.get("option", "")).strip().lower()
        dismiss_result = self._apply_arcanum_dismiss_effect(actor.name, arcanum, option, action)
        rules_text = (
            f"{actor.name} 遣散【{self._arcanum_display(arcanum)}】。"
            f"{dismiss_result['rules_text']}"
        )
        return ActionResolution(
            action=action,
            rules_text=rules_text,
            payload={
                "skill_name": skill_name,
                "arcanum": arcanum,
                "arcanum_display": self._arcanum_display(arcanum),
                **dismiss_result,
            },
        )

    def _register_arcanum_link_effects(self, actor_name: str, arcanum: str, action: Action, effect_key: str) -> list[str]:
        notes: list[str] = []

        def register(effect_type: str, target: str, data: dict, note: str) -> None:
            self.conflict_manager.register_effect(
                TimedEffect(
                    owner=actor_name,
                    effect_type=effect_type,
                    expires_on=EffectTiming.SCENE_END,
                    target=target,
                    source=f"奥灵：{self._arcanum_display(arcanum)}",
                    effect_key=effect_key,
                    data=data,
                    note=note,
                )
            )

        def resist(target: str, *damage_types: str) -> None:
            register(
                "affinity_buff",
                target,
                {"affinity_changes": {damage_type: Affinity.RESIST for damage_type in damage_types}},
                "奥灵赋予伤害抗性。",
            )

        def immune(target: str, *statuses: StatusEffect) -> None:
            register(
                "status_immunity",
                target,
                {"status_immunities": list(statuses)},
                "奥灵赋予异常免疫。",
            )

        if arcanum == "锻造":
            resist(actor_name, "fire")
            notes.append("火系抗性；你造成的火系伤害无视抗性")
        elif arcanum == "霜":
            resist(actor_name, "ice")
            immune(actor_name, StatusEffect.ENRAGED)
            notes.append("冰系抗性、激怒免疫；你造成的冰系伤害无视抗性")
        elif arcanum == "门":
            resist(actor_name, "dark")
            register("defense_bonus", actor_name, {"defense_bonus": {"magic": 1}}, "门径奥灵提高魔防。")
            notes.append("暗系抗性；魔法防御 +1")
        elif arcanum == "魔典":
            register("attribute_buff", actor_name, {"attribute_bonus": {"INS": 1}}, "魔典奥灵提高洞察骰级。")
            notes.append("理解所有语言；洞察骰级 +1")
        elif arcanum == "橡树":
            resist(actor_name, "poison")
            immune(actor_name, StatusEffect.POISONED)
            notes.append("毒系抗性；中毒免疫；恢复 HP 时额外恢复 5 点")
        elif arcanum == "天空":
            resist(actor_name, "wind", "lightning")
            notes.append("风系与雷系抗性；可用动作准确预测两旅行日内天气")
        elif arcanum == "剑":
            notes.append("攻击额外造成 5 点伤害，且攻击伤害变为无属性")
        elif arcanum == "塔":
            damage_type = self._selected_arcanum_damage_type(action, default="fire")
            targets = self._arcanum_ally_targets(actor_name, action)
            for target in targets:
                resist(target, damage_type)
            notes.append(f"除你以外的盟友获得{self._damage_type_text(damage_type)}抗性")
        elif arcanum == "轮":
            immune(actor_name, StatusEffect.SLOW)
            register("defense_bonus", actor_name, {"defense_bonus": {"physical": 1}}, "轮之奥灵提高物防。")
            notes.append("迟缓免疫；防御 +1")
        return notes

    def _apply_arcanum_dismiss_effect(self, actor_name: str, arcanum: str, option: str, action: Action) -> dict:
        if arcanum == "锻造" and option in {"forge", "锻造", "create", "创造"}:
            item_name = action.parameters.get("item_name") or action.parameters.get("created_item") or "火焰基础装备"
            self.world_state.add_memory(f"{actor_name} 遣散熔炉奥灵，创造了【{item_name}】。")
            return {
                "rules_text": f"解除效果【锻造】：创造一件基础装备【{item_name}】；若为武器，其伤害类型为火系。",
                "created_item": item_name,
            }

        if arcanum == "门" and option in {"warp", "折跃", "teleport", "传送"}:
            destination = action.parameters.get("destination", "一旅行日内曾到访地点")
            travelers = action.parameters.get("targets") or [actor_name]
            self.world_state.add_memory(f"{actor_name} 遣散门径奥灵，将 {', '.join(travelers)} 传送到：{destination}。")
            return {
                "rules_text": f"解除效果【折跃】：{', '.join(travelers)} 被传送至 {destination}。",
                "destination": destination,
                "travelers": travelers,
            }

        if arcanum == "魔典":
            question = action.parameters.get("question", "玩家提出一个问题")
            self.world_state.add_memory(f"{actor_name} 遣散魔典奥灵并询问神谕：{question}")
            return {
                "rules_text": f"解除效果【神谕】：GM 必须如实回答问题“{question}”。",
                "oracle_question": question,
            }

        if arcanum == "橡树":
            amount = 60 if self.character_manager.get(actor_name).level >= 40 else 50 if self.character_manager.get(actor_name).level >= 20 else 40
            targets = self._arcanum_targets(action, default_allies=True, actor_name=actor_name)
            changes = []
            for target_name in targets:
                self.conflict_manager.remove_status(target_name, StatusEffect.POISONED)
                before, after = self.character_manager.modify_resource(target_name, "hp", amount)
                changes.append(ResourceChange(target_name, "hp", after - before, before, after, "橡树奥灵遣散效果。"))
            return {
                "rules_text": f"解除效果【开花】：{', '.join(targets)} 解除中毒并恢复 {amount} HP。",
                "target_resource_changes": changes,
                "cleared_status": StatusEffect.POISONED,
            }

        if arcanum == "轮":
            targets = self._arcanum_targets(action, default_allies=False, actor_name=actor_name)
            events = []
            for target_name in targets:
                target = self.character_manager.get(target_name)
                if StatusEffect.SLOW in target.statuses:
                    events.append({"target": target_name, "effect": "next_turn_action_minus_1"})
                else:
                    applied = self.conflict_manager.apply_status(target_name, StatusEffect.SLOW)
                    events.append({"target": target_name, "effect": "slow", "applied": applied})
            return {
                "rules_text": f"解除效果【时间冻结】：{', '.join(targets)} 受到迟缓；已迟缓者下回合动作 -1。",
                "status_events": events,
            }

        damage_map = {
            "锻造": ("fire", "炼狱"),
            "霜": ("ice", "冰雪时代"),
            "门": ("dark", "湮灭"),
            "天空": ("lightning", "雷暴"),
            "塔": ("light", "审判"),
        }
        if arcanum in damage_map:
            damage_type, label = damage_map[arcanum]
            amount = self._arcanum_damage_amount(actor_name)
            targets = self._arcanum_targets(action, default_allies=False, actor_name=actor_name)
            changes = []
            for target_name in targets:
                before, after, affinity = self._apply_fixed_damage(
                    target_name,
                    amount,
                    damage_type,
                    ignore_resist=True,
                )
                changes.append(ResourceChange(target_name, "hp", after - before, before, after, f"{label}造成固定伤害。"))
                if after == 0:
                    self.conflict_manager.resolve_zero_hp(target_name)
            return {
                "rules_text": f"解除效果【{label}】：{', '.join(targets)} 各承受 {amount} 点{self._damage_type_text(damage_type)}伤害，无视抗性。",
                "target_resource_changes": changes,
            }

        return {"rules_text": "解除效果已记录为叙事性效果，未改动数值。"}

    def _normalize_arcanum_name(self, raw_name) -> str:
        text = str(raw_name or "").strip(" ：:「」『』【】[]")
        if text in self.ARCANUM_ALIASES:
            return self.ARCANUM_ALIASES[text]
        lower = text.lower()
        if lower in self.ARCANUM_ALIASES:
            return self.ARCANUM_ALIASES[lower]
        raise ValueError(f"未知奥灵：{text}")

    def _arcanum_display(self, arcanum: str) -> str:
        return self.ARCANUM_DISPLAY_NAMES.get(arcanum, arcanum)

    def _arcanum_effect_key(self, arcanum: str) -> str:
        return f"arcanum:{arcanum}"

    def _selected_arcanum_damage_type(self, action: Action, default: str = "fire") -> str:
        aliases = {
            "风": "wind",
            "风系": "wind",
            "雷": "lightning",
            "电": "lightning",
            "雷系": "lightning",
            "暗": "dark",
            "暗系": "dark",
            "土": "earth",
            "土系": "earth",
            "火": "fire",
            "火系": "fire",
            "冰": "ice",
            "冰系": "ice",
        }
        raw = action.parameters.get("damage_type") or action.parameters.get("chosen_damage_type") or default
        return aliases.get(str(raw), str(raw))

    def _arcanum_ally_targets(self, actor_name: str, action: Action) -> list[str]:
        explicit = action.parameters.get("targets")
        if isinstance(explicit, list) and explicit:
            return [name for name in explicit if self.character_manager.exists(name)]
        return [
            character.name
            for character in self.character_manager.all()
            if character.name != actor_name and "pc" in character.traits
        ]

    def _arcanum_targets(self, action: Action, *, default_allies: bool, actor_name: str) -> list[str]:
        raw_targets = action.parameters.get("targets", action.parameters.get("target"))
        if isinstance(raw_targets, str) and raw_targets:
            targets = [raw_targets]
        elif isinstance(raw_targets, list) and raw_targets:
            targets = list(raw_targets)
        elif default_allies:
            targets = [character.name for character in self.character_manager.all() if "pc" in character.traits]
        else:
            targets = [character.name for character in self.character_manager.all() if "enemy" in character.traits or "villain" in character.traits]
        return [target for target in targets if self.character_manager.exists(target)]

    def _arcanum_damage_amount(self, actor_name: str) -> int:
        actor = self.character_manager.get(actor_name)
        if actor.level >= 40:
            return 50
        if actor.level >= 20:
            return 40
        return 30

    def _resolve_shadow_strike(self, action: Action, skill_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        target_name = self._target_name(action)
        if not self.character_manager.exists(target_name):
            return self._resolve_scene_target_skill(action, skill_name, actor, target_name)
        rank = self._skill_rank(actor, skill_name)
        hp_roll = self.rules_engine.roll_die(actor.attributes["MIG"])
        hp_before, hp_after = self.character_manager.modify_resource(actor.name, "hp", -hp_roll)
        hp_change = ResourceChange(actor.name, "hp", hp_after - hp_before, hp_before, hp_after, "暗影击消耗生命力。")
        if hp_after == 0:
            event = self.conflict_manager.resolve_zero_hp(actor.name)
            return ActionResolution(
                action=action,
                rules_text=f"{actor.name} 以【暗影击】燃尽生命力，但 HP 降至 0，无法完成攻击。{event.summary}",
                payload={"skill_name": skill_name, "hp_change": hp_change, "conflict_event": event},
            )

        resolution = self._resolve_attack(
            Action(
                action_type=ActionType.ATTACK,
                parameters={
                    **action.parameters,
                    "actor": actor.name,
                    "target": target_name,
                    "attributes": action.parameters.get("attributes", actor.weapon_accuracy_attributes),
                    "damage_type": "dark",
                    "weapon_damage": actor.weapon_damage + rank + hp_roll,
                    "is_melee": action.parameters.get("is_melee", actor.weapon_range != "ranged"),
                },
            )
        )
        resolution.action = action
        resolution.payload["skill_name"] = skill_name
        resolution.payload["hp_change"] = hp_change
        resolution.rules_text = f"{actor.name} 消耗 {hp_before - hp_after} HP 发动【暗影击】。{resolution.rules_text}"
        return resolution

    def _resolve_heartbreaker(self, action: Action, skill_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        target_name = self._target_name(action)
        if not self.character_manager.exists(target_name):
            return self._resolve_scene_target_skill(action, skill_name, actor, target_name)
        bond_strength = actor.bond_strength_with(target_name)
        hp_cost = actor.hp // 2
        hp_before, hp_after = self.character_manager.modify_resource(actor.name, "hp", -hp_cost)
        resolution = self._resolve_attack(
            Action(
                action_type=ActionType.ATTACK,
                parameters={
                    **action.parameters,
                    "actor": actor.name,
                    "target": target_name,
                    "attributes": action.parameters.get("attributes", actor.weapon_accuracy_attributes),
                    "weapon_damage": actor.weapon_damage + 10 * bond_strength,
                    "is_melee": action.parameters.get("is_melee", actor.weapon_range != "ranged"),
                },
            )
        )
        resolution.action = action
        resolution.payload["skill_name"] = skill_name
        resolution.payload["hp_change"] = ResourceChange(
            actor.name,
            "hp",
            hp_after - hp_before,
            hp_before,
            hp_after,
            "薄情者消耗当前 HP 的一半。",
        )
        resolution.rules_text = f"{actor.name} 消耗 {hp_cost} HP 发动【薄情者】，羁绊强度 {bond_strength}。{resolution.rules_text}"
        return resolution

    def _resolve_provoke(self, action: Action, skill_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        target_name = self._target_name(action, "当前威胁")
        if not self.character_manager.exists(target_name):
            return self._resolve_scene_target_skill(action, skill_name, actor, target_name)
        target = self.character_manager.get(target_name)
        rank = self._skill_rank(actor, skill_name)
        mp_change = self._spend_mp_or_fail(action, actor.name, 5, f"发动【{skill_name}】。")
        if isinstance(mp_change, ActionResolution):
            return mp_change
        opposed = self.rules_engine.roll_opposed_check(actor, target, ["MIG", "WLP"], left_modifier=rank)
        success = opposed.winner == actor.name
        status_applied = False
        if success:
            status_applied = self.conflict_manager.apply_status(target.name, StatusEffect.ENRAGED)
            self.conflict_manager.register_effect(
                TimedEffect(
                    owner=actor.name,
                    effect_type="target_focus",
                    expires_on=EffectTiming.SCENE_END,
                    target=target.name,
                    source=skill_name,
                    effect_key=f"skill:{skill_name}:{target.name}",
                    data={"must_include_target": actor.name},
                    note=f"{target.name} 必须尽可能把 {actor.name} 纳入攻击或攻击法术目标。",
                )
            )
        return ActionResolution(
            action=action,
            rules_text=(
                f"{actor.name} 发动【{skill_name}】：对抗检定 {opposed.left_roll.total} vs {opposed.right_roll.total}，"
                f"{'成功' if success else '失败'}。"
            ),
            payload={
                "skill_name": skill_name,
                "resource_change": mp_change,
                "opposed_check": opposed,
                "status_applied": status_applied,
                "hinder_status": StatusEffect.ENRAGED,
            },
        )

    def _resolve_condemn(self, action: Action, skill_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        target_name = self._target_name(action, "当前目标")
        if not self.character_manager.exists(target_name):
            return self._resolve_scene_target_skill(action, skill_name, actor, target_name)
        target = self.character_manager.get(target_name)
        rank = self._skill_rank(actor, skill_name)
        mp_change = self._spend_mp_or_fail(action, actor.name, 5, f"发动【{skill_name}】。")
        if isinstance(mp_change, ActionResolution):
            return mp_change
        opposed = self.rules_engine.roll_opposed_check(actor, target, ["INS", "WLP"], left_modifier=rank)
        payload: dict[str, object] = {"skill_name": skill_name, "resource_change": mp_change, "opposed_check": opposed}
        rules_text = (
            f"{actor.name} 发动【{skill_name}】：对抗检定 {opposed.left_roll.total} vs {opposed.right_roll.total}，"
            f"{'成功' if opposed.winner == actor.name else '失败'}。"
        )
        if opposed.winner == actor.name:
            before, after = self.character_manager.modify_resource(target.name, "mp", -(rank * 10))
            status = StatusEffect(action.parameters.get("status_effect", StatusEffect.SHAKEN.value))
            applied = self.conflict_manager.apply_status(target.name, status)
            damage_bonus_effect = TimedEffect(
                owner=actor.name,
                effect_type="incoming_damage_bonus",
                expires_on=EffectTiming.OWNER_TURN_START,
                target=target.name,
                source=skill_name,
                effect_key=f"skill:{skill_name}:{target.name}:incoming_damage_bonus",
                data={"damage_bonus": rank},
                note=f"直到 {actor.name} 的下个回合开始，任何伤害来源对 {target.name} 额外造成 {rank} 点伤害。",
            )
            self.conflict_manager.register_effect(damage_bonus_effect)
            payload["target_resource_change"] = ResourceChange(
                target.name,
                "mp",
                after - before,
                before,
                after,
                "谴责使目标失去 MP。",
            )
            payload["hinder_status"] = status
            payload["status_applied"] = applied
            payload["skill_effect"] = damage_bonus_effect
            rules_text += (
                f" {target.name} 失去 {before - after} MP，并受到{self._status_name(status)}；"
                f"直到 {actor.name} 下个回合开始，任何伤害来源对其额外造成 {rank} 点伤害。"
            )
        return ActionResolution(action=action, rules_text=rules_text, payload=payload)

    def _resolve_encourage(self, action: Action, skill_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        target_name = action.parameters.get("target", actor.name)
        rank = self._skill_rank(actor, skill_name)
        mp_change = self._spend_mp_or_fail(action, actor.name, 5, f"发动【{skill_name}】。")
        if isinstance(mp_change, ActionResolution):
            return mp_change
        before, after = self.character_manager.modify_resource(target_name, "hp", rank * 10)
        attribute = action.parameters.get("chosen_attribute", "WLP").upper()
        effect = TimedEffect(
            owner=actor.name,
            effect_type="attribute_buff",
            expires_on=EffectTiming.OWNER_TURN_START,
            target=target_name,
            source=skill_name,
            effect_key=f"skill:{skill_name}:{target_name}:{attribute}",
            data={"attribute_bonus": {attribute: 1}},
            note="鼓舞使目标的一项属性骰提升到游说家下回合开始。",
        )
        self.conflict_manager.register_effect(effect)
        return ActionResolution(
            action=action,
            rules_text=f"{actor.name} 发动【{skill_name}】，{target_name} 恢复 {after - before} HP，{attribute} 临时提升 1 阶。",
            payload={
                "skill_name": skill_name,
                "resource_change": mp_change,
                "healing_change": ResourceChange(target_name, "hp", after - before, before, after, "鼓舞恢复 HP。"),
                "skill_effect": effect,
            },
        )

    def _resolve_stolen_time(self, action: Action, skill_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        rank = self._skill_rank(actor, skill_name)
        mp_cost = self._int_parameter(action.parameters, "mp_cost", 5)
        if mp_cost <= 0 or mp_cost % 5 != 0 or mp_cost > rank * 5:
            raise ValueError(f"【{skill_name}】的 MP 消耗必须是 5 的倍数，且不超过 SL x 5。")
        options = action.parameters.get("options") or [action.parameters.get("option", "apply_slow")]
        if len(options) > mp_cost // 5:
            raise ValueError(f"【{skill_name}】本次最多选择 {mp_cost // 5} 个效果。")
        mp_change = self._spend_mp_or_fail(action, actor.name, mp_cost, f"发动【{skill_name}】。")
        if isinstance(mp_change, ActionResolution):
            return mp_change
        effects = []
        for option in options:
            target_name = action.parameters.get("target", actor.name)
            if option == "apply_slow":
                effects.append(f"{target_name} 陷入迟缓" if self.conflict_manager.apply_status(target_name, StatusEffect.SLOW) else f"{target_name} 已迟缓")
            elif option == "clear_slow":
                effects.append(f"{target_name} 解除迟缓" if self.conflict_manager.remove_status(target_name, StatusEffect.SLOW) else f"{target_name} 没有迟缓")
            elif option == "free_equip":
                effects.append(f"{target_name} 可立即执行一次免费装备动作")
            elif option == "ally_next":
                ally = action.parameters.get("ally", target_name)
                self.conflict_manager.grant_bonus_turn(ally)
                effects.append(f"{ally} 获得一个紧随其后的奖励回合")
            elif option in {"hp_loss", "lose_hp", "slow_damage", "damage", "缓慢失血", "失去生命"}:
                damage_amount = 10 + rank * 5
                before, after = self.character_manager.apply_damage(target_name, damage_amount)
                effects.append(f"{target_name} 缓慢失去 {before - after} HP")
                if after == 0:
                    self.conflict_manager.resolve_zero_hp(target_name)
            else:
                raise ValueError(f"未知的【{skill_name}】选项：{option}")
        return ActionResolution(
            action=action,
            rules_text=f"{actor.name} 发动【{skill_name}】：{'；'.join(effects)}。",
            payload={"skill_name": skill_name, "resource_change": mp_change, "effects": effects},
        )

    def _resolve_soul_steal(self, action: Action, skill_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        target_name = self._target_name(action, "当前灵魂回路")
        if not self.character_manager.exists(target_name):
            return self._resolve_scene_target_skill(
                action,
                skill_name,
                actor,
                target_name,
                summary=f"{actor.name} 以【{skill_name}】触碰并读取场景中的灵魂痕迹【{target_name}】。",
            )
        target = self.character_manager.get(target_name)
        rank = self._skill_rank(actor, skill_name)
        roll = self.rules_engine.roll_check(
            actor=actor,
            attributes=["DEX", "WLP"],
            target_number=self.character_manager.effective_defense(target.name, "magic"),
            modifier=rank,
            target=target.name,
            reason=skill_name,
        )
        payload: dict[str, object] = {"skill_name": skill_name, "roll": roll}
        rules_text = f"{actor.name} 发动【{skill_name}】：检定 {roll.total} vs {roll.target_number}，{'成功' if roll.success else '失败'}。"
        if roll.success:
            rank_type = self.conflict_manager.state.enemy_ranks.get(target.name, EnemyRank.SOLDIER)
            if rank_type == EnemyRank.SOLDIER:
                before, after = self._restore_inventory_points(actor.name, rank)
                payload["resource_change"] = ResourceChange(actor.name, "inventory_points", after - before, before, after, "窃取灵魂恢复 IP。")
                rules_text += f" {actor.name} 恢复 {after - before} 点物资点。"
            else:
                multiplier = 50 if self.conflict_manager.is_villain(target.name) else 30
                value = target.level * multiplier
                payload["soul_treasure"] = {"target": target.name, "max_value": value}
                rules_text += f" 获得一件来自 {target.name} 的灵魂宝藏，价值上限 {value}Z。"
        return ActionResolution(action=action, rules_text=rules_text, payload=payload)

    def _resolve_see_you_later(self, action: Action, skill_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        fabula_change = self._spend_fabula_or_fail(action, actor.name, 1, f"发动【{skill_name}】。")
        if isinstance(fabula_change, ActionResolution):
            return fabula_change
        if self.conflict_manager.state.active:
            self.conflict_manager.remove_combatant_from_scene(actor.name)
        return ActionResolution(
            action=action,
            rules_text=f"{actor.name} 消耗 1 点物语点发动【{skill_name}】，从当前场景消失。",
            payload={"skill_name": skill_name, "resource_change": fabula_change},
        )

    def _resolve_bone_crusher(self, action: Action, skill_name: str) -> ActionResolution:
        return self._resolve_control_attack(
            action,
            skill_name,
            selectable_statuses=(StatusEffect.DAZED, StatusEffect.WEAKENED),
            default_status=StatusEffect.WEAKENED,
            mp_loss_per_rank=10,
            is_melee=True,
        )

    def _resolve_warning_shot(self, action: Action, skill_name: str) -> ActionResolution:
        return self._resolve_control_attack(
            action,
            skill_name,
            selectable_statuses=(StatusEffect.SHAKEN, StatusEffect.SLOW),
            default_status=StatusEffect.SHAKEN,
            mp_loss_per_rank=10,
            is_melee=False,
        )

    def _resolve_breach(self, action: Action, skill_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        target_name = self._target_name(action, "当前障碍")
        if not self.character_manager.exists(target_name):
            return self._resolve_scene_target_skill(action, skill_name, actor, target_name)
        target = self.character_manager.get(target_name)
        rank = self._skill_rank(actor, skill_name)
        mp_change = self._spend_mp_or_fail(action, actor.name, 5, f"发动【{skill_name}】。")
        if isinstance(mp_change, ActionResolution):
            return mp_change
        roll_resolution = self._resolve_roll(
            Action(
                action_type=ActionType.REQUEST_ROLL,
                parameters={
                    **action.parameters,
                    "actor": actor.name,
                    "target": target.name,
                    "attributes": action.parameters.get("attributes", actor.weapon_accuracy_attributes),
                    "target_number": self.character_manager.effective_defense(target.name, "physical"),
                    "modifier": self._int_parameter(action.parameters, "modifier", 0) + self._weapon_mastery_bonus(actor, True),
                    "non_damage": True,
                },
            )
        )
        roll_resolution.action = action
        roll_resolution.payload["skill_name"] = skill_name
        roll_resolution.payload["resource_change"] = mp_change
        if roll_resolution.payload["roll"].success:
            option = action.parameters.get("option", "next_damage_bonus")
            if option == "destroy_shield":
                destroyed = target.equipped_shield
                target.equipped_shield = ""
                roll_resolution.rules_text += f" {target.name} 的盾牌被破坏。"
                roll_resolution.payload["destroyed_equipment"] = destroyed
            elif option == "destroy_armor":
                destroyed = target.equipped_armor
                target.equipped_armor = "无防具"
                roll_resolution.rules_text += f" {target.name} 的护甲被破坏。"
                roll_resolution.payload["destroyed_equipment"] = destroyed
            else:
                effect = TimedEffect(
                    owner=target.name,
                    effect_type="next_damage_bonus",
                    expires_on=EffectTiming.OWNER_TURN_START,
                    target=target.name,
                    source=skill_name,
                    effect_key=f"skill:{skill_name}:{target.name}",
                    data={"damage_bonus": rank * 2},
                    note="目标下回合开始前第一次受到伤害时额外承受伤害。",
                )
                self.conflict_manager.register_effect(effect)
                roll_resolution.payload["skill_effect"] = effect
                roll_resolution.rules_text += f" {target.name} 下次受到伤害时额外承受 {rank * 2} 点伤害。"
        return roll_resolution

    def _resolve_protect(self, action: Action, skill_name: str) -> ActionResolution:
        guarded_target = self._target_name(action, action.parameters["actor"])
        resolution = self._resolve_guard(
            Action(action_type=ActionType.GUARD, parameters={"actor": action.parameters["actor"], "guarded_target": guarded_target})
        )
        resolution.action = action
        resolution.payload["skill_name"] = skill_name
        resolution.rules_text = f"【{skill_name}】{resolution.rules_text}"
        return resolution

    def _resolve_quick_assessment(self, action: Action, skill_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        target_name = self._target_name(action, "当前线索")
        if not self.character_manager.exists(target_name):
            return self._resolve_scene_object_investigation(
                Action(
                    action_type=ActionType.INVESTIGATE,
                    parameters={
                        **action.parameters,
                        "actor": actor.name,
                        "target": target_name,
                        "attributes": ["INS", "INS"],
                        "target_number": action.parameters.get("target_number", 7),
                    },
                ),
                actor,
                target_name,
            )
        target = self.character_manager.get(target_name)
        rank = self._skill_rank(actor, skill_name)
        mp_cost = self._int_parameter(action.parameters, "mp_cost", 5)
        if mp_cost <= 0 or mp_cost % 5 != 0 or mp_cost > rank * 5:
            raise ValueError("【快速评估】的 MP 消耗必须是 5 的倍数，且不超过 SL x 5。")
        mp_change = self._spend_mp_or_fail(action, actor.name, mp_cost, f"发动【{skill_name}】。")
        if isinstance(mp_change, ActionResolution):
            return mp_change
        reveals = []
        choices = mp_cost // 5
        if choices >= 1:
            reveals.append(f"特征：{'、'.join(target.traits) or '无记录'}")
        for damage_type in action.parameters.get("damage_types", [])[: max(0, choices - 1)]:
            affinity = target.temporary_affinities.get(damage_type, target.affinities.get(damage_type, Affinity.NORMAL))
            reveals.append(f"{self._damage_type_text(damage_type)}相性：{affinity.value}")
        return ActionResolution(
            action=action,
            rules_text=f"{actor.name} 发动【{skill_name}】，获得情报：{'；'.join(reveals)}。",
            payload={"skill_name": skill_name, "resource_change": mp_change, "information": reveals},
        )

    def _resolve_unexpected_ally(self, action: Action, skill_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        fabula_change = self._spend_fabula_or_fail(action, actor.name, 1, f"发动【{skill_name}】。")
        if isinstance(fabula_change, ActionResolution):
            return fabula_change
        ally = self._target_name(action, "意外盟友")
        fact = action.parameters.get("fact", f"{ally} 愿意在合理范围内帮助 {actor.name} 与小队。")
        self.world_state.apply_story_fact(fact)
        return ActionResolution(
            action=action,
            rules_text=f"{actor.name} 发动【{skill_name}】，让 {ally} 成为意外盟友。",
            payload={"skill_name": skill_name, "resource_change": fabula_change, "fact": fact},
        )

    def _resolve_disarming_rhetoric(self, action: Action, skill_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        target_name = self._target_name(action, "当前敌人")
        if not self.character_manager.exists(target_name):
            return self._resolve_scene_target_skill(action, skill_name, actor, target_name)
        target = self.character_manager.get(target_name)
        rank_type = self.conflict_manager.state.enemy_ranks.get(target.name, EnemyRank.SOLDIER)
        if rank_type != EnemyRank.SOLDIER:
            raise ValueError("【卸甲真言】只能选择士兵级别生物。")
        if StatusEffect.SHAKEN not in target.statuses and not target.in_crisis:
            raise ValueError("【卸甲真言】要求目标处于动摇或危机状态。")
        cost = 20 + target.level // 2
        mp_change = self._spend_mp_or_fail(action, actor.name, cost, f"发动【{skill_name}】。")
        if isinstance(mp_change, ActionResolution):
            return mp_change
        self.conflict_manager.remove_combatant_from_scene(target.name)
        return ActionResolution(
            action=action,
            rules_text=f"{actor.name} 发动【{skill_name}】，说服 {target.name} 和平离开冲突。",
            payload={"skill_name": skill_name, "resource_change": mp_change},
        )

    def _resolve_predictable(self, action: Action, skill_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        target_name = self._target_name(action, "当前目标")
        mp_change = self._spend_mp_or_fail(action, actor.name, 20, f"发动【{skill_name}】。")
        if isinstance(mp_change, ActionResolution):
            return mp_change
        predicted_action = action.parameters.get("predicted_action", "Attack")
        effect = TimedEffect(
            owner=actor.name,
            effect_type="action_tax",
            expires_on=EffectTiming.OWNER_TURN_START,
            target=target_name,
            source=skill_name,
            effect_key=f"skill:{skill_name}:{target_name}",
            data={"predicted_action": predicted_action, "mp_tax": 20},
            note=f"{target_name} 若执行 {predicted_action}，需要额外消耗 20 MP。",
        )
        self.conflict_manager.register_effect(effect)
        return ActionResolution(
            action=action,
            rules_text=f"{actor.name} 发动【{skill_name}】，预判 {target_name} 的 {predicted_action} 动作。",
            payload={"skill_name": skill_name, "resource_change": mp_change, "skill_effect": effect},
        )

    def _resolve_vanish(self, action: Action, skill_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        fabula_change = self._spend_fabula_or_fail(action, actor.name, 1, f"发动【{skill_name}】。")
        if isinstance(fabula_change, ActionResolution):
            return fabula_change
        effect = TimedEffect(
            owner=actor.name,
            effect_type="hidden",
            expires_on=EffectTiming.OWNER_TURN_START,
            target=actor.name,
            source=skill_name,
            effect_key=f"skill:{skill_name}:{actor.name}",
            note="敌人无法执行需要看见该角色的行动。",
        )
        self.conflict_manager.register_effect(effect)
        return ActionResolution(
            action=action,
            rules_text=f"{actor.name} 消耗 1 点物语点发动【{skill_name}】，直到下回合开始前从敌人视野中消失。",
            payload={"skill_name": skill_name, "resource_change": fabula_change, "skill_effect": effect},
        )

    def _resolve_hope(self, action: Action, skill_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        target_name = self._target_name(action, actor.name)
        if not self.character_manager.exists(target_name):
            return self._resolve_scene_target_skill(action, skill_name, actor, target_name)
        target = self.character_manager.get(target_name)
        mp_change = self._spend_mp_or_fail(action, actor.name, 40, f"施放英雄法术【{skill_name}】。")
        if isinstance(mp_change, ActionResolution):
            return mp_change
        recovery = target.crisis_threshold if target.crisis_threshold else target.max_hp // 2
        before, after = self.character_manager.modify_resource(target.name, "hp", recovery)
        self.conflict_manager.state.defeated_combatants.discard(target.name)
        self.conflict_manager.state.fallen_pcs.pop(target.name, None)
        if self.conflict_manager.state.active and target.name not in self.conflict_manager.state.turn_order:
            self.conflict_manager.state.turn_order.append(target.name)
        return ActionResolution(
            action=action,
            rules_text=f"{actor.name} 施放【{skill_name}】，{target.name} 恢复意识并恢复 {after - before} HP。",
            payload={
                "skill_name": skill_name,
                "resource_change": mp_change,
                "healing_change": ResourceChange(target.name, "hp", after - before, before, after, "希望恢复倒下的英雄。"),
            },
        )

    def _resolve_volcano(self, action: Action, skill_name: str) -> ActionResolution:
        bonus = 10 if self.character_manager.get(action.parameters["actor"]).level >= 40 else 5 if self.character_manager.get(action.parameters["actor"]).level >= 20 else 0
        return self._resolve_fixed_damage_hero_spell(action, skill_name, mp_cost=40, base_single=50 + bonus, base_multi=30 + bonus, damage_type="fire", ignore_resist_and_immune=True)

    def _resolve_comet(self, action: Action, skill_name: str) -> ActionResolution:
        bonus = 10 if self.character_manager.get(action.parameters["actor"]).level >= 40 else 5 if self.character_manager.get(action.parameters["actor"]).level >= 20 else 0
        return self._resolve_fixed_damage_hero_spell(action, skill_name, mp_cost=50, base_single=60 + bonus, base_multi=40 + bonus, damage_type="none", ignore_resist_and_immune=True)

    def _resolve_barrage(self, action: Action, skill_name: str) -> ActionResolution:
        return self._resolve_multi_attack_skill(action, skill_name, is_melee=False)

    def _resolve_bladestorm(self, action: Action, skill_name: str) -> ActionResolution:
        return self._resolve_multi_attack_skill(action, skill_name, is_melee=True)

    def _resolve_multi_attack_skill(self, action: Action, skill_name: str, *, is_melee: bool) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        targets = self._attack_target_names(action)
        if len(targets) > 3:
            raise ValueError(f"【{skill_name}】当前最多支持 3 个目标。")
        mp_change = self._spend_mp_or_fail(action, actor.name, 10, f"发动【{skill_name}】。")
        if isinstance(mp_change, ActionResolution):
            return mp_change
        resolution = self._resolve_attack_window(
            Action(
                action_type=ActionType.ATTACK,
                parameters={
                    **action.parameters,
                    "actor": actor.name,
                    "targets": targets,
                    "attributes": action.parameters.get("attributes", actor.weapon_accuracy_attributes),
                    "is_melee": is_melee,
                },
            )
        )
        resolution.action = action
        resolution.payload["skill_name"] = skill_name
        resolution.payload["resource_change"] = mp_change
        resolution.rules_text = f"{actor.name} 消耗 10 MP 发动【{skill_name}】。{resolution.rules_text}"
        return resolution

    def _resolve_control_attack(
        self,
        action: Action,
        skill_name: str,
        *,
        selectable_statuses: tuple[StatusEffect, ...],
        default_status: StatusEffect,
        mp_loss_per_rank: int,
        is_melee: bool,
    ) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        target_name = self._target_name(action, "当前目标")
        if not self.character_manager.exists(target_name):
            return self._resolve_scene_target_skill(action, skill_name, actor, target_name)
        target = self.character_manager.get(target_name)
        rank = self._skill_rank(actor, skill_name)
        roll_resolution = self._resolve_roll(
            Action(
                action_type=ActionType.REQUEST_ROLL,
                parameters={
                    **action.parameters,
                    "actor": actor.name,
                    "target": target.name,
                    "attributes": action.parameters.get("attributes", actor.weapon_accuracy_attributes),
                    "target_number": self.character_manager.effective_defense(target.name, "physical"),
                    "modifier": self._int_parameter(action.parameters, "modifier", 0) + self._weapon_mastery_bonus(actor, is_melee),
                    "non_damage": True,
                },
            )
        )
        roll_resolution.action = action
        roll_resolution.payload["skill_name"] = skill_name
        if roll_resolution.payload["roll"].success:
            option = action.parameters.get("option", "status")
            if option == "mp_loss":
                before, after = self.character_manager.modify_resource(target.name, "mp", -(rank * mp_loss_per_rank))
                roll_resolution.payload["target_resource_change"] = ResourceChange(
                    target.name,
                    "mp",
                    after - before,
                    before,
                    after,
                    f"【{skill_name}】使目标失去 MP。",
                )
                roll_resolution.rules_text += f" {target.name} 失去 {before - after} MP。"
            else:
                raw_status = action.parameters.get("status_effect")
                status = StatusEffect(raw_status) if raw_status is not None else default_status
                if status not in selectable_statuses:
                    raise ValueError(f"【{skill_name}】不能施加该状态。")
                applied = self.conflict_manager.apply_status(target.name, status)
                roll_resolution.payload["hinder_status"] = status
                roll_resolution.payload["status_applied"] = applied
                roll_resolution.rules_text += f" {target.name} 受到{self._status_name(status)}。"
        return roll_resolution

    def _resolve_fixed_damage_hero_spell(
        self,
        action: Action,
        skill_name: str,
        *,
        mp_cost: int,
        base_single: int,
        base_multi: int,
        damage_type: str,
        ignore_resist_and_immune: bool = False,
    ) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        mp_change = self._spend_mp_or_fail(action, actor.name, mp_cost, f"施放英雄法术【{skill_name}】。")
        if isinstance(mp_change, ActionResolution):
            return mp_change
        targets = list(action.parameters.get("targets") or [self._target_name(action)])
        targets = [target for target in targets if self.character_manager.exists(target)]
        if not targets:
            return self._resolve_scene_target_skill(action, skill_name, actor, self._target_name(action))
        amount = base_multi if len(targets) > 1 else base_single
        changes = []
        for target_name in targets:
            before, after, affinity = self._apply_fixed_damage(
                target_name,
                amount,
                damage_type,
                ignore_resist_and_immune=ignore_resist_and_immune,
            )
            changes.append(ResourceChange(target_name, "hp", after - before, before, after, f"【{skill_name}】造成固定伤害。"))
            if after == 0:
                self.conflict_manager.resolve_zero_hp(target_name)
        target_text = "、".join(targets)
        return ActionResolution(
            action=action,
            rules_text=f"{actor.name} 施放【{skill_name}】，{target_text} 各承受 {amount} 点{self._damage_type_text(damage_type)}伤害。",
            payload={"skill_name": skill_name, "resource_change": mp_change, "target_resource_changes": changes},
        )

    def _resolve_pending_skill(self, action: Action, skill_name: str) -> ActionResolution:
        coverage = skill_implementation_coverage(skill_name)
        note = coverage.implementation_note if coverage else "技能已识别，但尚未归类到覆盖表。"
        return ActionResolution(
            action=action,
            rules_text=(
                f"技能【{skill_name}】已识别，覆盖状态："
                f"{coverage.category if coverage else 'unknown'}。{note}"
                "当前不会自动改动数值。"
            ),
            payload={
                "skill_name": skill_name,
                "skill_pending": True,
                "coverage_category": coverage.category if coverage else "unknown",
                "coverage_note": note,
            },
        )

    def _resolve_roll(self, action: Action) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        target_name = self._target_name(action)
        target_exists = self.character_manager.exists(target_name)
        target = self.character_manager.get(target_name) if target_exists else actor
        teamwork_bonus, teamwork_payload = self._declared_teamwork_bonus(action, actor)
        advantage_bonus = self._consume_advantage_bonus(actor.name)
        outcome = self.rules_engine.roll_check(
            actor=actor,
            attributes=action.parameters.get("attributes", ["INS", "WLP"]),
            target_number=self._int_parameter(action.parameters, "target_number", 10, minimum=0),
            modifier=self._int_parameter(action.parameters, "modifier", 0) + teamwork_bonus + advantage_bonus,
            target=target_name,
            reason=action.parameters.get("reasoning", ""),
        )
        invocation_notes: list[str] = []
        invocation_payload: dict[str, object] = {}
        if action.parameters.get("invoke_trait") or action.parameters.get("trait_name") or action.parameters.get("invoke_bond_target") or action.parameters.get("bond_target"):
            outcome, invocation_notes, invocation_payload = self._apply_declared_invocations(action, outcome, actor)
        self._remember_roll(outcome)

        rules_text = f"检定 {outcome.total} vs {outcome.target_number}: {'成功' if outcome.success else '失败'}。"
        payload: dict[str, object] = {"roll": outcome}
        if advantage_bonus:
            payload["advantage_bonus"] = advantage_bonus
            rules_text += f" 机会【优势】提供 +{advantage_bonus} 修正。"
        if teamwork_payload:
            payload["conflict_teamwork"] = teamwork_payload
            rules_text += f" 团队合作提供 +{teamwork_payload['total_bonus']} 修正。"
        if invocation_payload:
            payload.update(invocation_payload)
            rules_text += " " + " ".join(invocation_notes)

        if outcome.critical_success:
            rules_text += " 触发大成功，获得 1 次机会。"
            trigger_results = self.trigger_manager.on_critical_success(actor.name)
            rules_text += self._trigger_rules_text(trigger_results)
            self._append_trigger_results(payload, trigger_results)
        if outcome.fumble:
            before, after = self.character_manager.modify_resource(actor.name, "fabula_points", 1)
            payload["fabula_gain"] = ResourceChange(
                target=actor.name,
                resource="fabula_points",
                amount=1,
                before=before,
                after=after,
                reason="大失败获得 1 点物语点。",
            )
            rules_text += " 触发大失败，对手获得 1 次机会，且掷骰角色获得 1 点物语点。"
            trigger_results = self.trigger_manager.on_fumble(actor.name)
            rules_text += self._trigger_rules_text(trigger_results)
            self._append_trigger_results(payload, trigger_results)

        if outcome.success and not action.parameters.get("non_damage", False) and not target_exists:
            payload["scene_object"] = target_name
            rules_text += f" 【{target_name}】不是已建档角色，本次检定只记录场景影响，不执行 HP 伤害结算。"
            self.world_state.add_memory(f"{actor.name} 对场景目标 {target_name} 的检定成功。")
            self.world_state.remember_subject_fact(target_name, f"被 {actor.name} 成功影响。")
        if outcome.success and not action.parameters.get("non_damage", False) and target_exists:
            next_damage_bonus = self._consume_next_damage_bonus(target.name)
            incoming_damage_bonus = self._incoming_damage_bonus(target.name)
            damage, affinity = self.rules_engine.compute_damage(
                high_roll=outcome.high_roll,
                weapon_damage=action.parameters.get("weapon_damage", actor.weapon_damage)
                + next_damage_bonus
                + incoming_damage_bonus,
                damage_type=action.parameters.get(
                    "damage_type",
                    self.character_manager.effective_weapon_damage_type(actor.name),
                ),
                target=target,
                ignore_resist=action.parameters.get("ignore_resist", False),
                ignore_all_affinities=action.parameters.get("ignore_all_affinities", False),
            )
            if next_damage_bonus:
                payload["next_damage_bonus"] = next_damage_bonus
            if incoming_damage_bonus:
                payload["incoming_damage_bonus"] = incoming_damage_bonus
            if damage >= 0:
                _, after = self.character_manager.apply_damage(target.name, damage)
            else:
                _, after = self.character_manager.modify_resource(target.name, "hp", -damage)
            outcome.damage = abs(damage)
            outcome.damage_type = action.parameters.get(
                "damage_type",
                self.character_manager.effective_weapon_damage_type(actor.name),
            )
            outcome.applied_affinity = affinity
            outcome.hp_after = after
            payload["target_status"] = self.character_manager.format_status(self.character_manager.get(target.name))
            rules_text += f" 伤害 {damage} ({affinity.value})."
            self._remember_damage_outcome(actor.name, target.name, outcome)
            self._apply_on_hit_status(action, target.name, payload)
            hit_trigger_results = self.trigger_manager.after_hit(
                actor.name,
                target.name,
                is_spell=bool(action.parameters.get("spell_name")),
                is_critical=outcome.critical_success,
                target_was_zero_hp=after == 0,
            )
            if hit_trigger_results:
                after = self.character_manager.get(target.name).hp
                outcome.hp_after = after
                payload["target_status"] = self.character_manager.format_status(self.character_manager.get(target.name))
                rules_text += self._trigger_rules_text(hit_trigger_results)
                self._append_trigger_results(payload, hit_trigger_results)

            if after == 0:
                zero_hp_trigger_results = self.trigger_manager.before_zero_hp(target.name)
                if zero_hp_trigger_results:
                    after = self.character_manager.get(target.name).hp
                    outcome.hp_after = after
                    payload["target_status"] = self.character_manager.format_status(self.character_manager.get(target.name))
                    rules_text += self._trigger_rules_text(zero_hp_trigger_results)
                    self._append_trigger_results(payload, zero_hp_trigger_results)
                if after == 0 and self.conflict_manager.prevent_zero_hp_once(target.name):
                    outcome.hp_after = 1
                    payload["conflict_event"] = self.conflict_event_survive_once(target.name)
                    payload["target_status"] = self.character_manager.format_status(self.character_manager.get(target.name))
                    rules_text += f" {payload['conflict_event'].summary}"
                    return ActionResolution(action=action, rules_text=rules_text, payload=payload)
                if after == 0:
                    defeat_event = self.conflict_manager.resolve_zero_hp(
                        target=target.name,
                        pc_choice=action.parameters.get("pc_zero_hp_choice", "give_up_resistance"),
                        pc_consequence=action.parameters.get("pc_consequence", "被俘虏并失去重要装备"),
                        villain_mode=action.parameters.get("villain_zero_hp_mode", "auto"),
                        allow_escalation=action.parameters.get("allow_escalation", True),
                        sacrifice_benefits_bond=action.parameters.get("sacrifice_benefits_bond"),
                        sacrifice_betters_world=action.parameters.get("sacrifice_betters_world"),
                    )
                    if defeat_event.hp_after is not None:
                        outcome.hp_after = defeat_event.hp_after
                    payload["conflict_event"] = defeat_event
                    payload["target_status"] = self.character_manager.format_status(self.character_manager.get(target.name))
                    rules_text += f" {defeat_event.summary}"
        if outcome.success and action.parameters.get("clock_name"):
            clock_name = action.parameters["clock_name"]
            self._ensure_clock_exists(action, clock_name, default_clock_type="objective")
            clock = self.clock_manager.get(clock_name)
            delta = self.rules_engine.clock_segments_from_roll(
                outcome,
                spend_critical_opportunity=bool(
                    action.parameters.get("spend_critical_opportunity_on_clock", False)
                ),
            ) * action.parameters.get("clock_direction", 1)
            corrected_threat_direction = False
            if (
                clock.clock_type == "threat"
                and delta > 0
                and "pc" in actor.traits
                and not action.parameters.get("allow_advance_threat_on_success", False)
            ):
                delta = -delta
                corrected_threat_direction = True
            before, after = self.clock_manager.advance(clock_name, delta)
            payload["clock_change"] = ClockChange(
                clock_name=clock.name,
                before=before,
                after=after,
                delta=delta,
                max_segments=clock.max_segments,
                reason="玩家成功压制威胁命刻。" if corrected_threat_direction else "检定成功改变命刻。",
            )
            if corrected_threat_direction:
                payload["clock_direction_corrected"] = True
            if delta >= 0:
                rules_text += f" 命刻 [{clock.name}] 推进 {delta} 格。"
            else:
                rules_text += f" 命刻 [{clock.name}] 擦除 {abs(delta)} 格。"
        elif not outcome.success and action.parameters.get("threat_clock_name"):
            clock_name = action.parameters["threat_clock_name"]
            self._ensure_clock_exists(action, clock_name, default_clock_type="threat", prefix="threat_clock_")
            if "threat_clock_delta" in action.parameters:
                delta = self._int_parameter(action.parameters, "threat_clock_delta", 1, minimum=0)
            else:
                delta = self.rules_engine.threat_clock_segments_from_roll(
                    outcome,
                    spend_fumble_opportunity=bool(
                        action.parameters.get("spend_fumble_opportunity_on_threat_clock", False)
                    ),
                )
            before, after = self.clock_manager.advance(clock_name, delta)
            clock = self.clock_manager.get(clock_name)
            payload["clock_change"] = ClockChange(
                clock_name=clock.name,
                before=before,
                after=after,
                delta=delta,
                max_segments=clock.max_segments,
                reason="检定失败推进威胁命刻。",
            )
            rules_text += f" 威胁命刻 [{clock.name}] 推进 {delta} 格。"
        return ActionResolution(action=action, rules_text=rules_text, payload=payload)

    def _append_trigger_results(self, payload: dict[str, object], trigger_results: list) -> None:
        if trigger_results:
            payload.setdefault("trigger_results", []).extend(trigger_results)

    def _trigger_rules_text(self, trigger_results: list) -> str:
        if not trigger_results:
            return ""
        return " " + " ".join(result.summary for result in trigger_results)

    def _resolve_resource(self, action: Action) -> ActionResolution:
        target_name = self._target_name(action, str(action.parameters.get("actor") or "系统"))
        if not self.character_manager.exists(target_name):
            self.world_state.remember_subject_fact(target_name, f"资源动作被识别，但 {target_name} 不是角色实体。")
            return ActionResolution(
                action=action,
                rules_text=f"资源修改目标【{target_name}】不是已建档角色，未改动数值，已作为场景记录保留。",
                payload={"resource_skipped": True, "scene_object": target_name},
            )
        before, after = self.character_manager.modify_resource(
            target_name,
            action.parameters["resource"],
            action.parameters["amount"],
        )
        change = ResourceChange(
            target=target_name,
            resource=action.parameters["resource"],
            amount=action.parameters["amount"],
            before=before,
            after=after,
            reason=action.parameters.get("reason", ""),
        )
        return ActionResolution(
            action=action,
            rules_text=f"{change.target} 的 {change.resource} 从 {change.before} 变为 {change.after}。",
            payload={"resource_change": change},
        )

    def _resolve_clock(self, action: Action) -> ActionResolution:
        clock_name = action.parameters["clock_name"]
        created = self._ensure_clock_exists(
            action,
            clock_name,
            default_clock_type=str(action.parameters.get("clock_type") or "objective"),
        )
        delta = self._int_parameter(action.parameters, "delta", 0)
        before, after = self.clock_manager.advance(clock_name, delta)
        clock = self.clock_manager.get(clock_name)
        change = ClockChange(
            clock_name=clock.name,
            before=before,
            after=after,
            delta=delta,
            max_segments=clock.max_segments,
            reason=action.parameters.get("reason", ""),
        )
        created_text = "创建并" if created else ""
        return ActionResolution(
            action=action,
            rules_text=f"{created_text}命刻 [{clock.name}] 从 {before}/{clock.max_segments} 变为 {after}/{clock.max_segments}。",
            payload={"clock_change": change},
        )

    def _ensure_clock_exists(
        self,
        action: Action,
        clock_name: str,
        *,
        default_clock_type: str,
        prefix: str = "",
    ) -> bool:
        if self.clock_manager.exists(clock_name):
            return False
        max_segments = self._first_int_parameter(
            action.parameters,
            [f"{prefix}max_segments", f"{prefix}segments", "max_segments", "segments"],
            6,
            minimum=1,
            maximum=99,
        )
        current = self._first_int_parameter(
            action.parameters,
            [f"{prefix}current", "current"],
            0,
            minimum=0,
            maximum=max_segments,
        )
        self.clock_manager.add(
            Clock(
                name=clock_name,
                max_segments=max_segments,
                current=max(0, min(max_segments, current)),
                clock_type=str(action.parameters.get(f"{prefix}clock_type") or action.parameters.get("clock_type") or default_clock_type),
                stakes=str(action.parameters.get(f"{prefix}stakes") or action.parameters.get("stakes") or ""),
                gm_note=str(action.parameters.get(f"{prefix}gm_note") or action.parameters.get("gm_note") or ""),
                auto_advance=str(action.parameters.get(f"{prefix}auto_advance") or action.parameters.get("auto_advance") or ""),
            )
        )
        return True

    def _resolve_story_change(self, action: Action) -> ActionResolution:
        target = action.parameters.get("target") or self._default_story_change_target()
        if not target:
            return ActionResolution(
                action=action,
                rules_text="没有找到可支付物语点的玩家角色，故事改写暂未生效。",
                payload={"story_change_failed": True, "reason": "缺少支付物语点的玩家角色。"},
            )
        cost = -abs(action.parameters.get("fabula_cost", action.parameters.get("cost", 1)))
        before, after = self.character_manager.modify_resource(target, "fabula_points", cost)
        fact = (
            action.parameters.get("fact")
            or action.parameters.get("story_change")
            or action.parameters.get("new_fact")
            or action.parameters.get("description")
            or action.parameters.get("content")
        )
        if not fact:
            fact = "玩家消耗物语点，为当前场景加入了一个有利的新故事元素。"
        self.world_state.apply_story_fact(fact)
        return ActionResolution(
            action=action,
            rules_text=f"{target} 消耗 1 点物语点 ({before} -> {after})，世界设定已更新。",
            payload={"fact": fact},
        )

    def _default_story_change_target(self) -> str:
        pcs = [character for character in self.character_manager.all() if "pc" in character.traits]
        if pcs:
            pcs.sort(key=lambda character: character.fabula_points, reverse=True)
            return pcs[0].name
        characters = self.character_manager.all()
        if not characters:
            return ""
        characters.sort(key=lambda character: character.fabula_points, reverse=True)
        return characters[0].name

    def _with_attack_infusion(self, action: Action) -> tuple[Action, TinkererGadgetResult]:
        actor = self.character_manager.get(action.parameters["actor"])
        infusion_name = action.parameters.get("infusion_name") or action.parameters.get("infusion") or "焦火"
        infusion_result = self.gadget_manager.prepare_infusion(actor.name, infusion_name)
        damage_type, bonus = self.gadget_manager.infusion_effect(infusion_name)
        parameters = dict(action.parameters)
        parameters.pop("infusion", None)
        parameters.pop("infusion_name", None)
        parameters["damage_type"] = damage_type
        parameters["weapon_damage"] = parameters.get("weapon_damage", actor.weapon_damage) + bonus
        if str(infusion_name).strip().lower() in {"毒液", "venom"}:
            parameters["post_hit_status"] = StatusEffect.POISONED.value
        return Action(action_type=action.action_type, parameters=parameters), infusion_result

    def _attach_gadget_result(
        self,
        resolution: ActionResolution,
        gadget_result: TinkererGadgetResult | None,
    ) -> ActionResolution:
        if gadget_result is None:
            return resolution
        resolution.payload["gadget_result"] = gadget_result
        resolution.rules_text = f"{gadget_result.summary} {resolution.rules_text}"
        return resolution

    def _apply_on_hit_status(self, action: Action, target_name: str, payload: dict[str, object]) -> None:
        raw_status = action.parameters.get("post_hit_status") or action.parameters.get("status_effect_on_hit")
        if not raw_status and action.parameters.get("actor") and self.character_manager.exists(action.parameters["actor"]):
            raw_status = self.character_manager.get(action.parameters["actor"]).equipment_on_hit_status
        if not raw_status:
            return
        status = raw_status if isinstance(raw_status, StatusEffect) else StatusEffect(raw_status)
        applied = self.conflict_manager.apply_status(target_name, status)
        payload.setdefault("on_hit_statuses", {})[target_name] = status
        payload.setdefault("status_applied_on_hit", {})[target_name] = applied

    def _uses_attack_window(self, action: Action) -> bool:
        targets = action.parameters.get("targets")
        target = action.parameters.get("target")
        has_multiple_targets = isinstance(targets, list) and len(targets) > 1
        target_is_list = isinstance(target, list) and len(target) > 1
        return has_multiple_targets or target_is_list or bool(action.parameters.get("reactions")) or bool(action.parameters.get("reaction"))

    def _attack_target_names(self, action: Action) -> list[str]:
        raw_targets = action.parameters.get("targets", action.parameters.get("target"))
        if raw_targets is None:
            return []
        if isinstance(raw_targets, str):
            return [raw_targets]
        return list(raw_targets)

    def _declared_reactions(self, action: Action) -> list[dict]:
        reactions = action.parameters.get("reactions", [])
        if action.parameters.get("reaction"):
            reactions = [action.parameters["reaction"], *list(reactions)]
        return [reaction for reaction in reactions if isinstance(reaction, dict)]

    def _available_attack_reactions(self, action: Action, roll, actual_targets: list, is_melee: bool) -> list[dict]:
        available = []
        if not is_melee and not roll.critical_success and not roll.fumble:
            for character in self.character_manager.all():
                if character.name == action.parameters["actor"]:
                    continue
                cost = 5 + max(0, roll.total)
                if skill_rank(character.skills, "干涉火力") > 0 and character.weapon_range == "ranged" and character.mp >= cost:
                    available.append(
                        {
                            "actor": character.name,
                            "skill_name": "干涉火力",
                            "mp_cost": cost,
                            "effect": "让本次远程攻击自动失败。",
                        }
                    )
        if is_melee and roll.total % 2 == 0:
            for target in actual_targets:
                if skill_rank(target.skills, "反击") > 0 and target.hp > 0:
                    available.append(
                        {
                            "actor": target.name,
                            "skill_name": "反击",
                            "effect": f"在攻击结算后对 {action.parameters['actor']} 进行一次 HR 视为 0 的近战反击。",
                        }
                    )
        return available

    def _apply_crossfire_reactions(self, action: Action, roll, is_melee: bool) -> tuple[bool, list[dict]]:
        events = []
        if is_melee:
            return False, events
        for reaction in self._declared_reactions(action):
            if self._normalized_skill_name(reaction.get("skill_name", "")) != "干涉火力":
                continue
            reactor = self.character_manager.get(reaction["actor"])
            if skill_rank(reactor.skills, "干涉火力") <= 0:
                events.append({"actor": reactor.name, "skill_name": "干涉火力", "cancelled": False, "reason": "未拥有技能。"})
                continue
            if reactor.weapon_range != "ranged":
                events.append({"actor": reactor.name, "skill_name": "干涉火力", "cancelled": False, "reason": "没有装备远程武器。"})
                continue
            if roll.critical_success:
                events.append({"actor": reactor.name, "skill_name": "干涉火力", "cancelled": False, "reason": "大成功不能被干涉火力取消。"})
                continue
            if roll.fumble:
                events.append({"actor": reactor.name, "skill_name": "干涉火力", "cancelled": False, "reason": "攻击已经大失败。"})
                continue
            cost = 5 + max(0, roll.total)
            if reactor.mp < cost:
                events.append({"actor": reactor.name, "skill_name": "干涉火力", "cancelled": False, "reason": "MP 不足。"})
                continue
            before, after = self.character_manager.modify_resource(reactor.name, "mp", -cost)
            change = ResourceChange(reactor.name, "mp", after - before, before, after, "干涉火力取消远程攻击。")
            events.append(
                {
                    "actor": reactor.name,
                    "skill_name": "干涉火力",
                    "cancelled": True,
                    "resource_change": change,
                    "rules_text": f"{reactor.name} 发动【干涉火力】，消耗 {cost} MP，使本次远程攻击自动失败。",
                }
            )
            return True, events
        return False, events

    def _apply_counter_reactions(self, action: Action, roll, actual_targets: list, is_melee: bool) -> list[dict]:
        events = []
        if not is_melee or roll.total % 2 != 0:
            return events
        actual_target_names = {target.name for target in actual_targets}
        attacker = self.character_manager.get(action.parameters["actor"])
        for reaction in self._declared_reactions(action):
            if self._normalized_skill_name(reaction.get("skill_name", "")) != "反击":
                continue
            reactor = self.character_manager.get(reaction["actor"])
            if reactor.name not in actual_target_names:
                events.append({"actor": reactor.name, "skill_name": "反击", "triggered": False, "reason": "该角色不是本次近战攻击的目标。"})
                continue
            if skill_rank(reactor.skills, "反击") <= 0:
                events.append({"actor": reactor.name, "skill_name": "反击", "triggered": False, "reason": "未拥有技能。"})
                continue
            counter_roll = self.rules_engine.roll_check(
                actor=reactor,
                attributes=reaction.get("attributes", reactor.weapon_accuracy_attributes),
                target_number=self.character_manager.effective_defense(attacker.name, "physical"),
                modifier=reaction.get("modifier", 0) + self._weapon_mastery_bonus(reactor, True),
                target=attacker.name,
                reason="反击",
            )
            if counter_roll.fumble:
                before, after = self.character_manager.modify_resource(reactor.name, "fabula_points", 1)
                events.append(
                    {
                        "actor": reactor.name,
                        "skill_name": "反击",
                        "fabula_gain": ResourceChange(reactor.name, "fabula_points", 1, before, after, "反击大失败获得物语点。"),
                    }
                )
            rules_text = f"{reactor.name} 触发【反击】：反击检定 {counter_roll.total} vs {counter_roll.target_number}，{'成功' if counter_roll.success else '失败'}。"
            if counter_roll.success:
                incoming_damage_bonus = self._incoming_damage_bonus(attacker.name)
                damage, affinity = self.rules_engine.compute_damage(
                    high_roll=0,
                    weapon_damage=reactor.weapon_damage + reactor.equipment_attack_damage_bonus + incoming_damage_bonus,
                    damage_type=self.character_manager.effective_weapon_damage_type(reactor.name),
                    target=attacker,
                    ignore_resist=self._attack_ignores_resist(
                        reactor, self.character_manager.effective_weapon_damage_type(reactor.name)
                    ),
                    ignore_all_affinities=reactor.equipment_ignore_all_affinities,
                )
                if damage >= 0:
                    _, after = self.character_manager.apply_damage(attacker.name, damage)
                else:
                    _, after = self.character_manager.modify_resource(attacker.name, "hp", -damage)
                counter_roll.high_roll = 0
                counter_roll.damage = abs(damage)
                counter_roll.damage_type = self.character_manager.effective_weapon_damage_type(reactor.name)
                counter_roll.applied_affinity = affinity
                counter_roll.hp_after = after
                rules_text += f" 造成 {counter_roll.damage} 点伤害。"
                if after == 0:
                    event = self.conflict_manager.resolve_zero_hp(attacker.name)
                    events.append({"conflict_event": event})
            events.append(
                {
                    "actor": reactor.name,
                    "skill_name": "反击",
                    "triggered": True,
                    "roll": counter_roll,
                    "rules_text": rules_text,
                }
            )
        return events

    def _normalized_skill_name(self, raw_name: str) -> str:
        name = raw_name.split("（+")[0].split("(+")[0].strip()
        aliases = {
            "召唤阿卡纳": "契约与召唤",
            "解除阿卡纳": "契约与召唤",
            "召唤奥灵": "契约与召唤",
            "遣散奥灵": "契约与召唤",
            "召唤奥秘": "契约与召唤",
            "解除奥秘": "契约与召唤",
            "绑定和召唤": "契约与召唤",
            "绑定与召唤": "契约与召唤",
        }
        return normalize_skill_reference_name(aliases.get(name, name))

    def _actor_has_skill(self, actor, skill_name: str) -> bool:
        return skill_rank(actor.skills, skill_name) > 0 or skill_name in actor.hero_skills

    def _skill_rank(self, actor, skill_name: str) -> int:
        return max(1, skill_rank(actor.skills, skill_name))

    def _spend_mp_or_fail(
        self,
        action: Action,
        actor_name: str,
        amount: int,
        reason: str,
    ) -> ResourceChange | ActionResolution:
        actor = self.character_manager.get(actor_name)
        if actor.mp < amount:
            return ActionResolution(
                action=action,
                rules_text=f"{actor.name} 的 MP 不足，无法{reason}",
                payload={
                    "skill_failed": True,
                    "resource_change": ResourceChange(actor.name, "mp", 0, actor.mp, actor.mp, "MP 不足。"),
                },
            )
        before, after = self.character_manager.modify_resource(actor.name, "mp", -amount)
        return ResourceChange(actor.name, "mp", after - before, before, after, reason)

    def _spend_fabula_or_fail(
        self,
        action: Action,
        actor_name: str,
        amount: int,
        reason: str,
    ) -> ResourceChange | ActionResolution:
        actor = self.character_manager.get(actor_name)
        if actor.fabula_points < amount:
            return ActionResolution(
                action=action,
                rules_text=f"{actor.name} 的物语点不足，无法{reason}",
                payload={
                    "skill_failed": True,
                    "resource_change": ResourceChange(
                        actor.name,
                        "fabula_points",
                        0,
                        actor.fabula_points,
                        actor.fabula_points,
                        "物语点不足。",
                    ),
                },
            )
        before, after = self.character_manager.modify_resource(actor.name, "fabula_points", -amount)
        return ResourceChange(actor.name, "fabula_points", after - before, before, after, reason)

    def _restore_inventory_points(self, actor_name: str, amount: int) -> tuple[int, int]:
        actor = self.character_manager.get(actor_name)
        before = actor.inventory_points
        maximum = actor.max_inventory_points if actor.max_inventory_points > 0 else before + amount
        actor.inventory_points = min(maximum, before + amount)
        return before, actor.inventory_points

    def _consume_next_damage_bonus(self, target_name: str) -> int:
        bonus = 0
        remaining_effects = []
        for effect in self.conflict_manager.state.active_effects:
            if effect.effect_type == "next_damage_bonus" and effect.target == target_name:
                bonus += int(effect.data.get("damage_bonus", 0))
                continue
            remaining_effects.append(effect)
        self.conflict_manager.state.active_effects = remaining_effects
        return bonus

    def _incoming_damage_bonus(self, target_name: str) -> int:
        bonus = 0
        for effect in self.conflict_manager.state.active_effects:
            if effect.effect_type == "incoming_damage_bonus" and effect.target == target_name:
                bonus += int(effect.data.get("damage_bonus", 0))
        return bonus

    def _apply_fixed_damage(
        self,
        target_name: str,
        amount: int,
        damage_type: str,
        *,
        ignore_resist: bool = False,
        ignore_resist_and_immune: bool = False,
    ) -> tuple[int, int, Affinity]:
        target = self.character_manager.get(target_name)
        effective_affinity = resolve_affinity(
            target.affinities.get(damage_type, Affinity.NORMAL),
            target.equipment_affinities.get(damage_type),
            target.temporary_affinities.get(damage_type),
            ignore_resist=ignore_resist or ignore_resist_and_immune,
            ignore_immune=ignore_resist_and_immune,
            ignore_all_affinities=damage_type == "none",
        )

        damage = amount + self._incoming_damage_bonus(target_name)
        if effective_affinity == Affinity.WEAK:
            damage *= 2
        elif effective_affinity == Affinity.RESIST:
            damage //= 2
        elif effective_affinity == Affinity.IMMUNE:
            damage = 0
        elif effective_affinity == Affinity.ABSORB:
            damage = -damage

        if damage >= 0:
            before, after = self.character_manager.apply_damage(target_name, damage)
        else:
            before, after = self.character_manager.modify_resource(target_name, "hp", -damage)
        return before, after, effective_affinity

    def _resolve_attack_target(self, action: Action):
        target = self.character_manager.get(self._target_name(action))
        if not action.parameters.get("is_melee", True):
            return target, ""
        guardian = self.character_manager.guardian_for(target.name)
        if guardian is None:
            return target, ""
        return guardian, f"{guardian.name} 挡在 {target.name} 身前，替同伴承受了这次近战攻击。"

    def _weapon_mastery_bonus(self, actor, is_melee: bool) -> int:
        if is_melee:
            return skill_rank(actor.skills, "近战武器精通")
        return skill_rank(actor.skills, "远程武器精通")

    def _attack_damage_type(self, actor, action: Action) -> str:
        if actor.active_arcanum == "剑":
            return "none"
        return action.parameters.get("damage_type", self.character_manager.effective_weapon_damage_type(actor.name))

    def _attack_defense_type(self, actor, action: Action) -> str:
        if action.parameters.get("defense_type"):
            return action.parameters["defense_type"]
        if actor.equipment_attack_targets_magic_defense:
            return "magic"
        return "physical"

    def _attack_ignores_resist(self, actor, damage_type: str) -> bool:
        return (
            actor.equipment_ignore_resist
            or (actor.active_arcanum == "锻造" and damage_type == "fire")
            or (actor.active_arcanum == "霜" and damage_type == "ice")
        )

    def _hero_damage_bonus(self, actor, *, is_spell: bool, is_melee: bool = True) -> int:
        bonus = 10 if actor.level >= 40 else 5
        arcanum_bonus = 5 if not is_spell and actor.active_arcanum == "剑" else 0
        if is_spell and "强力咒语" in actor.hero_skills:
            return bonus
        if not is_spell and is_melee and "强力攻击" in actor.hero_skills:
            return bonus + arcanum_bonus
        if not is_spell and not is_melee and "强力射击" in actor.hero_skills:
            return bonus + arcanum_bonus
        return arcanum_bonus

    def _species_text(self, character) -> str:
        species_aliases = {
            "野兽": "野兽",
            "beast": "野兽",
            "构装体": "构装体",
            "构造体": "构装体",
            "construct": "构装体",
            "恶魔": "恶魔",
            "demon": "恶魔",
            "元素": "元素",
            "elemental": "元素",
            "人型": "人型",
            "humanoid": "人型",
            "怪物": "怪物",
            "monster": "怪物",
            "植物": "植物",
            "plant": "植物",
            "不死族": "不死族",
            "undead": "不死族",
        }
        lowered_traits = {trait.lower(): trait for trait in character.traits}
        for raw_trait, label in species_aliases.items():
            if raw_trait.lower() in lowered_traits:
                return label
        return "未记录"

    def _affinities_text(self, character) -> str:
        damage_types = ("physical", "wind", "lightning", "dark", "earth", "fire", "ice", "light", "poison")
        entries = []
        for damage_type in damage_types:
            affinity = self.character_manager.effective_affinity(character.name, damage_type)
            if affinity and affinity != Affinity.NORMAL:
                entries.append(f"{self._damage_type_text(damage_type)}:{affinity.value}")
        return "、".join(entries) or "无特殊相性"

    def _status_name(self, status: StatusEffect) -> str:
        mapping = {
            StatusEffect.SLOW: "迟缓",
            StatusEffect.DAZED: "眩晕",
            StatusEffect.WEAKENED: "虚弱",
            StatusEffect.SHAKEN: "动摇",
            StatusEffect.ENRAGED: "激怒",
            StatusEffect.POISONED: "中毒",
        }
        return mapping[status]

    def _status_effect(self, value) -> StatusEffect:
        if isinstance(value, StatusEffect):
            return value
        key = str(value or "").strip().lower()
        aliases = {
            "迟缓": StatusEffect.SLOW,
            "slow": StatusEffect.SLOW,
            "眩晕": StatusEffect.DAZED,
            "dazed": StatusEffect.DAZED,
            "虚弱": StatusEffect.WEAKENED,
            "weakened": StatusEffect.WEAKENED,
            "动摇": StatusEffect.SHAKEN,
            "shaken": StatusEffect.SHAKEN,
            "激怒": StatusEffect.ENRAGED,
            "enraged": StatusEffect.ENRAGED,
            "中毒": StatusEffect.POISONED,
            "poisoned": StatusEffect.POISONED,
        }
        if key in aliases:
            return aliases[key]
        return StatusEffect(key)

    def _ritual_name(self, value) -> str:
        text = str(value or "").strip()
        bracket = re.search(r"[【\[]([^】\]]+)[】\]]", text)
        if bracket:
            text = bracket.group(1)
        text = re.sub(r"^仪式\s*[：:]\s*", "", text)
        text = text.strip(" ：:「」『』【】[]")
        return text or "未命名仪式"

    def _ritual_clock_name(self, value) -> str:
        name = self._ritual_name(value)
        return name if name.startswith("仪式：") else f"仪式：{name}"

    def _ritual_discipline(self, value) -> RitualDiscipline:
        if isinstance(value, RitualDiscipline):
            return value
        key = str(value).strip().lower()
        aliases = {
            "arcanism": RitualDiscipline.ARCANISM,
            "奥术": RitualDiscipline.ARCANISM,
            "奥术仪式": RitualDiscipline.ARCANISM,
            "chimerism": RitualDiscipline.CHIMERISM,
            "嵌合": RitualDiscipline.CHIMERISM,
            "嵌合术": RitualDiscipline.CHIMERISM,
            "elementalism": RitualDiscipline.ELEMENTALISM,
            "元素": RitualDiscipline.ELEMENTALISM,
            "元素术": RitualDiscipline.ELEMENTALISM,
            "entropism": RitualDiscipline.ENTROPISM,
            "熵": RitualDiscipline.ENTROPISM,
            "熵系": RitualDiscipline.ENTROPISM,
            "ritualism": RitualDiscipline.RITUALISM,
            "仪式": RitualDiscipline.RITUALISM,
            "仪式学": RitualDiscipline.RITUALISM,
            "spiritism": RitualDiscipline.SPIRITISM,
            "spirit": RitualDiscipline.SPIRITISM,
            "spiritual": RitualDiscipline.SPIRITISM,
            "soul": RitualDiscipline.SPIRITISM,
            "soul_magic": RitualDiscipline.SPIRITISM,
            "soul magic": RitualDiscipline.SPIRITISM,
            "灵魂": RitualDiscipline.SPIRITISM,
            "灵魂术": RitualDiscipline.SPIRITISM,
            "灵魂魔法": RitualDiscipline.SPIRITISM,
            "灵魂系": RitualDiscipline.SPIRITISM,
            "灵系": RitualDiscipline.SPIRITISM,
            "御魂": RitualDiscipline.SPIRITISM,
            "御魂系": RitualDiscipline.SPIRITISM,
            "御魂仪式": RitualDiscipline.SPIRITISM,
            "御魂系仪式": RitualDiscipline.SPIRITISM,
            "御魂使": RitualDiscipline.SPIRITISM,
        }
        if key in aliases:
            return aliases[key]
        return RitualDiscipline(key)

    def _ritual_potency(self, value) -> RitualPotency:
        if isinstance(value, RitualPotency):
            return value
        key = str(value).strip().lower()
        aliases = {
            "minor": RitualPotency.MINOR,
            "light": RitualPotency.MINOR,
            "slight": RitualPotency.MINOR,
            "low": RitualPotency.MINOR,
            "轻微": RitualPotency.MINOR,
            "小": RitualPotency.MINOR,
            "moderate": RitualPotency.MODERATE,
            "medium": RitualPotency.MODERATE,
            "中等": RitualPotency.MODERATE,
            "中": RitualPotency.MODERATE,
            "major": RitualPotency.MAJOR,
            "强大": RitualPotency.MAJOR,
            "大": RitualPotency.MAJOR,
            "extreme": RitualPotency.EXTREME,
            "极强": RitualPotency.EXTREME,
            "极端": RitualPotency.EXTREME,
        }
        if key in aliases:
            return aliases[key]
        return RitualPotency(key)

    def _ritual_scope(self, value) -> RitualScope:
        if isinstance(value, RitualScope):
            return value
        key = str(value).strip().lower()
        aliases = {
            "individual": RitualScope.INDIVIDUAL,
            "个体": RitualScope.INDIVIDUAL,
            "个人": RitualScope.INDIVIDUAL,
            "单体": RitualScope.INDIVIDUAL,
            "small": RitualScope.SMALL,
            "小": RitualScope.SMALL,
            "小型": RitualScope.SMALL,
            "小范围": RitualScope.SMALL,
            "房间": RitualScope.SMALL,
            "large": RitualScope.LARGE,
            "facility": RitualScope.LARGE,
            "大": RitualScope.LARGE,
            "大型": RitualScope.LARGE,
            "大范围": RitualScope.LARGE,
            "区域": RitualScope.LARGE,
            "huge": RitualScope.HUGE,
            "巨大": RitualScope.HUGE,
            "巨型": RitualScope.HUGE,
            "巨大范围": RitualScope.HUGE,
            "城市": RitualScope.HUGE,
        }
        if key in aliases:
            return aliases[key]
        return RitualScope(key)

    def _project_use(self, value) -> ProjectUse:
        if isinstance(value, ProjectUse):
            return value
        key = str(value).strip().lower()
        aliases = {
            "consumable": ProjectUse.CONSUMABLE,
            "一次性": ProjectUse.CONSUMABLE,
            "消耗品": ProjectUse.CONSUMABLE,
            "permanent": ProjectUse.PERMANENT,
            "永久": ProjectUse.PERMANENT,
            "永久性": ProjectUse.PERMANENT,
        }
        if key in aliases:
            return aliases[key]
        return ProjectUse(key)

    def _persistent_change_type(
        self,
        value,
        *,
        fallback: PersistentChangeType | None = PersistentChangeType.WORLD_FACT,
    ) -> PersistentChangeType | None:
        if isinstance(value, PersistentChangeType):
            return value
        if value in (None, ""):
            return fallback
        key = str(value).strip().lower()
        aliases = {
            "world_fact": PersistentChangeType.WORLD_FACT,
            "fact": PersistentChangeType.WORLD_FACT,
            "世界事实": PersistentChangeType.WORLD_FACT,
            "事实": PersistentChangeType.WORLD_FACT,
            "facility": PersistentChangeType.FACILITY,
            "设施": PersistentChangeType.FACILITY,
            "地点设施": PersistentChangeType.FACILITY,
            "equipment": PersistentChangeType.EQUIPMENT,
            "装备": PersistentChangeType.EQUIPMENT,
            "武器": PersistentChangeType.EQUIPMENT,
            "道具装备": PersistentChangeType.EQUIPMENT,
            "consumable": PersistentChangeType.CONSUMABLE,
            "一次性": PersistentChangeType.CONSUMABLE,
            "一次性道具": PersistentChangeType.CONSUMABLE,
            "消耗品": PersistentChangeType.CONSUMABLE,
            "transport": PersistentChangeType.TRANSPORT,
            "交通": PersistentChangeType.TRANSPORT,
            "交通工具": PersistentChangeType.TRANSPORT,
            "载具": PersistentChangeType.TRANSPORT,
        }
        if key in aliases:
            return aliases[key]
        return PersistentChangeType(key)

    def _string_list(self, value) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()] if str(value).strip() else []

    def _dict_list(self, value) -> list[dict]:
        if value is None:
            return []
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        return []

    def _ritual_potency_text(self, potency: RitualPotency) -> str:
        return {
            RitualPotency.MINOR: "轻微",
            RitualPotency.MODERATE: "中等",
            RitualPotency.MAJOR: "强大",
            RitualPotency.EXTREME: "极强",
        }[potency]

    def _ritual_scope_text(self, scope: RitualScope) -> str:
        return {
            RitualScope.INDIVIDUAL: "个体",
            RitualScope.SMALL: "小型",
            RitualScope.LARGE: "大型",
            RitualScope.HUGE: "巨大",
        }[scope]

    def _register_spell_effect(self, actor_name: str, target_name: str, action: Action, definition) -> TimedEffect:
        effect_type = {
            SpellEffectType.DEFENSE_BUFF: "defense_bonus",
            SpellEffectType.DEFENSE_FLOOR: "defense_floor",
            SpellEffectType.AFFINITY_BUFF: "affinity_buff",
            SpellEffectType.STATUS_IMMUNITY: "status_immunity",
            SpellEffectType.WEAPON_ENCHANT: "weapon_enchant",
            SpellEffectType.ATTRIBUTE_BUFF: "attribute_buff",
            SpellEffectType.EXTRA_ACTION: "extra_action",
            SpellEffectType.SURVIVE_ONCE: "survive_once",
        }[definition.effect_type]
        effect_key = f"spell:{definition.name}:{target_name}"
        selected_status = self._selected_status(action, definition, allow_missing=True)
        selected_damage_type = self._selected_damage_type(action, definition)
        selected_attribute = self._selected_attribute(action, definition)
        affinity_changes = dict(definition.affinity_changes)
        if not affinity_changes and selected_damage_type is not None and definition.effect_type == SpellEffectType.AFFINITY_BUFF:
            affinity_changes[selected_damage_type] = Affinity.RESIST
        status_immunities = tuple(definition.status_immunities)
        if not status_immunities and selected_status is not None and definition.effect_type == SpellEffectType.STATUS_IMMUNITY:
            status_immunities = (selected_status,)
        attribute_bonus = dict(definition.attribute_bonus)
        if not attribute_bonus and selected_attribute is not None and definition.effect_type == SpellEffectType.ATTRIBUTE_BUFF:
            attribute_bonus[selected_attribute] = 1
        defense_floor = dict(definition.defense_floor)
        if definition.name in {"屏障", "护卫灵气"} and defense_floor:
            actor = self.character_manager.get(actor_name)
            floor_value = 14 if actor.level >= 40 else 13 if actor.level >= 20 else 12
            defense_floor = {defense_type: floor_value for defense_type in defense_floor}
        weapon_damage_type = definition.weapon_damage_type or selected_damage_type
        effect = TimedEffect(
            owner=actor_name,
            effect_type=effect_type,
            expires_on=definition.duration or EffectTiming.SCENE_END,
            target=target_name,
            source=definition.name,
            effect_key=effect_key,
            data={
                "defense_bonus": dict(definition.defense_bonus),
                "defense_floor": defense_floor,
                "affinity_changes": affinity_changes,
                "status_immunities": status_immunities,
                "attribute_bonus": attribute_bonus,
                "weapon_damage_type": weapon_damage_type,
                "remaining_bonus_turns": definition.extra_actions,
            },
            note=definition.description,
        )
        self.conflict_manager.register_effect(effect)
        return effect

    def _duration_text(self, duration: EffectTiming | None) -> str:
        mapping = {
            EffectTiming.OWNER_TURN_START: "施法者下回合开始",
            EffectTiming.OWNER_TURN_END: "施法者本回合结束",
            EffectTiming.ROUND_END: "本轮结束",
            EffectTiming.SCENE_END: "场景结束",
            None: "效果结束",
        }
        return mapping[duration]

    def conflict_event_survive_once(self, target_name: str):
        from fu_gm.models import ConflictEvent

        return ConflictEvent(
            target=target_name,
            event_type="survive_once",
            summary=f"{target_name} 被守护法术强行留在 1 点 HP，暂时没有倒下。",
            hp_after=1,
        )

    def _spell_effect_rules_text(self, actor_name: str, target_name: str, definition, effect: TimedEffect) -> str:
        rules_text = f"{actor_name} 施放【{definition.name}】。"
        if definition.effect_type == SpellEffectType.DEFENSE_BUFF:
            bonus_text = "、".join(
                f"{kind}+{amount}" for kind, amount in effect.data.get("defense_bonus", {}).items() if amount
            )
            return rules_text + f" {target_name} 获得 {bonus_text}，持续至{self._duration_text(definition.duration)}。"
        if definition.effect_type == SpellEffectType.DEFENSE_FLOOR:
            floor_text = "、".join(
                f"{kind}至少 {amount}" for kind, amount in effect.data.get("defense_floor", {}).items() if amount
            )
            return rules_text + f" {target_name} 的防御被抬升为 {floor_text}，持续至{self._duration_text(definition.duration)}。"
        if definition.effect_type == SpellEffectType.AFFINITY_BUFF:
            affinity_text = "、".join(
                f"{damage_type}:{affinity.value}" for damage_type, affinity in effect.data.get("affinity_changes", {}).items()
            )
            return rules_text + f" {target_name} 获得 {affinity_text}，持续至{self._duration_text(definition.duration)}。"
        if definition.effect_type == SpellEffectType.STATUS_IMMUNITY:
            immunity_text = "、".join(self._status_name(status) for status in effect.data.get("status_immunities", ()))
            return rules_text + f" {target_name} 对 {immunity_text} 免疫，持续至{self._duration_text(definition.duration)}。"
        if definition.effect_type == SpellEffectType.WEAPON_ENCHANT:
            damage_type = effect.data.get("weapon_damage_type", definition.damage_type)
            return rules_text + f" {target_name} 的武器附着为 {self._damage_type_text(damage_type)}属性，持续至{self._duration_text(definition.duration)}。"
        if definition.effect_type == SpellEffectType.ATTRIBUTE_BUFF:
            attribute_text = "、".join(
                f"{attribute}+{value}" for attribute, value in effect.data.get("attribute_bonus", {}).items() if value
            )
            return rules_text + f" {target_name} 获得属性强化 {attribute_text}，持续至{self._duration_text(definition.duration)}。"
        if definition.effect_type == SpellEffectType.EXTRA_ACTION:
            return rules_text + f" {target_name} 在效果持续期间将额外获得 {definition.extra_actions} 次动作机会。"
        if definition.effect_type == SpellEffectType.SURVIVE_ONCE:
            return rules_text + f" {target_name} 在本场景中首次将要倒下时会保留 1 点 HP。"
        return rules_text

    def _selected_status(self, action: Action, definition, allow_missing: bool = False) -> StatusEffect | None:
        raw_status = action.parameters.get("chosen_status") or action.parameters.get("status_effect")
        if raw_status is not None:
            return self._status_effect(raw_status)
        if definition.status_effect is not None:
            return definition.status_effect
        if definition.selectable_statuses:
            return definition.selectable_statuses[0]
        if allow_missing:
            return None
        raise ValueError(f"法术【{definition.name}】需要一个状态选择。")

    def _selected_damage_type(self, action: Action, definition) -> str | None:
        if action.parameters.get("chosen_damage_type"):
            return action.parameters["chosen_damage_type"]
        if definition.damage_type != "arcane":
            return definition.damage_type
        if definition.selectable_damage_types:
            return definition.selectable_damage_types[0]
        return None

    def _selected_attribute(self, action: Action, definition) -> str | None:
        if action.parameters.get("chosen_attribute"):
            return action.parameters["chosen_attribute"]
        if definition.selectable_attributes:
            return definition.selectable_attributes[0]
        return None

    def _remember_damage_outcome(self, actor_name: str, target_name: str, outcome) -> None:
        damage_type_text = self._damage_type_text(outcome.damage_type)
        affinity_text = self._affinity_memory_text(outcome.applied_affinity)
        actor_note = f"对 {target_name} 的{damage_type_text}攻击造成 {outcome.damage} 点影响"
        if affinity_text:
            actor_note += f"，确认其{affinity_text}"
            self.world_state.remember_subject_fact(target_name, f"对{damage_type_text}{affinity_text}")
        if actor_name in self.world_state.npc_personas:
            self.world_state.remember_npc_event(actor_name, actor_note)
        if target_name in self.world_state.npc_personas:
            target_note = f"{actor_name} 使用{damage_type_text}攻击了自己"
            if affinity_text:
                target_note += f"，并暴露出{affinity_text}"
            self.world_state.remember_npc_event(target_name, target_note)

    def _affinity_memory_text(self, affinity: Affinity) -> str:
        mapping = {
            Affinity.WEAK: "对这种力量存在弱点",
            Affinity.RESIST: "对这种力量具有抗性",
            Affinity.IMMUNE: "对这种力量完全免疫",
            Affinity.ABSORB: "会吸收这种力量",
        }
        return mapping.get(affinity, "")

    def _damage_type_text(self, damage_type: str) -> str:
        mapping = {
            "physical": "物理",
            "fire": "火系",
            "ice": "冰系",
            "lightning": "雷系",
            "wind": "风系",
            "earth": "土系",
            "poison": "毒系",
            "dark": "暗系",
            "light": "光系",
            "arcane": "奥术",
            "none": "无属性",
        }
        return mapping.get(damage_type, damage_type)

