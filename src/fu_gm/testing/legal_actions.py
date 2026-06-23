from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fu_gm.http_server import FUGMHttpService
from fu_gm.testing.replay_models import LegalActionContext, ReplayScenario, ReplayStep


class LegalActionLayer:
    """Build a constrained action menu for synthetic players.

    This layer intentionally talks in Final Fabula terms rather than raw Python
    action names. The player simulator may phrase the action naturally, but it
    should not invent unsupported mechanics outside this menu.
    """

    def build(
        self,
        service: FUGMHttpService,
        scenario: ReplayScenario,
        step: ReplayStep,
    ) -> LegalActionContext:
        runtime = service.runtimes.get(scenario.campaign_id)
        if runtime is None:
            return LegalActionContext(
                stage_goal=step.stage_goal,
                legal_actions=["新建战役", "进入第零章"],
                notes=["当前还没有运行时，只能进行战役初始化或第零章开场。"],
            )

        app = runtime.app
        pcs = [character for character in app.character_manager.all() if "pc" in character.traits]
        enemies = [
            character
            for character in app.character_manager.all()
            if "enemy" in character.traits or "villain" in character.traits
        ]
        current_actor = app.conflict_manager.state.current_actor() or ""
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            current_actor=current_actor,
            conflict_active=app.conflict_manager.state.active,
            known_pcs=[character.name for character in pcs],
            known_enemies=[character.name for character in enemies],
            active_clocks=app.clock_manager.formatted(),
        )

        if step.kind.startswith("session_zero"):
            context.legal_actions = [
                "贡献世界事实",
                "提出地区或事件",
                "提出反派种子",
                "声明界限与帷幕",
                "创建或补全角色",
            ]
            context.notes.append("第零章允许共创世界，不需要消耗物语点。")
            return context

        actor = step.actor or self._speaker_character_guess(step.speaker, pcs)
        if context.conflict_active:
            if not current_actor:
                context.legal_actions = ["等待 GM 明确当前行动者"]
            elif actor and actor != current_actor:
                context.legal_actions = ["回合外等待", "给当前行动者建议", "声明预备想法但不结算"]
                context.notes.append(f"当前行动者是 {current_actor}，{actor} 不能结算消耗回合的行动。")
            elif actor and self._is_enemy_actor(actor, enemies):
                context.legal_actions = ["等待敌方行动"]
                context.notes.append("玩家不能替敌人行动。")
            else:
                context.legal_actions = [
                    "攻击",
                    "防御",
                    "妨碍",
                    "调查",
                    "推进目标命刻",
                    "压制威胁命刻",
                    "使用库存道具",
                    "施放已掌握法术",
                ]
                actor_sheet = self._character_by_name(actor, pcs)
                if actor_sheet is not None:
                    context.legal_spells = list(actor_sheet.spells)
                    context.legal_skills = list(actor_sheet.skills)
            return context

        context.legal_actions = [
            "普通叙事行动",
            "调查",
            "社交交涉",
            "推进目标命刻",
            "计划或推进仪式",
            "启动或推进工程",
            "消耗物语点引入事实",
            "请求休息或幕间",
        ]
        actor_sheet = self._character_by_name(actor, pcs)
        if actor_sheet is not None:
            context.legal_spells = list(actor_sheet.spells)
            context.legal_skills = list(actor_sheet.skills)
        return context

    def as_prompt_block(self, context: LegalActionContext) -> str:
        data = asdict(context)
        lines = [
            "当前合法行动上下文：",
            f"- 测试目标：{data['stage_goal'] or '未指定'}",
            f"- 冲突中：{data['conflict_active']}",
            f"- 当前行动者：{data['current_actor'] or '无'}",
            f"- 已知 PC：{'、'.join(data['known_pcs']) or '无'}",
            f"- 已知敌人：{'、'.join(data['known_enemies']) or '无'}",
            f"- 命刻：{'；'.join(data['active_clocks']) or '无'}",
            f"- 可选动作：{'、'.join(data['legal_actions']) or '无'}",
        ]
        if context.legal_spells:
            lines.append(f"- 当前角色已掌握法术：{'、'.join(context.legal_spells)}")
        if context.legal_skills:
            lines.append(f"- 当前角色已掌握技能：{'、'.join(context.legal_skills)}")
        if context.notes:
            lines.append("- 注意：" + "；".join(context.notes))
        return "\n".join(lines)

    def _speaker_character_guess(self, speaker: str, pcs: list[Any]) -> str:
        if not pcs:
            return ""
        if len(pcs) == 1:
            return pcs[0].name
        for character in pcs:
            if character.name and character.name in speaker:
                return character.name
        return ""

    def _character_by_name(self, name: str, characters: list[Any]) -> Any | None:
        for character in characters:
            if character.name == name:
                return character
        return None

    def _is_enemy_actor(self, actor: str, enemies: list[Any]) -> bool:
        return any(character.name == actor for character in enemies)
