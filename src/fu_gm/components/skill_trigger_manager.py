from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable
from itertools import combinations

from fu_gm.equipment_catalog import get_equipment_example
from fu_gm.models import Character
from fu_gm.skill_library import has_skill_name, skill_rank


@dataclass(frozen=True)
class SkillTriggerEffect:
    """A resolved numeric effect produced by a skill trigger."""

    source: str
    amount: int
    note: str = ""
    resource: str = ""
    resource_cost: int = 0


@dataclass(frozen=True)
class SkillJudgementWindow:
    """A GM-facing reminder for skills that need table judgement."""

    skill: str
    timing: str
    guidance: str


@dataclass(frozen=True)
class SkillRuleEvent:
    name: str
    actor: Character
    target: Character | None = None
    context: dict[str, object] = field(default_factory=dict)


@dataclass
class SkillEventResult:
    effects: list[SkillTriggerEffect] = field(default_factory=list)
    windows: list[dict[str, object]] = field(default_factory=list)
    facts: list[dict[str, object]] = field(default_factory=list)

    def merge(self, other: "SkillEventResult") -> None:
        self.effects.extend(other.effects)
        self.windows.extend(other.windows)
        self.facts.extend(other.facts)


class SkillEventBus:
    """Small synchronous rule bus with deterministic handler order."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable[[SkillRuleEvent], SkillEventResult]]] = {}

    def register(self, event_name: str, handler: Callable[[SkillRuleEvent], SkillEventResult]) -> None:
        handlers = self._handlers.setdefault(event_name, [])
        if handler not in handlers:
            handlers.append(handler)

    def emit(self, event: SkillRuleEvent) -> SkillEventResult:
        result = SkillEventResult()
        for handler in tuple(self._handlers.get(event.name, [])):
            result.merge(handler(event))
        return result

    def manifest(self) -> dict[str, int]:
        return {event: len(handlers) for event, handlers in sorted(self._handlers.items())}


GM_JUDGEMENT_WINDOWS: tuple[SkillJudgementWindow, ...] = (
    SkillJudgementWindow(
        "奥灵回响",
        "主动遣散非本回合召唤的奥灵后",
        "若装备魔法类武器，询问是否顺势施放总 MP 不高于技能等级×5 的法术。",
    ),
    SkillJudgementWindow(
        "痛楚",
        "对有羁绊的生物造成伤害后",
        "每个回合限一次，恢复技能等级×2 的 HP 与 MP；需要确认目标确有羁绊。",
    ),
    SkillJudgementWindow(
        "同源之毒",
        "拟兽使法术造成伤害后",
        "命中者中与模仿对象同物种的生物被施加中毒；需要 GM 确认物种来源。",
    ),
    SkillJudgementWindow(
        "苦痛教训",
        "另一个生物让你失去 HP 后",
        "可立即对来源执行调查顺势行动，检定获得技能等级修正；同一对象资料仍只能调查一次。",
    ),
    SkillJudgementWindow(
        "不屈意志",
        "消耗至少 1 点物语点后",
        "选择额外恢复 HP、恢复 MP 或解除一种异常状态。",
    ),
    SkillJudgementWindow(
        "死战不退",
        "执行防御且不掩护他人时",
        "恢复与最高羁绊强度相关的 HP，并临时提高力量或意志骰级。",
    ),
    SkillJudgementWindow(
        "保镖",
        "执行防御并掩护他人时",
        "被掩护者获得全伤害抵抗直到你的下个回合开始。",
    ),
    SkillJudgementWindow(
        "予以信任",
        "另一名能听见你的 PC 检定后",
        "可消耗物语点援用对方特质或羁绊帮其重掷/加值；若你对其有羁绊，对方恢复 MP。",
    ),
    SkillJudgementWindow(
        "阴狠手段",
        "攻击命中唯一目标且目标有异常状态时",
        "追加技能等级加目标异常状态数量的伤害；需要确认目标数量与异常状态。",
    ),
    SkillJudgementWindow(
        "疾速身法",
        "冲突第一轮开始时",
        "若已消耗 10 MP，可顺势攻击或顺势妨碍/推进目标，并获得技能等级修正。",
    ),
    SkillJudgementWindow(
        "快速评估",
        "冲突开始时",
        "可消耗 MP 揭示敌人特质或指定伤害类型相性；适合在第一轮前提醒。",
    ),
    SkillJudgementWindow(
        "鹰眼",
        "执行防御且不掩护他人时",
        "选择下次远程攻击额外伤害，或立即以弓/枪顺势攻击且高值视为 0。",
    ),
    SkillJudgementWindow(
        "治愈之力",
        "装备魔法武器并对盟友施法时",
        "每个盟友额外恢复 HP；该治疗与触发法术分开结算。",
    ),
    SkillJudgementWindow(
        "法术支援",
        "装备魔法武器并对盟友施法时",
        "可让一名有羁绊的目标在本场景下一次检定获得羁绊强度修正。",
    ),
    SkillJudgementWindow(
        "应急用品",
        "危机状态下自己的回合内",
        "每个冲突场景限一次，可额外执行一次消耗物资行动。",
    ),
    SkillJudgementWindow(
        "药剂雨",
        "制造单体恢复药剂时",
        "可额外影响至多技能等级个生物，但每个目标恢复量减半。",
    ),
)


SKILL_TRIGGER_EVENTS: dict[str, str] = {
    "奥灵回响": "arcanum_dismissed",
    "痛楚": "after_deal_damage",
    "灵智回流": "after_receive_damage",
    "同源之毒": "after_chimerist_spell_damage",
    "苦痛教训": "after_receive_damage",
    "不屈意志": "after_spend_fabula",
    "死战不退": "after_guard_without_cover",
    "保镖": "after_guard_with_cover",
    "予以信任": "after_ally_check",
    "阴狠手段": "after_single_target_hit",
    "疾速身法": "conflict_start",
    "快速评估": "conflict_start",
    "鹰眼": "after_guard_without_cover",
    "治愈之力": "after_ally_spell",
    "法术支援": "after_ally_spell",
    "应急用品": "turn_start",
    "药剂雨": "after_craft_healing_potion",
}


# These skills have context-aware handlers below.  Keeping the old generic GM
# reminder as well would create two choices for the same trigger.
_STRUCTURED_TRIGGER_SKILLS = {
    "奥灵回响",
    "痛楚",
    "同源之毒",
    "苦痛教训",
    "不屈意志",
    "死战不退",
    "保镖",
    "予以信任",
    "阴狠手段",
    "疾速身法",
    "快速评估",
    "鹰眼",
    "治愈之力",
    "法术支援",
    "应急用品",
    "药剂雨",
}


def gm_judgement_windows() -> tuple[SkillJudgementWindow, ...]:
    return GM_JUDGEMENT_WINDOWS


def gm_judgement_windows_for(actor: Character) -> list[SkillJudgementWindow]:
    owned = set(actor.skills) | set(actor.hero_skills)
    return [window for window in GM_JUDGEMENT_WINDOWS if has_skill_name(owned, window.skill)]


class SkillTriggerManager:
    """Centralizes passive skill triggers that are safe to resolve mechanically."""

    LIFECYCLE_EVENTS: tuple[str, ...] = (
        "session_start",
        "scene_start",
        "conflict_start",
        "turn_start",
        "prepare_spell",
        "before_check",
        "after_check",
        "before_damage",
        "after_deal_damage",
        "after_single_target_hit",
        "after_receive_damage",
        "after_chimerist_spell_damage",
        "enter_crisis",
        "after_guard_with_cover",
        "after_guard_without_cover",
        "after_ally_spell",
        "after_ally_check",
        "after_spell_damage",
        "after_clock_check",
        "after_spend_fabula",
        "travel_roll",
        "after_craft_healing_potion",
        "arcanum_dismissed",
        "scene_end",
        "session_end",
    )

    def __init__(self) -> None:
        self.event_bus = SkillEventBus()
        self.event_bus.register("before_damage", self._event_before_damage)
        self.event_bus.register("prepare_spell", self._event_prepare_spell)
        self.event_bus.register("before_check", self._event_before_check)
        self.event_bus.register("after_check", self._event_after_check)
        self.event_bus.register("after_clock_check", self._event_after_clock_check)
        self.event_bus.register("after_spell_damage", self._event_after_spell_damage)
        self.event_bus.register(
            "after_chimerist_spell_damage",
            self._event_after_chimerist_spell_damage,
        )
        self.event_bus.register("after_receive_damage", self._event_after_receive_damage)
        self.event_bus.register("after_deal_damage", self._event_after_deal_damage)
        self.event_bus.register("after_single_target_hit", self._event_after_single_target_hit)
        self.event_bus.register("travel_roll", self._event_travel_roll)
        self.event_bus.register("after_ally_spell", self._event_after_ally_spell)
        self.event_bus.register("enter_crisis", self._event_enter_crisis)
        self.event_bus.register("after_spend_fabula", self._event_after_spend_fabula)
        self.event_bus.register("after_guard_with_cover", self._event_after_guard_with_cover)
        self.event_bus.register("after_guard_without_cover", self._event_after_guard_without_cover)
        self.event_bus.register("conflict_start", self._event_conflict_start)
        self.event_bus.register("turn_start", self._event_turn_start)
        self.event_bus.register("arcanum_dismissed", self._event_arcanum_dismissed)
        self.event_bus.register("after_ally_check", self._event_after_ally_check)
        self.event_bus.register("after_craft_healing_potion", self._event_after_craft_healing_potion)
        self.event_bus.register("session_start", self._event_session_start)
        self.event_bus.register("scene_start", self._event_scene_start)
        self.event_bus.register("scene_end", self._event_scene_end)
        self.event_bus.register("session_end", self._event_session_end)
        for event_name in sorted(set(SKILL_TRIGGER_EVENTS.values())):
            self.event_bus.register(event_name, self._event_judgement_windows)

    def emit(
        self,
        event_name: str,
        actor: Character,
        *,
        target: Character | None = None,
        **context: object,
    ) -> SkillEventResult:
        return self.event_bus.emit(
            SkillRuleEvent(
                name=str(event_name),
                actor=actor,
                target=target,
                context=dict(context),
            )
        )

    def lifecycle_manifest(self) -> dict[str, object]:
        registered = self.event_bus.manifest()
        return {
            "events": list(self.LIFECYCLE_EVENTS),
            "registered_handlers": registered,
            "unhandled_events": [event for event in self.LIFECYCLE_EVENTS if event not in registered],
        }

    def _event_judgement_windows(self, event: SkillRuleEvent) -> SkillEventResult:
        windows = [
            window
            for window in self.judgement_windows_for_event(event.actor, event.name)
            if str(window.get("skill") or "") not in _STRUCTURED_TRIGGER_SKILLS
        ]
        return SkillEventResult(windows=windows)

    def _event_before_damage(self, event: SkillRuleEvent) -> SkillEventResult:
        return SkillEventResult(
            effects=self.damage_bonus_effects(
                event.actor,
                is_spell=bool(event.context.get("is_spell", False)),
                is_melee=bool(event.context.get("is_melee", True)),
            )
        )

    def _event_prepare_spell(self, event: SkillRuleEvent) -> SkillEventResult:
        if not bool(event.context.get("attack_spell", False)):
            return SkillEventResult()
        effects: list[SkillTriggerEffect] = []
        magic_weapon = bool(event.context.get("magic_weapon_equipped", False))
        barrage_rank = skill_rank(event.actor.skills, "魔法炮击")
        if magic_weapon and barrage_rank > 0:
            effects.append(
                SkillTriggerEffect(
                    "魔法炮击",
                    barrage_rank * 2,
                    "装备魔法类武器施放攻击性法术。",
                    resource="check_modifier",
                )
            )
        extra_mp = max(0, int(event.context.get("extra_mp", 0) or 0))
        cataclysm_rank = skill_rank(event.actor.skills, "天灾骤降")
        if (
            magic_weapon
            and cataclysm_rank > 0
            and bool(event.context.get("instant_spell", False))
            and extra_mp > 0
        ):
            effects.append(
                SkillTriggerEffect(
                    "天灾骤降",
                    (extra_mp // 10) * 5,
                    "额外精神值转化为法术伤害。",
                    resource="damage_bonus",
                    resource_cost=extra_mp,
                )
            )
        return SkillEventResult(effects=effects)

    def _event_before_check(self, event: SkillRuleEvent) -> SkillEventResult:
        return SkillEventResult(
            effects=self.check_modifier_effects(
                event.actor,
                attributes=[str(value) for value in event.context.get("attributes", [])],
                is_open_check=bool(event.context.get("is_open_check", False)),
            )
        )

    def _event_after_check(self, event: SkillRuleEvent) -> SkillEventResult:
        outcome = event.context.get("outcome")
        if (
            not has_skill_name(event.actor.skills, "幸运七")
            or "scene:skill:幸运七" in event.actor.trigger_cooldowns
            or outcome is None
            or bool(getattr(outcome, "success", False))
            or bool(getattr(outcome, "critical_success", False))
        ):
            return SkillEventResult()
        dice = list(getattr(outcome, "dice", []) or [])
        return SkillEventResult(
            windows=[
                {
                    "kind": "skill_judgement",
                    "label": "幸运七",
                    "actor": event.actor.name,
                    "timing": "检定后、结果定稿前",
                    "guidance": (
                        f"本场景可用一次【幸运七】：当前幸运数字为 {event.actor.lucky_number}，"
                        "选择一枚骰子替换；被替换的点数会成为新的幸运数字。"
                    ),
                    "action_type": "Skill",
                    "options": [
                        {
                            "skill_name": "幸运七",
                            "die_index": index + 1,
                            "current_roll": rolled,
                            "replacement": event.actor.lucky_number,
                        }
                        for index, (_size, rolled) in enumerate(dice)
                    ],
                    "priority": "normal",
                }
            ]
        )

    @staticmethod
    def _event_session_start(event: SkillRuleEvent) -> SkillEventResult:
        event.actor.lucky_number = 7
        event.actor.trigger_cooldowns = {
            key
            for key in event.actor.trigger_cooldowns
            if not key.startswith("session:") and not key.startswith("scene:")
        }
        return SkillEventResult(
            facts=[{"event": "session_start", "lucky_number": event.actor.lucky_number}]
        )

    @staticmethod
    def _event_scene_start(event: SkillRuleEvent) -> SkillEventResult:
        before = set(event.actor.trigger_cooldowns)
        event.actor.trigger_cooldowns = {
            key for key in before if not key.startswith("scene:")
        }
        return SkillEventResult(
            facts=[{"event": "scene_start", "reset_count": len(before - event.actor.trigger_cooldowns)}]
        )

    @staticmethod
    def _event_scene_end(event: SkillRuleEvent) -> SkillEventResult:
        before = set(event.actor.trigger_cooldowns)
        event.actor.trigger_cooldowns = {
            key for key in before if not key.startswith("scene:")
        }
        return SkillEventResult(
            facts=[{"event": "scene_end", "reset_count": len(before - event.actor.trigger_cooldowns)}]
        )

    @staticmethod
    def _event_session_end(event: SkillRuleEvent) -> SkillEventResult:
        return SkillEventResult(facts=[{"event": "session_end"}])

    def _event_after_clock_check(self, event: SkillRuleEvent) -> SkillEventResult:
        return SkillEventResult(
            effects=self.clock_bonus_effects(
                event.actor,
                silver_tongue_mp=int(event.context.get("silver_tongue_mp", 0) or 0),
                arcanum_resonance=bool(event.context.get("arcanum_resonance", False)),
            )
        )

    def _event_after_spell_damage(self, event: SkillRuleEvent) -> SkillEventResult:
        return SkillEventResult(
            effects=self.spell_damage_resource_effects(
                event.actor,
                damaged_targets=int(event.context.get("damaged_targets", 0) or 0),
            )
        )

    @staticmethod
    def _event_after_chimerist_spell_damage(event: SkillRuleEvent) -> SkillEventResult:
        if not has_skill_name(event.actor.skills, "同源之毒"):
            return SkillEventResult()
        origin_species = str(event.context.get("origin_species") or "").strip()
        damaged_targets = event.context.get("damaged_targets")
        if not origin_species or not isinstance(damaged_targets, list):
            return SkillEventResult()
        facts: list[dict[str, object]] = []
        for item in damaged_targets:
            if not isinstance(item, dict):
                continue
            target_name = str(item.get("target") or "").strip()
            target_species = str(item.get("species") or "").strip()
            if target_name and target_species == origin_species:
                facts.append(
                    {
                        "source": "同源之毒",
                        "effect": "apply_status",
                        "target": target_name,
                        "status": "poisoned",
                    }
                )
        return SkillEventResult(facts=facts)

    def _event_after_receive_damage(self, event: SkillRuleEvent) -> SkillEventResult:
        rank = skill_rank(event.actor.skills, "灵智回流")
        hp_lost = int(event.context.get("hp_lost", 0) or 0)
        result = SkillEventResult()
        if rank > 0 and hp_lost > 0:
            result.effects.append(
                SkillTriggerEffect(
                    source="灵智回流",
                    amount=rank * 2,
                    resource="mp",
                    note="受到伤害后恢复精神值。",
                )
            )
        lesson_rank = skill_rank(event.actor.skills, "苦痛教训")
        source_name = str(event.context.get("source_name") or "").strip()
        if lesson_rank > 0 and hp_lost > 0 and source_name:
            result.windows.append(
                {
                    "kind": "skill_parameter",
                    "label": "苦痛教训",
                    "actor": event.actor.name,
                    "guidance": f"是否立即调查造成伤害的【{source_name}】？本次检定获得 +{lesson_rank} 修正。",
                    "required_parameter": "choice",
                    "options": [
                        {"choice": "investigate", "target": source_name, "modifier": lesson_rank},
                        {"choice": "decline"},
                    ],
                    "blocking": True,
                }
            )
        return result

    def _event_after_deal_damage(self, event: SkillRuleEvent) -> SkillEventResult:
        rank = skill_rank(event.actor.skills, "痛楚")
        bonded = bool(event.target and event.actor.bond_strength_with(event.target.name) > 0)
        if rank <= 0 or not bonded or not bool(event.context.get("once_per_turn_available", True)):
            return SkillEventResult()
        amount = rank * 2
        return SkillEventResult(
            effects=[
                SkillTriggerEffect("痛楚", amount, "伤害羁绊对象后恢复生命值。", resource="hp"),
                SkillTriggerEffect("痛楚", amount, "伤害羁绊对象后恢复精神值。", resource="mp"),
            ]
        )

    def _event_after_single_target_hit(self, event: SkillRuleEvent) -> SkillEventResult:
        rank = skill_rank(event.actor.skills, "阴狠手段")
        statuses = int(event.context.get("target_status_count", 0) or 0)
        if rank <= 0 or statuses <= 0 or not bool(event.context.get("single_target", True)):
            return SkillEventResult()
        return SkillEventResult(
            effects=[SkillTriggerEffect("阴狠手段", rank + statuses, "唯一目标正受异常状态影响。")]
        )

    def _event_travel_roll(self, event: SkillRuleEvent) -> SkillEventResult:
        rank = skill_rank(event.actor.skills, "充足补给")
        treasure_rank = skill_rank(event.actor.skills, "宝物猎人")
        result = SkillEventResult()
        if rank > 0:
            result.effects.append(
                SkillTriggerEffect("充足补给", rank, "每次旅行掷骰后恢复物资点。", resource="inventory_points")
            )
        if treasure_rank > 0:
            result.facts.append(
                {
                    "source": "宝物猎人",
                    "discovery_threshold": treasure_rank + 1,
                    "roll": int(event.context.get("roll", 0) or 0),
                }
            )
        return result

    def _event_after_ally_spell(self, event: SkillRuleEvent) -> SkillEventResult:
        if not bool(event.context.get("magic_weapon_equipped")):
            return SkillEventResult()
        target_names = [str(name) for name in event.context.get("target_names", []) if str(name)]
        rank = skill_rank(event.actor.skills, "治愈之力")
        result = SkillEventResult()
        if rank > 0 and int(event.context.get("ally_targets", 0) or 0) > 0:
            amount = 3 + rank * len(event.actor.bonds)
            result.effects.append(
                SkillTriggerEffect("治愈之力", amount, "每名盟友获得一次独立治疗。", resource="hp")
            )
        bonded_targets = [name for name in target_names if event.actor.bond_strength_with(name) > 0]
        if has_skill_name(event.actor.skills, "法术支援") and bonded_targets:
            result.windows.append(
                {
                    "kind": "skill_parameter",
                    "label": "法术支援",
                    "actor": event.actor.name,
                    "guidance": "选择一名与你有羁绊的法术目标，令其本场景下一次检定获得羁绊强度修正；也可以不发动。",
                    "required_parameter": "target",
                    "options": [
                        {
                            "choice": "support",
                            "target": name,
                            "modifier": event.actor.bond_strength_with(name),
                        }
                        for name in bonded_targets
                    ]
                    + [{"choice": "decline"}],
                    "blocking": True,
                }
            )
        return result

    def _event_enter_crisis(self, event: SkillRuleEvent) -> SkillEventResult:
        facts: list[dict[str, object]] = []
        windows: list[dict[str, object]] = []
        if has_skill_name(event.actor.skills, "身负黑血"):
            facts.append({"source": "身负黑血", "resistances": ["dark", "poison"]})
        targets = [str(name) for name in event.context.get("visible_targets", []) if str(name)]
        targets = [name for name in targets if event.actor.bond_strength_with(name) <= 0]
        if (
            has_skill_name(event.actor.skills, "黑暗之心")
            and "scene:skill:黑暗之心" not in event.actor.trigger_cooldowns
            and targets
        ):
            windows.append(
                {
                    "kind": "skill_parameter",
                    "label": "黑暗之心",
                    "actor": event.actor.name,
                    "guidance": "你首次进入危机状态。可选择一个尚无羁绊的可见生物，建立憎恨羁绊；也可以不发动。",
                    "required_parameter": "target",
                    "options": [{"choice": "hate_bond", "target": name} for name in targets]
                    + [{"choice": "decline"}],
                    "blocking": True,
                }
            )
        return SkillEventResult(facts=facts, windows=windows)

    def _event_after_spend_fabula(self, event: SkillRuleEvent) -> SkillEventResult:
        rank = skill_rank(event.actor.skills, "不屈意志")
        if rank <= 0 or int(event.context.get("amount_spent", 0) or 0) <= 0:
            return SkillEventResult()
        options: list[dict[str, object]] = [
            {"choice": "recover_hp", "amount": rank * 5},
            {"choice": "recover_mp", "amount": rank * 5},
        ]
        options.extend({"choice": "clear_status", "status": status.value} for status in event.actor.statuses)
        return SkillEventResult(
            windows=[
                {
                    "kind": "skill_parameter",
                    "label": "不屈意志",
                    "actor": event.actor.name,
                    "guidance": f"【不屈意志】触发：恢复 {rank * 5} HP、恢复 {rank * 5} MP，或解除一种异常状态。",
                    "required_parameter": "choice",
                    "options": options,
                    "blocking": True,
                }
            ]
        )

    def _event_after_guard_with_cover(self, event: SkillRuleEvent) -> SkillEventResult:
        if not has_skill_name(event.actor.skills, "保镖") or event.target is None:
            return SkillEventResult()
        return SkillEventResult(
            facts=[
                {
                    "source": "保镖",
                    "effect": "all_damage_resistance",
                    "target": event.target.name,
                }
            ]
        )

    def _event_after_guard_without_cover(self, event: SkillRuleEvent) -> SkillEventResult:
        result = SkillEventResult()
        stand_rank = skill_rank(event.actor.skills, "死战不退")
        if stand_rank > 0:
            highest_bond = max((bond.strength for bond in event.actor.bonds), default=0)
            if highest_bond > 0:
                result.effects.append(
                    SkillTriggerEffect("死战不退", stand_rank * highest_bond, "防御时依靠最强羁绊恢复生命值。", resource="hp")
                )
            result.windows.append(
                {
                    "kind": "skill_parameter",
                    "label": "死战不退",
                    "actor": event.actor.name,
                    "guidance": "选择力量或意志；所选属性骰提升一级，持续到你的下个回合结束。",
                    "required_parameter": "attribute",
                    "options": [
                        {"choice": "attribute", "attribute": "MIG"},
                        {"choice": "attribute", "attribute": "WLP"},
                    ],
                    "blocking": True,
                }
            )
        hawkeye_rank = skill_rank(event.actor.skills, "鹰眼")
        if hawkeye_rank > 0:
            result.windows.append(
                {
                    "kind": "skill_parameter",
                    "label": "鹰眼",
                    "actor": event.actor.name,
                    "guidance": "选择：本场景下一次远程攻击额外造成伤害，或立即以弓/枪进行一次高值视为 0 的顺势攻击。",
                    "required_parameter": "choice",
                    "options": [
                        {"choice": "next_ranged_damage", "amount": hawkeye_rank * 3},
                        {"choice": "immediate_ranged_attack", "high_roll_zero": True},
                        {"choice": "decline"},
                    ],
                    "blocking": True,
                }
            )
        return result

    def _event_conflict_start(self, event: SkillRuleEvent) -> SkillEventResult:
        windows: list[dict[str, object]] = []
        quick_rank = skill_rank(event.actor.skills, "疾速身法")
        if quick_rank > 0 and event.actor.mp >= 10:
            windows.append(
                {
                    "kind": "skill_parameter",
                    "label": "疾速身法",
                    "actor": event.actor.name,
                    "guidance": "冲突开始时可消耗 10 MP，选择顺势攻击，或顺势妨碍/推进目标；检定获得技能等级修正。",
                    "required_parameter": "choice",
                    "options": [
                        {"choice": "attack", "modifier": quick_rank, "mp_cost": 10},
                        {"choice": "hinder_or_objective", "modifier": quick_rank, "mp_cost": 10},
                        {"choice": "decline"},
                    ],
                    "blocking": True,
                }
            )
        assessment_rank = skill_rank(event.actor.skills, "快速评估")
        if assessment_rank > 0 and event.actor.mp >= 5:
            windows.append(
                {
                    "kind": "skill_parameter",
                    "label": "快速评估",
                    "actor": event.actor.name,
                    "guidance": "冲突开始时可每消耗 5 MP 揭示一个可见生物的一项特质或一种伤害相性。",
                    "required_parameter": "assessment",
                    "options": [{"choice": "declare_assessment"}, {"choice": "decline"}],
                    "blocking": True,
                }
            )
        return SkillEventResult(windows=windows)

    def _event_turn_start(self, event: SkillRuleEvent) -> SkillEventResult:
        if (
            not has_skill_name(event.actor.skills, "应急用品")
            or not event.actor.in_crisis
            or "scene:skill:应急用品" in event.actor.trigger_cooldowns
        ):
            return SkillEventResult()
        return SkillEventResult(
            windows=[
                {
                    "kind": "skill_parameter",
                    "label": "应急用品",
                    "actor": event.actor.name,
                    "guidance": "你处于危机状态；本冲突可额外执行一次消耗物资行动。现在发动，还是保留到之后的回合？",
                    "required_parameter": "choice",
                    "options": [{"choice": "use_inventory_action"}, {"choice": "decline"}],
                    "blocking": False,
                }
            ]
        )

    def _event_arcanum_dismissed(self, event: SkillRuleEvent) -> SkillEventResult:
        rank = skill_rank(event.actor.skills, "奥灵回响")
        if (
            rank <= 0
            or not bool(event.context.get("active_dismissal"))
            or bool(event.context.get("summoned_this_turn"))
            or not bool(event.context.get("magic_weapon_equipped"))
        ):
            return SkillEventResult()
        return SkillEventResult(
            windows=[
                {
                    "kind": "skill_parameter",
                    "label": "奥灵回响",
                    "actor": event.actor.name,
                    "guidance": f"遣散效果结算完毕。可顺势施放总 MP 不高于 {rank * 5} 的法术。",
                    "required_parameter": "spell",
                    "options": [{"choice": "cast_spell", "max_mp": rank * 5}, {"choice": "decline"}],
                    "blocking": True,
                }
            ]
        )

    def _event_after_ally_check(self, event: SkillRuleEvent) -> SkillEventResult:
        rank = skill_rank(event.actor.skills, "予以信任")
        outcome = event.context.get("outcome")
        if (
            rank <= 0
            or event.target is None
            or event.actor.fabula_points <= 0
            or not bool(event.context.get("can_hear", True))
            or not bool(event.context.get("transaction_available"))
            or bool(getattr(outcome, "success", False))
            or bool(getattr(outcome, "fumble", False))
        ):
            return SkillEventResult()
        options: list[dict[str, object]] = [{"choice": "decline"}]
        for trait in (event.target.identity, event.target.theme, event.target.origin):
            clean_trait = str(trait or "").strip()
            if clean_trait:
                options.insert(
                    -1,
                    {
                        "choice": "assist_trait",
                        "trait": clean_trait,
                        "target": event.target.name,
                    },
                )
        for bond in event.target.bonds:
            if bond.strength <= 0:
                continue
            options.insert(
                -1,
                {
                    "choice": "assist_bond",
                    "bond_target": bond.target,
                    "strength": bond.strength,
                    "target": event.target.name,
                },
            )
        if len(options) == 1:
            return SkillEventResult()
        return SkillEventResult(
            windows=[
                {
                    "kind": "skill_parameter",
                    "label": "予以信任",
                    "actor": event.actor.name,
                    "guidance": f"是否发动【予以信任】帮助【{event.target.name}】？",
                    "required_parameter": "choice",
                    "options": options,
                    "blocking": True,
                }
            ]
        )

    def _event_after_craft_healing_potion(self, event: SkillRuleEvent) -> SkillEventResult:
        rank = skill_rank(event.actor.skills, "药剂雨")
        if rank <= 0 or not bool(event.context.get("single_target_healing")):
            return SkillEventResult()
        candidates = [
            str(name)
            for name in event.context.get("available_targets", [])
            if str(name).strip()
        ]
        if not candidates:
            return SkillEventResult()
        target_options: list[dict[str, object]] = []
        for count in range(1, min(rank, len(candidates)) + 1):
            for selected in combinations(candidates, count):
                target_options.append(
                    {
                        "choice": "select_targets",
                        "targets": list(selected),
                        "max_extra_targets": rank,
                    }
                )
        return SkillEventResult(
            windows=[
                {
                    "kind": "skill_parameter",
                    "label": "药剂雨",
                    "actor": event.actor.name,
                    "guidance": f"可让药剂额外影响至多 {rank} 个生物，但每个目标的恢复量减半。",
                    "required_parameter": "targets",
                    "options": target_options + [{"choice": "decline"}],
                    "blocking": True,
                }
            ]
        )

    def judgement_windows_for_event(self, actor: Character, event: str) -> list[dict[str, object]]:
        windows: list[dict[str, object]] = []
        for window in gm_judgement_windows_for(actor):
            if SKILL_TRIGGER_EVENTS.get(window.skill) != event:
                continue
            windows.append(
                {
                    "skill": window.skill,
                    "event": event,
                    "timing": window.timing,
                    "guidance": window.guidance,
                }
            )
        return windows

    def hook_manifest(self, actor: Character) -> dict[str, list[str]]:
        manifest: dict[str, list[str]] = {}
        for skill in [*actor.skills.keys(), *actor.hero_skills]:
            event = SKILL_TRIGGER_EVENTS.get(skill)
            if event:
                manifest.setdefault(event, []).append(skill)
        return manifest

    def damage_bonus_effects(self, actor: Character, *, is_spell: bool, is_melee: bool = True) -> list[SkillTriggerEffect]:
        effects: list[SkillTriggerEffect] = []

        if has_skill_name(actor.hero_skills, "绝处逢生") and actor.statuses:
            effects.append(
                SkillTriggerEffect(
                    source="绝处逢生",
                    amount=len(actor.statuses) * 2,
                    note="每项异常状态使造成的伤害提高 2 点。",
                )
            )

        adrenaline_rank = skill_rank(actor.skills, "肾上腺素")
        if adrenaline_rank > 0 and actor.in_crisis:
            effects.append(
                SkillTriggerEffect(
                    source="肾上腺素",
                    amount=adrenaline_rank * 2,
                    note="危机状态下造成额外伤害。",
                )
            )

        heroic_bonus = 10 if actor.level >= 40 else 5
        if is_spell and has_skill_name(actor.hero_skills, "强效法术"):
            effects.append(SkillTriggerEffect("强效法术", heroic_bonus, "法术造成额外伤害。"))
        elif not is_spell and is_melee and has_skill_name(actor.hero_skills, "猛力打击"):
            effects.append(SkillTriggerEffect("猛力打击", heroic_bonus, "近战攻击造成额外伤害。"))
        elif not is_spell and not is_melee and has_skill_name(actor.hero_skills, "强力射击"):
            effects.append(SkillTriggerEffect("强力射击", heroic_bonus, "远程攻击造成额外伤害。"))

        return effects

    def damage_bonus(self, actor: Character, *, is_spell: bool, is_melee: bool = True) -> int:
        return sum(effect.amount for effect in self.damage_bonus_effects(actor, is_spell=is_spell, is_melee=is_melee))

    def check_modifier_effects(
        self,
        actor: Character,
        *,
        attributes: list[str],
        is_open_check: bool = False,
    ) -> list[SkillTriggerEffect]:
        effects: list[SkillTriggerEffect] = []
        if has_skill_name(actor.hero_skills, "绝处逢生") and actor.statuses:
            effects.append(
                SkillTriggerEffect(
                    "绝处逢生",
                    len(actor.statuses),
                    "每项异常状态使所有检定获得 +1 修正。",
                )
            )
        if is_open_check and attributes == ["INS", "INS"]:
            rank = skill_rank(actor.skills, "知识就是力量")
            if rank > 0:
                effects.append(SkillTriggerEffect("知识就是力量", rank, "【洞察+洞察】开放检定获得修正。"))
        return effects

    def clock_bonus_effects(
        self,
        actor: Character,
        *,
        silver_tongue_mp: int = 0,
        arcanum_resonance: bool = False,
    ) -> list[SkillTriggerEffect]:
        effects: list[SkillTriggerEffect] = []

        silver_tongue_rank = skill_rank(actor.skills, "巧舌如簧")
        if silver_tongue_rank > 0 and silver_tongue_mp > 0:
            mp_to_spend = min(max(0, silver_tongue_mp), silver_tongue_rank * 20, actor.mp)
            segments = mp_to_spend // 20
            if segments > 0:
                effects.append(
                    SkillTriggerEffect(
                        source="巧舌如簧",
                        amount=segments,
                        note="语言、交涉、欺骗或威胁推进命刻。",
                        resource="mp",
                        resource_cost=segments * 20,
                    )
                )

        if arcanum_resonance and actor.active_arcanum and has_skill_name(actor.hero_skills, "奥灵共鸣"):
            effects.append(SkillTriggerEffect("奥灵共鸣", 1, "奥灵领域相关检定额外影响命刻。"))

        return effects

    def spell_damage_resource_effects(self, actor: Character, *, damaged_targets: int) -> list[SkillTriggerEffect]:
        if damaged_targets <= 0:
            return []
        rank = skill_rank(actor.skills, "摄能为食")
        if rank <= 0:
            return []
        if not self._has_chimerist_focus_weapon(actor):
            return []
        return [
            SkillTriggerEffect(
                source="摄能为食",
                amount=rank * 2,
                note="施法造成伤害后，因装备合适武器恢复 MP。",
                resource="mp",
            )
        ]

    def _has_chimerist_focus_weapon(self, actor: Character) -> bool:
        if self._is_red_robe_equipped(actor):
            return True
        item_name = actor.equipped_main_hand
        template_name = actor.equipment_templates.get(item_name, item_name)
        example = get_equipment_example(template_name)
        return bool(example is not None and example.category in {"魔法", "匕首", "链枷"})

    def has_magic_weapon(self, actor: Character) -> bool:
        if self._is_red_robe_equipped(actor):
            return True
        for item_name in (actor.equipped_main_hand, actor.equipped_off_hand):
            if not item_name:
                continue
            template_name = actor.equipment_templates.get(item_name, item_name)
            example = get_equipment_example(template_name)
            if example is not None and example.category == "魔法":
                return True
        return False

    def _is_red_robe_equipped(self, actor: Character) -> bool:
        for item_name in (actor.equipped_armor, actor.equipped_accessory):
            template_name = actor.equipment_templates.get(item_name, item_name)
            if template_name == "红色罩袍":
                return True
        return False
