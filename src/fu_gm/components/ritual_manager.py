from __future__ import annotations

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.models import (
    Character,
    Clock,
    ClockChange,
    ResourceChange,
    RitualCastResult,
    RitualDiscipline,
    RitualPlan,
    RitualPotency,
    RitualScope,
    RollOutcome,
)
from fu_gm.skill_library import normalize_skill_reference_name, skill_rank


RITUAL_POTENCY_TABLE = {
    RitualPotency.MINOR: {"label": "轻微", "mp": 20, "dl": 7, "clock": 4},
    RitualPotency.MODERATE: {"label": "中等", "mp": 30, "dl": 10, "clock": 6},
    RitualPotency.MAJOR: {"label": "强大", "mp": 40, "dl": 13, "clock": 6},
    RitualPotency.EXTREME: {"label": "极强", "mp": 50, "dl": 16, "clock": 8},
}

RITUAL_SCOPE_MULTIPLIERS = {
    RitualScope.INDIVIDUAL: 1,
    RitualScope.SMALL: 2,
    RitualScope.LARGE: 3,
    RitualScope.HUGE: 4,
}

RITUAL_DISCIPLINE_ATTRIBUTES = {
    RitualDiscipline.ARCANISM: ["WLP", "WLP"],
    RitualDiscipline.CHIMERISM: ["INS", "WLP"],
    RitualDiscipline.ELEMENTALISM: ["INS", "WLP"],
    RitualDiscipline.ENTROPISM: ["INS", "WLP"],
    RitualDiscipline.RITUALISM: ["INS", "WLP"],
    RitualDiscipline.SPIRITISM: ["INS", "WLP"],
}

RITUAL_PERMISSION_SKILLS = {
    RitualDiscipline.ARCANISM: "奥灵系仪式",
    RitualDiscipline.CHIMERISM: "拟兽系仪式",
    RitualDiscipline.ELEMENTALISM: "元素系仪式",
    RitualDiscipline.ENTROPISM: "熵系仪式",
    RitualDiscipline.RITUALISM: "仪式系仪式",
    RitualDiscipline.SPIRITISM: "御魂系仪式",
}

RITUALISM_CLASSES = {"拟兽使", "元素使", "熵术士", "御魂使"}

FORBIDDEN_RITUAL_TAGS = {
    "direct_damage",
    "apply_status",
    "clear_status",
    "hp_change",
    "mp_change",
    "ip_change",
    "fabula_change",
    "ultima_change",
    "copy_spell",
    "copy_skill",
    "create_creature",
    "create_equipment",
    "permanent_power",
}


class RitualManager:
    """处理仪式魔法的成本、命刻和最终检定。"""

    def __init__(
        self,
        rules_engine: RulesEngine,
        character_manager: CharacterManager,
        clock_manager: ClockManager,
    ) -> None:
        self.rules_engine = rules_engine
        self.character_manager = character_manager
        self.clock_manager = clock_manager
        self.active_rituals: dict[str, RitualPlan] = {}

    def plan_ritual(
        self,
        *,
        caster: str,
        name: str,
        discipline: RitualDiscipline,
        potency: RitualPotency,
        scope: RitualScope,
        effect: str,
        attributes: list[str] | None = None,
        rare_material: str = "",
        forbidden_tags: list[str] | None = None,
        enforce_permission: bool = True,
    ) -> RitualPlan:
        character = self.character_manager.get(caster)
        if enforce_permission:
            self._ensure_can_perform(character, discipline)
        forbidden = [tag for tag in forbidden_tags or [] if tag in FORBIDDEN_RITUAL_TAGS]
        if forbidden:
            raise ValueError(f"仪式不能实现这些机械效果：{', '.join(forbidden)}。")

        potency_row = RITUAL_POTENCY_TABLE[potency]
        mp_cost = int(potency_row["mp"]) * RITUAL_SCOPE_MULTIPLIERS[scope]
        if rare_material:
            mp_cost //= 2
        chosen_attributes = attributes or list(RITUAL_DISCIPLINE_ATTRIBUTES[discipline])
        if (
            discipline == RitualDiscipline.CHIMERISM
            and skill_rank(character.skills, "拟兽系仪式") > 0
        ):
            fixed_attributes = self._fixed_chimerist_ritual_attributes(character)
            if attributes is not None and list(attributes) != fixed_attributes:
                raise ValueError(
                    "拟兽系仪式必须使用角色习得技能时固定选择的属性组合。"
                )
            chosen_attributes = fixed_attributes
        self._validate_attributes_for_discipline(discipline, chosen_attributes)
        clock_name = f"仪式：{name}"
        return RitualPlan(
            name=name,
            caster=caster,
            discipline=discipline,
            potency=potency,
            scope=scope,
            effect=effect,
            mp_cost=mp_cost,
            target_number=int(potency_row["dl"]),
            attributes=list(chosen_attributes),
            clock_segments=int(potency_row["clock"]),
            clock_name=clock_name,
            rare_material=rare_material,
            forbidden_tags=list(forbidden_tags or []),
        )

    def start_conflict_ritual(
        self,
        plan: RitualPlan,
        *,
        scene_id: str = "",
        turn_serial: int = 0,
    ) -> RitualPlan:
        if not plan.clock_name:
            plan.clock_name = f"仪式：{plan.name}"
        if plan.clock_name in self.active_rituals:
            raise ValueError(f"仪式【{plan.name}】已经启动，不能重复建立。")
        if self.clock_manager.exists(plan.clock_name):
            raise ValueError(
                f"命刻【{plan.clock_name}】已经存在，不能被新的仪式覆盖。"
            )
        if self.clock_manager.is_retired(plan.clock_name):
            raise ValueError(
                f"仪式【{plan.name}】已经结案；新的仪式需要使用不同名称。"
            )
        plan.scene_id = str(scene_id or "").strip()
        plan.started_turn_serial = max(0, int(turn_serial or 0))
        plan.ready_turn_serial = 0
        self.clock_manager.add(
            Clock(
                name=plan.clock_name,
                max_segments=plan.clock_segments,
                clock_type="ritual",
                stakes=f"完成【{plan.name}】的仪式准备。",
                completion_consequence="仪式准备完成，可以进行最终施法检定。",
                scope="scene",
                scene_id=plan.scene_id,
                owner=plan.caster,
                source="conflict_ritual",
            )
        )
        self.active_rituals[plan.clock_name] = plan
        return plan

    def contribute_to_ritual(
        self,
        clock_name: str,
        *,
        actor: str,
        attributes: list[str] | None = None,
        target_number: int | None = None,
        modifier: int = 0,
        direction: int = 1,
        spend_critical_opportunity: bool = False,
        reason: str = "推进仪式命刻",
        turn_serial: int = 0,
    ) -> tuple[RollOutcome, ClockChange]:
        character = self.character_manager.get(actor)
        plan = self.active_rituals.get(clock_name)
        clock = self.clock_manager.get(clock_name)
        if clock.current >= clock.max_segments or clock.status == "ready":
            raise ValueError(f"仪式命刻【{clock.name}】已经准备完成，应进行最终施法检定。")
        chosen_attributes = attributes or (plan.attributes if plan is not None else ["INS", "WLP"])
        effective_target_number = target_number if target_number is not None else (plan.target_number if plan is not None else 10)
        outcome = self.rules_engine.roll_check(
            actor=character,
            attributes=chosen_attributes,
            target_number=effective_target_number,
            modifier=modifier,
            reason=reason,
        )
        if outcome.success:
            delta = self.rules_engine.clock_segments_from_roll(
                outcome,
                spend_critical_opportunity=spend_critical_opportunity,
            ) * direction
        else:
            delta = 0
        before, after = self.clock_manager.advance(clock_name, delta)
        clock = self.clock_manager.get(clock_name)
        if (
            plan is not None
            and after >= clock.max_segments
            and plan.ready_turn_serial <= 0
        ):
            plan.ready_turn_serial = max(0, int(turn_serial or 0))
        actual_delta = after - before
        change = ClockChange(
            clock_name=clock.name,
            before=before,
            after=after,
            delta=actual_delta,
            max_segments=clock.max_segments,
            reason=reason,
            clock_type=clock.clock_type,
            stakes=clock.stakes,
            completion_consequence=clock.completion_consequence,
        )
        return outcome, change

    def mark_ready(self, clock_name: str, *, turn_serial: int = 0) -> RitualPlan:
        plan = self._resolve_plan(clock_name)
        clock = self.clock_manager.get(plan.clock_name)
        if clock.current < clock.max_segments:
            raise ValueError(f"仪式命刻【{clock.name}】尚未填满。")
        clock.status = "ready"
        if plan.ready_turn_serial <= 0:
            plan.ready_turn_serial = max(0, int(turn_serial or 0))
        return plan

    def finish_ritual(
        self,
        plan_or_clock_name: RitualPlan | str,
        *,
        note: str = "",
    ) -> RitualPlan:
        plan = self._resolve_plan(plan_or_clock_name)
        if self.clock_manager.exists(plan.clock_name):
            self.clock_manager.resolve(
                plan.clock_name,
                note=note or f"仪式【{plan.name}】已经完成最终施法检定。",
                archive=True,
            )
        self.active_rituals.pop(plan.clock_name, None)
        return plan

    def cancel_scene(
        self,
        scene_id: str,
        *,
        reason: str = "场景结束",
    ) -> list[RitualPlan]:
        target = str(scene_id or "").strip()
        cancelled: list[RitualPlan] = []
        for clock_name, plan in list(self.active_rituals.items()):
            if target and plan.scene_id and plan.scene_id != target:
                continue
            if self.clock_manager.exists(clock_name):
                self.clock_manager.abandon(
                    clock_name,
                    note=reason or "仪式准备随场景结束而中断。",
                )
            self.active_rituals.pop(clock_name, None)
            cancelled.append(plan)
        return cancelled

    def cast_ritual(
        self,
        plan_or_clock_name: RitualPlan | str,
        *,
        catastrophe: str = "仪式失控，GM 应让效果以危险、代价或威胁命刻的方式扭曲。",
        require_completed_clock: bool = False,
    ) -> RitualCastResult:
        plan = self._resolve_plan(plan_or_clock_name)
        if require_completed_clock:
            clock = self.clock_manager.get(plan.clock_name)
            if clock.current < clock.max_segments:
                raise ValueError(f"仪式命刻【{plan.clock_name}】尚未填满，不能完成仪式。")

        caster = self.character_manager.get(plan.caster)
        if caster.mp < plan.mp_cost:
            raise ValueError(f"{caster.name} 的 MP 不足，仪式需要 {plan.mp_cost} MP。")
        before, after = self.character_manager.modify_resource(caster.name, "mp", -plan.mp_cost)
        mp_change = ResourceChange(
            target=caster.name,
            resource="mp",
            amount=-plan.mp_cost,
            before=before,
            after=after,
            reason=f"执行仪式【{plan.name}】。",
        )
        roll = self.rules_engine.roll_check(
            actor=self.character_manager.get(plan.caster),
            attributes=plan.attributes,
            target_number=plan.target_number,
            reason=f"仪式检定：{plan.name}",
        )
        success = roll.success
        summary = (
            f"{caster.name} 完成仪式【{plan.name}】：{roll.total} 对抗难度等级 {plan.target_number}，"
            f"{'成功' if success else '失败'}。"
        )
        if success:
            summary += f" 效果：{plan.effect}"
        else:
            summary += f" 灾变：{catastrophe}"
        return RitualCastResult(
            plan=plan,
            roll=roll,
            mp_change=mp_change,
            success=success,
            catastrophe="" if success else catastrophe,
            summary=summary,
        )

    def _resolve_plan(self, plan_or_clock_name: RitualPlan | str) -> RitualPlan:
        if isinstance(plan_or_clock_name, RitualPlan):
            return plan_or_clock_name
        return self.active_rituals[plan_or_clock_name]

    def _ensure_can_perform(self, character: Character, discipline: RitualDiscipline) -> None:
        if discipline == RitualDiscipline.RITUALISM and RITUALISM_CLASSES.intersection(character.classes):
            return
        required_skill = RITUAL_PERMISSION_SKILLS[discipline]
        if skill_rank(character.skills, required_skill) > 0 or required_skill in character.abilities:
            return
        legacy_name = normalize_skill_reference_name(required_skill)
        raise ValueError(f"{character.name} 尚未掌握【{legacy_name}】，不能执行该学科的仪式。")

    def _validate_attributes_for_discipline(self, discipline: RitualDiscipline, attributes: list[str]) -> None:
        if len(attributes) != 2:
            raise ValueError("仪式检定必须使用两个属性。")
        if discipline == RitualDiscipline.CHIMERISM:
            valid = attributes in (["INS", "WLP"], ["MIG", "WLP"])
            if not valid:
                raise ValueError("拟兽系仪式只能使用【洞察+意志】或【力量+意志】。")
            return
        expected = RITUAL_DISCIPLINE_ATTRIBUTES[discipline]
        if attributes != expected:
            raise ValueError(f"{discipline.value} 仪式必须使用【{'+'.join(expected)}】。")

    @staticmethod
    def _fixed_chimerist_ritual_attributes(character: Character) -> list[str]:
        choices = list(character.skill_options.get("拟兽系仪式", []))
        if len(choices) != 1:
            raise ValueError(
                "【拟兽系仪式】尚未记录习得时固定选择的"
                "【洞察+意志】或【力量+意志】。"
            )
        choice = str(choices[0]).replace("【", "").replace("】", "").replace(" ", "")
        if choice in {"洞察+意志", "INS+WLP"}:
            return ["INS", "WLP"]
        if choice in {"力量+意志", "MIG+WLP"}:
            return ["MIG", "WLP"]
        raise ValueError(
            "【拟兽系仪式】记录的固定属性组合无效；"
            "只能选择【洞察+意志】或【力量+意志】。"
        )
