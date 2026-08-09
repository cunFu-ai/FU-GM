from __future__ import annotations

from copy import deepcopy
from contextvars import ContextVar
import re
from dataclasses import replace
from typing import Any, Callable

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.action_dispatcher import ActionDispatcher
from fu_gm.components.action_transaction_coordinator import ActionTransactionCoordinator
from fu_gm.components.check_batch_manager import CheckBatchManager
from fu_gm.components.check_transaction_manager import CheckTransactionManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.combat_trait_manager import CombatTraitEvent, CombatTraitManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.dungeon_manager import DungeonManager
from fu_gm.components.decision_window_manager import DecisionWindowManager
from fu_gm.components.decision_action_resolver import DecisionActionResolver
from fu_gm.components.economy_manager import EconomyManager
from fu_gm.components.gadget_manager import TinkererGadgetManager
from fu_gm.components.loyal_companion_manager import LoyalCompanionManager
from fu_gm.components.npc_action_adapter import NPCActionAdapter
from fu_gm.components.npc_condition_manager import NPCConditionManager
from fu_gm.components.npc_ability_runtime import (
    is_living_creature,
    npc_affinity_override,
    npc_clock_extra_segments,
    npc_context_check_bonus,
)
from fu_gm.components.opportunity_resolver import OpportunityResolver
from fu_gm.components.project_manager import ProjectManager
from fu_gm.components.rest_manager import RestManager
from fu_gm.components.post_check_window_manager import PostCheckWindowManager
from fu_gm.components.post_check_decision_coordinator import PostCheckDecisionCoordinator
from fu_gm.components.post_check_state_journal import PostCheckStateJournal
from fu_gm.components.ritual_manager import RitualManager
from fu_gm.components.rules_engine import RulesEngine, resolve_affinity
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.skill_trigger_manager import SkillTriggerManager
from fu_gm.components.skill_lifecycle_coordinator import SkillLifecycleCoordinator, SkillLifecycleOutcome
from fu_gm.components.skill_lifecycle_event_buffer import SkillLifecycleEventBuffer
from fu_gm.components.spell_skill_manager import SpellSkillManager
from fu_gm.components.spell_parameter_manager import SpellParameterManager
from fu_gm.components.trigger_manager import TriggerManager
from fu_gm.components.world_state import WorldState
from fu_gm.equipment_catalog import get_equipment_example
from fu_gm.models import (
    Action,
    ActionResolution,
    ActionType,
    Affinity,
    Character,
    Clock,
    ClockChange,
    ConflictEvent,
    EffectTiming,
    EnemyRank,
    MemoryVisibility,
    PersistentChangeType,
    ProjectUse,
    ResourceChange,
    RitualDiscipline,
    RitualPotency,
    RitualScope,
    RestType,
    SpellEffectType,
    SpellTarget,
    StatusEffect,
    TimedEffect,
    TinkererGadgetResult,
)
from fu_gm.skill_library import (
    CLASS_SKILL_REFERENCES,
    SKILL_COVERAGE_PASSIVE_HARD,
    has_skill_name,
    normalize_skill_reference_name,
    skill_implementation_coverage,
    skill_rank,
)
from fu_gm.spellbook import get_spell_definition, normalize_spell_name


class ActionInterceptor:
    """在叙事前执行硬规则的中间拦截层。"""

    CLASS_NAMES = {reference.class_name for reference in CLASS_SKILL_REFERENCES if reference.class_name}
    TURN_CONSUMING_ACTIONS = {
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
        ActionType.OPEN_CHEST,
        ActionType.EXPLORE_DUNGEON,
        ActionType.PLAN_RITUAL,
        ActionType.CONTRIBUTE_RITUAL,
        ActionType.CAST_RITUAL,
        ActionType.REQUEST_ROLL,
        ActionType.NPCACT,
        ActionType.SELL_ITEM,
    }
    NARRATIVE_TARGET_EFFECTS = {
        SpellEffectType.DEFENSE_BUFF,
        SpellEffectType.DEFENSE_FLOOR,
        SpellEffectType.AFFINITY_BUFF,
        SpellEffectType.STATUS_IMMUNITY,
        SpellEffectType.ATTRIBUTE_BUFF,
        SpellEffectType.SURVIVE_ONCE,
        SpellEffectType.NARRATIVE,
    }

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
        rest_manager: RestManager | None = None,
        scene_manager: SceneManager | None = None,
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
        self.rest_manager = rest_manager or RestManager(character_manager, clock_manager)
        self.scene_manager = scene_manager
        self.loyal_companion_manager: LoyalCompanionManager | None = None
        self.skill_trigger_manager = SkillTriggerManager()
        self.spell_skill_manager = SpellSkillManager(self.skill_trigger_manager)
        self.decision_window_manager = DecisionWindowManager(world_state)
        self.check_batch_manager = CheckBatchManager(
            world_state,
            self.decision_window_manager,
        )
        self.spell_parameter_manager = SpellParameterManager(
            character_manager,
            self.decision_window_manager,
            scene_manager,
        )
        self.skill_lifecycle = SkillLifecycleCoordinator(
            self.skill_trigger_manager,
            self.decision_window_manager,
            character_manager,
            conflict_manager,
        )
        self.skill_lifecycle_events = SkillLifecycleEventBuffer()
        self.decision_action_resolver = DecisionActionResolver(
            character_manager,
            conflict_manager,
            self.decision_window_manager,
        )
        self.npc_action_adapter = NPCActionAdapter(character_manager, conflict_manager)
        self.npc_conditions = NPCConditionManager(
            character_manager,
            clock_manager,
            conflict_manager,
        )
        self.post_check_window_manager = PostCheckWindowManager()
        self.conflict_manager.bind_decision_window_manager(self.decision_window_manager)
        self.combat_trait_manager = CombatTraitManager()
        self.post_check_state = PostCheckStateJournal(
            rules_engine=self.rules_engine,
            clock_manager=self.clock_manager,
            ensure_clock_exists=self._ensure_clock_exists,
        )
        self.check_transaction_manager = CheckTransactionManager(
            character_manager=self.character_manager,
            clock_manager=self.clock_manager,
            conflict_manager=self.conflict_manager,
            world_state=self.world_state,
            post_check_state=self.post_check_state,
            ritual_manager=lambda: self.ritual_manager,
            project_manager=lambda: self.project_manager,
            dungeon_manager=lambda: self.dungeon_manager,
            transactional_actions=self._TRANSACTIONAL_CHECK_ACTIONS,
        )
        self.pending_check_transactions = self.check_transaction_manager.pending
        self.post_check_decisions = PostCheckDecisionCoordinator(
            characters=self.character_manager,
            conflict=self.conflict_manager,
            decisions=self.decision_window_manager,
            windows=self.post_check_window_manager,
            skill_triggers=self.skill_trigger_manager,
            skill_lifecycle=self.skill_lifecycle,
            check_transactions=self.check_transaction_manager,
            post_check_state=self.post_check_state,
            capture_skill_lifecycle=self._capture_skill_lifecycle,
            scenes=self.scene_manager,
        )
        self.reveal_motivation_provider: Callable[[str], str] | None = None
        self.opportunity_resolver = OpportunityResolver(
            characters=self.character_manager,
            clocks=self.clock_manager,
            conflict=self.conflict_manager,
            world=self.world_state,
            post_check_state=self.post_check_state,
            economy=self.economy_manager,
            ensure_clock_exists=self._ensure_clock_exists,
            status_effect=self._status_effect,
            status_name=self._status_name,
            reveal_motivation=lambda target: (
                self.reveal_motivation_provider(target)
                if callable(self.reveal_motivation_provider)
                else ""
            ),
        )
        self._damage_source_name: ContextVar[str] = ContextVar(
            "fu_gm_damage_source_name",
            default="",
        )
        self._active_rule_action: ContextVar[Action | None] = ContextVar(
            "fu_gm_active_rule_action",
            default=None,
        )
        self._advancing_check_batches = False
        self.action_dispatcher = self._build_action_dispatcher()
        self.action_transaction_coordinator = ActionTransactionCoordinator(self)
        self.character_manager.register_resource_listener(self._on_character_resource_change)
        self.conflict_manager.register_turn_start_listener(self._on_conflict_turn_start)

    _TRANSACTIONAL_CHECK_ACTIONS = {
        ActionType.ATTACK,
        ActionType.SPELL,
        ActionType.HINDER,
        ActionType.INVESTIGATE,
        ActionType.OBJECTIVE,
        ActionType.SKILL,
        ActionType.PLAN_RITUAL,
        ActionType.CONTRIBUTE_RITUAL,
        ActionType.CAST_RITUAL,
        ActionType.REQUEST_ROLL,
    }

    @property
    def _check_transaction_candidate(self) -> dict[str, object] | None:
        return self.check_transaction_manager.candidate

    @_check_transaction_candidate.setter
    def _check_transaction_candidate(self, value: dict[str, object] | None) -> None:
        self.check_transaction_manager.candidate = value

    @property
    def _replaying_check_transaction(self) -> bool:
        return self.check_transaction_manager.replaying

    @_replaying_check_transaction.setter
    def _replaying_check_transaction(self, value: bool) -> None:
        self.check_transaction_manager.replaying = bool(value)

    def _organized_chronicles_mode_enabled(self) -> bool:
        state = self.world_state.world_profile.optional_rules.get("organized_chronicles_mode")
        return bool(state and state.enabled)

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

    def _target_number_parameter(self, parameters: dict[str, Any], *, default: int = 10) -> int:
        raw_value = parameters.get("target_number", None)
        explicit = raw_value is not None and raw_value != ""
        value = self._int_parameter(parameters, "target_number", default)
        if explicit and value <= 0:
            raise ValueError("公开检定缺少有效难度等级：target_number 必须是 GM 明确裁定的正数。")
        return value if value > 0 else default

    def _target_number_or_defense(self, parameters: dict[str, Any], target_name: str, defense_type: str) -> int:
        default = self._effective_defense(target_name, defense_type)
        return self._target_number_parameter(parameters, default=default)

    def _effective_defense(self, target_name: str, defense_type: str) -> int:
        return (
            self.character_manager.effective_defense(target_name, defense_type)
            + self.conflict_manager.npc_passive_defense_bonus(
                target_name,
                defense_type,
            )
        )

    def _target_numbers_for_targets(
        self,
        parameters: dict[str, Any],
        target_names: list[str],
        defense_type: str,
    ) -> dict[str, int]:
        if "target_number" in parameters:
            default = self._effective_defense(target_names[0], defense_type)
            explicit = self._target_number_parameter(parameters, default=default)
            return {target_name: explicit for target_name in target_names}
        return {
            target_name: self._effective_defense(target_name, defense_type)
            for target_name in target_names
        }

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
        self._validate_timed_action_restriction(action)
        with self.skill_lifecycle_events.transaction():
            return self.action_transaction_coordinator.resolve(action)

    def _validate_timed_action_restriction(self, action: Action) -> None:
        if action.action_type == ActionType.NPCACT:
            return
        actor_name = str(action.parameters.get("actor") or "").strip()
        if not actor_name:
            return
        swallowed_reason = self.npc_conditions.action_restriction_reason(
            actor_name,
            action.action_type.value,
            clock_name=str(action.parameters.get("clock_name") or ""),
        )
        if swallowed_reason:
            raise ValueError(swallowed_reason)
        reason = self.conflict_manager.action_restriction_reason(
            actor_name,
            action.action_type.value,
        )
        if reason:
            raise ValueError(reason)

    def _build_action_dispatcher(self) -> ActionDispatcher:
        dispatcher = ActionDispatcher()
        dispatcher.register_many(
            {
                ActionType.ATTACK: self._resolve_attack,
                ActionType.SPELL: self._resolve_spell,
                ActionType.GUARD: self._resolve_guard,
                ActionType.EQUIP: self._resolve_equip,
                ActionType.HINDER: self._resolve_hinder,
                ActionType.INVESTIGATE: self._resolve_investigate,
                ActionType.OBJECTIVE: self._resolve_objective,
                ActionType.SKILL: self._resolve_skill_action,
                ActionType.USE_INVENTORY: self._resolve_use_inventory,
                ActionType.TINKERER_GADGET: self._resolve_tinkerer_gadget,
                ActionType.SHOP: self._resolve_shop,
                ActionType.REST: self._resolve_rest,
                ActionType.OPEN_CHEST: self._resolve_open_chest,
                ActionType.AWARD_REWARD: self._resolve_award_reward,
                ActionType.EXPLORE_DUNGEON: self._resolve_explore_dungeon,
                ActionType.NEXT_TURN: self._resolve_next_turn,
                ActionType.PLAN_RITUAL: self._resolve_plan_ritual,
                ActionType.CONTRIBUTE_RITUAL: self._resolve_contribute_ritual,
                ActionType.CAST_RITUAL: self._resolve_cast_ritual,
                ActionType.START_PROJECT: self._resolve_start_project,
                ActionType.HIRE_PROJECT_HELPERS: self._resolve_hire_project_helpers,
                ActionType.WORK_PROJECT: self._resolve_work_project,
                ActionType.NPCACT: self._resolve_npc_act,
                ActionType.NARRATE: self._resolve_narrate_action,
                ActionType.REQUEST_ROLL: self._resolve_roll,
                ActionType.MODIFY_RESOURCE: self._resolve_resource,
                ActionType.ADVANCE_CLOCK: self._resolve_clock,
                ActionType.INVOKE_TRAIT: self._resolve_trait_action,
                ActionType.INVOKE_BOND: self._resolve_bond_action,
                ActionType.TRIGGER_OPPORTUNITY: self._resolve_opportunity_action,
                ActionType.ACCEPT_STORY_CHANGE: self._resolve_story_change,
                ActionType.START_CONFLICT: self._resolve_start_conflict,
                ActionType.MANAGE_BOND: self._resolve_manage_bond,
                ActionType.SELL_ITEM: self._resolve_sell_item,
                ActionType.PLAYER_VS_PLAYER: self._resolve_player_vs_player,
                ActionType.ABSENT_PLAYER: self._resolve_absent_player,
                ActionType.RESOLVE_ZERO_HP: self._resolve_zero_hp_choice,
                ActionType.RESOLVE_DECISION: self._resolve_decision_choice,
            }
        )
        return dispatcher

    def _resolve_skill_action(self, action: Action) -> ActionResolution:
        coerced = self._coerce_misrouted_class_skill_action(action)
        return self._resolve_hinder(coerced) if coerced is not action else self._resolve_skill(action)

    def _resolve_narrate_action(self, action: Action) -> ActionResolution:
        source_windows = self.post_check_decisions.capture_source_windows(action)
        actor = str(action.parameters.get("actor") or "").strip()
        transaction = self.pending_check_transactions.get(actor) if actor else None
        if action.parameters.get("post_check_acceptance") and transaction is not None:
            resolution = self._commit_check_transaction_acceptance(
                acceptance_action=action,
                transaction=transaction,
            )
        else:
            resolution = self._resolve_narrate(action)
        self.post_check_decisions.settle(action, resolution, source_windows=source_windows)
        return resolution

    def _resolve_trait_action(self, action: Action) -> ActionResolution:
        source_windows = self.post_check_decisions.capture_source_windows(action)
        resolution = self._resolve_invoke_trait(action)
        self.post_check_decisions.settle(action, resolution, source_windows=source_windows)
        return resolution

    def _resolve_bond_action(self, action: Action) -> ActionResolution:
        source_windows = self.post_check_decisions.capture_source_windows(action)
        resolution = self._resolve_invoke_bond(action)
        self.post_check_decisions.settle(action, resolution, source_windows=source_windows)
        return resolution

    def _resolve_opportunity_action(self, action: Action) -> ActionResolution:
        source_windows = self.post_check_decisions.capture_source_windows(action)
        resolution = self._resolve_opportunity(action)
        self.post_check_decisions.settle(action, resolution, source_windows=source_windows)
        return resolution

    def _resolve_rest(self, action: Action) -> ActionResolution:
        if self.conflict_manager.state.active:
            return ActionResolution(
                action=action,
                rules_text="冲突仍在进行，队伍必须先脱离当前危险，才能开始休息。",
                payload={"rest_failed": True, "reason": "conflict_active"},
            )
        raw_type = str(action.parameters.get("rest_type") or "wilderness").strip().lower()
        rest_type = RestType.SETTLEMENT if raw_type in {
            "settlement",
            "town",
            "inn",
            "定居点",
            "城镇",
            "旅馆",
        } else RestType.WILDERNESS
        safe_source = str(action.parameters.get("safe_source") or "").strip()
        if not safe_source:
            return ActionResolution(
                action=action,
                rules_text="还需要先确认一处安全落脚点：野外可使用魔法帐篷或好客地点，定居点可使用旅馆或好客地点。",
                payload={"rest_failed": True, "reason": "safe_source_required"},
            )
        payer = str(action.parameters.get("payer") or action.parameters.get("actor") or "").strip() or None
        threat_clocks = [str(name) for name in action.parameters.get("threat_clocks", []) if str(name).strip()]
        actor = str(action.parameters.get("actor") or "").strip()
        explicit_participants = [
            str(name).strip()
            for name in action.parameters.get("participants", [])
            if str(name).strip()
        ]
        if explicit_participants:
            participants = explicit_participants
        elif actor:
            participants = [
                character.name
                for character in self.character_manager.all()
                if "pc" in character.traits
                and (
                    character.name == actor
                    or self.scene_manager.actors_share_movement_origin(
                        actor,
                        character.name,
                    )
                )
            ]
        else:
            participants = None
        self.rest_manager.validate(
            rest_type,
            safe_source=safe_source,
            payer=payer,
            threat_clocks=threat_clocks,
            participants=participants,
        )
        lodging_transaction = None
        if (
            rest_type == RestType.SETTLEMENT
            and str(action.parameters.get("rest_source_kind") or "").strip().lower()
            == "lodging"
        ):
            if not payer or payer not in (participants or []):
                raise ValueError("旅馆休息需要由本次休息队伍中的角色付款。")
            lodging_transaction = self.economy_manager.buy_lodging(
                payer,
                settlement_size=str(
                    action.parameters.get("settlement_size") or ""
                ).strip(),
                party_size=len(participants or []),
            )
        result = self.rest_manager.rest(
            rest_type,
            safe_source=safe_source,
            payer=payer,
            threat_clocks=threat_clocks,
            participants=participants,
        )
        tavern_talk_questions: dict[str, int] = {}
        rest_source_kind = str(
            action.parameters.get("rest_source_kind") or ""
        ).strip().lower()
        tavern_rest = (
            rest_type == RestType.SETTLEMENT
            and (
                rest_source_kind == "lodging"
                or any(
                    token in safe_source
                    for token in ("旅馆", "酒馆", "客栈", "旅店")
                )
            )
        )
        for character_name in result.recovered_characters:
            if not self.character_manager.exists(character_name):
                continue
            character = self.character_manager.get(character_name)
            character.skill_counters.pop("酒馆攀谈", None)
            rank = skill_rank(character.skills, "酒馆攀谈")
            if tavern_rest and rank > 0:
                character.skill_counters["酒馆攀谈"] = rank
                tavern_talk_questions[character_name] = rank
        loyal_companion_recoveries: list[dict[str, object]] = []
        if self.loyal_companion_manager is not None:
            for character_name in result.recovered_characters:
                recovery = self.loyal_companion_manager.apply_owner_rest(
                    character_name
                )
                if recovery is not None:
                    loyal_companion_recoveries.append(recovery)
        self.world_state.add_memory(result.summary)
        details = result.summary
        if lodging_transaction is not None:
            details = f"{lodging_transaction.summary} {details}"
        if result.ip_spent:
            details += f" {payer} 消耗 {result.ip_spent} 点物资搭建魔法帐篷。"
        if tavern_talk_questions:
            details += " " + "；".join(
                f"【{name}】本次可用【酒馆攀谈】询问 {count} 个问题"
                for name, count in tavern_talk_questions.items()
            ) + "。"
        if loyal_companion_recoveries:
            details += " " + "；".join(
                f"【{item['name']}】也随主人休息并恢复到完整状态"
                for item in loyal_companion_recoveries
            ) + "。"
        return ActionResolution(
            action=action,
            rules_text=details,
            payload={
                "rest_result": result,
                "rest_type": rest_type.value,
                "lodging_transaction": lodging_transaction,
                "tavern_talk_questions": tavern_talk_questions,
                "loyal_companion_recoveries": loyal_companion_recoveries,
            },
        )

    def _resolve_zero_hp_choice(self, action: Action) -> ActionResolution:
        return self.decision_action_resolver.resolve_zero_hp(
            action,
            require_all_sacrifice_conditions=self._organized_chronicles_mode_enabled(),
        )

    def _resolve_decision_choice(self, action: Action) -> ActionResolution:
        window_id = str(action.parameters.get("window_id") or "").strip()
        window = self.decision_window_manager.find_pending(window_id=window_id)
        if (
            window is not None
            and window.kind == "skill_parameter"
            and str(window.payload.get("skill") or window.payload.get("label") or "")
            == "予以信任"
        ):
            return self._resolve_trust_check_decision(action, window)
        if window is not None and action.parameters.get("post_check_acceptance"):
            if window.kind not in {"trait_invocation", "bond_invocation", "skill_judgement"}:
                raise ValueError("这个待决窗口不能用‘接受当前检定结果’处理。")
            if window.kind == "skill_judgement" and str(window.payload.get("label") or "") != "幸运七":
                raise ValueError("这个技能窗口不是检定结果定稿前的选择。")
            actor = str(action.parameters.get("actor") or "").strip()
            if actor != window.owner:
                raise ValueError(f"只有【{window.owner}】可以接受这次检定结果。")
            transaction = self.pending_check_transactions.get(actor)
            if transaction is None:
                raise ValueError("这次检定的暂存事务已经丢失，不能接受结果。")
            other_responders = [
                pending
                for pending in self._pending_pre_final_check_windows(
                    str(window.transaction_id or "").strip()
                )
                if pending.owner != actor
            ]
            silent_timeout = bool(
                action.parameters.get("_silent_failure_timeout")
            )
            if other_responders and not silent_timeout:
                raise ValueError(
                    f"先由【{other_responders[0].owner}】处理刚才的规则选择，"
                    "再决定是否保留检定结果。"
                )
            if silent_timeout:
                for pending in self._pending_pre_final_check_windows(
                    str(window.transaction_id or "").strip()
                ):
                    if pending.window_id == window.window_id:
                        continue
                    self.decision_window_manager.resolve(
                        window_id=pending.window_id,
                        responder=pending.owner,
                        resolution={"choice": "decline", "reason": "silent_failure_timeout"},
                    )
            source_windows = self.post_check_decisions.capture_source_windows(action)
            resolution = self._commit_check_transaction_acceptance(
                acceptance_action=action,
                transaction=transaction,
            )
            self.post_check_decisions.settle(
                action,
                resolution,
                source_windows=source_windows,
            )
            return resolution
        if window is not None and window.kind == "acceleration_benefit":
            actor = str(action.parameters.get("actor") or "").strip()
            selected = action.parameters.get("selected_option")
            selected = dict(selected) if isinstance(selected, dict) else {}
            choice = str(selected.get("choice") or action.parameters.get("choice") or "").strip()
            if choice != "decline":
                raise ValueError("【加速术】的顺势攻击或施法必须给出完整行动，不能只选择动作类别。")
            if actor != window.owner:
                raise ValueError(f"只有【{window.owner}】可以处理这次【加速术】选择。")
            completion = self.conflict_manager.complete_acceleration_turn_end(
                actor,
                benefit_used=False,
                effect_key=str(window.payload.get("effect_key") or ""),
            )
            self.decision_window_manager.resolve(
                window_id=window.window_id,
                responder=actor,
                resolution={"choice": "decline"},
            )
            return ActionResolution(
                action=action,
                rules_text=f"{actor}本回合不发动【加速术】；法术仍然持续。",
                payload={
                    "decision_window_id": window.window_id,
                    "decision_kind": window.kind,
                    "acceleration_completion": completion,
                    "resume_deferred_action": True,
                },
            )
        return self.decision_action_resolver.resolve(
            action,
            resolve_spell_parameter=self._resolve_spell_parameter_choice,
            resolve_investigation=self._resolve_investigate,
        )

    def _resolve_trust_check_decision(
        self,
        action: Action,
        window,
    ) -> ActionResolution:
        helper_name = str(action.parameters.get("actor") or "").strip()
        if helper_name != window.owner:
            raise ValueError(f"只有【{window.owner}】可以处理这次【予以信任】。")
        target_name = str(
            window.payload.get("source_actor")
            or window.payload.get("target")
            or ""
        ).strip()
        if not target_name or not self.character_manager.exists(target_name):
            raise ValueError("【予以信任】对应的受助角色已经不在当前检定中。")
        transaction = self.pending_check_transactions.get(target_name)
        if transaction is None:
            raise ValueError("【予以信任】对应的检定事务已经结束，不能再改写。")

        selected = action.parameters.get("selected_option")
        selected = (
            dict(selected)
            if isinstance(selected, dict)
            else {"choice": action.parameters.get("choice", "")}
        )
        choice = str(
            selected.get("choice") or action.parameters.get("choice") or ""
        ).strip()
        legal = any(
            all(option.get(key) == value for key, value in selected.items())
            for option in window.options
        )
        if not legal:
            raise ValueError("所选内容不在这次【予以信任】的合法选项中。")

        original_action = transaction.get("action")
        if not isinstance(original_action, Action):
            raise ValueError("【予以信任】缺少可恢复的原检定。")
        used_by = {
            str(name).strip()
            for name in original_action.parameters.get("_trust_assist_used_by", [])
            if str(name).strip()
        }
        if helper_name in used_by:
            raise ValueError(f"【{helper_name}】已经对这次检定使用过【予以信任】。")
        used_by.add(helper_name)
        original_action.parameters["_trust_assist_used_by"] = sorted(used_by)

        self.decision_window_manager.resolve(
            window_id=window.window_id,
            responder=helper_name,
            resolution={"choice": choice, "selected_option": selected},
        )
        transaction_id = str(window.transaction_id or "").strip()

        if choice == "decline":
            if self._pending_pre_final_check_windows(
                transaction_id,
                excluding={window.window_id},
            ):
                return ActionResolution(
                    action=action,
                    rules_text=f"{helper_name}不发动【予以信任】；这次检定仍在等待其他选择。",
                    payload={
                        "decision_window_id": window.window_id,
                        "decision_kind": window.kind,
                        "check_result_provisional": True,
                        "provisional_actor": target_name,
                        "decision_windows": self.decision_window_manager.public_summary(),
                    },
                )
            acceptance = Action(
                ActionType.RESOLVE_DECISION,
                {
                    "actor": target_name,
                    "window_id": window.window_id,
                    "post_check_acceptance": True,
                    "choice": "accept_result",
                },
            )
            resolution = self._commit_check_transaction_acceptance(
                acceptance_action=acceptance,
                transaction=transaction,
            )
            resolution.action = action
            resolution.rules_text = (
                f"{helper_name}不发动【予以信任】。{resolution.rules_text}"
            )
            resolution.payload.update(
                {
                    "decision_window_id": window.window_id,
                    "decision_kind": window.kind,
                    "trust_declined": True,
                }
            )
            return resolution

        target = self.character_manager.get(target_name)
        outcome = transaction.get("roll")
        if not hasattr(outcome, "actor"):
            raise ValueError("【予以信任】没有找到可改写的骰面。")
        if choice == "assist_trait":
            trait = str(selected.get("trait") or "").strip()
            if trait not in {
                str(target.identity or "").strip(),
                str(target.theme or "").strip(),
                str(target.origin or "").strip(),
            }:
                raise ValueError("【予以信任】只能援用受助角色的身份、主题或故乡。")
            adjusted = self.rules_engine.reroll_outcome(
                outcome,
                action.parameters.get(
                    "reroll_indices",
                    action.parameters.get("reroll_dice"),
                ),
                index_base=action.parameters.get("reroll_index_base"),
            )
            invocation_name = trait
            invocation_kind = "trusted_trait"
        elif choice == "assist_bond":
            bond_target = str(selected.get("bond_target") or "").strip()
            strength = target.bond_strength_with(bond_target)
            if strength <= 0:
                raise ValueError("【予以信任】选择的羁绊已经不存在。")
            adjusted = self.rules_engine.apply_bond_bonus(outcome, strength)
            invocation_name = bond_target
            invocation_kind = "trusted_bond"
        else:
            raise ValueError("【予以信任】只能选择援用特质、援用羁绊或不发动。")

        rank = skill_rank(self.character_manager.get(helper_name).skills, "予以信任")
        recovery = rank * 10 if self.character_manager.get(helper_name).bond_strength_with(target_name) > 0 else 0
        return self._replay_check_transaction(
            invocation_action=action,
            transaction=transaction,
            adjusted_outcome=adjusted,
            invocation_kind=invocation_kind,
            invocation_name=invocation_name,
            bond_strength=(
                target.bond_strength_with(invocation_name)
                if invocation_kind == "trusted_bond"
                else 0
            ),
            resource_payer_name=helper_name,
            assisted_actor_name=target_name,
            assisted_mp_recovery=recovery,
        )

    def _pending_pre_final_check_windows(
        self,
        transaction_id: str,
        *,
        excluding: set[str] | None = None,
    ) -> list:
        excluded = set(excluding or set())
        return [
            window
            for window in self.decision_window_manager.pending(blocking_only=True)
            if window.window_id not in excluded
            and (not transaction_id or window.transaction_id == transaction_id)
            and self.check_transaction_manager._is_pre_final_decision(window)
        ]

    def _validate_skill_action_followup(
        self,
        action: Action,
        *,
        after_commit: bool = False,
    ) -> None:
        window_id = str(
            action.parameters.get("_skill_followup_window_id") or ""
        ).strip()
        if not window_id:
            return
        actor_name = str(action.parameters.get("actor") or "").strip()
        window = self.decision_window_manager.get(window_id)
        if window is None or window.kind != "skill_parameter":
            raise ValueError("对应的技能顺势行动窗口不存在。")
        if (
            window.status.value != "pending"
            and not self._replaying_check_transaction
        ):
            raise ValueError("这次技能顺势行动已经处理完毕。")
        if actor_name != window.owner:
            raise ValueError(
                f"只有【{window.owner}】能执行这次技能顺势行动。"
            )
        skill = str(
            window.payload.get("skill")
            or window.payload.get("label")
            or ""
        ).strip()
        choice = str(action.parameters.get("choice") or "").strip()
        legal_choices = {
            str(option.get("choice") or "").strip()
            for option in window.options
            if str(option.get("choice") or "").strip()
        }
        if choice not in legal_choices:
            raise ValueError(
                f"【{choice or '未指定'}】不是【{skill}】当前窗口的合法选择。"
            )

        expected: dict[tuple[str, str], set[ActionType]] = {
            ("疾速身法", "attack"): {ActionType.ATTACK},
            ("疾速身法", "hinder_or_objective"): {
                ActionType.HINDER,
                ActionType.OBJECTIVE,
            },
            ("奥灵回响", "cast_spell"): {ActionType.SPELL},
            ("鹰眼", "immediate_ranged_attack"): {ActionType.ATTACK},
            ("应急用品", "use_inventory_action"): {
                ActionType.USE_INVENTORY,
            },
            ("快速评估", "declare_assessment"): {
                ActionType.SKILL,
            },
        }
        allowed = expected.get((skill, choice))
        if allowed is None or action.action_type not in allowed:
            raise ValueError(
                f"【{skill}】的选择【{choice}】不能用"
                f"【{action.action_type.value}】结算。"
            )
        actor = self.character_manager.get(actor_name)
        if skill == "疾速身法":
            if actor.mp < 10:
                raise ValueError("发动【疾速身法】需要 10 点精神值。")
            if action.action_type in {
                ActionType.HINDER,
                ActionType.OBJECTIVE,
            }:
                attributes = action.parameters.get("attributes")
                if not isinstance(attributes, list) or len(attributes) != 2:
                    raise ValueError(
                        "【疾速身法】的妨碍或推进目标检定必须明确两项属性。"
                    )
                if self._target_number_parameter(
                    action.parameters,
                    default=0,
                ) < 7:
                    raise ValueError(
                        "【疾速身法】的检定必须有不低于 7 的难度等级。"
                    )
                if (
                    action.action_type == ActionType.OBJECTIVE
                    and not self.clock_manager.exists(
                        str(
                            action.parameters.get("clock_name")
                            or action.parameters.get("target")
                            or ""
                        ).strip()
                    )
                ):
                    raise ValueError(
                        "【疾速身法】推进目标时必须指定一个已建立的命刻。"
                    )
        elif skill == "奥灵回响":
            if action.action_type != ActionType.SPELL:
                raise ValueError("【奥灵回响】只能顺势施放法术。")
        elif skill == "鹰眼":
            template_name = actor.equipment_templates.get(
                actor.equipped_main_hand,
                actor.equipped_main_hand,
            )
            weapon = get_equipment_example(template_name)
            if (
                weapon is None
                or weapon.category not in {"弓", "枪械"}
                or weapon.range_type != "ranged"
            ):
                raise ValueError(
                    "【鹰眼】的立即攻击要求装备弓类或枪械类武器。"
                )
            if action.parameters.get("_damage_high_roll_override") != 0:
                raise ValueError(
                    "【鹰眼】的立即攻击必须将伤害高值视为 0。"
                )
        elif skill == "应急用品":
            if not after_commit and not actor.in_crisis:
                raise ValueError(
                    "只有处于危机状态时才能发动【应急用品】。"
                )
            if "scene:skill:应急用品" in actor.trigger_cooldowns:
                raise ValueError(
                    "【应急用品】在本冲突场景已经发动过一次。"
                )
            if not str(
                action.parameters.get("item_name")
                or action.parameters.get("item")
                or ""
            ).strip():
                raise ValueError(
                    "【应急用品】必须实际选择一项消耗物资行动。"
                )
        elif skill == "快速评估":
            assessments = action.parameters.get("assessments")
            rank = skill_rank(actor.skills, "快速评估")
            if (
                not isinstance(assessments, list)
                or not assessments
                or len(assessments) > rank
            ):
                raise ValueError(
                    f"【快速评估】必须选择 1 至 {rank} 项合法评估。"
                )

    def _commit_skill_action_followup(
        self,
        resolution: ActionResolution,
    ) -> None:
        action = resolution.action
        window_id = str(
            action.parameters.get("_skill_followup_window_id") or ""
        ).strip()
        if not window_id:
            return
        if any(
            resolution.payload.get(flag)
            for flag in (
                "action_uncommitted",
                "spell_failed",
                "check_result_provisional",
            )
        ):
            return

        self._validate_skill_action_followup(
            action,
            after_commit=True,
        )
        actor_name = str(action.parameters.get("actor") or "").strip()
        window = self.decision_window_manager.get(window_id)
        if (
            window is None
            or window.kind != "skill_parameter"
            or window.status.value != "pending"
        ):
            raise ValueError("对应的技能顺势行动已经结束。")
        skill = str(
            window.payload.get("skill")
            or window.payload.get("label")
            or ""
        ).strip()
        choice = str(action.parameters.get("choice") or "").strip()

        if skill == "疾速身法":
            before, after = self.character_manager.modify_resource(
                actor_name,
                "mp",
                -10,
            )
            if before - after != 10:
                raise ValueError("发动【疾速身法】需要 10 点精神值。")
            resolution.payload["skill_resource_change"] = ResourceChange(
                target=actor_name,
                resource="mp",
                amount=after - before,
                before=before,
                after=after,
                reason="发动【疾速身法】。",
            )
        elif skill == "应急用品":
            self.character_manager.get(actor_name).trigger_cooldowns.add(
                "scene:skill:应急用品"
            )

        self.decision_window_manager.resolve(
            window_id=window_id,
            responder=actor_name,
            resolution={
                "choice": choice,
                "action_type": action.action_type.value,
                "target": action.parameters.get("target"),
                "spell_name": action.parameters.get("spell_name"),
                "item_name": (
                    action.parameters.get("item_name")
                    or action.parameters.get("item")
                ),
            },
        )
        resolution.rules_text = (
            f"【{skill}】触发。{resolution.rules_text}"
        ).strip()
        resolution.payload.update(
            {
                "skill_followup_resolved": True,
                "skill_name": skill,
                "skill_choice": choice,
                "decision_window_id": window_id,
                "decision_windows": (
                    self.decision_window_manager.public_summary()
                ),
            }
        )
        self._resume_deferred_turn_from_window(
            resolution.payload,
            window,
        )

    def _resume_deferred_turn_from_window(
        self,
        payload: dict[str, object],
        window,
    ) -> None:
        try:
            deferred_serial = int(
                window.payload.get("deferred_turn_serial") or 0
            )
        except (TypeError, ValueError):
            deferred_serial = 0
        if (
            not self.conflict_manager.state.active
            or deferred_serial <= 0
            or deferred_serial
            != int(self.conflict_manager.state.turn_serial or 0)
            or self.decision_window_manager.has_blocking()
        ):
            return
        payload["resume_deferred_action"] = True
        payload["deferred_action_type"] = str(
            window.payload.get("source_action_type") or "skill"
        )
        payload["deferred_action_owner"] = str(
            window.payload.get("deferred_turn_actor") or ""
        )

    def _validate_acceleration_followup(self, action: Action) -> None:
        window_id = str(action.parameters.get("_acceleration_window_id") or "").strip()
        if not window_id:
            return
        if action.action_type not in {ActionType.ATTACK, ActionType.SPELL}:
            raise ValueError("【加速术】只允许使用装备武器顺势攻击，或顺势施放法术。")
        actor = str(action.parameters.get("actor") or "").strip()
        window = self.decision_window_manager.get(window_id)
        if window is None or window.kind != "acceleration_benefit":
            raise ValueError("对应的【加速术】回合末选择不存在。")
        if actor != window.owner:
            raise ValueError(f"只有【{window.owner}】能使用这次【加速术】增益。")
        if self.conflict_manager.state.pending_turn_end_actor != actor:
            raise ValueError(f"【{actor}】当前不在【加速术】的回合末触发时机。")
        if window.status.value != "pending" and not self._replaying_check_transaction:
            raise ValueError("这次【加速术】增益已经处理完毕。")

    def _commit_acceleration_followup(self, resolution: ActionResolution) -> None:
        action = resolution.action
        window_id = str(action.parameters.get("_acceleration_window_id") or "").strip()
        if not window_id:
            return
        if any(
            resolution.payload.get(flag)
            for flag in (
                "action_uncommitted",
                "spell_failed",
                "check_result_provisional",
            )
        ):
            return

        actor = str(action.parameters.get("actor") or "").strip()
        window = self.decision_window_manager.get(window_id)
        if window is None or window.kind != "acceleration_benefit":
            raise ValueError("对应的【加速术】回合末选择不存在。")
        self._validate_acceleration_followup(action)
        choice = "attack" if action.action_type == ActionType.ATTACK else "cast_spell"
        if window.status.value == "pending":
            self.decision_window_manager.resolve(
                window_id=window.window_id,
                responder=actor,
                resolution={
                    "choice": choice,
                    "action_type": action.action_type.value,
                    "target": action.parameters.get("target"),
                    "spell_name": action.parameters.get("spell_name"),
                },
            )
        completion = self.conflict_manager.complete_acceleration_turn_end(
            actor,
            benefit_used=True,
            effect_key=str(window.payload.get("effect_key") or ""),
        )
        ending = "；这已是第二次受益，【加速术】随即结束" if completion["effect_expired"] else ""
        resolution.rules_text = f"【加速术】触发。{resolution.rules_text}{ending}。".replace("。。", "。")
        resolution.payload.update(
            {
                "acceleration_benefit_used": True,
                "acceleration_choice": choice,
                "acceleration_completion": completion,
                "decision_window_id": window.window_id,
                "resume_deferred_action": True,
            }
        )

    def _commit_immediate_attack_followup(
        self,
        resolution: ActionResolution,
    ) -> None:
        action = resolution.action
        window_id = str(
            action.parameters.get("_immediate_attack_window_id") or ""
        ).strip()
        if not window_id:
            return
        if any(
            resolution.payload.get(flag)
            for flag in (
                "action_uncommitted",
                "check_result_provisional",
            )
        ):
            return
        if action.action_type != ActionType.ATTACK:
            raise ValueError("【抢攻】待决窗口只能结算一次顺势攻击。")
        actor = str(action.parameters.get("actor") or "").strip()
        target = str(action.parameters.get("target") or "").strip()
        window = self.decision_window_manager.get(window_id)
        if window is None or window.kind != "immediate_attack":
            raise ValueError("对应的【抢攻】待决窗口不存在。")
        if window.status.value != "pending":
            raise ValueError("这次【抢攻】顺势攻击已经处理完毕。")
        if actor != window.owner:
            raise ValueError(f"只有【{window.owner}】能使用这次【抢攻】。")
        legal_targets = {
            str(item)
            for item in window.payload.get("legal_targets", [])
            if str(item)
        }
        if target not in legal_targets:
            raise ValueError(f"【{target}】不是这次【抢攻】的合法目标。")
        self.decision_window_manager.resolve(
            window_id=window_id,
            responder=actor,
            resolution={
                "choice": "attack",
                "target": target,
                "action_type": ActionType.ATTACK.value,
            },
        )
        resolution.rules_text = f"【抢攻】触发。{resolution.rules_text}".strip()
        resolution.payload.update(
            {
                "immediate_attack_resolved": True,
                "decision_window_id": window_id,
                "decision_windows": (
                    self.decision_window_manager.public_summary()
                ),
            }
        )
        self._resume_deferred_turn_from_window(
            resolution.payload,
            window,
        )

    def _resolve_spell_parameter_choice(
        self,
        action: Action,
        window,
        selected: dict[str, object],
    ) -> ActionResolution:
        spell_name = str(window.payload.get("spell_name") or "").strip()
        definition = get_spell_definition(spell_name)
        resumed_action = self.spell_parameter_manager.resume_action(window, selected, definition)
        # Resolve first so an unexpected execution error leaves the persisted
        # choice available for a retry rather than consuming it silently.
        resumed = self._resolve_spell(resumed_action)
        resolved_window = self.decision_window_manager.resolve(
            window_id=window.window_id,
            responder=window.owner,
            resolution={
                "choice": "cast_spell",
                "selected_option": dict(selected),
            },
        )
        resumed.payload.update(
            {
                "decision_window_id": window.window_id,
                "decision_kind": window.kind,
                "decision_resolution": dict(resolved_window.resolution),
                "spell_parameters_resolved": True,
                "committed_source_action": deepcopy(resumed_action),
                "decision_windows": self.decision_window_manager.public_summary(),
            }
        )
        return resumed

    def _resolve_out_of_turn_action(self, action: Action) -> ActionResolution | None:
        if not self.conflict_manager.state.active:
            return None
        if not action.parameters.get("_enforce_turn_order"):
            return None
        if action.action_type not in self.TURN_CONSUMING_ACTIONS:
            return None
        actor_name = self._action_actor_name(action)
        if not actor_name or not self.character_manager.exists(actor_name):
            return None
        current_actor = self.conflict_manager.state.current_actor()
        if not current_actor or actor_name == current_actor:
            return None
        if self._is_explicit_assist_for_current_actor(action, actor_name, current_actor):
            if self.conflict_manager.register_team_assist(
                actor_name,
                current_actor,
                reason=self._action_summary(action),
            ):
                return ActionResolution(
                    action=action,
                    rules_text=(
                        f"{actor_name} 把本轮行动投入到协助 {current_actor}。"
                        f"{current_actor} 的下一次检定会获得团队合作加成。"
                    ),
                    payload={
                        "team_assist_registered": True,
                        "out_of_turn": True,
                        "supporter": actor_name,
                        "leader": current_actor,
                        "turn_board": self.conflict_manager.format_turn_board(),
                    },
                )
            return ActionResolution(
                action=action,
                rules_text=(
                    f"{actor_name} 想协助 {current_actor}，但这次协助暂时不能登记。"
                    "可能是该角色已经行动过，或协助对象不是当前行动者。"
                ),
                payload={
                    "out_of_turn": True,
                    "team_assist_rejected": True,
                    "supporter": actor_name,
                    "leader": current_actor,
                    "turn_board": self.conflict_manager.format_turn_board(),
                },
            )

        if (
            action.parameters.get("_turn_timing") != "defer"
            and self.conflict_manager.claim_current_side_turn(actor_name)
        ):
            return None

        summary = self._action_summary(action)
        held_parameters = {
            key: deepcopy(value)
            for key, value in action.parameters.items()
            if key not in {"_enforce_turn_order", "_speaker", "player_facing_reply"}
        }
        speaker = str(action.parameters.get("_speaker") or "").strip()
        held = self.conflict_manager.register_held_action(
            actor_name,
            action.action_type.value,
            summary,
            speaker=speaker,
            action_parameters=held_parameters,
        )
        mention = f"@{speaker}" if speaker else f"【{actor_name}】"
        return ActionResolution(
            action=action,
            rules_text=(
                f"{mention}，不准插队～现在是【{current_actor}】的回合；"
                f"你的行动我先缓存。轮到【{actor_name}】时，我会提醒你确认或改行动。"
            ),
            payload={
                "out_of_turn": True,
                "held_action": held,
                "current_actor": current_actor,
                "actor": actor_name,
                "turn_board": self.conflict_manager.format_turn_board(),
            },
        )

    def _is_explicit_assist_for_current_actor(self, action: Action, actor_name: str, current_actor: str) -> bool:
        text = " ".join(
            str(action.parameters.get(key) or "")
            for key in ("reasoning", "in_mind_reply", "summary", "description", "intent", "note")
        )
        has_assist_signal = any(token in text for token in ("协助", "支援", "帮忙", "帮助", "配合", "teamwork", "assist", "support"))
        if not has_assist_signal:
            return False
        explicit_target = str(
            action.parameters.get("assist_target")
            or action.parameters.get("leader")
            or action.parameters.get("supported_actor")
            or ""
        ).strip()
        return explicit_target == current_actor or explicit_target in {"当前行动者", "当前回合角色", "轮到的人"}

    def _action_summary(self, action: Action) -> str:
        parts = [
            str(action.parameters.get("summary") or "").strip(),
            str(action.parameters.get("reasoning") or "").strip(),
            str(action.parameters.get("intent") or "").strip(),
        ]
        target = str(
            action.parameters.get("target")
            or action.parameters.get("clock_name")
            or action.parameters.get("spell_name")
            or action.parameters.get("skill_name")
            or ""
        ).strip()
        if target:
            parts.append(f"目标：{target}")
        summary = "；".join(part for part in parts if part)
        return summary or f"{action.action_type.value} 行动"

    def _finalize_resolution(self, resolution: ActionResolution) -> ActionResolution:
        self._attach_npc_interposition_notice(resolution)
        if resolution.payload.pop("_already_finalized", False):
            self.check_batch_manager.observe_resolution(resolution)
            self._progress_check_batches(resolution)
            self._drain_skill_lifecycle_events(resolution)
            if self.conflict_manager.state.active:
                resolution.payload.setdefault(
                    "turn_board",
                    self.conflict_manager.format_turn_board(),
                )
                resolution.payload.setdefault(
                    "combat_log",
                    self.conflict_manager.format_combat_log(),
                )
            return resolution
        self._ensure_primary_roll_specials(resolution)
        self._attach_post_check_windows(resolution)
        self._bind_reactive_check_windows(resolution)
        self._drain_skill_lifecycle_events(resolution)
        self._store_check_transaction(resolution)
        self._finalize_npc_condition_windows(resolution)
        self._commit_counter_followups(resolution)
        self.check_batch_manager.observe_resolution(resolution)
        self._progress_check_batches(resolution)
        self._drain_skill_lifecycle_events(resolution)
        self._commit_acceleration_followup(resolution)
        self._commit_immediate_attack_followup(resolution)
        self._commit_skill_action_followup(resolution)
        resume = resolution.payload.pop("_decision_resume_candidate", None)
        if isinstance(resume, dict):
            owner = str(resume.get("owner") or "")
            if not self.decision_window_manager.has_blocking(owner=owner):
                resolution.payload["resume_deferred_action"] = True
                resolution.payload["deferred_action_type"] = str(resume.get("source_action_type") or "")
                resolution.payload["deferred_action_owner"] = owner
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
        if (
            action.action_type in loggable_actions
            and resolution.rules_text
            and not action.parameters.get("_check_batch_roll")
        ):
            actor = action.parameters.get("actor") or action.parameters.get("target") or self.conflict_manager.state.current_actor() or "system"
            event_type = action.parameters.get("npc_action_type") if action.action_type == ActionType.NPCACT else action.action_type.value
            self.conflict_manager.record_log(str(actor), str(event_type), resolution.rules_text)
            resolution.payload["combat_log"] = self.conflict_manager.format_combat_log()
            resolution.payload["turn_board"] = self.conflict_manager.format_turn_board()
        return resolution

    def _drain_skill_lifecycle_events(
        self,
        resolution: ActionResolution,
    ) -> None:
        lifecycle_batch = self.skill_lifecycle_events.drain()
        if lifecycle_batch.records:
            records = list(lifecycle_batch.records)
            resolution.payload.setdefault("skill_trigger_events", []).extend(records)
            summaries = [str(record.get("summary") or "") for record in records if record.get("summary")]
            if summaries:
                resolution.rules_text = (resolution.rules_text + " " + " ".join(summaries)).strip()
        if lifecycle_batch.windows:
            resolution.payload.setdefault("skill_decision_windows", []).extend(
                list(lifecycle_batch.windows)
            )
            resolution.payload["decision_windows"] = self.decision_window_manager.public_summary()

    def _commit_counter_followups(self, resolution: ActionResolution) -> None:
        followups = resolution.payload.get("counter_followups")
        if (
            not isinstance(followups, list)
            or resolution.payload.get("check_result_provisional")
            or resolution.payload.get("_counter_followups_processed")
        ):
            return
        resolution.payload["_counter_followups_processed"] = True
        events = resolution.payload.setdefault("reaction_events", [])
        for followup in followups:
            if not isinstance(followup, dict):
                continue
            if not followup.get("triggered"):
                events.append(dict(followup))
                continue
            parameters = followup.get("action_parameters")
            if not isinstance(parameters, dict):
                continue
            counter = self.resolve(
                Action(
                    ActionType.ATTACK,
                    deepcopy(parameters),
                )
            )
            event = {
                "actor": str(followup.get("actor") or ""),
                "skill_name": "反击",
                "triggered": True,
                "roll": counter.payload.get("roll"),
                "rules_text": counter.rules_text,
                "check_result_provisional": bool(
                    counter.payload.get("check_result_provisional")
                ),
                "decision_windows": counter.payload.get("decision_windows", []),
            }
            if counter.payload.get("conflict_event") is not None:
                event["conflict_event"] = counter.payload["conflict_event"]
            events.append(event)
            resolution.rules_text = (
                f"{resolution.rules_text} {str(counter.rules_text or '').strip()}"
            ).strip()
        resolution.payload["decision_windows"] = (
            self.decision_window_manager.public_summary()
        )

    def _ensure_primary_roll_specials(self, resolution: ActionResolution) -> None:
        """Apply universal critical/fumble effects to every exposed PC check."""

        outcome = resolution.payload.get("roll")
        if outcome is None or not hasattr(outcome, "actor"):
            return
        actor_name = str(getattr(outcome, "actor", "") or "").strip()
        if not actor_name or not self.character_manager.exists(actor_name):
            return
        actor = self.character_manager.get(actor_name)

        if bool(getattr(outcome, "fumble", False)) and "pc" in actor.traits:
            if "fabula_gain" not in resolution.payload:
                before, after = self.character_manager.modify_resource(
                    actor_name,
                    "fabula_points",
                    1,
                )
                resolution.payload["fabula_gain"] = ResourceChange(
                    target=actor_name,
                    resource="fabula_points",
                    amount=after - before,
                    before=before,
                    after=after,
                    reason="大失败获得 1 点物语点。",
                )
            if "大失败" not in resolution.rules_text:
                resolution.rules_text = (
                    f"{resolution.rules_text} 触发大失败；对手获得 1 次机会，"
                    f"{actor_name} 获得 1 点物语点。"
                ).strip()

        if not (
            bool(getattr(outcome, "critical_success", False))
            or bool(getattr(outcome, "fumble", False))
        ):
            return
        if "trigger_results" in resolution.payload:
            return
        trigger_results = (
            self.trigger_manager.on_critical_success(actor_name)
            if bool(getattr(outcome, "critical_success", False))
            else self.trigger_manager.on_fumble(actor_name)
        )
        self._append_trigger_results(resolution.payload, trigger_results)
        resolution.rules_text += self._trigger_rules_text(trigger_results)

    def _on_character_resource_change(self, name: str, resource: str, before: int, after: int) -> None:
        if not self.character_manager.exists(name):
            return
        actor = self.character_manager.get(name)
        if resource == "fabula_points" and after < before:
            outcome = self.skill_lifecycle.trigger(
                "after_spend_fabula",
                actor,
                amount_spent=before - after,
            )
            self._capture_skill_lifecycle(outcome)
            return
        if resource != "hp" or after >= before:
            return
        damage_source_name = self._damage_source_name.get()
        source = (
            self.character_manager.get(damage_source_name)
            if damage_source_name and self.character_manager.exists(damage_source_name)
            else None
        )
        outcome = self.skill_lifecycle.trigger(
            "after_receive_damage",
            actor,
            target=source,
            hp_lost=before - after,
            source_name=source.name if source is not None else "",
        )
        self._capture_skill_lifecycle(outcome)
        threshold = actor.crisis_threshold if actor.crisis_threshold > 0 else actor.max_hp // 2
        if before > threshold >= after:
            crisis = self.skill_lifecycle.trigger(
                "enter_crisis",
                actor,
                visible_targets=[character.name for character in self.character_manager.all() if character.name != actor.name],
            )
            self._capture_skill_lifecycle(crisis)
        swallow_changes = self.npc_conditions.advance_for_source_damage(name)
        if swallow_changes:
            records = []
            for change in swallow_changes:
                summary = (
                    f"【{name}】受伤使【{change['target']}】的脱困命刻推进至"
                    f" {change['after']}/{change['max_segments']}。"
                )
                if change.get("released"):
                    summary += f"【{change['target']}】随即脱困。"
                records.append({**change, "source": "松弛之攫", "summary": summary})
            self._capture_skill_lifecycle(
                SkillLifecycleOutcome(
                    event="npc_source_damaged",
                    result=self.skill_trigger_manager.emit(
                        "npc_source_damaged",
                        actor,
                    ),
                    records=records,
                )
            )

    def _on_conflict_turn_start(self, actor_name: str, turn_serial: int) -> None:
        if not self.character_manager.exists(actor_name):
            return
        self._apply_swallowed_turn_start_damage(actor_name)
        if self.loyal_companion_manager is not None:
            self.loyal_companion_manager.on_owner_turn_start(
                actor_name,
                turn_serial,
            )
        outcome = self.skill_lifecycle.trigger(
            "turn_start",
            self.character_manager.get(actor_name),
            turn_serial=turn_serial,
        )
        self._capture_skill_lifecycle(outcome)

    def _apply_swallowed_turn_start_damage(self, actor_name: str) -> None:
        swallowed = self.npc_conditions.swallowed(actor_name)
        if swallowed is None:
            return
        target = self.character_manager.get(actor_name)
        source = (
            self.character_manager.get(swallowed.source)
            if self.character_manager.exists(swallowed.source)
            else None
        )
        damage, affinity = self.rules_engine.compute_damage(
            high_roll=0,
            weapon_damage=swallowed.damage,
            damage_type=swallowed.damage_type,
            target=target,
        )
        if damage >= 0:
            before, after = self._apply_damage_from(
                swallowed.source,
                actor_name,
                damage,
            )
        else:
            before, after = self.character_manager.modify_resource(
                actor_name,
                "hp",
                -damage,
            )
        payload: dict[str, object] = {}
        source_action = Action(
            ActionType.MODIFY_RESOURCE,
            {
                "actor": swallowed.source,
                "target": actor_name,
                "damage_type": swallowed.damage_type,
                "source": "吞噬",
            },
        )
        if source is not None:
            self._apply_combat_trait_after_damage(
                actor_name,
                affinity,
                abs(damage),
                payload,
                hp_before=before,
                action=source_action,
                source_actor=swallowed.source,
                is_spell=False,
            )
        event = None
        if after == 0:
            after, event, _ = self._resolve_zero_hp_after_damage(
                source_action,
                source_actor=swallowed.source,
                target_name=actor_name,
                payload=payload,
                damage_type=swallowed.damage_type,
            )
        record: dict[str, object] = {
            "source": "吞噬",
            "target": actor_name,
            "damage": abs(damage),
            "damage_type": swallowed.damage_type,
            "affinity": affinity.value,
            "hp_before": before,
            "hp_after": after,
            "summary": (
                f"【{actor_name}】在【{swallowed.source}】体内受到"
                f" {abs(damage)} 点物理伤害。"
            ),
        }
        if event is not None:
            record["conflict_event"] = event
            record["summary"] = f"{record['summary']}{event.summary}"
        self._capture_skill_lifecycle(
            SkillLifecycleOutcome(
                event="swallowed_turn_start",
                result=self.skill_trigger_manager.emit(
                    "swallowed_turn_start",
                    target,
                    source=swallowed.source,
                ),
                records=[record],
            )
        )

    def _capture_skill_lifecycle(self, outcome: SkillLifecycleOutcome) -> None:
        self._bind_skill_windows_to_source_action(outcome)
        enriched_records: list[dict[str, object]] = []
        for record in outcome.records:
            enriched = dict(record)
            source = str(enriched.get("source") or "")
            amount = int(enriched.get("amount", 0) or 0)
            resource = str(enriched.get("resource") or "")
            target = str(enriched.get("target") or "")
            if source and amount > 0 and resource:
                label = {"hp": "HP", "mp": "MP", "inventory_points": "物资点"}.get(resource, resource)
                enriched["summary"] = f"【{source}】使{target}恢复 {amount} {label}。"
            elif source == "身负黑血":
                enriched["summary"] = f"【身负黑血】生效，{target}对暗系和毒系伤害获得抵抗。"
            enriched_records.append(enriched)
        self.skill_lifecycle_events.capture(
            SkillLifecycleOutcome(
                event=outcome.event,
                result=outcome.result,
                records=enriched_records,
                windows=list(outcome.windows),
            )
        )

    def _bind_skill_windows_to_source_action(
        self,
        outcome: SkillLifecycleOutcome,
    ) -> None:
        """Persist which interrupted conflict action a skill choice must resume."""

        if not self.conflict_manager.state.active:
            return
        action = self._active_rule_action.get()
        if action is None:
            return
        (
            deferred_actor,
            deferred_serial,
            source_action_type,
            resume_point,
        ) = self._deferred_turn_lineage_for_action(action)
        if not deferred_actor or deferred_serial <= 0 or not resume_point:
            return
        for summary in outcome.windows:
            window_id = str(summary.get("window_id") or "").strip()
            window = self.decision_window_manager.get(window_id)
            if window is None or not window.blocking:
                continue
            window.resume_point = resume_point
            window.payload.update(
                {
                    "deferred_turn_actor": deferred_actor,
                    "deferred_turn_serial": deferred_serial,
                    "source_action_type": source_action_type,
                    "resume_point": resume_point,
                }
            )

    def _deferred_turn_lineage_for_action(
        self,
        action: Action,
    ) -> tuple[str, int, str, str]:
        """Return the original normal turn paused by a nested follow-up."""

        source_action_type = action.action_type.value
        parent_window_id = str(
            action.parameters.get("_skill_followup_window_id")
            or action.parameters.get("_immediate_attack_window_id")
            or action.parameters.get("_acceleration_window_id")
            or ""
        ).strip()
        if parent_window_id:
            parent = self.decision_window_manager.get(parent_window_id)
            if parent is not None:
                try:
                    deferred_serial = int(
                        parent.payload.get("deferred_turn_serial") or 0
                    )
                except (TypeError, ValueError):
                    deferred_serial = 0
                deferred_actor = str(
                    parent.payload.get("deferred_turn_actor") or ""
                ).strip()
                resume_point = str(
                    parent.resume_point
                    or parent.payload.get("resume_point")
                    or ""
                ).strip()
                if (
                    not deferred_actor
                    and parent.kind == "acceleration_benefit"
                    and resume_point == "conflict_turn_end"
                ):
                    deferred_actor = parent.owner
                    deferred_serial = int(
                        self.conflict_manager.state.turn_serial or 0
                    )
                source_action_type = str(
                    parent.payload.get("source_action_type")
                    or (
                        "acceleration"
                        if parent.kind == "acceleration_benefit"
                        else source_action_type
                    )
                ).strip()
                if deferred_actor and deferred_serial > 0 and resume_point:
                    return (
                        deferred_actor,
                        deferred_serial,
                        source_action_type,
                        resume_point,
                    )

        if self._action_defers_conflict_turn(action):
            return (
                str(self.conflict_manager.state.current_actor() or "").strip(),
                int(self.conflict_manager.state.turn_serial or 0),
                source_action_type,
                "conflict_action_end",
            )
        return "", 0, source_action_type, ""

    def _action_defers_conflict_turn(self, action: Action) -> bool:
        if not self.conflict_manager.state.active:
            return False
        if action.parameters.get("opportunity_action"):
            return False
        actor = self._action_actor_name(action)
        if not actor or actor != self.conflict_manager.state.current_actor():
            return False
        if action.action_type not in self.TURN_CONSUMING_ACTIONS:
            return False
        if action.action_type == ActionType.SKILL:
            skill_name = self._normalized_skill_name(
                str(action.parameters.get("skill_name") or "")
            )
            mode = str(action.parameters.get("mode") or "").strip().lower()
            if skill_name == "契约与召唤" and mode in {
                "dismiss",
                "release",
                "解除",
                "解除阿卡纳",
                "遣散",
                "遣散奥灵",
                "释放",
                "解放",
            }:
                return False
        return True

    def _apply_damage_from(self, source_name: str, target_name: str, amount: int) -> tuple[int, int]:
        token = self._damage_source_name.set(str(source_name or ""))
        try:
            return self.character_manager.apply_damage(target_name, amount)
        finally:
            self._damage_source_name.reset(token)

    def _single_target_hit_bonus(
        self,
        actor: Character,
        target: Character,
    ) -> tuple[int, list[dict[str, object]]]:
        result = self.skill_trigger_manager.emit(
            "after_single_target_hit",
            actor,
            target=target,
            single_target=True,
            target_status_count=len(target.statuses),
        )
        effects = [
            {"source": effect.source, "amount": effect.amount, "note": effect.note}
            for effect in result.effects
            if effect.amount > 0
        ]
        return sum(int(effect["amount"]) for effect in effects), effects

    def _after_actor_deals_damage(
        self,
        actor: Character,
        target: Character,
        hp_before: int,
        hp_after: int,
    ) -> None:
        if hp_after >= hp_before:
            return
        serial = int(getattr(self.conflict_manager.state, "turn_serial", 0) or 0)
        cooldown_key = f"scene:turn:{self.conflict_manager.state.scene_name}:{serial}:痛楚"
        available = cooldown_key not in actor.trigger_cooldowns
        outcome = self.skill_lifecycle.trigger(
            "after_deal_damage",
            actor,
            target=target,
            hp_lost=hp_before - hp_after,
            once_per_turn_available=available,
        )
        if any(record.get("source") == "痛楚" for record in outcome.records):
            actor.trigger_cooldowns.add(cooldown_key)
        self._capture_skill_lifecycle(outcome)

    def _build_check_transaction_candidate(self, action: Action) -> dict[str, object] | None:
        return self.check_transaction_manager.build_candidate(action)

    def _action_actor_name(self, action: Action) -> str:
        return self.check_transaction_manager.actor_name_for_action(action)

    def _snapshot_check_state(self) -> dict[str, object]:
        return self.check_transaction_manager.snapshot()

    def _restore_check_state(self, snapshot: dict[str, object]) -> None:
        self.check_transaction_manager.restore(snapshot)

    def _store_check_transaction(self, resolution: ActionResolution) -> None:
        self.check_transaction_manager.stage(resolution)

    def _attach_post_check_windows(self, resolution: ActionResolution) -> None:
        self.post_check_decisions.attach(resolution)

    def _bind_reactive_check_windows(self, resolution: ActionResolution) -> None:
        parent_id = self._reactive_check_parent_id(resolution)
        if not parent_id:
            return
        parent = self.decision_window_manager.get(parent_id)
        if parent is None:
            return
        lineage = {
            "deferred_turn_actor": str(
                parent.payload.get("deferred_turn_actor") or ""
            ),
            "deferred_turn_serial": int(
                parent.payload.get("deferred_turn_serial") or 0
            ),
            "source_action_type": str(
                parent.payload.get("source_action_type") or "Attack"
            ),
            "resume_point": str(
                parent.payload.get("resume_point")
                or parent.resume_point
                or "conflict_action_end"
            ),
            "reactive_check_window_id": parent_id,
        }
        for summary in [
            *(resolution.payload.get("post_check_windows") or []),
            *(resolution.payload.get("skill_decision_windows") or []),
            *(resolution.payload.get("decision_windows") or []),
        ]:
            if not isinstance(summary, dict):
                continue
            window_id = str(summary.get("window_id") or "").strip()
            window = self.decision_window_manager.get(window_id)
            if window is None:
                continue
            window.payload.update(lineage)
            window.resume_point = lineage["resume_point"]

    def _reactive_check_parent_id(
        self,
        resolution: ActionResolution,
    ) -> str:
        direct = str(
            resolution.action.parameters.get("_reactive_check_window_id") or ""
        ).strip()
        if direct:
            return direct
        source_window_id = str(
            resolution.action.parameters.get("window_id") or ""
        ).strip()
        if not source_window_id:
            return ""
        source_window = self.decision_window_manager.get(source_window_id)
        if source_window is None:
            return ""
        return str(
            source_window.payload.get("reactive_check_window_id") or ""
        ).strip()

    def _finalize_npc_condition_windows(
        self,
        resolution: ActionResolution,
    ) -> None:
        released = self.npc_conditions.release_completed()
        if released:
            resolution.payload["released_swallowed_targets"] = [
                {
                    "source": state.source,
                    "target": state.target,
                    "clock_name": state.escape_clock,
                }
                for state in released
            ]

        parent_id = self._reactive_check_parent_id(resolution)
        if not parent_id:
            return
        parent = self.decision_window_manager.find_pending(window_id=parent_id)
        outcome = resolution.payload.get("roll")
        if parent is None:
            return
        if outcome is not None and hasattr(outcome, "success"):
            parent.payload["reactive_check_success"] = bool(outcome.success)
            parent.payload["reactive_check_total"] = int(outcome.total)
        if resolution.payload.get("check_result_provisional"):
            return
        other_blockers = [
            window
            for window in self.decision_window_manager.pending(blocking_only=True)
            if window.window_id != parent.window_id
        ]
        if other_blockers:
            return
        if outcome is not None and hasattr(outcome, "success"):
            succeeded = bool(outcome.success)
        elif "reactive_check_success" in parent.payload:
            succeeded = bool(parent.payload["reactive_check_success"])
        else:
            return
        target = parent.owner
        failed = not succeeded
        condition = str(
            parent.payload.get("failure_condition") or "petrified"
        ).strip()
        applied = False
        if failed:
            applied = self.conflict_manager.incapacitate_persistently(
                target,
                condition=condition,
                note=str(parent.payload.get("failure_note") or "石化"),
            )
        self.decision_window_manager.resolve(
            window_id=parent.window_id,
            responder=target,
            resolution={
                "choice": "roll",
                "success": not failed,
                "condition": condition if failed else "",
            },
        )
        resolution.payload["reactive_check_resolved"] = {
            "window_id": parent.window_id,
            "target": target,
            "success": not failed,
            "condition": condition if failed else "",
            "condition_applied": applied,
        }
        if not self.decision_window_manager.has_blocking():
            resolution.payload["resume_deferred_action"] = True
            resolution.payload["deferred_action_type"] = str(
                parent.payload.get("source_action_type") or "Attack"
            )
            resolution.payload["deferred_action_owner"] = str(
                parent.payload.get("deferred_turn_actor") or ""
            )

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

    def _zero_hp_source_actor(self, action: Action, fallback: str) -> str:
        """Return the PC who owns a controlled creature's final blow."""

        requested = str(action.parameters.get("_fate_owner") or "").strip()
        if (
            requested
            and self.character_manager.exists(requested)
            and "pc" in self.character_manager.get(requested).traits
        ):
            return requested
        return fallback

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
        if params.get("post_check_acceptance"):
            return ActionResolution(
                action=action,
                rules_text=str(params.get("summary") or "刚才的检定结果保留。"),
                payload={"post_check_acceptance": True},
            )
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
            if (
                self.character_manager.exists(name)
                and "pc" in self.character_manager.get(name).traits
            ):
                # Player-owned character state is committed by character and
                # rule tools, never through an NPC narration patch.
                continue
            persona = self.world_state.ensure_npc_persona(
                name,
                aliases=self._string_list(item.get("aliases")),
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
                current_location=str(item.get("current_location") or item.get("location") or ""),
                current_mood=str(item.get("current_mood") or item.get("mood") or ""),
                current_stance=str(item.get("current_stance") or item.get("stance") or ""),
                active_goal=str(item.get("active_goal") or ""),
                last_seen_scene=str(item.get("scene_id") or params.get("scene_id") or ""),
                voice_examples=self._string_list(item.get("voice_examples")),
            )
            relationships = item.get("relationships")
            if isinstance(relationships, dict):
                for target, relationship in relationships.items():
                    if str(target).strip() and str(relationship).strip():
                        persona.relationships[str(target).strip()] = str(relationship).strip()
            completed_goal = str(item.get("completed_goal") or "").strip()
            if completed_goal:
                self.world_state.update_npc_state(name, completed_goal=completed_goal)
            note = str(item.get("note") or item.get("memory") or item.get("event") or "").strip()
            if note:
                try:
                    salience = int(item.get("salience", 2) or 2)
                except (TypeError, ValueError):
                    salience = 2
                self.world_state.remember_npc_event(
                    name,
                    note,
                    scene_id=str(item.get("scene_id") or params.get("scene_id") or ""),
                    source="narrate.npc_updates",
                    salience=salience,
                )
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
            if source in self.world_state.npc_personas:
                self.world_state.update_npc_state(
                    source,
                    relationship_target=target,
                    relationship=relation,
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
        rare_material_item_id = str(
            action.parameters.get("_rare_material_item_id") or ""
        ).strip()
        if (
            action.parameters.get("rare_material")
            and action.parameters.get("_strict_tool_transaction")
            and not rare_material_item_id
        ):
            raise ValueError("仪式半价素材必须来自施法者实际持有的剧情物件。")
        ritual_name = self._ritual_name(action.parameters["name"])
        effect = self._sanitize_freeform_effect(action.parameters.get("effect", ""))
        plan = manager.plan_ritual(
            caster=caster,
            name=ritual_name,
            discipline=self._ritual_discipline(action.parameters.get("discipline", "ritualism")),
            potency=self._ritual_potency(action.parameters.get("potency", "minor")),
            scope=self._ritual_scope(action.parameters.get("scope", "individual")),
            effect=effect,
            attributes=action.parameters.get("attributes"),
            rare_material=action.parameters.get("rare_material", ""),
            forbidden_tags=action.parameters.get("forbidden_tags", []),
            enforce_permission=action.parameters.get("enforce_permission", True),
        )
        clock_change = None
        should_track_clock = bool(action.parameters.get("track_clock", False))
        starts_conflict_clock = bool(
            action.parameters.get("start_conflict_clock", False)
            or action.parameters.get("conflict_ritual", False)
            or self.conflict_manager.state.active
        )
        if starts_conflict_clock or should_track_clock:
            outcome = manager.rules_engine.roll_check(
                actor=manager.character_manager.get(plan.caster),
                attributes=plan.attributes,
                target_number=plan.target_number,
                modifier=self._int_parameter(action.parameters, "modifier", 0),
                reason=action.parameters.get("reasoning", f"启动仪式【{plan.name}】"),
            )
            payload = {"ritual_plan": plan, "roll": outcome, "ritual_start_check": True}
            if not outcome.success:
                self.world_state.add_memory(
                    f"仪式启动失败：{plan.caster} 准备【{plan.name}】，"
                    f"{outcome.total} 对抗难度等级 {plan.target_number}。"
                )
                return ActionResolution(
                    action=action,
                    rules_text=(
                        f"{plan.caster} 尝试启动仪式【{plan.name}】：{outcome.total} 对抗难度等级 "
                        f"{plan.target_number}，失败。仪式命刻没有建立。"
                    ),
                    payload=payload,
                )

            current_scene = (
                self.scene_manager.current_scene
                if self.scene_manager is not None
                else None
            )
            manager.start_conflict_ritual(
                plan,
                scene_id=str(getattr(current_scene, "scene_id", "") or ""),
                turn_serial=int(self.conflict_manager.state.turn_serial or 0),
            )
            clock = self.clock_manager.get(plan.clock_name)
            delta = manager.rules_engine.clock_segments_from_roll(
                outcome,
                spend_critical_opportunity=bool(action.parameters.get("spend_critical_opportunity_on_clock", False)),
            )
            before, after = self.clock_manager.advance(plan.clock_name, delta)
            if after >= clock.max_segments:
                manager.mark_ready(
                    plan.clock_name,
                    turn_serial=int(self.conflict_manager.state.turn_serial or 0),
                )
            actual_delta = after - before
            clock_change = ClockChange(
                clock_name=clock.name,
                before=before,
                after=after,
                delta=actual_delta,
                max_segments=clock.max_segments,
                reason="启动仪式并推进仪式命刻。",
                clock_type=clock.clock_type,
                stakes=clock.stakes,
                completion_consequence=clock.completion_consequence,
            )
            payload["clock_change"] = clock_change
            if rare_material_item_id:
                consumed = self._consume_story_material(
                    item_id=rare_material_item_id,
                    actor=str(caster or ""),
                    purpose=f"启动仪式【{plan.name}】",
                )
                plan.notes.append(f"已消耗稀有施法材料【{consumed.name}】。")
                payload["consumed_ritual_material"] = consumed.name
            self.world_state.add_memory(
                f"仪式启动：{plan.caster} 准备【{plan.name}】，{outcome.total} 对抗难度等级 "
                f"{plan.target_number}，命刻 {clock_change.after}/{clock_change.max_segments}。"
            )
            return ActionResolution(
                action=action,
                rules_text=(
                    f"{plan.caster} 启动仪式【{plan.name}】：{outcome.total} 对抗难度等级 "
                    f"{plan.target_number}，成功。已创建命刻【{plan.clock_name}】{plan.clock_segments} 格，"
                    f"并推进到 {clock_change.after}/{clock_change.max_segments}。"
                ),
                payload=payload,
            )
        self.world_state.add_memory(
            f"仪式计划：{plan.caster} 准备【{plan.name}】，消耗 {plan.mp_cost} MP，难度等级 {plan.target_number}。"
        )
        rules_text = (
            f"{plan.caster} 计划仪式【{plan.name}】：{self._ritual_potency_text(plan.potency)}效力、"
            f"{self._ritual_scope_text(plan.scope)}范围，需要 {plan.mp_cost} MP，难度等级 {plan.target_number}。"
        )
        payload = {"ritual_plan": plan}
        return ActionResolution(action=action, rules_text=rules_text, payload=payload)

    def _sanitize_freeform_effect(self, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        text = re.sub(r"^\s*[^:：\n]{1,16}\s*[:：]\s*", "", text).strip()
        for pattern in [
            r"(?:效果|作用|目的)\s*(?:是|为|：|:)\s*(?P<effect>[^。\n！？]+[。！？]?)",
            r"(?:希望|想要|打算)\s*(?P<effect>让[^。\n！？]+[。！？]?)",
        ]:
            match = re.search(pattern, text)
            if match:
                text = match.group("effect")
                break
        else:
            if "：" in text:
                text = text.rsplit("：", 1)[1]
            elif ":" in text:
                text = text.rsplit(":", 1)[1]
        text = re.sub(r"^\s*[^:：\n]{1,16}\s*[:：]\s*", "", text).strip()
        if "\n" in text:
            text = text.split("\n", 1)[0]
        text = re.sub(r"\s+", " ", text)
        return text.strip(" ：:，,；;「」『』【】[]")

    def _resolve_contribute_ritual(self, action: Action) -> ActionResolution:
        manager = self._require_ritual_manager()
        clock_name = self._ritual_clock_name(action.parameters.get("clock_name") or action.parameters.get("name", ""))
        actor = action.parameters.get("actor") or action.parameters.get("caster")
        explicit_target_number = "target_number" in action.parameters
        outcome, change = manager.contribute_to_ritual(
            clock_name,
            actor=actor,
            attributes=action.parameters.get("attributes"),
            target_number=self._target_number_parameter(action.parameters, default=10)
            if explicit_target_number
            else None,
            modifier=self._int_parameter(action.parameters, "modifier", 0),
            direction=action.parameters.get("direction", 1),
            spend_critical_opportunity=bool(action.parameters.get("spend_critical_opportunity_on_clock", False)),
            reason=action.parameters.get("reasoning", "推进仪式命刻"),
            turn_serial=int(self.conflict_manager.state.turn_serial or 0),
        )
        self.world_state.add_memory(
            f"{actor} 推进仪式【{clock_name}】：{outcome.total} 对抗难度等级 {outcome.target_number}，命刻 {change.after}/{change.max_segments}。"
        )
        return ActionResolution(
            action=action,
            rules_text=(
                f"{actor} 尝试推进仪式【{clock_name}】：{outcome.total} 对抗难度等级 {outcome.target_number}，"
                f"命刻 {change.before}/{change.max_segments} -> {change.after}/{change.max_segments}。"
            ),
            payload={"roll": outcome, "clock_change": change},
        )

    def _resolve_cast_ritual(self, action: Action) -> ActionResolution:
        manager = self._require_ritual_manager()
        conflict_active = bool(self.conflict_manager.state.active)
        rare_material_item_id = str(
            action.parameters.get("_rare_material_item_id") or ""
        ).strip()
        if (
            action.parameters.get("rare_material")
            and action.parameters.get("_strict_tool_transaction")
            and not rare_material_item_id
        ):
            raise ValueError("仪式半价素材必须来自施法者实际持有的剧情物件。")
        if action.parameters.get("clock_name"):
            plan_or_clock_name = self._ritual_clock_name(action.parameters["clock_name"])
        elif action.parameters.get("name") and self._ritual_clock_name(action.parameters["name"]) in manager.active_rituals:
            plan_or_clock_name = self._ritual_clock_name(action.parameters["name"])
        else:
            if conflict_active:
                raise ValueError("冲突场景中的仪式必须先成功启动并填满仪式命刻。")
            caster = action.parameters.get("caster") or action.parameters.get("actor")
            plan_or_clock_name = manager.plan_ritual(
                caster=caster,
                name=self._ritual_name(action.parameters["name"]),
                discipline=self._ritual_discipline(action.parameters.get("discipline", "ritualism")),
                potency=self._ritual_potency(action.parameters.get("potency", "minor")),
                scope=self._ritual_scope(action.parameters.get("scope", "individual")),
                effect=self._sanitize_freeform_effect(action.parameters.get("effect", "")),
                attributes=action.parameters.get("attributes"),
                rare_material=action.parameters.get("rare_material", ""),
                forbidden_tags=action.parameters.get("forbidden_tags", []),
                enforce_permission=action.parameters.get("enforce_permission", True),
            )
        tracked_plan = (
            manager._resolve_plan(plan_or_clock_name)
            if isinstance(plan_or_clock_name, str)
            else plan_or_clock_name
        )
        require_completed_clock = bool(
            conflict_active
            or action.parameters.get("require_completed_clock", False)
            or tracked_plan.clock_name in manager.active_rituals
        )
        if require_completed_clock:
            plan = tracked_plan
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
                current_turn_serial = int(self.conflict_manager.state.turn_serial or 0)
                if (
                    conflict_active
                    and plan.ready_turn_serial > 0
                    and current_turn_serial <= plan.ready_turn_serial
                ):
                    return ActionResolution(
                        action=action,
                        rules_text=(
                            f"仪式【{plan.name}】的准备刚刚完成；"
                            f"【{plan.caster}】要到自己的下个回合才能进行最终施法检定。"
                        ),
                        payload={
                            "ritual_plan": plan,
                            "clock": clock,
                            "ritual_waiting_for_next_turn": True,
                        },
                    )
            else:
                raise ValueError(f"仪式命刻【{plan.clock_name}】不存在，不能完成仪式。")
        result = manager.cast_ritual(
            plan_or_clock_name,
            catastrophe=action.parameters.get(
                "catastrophe",
                "仪式失控，GM 应让效果以危险、代价或威胁命刻的方式扭曲。",
            ),
            require_completed_clock=require_completed_clock,
        )
        consumed_ritual_material = ""
        if rare_material_item_id:
            consumed = self._consume_story_material(
                item_id=rare_material_item_id,
                actor=result.plan.caster,
                purpose=f"完成仪式【{result.plan.name}】",
            )
            consumed_ritual_material = consumed.name
            result.plan.notes.append(f"已消耗稀有施法材料【{consumed.name}】。")
        persistence = None
        if result.success:
            persistence = self._persist_ritual_result(action, result)
        if result.plan.clock_name in manager.active_rituals:
            manager.finish_ritual(
                result.plan,
                note=(
                    f"仪式【{result.plan.name}】最终施法"
                    f"{'成功' if result.success else '失败'}。"
                ),
            )
        self.world_state.add_memory(result.summary)
        payload = {
            "ritual_result": result,
            "ritual_plan": result.plan,
            "roll": result.roll,
            "resource_change": result.mp_change,
        }
        if persistence is not None:
            payload["persistence"] = persistence
        if consumed_ritual_material:
            payload["consumed_ritual_material"] = consumed_ritual_material
        return ActionResolution(
            action=action,
            rules_text=result.summary,
            payload=payload,
        )

    def _resolve_start_project(self, action: Action) -> ActionResolution:
        manager = self._require_project_manager()
        inventor = action.parameters.get("inventor") or action.parameters.get("actor")
        required_material_ids = [
            str(item or "").strip()
            for item in action.parameters.get(
                "_project_required_material_item_ids", []
            )
            if str(item or "").strip()
        ]
        cost_material_ids = [
            str(item or "").strip()
            for item in action.parameters.get(
                "_project_cost_material_item_ids", []
            )
            if str(item or "").strip()
        ]
        if action.parameters.get("_strict_tool_transaction"):
            potency = self._ritual_potency(
                action.parameters.get("potency", "minor")
            )
            if potency != RitualPotency.MINOR and not required_material_ids:
                raise ValueError("中等或更高能效的工程必须消耗已取得的特殊原料。")
            if int(action.parameters.get("material_credit", 0) or 0) > 0 and not cost_material_ids:
                raise ValueError("工程素材抵扣必须对应实际持有并消耗的材料。")
        project = manager.start_project(
            inventor=inventor,
            name=action.parameters["name"],
            potency=self._ritual_potency(action.parameters.get("potency", "minor")),
            scope=self._ritual_scope(action.parameters.get("scope", "individual")),
            use=self._project_use(action.parameters.get("use", "consumable")),
            effect=self._sanitize_freeform_effect(action.parameters.get("effect", "")),
            output_type=self._persistent_change_type(
                action.parameters.get("output_type"),
                fallback=None,
            ),
            owner=action.parameters.get("owner", ""),
            location=action.parameters.get("location", ""),
            flaw=action.parameters.get("flaw", ""),
            special_materials=action.parameters.get("special_materials", []),
            cost_materials=action.parameters.get("cost_materials", []),
            material_credit=action.parameters.get("material_credit", 0),
            enforce_permission=action.parameters.get("enforce_permission", True),
        )
        consumed_required = [
            self._consume_story_material(
                item_id=item_id,
                actor=str(inventor or ""),
                purpose=f"作为工程【{project.name}】的特殊原料",
            ).name
            for item_id in required_material_ids
        ]
        consumed_cost = [
            self._consume_story_material(
                item_id=item_id,
                actor=str(inventor or ""),
                purpose=f"抵扣工程【{project.name}】的材料消耗",
            ).name
            for item_id in cost_material_ids
        ]
        self.world_state.add_memory(
            f"项目启动：{inventor} 开始制作【{project.name}】，成本 {project.material_cost}Z，进度 {project.required_progress}。"
        )
        material_text = ""
        if consumed_required:
            material_text += " 已消耗特殊原料：" + "、".join(consumed_required) + "。"
        if consumed_cost:
            material_text += " 已用珍贵材料抵扣消耗：" + "、".join(consumed_cost) + "。"
        return ActionResolution(
            action=action,
            rules_text=(
                f"{inventor} 启动项目【{project.name}】：总成本 {project.material_cost}Z，"
                f"需要进度 {project.required_progress}，当前 {project.current_progress}/{project.required_progress}。"
                f"{material_text}"
            ),
            payload={
                "project": project,
                "consumed_required_materials": consumed_required,
                "consumed_cost_materials": consumed_cost,
            },
        )

    def _consume_story_material(
        self,
        *,
        item_id: str,
        actor: str,
        purpose: str,
    ):
        item = self.world_state.find_story_item(item_id=item_id)
        if item is None:
            raise ValueError("准备消耗的素材剧情物件已经不存在。")
        scene = self.scene_manager.current_scene if self.scene_manager else None
        location = str(
            getattr(scene, "location", "")
            or getattr(scene, "name", "")
            or item.location
            or "当前场景"
        ).strip()
        public_fact = f"【{actor}】将【{item.name}】用于{purpose}，素材已被消耗。"
        return self.world_state.commit_story_item_action(
            operation="consume",
            item_name=item.name,
            item_id=item.item_id,
            actor=actor,
            scene_location=location,
            public_fact=public_fact,
            source="规则结算:素材消耗",
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
        completed_now = bool(
            result.completed
            and result.before < result.project.required_progress
            and result.after >= result.project.required_progress
        )
        persistence = (
            self._persist_project_result(result.project)
            if completed_now
            else None
        )
        payload = {
            "project_progress": result,
            "project": result.project,
            "project_completed": completed_now,
        }
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

        if gadget_type in {"alchemy", "炼金术", "炼金装置", "调合"} or mode in {"alchemy", "炼金术", "炼金装置", "调合"}:
            try:
                result = self.gadget_manager.use_alchemy(
                    actor,
                    tier=action.parameters.get("tier", "basic"),
                    target_roll=action.parameters.get("target_roll"),
                    effect_roll=action.parameters.get("effect_roll"),
                    targets=action.parameters.get("targets"),
                )
            except ValueError as exc:
                return self._tinkerer_gadget_failure(action, str(exc))
            self.world_state.add_memory(result.summary)
            actor_character = self.character_manager.get(str(actor))
            healing_changes = [
                change
                for change in result.resource_changes
                if change.resource in {"hp", "mp"} and int(change.amount) > 0
            ]
            if len(result.targets) == 1 and healing_changes:
                primary_target = result.targets[0]
                lifecycle = self.skill_lifecycle.trigger(
                    "after_craft_healing_potion",
                    actor_character,
                    single_target_healing=True,
                    primary_target=primary_target,
                    healing_changes=[
                        {
                            "resource": change.resource,
                            "base_amount": int(change.amount),
                            "before": int(change.before),
                        }
                        for change in healing_changes
                    ],
                    available_targets=[
                        character.name
                        for character in self.character_manager.all()
                        if character.name != primary_target
                    ],
                )
                self._capture_skill_lifecycle(lifecycle)
            return ActionResolution(action=action, rules_text=result.summary, payload={"gadget_result": result})

        if gadget_type in {"infusion", "注魔装置", "灌注术", "灌注"} or mode in self.gadget_manager.INFUSIONS:
            infusion_name = action.parameters.get("infusion_name") or action.parameters.get("mode") or "焦火"
            try:
                result = self.gadget_manager.prepare_infusion(actor, infusion_name)
            except ValueError as exc:
                return self._tinkerer_gadget_failure(action, str(exc))
            self.world_state.add_memory(result.summary)
            return ActionResolution(action=action, rules_text=result.summary, payload={"gadget_result": result})

        if gadget_type in {"magitech", "魔导装置", "魔科技", "magictech"} or any(token in mode for token in ["魔法加农炮", "魔加农", "魔导覆写", "覆写", "篡夺", "法球", "天球"]):
            if any(token in mode for token in ["魔导覆写", "覆写", "篡夺", "override"]):
                target_name = self._target_name(action, "当前构装体")
                if not self.character_manager.exists(target_name):
                    try:
                        self.gadget_manager.require_portable_device(actor, "魔导装置", 1)
                    except ValueError as exc:
                        return self._tinkerer_gadget_failure(action, str(exc))
                    self.world_state.add_memory(f"{actor} 尝试以魔科技篡夺影响场景装置【{target_name}】。")
                    self.world_state.remember_subject_fact(target_name, f"被 {actor} 尝试魔科技篡夺。")
                    return ActionResolution(
                        action=action,
                        rules_text=f"{actor} 尝试对【{target_name}】进行魔科技篡夺；该目标不是已建档构装体，已作为场景装置交互记录。",
                        payload={"gadget_result": None, "scene_object": target_name, "scene_gadget": True},
                    )
                try:
                    result = self.gadget_manager.magitech_override(
                        actor,
                        target_name,
                        action.parameters.get("forced_action") or action.parameters.get("command") or "指定行动",
                    )
                except ValueError as exc:
                    return self._tinkerer_gadget_failure(action, str(exc))
                self.world_state.add_memory(result.summary)
                return ActionResolution(action=action, rules_text=result.summary, payload={"gadget_result": result})
            if any(token in mode for token in ["法球", "天球", "magisphere"]):
                return self._resolve_magisphere(action, actor)
            try:
                result = self.gadget_manager.create_magicannon(actor, action.parameters.get("damage_type", "physical"))
            except ValueError as exc:
                return self._tinkerer_gadget_failure(action, str(exc))
            self.world_state.add_memory(result.summary)
            return ActionResolution(action=action, rules_text=result.summary, payload={"gadget_result": result})

        return self._tinkerer_gadget_failure(
            action,
            "请说明实际发动的炼金、注魔或魔导装置规则功能。",
        )

    def _resolve_magisphere(self, action: Action, actor: str) -> ActionResolution:
        try:
            self.gadget_manager.require_portable_device(actor, "魔导装置", 3)
        except ValueError as exc:
            return self._tinkerer_gadget_failure(action, str(exc))
        ip_change = self.gadget_manager.spend_ip(actor, 2, "使用法球。")
        secret_formula_rank = skill_rank(
            self.character_manager.get(actor).skills,
            "秘密配方",
        )
        spell_action = Action(
            action_type=ActionType.SPELL,
            parameters={
                **action.parameters,
                "actor": actor,
                "spell_name": action.parameters.get("spell_name") or action.parameters.get("spell") or "落雷",
                "target": action.parameters.get("target", actor),
                "_gadget_damage_bonus": secret_formula_rank,
                "_gadget_healing_bonus": secret_formula_rank * 5,
            },
        )
        nested = self._resolve_spell(spell_action)
        result = TinkererGadgetResult(
            actor=actor,
            gadget_type="魔导装置",
            mode="法球",
            ip_change=ip_change,
            nested_resolution=nested,
            summary=f"{actor} 消耗 2 IP 使用法球，并立即释放【{spell_action.parameters['spell_name']}】。",
        )
        self.world_state.add_memory(result.summary)
        return ActionResolution(
            action=action,
            rules_text=f"{result.summary} {nested.rules_text}",
            payload={"gadget_result": result, "nested_resolution": nested},
        )

    @staticmethod
    def _tinkerer_gadget_failure(action: Action, message: str) -> ActionResolution:
        if action.parameters.get("_strict_tool_transaction"):
            raise ValueError(str(message or "这项装置目前无法发动。"))
        return ActionResolution(
            action=action,
            rules_text=str(message or "这项装置目前无法发动。"),
            payload={"gadget_failed": True, "needs_clarification": True},
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
        chest_name = action.parameters.get("chest_name") or action.parameters.get("name") or "宝箱"
        scene = (
            self.scene_manager.current_scene
            if self.scene_manager is not None
            else None
        )
        chest_key = str(action.parameters.get("_chest_key") or "").strip() or "|".join(
            [
                str(getattr(scene, "location", "") or getattr(scene, "name", "") or "").strip(),
                str(chest_name).strip(),
            ]
        )
        if any(
            event.kind == "chest_open_commit"
            and str(event.payload.get("chest_key") or "") == chest_key
            for event in self.world_state.memory_events
        ):
            raise ValueError(f"【{chest_name}】已经开启并结算过奖励。")
        reward = self.economy_manager.open_chest(
            opener,
            chest_name,
            rarity=action.parameters.get("rarity", "standard"),
            fixed_item=action.parameters.get("fixed_item", ""),
            fixed_zenit=action.parameters.get("fixed_zenit"),
        )
        self.world_state.record_memory_event(
            f"宝箱结算回执：【{chest_name}】已由{opener}开启。",
            kind="chest_open_commit",
            visibility=MemoryVisibility.PRIVATE,
            entities=[str(opener), str(chest_name)],
            tags=["rules", "chest", "idempotency"],
            source="ActionInterceptor",
            payload={
                "chest_key": chest_key,
                "scene_id": str(getattr(scene, "scene_id", "") or ""),
                "opener": str(opener),
            },
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
        receipt_id = str(action.parameters.get("_check_receipt_id") or "").strip()
        receipt_event = None
        if receipt_id:
            receipt_event = next(
                (
                    event
                    for event in reversed(self.world_state.memory_events)
                    if event.event_id == receipt_id
                    and event.kind == "resolved_check"
                ),
                None,
            )
            if receipt_event is None:
                raise ValueError("地下城检定回执不存在或已失效。")
            if list(receipt_event.payload.get("consumed_by") or []):
                raise ValueError("这份地下城检定回执已经被消费。")
            if bool(receipt_event.payload.get("success")) != bool(
                action.parameters.get("success")
            ):
                raise ValueError("地下城success与最终检定回执不一致。")
        elif (
            action.parameters.get("_strict_tool_transaction")
            and action.parameters.get("success") is not None
        ):
            raise ValueError("地下城success必须引用最终检定回执。")
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
                if (
                    not fixed_item
                    and self.economy_manager.is_registered_reward_item(result.treasure)
                ):
                    fixed_item = result.treasure
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
        if receipt_event is not None:
            consumed_by = list(receipt_event.payload.get("consumed_by") or [])
            consumed_by.append(
                {
                    "dungeon": result.dungeon_name,
                    "area": result.area_name,
                    "action": result.action,
                }
            )
            receipt_event.payload["consumed_by"] = consumed_by
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
        self.post_check_state.remember_roll(outcome)

    def _remember_pending_clock_check(
        self,
        action: Action,
        outcome,
        payload: dict[str, object],
    ) -> None:
        self.post_check_state.remember_clock_check(action, outcome, payload)

    def _reconcile_pending_clock_check(self, actor: Character, outcome) -> dict[str, object]:
        return self.post_check_state.reconcile_clock_check(actor, outcome)

    def _consume_advantage_bonus(self, actor_name: str) -> int:
        return self.post_check_state.consume_advantage(actor_name)

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
            invocation_rationale = self._validate_pc_trait_invocation(
                actor,
                str(trait_name),
                action.parameters.get("invocation_rationale"),
            )
            reroll_indices = action.parameters.get("reroll_indices", action.parameters.get("reroll_dice"))
            if reroll_indices is None:
                values = [rolled for _, rolled in invoked_roll.dice]
                lowest = min(range(len(values)), key=lambda index: values[index]) if values else 0
                reroll_indices = [lowest]
            resource_change = self._spend_invocation_resource(actor.name, str(trait_name), is_trait=True)
            before_roll = invoked_roll
            invoked_roll = self.rules_engine.reroll_outcome(
                invoked_roll,
                reroll_indices,
                index_base=action.parameters.get("reroll_index_base"),
            )
            payload["trait_invocation"] = {
                "trait_name": str(trait_name),
                "invocation_rationale": invocation_rationale,
                "before_roll": before_roll,
                "after_roll": invoked_roll,
                "resource_change": resource_change,
            }
            notes.append(
                f"{actor.name} 援用特质【{trait_name}】：{invocation_rationale}。"
                f"重掷后结算值变为 {invoked_roll.total}。"
            )

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

    @staticmethod
    def _validate_pc_trait_invocation(actor, trait_name: str, rationale: object) -> str:
        """Validate the hard boundary after the GM has judged relevance."""

        if "pc" not in actor.traits:
            return str(rationale or "").strip()
        clean_trait = str(trait_name or "").strip()
        legal_traits = {
            str(value or "").strip()
            for value in (actor.identity, actor.theme, actor.origin)
            if str(value or "").strip()
        }
        if clean_trait not in legal_traits:
            raise ValueError(
                f"【{clean_trait or '未命名特质'}】不是{actor.name}当前可援用的身份、主题或故乡。"
            )
        clean_rationale = str(rationale or "").strip()
        if not clean_rationale:
            raise ValueError("援用特质前，玩家必须说明它与本次检定有何关联。")
        return clean_rationale

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
        self._validate_acceleration_followup(action)
        infusion_result = None
        if action.parameters.get("infusion") or action.parameters.get("infusion_name"):
            try:
                action, infusion_result = self._with_attack_infusion(action)
            except ValueError as exc:
                gadget_action = Action(
                    action_type=ActionType.TINKERER_GADGET,
                    parameters=dict(action.parameters),
                )
                return self._tinkerer_gadget_failure(gadget_action, str(exc))
        if self._uses_attack_window(action):
            resolution = self._resolve_attack_window(action)
            return self._attach_gadget_result(resolution, infusion_result)
        actor = self.character_manager.get(action.parameters["actor"])
        actual_target, cover_text = self._resolve_attack_target(action)
        damage_type = self._attack_damage_type(actor, action)
        defense_type = self._attack_defense_type(actor, action)
        conditional_damage_bonus = self._npc_conditional_attack_damage_bonus(
            action,
            actual_target,
        )
        gale_check_bonus, gale_damage_bonus = self._gale_combo_bonuses(
            actor,
            action,
            is_melee=action.parameters.get("is_melee", actor.weapon_range != "ranged"),
            target_count=1,
        )
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
                + actor.equipment_accuracy_bonus
                + gale_check_bonus,
                "target": actual_target.name,
                "target_number": self._target_number_or_defense(action.parameters, actual_target.name, defense_type),
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
                + actor.equipment_attack_damage_bonus
                + gale_damage_bonus
                + conditional_damage_bonus
                + self._consume_outgoing_ranged_damage_bonus(
                    actor.name,
                    is_melee=action.parameters.get("is_melee", actor.weapon_range != "ranged"),
                ),
                "critical_on_any_pair": self._rage_attack_is_active(actor),
                "_weapon_attack": True,
                "_npc_conditional_damage_bonus": conditional_damage_bonus,
            },
        )
        resolution = self._resolve_roll(attack_action)
        resolution.action = action
        self._apply_npc_attack_after_attack_effects(
            actor,
            action=action,
            payload=resolution.payload,
        )
        if action.parameters.get("_random_damage_type_roll"):
            resolution.payload["random_damage_type_roll"] = action.parameters[
                "_random_damage_type_roll"
            ]
            resolution.payload["random_damage_type"] = damage_type
        cleared_statuses = list(
            action.parameters.get("npc_pre_action_statuses_cleared") or []
        )
        if cleared_statuses:
            resolution.payload["npc_pre_action_statuses_cleared"] = (
                cleared_statuses
            )
            resolution.rules_text = (
                f"{actor.name}先解除"
                f"{'、'.join(str(item) for item in cleared_statuses)}。 "
                f"{resolution.rules_text}"
            )
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
                self._validate_flying_melee_target(actor, target, action)
                guardian = self.character_manager.guardian_for(target.name)
                if guardian is not None:
                    cover_texts.append(f"{guardian.name} 挡在 {target.name} 身前，替同伴承受了这次近战攻击。")
                    target = guardian
            interposer = self.conflict_manager.npc_interposer_for(
                target.name,
                source_actor=actor.name,
            )
            if interposer is not None:
                cover_texts.append(
                    f"{interposer.name}挺身代替{target.name}承受这次攻击。"
                )
                target = interposer
            actual_targets.append(target)
        if not actual_targets:
            raise ValueError("攻击至少需要一个目标。")

        teamwork_bonus, teamwork_payload = self._declared_teamwork_bonus(action, actor)
        advantage_bonus = self._consume_advantage_bonus(actor.name)
        modifier = self._int_parameter(action.parameters, "modifier", 0) + actor.weapon_accuracy_modifier + self._weapon_mastery_bonus(
            actor, is_melee
        )
        next_check_bonus = self._consume_next_check_bonus(actor.name)
        modifier += actor.equipment_accuracy_bonus + teamwork_bonus + advantage_bonus + next_check_bonus
        gale_check_bonus, gale_damage_bonus = self._gale_combo_bonuses(
            actor,
            action,
            is_melee=is_melee,
            target_count=len(actual_targets),
        )
        modifier += gale_check_bonus
        defense_type = self._attack_defense_type(actor, action)
        first_target_number = self._target_number_or_defense(action.parameters, actual_targets[0].name, defense_type)
        shared_roll = self.rules_engine.roll_check(
            actor=actor,
            attributes=action.parameters.get("attributes", actor.weapon_accuracy_attributes),
            target_number=first_target_number,
            modifier=modifier + self._active_hit_check_bonus(actor.name),
            target="、".join(target.name for target in actual_targets),
            reason=action.parameters.get("reasoning", ""),
            critical_on_any_pair=self._rage_attack_is_active(actor),
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
        if next_check_bonus:
            payload["next_check_bonus"] = next_check_bonus
        if invocation_payload:
            payload.update(invocation_payload)
        rules_parts = []
        if advantage_bonus:
            rules_parts.append(f"机会【优势】提供 +{advantage_bonus} 修正")
        if next_check_bonus:
            rules_parts.append(f"法术或技能支援提供 +{next_check_bonus} 修正")
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
        if action.parameters.get("_random_damage_type_roll"):
            payload["random_damage_type_roll"] = action.parameters[
                "_random_damage_type_roll"
            ]
            payload["random_damage_type"] = damage_type
        ignore_resist = action.parameters.get("ignore_resist", False) or self._attack_ignores_resist(actor, damage_type)
        ignore_all_affinities = action.parameters.get("ignore_all_affinities", False) or actor.equipment_ignore_all_affinities
        weapon_damage = action.parameters.get("weapon_damage", actor.weapon_damage) + self._hero_damage_bonus(
            actor,
            is_spell=False,
            is_melee=is_melee,
        ) + actor.equipment_attack_damage_bonus + gale_damage_bonus + self._consume_outgoing_ranged_damage_bonus(
            actor.name,
            is_melee=is_melee,
        )
        dirty_bonus = 0
        if len(actual_targets) == 1:
            dirty_bonus, dirty_effects = self._single_target_hit_bonus(actor, actual_targets[0])
            if dirty_effects:
                payload["single_target_skill_effects"] = dirty_effects
        for target in actual_targets:
            target_number = self._target_number_or_defense(action.parameters, target.name, defense_type)
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
            if success and action.parameters.get("non_damage", False):
                statuses_before_hit = set(target.statuses)
                self._apply_on_hit_status(action, target.name, payload)
                self._apply_npc_attack_hit_effects(
                    actor,
                    target,
                    actual_hp_loss=0,
                    statuses_before_hit=statuses_before_hit,
                    action=action,
                    payload=payload,
                )
                payload.setdefault("target_statuses", {})[target.name] = (
                    self.character_manager.format_status(
                        self.character_manager.get(target.name)
                    )
                )
                outcomes.append(outcome)
                rules_parts.append(
                    f"{target.name}: {outcome.total} 对抗 {target_number}，命中"
                )
                continue
            if success:
                statuses_before_hit = set(target.statuses)
                next_damage_bonus = self._consume_next_damage_bonus(target.name)
                incoming_damage_bonus = self._incoming_damage_bonus(
                    target.name,
                    damage_type,
                )
                damage_high_roll = (
                    self._int_parameter(
                        action.parameters,
                        "_damage_high_roll_override",
                        0,
                    )
                    if "_damage_high_roll_override" in action.parameters
                    else outcome.high_roll
                )
                damage, affinity = self.rules_engine.compute_damage(
                    high_roll=damage_high_roll,
                    weapon_damage=(
                        weapon_damage
                        + dirty_bonus
                        + next_damage_bonus
                        + incoming_damage_bonus
                        + self._npc_conditional_attack_damage_bonus(
                            action,
                            target,
                        )
                    ),
                    damage_type=damage_type,
                    target=target,
                    ignore_resist=ignore_resist,
                    ignore_all_affinities=ignore_all_affinities,
                )
                if damage >= 0:
                    before, after = self._apply_damage_from(actor.name, target.name, damage)
                else:
                    before, after = self.character_manager.modify_resource(target.name, "hp", -damage)
                outcome.damage = abs(damage)
                outcome.high_roll = damage_high_roll
                outcome.applied_affinity = affinity
                outcome.hp_after = after
                self._after_actor_deals_damage(actor, target, before, after)
                self._apply_combat_trait_after_damage(
                    target.name,
                    affinity,
                    abs(damage),
                    payload,
                    hp_before=before,
                    action=action,
                    source_actor=actor.name,
                    is_spell=False,
                )
                if next_damage_bonus:
                    payload.setdefault("next_damage_bonuses", {})[target.name] = next_damage_bonus
                if incoming_damage_bonus:
                    payload.setdefault("incoming_damage_bonuses", {})[target.name] = incoming_damage_bonus
                payload.setdefault("target_statuses", {})[target.name] = self.character_manager.format_status(
                    self.character_manager.get(target.name)
                )
                self._remember_damage_outcome(actor.name, target.name, outcome)
                self._apply_on_hit_status(action, target.name, payload)
                self._apply_npc_attack_hit_effects(
                    actor,
                    target,
                    actual_hp_loss=max(0, before - after),
                    statuses_before_hit=statuses_before_hit,
                    action=action,
                    payload=payload,
                )
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
                    self._apply_combat_trait_before_zero_hp(
                        target.name,
                        payload,
                        action=action,
                        source_actor=actor.name,
                        damage_type=damage_type,
                    )
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
                            pc_consequence=str(action.parameters.get("pc_consequence") or ""),
                            source_actor=self._zero_hp_source_actor(
                                action,
                                actor.name,
                            ),
                            villain_mode=action.parameters.get("villain_zero_hp_mode", "auto"),
                            allow_escalation=action.parameters.get("allow_escalation", True),
                            sacrifice_benefits_bond=action.parameters.get("sacrifice_benefits_bond"),
                            sacrifice_betters_world=action.parameters.get("sacrifice_betters_world"),
                            require_all_sacrifice_conditions=self._organized_chronicles_mode_enabled(),
                        )
                        if event.hp_after is not None:
                            outcome.hp_after = event.hp_after
                    else:
                        event = None
                    if event is not None:
                        conflict_events.append(event)
            if not outcome.success:
                missed_events = self.combat_trait_manager.after_attack_missed(
                    target,
                    triggering_actor=actor.name,
                )
                self._append_combat_trait_events(payload, missed_events)
                self._resolve_npc_ability_events(
                    action,
                    missed_events,
                    payload,
                )
            outcomes.append(outcome)
            rules_parts.append(
                f"{target.name}: {outcome.total} 对抗 {target_number}，"
                f"{'命中' if outcome.success else '未命中'}"
                + (f"，造成 {outcome.damage} 点{self._damage_type_text(outcome.damage_type)}伤害" if outcome.success else "")
            )

        self_damage = int(
            action.parameters.get("self_hp_loss_if_all_miss", 0) or 0
        )
        if self_damage > 0 and not any(outcome.success for outcome in outcomes):
            before_self, after_self = self.character_manager.modify_resource(
                actor.name,
                "hp",
                -self_damage,
            )
            self_change = ResourceChange(
                target=actor.name,
                resource="hp",
                amount=after_self - before_self,
                before=before_self,
                after=after_self,
                reason="基础攻击全部未命中的反噬。",
            )
            payload["npc_all_miss_self_damage"] = self_change
            rules_parts.append(
                f"{actor.name}因攻击全部未命中而失去{before_self - after_self}点生命值"
            )
            if after_self == 0:
                after_self, event, event_text = self._resolve_zero_hp_after_damage(
                    action,
                    source_actor=actor.name,
                    target_name=actor.name,
                    payload=payload,
                )
                if event is not None:
                    conflict_events.append(event)
                if event_text:
                    rules_parts.append(event_text)

        payload["rolls"] = outcomes
        payload["roll"] = outcomes[0]
        if conflict_events:
            payload["conflict_events"] = conflict_events
            payload["conflict_event"] = conflict_events[0]

        counter_followups = self._prepare_counter_reactions(
            action,
            shared_roll,
            actual_targets,
            is_melee,
        )
        if counter_followups:
            payload["counter_followups"] = counter_followups

        self._apply_npc_attack_after_attack_effects(
            actor,
            action=action,
            payload=payload,
        )

        rules_text = f"多目标攻击检定 {shared_roll.total}: " if len(actual_targets) > 1 else f"攻击检定 {shared_roll.total}: "
        if cover_texts:
            rules_text = " ".join(cover_texts) + " " + rules_text
            payload["cover_texts"] = cover_texts
        return ActionResolution(action=action, rules_text=rules_text + "；".join(rules_parts) + "。", payload=payload)

    def _resolve_equip(self, action: Action) -> ActionResolution:
        actor_name = action.parameters["actor"]
        raw_slots = action.parameters.get("slots")
        allow_armor = not self.conflict_manager.state.active
        if isinstance(raw_slots, dict):
            equipped_slots = self.economy_manager.configure_loadout(
                actor_name,
                {
                    str(slot): str(item or "")
                    for slot, item in raw_slots.items()
                },
                allow_armor=allow_armor,
            )
            actor = self.character_manager.get(actor_name)
            changed = "、".join(
                f"{slot}={item or '空'}" for slot, item in raw_slots.items()
            )
            return ActionResolution(
                action=action,
                rules_text=(
                    f"{actor_name} 执行装备行动，调整：{changed or '无变更'}。"
                    f" 当前主手【{actor.equipped_main_hand}】、副手【{actor.equipped_off_hand or '空'}】、"
                    f"盾牌【{actor.equipped_shield or '无'}】、防具【{actor.equipped_armor or '无防具'}】、"
                    f"饰品【{actor.equipped_accessory or '无'}】。"
                ),
                payload={
                    "equipped_slots": equipped_slots,
                    "actor_status": self.character_manager.format_status(actor),
                },
            )
        raw_items = action.parameters.get("items", action.parameters.get("item_names", action.parameters.get("item_name", [])))
        if isinstance(raw_items, str):
            item_names = [item.strip() for item in re.split(r"[、,，/]+", raw_items) if item.strip()]
        elif isinstance(raw_items, dict):
            item_names = [self._equipment_item_name_from_value(raw_items)]
        else:
            item_names = [self._equipment_item_name_from_value(item) for item in raw_items]
            item_names = [item for item in item_names if item]
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
        pending_initiative = self.check_batch_manager.pending(kind="initiative")
        if pending_initiative:
            batch = pending_initiative[0]
            resolution = ActionResolution(
                action=action,
                # This is an internal resumable state, not a player-facing
                # rules announcement. The runtime tool renders the actual
                # initiative roll and keeps the batch pending.
                rules_text="",
                payload={
                    "initiative_pending": True,
                    "check_batch_id": batch.batch_id,
                    "initiative_rolls": dict(batch.rolls),
                    "decision_windows": self.decision_window_manager.public_summary(),
                },
            )
            self._progress_check_batches(resolution)
            return resolution
        pcs = self._string_sequence(action.parameters.get("pcs")) or [
            character.name for character in self.character_manager.all() if "pc" in character.traits
        ]
        pcs = list(dict.fromkeys(name for name in pcs if name))
        if not pcs:
            raise ValueError("开始冲突前至少需要一名在场玩家角色。")
        missing_pcs = [
            name
            for name in pcs
            if not self.character_manager.exists(name)
        ]
        if missing_pcs:
            raise ValueError(
                f"以下玩家角色尚未建档，不能参加冲突：{'、'.join(missing_pcs)}。"
            )
        allied_npcs = list(
            dict.fromkeys(
                name
                for name in self._string_sequence(
                    action.parameters.get("allied_npcs")
                )
                if name
            )
        )
        missing_allies = [
            name
            for name in allied_npcs
            if not self.character_manager.exists(name)
        ]
        if missing_allies:
            raise ValueError(
                "以下盟友NPC尚未建档，不能参加冲突："
                + "、".join(missing_allies)
                + "。"
            )
        invalid_allies = [
            name
            for name in allied_npcs
            if (
                "ally" not in self.character_manager.get(name).traits
                or "pc" in self.character_manager.get(name).traits
                or {"enemy", "villain"}
                & set(self.character_manager.get(name).traits)
            )
        ]
        if invalid_allies:
            raise ValueError(
                "以下角色不是可执行完整回合的盟友NPC："
                + "、".join(invalid_allies)
                + "。"
            )
        enemies = self._string_sequence(action.parameters.get("enemies")) or [
            character.name for character in self.character_manager.all() if "enemy" in character.traits or "villain" in character.traits
        ]
        enemies = [enemy_name for enemy_name in enemies if enemy_name and not self.clock_manager.exists(enemy_name)]
        missing_enemies = [
            enemy_name
            for enemy_name in enemies
            if not self.character_manager.exists(enemy_name)
        ]
        if missing_enemies:
            raise ValueError(
                "以下敌人没有规则战斗档案，不能开始冲突："
                + "、".join(missing_enemies)
                + "。请先完成NPC战斗建档。"
            )
        for enemy_name in enemies:
            if self.character_manager.exists(enemy_name):
                enemy = self.character_manager.get(enemy_name)
                if enemy_name not in self.conflict_manager.state.enemy_ranks and ("enemy" in enemy.traits or "villain" in enemy.traits):
                    rank = EnemyRank.VILLAIN if "villain" in enemy.traits else EnemyRank.SOLDIER
                    self.conflict_manager.register_enemy(enemy_name, rank, ultima_points=self._int_parameter(action.parameters, "ultima_points", 0, minimum=0))
        leader_name = str(action.parameters.get("leader") or (pcs[0] if pcs else ""))
        if leader_name not in pcs:
            raise ValueError("团队先攻的领队必须是本场冲突中的玩家角色。")
        leader = self.character_manager.get(leader_name)
        support_names = list(
            dict.fromkeys(
                name
                for name in (
                    self._string_sequence(action.parameters.get("supporters"))
                    or pcs[1:]
                )
                if name != leader_name and name in pcs
            )
        )
        supporters = [self.character_manager.get(name) for name in support_names if self.character_manager.exists(name)]
        enemy_characters = [self.character_manager.get(name) for name in enemies if self.character_manager.exists(name)]
        target_number = self.rules_engine.initiative_target(enemy_characters)
        source_action = deepcopy(action)
        source_action.parameters.update(
            {
                "_initiative_scene_name": scene_name,
                "_initiative_pcs": list(pcs),
                "_initiative_allied_npcs": list(allied_npcs),
                "_initiative_player_side": [*pcs, *allied_npcs],
                "_initiative_enemies": list(enemies),
                "_initiative_leader": leader_name,
                "_initiative_supporters": list(support_names),
                "_initiative_target_number": target_number,
                "_initiative_attributes": ["DEX", "INS"],
            }
        )
        batch = self.check_batch_manager.begin(
            kind="initiative",
            source_action=source_action,
            actor_order=[leader_name, *support_names],
            roles={
                leader_name: "leader",
                **{name: "supporter" for name in support_names},
            },
        )
        resolution = ActionResolution(
            action=action,
            rules_text="开始团队先攻检定。",
            payload={
                "initiative_pending": True,
                "check_batch_id": batch.batch_id,
                "initiative_target_number": target_number,
                "initiative_leader": leader.name,
                "initiative_supporters": [supporter.name for supporter in supporters],
            },
        )
        self._progress_check_batches(resolution)
        if resolution.payload.get("check_batch_completions"):
            resolution.payload["initiative_pending"] = False
        else:
            resolution.payload["decision_windows"] = (
                self.decision_window_manager.public_summary()
            )
        return resolution

    def _initiative_batch_roll_action(self, batch, actor_name: str) -> Action:
        source = dict(batch.source_parameters)
        leader_name = str(source.get("_initiative_leader") or "").strip()
        attributes = list(source.get("_initiative_attributes") or ["DEX", "INS"])
        is_leader = actor_name == leader_name
        actor = self.character_manager.get(actor_name)
        return Action(
            ActionType.REQUEST_ROLL,
            {
                "actor": actor_name,
                "target": (
                    str(source.get("_initiative_scene_name") or "冲突先攻")
                    if is_leader
                    else leader_name
                ),
                "attributes": attributes,
                "target_number": (
                    int(source.get("_initiative_target_number", 10) or 10)
                    if is_leader
                    else 10
                ),
                "modifier": actor.initiative if is_leader else 0,
                "reason": "团队先攻领队检定" if is_leader else "团队先攻支援检定",
                "non_damage": True,
                "_check_batch_roll": True,
                "_check_batch_id": batch.batch_id,
                "_check_batch_kind": batch.kind,
                "_check_batch_role": batch.roles.get(actor_name, ""),
            },
        )

    def _pvp_batch_roll_action(self, batch, actor_name: str) -> Action:
        source = dict(batch.source_parameters)
        left_name = str(source.get("_pvp_left") or "").strip()
        is_left = actor_name == left_name
        other_name = str(
            source.get("_pvp_right") if is_left else source.get("_pvp_left")
        ).strip()
        return Action(
            ActionType.REQUEST_ROLL,
            {
                "actor": actor_name,
                "target": other_name,
                "attributes": list(
                    source.get("_pvp_attributes") or ["WLP", "WLP"]
                ),
                "target_number": 0,
                "modifier": int(
                    source.get(
                        "_pvp_left_modifier"
                        if is_left
                        else "_pvp_right_modifier",
                        0,
                    )
                    or 0
                )
                + (2 if self.character_manager.get(actor_name).guarding else 0),
                "reason": "玩家对抗检定",
                "non_damage": True,
                "_opposed_check_roll": True,
                "_check_batch_roll": True,
                "_check_batch_id": batch.batch_id,
                "_check_batch_kind": batch.kind,
                "_check_batch_role": batch.roles.get(actor_name, ""),
            },
        )

    def _check_batch_roll_action(self, batch, actor_name: str) -> Action:
        if batch.kind == "initiative":
            return self._initiative_batch_roll_action(batch, actor_name)
        if batch.kind == "pvp_opposed":
            return self._pvp_batch_roll_action(batch, actor_name)
        raise ValueError(f"尚未实现多人检定批次【{batch.kind}】的掷骰规则。")

    def _complete_initiative_batch(self, batch) -> dict[str, object]:
        source = dict(batch.source_parameters)
        leader_name = str(source.get("_initiative_leader") or "").strip()
        supporter_names = [
            str(name)
            for name in source.get("_initiative_supporters", [])
            if str(name).strip()
        ]
        leader = self.character_manager.get(leader_name)
        supporters = [
            self.character_manager.get(name)
            for name in supporter_names
            if self.character_manager.exists(name)
        ]
        initiative = self.rules_engine.resolve_team_check(
            leader=leader,
            supporters=supporters,
            attributes=list(source.get("_initiative_attributes") or ["DEX", "INS"]),
            target_number=int(source.get("_initiative_target_number", 10) or 10),
            leader_roll=batch.rolls[leader_name],
            support_rolls={
                name: batch.rolls[name]
                for name in supporter_names
            },
        )
        players_first = bool(initiative.success)
        pcs = [
            str(name)
            for name in source.get("_initiative_pcs", [])
            if str(name).strip()
        ]
        allied_npcs = [
            str(name)
            for name in source.get("_initiative_allied_npcs", [])
            if str(name).strip()
        ]
        player_side = [
            str(name)
            for name in (
                source.get("_initiative_player_side", [])
                or [*pcs, *allied_npcs]
            )
            if str(name).strip()
        ]
        enemies = [
            str(name)
            for name in source.get("_initiative_enemies", [])
            if str(name).strip()
        ]
        scene_name = str(
            source.get("_initiative_scene_name")
            or source.get("scene_name")
            or source.get("name")
            or "冲突场景"
        )
        turn_order = self.conflict_manager.start_scene_from_initiative(
            scene_name,
            player_side,
            enemies,
            players_first=players_first,
            parent_scene_id=str(source.get("_parent_scene_id") or ""),
            parent_scene_name=str(source.get("_parent_scene_name") or ""),
            parent_scene_type=str(source.get("_parent_scene_type") or ""),
            parent_scene_objective=str(
                source.get("_parent_scene_objective") or ""
            ),
            parent_scene_summary=str(source.get("_parent_scene_summary") or ""),
        )
        appearance_events = [
            self.conflict_manager.award_villain_appearance_fabula(enemy_name)
            for enemy_name in enemies
            if self.conflict_manager.is_villain(enemy_name)
        ]
        skill_decision_windows: list[dict[str, object]] = []
        for pc_name in pcs:
            if not self.character_manager.exists(pc_name):
                continue
            skill_outcome = self.skill_lifecycle.trigger(
                "conflict_start",
                self.character_manager.get(pc_name),
                visible_targets=list(enemies),
            )
            self._capture_skill_lifecycle(skill_outcome)
            skill_decision_windows.extend(skill_outcome.windows)
        result = {
            "initiative": initiative,
            "turn_order": turn_order,
            "players_first": players_first,
            "villain_appearance_events": appearance_events,
            "skill_decision_windows": skill_decision_windows,
            "scene_name": scene_name,
        }
        self.check_batch_manager.complete(batch, result=result)
        return result

    def _complete_pvp_batch_round(self, batch) -> dict[str, object]:
        source = dict(batch.source_parameters)
        left_name = str(source.get("_pvp_left") or "").strip()
        right_name = str(source.get("_pvp_right") or "").strip()
        opposed = self.rules_engine.resolve_opposed_check(
            left=self.character_manager.get(left_name),
            right=self.character_manager.get(right_name),
            attributes=list(source.get("_pvp_attributes") or ["WLP", "WLP"]),
            left_roll=batch.rolls[left_name],
            right_roll=batch.rolls[right_name],
            attempts=len(batch.roll_history) + 1,
        )
        if opposed is None:
            attempt = len(batch.roll_history) + 1
            self.check_batch_manager.reset_round(batch)
            return {
                "pvp_tied": True,
                "attempt": attempt,
                "left": left_name,
                "right": right_name,
            }
        result = {
            "opposed_check": opposed,
            "winner": opposed.winner,
            "pvp_tied": False,
        }
        self.check_batch_manager.complete(batch, result=result)
        return result

    def _progress_check_batches(self, resolution: ActionResolution) -> None:
        if self._advancing_check_batches:
            return
        self._advancing_check_batches = True
        try:
            child_rolls: list[dict[str, object]] = []
            completions: list[dict[str, object]] = []
            tied_rounds: list[dict[str, object]] = []
            for batch in list(self.check_batch_manager.pending()):
                if batch.kind not in {"initiative", "pvp_opposed"}:
                    continue
                while batch.status == "pending":
                    actor_name = self.check_batch_manager.next_actor(batch)
                    if actor_name:
                        child = self.resolve(
                            self._check_batch_roll_action(batch, actor_name)
                        )
                        child_rolls.append(
                            {
                                "actor": actor_name,
                                "roll": child.payload.get("roll"),
                                "provisional": bool(
                                    child.payload.get("check_result_provisional")
                                ),
                                "rules_text": child.rules_text,
                                "decision_windows": child.payload.get(
                                    "decision_windows",
                                    [],
                                ),
                            }
                        )
                        if self.check_batch_manager.has_blocking_window(batch):
                            break
                        continue
                    if self.check_batch_manager.ready(batch):
                        if batch.kind == "initiative":
                            completions.append(
                                self._complete_initiative_batch(batch)
                            )
                            break
                        pvp_result = self._complete_pvp_batch_round(batch)
                        if pvp_result.get("pvp_tied"):
                            tied_rounds.append(pvp_result)
                            if len(batch.roll_history) >= 20:
                                raise ValueError(
                                    "玩家对抗检定连续二十轮平手，请暂停并由玩家重新确认处理方式。"
                                )
                            continue
                        completions.append(pvp_result)
                        break
                    break
            if child_rolls:
                resolution.payload.setdefault("check_batch_rolls", []).extend(
                    child_rolls
                )
            if completions:
                resolution.payload.setdefault(
                    "check_batch_completions",
                    [],
                ).extend(completions)
                resolution.payload.update(completions[-1])
                completion_segments: list[str] = []
                for item in completions:
                    if item.get("initiative") is not None:
                        initiative = item["initiative"]
                        completion_segments.append(
                            f"先攻团队检定：{initiative.leader} "
                            f"{initiative.final_total} 对抗难度等级 "
                            f"{initiative.target_number}，"
                            f"{'玩家方先行动' if item['players_first'] else '敌方先行动'}。"
                            f"回合顺序：{' -> '.join(item['turn_order'])}。"
                        )
                    elif item.get("opposed_check") is not None:
                        opposed = item["opposed_check"]
                        completion_segments.append(
                            f"玩家对抗检定由【{opposed.winner}】胜出。"
                        )
                completion_text = " ".join(completion_segments)
                resolution.rules_text = (
                    f"{resolution.rules_text} {completion_text}"
                ).strip()
            if tied_rounds:
                resolution.payload.setdefault("check_batch_ties", []).extend(
                    tied_rounds
                )
                resolution.rules_text = (
                    f"{resolution.rules_text} 双方本轮对抗平手，重新进行对抗检定。"
                ).strip()
        finally:
            self._advancing_check_batches = False

    def _resolve_invoke_trait(self, action: Action) -> ActionResolution:
        actor_name = action.parameters["actor"]
        actor = self.character_manager.get(actor_name)
        outcome = self.post_check_state.roll_for(actor_name)
        if outcome is None:
            if action.parameters.get("skip_if_pending_roll_success"):
                return ActionResolution(
                    action=action,
                    rules_text=f"{actor_name} 当前没有待处理检定；按玩家声明，本次援用特质不触发，不消耗物语点。",
                    payload={"skipped_invocation": True, "actor_status": self.character_manager.format_status(actor)},
                )
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
        invocation_rationale = self._validate_pc_trait_invocation(
            actor,
            trait_name,
            action.parameters.get("invocation_rationale"),
        )
        transaction = self.pending_check_transactions.get(actor_name)
        if transaction is not None:
            rerolled = self.rules_engine.reroll_outcome(
                outcome,
                action.parameters.get("reroll_indices", action.parameters.get("reroll_dice")),
                index_base=action.parameters.get("reroll_index_base"),
            )
            return self._replay_check_transaction(
                invocation_action=action,
                transaction=transaction,
                adjusted_outcome=rerolled,
                invocation_kind="trait",
                invocation_name=trait_name,
            )
        resource = self._spend_invocation_resource(actor_name, trait_name, is_trait=True)
        rerolled = self.rules_engine.reroll_outcome(
            outcome,
            action.parameters.get("reroll_indices", action.parameters.get("reroll_dice")),
            index_base=action.parameters.get("reroll_index_base"),
        )
        self.post_check_state.replace_roll(actor_name, rerolled)
        reconciliation = self._reconcile_pending_clock_check(actor, rerolled)
        revised_text = self._reconciled_clock_rules_text(reconciliation)
        return ActionResolution(
            action=action,
            rules_text=(revised_text or "").strip(),
            payload={
                "before_roll": outcome,
                "roll": rerolled,
                "resource_change": resource,
                "actor_status": self.character_manager.format_status(actor),
                **reconciliation,
            },
        )

    def _resolve_invoke_bond(self, action: Action) -> ActionResolution:
        actor_name = action.parameters["actor"]
        actor = self.character_manager.get(actor_name)
        outcome = self.post_check_state.roll_for(actor_name)
        if outcome is None:
            raise ValueError(f"{actor_name} 没有可援用羁绊的待处理检定。")
        if outcome.fumble:
            raise ValueError("大失败自动失败，不能靠羁绊加值改写。")
        bond_target = str(action.parameters.get("bond_target") or action.parameters.get("target") or "")
        strength = actor.bond_strength_with(bond_target)
        if strength <= 0:
            raise ValueError(f"{actor_name} 对【{bond_target}】没有可援用的羁绊。")
        transaction = self.pending_check_transactions.get(actor_name)
        if transaction is not None:
            adjusted = self.rules_engine.apply_bond_bonus(outcome, strength)
            return self._replay_check_transaction(
                invocation_action=action,
                transaction=transaction,
                adjusted_outcome=adjusted,
                invocation_kind="bond",
                invocation_name=bond_target,
                bond_strength=strength,
            )
        before, after = self.character_manager.modify_resource(actor_name, "fabula_points", -1)
        if before <= after:
            raise ValueError(f"{actor_name} 没有足够物语点援用羁绊。")
        adjusted = self.rules_engine.apply_bond_bonus(outcome, strength)
        self.post_check_state.replace_roll(actor_name, adjusted)
        reconciliation = self._reconcile_pending_clock_check(actor, adjusted)
        revised_text = self._reconciled_clock_rules_text(reconciliation)
        change = ResourceChange(actor_name, "fabula_points", after - before, before, after, "援用羁绊。")
        return ActionResolution(
            action=action,
            rules_text=(
                f"{actor_name} 援用对【{bond_target}】的羁绊，检定 +{strength}，从 {outcome.total} 变为 {adjusted.total}。"
                f"{revised_text}"
            ),
            payload={
                "before_roll": outcome,
                "roll": adjusted,
                "resource_change": change,
                "bond_strength": strength,
                **reconciliation,
            },
        )

    def _replay_check_transaction(
        self,
        *,
        invocation_action: Action,
        transaction: dict[str, object],
        adjusted_outcome,
        invocation_kind: str,
        invocation_name: str,
        bond_strength: int = 0,
        resource_payer_name: str = "",
        assisted_actor_name: str = "",
        assisted_mp_recovery: int = 0,
    ) -> ActionResolution:
        invocation_actor_name = str(invocation_action.parameters["actor"])
        original_outcome = deepcopy(transaction["roll"])
        checked_actor_name = str(getattr(original_outcome, "actor", "") or "").strip()
        if not checked_actor_name:
            raise ValueError("这次检定事务缺少实际掷骰者。")
        resource_payer = str(resource_payer_name or invocation_actor_name).strip()
        assisted_actor = str(assisted_actor_name or checked_actor_name).strip()
        snapshot = transaction["snapshot"]
        original_action = deepcopy(transaction["action"])
        invocation_history = [
            dict(item)
            for item in transaction.get("invocation_history", [])
            if isinstance(item, dict)
        ]
        bond_invoked = bool(transaction.get("bond_invoked"))
        self.pending_check_transactions.pop(checked_actor_name, None)
        self._restore_check_state(snapshot)

        if invocation_kind in {"trait", "trusted_trait"}:
            resource_change = self._spend_invocation_resource(
                resource_payer,
                invocation_name,
                is_trait=True,
            )
            if invocation_kind == "trusted_trait":
                invocation_text = (
                    f"{resource_payer}发动【予以信任】，援用{checked_actor_name}的"
                    f"特质【{invocation_name}】重掷；检定从 {original_outcome.total} "
                    f"变为 {adjusted_outcome.total}。"
                )
            else:
                # The rationale is validation evidence already spoken by the
                # player. Once accepted, only the final roll belongs in chat.
                invocation_text = ""
        else:
            before, after = self.character_manager.modify_resource(
                resource_payer,
                "fabula_points",
                -1,
            )
            if before <= after:
                raise ValueError(f"{resource_payer} 没有足够物语点援用羁绊。")
            resource_change = ResourceChange(
                resource_payer,
                "fabula_points",
                after - before,
                before,
                after,
                "援用羁绊为检定提供加值。",
            )
            if invocation_kind == "trusted_bond":
                invocation_text = (
                    f"{resource_payer}发动【予以信任】，援用{checked_actor_name}对"
                    f"【{invocation_name}】的羁绊；检定 +{bond_strength}，"
                    f"从 {original_outcome.total} 变为 {adjusted_outcome.total}。"
                )
            else:
                invocation_text = ""

        assisted_mp_change = None
        if (
            assisted_mp_recovery > 0
            and assisted_actor
            and self.character_manager.exists(assisted_actor)
        ):
            before_mp, after_mp = self.character_manager.modify_resource(
                assisted_actor,
                "mp",
                assisted_mp_recovery,
            )
            assisted_mp_change = ResourceChange(
                assisted_actor,
                "mp",
                after_mp - before_mp,
                before_mp,
                after_mp,
                "【予以信任】恢复精神值。",
            )
            if after_mp > before_mp:
                invocation_text += (
                    f" {assisted_actor}恢复 {after_mp - before_mp} 点精神值。"
                )

        invocation_history.append(
            {
                "kind": invocation_kind,
                "name": invocation_name,
                "invoker": invocation_actor_name,
                "resource_payer": resource_payer,
                "before_total": int(original_outcome.total),
                "after_total": int(adjusted_outcome.total),
            }
        )
        bond_invoked = bond_invoked or invocation_kind in {"bond", "trusted_bond"}
        next_snapshot = self._snapshot_check_state()
        for key in ("invoke_trait", "trait_name", "invoke_trait_name", "invoke_bond_target", "bond_target"):
            original_action.parameters.pop(key, None)
        replay_sequence = [
            deepcopy(item)
            for item in transaction.get("roll_sequence", [])
            if hasattr(item, "actor")
        ]
        if not replay_sequence:
            replay_sequence = [deepcopy(original_outcome)]
        roll_index = max(0, int(transaction.get("roll_index", 0) or 0))
        if roll_index >= len(replay_sequence):
            roll_index = 0
        replay_sequence[roll_index] = deepcopy(adjusted_outcome)
        for forced_outcome in replay_sequence:
            self.rules_engine.force_next_check_outcome(forced_outcome)
        self._replaying_check_transaction = True
        self.check_transaction_manager.allow_restage = True
        self._check_transaction_candidate = {
            "action": deepcopy(original_action),
            "snapshot": next_snapshot,
            "invocation_history": invocation_history,
            "bond_invoked": bond_invoked,
        }
        try:
            replayed = self.resolve(original_action)
        except Exception:
            self._check_transaction_candidate = None
            self._restore_check_state(snapshot)
            raise
        finally:
            self._replaying_check_transaction = False
            self.check_transaction_manager.allow_restage = False
            self.rules_engine.clear_forced_check_outcomes()

        replayed.action = invocation_action
        replayed.rules_text = f"{invocation_text} {replayed.rules_text}"
        replayed_action_resource_change = replayed.payload.get("resource_change")
        replayed.payload.update(
            {
                "before_roll": original_outcome,
                "roll": replayed.payload.get("roll", adjusted_outcome),
                "resource_change": resource_change,
                "check_transaction_replayed": True,
                "check_transaction_invocation_kind": invocation_kind,
                "check_transaction_invocation_name": invocation_name,
                "check_transaction_invocation_history": invocation_history,
                "check_transaction_invocation_text": invocation_text,
                "committed_source_action": deepcopy(original_action),
                "actor_status": self.character_manager.format_status(
                    self.character_manager.get(checked_actor_name)
                ),
                "resource_payer_status": self.character_manager.format_status(
                    self.character_manager.get(resource_payer)
                ),
                "_already_finalized": True,
            }
        )
        if assisted_mp_change is not None:
            replayed.payload["assisted_mp_change"] = assisted_mp_change
        if replayed_action_resource_change is not None:
            replayed.payload["replayed_action_resource_change"] = replayed_action_resource_change
        if original_action.parameters.get("clock_name") or original_action.parameters.get("threat_clock_name"):
            replayed.payload["clock_reconciled"] = True
        if invocation_kind == "bond":
            replayed.payload["bond_strength"] = bond_strength
        if not replayed.payload.get("check_result_provisional"):
            windows = replayed.payload.get("post_check_windows") or []
            replayed.payload["post_check_windows"] = [
                window
                for window in windows
                if not isinstance(window, dict)
                or window.get("kind") not in {"trait_invocation", "bond_invocation"}
            ]
            self.post_check_state.discard_actor(checked_actor_name)
        return replayed

    def _commit_check_transaction_acceptance(
        self,
        *,
        acceptance_action: Action,
        transaction: dict[str, object],
    ) -> ActionResolution:
        """Commit an unchanged provisional check from its pre-check snapshot."""

        actor_name = str(acceptance_action.parameters.get("actor") or "").strip()
        original_outcome = deepcopy(transaction["roll"])
        original_action = deepcopy(transaction["action"])
        snapshot = transaction["snapshot"]
        self.pending_check_transactions.pop(actor_name, None)
        self._restore_check_state(snapshot)
        replay_sequence = [
            deepcopy(item)
            for item in transaction.get("roll_sequence", [])
            if hasattr(item, "actor")
        ]
        if not replay_sequence:
            replay_sequence = [deepcopy(original_outcome)]
        for forced_outcome in replay_sequence:
            self.rules_engine.force_next_check_outcome(forced_outcome)
        self._replaying_check_transaction = True
        self._check_transaction_candidate = None
        try:
            replayed = self.resolve(original_action)
        except Exception:
            self._restore_check_state(snapshot)
            raise
        finally:
            self._replaying_check_transaction = False
            self.rules_engine.clear_forced_check_outcomes()

        replayed.action = acceptance_action
        replayed.rules_text = f"{actor_name}保留刚才的检定结果。 {replayed.rules_text}".strip()
        replayed.payload.update(
            {
                "before_roll": original_outcome,
                "roll": replayed.payload.get("roll", original_outcome),
                "check_transaction_accepted": True,
                "check_transaction_acceptance_text": f"{actor_name}接受这次检定结果。",
                "committed_source_action": deepcopy(original_action),
                "_already_finalized": True,
            }
        )
        replayed.payload["post_check_windows"] = [
            window
            for window in (replayed.payload.get("post_check_windows") or [])
            if not isinstance(window, dict)
            or window.get("kind") not in {"trait_invocation", "bond_invocation"}
        ]
        self.post_check_state.discard_actor(actor_name)
        return replayed

    def _reconciled_clock_rules_text(self, reconciliation: dict[str, object]) -> str:
        change = reconciliation.get("clock_change")
        if change is None:
            return ""
        delta = int(getattr(change, "delta", 0))
        if delta > 0:
            verb = f"推进 {delta} 格"
        elif delta < 0:
            verb = f"擦除 {abs(delta)} 格"
        else:
            verb = "进度不变"
        return f" 命刻【{change.clock_name}】按新结果重新结算：{verb}，当前 {change.after}/{change.max_segments}。"

    def _resolve_opportunity(self, action: Action) -> ActionResolution:
        return self.opportunity_resolver.resolve(action)

    def pending_opportunity(self, actor: str = "") -> dict[str, object] | None:
        actor_name = str(actor or "").strip()
        window = (
            self.decision_window_manager.find_pending(kind="opportunity_parameter", owner=actor_name)
            if actor_name
            else None
        )
        if window is None and not actor_name:
            pending = self.decision_window_manager.pending(kind="opportunity_parameter")
            window = pending[0] if len(pending) == 1 else None
        if window is None:
            return None
        return {
            "actor": window.owner,
            "effect": "reveal",
            "label": "揭示",
            "window_id": window.window_id,
            "required_parameter": window.payload.get("required_parameter", "target"),
        }

    def _reveal_opportunity_motivation(self, action: Action, target: str) -> tuple[str, bool]:
        return self.opportunity_resolver.reveal_motivation(action, target)

    def _normalize_opportunity_effect(self, effect: str) -> str:
        return self.opportunity_resolver.normalize_effect(effect)

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
        pending_pvp = self.check_batch_manager.pending(kind="pvp_opposed")
        if pending_pvp:
            batch = pending_pvp[0]
            resolution = ActionResolution(
                action,
                "玩家对抗检定尚未定稿。",
                {
                    "pvp_pending": True,
                    "check_batch_id": batch.batch_id,
                    "opposed_rolls": dict(batch.rolls),
                    "decision_windows": self.decision_window_manager.public_summary(),
                },
            )
            self._progress_check_batches(resolution)
            return resolution
        left_name = str(action.parameters["actor"]).strip()
        right_name = str(action.parameters["target"]).strip()
        if left_name == right_name:
            raise ValueError("玩家对抗检定必须由两个不同角色进行。")
        left = self.character_manager.get(left_name)
        right = self.character_manager.get(right_name)
        if "pc" not in left.traits or "pc" not in right.traits:
            raise ValueError("玩家对玩家冲突的双方都必须是玩家角色。")
        source_action = deepcopy(action)
        source_action.parameters.update(
            {
                "_pvp_left": left_name,
                "_pvp_right": right_name,
                "_pvp_attributes": list(
                    action.parameters.get("attributes", ["WLP", "WLP"])
                ),
                "_pvp_left_modifier": self._int_parameter(
                    action.parameters,
                    "left_modifier",
                    0,
                ),
                "_pvp_right_modifier": self._int_parameter(
                    action.parameters,
                    "right_modifier",
                    0,
                ),
            }
        )
        batch = self.check_batch_manager.begin(
            kind="pvp_opposed",
            source_action=source_action,
            actor_order=[left.name, right.name],
            roles={left.name: "left", right.name: "right"},
        )
        resolution = ActionResolution(
            action,
            "开始玩家对抗检定。",
            {
                "pvp_pending": True,
                "check_batch_id": batch.batch_id,
                "left": left.name,
                "right": right.name,
            },
        )
        self._progress_check_batches(resolution)
        if resolution.payload.get("opposed_check") is not None:
            resolution.payload["pvp_pending"] = False
        else:
            resolution.rules_text = (
                f"{resolution.rules_text} 对抗结果尚在等待双方处理检定选择。"
            )
            resolution.payload["decision_windows"] = (
                self.decision_window_manager.public_summary()
            )
        return resolution

    def _resolve_absent_player(self, action: Action) -> ActionResolution:
        actor = str(action.parameters.get("actor") or action.parameters.get("character") or "")
        mode = str(action.parameters.get("mode") or "fade_out")
        note = str(action.parameters.get("note") or "")
        mode_text = {
            "fade_out": "暂时淡出镜头",
            "return_later": "稍后回归",
            "group_control": "由同桌暂管",
            "table_control": "由同桌暂管",
        }.get(mode, mode)
        if actor and self.character_manager.exists(actor):
            self.world_state.mark_player_absent(actor, note)
            self.world_state.remember_subject_fact(actor, f"本场缺席处理：{mode_text}。{note}".strip())
        return ActionResolution(
            action,
            f"缺席玩家处理：{actor or '未指定角色'} {mode_text}。{note}",
            {"actor": actor, "mode": mode, "note": note},
        )

    def _string_sequence(self, value) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [piece.strip() for piece in re.split(r"[、,，/]+", value) if piece.strip()]
        if isinstance(value, dict):
            text = self._name_from_structured_value(value)
            return [text] if text else []
        result: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = self._name_from_structured_value(item)
            else:
                text = str(item).strip()
            if text:
                result.append(text)
        return result

    def _name_from_structured_value(self, value: dict) -> str:
        for key in ("name", "title", "actor", "character", "enemy", "npc", "id"):
            text = str(value.get(key) or "").strip()
            if text:
                return text
        return ""

    def _resolve_npc_act(self, action: Action) -> ActionResolution:
        translated = self.npc_action_adapter.translate(action)
        if isinstance(translated, ActionResolution):
            return translated
        self._validate_timed_action_restriction(translated)
        resolution = self.action_dispatcher.dispatch(translated)
        if resolution is None:
            return ActionResolution(
                action=action,
                rules_text="NPC 动作适配后没有找到对应的规则处理器。",
                payload={"npc_action_unresolved": True},
            )
        resolution.action = action
        return resolution

    def _resolve_spell(self, action: Action) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        spell_name = action.parameters.get("spell_name")
        if spell_name:
            try:
                definition = get_spell_definition(spell_name)
            except ValueError:
                target_name = (
                    action.parameters.get("target")
                    or action.parameters.get("subject")
                    or action.parameters.get("scene_object")
                    or "当前魔法目标"
                )
                return self._resolve_scene_object_spell(action, actor, target_name, spell_name)
            if action.parameters.get("_acceleration_window_id"):
                action = self.spell_parameter_manager.bind_explicit_choices(
                    action,
                    definition,
                    str(action.parameters.get("declaration_text") or ""),
                )
                self._validate_acceleration_followup(action)
            resolution = self._resolve_spell_from_definition(action, spell_name)
            return self._apply_ally_spell_triggers(action, resolution)

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
                "target_number": self._target_number_or_defense(action.parameters, target.name, "magic"),
                "modifier": self._int_parameter(action.parameters, "modifier", 0)
                + actor.equipment_spell_bonus
                + actor.npc_spell_check_bonus,
                "weapon_damage": action.parameters.get("fixed_damage", 0)
                + self._hero_damage_bonus(actor, is_spell=True)
                + actor.equipment_spell_damage_bonus
                + self._npc_spell_damage_bonus(
                    actor,
                    str(action.parameters.get("spell_name") or ""),
                ),
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
        return self._apply_ally_spell_triggers(action, resolution)

    def _apply_ally_spell_triggers(
        self,
        action: Action,
        resolution: ActionResolution,
    ) -> ActionResolution:
        if resolution.payload.get("spell_failed"):
            return resolution
        actor_name = str(action.parameters.get("actor") or "")
        if not actor_name or not self.character_manager.exists(actor_name):
            return resolution
        actor = self.character_manager.get(actor_name)
        raw_targets = action.parameters.get("targets")
        if isinstance(raw_targets, str):
            target_names = [name.strip() for name in re.split(r"[、,，/]+", raw_targets) if name.strip()]
        elif isinstance(raw_targets, list):
            target_names = [str(name).strip() for name in raw_targets if str(name).strip()]
        else:
            target = str(action.parameters.get("target") or "").strip()
            target_names = [target] if target else []
        actor_is_pc = "pc" in actor.traits
        allies = [
            name
            for name in target_names
            if name != actor.name
            and self.character_manager.exists(name)
            and (("pc" in self.character_manager.get(name).traits) == actor_is_pc)
        ]
        if not allies:
            return resolution
        lifecycle = self.skill_lifecycle.trigger(
            "after_ally_spell",
            actor,
            effect_targets=allies,
            ally_targets=len(allies),
            target_names=allies,
            magic_weapon_equipped=self.skill_trigger_manager.has_magic_weapon(actor),
        )
        self._capture_skill_lifecycle(lifecycle)
        return resolution

    def _resolve_spell_from_definition(self, action: Action, spell_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        definition = get_spell_definition(spell_name)
        access_error = self._spell_access_error(actor, definition)
        if access_error:
            return ActionResolution(
                action=action,
                rules_text=access_error,
                payload={
                    "spell_failed": True,
                    "spell_name": definition.name,
                    "action_uncommitted": True,
                },
            )
        explicit_scene_object = str(action.parameters.get("scene_object") or "").strip()
        if (
            explicit_scene_object
            and not self.character_manager.exists(explicit_scene_object)
            and definition.effect_type == SpellEffectType.DAMAGE
        ):
            return self._resolve_scene_object_spell(
                action,
                actor,
                explicit_scene_object,
                definition.name,
                default_mp_cost=definition.mp_cost,
            )

        requirement = self.spell_parameter_manager.inspect(action, definition, actor.name)
        if requirement is not None:
            scope_kind = "conflict" if self.conflict_manager.state.active else "scene"
            scope_id = self.conflict_manager.state.scene_name if self.conflict_manager.state.active else "current"
            window = self.spell_parameter_manager.open_window(
                action,
                definition,
                actor.name,
                requirement,
                scope_kind=scope_kind,
                scope_id=scope_id,
            )
            return ActionResolution(
                action=action,
                rules_text=window.prompt,
                payload={
                    "spell_name": definition.name,
                    "spell_parameter_required": True,
                    "action_uncommitted": True,
                    "decision_window_id": window.window_id,
                    "decision_windows": self.decision_window_manager.public_summary(),
                    "required_fields": list(requirement.missing_fields),
                    "invalid_targets": list(requirement.invalid_targets),
                },
            )
        self.spell_parameter_manager.prepare_action(action, definition)
        target_names = self._spell_target_names(action, definition, actor.name)
        if not target_names:
            return ActionResolution(
                action=action,
                rules_text=f"【{definition.name}】当前没有合法目标，法术未发动。",
                payload={
                    "spell_failed": True,
                    "spell_name": definition.name,
                    "action_uncommitted": True,
                },
            )
        missing_targets = [name for name in target_names if not self._spell_target_exists(name)]
        if missing_targets:
            raise ValueError(f"法术【{definition.name}】的目标已不在当前规则场景中。")
        narrative_targets = [name for name in target_names if not self.character_manager.exists(name)]
        if narrative_targets and definition.effect_type not in self.NARRATIVE_TARGET_EFFECTS:
            return ActionResolution(
                action=action,
                rules_text=(
                    f"【{definition.name}】需要使用目标的生命值、防御或装备数据结算；"
                    f"请先为{'、'.join(narrative_targets)}建立 NPC 战斗数据。"
                ),
                payload={
                    "spell_failed": True,
                    "spell_name": definition.name,
                    "narrative_targets_need_combat_profile": narrative_targets,
                    "action_uncommitted": True,
                },
            )
        target_names, interposition_notices = self._npc_spell_interpositions(
            actor,
            target_names,
            definition,
        )
        if interposition_notices:
            action.parameters["_npc_interpose_text"] = " ".join(
                interposition_notices
            )
        target = (
            self.character_manager.get(target_names[0])
            if self.character_manager.exists(target_names[0])
            else None
        )

        default_mp_cost = (
            definition.mp_cost * len(target_names)
            if self._is_multi_target_spell(definition)
            and definition.mp_cost_per_target
            else definition.mp_cost
        )
        # Canonical spell costs come from the rule definition. Tool callers may
        # choose targets and skill options, but cannot underpay a multi-target
        # spell by submitting a smaller ``mp_cost``.
        requested_mp_cost = default_mp_cost
        spell_skills = self.spell_skill_manager.prepare(
            actor,
            definition,
            base_mp_cost=requested_mp_cost,
            target_count=len(target_names),
            parameters=action.parameters,
        )
        if not spell_skills.valid:
            return ActionResolution(
                action=action,
                rules_text=spell_skills.error,
                payload={
                    "spell_failed": True,
                    "spell_name": definition.name,
                    "skill_validation_failed": True,
                },
            )
        action.parameters["_spell_skill_attributes"] = list(spell_skills.attributes)
        action.parameters["_spell_skill_check_modifier"] = spell_skills.check_modifier
        action.parameters["_spell_skill_damage_bonus"] = spell_skills.damage_bonus
        action.parameters["_spell_skill_preparation"] = spell_skills.as_payload()
        mp_cost = spell_skills.mp_cost
        acceleration_window_id = str(action.parameters.get("_acceleration_window_id") or "").strip()
        if acceleration_window_id:
            acceleration_window = self.decision_window_manager.get(acceleration_window_id)
            max_spell_mp = int(
                (acceleration_window.payload.get("max_spell_mp", 10) if acceleration_window else 10)
                or 10
            )
            if mp_cost > max_spell_mp:
                return ActionResolution(
                    action=action,
                    rules_text=(
                        f"【加速术】只能顺势施放总精神值消耗不高于 {max_spell_mp} 点的法术；"
                        f"【{definition.name}】本次需要 {mp_cost} 点。"
                    ),
                    payload={
                        "spell_failed": True,
                        "action_uncommitted": True,
                        "spell_name": definition.name,
                        "acceleration_mp_limit": max_spell_mp,
                        "requested_spell_mp": mp_cost,
                    },
                )
        skill_window_id = str(
            action.parameters.get("_skill_followup_window_id") or ""
        ).strip()
        if skill_window_id:
            skill_window = self.decision_window_manager.get(skill_window_id)
            if (
                skill_window is not None
                and str(
                    skill_window.payload.get("skill")
                    or skill_window.payload.get("label")
                    or ""
                ).strip()
                == "奥灵回响"
            ):
                max_spell_mp = max(
                    (
                        int(option.get("max_mp", 0) or 0)
                        for option in skill_window.options
                        if str(option.get("choice") or "") == "cast_spell"
                    ),
                    default=0,
                )
                if mp_cost > max_spell_mp:
                    return ActionResolution(
                        action=action,
                        rules_text=(
                            f"【奥灵回响】只能顺势施放总精神值消耗不高于"
                            f" {max_spell_mp} 点的法术；"
                            f"【{definition.name}】本次需要 {mp_cost} 点。"
                        ),
                        payload={
                            "spell_failed": True,
                            "action_uncommitted": True,
                            "spell_name": definition.name,
                            "arcanum_echo_mp_limit": max_spell_mp,
                            "requested_spell_mp": mp_cost,
                        },
                    )
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

        if (
            definition.effect_type == SpellEffectType.DAMAGE
            and definition.fixed_damage_only
        ):
            resolution = self._resolve_spell_fixed_damage(
                action,
                definition,
                target_names,
            )
            resolution.action = action
            resolution.payload["resource_change"] = mp_change
            resolution.payload["spell_name"] = definition.name
            resolution.payload["spell_skill_preparation"] = spell_skills.as_payload()
            return resolution

        if definition.effect_type == SpellEffectType.DAMAGE:
            assert target is not None
            if len(target_names) > 1:
                resolution = self._resolve_spell_damage_multi(action, definition, target_names)
            else:
                resolution = self._resolve_spell_damage(action, definition, target.name)
            resolution.action = action
            resolution.payload["resource_change"] = mp_change
            resolution.payload["spell_name"] = definition.name
            resolution.payload["spell_skill_preparation"] = spell_skills.as_payload()
            return resolution

        if definition.effect_type == SpellEffectType.MP_DAMAGE:
            assert target is not None
            if len(target_names) > 1:
                resolution = self._resolve_spell_mp_damage_multi(
                    action,
                    definition,
                    target_names,
                    mp_change,
                )
            else:
                resolution = self._resolve_spell_mp_damage(
                    action,
                    definition,
                    target.name,
                    mp_change,
                )
            resolution.payload["spell_skill_preparation"] = spell_skills.as_payload()
            return resolution

        if definition.effect_type == SpellEffectType.HEAL:
            assert target is not None
            if len(target_names) > 1:
                return self._resolve_spell_heal_multi(action, definition, target_names, mp_change)
            return self._resolve_spell_heal(action, definition, target.name, mp_change)

        if definition.effect_type == SpellEffectType.STATUS_CLEAR:
            assert target is not None
            if len(target_names) > 1:
                return self._resolve_spell_status_clear_multi(action, definition, target_names, mp_change)
            return self._resolve_spell_status_clear(action, definition, target.name, mp_change)

        if definition.effect_type == SpellEffectType.STATUS_APPLY:
            assert target is not None
            if len(target_names) > 1:
                return self._resolve_spell_status_apply_multi(action, definition, target_names, mp_change)
            return self._resolve_spell_status_apply(action, definition, target.name, mp_change)

        if definition.effect_type == SpellEffectType.DISPEL:
            assert target is not None
            return self._resolve_spell_dispel(action, definition, target.name, mp_change)

        if definition.effect_type == SpellEffectType.IMMEDIATE_ATTACK:
            assert target is not None
            return self._resolve_spell_immediate_attack(
                action,
                definition,
                target.name,
                mp_change,
            )

        if definition.name == "终焉降临":
            assert target is not None
            target_name = target.name
            amount = 20 + target.level // 2
            before, after = self._apply_damage_from(actor.name, target_name, amount)
            zero_hp_event = None
            if after == 0:
                zero_hp_event = self.conflict_manager.resolve_zero_hp(
                    target_name,
                    source_actor=self._zero_hp_source_actor(
                        action,
                        actor.name,
                    ),
                )
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

        if definition.requires_check:
            resolution = self._resolve_spell_checked_timed_effect(
                action,
                definition,
                target_names,
                mp_change,
            )
            resolution.payload["spell_skill_preparation"] = spell_skills.as_payload()
            return resolution

        timed_effects = [
            self._register_spell_effect(actor.name, target_name, action, definition)
            for target_name in target_names
        ]
        rules_text = self._spell_effect_group_rules_text(
            actor.name,
            target_names,
            definition,
            timed_effects,
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

    def _npc_spell_interpositions(
        self,
        actor: Character,
        target_names: list[str],
        definition,
    ) -> tuple[list[str], list[str]]:
        if not self.conflict_manager.state.active:
            return list(target_names), []
        dangerous_effects = {
            SpellEffectType.DAMAGE,
            SpellEffectType.MP_DAMAGE,
            SpellEffectType.STATUS_APPLY,
            SpellEffectType.DISPEL,
            SpellEffectType.DAMAGE_VULNERABILITY,
        }
        if (
            definition.effect_type not in dangerous_effects
            and definition.name not in {"终焉降临", "时空静滞"}
        ):
            return list(target_names), []

        actor_side = self.conflict_manager.combat_side(actor.name)
        resolved: list[str] = []
        notices: list[str] = []
        for target_name in target_names:
            if (
                not self.character_manager.exists(target_name)
                or self.conflict_manager.combat_side(target_name) == actor_side
            ):
                resolved.append(target_name)
                continue
            interposer = self.conflict_manager.npc_interposer_for(
                target_name,
                source_actor=actor.name,
            )
            if interposer is None:
                resolved.append(target_name)
                continue
            resolved.append(interposer.name)
            notices.append(
                f"{interposer.name}挺身代替{target_name}承受【{definition.name}】。"
            )
        return resolved, notices

    @staticmethod
    def _attach_npc_interposition_notice(
        resolution: ActionResolution,
    ) -> None:
        if resolution.payload.get("npc_interposition_noted"):
            return
        notice = str(
            resolution.action.parameters.get("_npc_interpose_text") or ""
        ).strip()
        if not notice:
            return
        resolution.rules_text = f"{notice} {resolution.rules_text}".strip()
        resolution.payload["npc_interposition_noted"] = True

    def _spell_access_error(self, actor: Character, definition) -> str:
        if actor.level < int(definition.minimum_level or 0):
            return (
                f"【{definition.name}】要求施法者至少达到"
                f"{definition.minimum_level}级；法术未发动。"
            )
        if definition.allowed_npc_ranks:
            rank = self.conflict_manager.state.enemy_ranks.get(actor.name)
            if rank is None or rank.value not in definition.allowed_npc_ranks:
                return (
                    f"【{definition.name}】只允许指定阶级的NPC使用；"
                    "法术未发动。"
                )
        if definition.npc_last_turn_only:
            action_count = int(
                self.conflict_manager.state.enemy_action_counts.get(
                    actor.name,
                    1,
                )
                or 1
            )
            is_last = action_count <= 1 or (
                self.conflict_manager.state.current_bonus_actor == actor.name
                and not any(
                    queued_actor == actor.name and queued_kind == "rank"
                    for queued_actor, queued_kind in zip(
                        self.conflict_manager.state.queued_turns,
                        self.conflict_manager.state.queued_turn_kinds,
                    )
                )
            )
            if not is_last:
                return (
                    f"【{definition.name}】只能在该NPC本轮最后一个回合使用；"
                    "法术未发动。"
                )
        return ""

    def _spell_target_names(self, action: Action, definition, actor_name: str) -> list[str]:
        if definition.target == SpellTarget.SELF:
            return [actor_name]
        if definition.target == SpellTarget.ALL_ENEMIES:
            return self.spell_parameter_manager.target_candidates(
                definition,
                actor_name,
            )
        raw_targets = (
            action.parameters.get("targets")
            or action.parameters.get("target_names")
            or action.parameters.get("target")
            or action.parameters.get("subject")
            or action.parameters.get("scene_object")
            or actor_name
        )
        if isinstance(raw_targets, str):
            names = [
                piece.strip()
                for piece in re.split(r"\s*[、,，/；;]\s*", raw_targets)
                if piece.strip()
            ]
        elif isinstance(raw_targets, list):
            names = [str(name).strip() for name in raw_targets if str(name).strip()]
        else:
            names = [str(raw_targets).strip()]
        if not names:
            names = [actor_name]
        if definition.target == SpellTarget.UP_TO_THREE_CREATURES:
            return names[:3]
        if definition.target == SpellTarget.ANY_VISIBLE_CREATURES:
            return names
        return names[:1]

    def _spell_target_exists(self, name: str) -> bool:
        return self.character_manager.exists(name) or bool(
            self.scene_manager is not None and self.scene_manager.is_participant(name)
        )

    def _is_multi_target_spell(self, definition) -> bool:
        return definition.target in {
            SpellTarget.UP_TO_THREE_CREATURES,
            SpellTarget.ANY_VISIBLE_CREATURES,
            SpellTarget.ALL_ENEMIES,
        }

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
        target_number = self._target_number_parameter(action.parameters, default=10)
        outcome = self.rules_engine.roll_check(
            actor=actor,
            attributes=action.parameters.get("attributes", ["INS", "WLP"]),
            target_number=target_number,
            modifier=self._int_parameter(action.parameters, "modifier", 0)
            + actor.equipment_spell_bonus
            + actor.npc_spell_check_bonus,
            target=target_name,
            reason=action.parameters.get("reasoning", ""),
            critical_on_any_pair=bool(action.parameters.get("critical_on_any_pair", False)),
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
        lifecycle = None
        if guarded_target:
            rules_text = f"{actor.name} 进入防御姿态，并掩护 {guarded_target} 免受近战攻击。"
            if self.character_manager.exists(str(guarded_target)):
                lifecycle = self.skill_lifecycle.trigger(
                    "after_guard_with_cover",
                    actor,
                    target=self.character_manager.get(str(guarded_target)),
                )
        else:
            rules_text = f"{actor.name} 进入防御姿态，本轮对所有伤害获得抵抗，对抗检定 +2。"
            lifecycle = self.skill_lifecycle.trigger("after_guard_without_cover", actor)
        if lifecycle is not None:
            self._capture_skill_lifecycle(lifecycle)
        payload: dict[str, object] = {
            "guarding": True,
            "guarded_target": guarded_target,
        }
        npc_events = self.combat_trait_manager.after_guard(
            actor,
            guarded_target=str(guarded_target or ""),
            terrain=str(action.parameters.get("terrain") or ""),
        )
        self._append_combat_trait_events(payload, npc_events)
        self._resolve_npc_ability_events(action, npc_events, payload)
        if any(
            attack.bonus_if_previous_guard > 0
            for attack in actor.npc_attacks
        ):
            actor.npc_skill_effects["previous_action_guarded"] = True
        return ActionResolution(
            action=action,
            rules_text=rules_text,
            payload=payload,
        )

    def _resolve_spell_damage(self, action: Action, definition, target_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        spell_action = Action(
            action_type=ActionType.REQUEST_ROLL,
            parameters={
                **action.parameters,
                "target": target_name,
                "attributes": self._spell_check_attributes(
                    actor,
                    action,
                    definition,
                ),
                "target_number": self._target_number_or_defense(action.parameters, target_name, definition.defense_type),
                "weapon_damage": definition.fixed_damage
                + self._hero_damage_bonus(actor, is_spell=True)
                + actor.equipment_spell_damage_bonus
                + self._npc_spell_damage_bonus(actor, definition.name)
                + self._int_parameter(action.parameters, "_spell_skill_damage_bonus", 0)
                + self._int_parameter(action.parameters, "_gadget_damage_bonus", 0),
                "damage_type": self._selected_damage_type(action, definition) or definition.damage_type,
                "modifier": self._int_parameter(action.parameters, "modifier", 0)
                + actor.equipment_spell_bonus
                + actor.npc_spell_check_bonus
                + self._int_parameter(action.parameters, "_spell_skill_check_modifier", 0),
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
            and (
                not definition.drain_requires_target_above_zero
                or self.character_manager.get(target_name).hp > 0
            )
        ):
            recovered = int(
                resolution.payload.get(
                    "actual_hp_loss",
                    resolution.payload["roll"].damage,
                )
                or 0
            ) // 2
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
        conflict_event = resolution.payload.get("conflict_event")
        phase_changed = (
            isinstance(conflict_event, ConflictEvent)
            and conflict_event.event_type in {"boss_phase", "escalation"}
        )
        if (
            resolution.payload["roll"].success
            and definition.apply_status_on_success
            and not phase_changed
        ):
            statuses = self._selected_statuses(action, definition)
            applied = [
                status
                for status in statuses
                if self.conflict_manager.apply_status(target_name, status)
            ]
            resolution.payload["status_effects"] = statuses
            resolution.payload["status_applied_effects"] = applied
            resolution.payload["status_applied"] = bool(applied)
            if applied:
                resolution.rules_text += (
                    f" {target_name} 被施加"
                    + "、".join(self._status_name(status) for status in applied)
                    + "。"
                )
        self._attach_spell_opportunity_metadata(
            resolution.payload,
            definition,
            [target_name],
        )
        roll = resolution.payload["roll"]
        damaged_targets = 1 if roll.success and roll.damage > 0 and roll.applied_affinity != Affinity.ABSORB else 0
        self._apply_spell_damage_resource_triggers(action, resolution, damaged_targets=damaged_targets)
        return resolution

    def _resolve_spell_damage_multi(self, action: Action, definition, target_names: list[str]) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        target_numbers = self._target_numbers_for_targets(action.parameters, target_names, definition.defense_type)
        roll = self.rules_engine.roll_check(
            actor=actor,
            attributes=self._spell_check_attributes(
                actor,
                action,
                definition,
            ),
            target_number=min(target_numbers.values()),
            modifier=self._int_parameter(action.parameters, "modifier", 0)
            + actor.equipment_spell_bonus
            + actor.npc_spell_check_bonus
            + self._int_parameter(action.parameters, "_spell_skill_check_modifier", 0)
            + self._consume_next_check_bonus(actor.name),
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
        conflict_events: list[ConflictEvent] = []
        status_applied_by_target: dict[str, list[StatusEffect]] = {}
        for target_name in hit_targets:
            target = self.character_manager.get(target_name)
            next_damage_bonus = self._consume_next_damage_bonus(target.name)
            incoming_damage_bonus = self._incoming_damage_bonus(
                target.name,
                damage_type,
            )
            damage, affinity = self.rules_engine.compute_damage(
                high_roll=roll.high_roll,
                weapon_damage=definition.fixed_damage
                + self._hero_damage_bonus(actor, is_spell=True)
                + actor.equipment_spell_damage_bonus
                + self._npc_spell_damage_bonus(actor, definition.name)
                + self._int_parameter(action.parameters, "_spell_skill_damage_bonus", 0)
                + self._int_parameter(action.parameters, "_gadget_damage_bonus", 0)
                + next_damage_bonus
                + incoming_damage_bonus,
                damage_type=damage_type,
                target=target,
                ignore_resist=definition.ignore_resist or self._attack_ignores_resist(actor, damage_type),
                ignore_all_affinities=action.parameters.get("ignore_all_affinities", False)
                or actor.equipment_ignore_all_affinities,
            )
            if damage >= 0:
                before_hp, after_hp = self._apply_damage_from(actor.name, target.name, damage)
            else:
                before_hp, after_hp = self.character_manager.modify_resource(target.name, "hp", -damage)
            actual_hp_loss = max(0, before_hp - after_hp)
            dealt = actual_hp_loss if damage >= 0 else after_hp - before_hp
            self._after_actor_deals_damage(actor, target, before_hp, after_hp)
            total_damage += max(0, dealt)
            payload["damage_results"].append(
                {
                    "target": target.name,
                    "damage": abs(damage),
                    "damage_type": damage_type,
                    "affinity": affinity,
                    "actual_hp_loss": actual_hp_loss,
                    "hp_after": after_hp,
                }
            )
            self._apply_combat_trait_after_damage(
                target.name,
                affinity,
                abs(damage),
                payload,
                hp_before=before_hp,
                action=action,
                source_actor=actor.name,
                is_spell=True,
            )
            applied_statuses: list[StatusEffect] = []
            if definition.apply_status_on_success:
                applied_statuses = [
                    status
                    for status in self._selected_statuses(action, definition)
                    if self.conflict_manager.apply_status(target.name, status)
                ]
            if after_hp == 0:
                after_hp, event, event_text = self._resolve_zero_hp_after_damage(
                    action,
                    source_actor=self._zero_hp_source_actor(
                        action,
                        actor.name,
                    ),
                    target_name=target.name,
                    payload=payload,
                    damage_type=damage_type,
                )
                payload["damage_results"][-1]["hp_after"] = after_hp
                if event is not None:
                    conflict_events.append(event)
                    if event.event_type in {"boss_phase", "escalation"}:
                        # The spell finished affecting the defeated form before
                        # the new form appeared; phase restoration clears those
                        # statuses instead of carrying them onto the new body.
                        applied_statuses = []
                if event_text:
                    rules_text += f" {event_text}"
            self._remember_damage_outcome(actor.name, target.name, roll)
            rules_text += f" {target.name} 伤害 {damage}（{self._affinity_label(affinity)}）。"
            if applied_statuses:
                status_applied_by_target[target.name] = applied_statuses
                rules_text += (
                    f" {target.name} 被施加"
                    + "、".join(
                        self._status_name(status)
                        for status in applied_statuses
                    )
                    + "。"
                )
        roll.damage = total_damage
        roll.damage_type = damage_type
        if conflict_events:
            payload["conflict_events"] = conflict_events
            payload["conflict_event"] = conflict_events[0]
        if status_applied_by_target:
            payload["status_applied_by_target"] = status_applied_by_target
            payload["status_applied_targets"] = list(status_applied_by_target)
        self._attach_spell_opportunity_metadata(
            payload,
            definition,
            hit_targets,
        )
        damaged_targets = sum(
            1
            for result in payload["damage_results"]
            if result["damage"] > 0 and result["affinity"] != Affinity.ABSORB
        )
        self._apply_spell_damage_resource_triggers(action, ActionResolution(action, rules_text, payload), damaged_targets=damaged_targets)
        if payload.get("skill_resource_changes"):
            rules_text += " " + " ".join(
                f"【{change['source']}】恢复 {change['amount']} MP。"
                for change in payload["skill_resource_changes"]
            )
        return ActionResolution(action=action, rules_text=rules_text, payload=payload)

    @staticmethod
    def _attach_spell_opportunity_metadata(
        payload: dict[str, object],
        definition,
        hit_targets: list[str],
    ) -> None:
        roll = payload.get("roll")
        if not bool(getattr(roll, "critical_success", False)):
            return
        metadata: dict[str, object] = {
            "spell_name": definition.name,
            "targets": list(hit_targets),
        }
        if definition.status_effect is not None:
            metadata["statuses"] = [definition.status_effect.value]
        if definition.opportunity_turn_penalty:
            metadata["turn_penalty"] = int(
                definition.opportunity_turn_penalty
            )
        if definition.opportunity_ground_flying:
            metadata["ground_flying"] = True
        if len(metadata) > 2:
            payload["spell_opportunity"] = metadata

    def _resolve_spell_fixed_damage(
        self,
        action: Action,
        definition,
        target_names: list[str],
    ) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        damage_type = (
            self._selected_damage_type(action, definition)
            or definition.damage_type
        )
        base_amount = (
            int(definition.fixed_damage)
            + self._hero_damage_bonus(actor, is_spell=True)
            + actor.equipment_spell_damage_bonus
            + self._npc_spell_damage_bonus(actor, definition.name)
            + self._int_parameter(
                action.parameters,
                "_spell_skill_damage_bonus",
                0,
            )
            + self._int_parameter(
                action.parameters,
                "_gadget_damage_bonus",
                0,
            )
        )
        payload: dict[str, object] = {
            "spell_name": definition.name,
            "damage_results": [],
            "hit_targets": list(target_names),
            "fixed_damage": True,
        }
        conflict_events: list[ConflictEvent] = []
        rules_parts = [
            f"{actor.name}施放【{definition.name}】。"
        ]
        damaged_targets = 0
        for target_name in target_names:
            target = self.character_manager.get(target_name)
            next_damage_bonus = self._consume_next_damage_bonus(target_name)
            before_hp, after_hp, affinity = self._apply_fixed_damage(
                target_name,
                base_amount + next_damage_bonus,
                damage_type,
                source_name=actor.name,
                ignore_resist=definition.ignore_resist
                or self._attack_ignores_resist(actor, damage_type),
                ignore_resist_and_immune=bool(
                    action.parameters.get("ignore_resist_and_immune")
                ),
            )
            actual_hp_loss = max(0, before_hp - after_hp)
            if actual_hp_loss > 0 and affinity != Affinity.ABSORB:
                damaged_targets += 1
            self._after_actor_deals_damage(actor, target, before_hp, after_hp)
            self._apply_combat_trait_after_damage(
                target_name,
                affinity,
                actual_hp_loss,
                payload,
                hp_before=before_hp,
                action=action,
                source_actor=actor.name,
                is_spell=True,
            )
            event_text = ""
            if after_hp == 0:
                after_hp, event, event_text = self._resolve_zero_hp_after_damage(
                    action,
                    source_actor=self._zero_hp_source_actor(
                        action,
                        actor.name,
                    ),
                    target_name=target_name,
                    payload=payload,
                    damage_type=damage_type,
                )
                if event is not None:
                    conflict_events.append(event)
            result = {
                "target": target_name,
                "base_damage": base_amount,
                "next_damage_bonus": next_damage_bonus,
                "damage_type": damage_type,
                "affinity": affinity,
                "actual_hp_loss": actual_hp_loss,
                "hp_after": after_hp,
            }
            payload["damage_results"].append(result)
            rules_parts.append(
                f"{target_name}失去{actual_hp_loss}点HP"
                f"（{self._affinity_label(affinity)}）。"
            )
            if event_text:
                rules_parts.append(event_text)
        if conflict_events:
            payload["conflict_events"] = conflict_events
            payload["conflict_event"] = conflict_events[0]
        resolution = ActionResolution(
            action=action,
            rules_text=" ".join(rules_parts),
            payload=payload,
        )
        self._apply_spell_damage_resource_triggers(
            action,
            resolution,
            damaged_targets=damaged_targets,
        )
        return resolution

    def _apply_spell_damage_resource_triggers(
        self,
        action: Action,
        resolution: ActionResolution,
        *,
        damaged_targets: int,
    ) -> None:
        actor = self.character_manager.get(action.parameters["actor"])
        effects = self.skill_trigger_manager.emit(
            "after_spell_damage",
            actor,
            damaged_targets=damaged_targets,
        ).effects
        if not effects:
            self._apply_chimerist_poison_trigger(action, resolution)
            return
        for effect in effects:
            if effect.resource != "mp" or effect.amount <= 0:
                continue
            before, after = self.character_manager.modify_resource(actor.name, "mp", effect.amount)
            resolution.payload.setdefault("skill_resource_changes", []).append(
                {
                    "source": effect.source,
                    "resource": "mp",
                    "amount": after - before,
                    "before": before,
                    "after": after,
                    "note": effect.note,
                }
            )
            resolution.rules_text += f" 【{effect.source}】恢复 {after - before} MP。"
        self._apply_chimerist_poison_trigger(action, resolution)

    def _apply_chimerist_poison_trigger(
        self,
        action: Action,
        resolution: ActionResolution,
    ) -> None:
        origin_species = str(
            action.parameters.get("chimerist_origin_species")
            or action.parameters.get("mimic_species")
            or ""
        ).strip()
        if not origin_species:
            return
        target_names: list[str] = []
        hit_targets = resolution.payload.get("hit_targets")
        if isinstance(hit_targets, list):
            target_names.extend(str(name) for name in hit_targets if str(name))
        else:
            roll = resolution.payload.get("roll")
            target_name = str(action.parameters.get("target") or "").strip()
            if target_name and bool(getattr(roll, "success", False)):
                target_names.append(target_name)
        damaged_targets = [
            {"target": name, "species": self._species_text(self.character_manager.get(name))}
            for name in target_names
            if self.character_manager.exists(name)
        ]
        if not damaged_targets:
            return
        outcome = self.skill_lifecycle.trigger(
            "after_chimerist_spell_damage",
            self.character_manager.get(action.parameters["actor"]),
            origin_species=origin_species,
            damaged_targets=damaged_targets,
        )
        self._capture_skill_lifecycle(outcome)

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
            attributes=self._spell_check_attributes(
                actor,
                action,
                definition,
            ),
            target_number=self._target_number_or_defense(action.parameters, target_name, definition.defense_type),
            modifier=self._int_parameter(action.parameters, "modifier", 0)
            + actor.equipment_spell_bonus
            + actor.npc_spell_check_bonus
            + self._int_parameter(action.parameters, "_spell_skill_check_modifier", 0),
            target=target_name,
            reason=action.parameters.get("reasoning", ""),
        )
        resource_loss = None
        resource_gain = None
        rules_text = f"检定 {roll.total} 对抗难度等级 {roll.target_number}: {'成功' if roll.success else '失败'}。"
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
            loss_amount = (
                int(target.mp * definition.resource_fraction_loss)
                if definition.resource_fraction_loss > 0
                else max(0, roll.high_roll + definition.fixed_damage)
            )
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

    def _resolve_spell_mp_damage_multi(
        self,
        action: Action,
        definition,
        target_names: list[str],
        mp_change: ResourceChange,
    ) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        target_numbers = self._target_numbers_for_targets(
            action.parameters,
            target_names,
            definition.defense_type,
        )
        roll = self.rules_engine.roll_check(
            actor=actor,
            attributes=self._spell_check_attributes(actor, action, definition),
            target_number=min(target_numbers.values()),
            modifier=self._int_parameter(action.parameters, "modifier", 0)
            + actor.equipment_spell_bonus
            + actor.npc_spell_check_bonus
            + self._int_parameter(
                action.parameters,
                "_spell_skill_check_modifier",
                0,
            ),
            target="、".join(target_names),
            reason=action.parameters.get("reasoning", ""),
        )
        if roll.critical_success:
            hit_targets = list(target_names)
        elif roll.fumble:
            hit_targets = []
        else:
            hit_targets = [
                name
                for name in target_names
                if roll.total >= target_numbers[name]
            ]
        roll.success = bool(hit_targets)
        payload: dict[str, object] = {
            "roll": roll,
            "resource_change": mp_change,
            "spell_name": definition.name,
            "target_numbers": target_numbers,
            "hit_targets": hit_targets,
            "target_resource_changes": [],
        }
        rules_text = (
            f"施法检定 {roll.total}: 命中 "
            f"{len(hit_targets)}/{len(target_names)} 个目标。"
        )
        if roll.critical_success:
            rules_text += " 触发大成功，获得 1 次机会。"
            trigger_results = self.trigger_manager.on_critical_success(actor.name)
            self._append_trigger_results(payload, trigger_results)
            rules_text += self._trigger_rules_text(trigger_results)
        if roll.fumble:
            before, after = self.character_manager.modify_resource(
                actor.name,
                "fabula_points",
                1,
            )
            payload["fabula_gain"] = ResourceChange(
                target=actor.name,
                resource="fabula_points",
                amount=1,
                before=before,
                after=after,
                reason="大失败获得 1 点物语点。",
            )
            rules_text += (
                " 触发大失败，对手获得 1 次机会，"
                "且掷骰角色获得 1 点物语点。"
            )
            trigger_results = self.trigger_manager.on_fumble(actor.name)
            self._append_trigger_results(payload, trigger_results)
            rules_text += self._trigger_rules_text(trigger_results)

        total_loss = 0
        for target_name in hit_targets:
            target = self.character_manager.get(target_name)
            loss_amount = (
                int(target.mp * definition.resource_fraction_loss)
                if definition.resource_fraction_loss > 0
                else max(0, roll.high_roll + definition.fixed_damage)
            )
            before, after = self.character_manager.modify_resource(
                target_name,
                "mp",
                -loss_amount,
            )
            actual_loss = before - after
            total_loss += actual_loss
            payload["target_resource_changes"].append(
                ResourceChange(
                    target=target_name,
                    resource="mp",
                    amount=-actual_loss,
                    before=before,
                    after=after,
                    reason=f"【{definition.name}】削减了目标的 MP。",
                )
            )
            rules_text += f" {target_name} 失去 {actual_loss} 点 MP。"
        roll.damage = total_loss
        roll.damage_type = definition.damage_type
        return ActionResolution(
            action=action,
            rules_text=rules_text,
            payload=payload,
        )

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
        cleared_status = None
        if definition.clear_selected_status:
            selected_statuses = self._selected_statuses(action, definition)
            if selected_statuses:
                selected_status = selected_statuses[0]
                if self.conflict_manager.remove_status(
                    target_name,
                    selected_status,
                ):
                    cleared_status = selected_status
        clear_text = (
            f"，并解除{self._status_name(cleared_status)}"
            if cleared_status is not None
            else ""
        )
        return ActionResolution(
            action=action,
            rules_text=(
                f"{target_name} 受到【{definition.name}】影响，规则恢复量 "
                f"{amount} 点 HP；HP {before}->{after}，实际恢复 "
                f"{after - before} 点{clear_text}。"
            ),
            payload={
                "resource_change": mp_change,
                "spell_name": definition.name,
                "healing_change": change,
                "status_cleared": (
                    cleared_status.value
                    if cleared_status is not None
                    else None
                ),
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
        elif definition.name in {"舔舐伤口", "弹弹舞"}:
            if actor.level >= 60:
                amount = 50
            elif actor.level >= 40:
                amount = 40
            elif actor.level >= 20:
                amount = 30
            else:
                amount = definition.fixed_damage
        else:
            amount = definition.fixed_damage
        return (
            amount
            + actor.equipment_healing_bonus
            + self._int_parameter(
                action.parameters,
                "_gadget_healing_bonus",
                0,
            )
        )

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

    def _resolve_spell_checked_timed_effect(
        self,
        action: Action,
        definition,
        target_names: list[str],
        mp_change: ResourceChange,
    ) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        target_numbers = self._target_numbers_for_targets(
            action.parameters,
            target_names,
            definition.defense_type,
        )
        roll = self.rules_engine.roll_check(
            actor=actor,
            attributes=self._spell_check_attributes(
                actor,
                action,
                definition,
            ),
            target_number=min(target_numbers.values()),
            modifier=self._int_parameter(action.parameters, "modifier", 0)
            + actor.equipment_spell_bonus
            + actor.npc_spell_check_bonus
            + self._int_parameter(
                action.parameters,
                "_spell_skill_check_modifier",
                0,
            )
            + self._consume_next_check_bonus(actor.name),
            target="、".join(target_names),
            reason=action.parameters.get("reasoning", ""),
        )
        self._remember_roll(roll)
        if roll.critical_success:
            hit_targets = list(target_names)
        elif roll.fumble:
            hit_targets = []
        else:
            hit_targets = [
                target_name
                for target_name in target_names
                if roll.total >= target_numbers[target_name]
            ]
        roll.success = bool(hit_targets)
        effects = [
            self._register_spell_effect(
                actor.name,
                target_name,
                action,
                definition,
            )
            for target_name in hit_targets
        ]
        payload: dict[str, object] = {
            "roll": roll,
            "resource_change": mp_change,
            "spell_name": definition.name,
            "target_numbers": target_numbers,
            "hit_targets": hit_targets,
            "spell_effect": (
                effects[0]
                if len(effects) == 1
                else effects
            ),
        }
        rules_text = (
            f"{actor.name}施放【{definition.name}】；施法检定{roll.total}，"
            f"命中{len(hit_targets)}/{len(target_names)}个目标。"
        )
        if effects:
            rules_text += " " + self._spell_effect_group_rules_text(
                actor.name,
                hit_targets,
                definition,
                effects,
            )
        return ActionResolution(
            action=action,
            rules_text=rules_text,
            payload=payload,
        )

    def _resolve_spell_immediate_attack(
        self,
        action: Action,
        definition,
        target_name: str,
        mp_change: ResourceChange,
    ) -> ActionResolution:
        actor_name = str(action.parameters["actor"])
        legal_targets = self._immediate_attack_targets(target_name)
        requested_target = str(
            action.parameters.get("attack_target") or ""
        ).strip()
        if requested_target:
            if requested_target not in legal_targets:
                raise ValueError(
                    f"【{target_name}】不能以【{requested_target}】作为这次顺势攻击的目标。"
                )
            attack = self.resolve(
                Action(
                    ActionType.ATTACK,
                    {
                        "actor": target_name,
                        "target": requested_target,
                        "_reaction_followup": True,
                        "_enforce_turn_order": False,
                        "opportunity_action": True,
                        "reasoning": f"【{definition.name}】提供的顺势攻击。",
                    },
                )
            )
            return ActionResolution(
                action=action,
                rules_text=(
                    f"{actor_name}施放【{definition.name}】，"
                    f"{target_name}立刻攻击{requested_target}。{attack.rules_text}"
                ),
                payload={
                    "resource_change": mp_change,
                    "spell_name": definition.name,
                    "immediate_attack": attack,
                    "immediate_attack_actor": target_name,
                    "immediate_attack_target": requested_target,
                    "decision_windows": (
                        self.decision_window_manager.public_summary()
                    ),
                },
            )

        if not legal_targets:
            return ActionResolution(
                action=action,
                rules_text=(
                    f"{actor_name}施放【{definition.name}】，但"
                    f"{target_name}当前没有合法的顺势攻击目标。"
                ),
                payload={
                    "resource_change": mp_change,
                    "spell_name": definition.name,
                    "immediate_attack_unavailable": True,
                },
            )

        scope_kind = (
            "conflict"
            if self.conflict_manager.state.active
            else "scene"
        )
        scope_id = (
            self.conflict_manager.state.scene_name
            if self.conflict_manager.state.active
            else "current"
        )
        (
            deferred_actor,
            deferred_serial,
            source_action_type,
            resume_point,
        ) = self._deferred_turn_lineage_for_action(action)
        window_payload: dict[str, object] = {
            "spell_name": definition.name,
            "caster": actor_name,
            "legal_targets": list(legal_targets),
        }
        if deferred_actor and deferred_serial > 0 and resume_point:
            window_payload.update(
                {
                    "deferred_turn_actor": deferred_actor,
                    "deferred_turn_serial": deferred_serial,
                    "source_action_type": source_action_type,
                    "resume_point": resume_point,
                }
            )
        window = self.decision_window_manager.create(
            kind="immediate_attack",
            owner=target_name,
            prompt=(
                f"【{definition.name}】让【{target_name}】可以立刻使用装备武器"
                "进行一次顺势攻击。请选择目标，或放弃这次攻击。"
            ),
            options=[
                {
                    "choice": "attack",
                    "target": candidate,
                    "label": f"攻击{candidate}",
                }
                for candidate in legal_targets
            ]
            + [{"choice": "decline", "label": "不攻击"}],
            scope_kind=scope_kind,
            scope_id=scope_id,
            blocking=True,
            action_type=ActionType.ATTACK.value,
            resume_point=resume_point,
            payload=window_payload,
            dedupe_key=(
                f"immediate_attack:{scope_kind}:{scope_id}:"
                f"{target_name}:{definition.name}"
            ),
        )
        return ActionResolution(
            action=action,
            rules_text=window.prompt,
            payload={
                "resource_change": mp_change,
                "spell_name": definition.name,
                "immediate_attack_pending": True,
                "decision_window_id": window.window_id,
                "decision_windows": (
                    self.decision_window_manager.public_summary()
                ),
            },
        )

    def _immediate_attack_targets(self, actor_name: str) -> list[str]:
        if not self.character_manager.exists(actor_name):
            return []
        actor = self.character_manager.get(actor_name)
        if self.conflict_manager.state.active:
            candidates = list(
                dict.fromkeys(self.conflict_manager.state.turn_order)
            )
        else:
            candidates = [
                character.name
                for character in self.character_manager.all()
            ]
        actor_is_pc = "pc" in actor.traits
        return [
            name
            for name in candidates
            if name != actor_name
            and self.character_manager.exists(name)
            and self.character_manager.get(name).hp > 0
            and (
                ("pc" in self.character_manager.get(name).traits)
                != actor_is_pc
            )
        ]

    def _resolve_spell_status_apply(
        self,
        action: Action,
        definition,
        target_name: str,
        mp_change: ResourceChange,
    ) -> ActionResolution:
        statuses = self._selected_statuses(action, definition)
        if not statuses and definition.status_effect is not None:
            statuses = [definition.status_effect]
        if definition.automatic_effect:
            applied = [
                status
                for status in statuses
                if self.conflict_manager.apply_status(target_name, status)
            ]
            labels = "、".join(self._status_name(status) for status in applied)
            return ActionResolution(
                action=action,
                rules_text=(
                    f"{action.parameters['actor']}施放【{definition.name}】，"
                    f"{target_name}{'被施加' + labels if labels else '没有受到新的异常状态'}。"
                ),
                payload={
                    "resource_change": mp_change,
                    "spell_name": definition.name,
                    "status_effects": statuses,
                    "status_applied": bool(applied),
                    "status_applied_effects": applied,
                    "status_applied_targets": [target_name] if applied else [],
                },
            )
        resolution = self._resolve_roll(
            Action(
                action_type=ActionType.REQUEST_ROLL,
                parameters={
                    **action.parameters,
                    "target": target_name,
                    "attributes": self._spell_check_attributes(
                        self.character_manager.get(
                            action.parameters["actor"]
                        ),
                        action,
                        definition,
                    ),
                    "target_number": self._target_number_or_defense(action.parameters, target_name, definition.defense_type),
                    "modifier": self._int_parameter(action.parameters, "modifier", 0)
                    + self.character_manager.get(action.parameters["actor"]).equipment_spell_bonus
                    + self.character_manager.get(action.parameters["actor"]).npc_spell_check_bonus
                    + self._int_parameter(
                        action.parameters,
                        "_spell_skill_check_modifier",
                        0,
                    ),
                    "non_damage": True,
                    "spell_name": definition.name,
                },
            )
        )
        resolution.action = action
        resolution.payload["resource_change"] = mp_change
        resolution.payload["spell_name"] = definition.name
        resolution.payload["status_effects"] = statuses
        if resolution.payload["roll"].success:
            applied = [
                status
                for status in statuses
                if self.conflict_manager.apply_status(target_name, status)
            ]
            resolution.payload["status_applied"] = bool(applied)
            resolution.payload["status_applied_effects"] = applied
            if len(statuses) == 1:
                resolution.payload["hinder_status"] = statuses[0]
            if applied:
                resolution.rules_text += (
                    f" {target_name} 被施加"
                    + "、".join(self._status_name(status) for status in applied)
                    + "。"
                )
        return resolution

    def _resolve_spell_status_apply_multi(
        self,
        action: Action,
        definition,
        target_names: list[str],
        mp_change: ResourceChange,
    ) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        statuses = self._selected_statuses(action, definition)
        if not statuses and definition.status_effect is not None:
            statuses = [definition.status_effect]
        if definition.automatic_effect:
            applied_targets: dict[str, list[StatusEffect]] = {}
            for target_name in target_names:
                applied = [
                    status
                    for status in statuses
                    if self.conflict_manager.apply_status(target_name, status)
                ]
                if applied:
                    applied_targets[target_name] = applied
            status_text = "、".join(self._status_name(status) for status in statuses)
            return ActionResolution(
                action=action,
                rules_text=(
                    f"{actor.name}施放【{definition.name}】，对"
                    f"{'、'.join(target_names)}施加{status_text}。"
                ),
                payload={
                    "resource_change": mp_change,
                    "spell_name": definition.name,
                    "status_effects": statuses,
                    "status_applied_targets": list(applied_targets),
                    "status_applied_by_target": applied_targets,
                },
            )
        target_numbers = self._target_numbers_for_targets(action.parameters, target_names, definition.defense_type)
        roll = self.rules_engine.roll_check(
            actor=actor,
            attributes=self._spell_check_attributes(
                actor,
                action,
                definition,
            ),
            target_number=min(target_numbers.values()),
            modifier=self._int_parameter(action.parameters, "modifier", 0)
            + actor.equipment_spell_bonus
            + actor.npc_spell_check_bonus
            + self._int_parameter(
                action.parameters,
                "_spell_skill_check_modifier",
                0,
            )
            + self._consume_next_check_bonus(actor.name),
            target="、".join(target_names),
            reason=action.parameters.get("reasoning", ""),
        )
        hit_targets = []
        if roll.critical_success:
            hit_targets = list(target_names)
        elif not roll.fumble:
            hit_targets = [name for name in target_names if roll.total >= target_numbers[name]]
        roll.success = bool(hit_targets)
        applied_targets: dict[str, list[StatusEffect]] = {}
        for target_name in hit_targets:
            applied = [
                status
                for status in statuses
                if self.conflict_manager.apply_status(target_name, status)
            ]
            if applied:
                applied_targets[target_name] = applied
        status_text = "、".join(self._status_name(status) for status in statuses)
        rules_text = (
            f"施法检定 {roll.total}: 命中 {len(hit_targets)}/{len(target_names)} 个目标。"
            f" {status_text}影响：{('、'.join(applied_targets) if applied_targets else '无新增目标')}。"
        )
        payload: dict[str, object] = {
            "roll": roll,
            "resource_change": mp_change,
            "spell_name": definition.name,
            "status_effects": statuses,
            "target_numbers": target_numbers,
            "hit_targets": hit_targets,
            "status_applied_targets": list(applied_targets),
            "status_applied_by_target": applied_targets,
        }
        if len(statuses) == 1:
            payload["hinder_status"] = statuses[0]
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
        target_number = self._target_number_parameter(action.parameters, default=7)
        outcome = self.rules_engine.roll_check(
            actor=actor,
            attributes=action.parameters.get("attributes", ["INS", "INS"]),
            target_number=target_number,
            modifier=self._int_parameter(action.parameters, "modifier", 0),
            target=target_name,
            reason=action.parameters.get("reasoning", ""),
        )
        information: list[str] = []
        success_information = (
            action.parameters.get("success_information")
            or action.parameters.get("clues")
            or action.parameters.get("information")
            or []
        )
        high_success_information = action.parameters.get("high_success_information") or []
        if isinstance(success_information, str):
            success_information = [success_information]
        if isinstance(high_success_information, str):
            high_success_information = [high_success_information]
        if outcome.success:
            information.extend(
                str(item).strip()
                for item in success_information
                if str(item).strip()
            )
        if outcome.success and (outcome.critical_success or outcome.total >= 13):
            information.extend(
                str(item).strip()
                for item in high_success_information
                if str(item).strip()
            )

        if information:
            joined = "；".join(information)
            self.world_state.add_memory(f"{actor.name} 调查场景线索 {target_name}：{joined}")
            self.world_state.remember_subject_fact(target_name, joined)

        rules_text = f"调查检定 {outcome.total}: {'成功' if outcome.total >= target_number else '失败'}。"
        payload: dict[str, object] = {"roll": outcome, "information": information, "scene_object": target_name}
        if information:
            rules_text += " 获取了场景线索。"
        prospective_clock_name = str(
            action.parameters.get("establish_threat_clock_name")
            or action.parameters.get("threat_clock_name")
            or ""
        ).strip(" ：:「」『』【】[]")
        clock_existed_before = bool(
            prospective_clock_name
            and self.clock_manager.exists(prospective_clock_name)
        )
        clock_change = self._scene_investigation_clock_change(action, outcome)
        if clock_change is not None:
            payload["clock_change"] = clock_change
            if not clock_existed_before and self.clock_manager.exists(clock_change.clock_name):
                payload["clock_created"] = True
            if clock_change.delta:
                rules_text += f" 威胁命刻 [{clock_change.clock_name}] 推进 {abs(clock_change.delta)} 格。"
            else:
                rules_text += f" 公开威胁命刻 [{clock_change.clock_name}]。"
        return ActionResolution(action=action, rules_text=rules_text, payload=payload)

    def _scene_investigation_clock_change(self, action: Action, outcome: RollOutcome) -> ClockChange | None:
        clock_name = str(
            action.parameters.get("establish_threat_clock_name")
            or action.parameters.get("threat_clock_name")
            or ""
        ).strip(" ：:「」『』【】[]")
        if not clock_name:
            return None
        if self.clock_manager.is_retired(clock_name):
            # The threat has already happened.  Repeating its name in a later
            # investigation may reveal consequences, but must never recreate
            # the same countdown at 0/N.
            return None
        max_segments = self._int_parameter(
            action.parameters,
            "establish_threat_clock_segments",
            self._int_parameter(action.parameters, "threat_clock_max_segments", 6, minimum=1),
            minimum=1,
        )
        stakes = str(
            action.parameters.get("establish_threat_clock_stakes")
            or action.parameters.get("threat_clock_stakes")
            or "填满后威胁降临。"
        )
        if not self.clock_manager.exists(clock_name):
            self.clock_manager.add(
                Clock(
                    name=clock_name,
                    max_segments=max_segments,
                    current=0,
                    clock_type="threat",
                    stakes=stakes,
                    auto_advance=str(
                        action.parameters.get("establish_threat_clock_auto_advance")
                        or action.parameters.get("threat_clock_auto_advance")
                        or action.parameters.get("auto_advance")
                        or ""
                    ),
                )
            )
        clock = self.clock_manager.get(clock_name)
        delta = 0
        establishing_clock = bool(action.parameters.get("establish_threat_clock_name"))
        if establishing_clock and clock.current == 0:
            delta = self._int_parameter(action.parameters, "establish_threat_clock_delta", 0, minimum=0)
        if not outcome.success:
            if "threat_clock_delta" in action.parameters:
                delta += self._int_parameter(action.parameters, "threat_clock_delta", 1, minimum=0)
            elif bool(
                action.parameters.get("advance_threat_on_failure")
                or action.parameters.get("threat_clock_advance_on_failure")
                or action.parameters.get("advance_established_threat_on_failure")
                or action.parameters.get("establish_threat_clock_advance_on_failure")
            ):
                delta += self.rules_engine.threat_clock_segments_from_roll(
                    outcome,
                    spend_fumble_opportunity=bool(action.parameters.get("spend_fumble_opportunity_on_threat_clock", False)),
                )
        if not establishing_clock and delta == 0 and not action.parameters.get("reveal_threat_clock_state"):
            return None
        before, after = self.clock_manager.advance(clock_name, delta)
        actual_delta = after - before
        return ClockChange(
            clock_name=clock.name,
            before=before,
            after=after,
            delta=actual_delta,
            max_segments=clock.max_segments,
            reason="GM 判断线索显示威胁正在逼近。",
            clock_type=clock.clock_type,
            stakes=clock.stakes,
            completion_consequence=clock.completion_consequence,
        )

    def _resolve_objective(self, action: Action) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        clock_name = action.parameters.get("clock_name") or action.parameters.get("target") or "当前目标命刻"
        target_number = self._target_number_parameter(action.parameters, default=10)
        if target_number < 7:
            raise ValueError("目标行动缺少有效难度等级：难度等级必须至少为 7，不能使用命刻格数代替。")
        generic_clock_names = {"当前命刻", "当前目标命刻", "当前目标", "当前线索", "场景目标"}
        should_track_objective = bool(clock_name and str(clock_name).strip() not in generic_clock_names)
        if should_track_objective:
            self._ensure_clock_exists(action, clock_name, default_clock_type="objective")
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
        if clock_change is None and should_track_objective and self.clock_manager.exists(clock_name):
            clock = self.clock_manager.get(clock_name)
            resolution.payload["clock_state"] = {
                "clock_name": clock.name,
                "current": clock.current,
                "max_segments": clock.max_segments,
                "clock_type": clock.clock_type,
            }
            resolution.rules_text += f" 命刻 [{clock.name}] 仍为 {clock.current}/{clock.max_segments}。"
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
            "摧心重击": self._resolve_heartbreaker,
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
            "缴械雄辩": self._resolve_disarming_rhetoric,
            "不出所料！": self._resolve_predictable,
            "影逝": self._resolve_vanish,
            "重燃希望": self._resolve_hope,
            "火山": self._resolve_volcano,
            "彗星": self._resolve_comet,
            "弹幕射击": self._resolve_barrage,
            "利刃风暴": self._resolve_bladestorm,
            "契约与召唤": self._resolve_bind_and_summon,
            "幸运七": self._resolve_lucky_seven,
            "忠诚伙伴": self._resolve_loyal_companion,
        }
        if skill_name in handlers:
            return handlers[skill_name](action, skill_name)
        return self._resolve_pending_skill(action, skill_name)

    def _resolve_loyal_companion(
        self,
        action: Action,
        skill_name: str,
    ) -> ActionResolution:
        if self.loyal_companion_manager is None:
            raise ValueError("忠诚伙伴规则组件尚未接入当前战役。")
        owner = self.character_manager.get(action.parameters["actor"])
        companion = self.loyal_companion_manager.require_available(owner.name)
        self.loyal_companion_manager.assert_command_available(owner.name)
        raw_action_type = str(
            action.parameters.get("companion_action_type")
            or action.parameters.get("command_action_type")
            or action.parameters.get("subaction")
            or ""
        ).strip()
        aliases = {
            "攻击": ActionType.ATTACK,
            "Attack": ActionType.ATTACK,
            "attack": ActionType.ATTACK,
            "施法": ActionType.SPELL,
            "Spell": ActionType.SPELL,
            "spell": ActionType.SPELL,
            "防御": ActionType.GUARD,
            "Guard": ActionType.GUARD,
            "guard": ActionType.GUARD,
            "妨碍": ActionType.HINDER,
            "Hinder": ActionType.HINDER,
            "hinder": ActionType.HINDER,
            "调查": ActionType.INVESTIGATE,
            "Investigate": ActionType.INVESTIGATE,
            "investigate": ActionType.INVESTIGATE,
            "推进目标": ActionType.OBJECTIVE,
            "Objective": ActionType.OBJECTIVE,
            "objective": ActionType.OBJECTIVE,
        }
        companion_action_type = aliases.get(raw_action_type)
        if companion_action_type is None:
            raise ValueError(
                "使用【忠诚伙伴】需要说明伙伴执行攻击、施法、防御、妨碍、"
                "调查或推进目标中的哪一种行动。"
            )
        parameters = {
            key: deepcopy(value)
            for key, value in action.parameters.items()
            if key
            not in {
                "skill_name",
                "companion_action_type",
                "command_action_type",
                "subaction",
                "attack_name",
            }
        }
        parameters.update(
            {
                "actor": companion.name,
                "_reaction_followup": "忠诚伙伴",
                "_enforce_turn_order": False,
                "_decision_owner": owner.name,
                "_fate_owner": owner.name,
            }
        )
        previous_attack: dict[str, object] | None = None
        if companion_action_type == ActionType.ATTACK:
            profile = self.loyal_companion_manager.attack_profile(
                owner.name,
                str(action.parameters.get("attack_name") or ""),
            )
            previous_attack = self.loyal_companion_manager.apply_attack_profile(
                companion,
                profile,
            )
            parameters["attributes"] = list(profile["attributes"])
            parameters["damage_type"] = str(profile["damage_type"])
            parameters["weapon_damage"] = int(profile["weapon_damage"])
            parameters["is_melee"] = str(profile["range"]) != "ranged"
            if int(profile["multi_attack"]) > 1:
                parameters["multi_attack"] = int(profile["multi_attack"])
        try:
            nested = self.resolve(Action(companion_action_type, parameters))
        finally:
            if previous_attack is not None:
                self.loyal_companion_manager.restore_attack_profile(
                    companion,
                    previous_attack,
                )
        self.loyal_companion_manager.mark_command_used(owner.name)
        return ActionResolution(
            action=action,
            rules_text=(
                f"{owner.name}用自己的行动指挥{companion.name}。"
                f"{nested.rules_text}"
            ),
            payload={
                "skill_name": skill_name,
                "companion": companion.name,
                "companion_action_type": companion_action_type.value,
                "nested_resolution": nested,
                "roll": nested.payload.get("roll"),
                "post_check_windows": nested.payload.get(
                    "post_check_windows",
                    [],
                ),
                "decision_windows": nested.payload.get(
                    "decision_windows",
                    [],
                ),
                "conflict_event": nested.payload.get("conflict_event"),
                "_already_finalized": True,
            },
        )

    def _resolve_lucky_seven(self, action: Action, skill_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        cooldown_key = "scene:skill:幸运七"
        if cooldown_key in actor.trigger_cooldowns:
            raise ValueError("【幸运七】在本场景已经使用过一次。")
        outcome = self.post_check_state.roll_for(actor.name)
        transaction = self.pending_check_transactions.get(actor.name)
        if outcome is None or transaction is None:
            raise ValueError(f"{actor.name} 没有可由【幸运七】改写的待处理检定。")
        raw_index = action.parameters.get("die_index") or action.parameters.get("replace_die")
        if raw_index in {None, ""}:
            return ActionResolution(
                action=action,
                rules_text=(
                    f"【幸运七】当前幸运数字是 {actor.lucky_number}。"
                    "请选择替换第一枚还是第二枚骰子。"
                ),
                payload={
                    "skill_name": skill_name,
                    "skill_parameter_required": True,
                    "required_parameter": "die_index",
                    "dice": list(outcome.dice),
                    "lucky_number": actor.lucky_number,
                },
            )
        try:
            selected = int(raw_index)
        except (TypeError, ValueError) as exc:
            raise ValueError("【幸运七】需要选择第一枚或第二枚骰子。") from exc
        index = selected - 1 if selected in {1, 2} else selected
        dice = list(outcome.dice)
        if index < 0 or index >= len(dice):
            raise ValueError("【幸运七】需要选择第一枚或第二枚骰子。")

        old_value = int(dice[index][1])
        dice[index] = (int(dice[index][0]), int(actor.lucky_number))
        adjusted = self.rules_engine.recompute_outcome(outcome, dice=dice)
        original = deepcopy(outcome)
        snapshot = transaction["snapshot"]
        original_action = deepcopy(transaction["action"])
        self.pending_check_transactions.pop(actor.name, None)
        self._restore_check_state(snapshot)
        restored_actor = self.character_manager.get(actor.name)
        restored_actor.lucky_number = old_value
        restored_actor.trigger_cooldowns.add(cooldown_key)
        self.rules_engine.force_next_check_outcome(adjusted)
        self._replaying_check_transaction = True
        self._check_transaction_candidate = None
        try:
            replayed = self.resolve(original_action)
        except Exception:
            self._restore_check_state(snapshot)
            raise
        finally:
            self._replaying_check_transaction = False
            self.rules_engine.clear_forced_check_outcomes()

        replayed.action = action
        replayed.rules_text = (
            f"{actor.name}发动【幸运七】，以 {int(dice[index][1])} 替换第 {index + 1} 枚骰子的 {old_value}；"
            f"幸运数字变为 {old_value}，检定从 {original.total} 变为 {adjusted.total}。 "
            f"{replayed.rules_text}"
        )
        replayed.payload.update(
            {
                "skill_name": skill_name,
                "before_roll": original,
                "roll": replayed.payload.get("roll", adjusted),
                "lucky_number_before": int(dice[index][1]),
                "lucky_number_after": old_value,
                "check_transaction_replayed": True,
                "_already_finalized": True,
            }
        )
        replayed.payload["post_check_windows"] = [
            window
            for window in (replayed.payload.get("post_check_windows") or [])
            if not (
                isinstance(window, dict)
                and window.get("kind") == "skill_judgement"
                and window.get("label") == "幸运七"
            )
        ]
        self.decision_window_manager.cancel_matching(
            kind="skill_judgement",
            owner=actor.name,
            reason="lucky_seven_resolved",
        )
        self.post_check_state.discard_actor(actor.name)
        return replayed

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
        actor.trigger_cooldowns.add(
            f"scene:arcanum_summoned_turn:{int(getattr(self.conflict_manager.state, 'turn_serial', 0) or 0)}"
        )
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
        current_turn_serial = int(getattr(self.conflict_manager.state, "turn_serial", 0) or 0)
        summoned_this_turn = (
            f"scene:arcanum_summoned_turn:{current_turn_serial}" in actor.trigger_cooldowns
        )
        actor.active_arcanum = ""
        lifecycle = self.skill_lifecycle.trigger(
            "arcanum_dismissed",
            actor,
            active_dismissal=True,
            summoned_this_turn=summoned_this_turn,
            magic_weapon_equipped=self.skill_trigger_manager.has_magic_weapon(actor),
            arcanum=arcanum,
        )
        self._capture_skill_lifecycle(lifecycle)
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
                    self.conflict_manager.resolve_zero_hp(
                        target_name,
                        source_actor=actor_name,
                    )
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
            event = self.conflict_manager.resolve_zero_hp(
                actor.name,
                source_actor=self._zero_hp_source_actor(
                    action,
                    actor.name,
                ),
            )
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
            "摧心重击消耗当前 HP 的一半。",
        )
        resolution.rules_text = f"{actor.name} 消耗 {hp_cost} HP 发动【{skill_name}】，羁绊强度 {bond_strength}。{resolution.rules_text}"
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
        actor_roll = self._prepare_opposed_actor_roll(opposed)
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
                f"{actor.name} 发动【{skill_name}】：对抗检定 {opposed.left_roll.total} 对抗 {opposed.right_roll.total}，"
                f"{'成功' if success else '失败'}。"
            ),
            payload={
                "skill_name": skill_name,
                "resource_change": mp_change,
                "roll": actor_roll,
                "check_roll_sequence": [opposed.left_roll, opposed.right_roll],
                "check_roll_index": 0,
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
        actor_roll = self._prepare_opposed_actor_roll(opposed)
        payload: dict[str, object] = {
            "skill_name": skill_name,
            "resource_change": mp_change,
            "roll": actor_roll,
            "check_roll_sequence": [opposed.left_roll, opposed.right_roll],
            "check_roll_index": 0,
            "opposed_check": opposed,
        }
        rules_text = (
            f"{actor.name} 发动【{skill_name}】：对抗检定 {opposed.left_roll.total} 对抗 {opposed.right_roll.total}，"
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
        resolution = ActionResolution(action=action, rules_text=rules_text, payload=payload)
        return self._apply_reprise(action, resolution, self._resolve_condemn, skill_name)

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
        resolution = ActionResolution(
            action=action,
            rules_text=f"{actor.name} 发动【{skill_name}】，{target_name} 恢复 {after - before} HP，{attribute} 临时提升 1 阶。",
            payload={
                "skill_name": skill_name,
                "resource_change": mp_change,
                "healing_change": ResourceChange(target_name, "hp", after - before, before, after, "鼓舞恢复 HP。"),
                "skill_effect": effect,
            },
        )
        return self._apply_reprise(action, resolution, self._resolve_encourage, skill_name)

    def _apply_reprise(self, action: Action, resolution: ActionResolution, handler, skill_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        if not bool(action.parameters.get("repeat")) or not has_skill_name(actor.hero_skills, "复诵"):
            return resolution
        repeat_parameters = dict(action.parameters)
        repeat_parameters["repeat"] = False
        repeat_parameters["target"] = action.parameters.get("repeat_target", action.parameters.get("target", actor.name))
        if action.parameters.get("repeat_status_effect"):
            repeat_parameters["status_effect"] = action.parameters["repeat_status_effect"]
        if action.parameters.get("repeat_chosen_attribute"):
            repeat_parameters["chosen_attribute"] = action.parameters["repeat_chosen_attribute"]
        repeated = handler(Action(ActionType.SKILL, repeat_parameters), skill_name)
        resolution.rules_text += f" 【复诵】{repeated.rules_text}"
        resolution.payload["reprise"] = repeated.payload
        return resolution

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
                before, after = self._apply_damage_from(actor.name, target_name, damage_amount)
                effects.append(f"{target_name} 缓慢失去 {before - after} HP")
                if after == 0:
                    self.conflict_manager.resolve_zero_hp(
                        target_name,
                        source_actor=self._zero_hp_source_actor(
                            action,
                            actor.name,
                        ),
                    )
            else:
                raise ValueError(f"未知的【{skill_name}】选项：{option}")
        return ActionResolution(
            action=action,
            rules_text=f"{actor.name} 发动【{skill_name}】：{'；'.join(effects)}。",
            payload={"skill_name": skill_name, "resource_change": mp_change, "effects": effects},
        )

    def _resolve_soul_steal(self, action: Action, skill_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        target_names = self._attack_target_names(action) or [self._target_name(action, "当前灵魂回路")]
        if len(target_names) > 1 and not has_skill_name(actor.hero_skills, "洗劫一空"):
            raise ValueError("【窃取灵魂】通常只能选择一个目标；【洗劫一空】可将其扩展为任意数量。")
        missing = [name for name in target_names if not self.character_manager.exists(name)]
        if missing:
            target_name = missing[0]
            return self._resolve_scene_target_skill(
                action,
                skill_name,
                actor,
                target_name,
                summary=f"{actor.name} 以【{skill_name}】触碰并读取场景中的灵魂痕迹【{target_name}】。",
            )
        targets = [self.character_manager.get(name) for name in target_names]
        available_targets = [
            target
            for target in targets
            if "skill:soul_stolen" not in target.permanent_trigger_keys
        ]
        if not available_targets:
            return ActionResolution(
                action=action,
                rules_text="这些生物的灵魂在本场冲突中已经被成功窃取过。",
                payload={"skill_name": skill_name, "already_stolen": target_names},
            )
        rank = self._skill_rank(actor, skill_name)
        target_numbers = {
            target.name: self.character_manager.effective_defense(target.name, "magic")
            for target in available_targets
        }
        roll = self.rules_engine.roll_check(
            actor=actor,
            attributes=["DEX", "WLP"],
            target_number=min(target_numbers.values()),
            modifier=rank,
            target="、".join(target_numbers),
            reason=skill_name,
        )
        hit_targets = [
            target
            for target in available_targets
            if roll.critical_success or (not roll.fumble and roll.total >= target_numbers[target.name])
        ]
        roll.success = bool(hit_targets)
        payload: dict[str, object] = {
            "skill_name": skill_name,
            "roll": roll,
            "target_numbers": target_numbers,
            "hit_targets": [target.name for target in hit_targets],
            "resource_changes": [],
            "soul_treasures": [],
        }
        rules_text = f"{actor.name} 发动【{skill_name}】：检定 {roll.total}，成功影响 {len(hit_targets)}/{len(available_targets)} 个目标。"
        for target in hit_targets:
            target.permanent_trigger_keys.add("skill:soul_stolen")
            rank_type = self.conflict_manager.state.enemy_ranks.get(target.name, EnemyRank.SOLDIER)
            if rank_type == EnemyRank.SOLDIER:
                before, after = self._restore_inventory_points(actor.name, rank)
                change = ResourceChange(actor.name, "inventory_points", after - before, before, after, "窃取灵魂恢复 IP。")
                payload["resource_changes"].append(change)
                rules_text += f" 从 {target.name} 恢复 {after - before} 点物资点。"
            else:
                multiplier = 50 if self.conflict_manager.is_villain(target.name) else 30
                value = target.level * multiplier
                treasure = {"target": target.name, "max_value": value}
                payload["soul_treasures"].append(treasure)
                rules_text += f" 获得来自 {target.name} 的灵魂宝藏，价值上限 {value}Z。"
        if len(payload["resource_changes"]) == 1:
            payload["resource_change"] = payload["resource_changes"][0]
        if len(payload["soul_treasures"]) == 1:
            payload["soul_treasure"] = payload["soul_treasures"][0]
        return ActionResolution(action=action, rules_text=rules_text, payload=payload)

    @staticmethod
    def _prepare_opposed_actor_roll(opposed):
        """Expose the acting side of an opposed check to post-check rules."""

        roll = opposed.left_roll
        roll.target_number = int(opposed.right_roll.total)
        roll.margin = int(roll.total - opposed.right_roll.total)
        roll.success = opposed.winner == opposed.left
        return roll

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
        actor = self.character_manager.get(action.parameters["actor"])
        return self._resolve_control_attack(
            action,
            skill_name,
            selectable_statuses=(StatusEffect.SHAKEN, StatusEffect.SLOW),
            default_status=StatusEffect.SHAKEN,
            mp_loss_per_rank=10,
            is_melee=False,
            max_effects=2 if has_skill_name(actor.hero_skills, "完美瞄准") else 1,
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
        rank = self._skill_rank(actor, skill_name)
        assessments = action.parameters.get("assessments")
        if (
            not isinstance(assessments, list)
            or not assessments
            or len(assessments) > rank
        ):
            raise ValueError(
                f"【快速评估】必须选择 1 至 {rank} 项特质或相性评估。"
            )
        mp_cost = len(assessments) * 5
        if mp_cost <= 0 or mp_cost % 5 != 0 or mp_cost > rank * 5:
            raise ValueError("【快速评估】的 MP 消耗必须是 5 的倍数，且不超过 SL x 5。")
        mp_change = self._spend_mp_or_fail(action, actor.name, mp_cost, f"发动【{skill_name}】。")
        if isinstance(mp_change, ActionResolution):
            return mp_change
        reveals: list[str] = []
        revealed_traits: dict[str, set[str]] = {}
        internal_traits = {
            "pc",
            "npc",
            "enemy",
            "ally",
            "villain",
            "beast",
            "construct",
            "demon",
            "elemental",
            "humanoid",
            "monster",
            "plant",
            "undead",
            "玩家角色",
            "非玩家角色",
            "敌人",
            "盟友",
            "反派",
            "野兽",
            "构装体",
            "恶魔",
            "元素",
            "人型",
            "怪物",
            "植物",
            "不死族",
        }
        for raw in assessments:
            if not isinstance(raw, dict):
                raise ValueError("【快速评估】的评估项目格式无效。")
            target_name = str(raw.get("target") or "").strip()
            if (
                not target_name
                or not self.character_manager.exists(target_name)
                or (
                    self.conflict_manager.state.active
                    and target_name not in self.conflict_manager.state.turn_order
                )
            ):
                raise ValueError(
                    f"【{target_name or '未指定'}】不是当前可见的生物。"
                )
            target = self.character_manager.get(target_name)
            kind = str(raw.get("kind") or "").strip().lower()
            if kind == "trait":
                candidates = [
                    str(item).strip()
                    for item in target.traits
                    if str(item).strip()
                    and str(item).strip().lower() not in internal_traits
                ]
                requested = str(raw.get("trait") or "").strip()
                if requested:
                    if requested not in candidates:
                        raise ValueError(
                            f"【{target_name}】没有可揭示的真实特质【{requested}】。"
                        )
                    trait = requested
                else:
                    already = revealed_traits.setdefault(target_name, set())
                    trait = next(
                        (
                            candidate
                            for candidate in candidates
                            if candidate not in already
                        ),
                        "",
                    )
                if not trait:
                    raise ValueError(
                        f"【{target_name}】没有更多可由【快速评估】揭示的特质。"
                    )
                revealed_traits.setdefault(target_name, set()).add(trait)
                information = f"{target_name}的特质：【{trait}】"
            elif kind == "affinity":
                damage_type = str(raw.get("damage_type") or "").strip()
                if damage_type not in {
                    "physical",
                    "wind",
                    "lightning",
                    "dark",
                    "earth",
                    "fire",
                    "ice",
                    "light",
                    "poison",
                }:
                    raise ValueError("【快速评估】指定了无效的伤害类型。")
                affinity = self.character_manager.effective_affinity(
                    target_name,
                    damage_type,
                )
                information = (
                    f"{target_name}的{self._damage_type_text(damage_type)}相性："
                    f"{self._affinity_label(affinity)}"
                )
            else:
                raise ValueError(
                    "【快速评估】每项只能选择揭示特质或伤害相性。"
                )
            reveals.append(information)
            self.world_state.remember_subject_fact(
                target_name,
                f"【快速评估】揭示：{information}",
            )
        return ActionResolution(
            action=action,
            rules_text=f"{actor.name} 发动【{skill_name}】，获得情报：{'；'.join(reveals)}。",
            payload={"skill_name": skill_name, "resource_change": mp_change, "information": reveals},
        )

    def _resolve_unexpected_ally(self, action: Action, skill_name: str) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        ally = self._target_name(action, "意外盟友")
        fact = action.parameters.get("fact", f"{ally} 愿意在合理范围内帮助 {actor.name} 与小队。")
        violation = self.world_state.iconic_protection_violation(fact)
        if violation:
            self.world_state.record_transparency_audit(
                "iconic_element_protection",
                False,
                violation,
                severity="warning",
                source="ActionInterceptor._resolve_unexpected_ally",
            )
            return ActionResolution(
                action=action,
                rules_text=violation,
                payload={
                    "skill_name": skill_name,
                    "story_change_failed": True,
                    "iconic_violation": True,
                    "fact": fact,
                    "reason": violation,
                },
            )
        fabula_change = self._spend_fabula_or_fail(action, actor.name, 1, f"发动【{skill_name}】。")
        if isinstance(fabula_change, ActionResolution):
            return fabula_change
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
            raise ValueError("【缴械雄辩】只能选择士兵级别生物。")
        if StatusEffect.SHAKEN not in target.statuses and not target.in_crisis:
            raise ValueError("【缴械雄辩】要求目标处于动摇或危机状态。")
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
        if target_name in self.conflict_manager.state.sacrifices:
            raise ValueError("【重燃希望】无法令已经牺牲的玩家角色复活。")
        if target_name not in self.conflict_manager.state.fallen_pcs:
            raise ValueError("【重燃希望】只能选择一名已经放弃抵抗的玩家角色。")
        current_scene = self.scene_manager.current_scene if self.scene_manager is not None else None
        if current_scene is None or target_name not in current_scene.participants:
            raise ValueError("【重燃希望】只能影响叙事层面上仍处于当前场景的玩家角色。")
        cooldown_key = f"scene:skill:重燃希望:target:{target_name}"
        if cooldown_key in target.trigger_cooldowns:
            raise ValueError(f"【{target_name}】在本场景中已经被【重燃希望】影响过一次。")
        mp_change = self._spend_mp_or_fail(action, actor.name, 40, f"施放英雄法术【{skill_name}】。")
        if isinstance(mp_change, ActionResolution):
            return mp_change
        recovery = target.crisis_threshold if target.crisis_threshold else target.max_hp // 2
        before, after = self.character_manager.modify_resource(target.name, "hp", recovery)
        self.conflict_manager.state.defeated_combatants.discard(target.name)
        self.conflict_manager.state.fallen_pcs.pop(target.name, None)
        target.trigger_cooldowns.add(cooldown_key)
        if self.conflict_manager.state.active and target.name not in self.conflict_manager.state.turn_order:
            self.conflict_manager.state.turn_order.append(target.name)
        return ActionResolution(
            action=action,
            rules_text=f"{actor.name} 施放【{skill_name}】，{target.name} 恢复意识并恢复 {after - before} HP。",
            payload={
                "skill_name": skill_name,
                "resource_change": mp_change,
                "healing_change": ResourceChange(target.name, "hp", after - before, before, after, "重燃希望恢复倒下的英雄。"),
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
                    "multi_attack": max(2, self._int_parameter(action.parameters, "multi_attack", 2)),
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
        max_effects: int = 1,
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
            raw_options = action.parameters.get("options")
            options = list(raw_options) if isinstance(raw_options, list) else [action.parameters.get("option", "status")]
            if len(options) > max_effects:
                raise ValueError(f"【{skill_name}】本次最多选择 {max_effects} 项效果。")
            raw_statuses = action.parameters.get("status_effects")
            status_queue = list(raw_statuses) if isinstance(raw_statuses, list) else []
            applied_effects: list[dict[str, object]] = []
            for option in options:
                if option == "mp_loss":
                    before, after = self.character_manager.modify_resource(target.name, "mp", -(rank * mp_loss_per_rank))
                    change = ResourceChange(
                        target.name,
                        "mp",
                        after - before,
                        before,
                        after,
                        f"【{skill_name}】使目标失去 MP。",
                    )
                    roll_resolution.payload["target_resource_change"] = change
                    applied_effects.append({"option": "mp_loss", "change": change})
                    roll_resolution.rules_text += f" {target.name} 失去 {before - after} MP。"
                    continue
                raw_status = option if option not in {"status", "abnormal_status"} else (
                    status_queue.pop(0) if status_queue else action.parameters.get("status_effect", default_status.value)
                )
                status = StatusEffect(raw_status)
                if status not in selectable_statuses:
                    raise ValueError(f"【{skill_name}】不能施加该状态。")
                applied = self.conflict_manager.apply_status(target.name, status)
                roll_resolution.payload["hinder_status"] = status
                roll_resolution.payload["status_applied"] = applied
                applied_effects.append({"option": "status", "status": status, "applied": applied})
                roll_resolution.rules_text += f" {target.name} 受到{self._status_name(status)}。"
            roll_resolution.payload["selected_effects"] = applied_effects
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
                self.conflict_manager.resolve_zero_hp(
                    target_name,
                    source_actor=self._zero_hp_source_actor(
                        action,
                        actor.name,
                    ),
                )
        target_text = "、".join(targets)
        return ActionResolution(
            action=action,
            rules_text=f"{actor.name} 施放【{skill_name}】，{target_text} 各承受 {amount} 点{self._damage_type_text(damage_type)}伤害。",
            payload={"skill_name": skill_name, "resource_change": mp_change, "target_resource_changes": changes},
        )

    def _resolve_pending_skill(self, action: Action, skill_name: str) -> ActionResolution:
        coverage = skill_implementation_coverage(skill_name)
        if coverage and coverage.category == SKILL_COVERAGE_PASSIVE_HARD:
            raise ValueError(
                f"【{skill_name}】会在满足技能条件时生效，不是可以单独发动的一次行动。"
            )
        raise ValueError(
            f"【{skill_name}】没有可直接提交的技能行动执行器。"
            "若它是触发式能力，请先提交实际触发它的攻击、施法、防御或其他行动；"
            "若规则确实要求主动发动，则当前规则实现尚不完整，不能把本次行动判为成功。"
        )

    def _resolve_roll(self, action: Action) -> ActionResolution:
        actor = self.character_manager.get(action.parameters["actor"])
        target_name = self._target_name(action)
        target_exists = self.character_manager.exists(target_name)
        target = self.character_manager.get(target_name) if target_exists else actor
        teamwork_bonus, teamwork_payload = self._declared_teamwork_bonus(action, actor)
        advantage_bonus = self._consume_advantage_bonus(actor.name)
        next_check_bonus = self._consume_next_check_bonus(actor.name)
        npc_context = str(
            action.parameters.get("check_context")
            or action.parameters.get("reasoning")
            or action.parameters.get("reason")
            or ""
        ).strip()
        npc_context_bonus = npc_context_check_bonus(actor, npc_context)
        attributes = self.rules_engine.normalize_check_attributes(
            actor,
            action.parameters.get("attributes", ["INS", "WLP"]),
        )
        check_trigger_effects = self.skill_trigger_manager.emit(
            "before_check",
            actor,
            attributes=attributes,
            is_open_check=bool(action.parameters.get("open_check") or action.parameters.get("is_open_check")),
        ).effects
        check_trigger_bonus = sum(effect.amount for effect in check_trigger_effects)
        opposed_roll = bool(action.parameters.get("_opposed_check_roll"))
        outcome = self.rules_engine.roll_check(
            actor=actor,
            attributes=attributes,
            target_number=(
                0
                if opposed_roll
                else self._target_number_parameter(action.parameters, default=10)
            ),
            modifier=self._int_parameter(action.parameters, "modifier", 0)
            + teamwork_bonus
            + advantage_bonus
            + next_check_bonus
            + check_trigger_bonus
            + npc_context_bonus
            + (
                self._active_hit_check_bonus(actor.name)
                if action.parameters.get("_weapon_attack")
                else 0
            ),
            target=target_name,
            reason=action.parameters.get("reasoning")
            or action.parameters.get("reason", ""),
        )
        invocation_notes: list[str] = []
        invocation_payload: dict[str, object] = {}
        if action.parameters.get("invoke_trait") or action.parameters.get("trait_name") or action.parameters.get("invoke_bond_target") or action.parameters.get("bond_target"):
            outcome, invocation_notes, invocation_payload = self._apply_declared_invocations(action, outcome, actor)
        self._remember_roll(outcome)

        rules_text = (
            f"{actor.name}的对抗检定骰面总值为 {outcome.total}。"
            if opposed_roll
            else (
                f"检定 {outcome.total} 对抗难度等级 {outcome.target_number}: "
                f"{'成功' if outcome.success else '失败'}。"
            )
        )
        payload: dict[str, object] = {"roll": outcome}
        if npc_context_bonus:
            payload["npc_context_check_bonus"] = {
                "amount": npc_context_bonus,
                "context": npc_context,
            }
        if advantage_bonus:
            payload["advantage_bonus"] = advantage_bonus
            rules_text += f" 机会【优势】提供 +{advantage_bonus} 修正。"
        if next_check_bonus:
            payload["next_check_bonus"] = next_check_bonus
            rules_text += f" 支援效果提供 +{next_check_bonus} 修正。"
        if check_trigger_effects:
            payload["skill_trigger_effects"] = [
                {
                    "source": effect.source,
                    "amount": effect.amount,
                    "note": effect.note,
                }
                for effect in check_trigger_effects
            ]
            rules_text += " " + " ".join(
                f"【{effect.source}】提供 +{effect.amount} 修正。" for effect in check_trigger_effects
            )
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

        if (
            not outcome.success
            and action.parameters.get("_weapon_attack")
            and target_exists
        ):
            missed_events = self.combat_trait_manager.after_attack_missed(
                target,
                triggering_actor=actor.name,
            )
            self._append_combat_trait_events(payload, missed_events)
            self._resolve_npc_ability_events(action, missed_events, payload)

        if (
            outcome.success
            and action.parameters.get("non_damage", False)
            and target_exists
        ):
            statuses_before_hit = set(target.statuses)
            self._apply_on_hit_status(action, target.name, payload)
            self._apply_npc_attack_hit_effects(
                actor,
                target,
                actual_hp_loss=0,
                statuses_before_hit=statuses_before_hit,
                action=action,
                payload=payload,
            )
            payload["target_status"] = self.character_manager.format_status(
                self.character_manager.get(target.name)
            )

        if outcome.success and not action.parameters.get("non_damage", False) and not target_exists:
            payload["scene_object"] = target_name
            rules_text += f" 【{target_name}】不是已建档角色，本次检定只记录场景影响，不执行 HP 伤害结算。"
            self.world_state.add_memory(f"{actor.name} 对场景目标 {target_name} 的检定成功。")
            self.world_state.remember_subject_fact(target_name, f"被 {actor.name} 成功影响。")
        if outcome.success and not action.parameters.get("non_damage", False) and target_exists:
            statuses_before_hit = set(target.statuses)
            next_damage_bonus = self._consume_next_damage_bonus(target.name)
            damage_type = action.parameters.get(
                "damage_type",
                self.character_manager.effective_weapon_damage_type(actor.name),
            )
            incoming_damage_bonus = self._incoming_damage_bonus(
                target.name,
                damage_type,
            )
            dirty_bonus = 0
            dirty_effects: list[dict[str, object]] = []
            if action.parameters.get("_weapon_attack"):
                dirty_bonus, dirty_effects = self._single_target_hit_bonus(actor, target)
            damage_high_roll = (
                self._int_parameter(
                    action.parameters,
                    "_damage_high_roll_override",
                    0,
                )
                if "_damage_high_roll_override" in action.parameters
                else outcome.high_roll
            )
            damage, affinity = self.rules_engine.compute_damage(
                high_roll=damage_high_roll,
                weapon_damage=action.parameters.get("weapon_damage", actor.weapon_damage)
                + dirty_bonus
                + next_damage_bonus
                + incoming_damage_bonus,
                damage_type=damage_type,
                target=target,
                ignore_resist=action.parameters.get("ignore_resist", False),
                ignore_all_affinities=action.parameters.get("ignore_all_affinities", False),
            )
            if next_damage_bonus:
                payload["next_damage_bonus"] = next_damage_bonus
            if incoming_damage_bonus:
                payload["incoming_damage_bonus"] = incoming_damage_bonus
            if dirty_effects:
                payload["single_target_skill_effects"] = dirty_effects
            if damage >= 0:
                before, after = self._apply_damage_from(actor.name, target.name, damage)
            else:
                before, after = self.character_manager.modify_resource(target.name, "hp", -damage)
            outcome.damage = abs(damage)
            payload["actual_hp_loss"] = max(0, before - after)
            outcome.high_roll = damage_high_roll
            outcome.damage_type = damage_type
            outcome.applied_affinity = affinity
            outcome.hp_after = after
            self._after_actor_deals_damage(actor, target, before, after)
            self._apply_combat_trait_after_damage(
                target.name,
                affinity,
                abs(damage),
                payload,
                hp_before=before,
                action=action,
                source_actor=actor.name,
                is_spell=bool(action.parameters.get("spell_name")),
            )
            payload["target_status"] = self.character_manager.format_status(self.character_manager.get(target.name))
            rules_text += f" 伤害 {damage}（{self._affinity_label(affinity)}）。"
            self._remember_damage_outcome(actor.name, target.name, outcome)
            self._apply_on_hit_status(action, target.name, payload)
            self._apply_npc_attack_hit_effects(
                actor,
                target,
                actual_hp_loss=max(0, before - after),
                statuses_before_hit=statuses_before_hit,
                action=action,
                payload=payload,
            )
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
                self._apply_combat_trait_before_zero_hp(
                    target.name,
                    payload,
                    action=action,
                    source_actor=actor.name,
                    damage_type=damage_type,
                )
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
                        pc_consequence=str(action.parameters.get("pc_consequence") or ""),
                        source_actor=self._zero_hp_source_actor(
                            action,
                            actor.name,
                        ),
                        villain_mode=action.parameters.get("villain_zero_hp_mode", "auto"),
                        allow_escalation=action.parameters.get("allow_escalation", True),
                        sacrifice_benefits_bond=action.parameters.get("sacrifice_benefits_bond"),
                        sacrifice_betters_world=action.parameters.get("sacrifice_betters_world"),
                        require_all_sacrifice_conditions=self._organized_chronicles_mode_enabled(),
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
            clock_trigger_effects = self.skill_trigger_manager.emit(
                "after_clock_check",
                actor,
                silver_tongue_mp=self._first_int_parameter(
                    action.parameters,
                    ["silver_tongue_mp", "eloquence_mp", "clock_extra_mp"],
                    0,
                    minimum=0,
                ),
                arcanum_resonance=bool(
                    action.parameters.get("arcanum_resonance")
                    or action.parameters.get("arcana_domain_relevant")
                    or action.parameters.get("arcanum_domain_relevant")
                ),
            ).effects
            if delta != 0 and clock_trigger_effects:
                direction = 1 if delta > 0 else -1
                for effect in clock_trigger_effects:
                    if effect.resource == "mp" and effect.resource_cost > 0:
                        before_mp, after_mp = self.character_manager.modify_resource(
                            actor.name,
                            "mp",
                            -effect.resource_cost,
                        )
                        payload.setdefault("resource_changes", []).append(
                            ResourceChange(
                                target=actor.name,
                                resource="mp",
                                amount=after_mp - before_mp,
                                before=before_mp,
                                after=after_mp,
                                reason=f"发动【{effect.source}】额外影响命刻。",
                            )
                        )
                    delta += direction * effect.amount
                payload["clock_skill_trigger_effects"] = [
                    {
                        "source": effect.source,
                        "amount": effect.amount,
                        "note": effect.note,
                        "resource": effect.resource,
                        "resource_cost": effect.resource_cost,
                    }
                    for effect in clock_trigger_effects
                ]
            npc_clock_bonus = (
                npc_clock_extra_segments(actor, clock.name)
                if delta != 0
                else 0
            )
            if npc_clock_bonus:
                delta += (1 if delta > 0 else -1) * npc_clock_bonus
                payload["npc_clock_bonus"] = {
                    "amount": npc_clock_bonus,
                    "source": "疾速",
                }
            before, after = self.clock_manager.advance(clock_name, delta)
            actual_delta = after - before
            payload["clock_change"] = ClockChange(
                clock_name=clock.name,
                before=before,
                after=after,
                delta=actual_delta,
                max_segments=clock.max_segments,
                reason="玩家成功压制威胁命刻。" if corrected_threat_direction else "检定成功改变命刻。",
                clock_type=clock.clock_type,
                stakes=clock.stakes,
                completion_consequence=clock.completion_consequence,
            )
            if corrected_threat_direction:
                payload["clock_direction_corrected"] = True
            if actual_delta == 0:
                rules_text += f" 命刻 [{clock.name}] 已在边界，进度未变化。"
            elif actual_delta >= 0:
                rules_text += f" 命刻 [{clock.name}] 推进 {actual_delta} 格。"
            else:
                rules_text += f" 命刻 [{clock.name}] 擦除 {abs(actual_delta)} 格。"
            if payload.get("clock_skill_trigger_effects"):
                rules_text += " " + " ".join(
                    f"【{effect['source']}】额外影响 {effect['amount']} 格。"
                    for effect in payload["clock_skill_trigger_effects"]
                )
            if payload.get("npc_clock_bonus"):
                rules_text += f" 【疾速】额外影响 {npc_clock_bonus} 格。"
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
            actual_delta = after - before
            payload["clock_change"] = ClockChange(
                clock_name=clock.name,
                before=before,
                after=after,
                delta=actual_delta,
                max_segments=clock.max_segments,
                reason="检定失败推进威胁命刻。",
                clock_type=clock.clock_type,
                stakes=clock.stakes,
                completion_consequence=clock.completion_consequence,
            )
            if actual_delta == 0:
                rules_text += f" 威胁命刻 [{clock.name}] 已在边界，进度未变化。"
            else:
                rules_text += f" 威胁命刻 [{clock.name}] 推进 {actual_delta} 格。"
        self._remember_pending_clock_check(action, outcome, payload)
        return ActionResolution(action=action, rules_text=rules_text, payload=payload)

    def _append_trigger_results(self, payload: dict[str, object], trigger_results: list) -> None:
        if trigger_results:
            payload.setdefault("trigger_results", []).extend(trigger_results)

    def _apply_combat_trait_after_damage(
        self,
        target_name: str,
        affinity: Affinity,
        damage: int,
        payload: dict[str, object],
        *,
        hp_before: int | None = None,
        action: Action | None = None,
        source_actor: str = "",
        is_spell: bool = False,
    ) -> list[CombatTraitEvent]:
        if not self.character_manager.exists(target_name):
            return []
        target = self.character_manager.get(target_name)
        events = self.combat_trait_manager.after_damage(
            target,
            affinity=affinity,
            damage=damage,
            hp_before=hp_before,
            triggering_actor=source_actor,
            is_spell=is_spell,
        )
        self._append_combat_trait_events(payload, events)
        if action is not None:
            self._resolve_npc_ability_events(action, events, payload)
        return events

    def _apply_combat_trait_before_zero_hp(
        self,
        target_name: str,
        payload: dict[str, object],
        *,
        action: Action | None = None,
        source_actor: str = "",
        damage_type: str = "",
    ) -> list[CombatTraitEvent]:
        if not self.character_manager.exists(target_name):
            return []
        target = self.character_manager.get(target_name)
        events = self.combat_trait_manager.before_zero_hp(
            target,
            triggering_actor=source_actor,
            damage_type=damage_type,
        )
        self._append_combat_trait_events(payload, events)
        if action is not None:
            self._resolve_npc_ability_events(action, events, payload)
        return events

    def _resolve_npc_ability_events(
        self,
        action: Action,
        events: list[CombatTraitEvent],
        payload: dict[str, object],
    ) -> None:
        """Apply the externally targeted part of typed NPC abilities.

        The trait manager owns trigger detection and self-only mutations. This
        interceptor owns damage, statuses and the zero-HP lifecycle so those
        effects use the same authoritative rules as ordinary attacks/spells.
        An ability can resolve at most once inside one action transaction,
        preventing two reactive damage abilities from recursing forever.
        """

        resolved_ids = payload.setdefault("_npc_ability_resolved_ids", [])
        if not isinstance(resolved_ids, list):
            resolved_ids = []
            payload["_npc_ability_resolved_ids"] = resolved_ids
        results = payload.setdefault("npc_ability_results", [])
        if not isinstance(results, list):
            results = []
            payload["npc_ability_results"] = results

        for event in events:
            data = dict(event.data or {})
            if not data.get("pending_external_resolution"):
                continue
            ability_id = str(data.get("ability_id") or "").strip()
            if not ability_id or ability_id in resolved_ids:
                continue
            resolved_ids.append(ability_id)
            target_names = self._npc_ability_target_names(
                owner=event.actor,
                target_scope=str(data.get("target_scope") or "self"),
                triggering_actor=str(data.get("triggering_actor") or ""),
            )
            effect_type = str(data.get("effect_type") or "")
            ability_result: dict[str, object] = {
                "ability_id": ability_id,
                "ability_name": str(data.get("ability_name") or ""),
                "owner": event.actor,
                "effect_type": effect_type,
                "targets": list(target_names),
            }

            if effect_type == "fixed_damage":
                amount = max(0, int(data.get("amount") or 0))
                damage_type = str(data.get("damage_type") or "physical")
                damage_results: list[dict[str, object]] = []
                for target_name in target_names:
                    if (
                        not self.character_manager.exists(target_name)
                        or self.character_manager.get(target_name).hp <= 0
                    ):
                        continue
                    before, after, affinity = self._apply_fixed_damage(
                        target_name,
                        amount,
                        damage_type,
                        source_name=event.actor,
                        ignore_resist=bool(data.get("ignore_resist")),
                    )
                    actual_loss = max(0, before - after)
                    damage_result: dict[str, object] = {
                        "target": target_name,
                        "amount": actual_loss,
                        "damage_type": damage_type,
                        "affinity": affinity,
                        "hp_before": before,
                        "hp_after": after,
                    }
                    self._apply_combat_trait_after_damage(
                        target_name,
                        affinity,
                        actual_loss,
                        payload,
                        hp_before=before,
                        action=action,
                        source_actor=event.actor,
                        is_spell=False,
                    )
                    if after == 0:
                        after, conflict_event, _ = self._resolve_zero_hp_after_damage(
                            action,
                            source_actor=event.actor,
                            target_name=target_name,
                            payload=payload,
                            damage_type=damage_type,
                        )
                        damage_result["hp_after"] = after
                        if conflict_event is not None:
                            damage_result["conflict_event"] = conflict_event
                    damage_results.append(damage_result)
                ability_result["damage_results"] = damage_results
            elif effect_type == "status_apply":
                statuses = [StatusEffect(value) for value in data.get("statuses", [])]
                applied_by_target: dict[str, list[str]] = {}
                for target_name in target_names:
                    if (
                        not self.character_manager.exists(target_name)
                        or self.character_manager.get(target_name).hp <= 0
                    ):
                        continue
                    applied_by_target[target_name] = [
                        status.value
                        for status in statuses
                        if self.conflict_manager.apply_status(target_name, status)
                    ]
                ability_result["applied_statuses"] = applied_by_target
            elif effect_type == "clear_statuses":
                cleared_by_target: dict[str, list[str]] = {}
                statuses = [StatusEffect(value) for value in data.get("statuses", [])]
                for target_name in target_names:
                    if not self.character_manager.exists(target_name):
                        continue
                    cleared_by_target[target_name] = [
                        status.value
                        for status in statuses
                        if self.conflict_manager.remove_status(target_name, status)
                    ]
                ability_result["cleared_statuses"] = cleared_by_target
            elif effect_type == "affinity_change":
                raw_affinities = dict(data.get("affinity_changes") or {})
                affinity_changes = {
                    damage_type: Affinity(value)
                    for damage_type, value in raw_affinities.items()
                }
                expires_on = str(data.get("expires_on") or "").strip()
                applied_targets: list[str] = []
                for target_name in target_names:
                    if not self.character_manager.exists(target_name):
                        continue
                    if expires_on:
                        self.conflict_manager.register_effect(
                            TimedEffect(
                                owner=event.actor,
                                effect_type="affinity_buff",
                                expires_on=EffectTiming(expires_on),
                                target=target_name,
                                source=str(data.get("ability_name") or "NPC能力"),
                                effect_key=ability_id,
                                data={"affinity_changes": affinity_changes},
                            )
                        )
                    else:
                        self.character_manager.get(
                            target_name
                        ).temporary_affinities.update(affinity_changes)
                    applied_targets.append(target_name)
                ability_result["affinity_changes"] = raw_affinities
                ability_result["applied_targets"] = applied_targets
            elif effect_type == "terrain_guard":
                raw_affinities = dict(data.get("affinity_changes") or {})
                affinity_changes = {
                    damage_type: Affinity(value)
                    for damage_type, value in raw_affinities.items()
                }
                expires_on = EffectTiming(
                    str(data.get("expires_on") or EffectTiming.OWNER_TURN_START.value)
                )
                amount = max(0, int(data.get("amount") or 0))
                for target_name in target_names:
                    if amount:
                        self.conflict_manager.register_effect(
                            TimedEffect(
                                owner=event.actor,
                                effect_type="defense_bonus",
                                expires_on=expires_on,
                                target=target_name,
                                source=str(data.get("ability_name") or "NPC能力"),
                                effect_key=f"{ability_id}:defense",
                                data={"defense_bonus": {"physical": amount}},
                            )
                        )
                    if affinity_changes:
                        self.conflict_manager.register_effect(
                            TimedEffect(
                                owner=event.actor,
                                effect_type="affinity_buff",
                                expires_on=expires_on,
                                target=target_name,
                                source=str(data.get("ability_name") or "NPC能力"),
                                effect_key=f"{ability_id}:affinity",
                                data={"affinity_changes": affinity_changes},
                            )
                        )
                ability_result["defense_bonus"] = {"physical": amount}
                ability_result["affinity_changes"] = raw_affinities
                ability_result["terrain"] = str(data.get("context_keyword") or "")
            results.append(ability_result)

    def _npc_ability_target_names(
        self,
        *,
        owner: str,
        target_scope: str,
        triggering_actor: str,
    ) -> list[str]:
        if target_scope == "self":
            return [owner] if self.character_manager.exists(owner) else []
        if target_scope == "triggering_actor":
            return (
                [triggering_actor]
                if triggering_actor and self.character_manager.exists(triggering_actor)
                else []
            )

        participants = list(self.conflict_manager.state.turn_order)
        if not participants:
            participants = [character.name for character in self.character_manager.all()]
        participants = list(dict.fromkeys([*participants, owner]))
        if target_scope == "all_creatures":
            return [
                name
                for name in participants
                if self.character_manager.exists(name)
                and self.character_manager.get(name).hp > 0
            ]
        if target_scope == "all_other_creatures":
            return [
                name
                for name in participants
                if name != owner
                and self.character_manager.exists(name)
                and self.character_manager.get(name).hp > 0
            ]
        if target_scope == "all_living_creatures":
            return [
                name
                for name in participants
                if name != owner
                and self.character_manager.exists(name)
                and self.character_manager.get(name).hp > 0
                and is_living_creature(self.character_manager.get(name))
            ]

        owner_side = self.conflict_manager.combat_side(owner)
        opponents = [
            name
            for name in participants
            if name != owner
            and self.character_manager.exists(name)
            and self.character_manager.get(name).hp > 0
            and self.conflict_manager.combat_side(name) != owner_side
        ]
        if target_scope == "one_enemy":
            return opponents[:1]
        if target_scope == "all_enemies":
            return opponents
        return []

    def _resolve_zero_hp_after_damage(
        self,
        action: Action,
        *,
        source_actor: str,
        target_name: str,
        payload: dict[str, object],
        damage_type: str = "",
    ) -> tuple[int, ConflictEvent | None, str]:
        """Run the complete zero-HP lifecycle after any source of damage."""

        target = self.character_manager.get(target_name)
        if target.hp > 0:
            return target.hp, None, ""

        self._apply_combat_trait_before_zero_hp(
            target_name,
            payload,
            action=action,
            source_actor=source_actor,
            damage_type=damage_type,
        )
        trigger_results = self.trigger_manager.before_zero_hp(target_name)
        if trigger_results:
            self._append_trigger_results(payload, trigger_results)
        target = self.character_manager.get(target_name)
        trigger_text = self._trigger_rules_text(trigger_results)
        if target.hp > 0:
            return target.hp, None, trigger_text

        if self.conflict_manager.prevent_zero_hp_once(target_name):
            event = self.conflict_event_survive_once(target_name)
            return 1, event, f"{trigger_text} {event.summary}".strip()

        event = self.conflict_manager.resolve_zero_hp(
            target=target_name,
            pc_consequence=str(action.parameters.get("pc_consequence") or ""),
            source_actor=source_actor,
            villain_mode=action.parameters.get("villain_zero_hp_mode", "auto"),
            allow_escalation=action.parameters.get("allow_escalation", True),
            sacrifice_benefits_bond=action.parameters.get(
                "sacrifice_benefits_bond"
            ),
            sacrifice_betters_world=action.parameters.get(
                "sacrifice_betters_world"
            ),
            require_all_sacrifice_conditions=self._organized_chronicles_mode_enabled(),
        )
        hp_after = (
            int(event.hp_after)
            if event.hp_after is not None
            else self.character_manager.get(target_name).hp
        )
        return hp_after, event, f"{trigger_text} {event.summary}".strip()

    def _append_combat_trait_events(
        self,
        payload: dict[str, object],
        events: list[CombatTraitEvent],
    ) -> None:
        if not events:
            return
        payload.setdefault("combat_trait_events", []).extend(events)
        for event in events:
            if event.effect is not None and self.conflict_manager.state.active:
                self.conflict_manager.register_effect(event.effect)
            self.conflict_manager.record_log(event.actor, event.event_type, event.summary)

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
        actual_delta = after - before
        change = ClockChange(
            clock_name=clock.name,
            before=before,
            after=after,
            delta=actual_delta,
            max_segments=clock.max_segments,
            reason=action.parameters.get("reason", ""),
            clock_type=clock.clock_type,
            stakes=clock.stakes,
            completion_consequence=clock.completion_consequence,
        )
        verb = "创建命刻" if created else "命刻"
        if actual_delta == 0:
            rules_text = f"{verb}【{clock.name}】已在 {after}/{clock.max_segments}，进度未变化。"
        else:
            rules_text = f"{verb}【{clock.name}】从 {before}/{clock.max_segments} 变为 {after}/{clock.max_segments}。"
        return ActionResolution(
            action=action,
            rules_text=rules_text,
            payload={"clock_change": change, "clock_created": created},
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
        if self.clock_manager.is_retired(clock_name) and not bool(action.parameters.get("allow_clock_reopen")):
            raise ValueError(
                f"命刻【{clock_name}】的后果已经兑现；若局势产生新的倒计时，请使用新的命刻名称。"
            )
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
                    scope=str(action.parameters.get(f"{prefix}clock_scope") or action.parameters.get("clock_scope") or ""),
                    scene_id=str(action.parameters.get(f"{prefix}scene_id") or action.parameters.get("scene_id") or ""),
                    owner=str(action.parameters.get(f"{prefix}clock_owner") or action.parameters.get("clock_owner") or ""),
                    source=str(action.parameters.get(f"{prefix}clock_source") or action.parameters.get("clock_source") or ""),
                    completion_consequence=str(
                        action.parameters.get(f"{prefix}completion_consequence")
                        or action.parameters.get("completion_consequence")
                        or ""
                    ),
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
        cost = abs(action.parameters.get("fabula_cost", action.parameters.get("cost", 1)))
        fact = (
            action.parameters.get("fact")
            or action.parameters.get("story_change")
            or action.parameters.get("new_fact")
            or action.parameters.get("description")
            or action.parameters.get("content")
        )
        if not fact:
            fact = "玩家消耗物语点，为当前场景加入了一个有利的新故事元素。"
        violation = self.world_state.iconic_protection_violation(fact)
        if violation:
            self.world_state.record_transparency_audit(
                "iconic_element_protection",
                False,
                violation,
                severity="warning",
                source="ActionInterceptor._resolve_story_change",
            )
            return ActionResolution(
                action=action,
                rules_text=violation,
                payload={
                    "story_change_failed": True,
                    "iconic_violation": True,
                    "fact": fact,
                    "reason": violation,
                },
            )
        before, after = self.character_manager.modify_resource(target, "fabula_points", -cost)
        self.world_state.apply_story_fact(fact)
        followup_intent = str(action.parameters.get("followup_intent") or "").strip()
        return ActionResolution(
            action=action,
            rules_text=f"{target} 消耗 {cost} 点物语点 ({before} -> {after})，世界设定已更新。",
            payload={"fact": fact, "followup_intent": followup_intent, "fabula_cost": cost},
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

    @staticmethod
    def _npc_conditional_attack_damage_bonus(action: Action, target) -> int:
        bonus = int(action.parameters.get("conditional_damage_bonus", 0) or 0)
        if bonus <= 0:
            return 0
        if action.parameters.get("conditional_any_target_status"):
            return bonus if target.statuses else 0
        required_statuses: set[StatusEffect] = set()
        for raw_status in action.parameters.get("conditional_target_statuses", []):
            try:
                required_statuses.add(
                    raw_status
                    if isinstance(raw_status, StatusEffect)
                    else StatusEffect(str(raw_status))
                )
            except ValueError:
                continue
        return bonus if required_statuses.intersection(target.statuses) else 0

    def _apply_npc_attack_hit_effects(
        self,
        actor,
        target,
        *,
        actual_hp_loss: int,
        statuses_before_hit: set[StatusEffect] | None = None,
        action: Action,
        payload: dict[str, object],
    ) -> None:
        changes: list[ResourceChange] = []
        recover_fraction = float(
            action.parameters.get("recover_hp_fraction", 0.0) or 0.0
        )
        hp_recovery = int(max(0, actual_hp_loss) * recover_fraction)
        if hp_recovery > 0:
            before, after = self.character_manager.modify_resource(
                actor.name,
                "hp",
                hp_recovery,
            )
            changes.append(
                ResourceChange(
                    actor.name,
                    "hp",
                    after - before,
                    before,
                    after,
                    "NPC基础攻击命中后的生命恢复。",
                )
            )

        recover_mp = int(action.parameters.get("recover_mp_on_hit", 0) or 0)
        if recover_mp > 0:
            before, after = self.character_manager.modify_resource(
                actor.name,
                "mp",
                recover_mp,
            )
            changes.append(
                ResourceChange(
                    actor.name,
                    "mp",
                    after - before,
                    before,
                    after,
                    "NPC基础攻击命中后的精神恢复。",
                )
            )

        target_mp_loss = int(action.parameters.get("target_mp_loss", 0) or 0)
        if target_mp_loss > 0:
            before, after = self.character_manager.modify_resource(
                target.name,
                "mp",
                -target_mp_loss,
            )
            changes.append(
                ResourceChange(
                    target.name,
                    "mp",
                    after - before,
                    before,
                    after,
                    "NPC基础攻击命中造成精神值损失。",
                )
            )

        target_ip_loss = int(action.parameters.get("target_ip_loss", 0) or 0)
        if target_ip_loss > 0:
            before, after = self.character_manager.modify_resource(
                target.name,
                "inventory_points",
                -target_ip_loss,
            )
            changes.append(
                ResourceChange(
                    target.name,
                    "inventory_points",
                    after - before,
                    before,
                    after,
                    "NPC基础攻击命中造成物资点损失。",
                )
            )

        if changes:
            payload.setdefault("npc_attack_hit_effects", []).extend(changes)

        self._apply_structured_npc_attack_hit_effects(
            actor,
            target,
            action=action,
            payload=payload,
            statuses_before_hit=set(statuses_before_hit or ()),
        )

    def _apply_structured_npc_attack_hit_effects(
        self,
        actor: Character,
        target: Character,
        *,
        action: Action,
        payload: dict[str, object],
        statuses_before_hit: set[StatusEffect],
    ) -> None:
        resolved: list[dict[str, object]] = []
        for effect in self._npc_attack_effects(action, trigger="on_hit"):
            effect_type = str(effect.get("effect_type") or "")
            required_status = self._optional_status_effect(
                effect.get("required_status")
            )
            status_source = (
                statuses_before_hit
                if effect.get("required_status_before_hit")
                else set(target.statuses)
            )
            if required_status is not None and required_status not in status_source:
                continue

            if effect_type == "swallow":
                if target.hp <= 0:
                    continue
                if len(self.npc_conditions.swallowed_by(actor.name)) >= self.npc_conditions.capacity_for(actor.name):
                    resolved.append(
                        {
                            "effect_type": effect_type,
                            "target": target.name,
                            "applied": False,
                            "reason": "capacity_full",
                        }
                    )
                    continue
                swallowed = self.npc_conditions.swallow(
                    actor.name,
                    target.name,
                    damage=max(0, int(effect.get("amount", 20) or 20)),
                    damage_type=str(effect.get("damage_type") or "physical"),
                    clock_segments=max(
                        1,
                        int(effect.get("clock_segments", 4) or 4),
                    ),
                )
                resolved.append(
                    {
                        "effect_type": effect_type,
                        "target": target.name,
                        "source": actor.name,
                        "clock_name": swallowed.escape_clock,
                        "applied": True,
                    }
                )
                continue

            if effect_type == "reactive_check":
                if target.hp <= 0:
                    continue
                (
                    deferred_actor,
                    deferred_serial,
                    source_action_type,
                    resume_point,
                ) = self._deferred_turn_lineage_for_action(action)
                window = self.decision_window_manager.create(
                    kind="reactive_check",
                    owner=target.name,
                    prompt=(
                        f"【{target.name}】需要进行【力量+意志】检定，"
                        f"抵抗【{actor.name}】的石化效果。"
                    ),
                    options=[{"choice": "roll", "label": "进行抗性检定"}],
                    scope_kind="conflict",
                    scope_id=str(self.conflict_manager.state.scene_name or "current"),
                    blocking=True,
                    action_type=ActionType.RESOLVE_DECISION.value,
                    transaction_id=(
                        f"reactive:{actor.name}:{target.name}:"
                        f"{self.conflict_manager.state.turn_serial}"
                    ),
                    resume_point=resume_point or "conflict_action_end",
                    payload={
                        "source_actor": actor.name,
                        "source_attack": str(
                            action.parameters.get("attack_name") or "NPC攻击"
                        ),
                        "attributes": list(
                            effect.get("check_attributes") or ["MIG", "WLP"]
                        ),
                        "target_number": max(
                            1,
                            int(effect.get("target_number", 10) or 10),
                        ),
                        "failure_condition": str(
                            effect.get("trait") or "petrified"
                        ),
                        "failure_note": str(effect.get("note") or "石化"),
                        "deferred_turn_actor": deferred_actor or actor.name,
                        "deferred_turn_serial": deferred_serial
                        or int(self.conflict_manager.state.turn_serial or 0),
                        "source_action_type": source_action_type
                        or action.action_type.value,
                        "resume_point": resume_point or "conflict_action_end",
                    },
                    dedupe_key=(
                        f"reactive_check:{actor.name}:{target.name}:"
                        f"{self.conflict_manager.state.turn_serial}"
                    ),
                )
                resolved.append(
                    {
                        "effect_type": effect_type,
                        "target": target.name,
                        "source": actor.name,
                        "window_id": window.window_id,
                        "applied": True,
                    }
                )
                continue

            if effect_type == "action_penalty":
                amount = max(0, int(effect.get("amount", 0) or 0))
                if amount:
                    self.conflict_manager.penalize_next_turn(target.name, amount)
                    resolved.append(
                        {
                            "effect_type": effect_type,
                            "target": target.name,
                            "amount": amount,
                        }
                    )
                continue

            if effect_type in {
                "action_restriction",
                "action_restriction_while_status",
            }:
                timing = self._npc_attack_effect_timing(
                    effect,
                    EffectTiming.SCENE_END,
                )
                requires_status = (
                    required_status.value
                    if effect_type == "action_restriction_while_status"
                    and required_status is not None
                    else ""
                )
                timed = TimedEffect(
                    owner=target.name,
                    target=target.name,
                    effect_type="action_restriction",
                    expires_on=timing,
                    source=str(action.parameters.get("attack_name") or "NPC攻击"),
                    effect_key=(
                        f"npc_attack:{action.parameters.get('attack_id') or 'attack'}:"
                        f"restriction:{','.join(effect.get('action_types') or [])}"
                    ),
                    data={
                        "action_types": list(effect.get("action_types") or []),
                        "requires_status": requires_status,
                        "expire_after_turn_serial": (
                            self.conflict_manager.state.turn_serial + 1
                            if timing == EffectTiming.OWNER_TURN_END
                            else 0
                        ),
                    },
                    note=str(effect.get("note") or "当前效果禁止这项行动。"),
                )
                self.conflict_manager.register_effect(timed)
                resolved.append(
                    {
                        "effect_type": effect_type,
                        "target": target.name,
                        "action_types": list(effect.get("action_types") or []),
                    }
                )
                continue

            if effect_type == "suppress_resistance":
                damage_type = str(effect.get("damage_type") or "")
                if (
                    not damage_type
                    or self.character_manager.effective_affinity(
                        target.name,
                        damage_type,
                    )
                    != Affinity.RESIST
                ):
                    continue
                timing = self._npc_attack_effect_timing(
                    effect,
                    EffectTiming.OWNER_TURN_END,
                )
                timed = TimedEffect(
                    owner=actor.name,
                    target=target.name,
                    effect_type="affinity_buff",
                    expires_on=timing,
                    source=str(action.parameters.get("attack_name") or "NPC攻击"),
                    effect_key=(
                        f"npc_attack:{action.parameters.get('attack_id') or 'attack'}:"
                        f"suppress_resistance:{target.name}:{damage_type}"
                    ),
                    data={
                        "affinity_changes": {damage_type: Affinity.WEAK},
                        "expire_after_turn_serial": (
                            self.conflict_manager.state.turn_serial + 1
                        ),
                    },
                    note=str(effect.get("note") or ""),
                )
                self.conflict_manager.register_effect(timed)
                resolved.append(
                    {
                        "effect_type": effect_type,
                        "target": target.name,
                        "damage_type": damage_type,
                    }
                )
                continue

            if effect_type == "affinity_while_status":
                if required_status is None:
                    continue
                raw_affinity = str(effect.get("affinity") or "")
                if not raw_affinity:
                    continue
                affinity = Affinity(raw_affinity)
                changes = {
                    str(damage_type): affinity
                    for damage_type in effect.get("damage_types", [])
                    if str(damage_type)
                }
                if not changes:
                    continue
                timed = TimedEffect(
                    owner=target.name,
                    target=target.name,
                    effect_type="affinity_buff",
                    expires_on=self._npc_attack_effect_timing(
                        effect,
                        EffectTiming.SCENE_END,
                    ),
                    source=str(action.parameters.get("attack_name") or "NPC攻击"),
                    effect_key=(
                        f"npc_attack:{action.parameters.get('attack_id') or 'attack'}:"
                        f"affinity_while:{required_status.value}"
                    ),
                    data={
                        "affinity_changes": changes,
                        "requires_status": required_status.value,
                    },
                    note=str(effect.get("note") or ""),
                )
                self.conflict_manager.register_effect(timed)
                resolved.append(
                    {
                        "effect_type": effect_type,
                        "target": target.name,
                        "required_status": required_status.value,
                        "damage_types": list(changes),
                    }
                )

        if resolved:
            payload.setdefault("npc_structured_attack_effects", []).extend(resolved)

    def _apply_npc_attack_after_attack_effects(
        self,
        actor: Character,
        *,
        action: Action,
        payload: dict[str, object],
    ) -> None:
        resolved: list[dict[str, object]] = []
        for effect in self._npc_attack_effects(action, trigger="after_attack"):
            if (
                str(effect.get("effect_type") or "") != "suppress_trait"
                or str(effect.get("target_scope") or "target") != "self"
            ):
                continue
            trait = str(effect.get("trait") or "")
            if not trait:
                continue
            timed = TimedEffect(
                owner=actor.name,
                target=actor.name,
                effect_type="trait_suppression",
                expires_on=self._npc_attack_effect_timing(
                    effect,
                    EffectTiming.OWNER_TURN_START,
                ),
                source=str(action.parameters.get("attack_name") or "NPC攻击"),
                effect_key=f"{trait.lower()}_suppressed",
                data={"suppressed_trait": trait},
                note=str(effect.get("note") or ""),
            )
            self.conflict_manager.register_effect(timed)
            resolved.append(
                {
                    "effect_type": "suppress_trait",
                    "target": actor.name,
                    "trait": trait,
                }
            )
        if resolved:
            payload.setdefault("npc_structured_attack_effects", []).extend(resolved)

    @staticmethod
    def _npc_attack_effects(
        action: Action,
        *,
        trigger: str,
    ) -> list[dict[str, object]]:
        return [
            dict(effect)
            for effect in action.parameters.get("npc_attack_effects", [])
            if isinstance(effect, dict)
            and str(effect.get("trigger") or "on_hit") == trigger
        ]

    @staticmethod
    def _npc_attack_effect_timing(
        effect: dict[str, object],
        fallback: EffectTiming,
    ) -> EffectTiming:
        raw = effect.get("expires_on")
        if isinstance(raw, EffectTiming):
            return raw
        try:
            return EffectTiming(str(raw)) if raw else fallback
        except ValueError:
            return fallback

    @staticmethod
    def _optional_status_effect(value: object) -> StatusEffect | None:
        if isinstance(value, StatusEffect):
            return value
        clean = str(value or "").strip()
        if not clean:
            return None
        try:
            return StatusEffect(clean)
        except ValueError:
            return None

    def _uses_attack_window(self, action: Action) -> bool:
        targets = action.parameters.get("targets")
        target = action.parameters.get("target")
        has_multiple_targets = isinstance(targets, list) and len(targets) > 1
        target_is_list = isinstance(target, list) and len(target) > 1
        target_names = self._attack_target_names(action)
        has_typed_miss_reaction = any(
            self.character_manager.exists(name)
            and any(
                profile.trigger == "attack_missed"
                for profile in self.character_manager.get(name).npc_ability_profiles
            )
            for name in target_names
        )
        return (
            has_multiple_targets
            or target_is_list
            or has_typed_miss_reaction
            or int(action.parameters.get("self_hp_loss_if_all_miss", 0) or 0) > 0
            or bool(action.parameters.get("reactions"))
            or bool(action.parameters.get("reaction"))
        )

    def _attack_target_names(self, action: Action) -> list[str]:
        raw_targets = action.parameters.get("targets")
        if raw_targets in (None, "", []):
            raw_targets = action.parameters.get("target")
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

    def _prepare_counter_reactions(
        self,
        action: Action,
        roll,
        actual_targets: list,
        is_melee: bool,
    ) -> list[dict[str, object]]:
        followups: list[dict[str, object]] = []
        if not is_melee or roll.total % 2 != 0:
            return followups
        actual_target_names = {target.name for target in actual_targets}
        for reaction in self._declared_reactions(action):
            if self._normalized_skill_name(reaction.get("skill_name", "")) != "反击":
                continue
            reactor = self.character_manager.get(reaction["actor"])
            if reactor.name not in actual_target_names:
                followups.append(
                    {
                        "actor": reactor.name,
                        "skill_name": "反击",
                        "triggered": False,
                        "reason": "该角色不是本次近战攻击的目标。",
                    }
                )
                continue
            if skill_rank(reactor.skills, "反击") <= 0:
                followups.append(
                    {
                        "actor": reactor.name,
                        "skill_name": "反击",
                        "triggered": False,
                        "reason": "未拥有技能。",
                    }
                )
                continue
            if reactor.hp <= 0:
                followups.append(
                    {
                        "actor": reactor.name,
                        "skill_name": "反击",
                        "triggered": False,
                        "reason": "该角色已经无法行动。",
                    }
                )
                continue
            followups.append(
                {
                    "actor": reactor.name,
                    "skill_name": "反击",
                    "triggered": True,
                    "action_parameters": {
                        "actor": reactor.name,
                        "target": str(action.parameters["actor"]),
                        "attributes": reaction.get(
                            "attributes",
                            reactor.weapon_accuracy_attributes,
                        ),
                        "modifier": reaction.get("modifier", 0),
                        "is_melee": True,
                        "_damage_high_roll_override": 0,
                        "_reaction_followup": "反击",
                        "reasoning": "反击",
                    },
                }
            )
        return followups

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
        return skill_rank(actor.skills, skill_name) > 0 or has_skill_name(actor.hero_skills, skill_name)

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

    def _consume_next_check_bonus(self, actor_name: str) -> int:
        bonus = 0
        remaining_effects = []
        for effect in self.conflict_manager.state.active_effects:
            if effect.effect_type == "next_check_bonus" and effect.target == actor_name:
                bonus += int(effect.data.get("check_bonus", 0) or 0)
                self.conflict_manager._cleanup_effect(effect)
                continue
            remaining_effects.append(effect)
        self.conflict_manager.state.active_effects = remaining_effects
        return bonus

    def _consume_outgoing_ranged_damage_bonus(self, actor_name: str, *, is_melee: bool) -> int:
        if is_melee:
            return 0
        bonus = 0
        remaining_effects = []
        for effect in self.conflict_manager.state.active_effects:
            if effect.effect_type == "outgoing_ranged_damage_bonus" and effect.target == actor_name:
                bonus += int(effect.data.get("damage_bonus", 0) or 0)
                continue
            remaining_effects.append(effect)
        self.conflict_manager.state.active_effects = remaining_effects
        return bonus

    def _active_hit_check_bonus(self, actor_name: str) -> int:
        bonus = 0
        for effect in self.conflict_manager.state.active_effects:
            if (
                effect.effect_type == "hit_check_bonus"
                and effect.target == actor_name
            ):
                bonus += int(effect.data.get("check_bonus", 0) or 0)
        return bonus

    def _incoming_damage_bonus(
        self,
        target_name: str,
        damage_type: str,
    ) -> int:
        bonus = 0
        for effect in self.conflict_manager.state.active_effects:
            if (
                effect.effect_type != "incoming_damage_bonus"
                or effect.target != target_name
            ):
                continue
            selected_type = str(effect.data.get("damage_type") or "").strip()
            if selected_type and selected_type != damage_type:
                continue
            bonus += int(effect.data.get("damage_bonus", 0))
        return bonus

    def _apply_fixed_damage(
        self,
        target_name: str,
        amount: int,
        damage_type: str,
        *,
        source_name: str = "",
        ignore_resist: bool = False,
        ignore_resist_and_immune: bool = False,
    ) -> tuple[int, int, Affinity]:
        target = self.character_manager.get(target_name)
        npc_override = npc_affinity_override(target, damage_type)
        effective_affinity = resolve_affinity(
            (
                npc_override
                if npc_override is not None
                else target.affinities.get(damage_type, Affinity.NORMAL)
            ),
            target.equipment_affinities.get(damage_type),
            target.temporary_affinities.get(damage_type),
            ignore_resist=ignore_resist or ignore_resist_and_immune,
            ignore_immune=ignore_resist_and_immune,
            ignore_all_affinities=damage_type == "none",
        )

        damage = amount + self._incoming_damage_bonus(target_name, damage_type)
        if effective_affinity == Affinity.WEAK:
            damage *= 2
        elif effective_affinity == Affinity.RESIST:
            damage //= 2
        elif effective_affinity == Affinity.IMMUNE:
            damage = 0
        elif effective_affinity == Affinity.ABSORB:
            damage = -damage

        if damage >= 0:
            if source_name:
                before, after = self._apply_damage_from(
                    source_name,
                    target_name,
                    damage,
                )
            else:
                before, after = self.character_manager.apply_damage(
                    target_name,
                    damage,
                )
        else:
            before, after = self.character_manager.modify_resource(target_name, "hp", -damage)
        return before, after, effective_affinity

    def _resolve_attack_target(self, action: Action):
        target = self.character_manager.get(self._target_name(action))
        actor = self.character_manager.get(str(action.parameters.get("actor") or ""))
        notices: list[str] = []
        if action.parameters.get("is_melee", True):
            self._validate_flying_melee_target(actor, target, action)
            guardian = self.character_manager.guardian_for(target.name)
            if guardian is not None:
                notices.append(
                    f"{guardian.name} 挡在 {target.name} 身前，替同伴承受了这次近战攻击。"
                )
                target = guardian
        interposer = self.conflict_manager.npc_interposer_for(
            target.name,
            source_actor=actor.name,
        )
        if interposer is not None:
            notices.append(f"{interposer.name}挺身代替{target.name}承受这次攻击。")
            target = interposer
        return target, " ".join(notices)

    def _validate_flying_melee_target(
        self,
        actor: Character,
        target: Character,
        action: Action,
    ) -> None:
        if not self._flight_is_active(target):
            return
        if self._flight_is_active(actor) or action.parameters.get("can_target_flying"):
            return
        raise ValueError(
            f"【{target.name}】正在飞行，未飞行的【{actor.name}】无法用普通近战攻击选中它。"
        )

    def _flight_is_active(self, character: Character) -> bool:
        if not self.combat_trait_manager.has_flight(character) or character.in_crisis:
            return False
        return not any(
            effect.target == character.name
            and (
                effect.effect_key == "flight_suppressed"
                or effect.data.get("suppressed_trait") == "飞行"
            )
            for effect in self.conflict_manager.state.active_effects
        )

    def _weapon_mastery_bonus(self, actor, is_melee: bool) -> int:
        if is_melee:
            return skill_rank(actor.skills, "近战武器精通")
        return skill_rank(actor.skills, "远程武器精通")

    def _rage_attack_is_active(self, actor: Character) -> bool:
        if not has_skill_name(actor.skills, "狂暴"):
            return False
        item_name = actor.equipped_main_hand or "徒手攻击"
        template_name = actor.equipment_templates.get(item_name, item_name)
        weapon = get_equipment_example(template_name)
        category = weapon.category if weapon is not None else ""
        return category in {"格斗", "匕首", "链枷", "投掷"}

    def _gale_combo_bonuses(
        self,
        actor: Character,
        action: Action,
        *,
        is_melee: bool,
        target_count: int,
    ) -> tuple[int, int]:
        if not is_melee or not has_skill_name(actor.hero_skills, "疾风连打"):
            return 0, 0
        multi_attack = self._int_parameter(
            action.parameters,
            "multi_attack",
            actor.equipment_multi_attack,
        )
        if multi_attack < 2:
            return 0, 0
        return multi_attack, 5 + (5 if target_count == 1 else 0)

    def _attack_damage_type(self, actor, action: Action) -> str:
        if actor.active_arcanum == "剑":
            return "none"
        random_damage_types = [
            str(value)
            for value in action.parameters.get("random_damage_types", [])
            if str(value).strip()
        ]
        if random_damage_types:
            roll = self.rules_engine.roll_die(len(random_damage_types))
            action.parameters["_random_damage_type_roll"] = roll
            return random_damage_types[roll - 1]
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
        arcanum_bonus = 5 if not is_spell and actor.active_arcanum == "剑" else 0
        effects = self.skill_trigger_manager.emit(
            "before_damage",
            actor,
            is_spell=is_spell,
            is_melee=is_melee,
        ).effects
        return sum(effect.amount for effect in effects) + arcanum_bonus

    @staticmethod
    def _npc_spell_damage_bonus(actor: Character, spell_name: str) -> int:
        canonical = normalize_spell_name(spell_name)
        return int(actor.npc_spell_damage_bonus) + int(
            actor.npc_spell_specific_damage_bonuses.get(canonical, 0)
        )

    @staticmethod
    def _spell_check_attributes(
        actor: Character,
        action: Action,
        definition,
    ) -> list[str]:
        configured = actor.npc_spell_attributes.get(
            normalize_spell_name(definition.name)
        )
        if isinstance(configured, (list, tuple)) and len(configured) == 2:
            return [str(item) for item in configured]
        skill_attributes = action.parameters.get("_spell_skill_attributes")
        if isinstance(skill_attributes, (list, tuple)) and len(skill_attributes) == 2:
            return [str(item) for item in skill_attributes]
        return list(definition.attributes)

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
                entries.append(f"{self._damage_type_text(damage_type)}:{self._affinity_label(affinity)}")
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
            "奥灵": RitualDiscipline.ARCANISM,
            "奥灵系仪式": RitualDiscipline.ARCANISM,
            "chimerism": RitualDiscipline.CHIMERISM,
            "嵌合": RitualDiscipline.CHIMERISM,
            "嵌合术": RitualDiscipline.CHIMERISM,
            "拟兽": RitualDiscipline.CHIMERISM,
            "拟兽系仪式": RitualDiscipline.CHIMERISM,
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
            SpellEffectType.EXTRA_ACTION: "acceleration",
            SpellEffectType.SURVIVE_ONCE: "survive_once",
            SpellEffectType.CHECK_BONUS: "hit_check_bonus",
            SpellEffectType.DAMAGE_VULNERABILITY: "incoming_damage_bonus",
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
                "benefits_used": 0,
                "max_benefits": definition.extra_actions,
                "max_spell_mp": 10,
                "check_bonus": definition.check_bonus,
                "damage_bonus": definition.incoming_damage_bonus,
                "damage_type": selected_damage_type,
            },
            note=definition.description,
        )
        if self.character_manager.exists(target_name):
            self.conflict_manager.register_effect(effect)
        elif self.scene_manager is not None and self.scene_manager.is_participant(target_name):
            self.scene_manager.record_narrative_effect(effect)
        else:
            raise ValueError(f"{target_name}不在当前规则场景中。")
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
                f"{self._defense_type_text(kind)}+{amount}" for kind, amount in effect.data.get("defense_bonus", {}).items() if amount
            )
            return rules_text + f" {target_name} 获得 {bonus_text}，持续至{self._duration_text(definition.duration)}。"
        if definition.effect_type == SpellEffectType.DEFENSE_FLOOR:
            floor_text = "、".join(
                f"{self._defense_type_text(kind)}至少 {amount}" for kind, amount in effect.data.get("defense_floor", {}).items() if amount
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
            return (
                rules_text
                + f" 在【{target_name}】每个回合结束时，其可选择用装备武器顺势攻击，"
                "或顺势施放总精神值消耗不高于 10 点的法术；第二次获得这项增益后，法术结束。"
            )
        if definition.effect_type == SpellEffectType.CHECK_BONUS:
            return (
                rules_text
                + f" {target_name} 的命中检定获得 +{effect.data.get('check_bonus', 0)} 修正，"
                f"持续至{self._duration_text(definition.duration)}。"
            )
        if definition.effect_type == SpellEffectType.DAMAGE_VULNERABILITY:
            damage_type = str(effect.data.get("damage_type") or "")
            return (
                rules_text
                + f" {self._damage_type_text(damage_type)}伤害来源对 {target_name} 额外造成"
                f" {effect.data.get('damage_bonus', 0)} 点伤害，"
                f"持续至{self._duration_text(definition.duration)}。"
            )
        if definition.effect_type == SpellEffectType.SURVIVE_ONCE:
            return rules_text + f" {target_name} 在本场景中首次将要倒下时会保留 1 点 HP。"
        return rules_text

    def _spell_effect_group_rules_text(
        self,
        actor_name: str,
        target_names: list[str],
        definition,
        effects: list[TimedEffect],
    ) -> str:
        """Render one public rules sentence for a multi-target timed spell."""

        if not target_names or not effects:
            return f"{actor_name} 施放【{definition.name}】。"
        target_text = "、".join(target_names)
        effect = effects[0]
        duration = self._duration_text(definition.duration)
        prefix = f"{actor_name} 施放【{definition.name}】，{target_text}"
        if definition.effect_type == SpellEffectType.DEFENSE_BUFF:
            bonus_text = "、".join(
                f"{self._defense_type_text(kind)}+{amount}"
                for kind, amount in effect.data.get("defense_bonus", {}).items()
                if amount
            )
            return f"{prefix}获得{bonus_text}，持续至{duration}。"
        if definition.effect_type == SpellEffectType.DEFENSE_FLOOR:
            floor_text = "、".join(
                f"{self._defense_type_text(kind)}至少{amount}"
                for kind, amount in effect.data.get("defense_floor", {}).items()
                if amount
            )
            return f"{prefix}的防御提升为{floor_text}，持续至{duration}。"
        if definition.effect_type == SpellEffectType.AFFINITY_BUFF:
            affinity_text = "、".join(
                f"对{self._damage_type_text(damage_type)}伤害获得{self._affinity_label(affinity)}相性"
                for damage_type, affinity in effect.data.get("affinity_changes", {}).items()
            )
            return f"{prefix}{affinity_text}，持续至{duration}。"
        if definition.effect_type == SpellEffectType.STATUS_IMMUNITY:
            immunity_text = "、".join(
                self._status_name(status)
                for status in effect.data.get("status_immunities", ())
            )
            return f"{prefix}对{immunity_text}免疫，持续至{duration}。"
        if definition.effect_type == SpellEffectType.WEAPON_ENCHANT:
            damage_type = effect.data.get("weapon_damage_type", definition.damage_type)
            return (
                f"{prefix}的武器伤害变为{self._damage_type_text(damage_type)}伤害，"
                f"持续至{duration}。"
            )
        if definition.effect_type == SpellEffectType.ATTRIBUTE_BUFF:
            attribute_text = "、".join(
                f"{attribute}+{value}"
                for attribute, value in effect.data.get("attribute_bonus", {}).items()
                if value
            )
            return f"{prefix}获得属性强化{attribute_text}，持续至{duration}。"
        if definition.effect_type == SpellEffectType.EXTRA_ACTION:
            return (
                f"{prefix}在其每个回合结束时可选择用装备武器顺势攻击，"
                "或顺势施放总精神值消耗不高于10点的法术；第二次获得这项增益后，法术结束。"
            )
        if definition.effect_type == SpellEffectType.SURVIVE_ONCE:
            return f"{prefix}在生命值将降为0时改为保留1点生命值，持续至{duration}。"
        if definition.effect_type == SpellEffectType.CHECK_BONUS:
            return (
                f"{prefix}的命中检定获得+{effect.data.get('check_bonus', 0)}修正，"
                f"持续至{duration}。"
            )
        if definition.effect_type == SpellEffectType.DAMAGE_VULNERABILITY:
            damage_type = str(effect.data.get("damage_type") or "")
            return (
                f"{self._damage_type_text(damage_type)}伤害来源对{target_text}额外造成"
                f"{effect.data.get('damage_bonus', 0)}点伤害，持续至{duration}。"
            )
        return " ".join(
            self._spell_effect_rules_text(actor_name, target_name, definition, timed_effect)
            for target_name, timed_effect in zip(target_names, effects)
        )

    def _selected_status(self, action: Action, definition, allow_missing: bool = False) -> StatusEffect | None:
        raw_status = action.parameters.get("chosen_status") or action.parameters.get("status_effect")
        if raw_status is not None:
            return self._status_effect(raw_status)
        if definition.status_effect is not None:
            return definition.status_effect
        if allow_missing:
            return None
        raise ValueError(f"法术【{definition.name}】需要一个状态选择。")

    def _selected_statuses(self, action: Action, definition) -> list[StatusEffect]:
        raw_values = action.parameters.get("chosen_statuses")
        if isinstance(raw_values, str):
            values = [
                item.strip()
                for item in re.split(r"[、,，/；;\s]+", raw_values)
                if item.strip()
            ]
        elif isinstance(raw_values, (list, tuple, set)):
            values = [item for item in raw_values if item not in (None, "")]
        else:
            single = self._selected_status(action, definition, allow_missing=True)
            return [single] if single is not None else []
        statuses = list(
            dict.fromkeys(self._status_effect(value) for value in values)
        )
        required = max(1, int(definition.selectable_status_count or 1))
        if definition.selectable_statuses and len(statuses) != required:
            raise ValueError(
                f"法术【{definition.name}】需要选择 {required} 种不同异常状态。"
            )
        return statuses

    def _selected_damage_type(self, action: Action, definition) -> str | None:
        if action.parameters.get("chosen_damage_type"):
            return action.parameters["chosen_damage_type"]
        if definition.damage_type != "arcane":
            return definition.damage_type
        return None

    def _selected_attribute(self, action: Action, definition) -> str | None:
        if action.parameters.get("chosen_attribute"):
            return action.parameters["chosen_attribute"]
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

    def _affinity_label(self, affinity: Affinity | str) -> str:
        try:
            normalized = affinity if isinstance(affinity, Affinity) else Affinity(str(affinity))
        except ValueError:
            normalized = Affinity.NORMAL
        mapping = {
            Affinity.NORMAL: "通常相性",
            Affinity.WEAK: "弱点",
            Affinity.RESIST: "抵抗",
            Affinity.IMMUNE: "免疫",
            Affinity.ABSORB: "吸收",
        }
        return mapping[normalized]

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

    def _defense_type_text(self, defense_type: str) -> str:
        mapping = {
            "physical": "物防",
            "magic": "魔防",
        }
        return mapping.get(str(defense_type), str(defense_type))

