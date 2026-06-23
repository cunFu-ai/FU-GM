from __future__ import annotations

from math import ceil

from fu_gm.components.character_manager import CharacterManager
from fu_gm.models import (
    PersistentChangeType,
    ProjectProgressResult,
    ProjectState,
    ProjectUse,
    ResourceChange,
    RitualPotency,
    RitualScope,
)
from fu_gm.skill_library import skill_rank


PROJECT_BASE_COSTS = {
    RitualPotency.MINOR: 100,
    RitualPotency.MODERATE: 200,
    RitualPotency.MAJOR: 400,
    RitualPotency.EXTREME: 800,
}

PROJECT_SCOPE_MULTIPLIERS = {
    RitualScope.INDIVIDUAL: 1,
    RitualScope.SMALL: 2,
    RitualScope.LARGE: 3,
    RitualScope.HUGE: 4,
}

PROJECT_USE_MULTIPLIERS = {
    ProjectUse.CONSUMABLE: 1,
    ProjectUse.PERMANENT: 5,
}


class ProjectManager:
    """处理造物使项目和自定义发明。"""

    def __init__(self, character_manager: CharacterManager) -> None:
        self.character_manager = character_manager
        self.projects: dict[str, ProjectState] = {}

    def estimate_cost(
        self,
        potency: RitualPotency,
        scope: RitualScope,
        use: ProjectUse,
        *,
        flaw: str = "",
    ) -> int:
        cost = PROJECT_BASE_COSTS[potency] * PROJECT_SCOPE_MULTIPLIERS[scope] * PROJECT_USE_MULTIPLIERS[use]
        if flaw:
            cost = cost * 3 // 4
        return max(1, cost)

    def required_progress_for_cost(self, material_cost: int) -> int:
        return max(1, ceil(material_cost / 100))

    def start_project(
        self,
        *,
        inventor: str,
        name: str,
        potency: RitualPotency,
        scope: RitualScope,
        use: ProjectUse,
        effect: str,
        output_type: PersistentChangeType | str | None = None,
        owner: str = "",
        location: str = "",
        flaw: str = "",
        special_materials: list[str] | None = None,
        material_credit: int = 0,
        enforce_permission: bool = True,
    ) -> ProjectState:
        character = self.character_manager.get(inventor)
        if enforce_permission and "造物使" not in character.classes and "可发起项目" not in character.abilities:
            raise ValueError(f"{inventor} 不是造物使，不能发起项目。")
        material_cost = self.estimate_cost(potency, scope, use, flaw=flaw)
        visionary_credit = skill_rank(character.skills, "先见之明") * 100
        paid_cost = max(0, material_cost - max(0, material_credit) - visionary_credit)
        if character.zenit < paid_cost:
            raise ValueError(f"{inventor} 的泽尼特不足，项目需要支付 {paid_cost}Z。")
        self.character_manager.modify_resource(inventor, "zenit", -paid_cost)
        project = ProjectState(
            name=name,
            inventor=inventor,
            potency=potency,
            scope=scope,
            use=use,
            effect=effect,
            material_cost=material_cost,
            required_progress=self.required_progress_for_cost(material_cost),
            output_type=self._resolve_output_type(output_type, use),
            owner=owner,
            location=location,
            flaw=flaw,
            special_materials=list(special_materials or []),
            notes=[
                f"总成本 {material_cost}Z，已支付 {paid_cost}Z。",
                *([f"缺陷：{flaw}"] if flaw else []),
            ],
        )
        self.projects[name] = project
        return project

    def _resolve_output_type(
        self,
        output_type: PersistentChangeType | str | None,
        use: ProjectUse,
    ) -> PersistentChangeType:
        if isinstance(output_type, PersistentChangeType):
            return output_type
        if output_type:
            return PersistentChangeType(output_type)
        if use == ProjectUse.CONSUMABLE:
            return PersistentChangeType.CONSUMABLE
        return PersistentChangeType.WORLD_FACT

    def hire_helpers(self, project_name: str, *, payer: str, count: int = 1) -> ResourceChange:
        if count < 1:
            raise ValueError("雇佣帮手数量必须至少为 1。")
        project = self.projects[project_name]
        cost = project.material_cost // 2 * count
        character = self.character_manager.get(payer)
        if character.zenit < cost:
            raise ValueError(f"{payer} 的泽尼特不足，雇佣 {count} 名帮手需要 {cost}Z。")
        before, after = self.character_manager.modify_resource(payer, "zenit", -cost)
        project.helpers += count
        return ResourceChange(
            target=payer,
            resource="zenit",
            amount=-cost,
            before=before,
            after=after,
            reason=f"为项目【{project_name}】雇佣 {count} 名帮手。",
        )

    def work_on_project(self, project_name: str, workers: list[str], *, days: int = 1) -> ProjectProgressResult:
        if days < 1:
            raise ValueError("项目推进至少需要 1 天。")
        project = self.projects[project_name]
        if project.completed:
            return ProjectProgressResult(
                project=project,
                workers=list(workers),
                progress_added=0,
                before=project.current_progress,
                after=project.current_progress,
                completed=True,
                summary=f"项目【{project.name}】已经完成。",
            )
        daily_progress = project.helpers
        for worker_name in workers:
            worker = self.character_manager.get(worker_name)
            daily_progress += 1
            if "造物使" in worker.classes:
                daily_progress += 1
            daily_progress += skill_rank(worker.skills, "先见之明")
        before = project.current_progress
        progress_added = daily_progress * days
        project.current_progress = min(project.required_progress, project.current_progress + progress_added)
        project.completed = project.current_progress >= project.required_progress
        summary = (
            f"项目【{project.name}】推进 {project.current_progress}/{project.required_progress}。"
            if not project.completed
            else f"项目【{project.name}】完成：{project.effect}"
        )
        return ProjectProgressResult(
            project=project,
            workers=list(workers),
            progress_added=progress_added,
            before=before,
            after=project.current_progress,
            completed=project.completed,
            summary=summary,
        )

