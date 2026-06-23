from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RuleTerm:
    name: str
    player_meaning: str
    usage_guardrail: str


@dataclass(frozen=True)
class RuleGlossary:
    terms: list[RuleTerm] = field(default_factory=list)
    global_guardrails: list[str] = field(default_factory=list)

    def render_for_player_prompt(self, *, legal_actions: list[str] | None = None) -> str:
        lines = ["《最终物语》玩家模拟专用规则词汇与护栏："]
        for term in self.terms:
            lines.append(f"- {term.name}：{term.player_meaning}；发言约束：{term.usage_guardrail}")
        if legal_actions:
            lines.append("本轮可选合法意图：" + "、".join(legal_actions))
        if self.global_guardrails:
            lines.append("通用护栏：")
            lines.extend(f"- {item}" for item in self.global_guardrails)
        return "\n".join(lines)


FINAL_FABULA_GLOSSARY = RuleGlossary(
    terms=[
        RuleTerm(
            "场景",
            "一段围绕具体角色、难题或冲突展开的游戏过程。",
            "玩家可以要求一个场景或描述行动，但不要宣布场景已经解决。",
        ),
        RuleTerm(
            "冲突场景",
            "战斗、追逐、紧张谈判等需要按轮与回合处理的高潮场景。",
            "只有当前行动者能声明会消耗回合的行动；其他玩家只能等待、简短建议或说明预备想法。",
        ),
        RuleTerm(
            "属性检定",
            "用两个属性骰相加并比较难度等级的判定。",
            "玩家只描述想做什么和怎么做，不自行编骰子结果；检定永远是两颗骰。",
        ),
        RuleTerm(
            "大成功",
            "两颗骰掷出相同且都大于等于 6。",
            "玩家不能宣称自己大成功，只能在系统结算后回应机会效果。",
        ),
        RuleTerm(
            "大失败",
            "两颗骰都掷出 1。",
            "玩家不能主动要求大失败；结算后可以表现挫折并接受物语点。",
        ),
        RuleTerm(
            "命刻",
            "追踪目标进度、倒计时或威胁逼近的进度条。",
            "玩家可以尝试推进目标命刻或压制威胁命刻，但不能直接宣布命刻填满。",
        ),
        RuleTerm(
            "目标命刻",
            "越填越接近玩家目标完成的命刻。",
            "成功通常推进它；失败不应被玩家说成目标已经完成。",
        ),
        RuleTerm(
            "威胁命刻",
            "越填越接近危险发生的命刻。",
            "玩家成功通常是擦除或延缓它，不要说成功推进威胁。",
        ),
        RuleTerm(
            "物语点",
            "玩家可用于援用特质/羁绊、或向故事加入新元素的资源。",
            "在第零章外新增确定世界事实时，必须明确愿意消耗物语点。",
        ),
        RuleTerm(
            "特质",
            "角色的身份、主题、故乡。",
            "只能用于解释重掷或角色动机，不能当作新技能。",
        ),
        RuleTerm(
            "羁绊",
            "角色对人物、组织或信仰的情感关系。",
            "可以请求援用来加值，但不能替代行动前提。",
        ),
        RuleTerm(
            "仪式",
            "用魔法学派完成更广泛叙事效果的流程。",
            "只有角色掌握相关仪式能力时才可声明施行；不确定时说普通调查或请求 GM 判断。",
        ),
        RuleTerm(
            "工程",
            "长期或复杂的制作、修复、设施建设等项目。",
            "修信号塔、改装载具、建装置属于工程，不要误说成仪式。",
        ),
        RuleTerm(
            "法术",
            "角色表中已掌握的魔法。",
            "只能施放角色已拥有的法术；治疗等固定效果不能自行编恢复数值。",
        ),
        RuleTerm(
            "协助",
            "帮助当前行动者或共同推进同一目标。",
            "在冲突里协助会消耗自己的回合，发言要说明是在帮谁或一起推进什么。",
        ),
        RuleTerm(
            "机会",
            "大成功或规则效果带来的有利转折。",
            "只有系统说明产生机会后，玩家才选择揭示、进展、纽带、情报等机会效果。",
        ),
    ],
    global_guardrails=[
        "用真人玩家口吻发言，但不要替 GM 描述最终结果。",
        "不要发明规则书没有的职业、法术、伤害类型、资源或自动成功机制。",
        "不要把测试目标、JSON、内部状态、规则审计语直接说给 GM。",
        "当剧情不确定时，说“我尝试”“我想看看能不能”“如果需要检定”。",
        "如果当前不是该角色回合，只能等待、催促当前行动者或说出预备动作，不能结算攻击/施法/目标行动。",
        "第零章可以自由共创；第一章之后新增公开事实需要物语点或 GM 确认。",
    ],
)
