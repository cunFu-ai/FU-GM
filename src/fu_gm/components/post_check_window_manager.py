from __future__ import annotations

from dataclasses import dataclass, field

from fu_gm.models import Character, RollOutcome
from fu_gm.skill_library import skill_rank


@dataclass(frozen=True)
class PostCheckWindow:
    """A structured, non-player-facing prompt for decisions after a check."""

    kind: str
    label: str
    actor: str
    timing: str
    guidance: str
    action_type: str = ""
    options: list[dict[str, object]] = field(default_factory=list)
    priority: str = "normal"

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "label": self.label,
            "actor": self.actor,
            "timing": self.timing,
            "guidance": self.guidance,
            "action_type": self.action_type,
            "options": list(self.options),
            "priority": self.priority,
        }


class PostCheckWindowManager:
    """Builds audit-friendly windows for Fabula Ultima post-check choices.

    These windows are deliberately stored in payloads instead of rendered
    directly. Expressor and dashboards can use them, while the GM remains free
    to phrase the moment naturally at the table.
    """

    def build(
        self,
        actor: Character,
        outcome: RollOutcome,
        *,
        allow_success_invocation: bool = False,
    ) -> list[dict[str, object]]:
        windows: list[PostCheckWindow] = []
        windows.extend(self._opportunity_windows(actor, outcome))
        windows.extend(
            self._invocation_windows(
                actor,
                outcome,
                allow_success_invocation=allow_success_invocation,
            )
        )
        windows.extend(self._skill_windows(actor, outcome))
        return [window.as_dict() for window in windows]

    def _opportunity_windows(self, actor: Character, outcome: RollOutcome) -> list[PostCheckWindow]:
        options = self._opportunity_options()
        if outcome.critical_success:
            return [
                PostCheckWindow(
                    kind="critical_opportunity",
                    label="大成功机会",
                    actor=actor.name,
                    timing="检定结算后",
                    action_type="TriggerOpportunity",
                    guidance="该检定的操控者可选择一个机会效果；【揭示】还必须选定一个生物，随后得知其目标或动机。",
                    options=options,
                    priority="high",
                )
            ]
        if outcome.fumble:
            return [
                PostCheckWindow(
                    kind="fumble_opportunity",
                    label="大失败机会",
                    actor=actor.name,
                    timing="检定结算后",
                    action_type="TriggerOpportunity",
                    guidance="GM 或对手获得一个机会效果；PC 获得 1 点物语点的资源变化由硬规则处理。",
                    options=options,
                    priority="high",
                )
            ]
        return []

    @staticmethod
    def _opportunity_options() -> list[dict[str, object]]:
        """The complete core-rule opportunity list (page 41)."""

        return [
            {"effect": "揭示", "summary": "得知所选生物的目标或动机。", "requires": ["target"]},
            {"effect": "进展", "summary": "在一个现有命刻上填充或擦除至多2格。", "requires": ["clock_name"]},
            {"effect": "纽带", "summary": "建立羁绊，或为现有羁绊添加一种情感。", "requires": ["target", "emotion"]},
            {"effect": "情报", "summary": "发现一条有用线索或情报。"},
            {"effect": "青睐", "summary": "赢得某人的支持或赞赏。", "requires": ["target"]},
            {"effect": "审视", "summary": "发现可见生物的一项弱点或特质。", "requires": ["target"]},
            {"effect": "失态", "summary": "令场景中的生物表达一句妥协性言论。", "requires": ["target"]},
            {"effect": "失物", "summary": "一件物品损坏、遗失、失窃或被丢弃。"},
            {"effect": "受苦", "summary": "施加眩晕、动摇、迟缓或虚弱。", "requires": ["target", "status_effect"]},
            {"effect": "优势", "summary": "自己或盟友的下一次检定获得+4。", "requires": ["target"]},
            {"effect": "转折", "summary": "所选某人或某物突然出现在场景中。", "requires": ["subject"]},
            {"effect": "自定义", "summary": "提出一个符合当前场景的其他意外转折。", "requires": ["description"]},
        ]

    def _invocation_windows(
        self,
        actor: Character,
        outcome: RollOutcome,
        *,
        allow_success_invocation: bool = False,
    ) -> list[PostCheckWindow]:
        # Successful checks settle immediately. Optional rerolls remain
        # available after a failure, but do not turn every roll into a prompt.
        if (
            (outcome.success and not allow_success_invocation)
            or outcome.fumble
            or actor.fabula_points <= 0
            or "pc" not in actor.traits
        ):
            return []

        windows: list[PostCheckWindow] = []
        traits = self._invokable_traits(actor)
        if traits:
            windows.append(
                PostCheckWindow(
                    kind="trait_invocation",
                    label="援用特质",
                    actor=actor.name,
                    timing="检定后、结果定稿前",
                    action_type="InvokeTrait",
                    guidance="",
                    options=[{"trait": trait} for trait in traits],
                    priority="normal" if outcome.success else "high",
                )
            )

        bond_options = [
            {"target": bond.target, "strength": bond.strength, "emotions": list(bond.emotions)}
            for bond in actor.bonds
            if bond.strength > 0
        ]
        if bond_options:
            windows.append(
                PostCheckWindow(
                    kind="bond_invocation",
                    label="援用羁绊",
                    actor=actor.name,
                    timing="检定后、结果定稿前",
                    action_type="InvokeBond",
                    guidance="",
                    options=bond_options,
                    priority="normal" if outcome.success else "high",
                )
            )
        return windows

    def _skill_windows(self, actor: Character, outcome: RollOutcome) -> list[PostCheckWindow]:
        windows: list[PostCheckWindow] = []
        if skill_rank(actor.skills, "灵光洞见") > 0 and outcome.total >= 13 and outcome.target:
            windows.append(
                PostCheckWindow(
                    kind="skill_judgement",
                    label="灵光洞见",
                    actor=actor.name,
                    timing="调查检定 13+ 后",
                    guidance="若本次是在调查生物、物品或地点，玩家可向 GM 提出至多技能等级个相关问题；同一对象只触发一次。",
                    options=[{"target": outcome.target, "max_questions": skill_rank(actor.skills, "灵光洞见")}],
                    priority="high",
                )
            )
        return windows

    def _invokable_traits(self, actor: Character) -> list[str]:
        traits: list[str] = []
        # Only the three core traits may fuel a PC reroll. Species, internal
        # role tags and free-form notes in ``traits`` are not invokable.
        for value in (actor.identity, actor.theme, actor.origin):
            text = str(value or "").strip()
            if not text:
                continue
            if text not in traits:
                traits.append(text)
        return traits
