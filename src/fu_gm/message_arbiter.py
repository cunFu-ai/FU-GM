from __future__ import annotations

import re
from dataclasses import dataclass, field

from fu_gm.safety_parser import extract_safety_declarations


@dataclass
class MessageRouteDecision:
    """群消息入口仲裁结果。

    FU-GM 不应该吞掉所有群聊；这里仅决定“这句话该不该由跑团 GM 接手”。
    """

    target: str
    mode: str = ""
    reason: str = ""
    confidence: float = 0.0
    stop_astrbot: bool = False
    tags: list[str] = field(default_factory=list)

    @property
    def should_call_fu_gm(self) -> bool:
        return self.target == "fu_gm"

    @property
    def should_reply(self) -> bool:
        return self.target == "fu_gm"


class HeuristicMessageArbiter:
    """低延迟自然消息仲裁器。

    设计目标：
    - 明确跑团动作、规则问题、Session 0 内容自然交给 FU-GM。
    - 玩家间战术讨论默认静默，避免 GM 和 AstrBot 都插话。
    - 无关闲聊继续交给 AstrBot 本体，不由 FU-GM 抢答。
    """

    DEFAULT_GM_ALIASES = ("时悠", "悠老师", "小夜", "织星者", "gm", "GM", "主持", "GM姐姐")

    def __init__(self, gm_aliases: list[str] | tuple[str, ...] | None = None) -> None:
        aliases = gm_aliases or self.DEFAULT_GM_ALIASES
        self.gm_aliases = tuple(alias for alias in aliases if alias)

    def decide(
        self,
        message: str,
        *,
        speaker: str = "",
        is_private: bool = False,
        is_group: bool = True,
    ) -> MessageRouteDecision:
        text = " ".join(str(message or "").strip().split())
        if not text:
            return MessageRouteDecision(target="silent", reason="空消息", confidence=1.0, stop_astrbot=False)

        if text.startswith("/"):
            return MessageRouteDecision(target="astrbot", reason="显式命令留给命令处理器", confidence=0.8)

        if extract_safety_declarations(text):
            return MessageRouteDecision(
                target="fu_gm",
                mode="safety",
                reason="自然语言安全边界声明",
                confidence=0.95,
                stop_astrbot=True,
                tags=["safety"],
            )

        direct = self._directly_addresses_gm(text)
        if self._looks_like_session_zero(text):
            return MessageRouteDecision(
                target="fu_gm",
                mode="session_zero",
                reason="世界/角色/小队创建相关内容",
                confidence=0.9 if direct else 0.75,
                stop_astrbot=True,
                tags=["session_zero"],
            )

        if self._looks_like_game_action(text):
            return MessageRouteDecision(
                target="fu_gm",
                mode="game",
                reason="自然语言跑团行动声明",
                confidence=0.9,
                stop_astrbot=True,
                tags=["game_action"],
            )

        if self._looks_like_rules_question(text):
            return MessageRouteDecision(
                target="fu_gm",
                mode="casual",
                reason="跑团规则或战役信息问题",
                confidence=0.85 if direct else 0.72,
                stop_astrbot=True,
                tags=["rules_question"],
            )

        if direct:
            return MessageRouteDecision(
                target="fu_gm",
                mode="casual",
                reason="自然称呼 GM",
                confidence=0.8,
                stop_astrbot=True,
                tags=["gm_addressed"],
            )

        if is_group and self._looks_like_table_discussion(text):
            return MessageRouteDecision(
                target="silent",
                reason="玩家间战术/剧情讨论，GM 暂不插话",
                confidence=0.7,
                stop_astrbot=True,
                tags=["table_discussion"],
            )

        return MessageRouteDecision(target="astrbot", reason="非跑团入口，交给 AstrBot 本体", confidence=0.55)

    def should_accept_open_session_zero_input(self, message: str) -> bool:
        """第零章已开启时，判断普通群聊是否值得交给 GM 推进。

        Session 0 已经是明确的跑团语境，因此这里使用“负向静默”而不是
        “关键词白名单”：只要发言有实质内容，默认交给 GM；只有纯水声、
        单字附和或明显玩家间协调才静默，避免主持人抢话。
        """

        text = " ".join(str(message or "").strip().split())
        if not text or self._looks_like_low_content_chatter(text):
            return False
        if text.startswith("/"):
            return False
        if self._looks_like_open_session_zero_side_talk(text):
            return False
        return True

    def _directly_addresses_gm(self, text: str) -> bool:
        stripped = text.strip()
        lowered = stripped.lower()
        for alias in self.gm_aliases:
            if not alias:
                continue
            if alias.lower() in lowered:
                return True
        return False

    def _looks_like_session_zero(self, text: str) -> bool:
        tokens = (
            "第零章",
            "session 0",
            "session0",
            "世界创建",
            "世界设定",
            "角色创建",
            "角色设定",
            "我的角色",
            "小队表",
            "世界表",
            "第一幕",
            "序章",
            "八大支柱",
            "界限",
            "帷幕",
        )
        lowered = text.lower()
        return any(token in text or token in lowered for token in tokens)

    def _looks_like_game_action(self, text: str) -> bool:
        if self._looks_like_soft_discussion(text) and not self._looks_like_committed_action(text):
            return False
        action_starters = (
            "我攻击",
            "我施法",
            "我防御",
            "我守护",
            "我调查",
            "我检定",
            "我打开",
            "我推进",
            "我使用",
            "我购买",
            "我休息",
            "我进入",
            "我尝试",
            "我要攻击",
            "我要施法",
            "我要调查",
            "我要打开",
            "我要使用",
            "我要推进",
            "那我",
            "我来",
        )
        action_terms = (
            "攻击",
            "施法",
            "防御",
            "守护",
            "掩护",
            "妨碍",
            "调查",
            "推进命刻",
            "擦除命刻",
            "检定",
            "开宝箱",
            "打开宝箱",
            "使用物资",
            "消耗物资",
            "释放法术",
            "进行仪式",
            "开始项目",
            "推进项目",
            "召唤阿卡纳",
            "解除阿卡纳",
            "召唤奥灵",
            "遣散奥灵",
            "解除奥灵",
            "休息",
            "旅行",
        )
        if text.startswith(action_starters):
            return True
        if self._looks_like_committed_action(text) and any(term in text for term in action_terms):
            return True
        return False

    def _looks_like_rules_question(self, text: str) -> bool:
        questionish = any(token in text for token in ("？", "?", "吗", "怎么", "能不能", "可以"))
        rules_terms = (
            "规则",
            "检定",
            "伤害",
            "相性",
            "弱点",
            "抵抗",
            "免疫",
            "吸收",
            "危机",
            "命刻",
            "物语点",
            "终结点",
            "异常状态",
            "升级",
            "经验",
            "职业技能",
            "英雄技能",
            "仪式",
            "项目",
            "发明",
            "角色卡",
            "小队表",
            "世界表",
            "先攻",
            "防御动作",
        )
        class_terms = (
            "职业",
            "职业选择",
            "可选职业",
            "车卡",
            "建卡",
            "角色卡",
            "职业技能",
            "英雄技能",
        )
        return questionish and (any(term in text for term in rules_terms) or any(term in text for term in class_terms))

    def _looks_like_table_discussion(self, text: str) -> bool:
        return self._looks_like_soft_discussion(text) and any(
            term in text
            for term in (
                "攻击",
                "调查",
                "施法",
                "宝箱",
                "boss",
                "Boss",
                "反派",
                "命刻",
                "地下城",
                "休息",
                "旅行",
                "线索",
                "要不要",
                "怎么办",
            )
        )

    def _looks_like_open_session_zero_side_talk(self, text: str) -> bool:
        """识别第零章中明显不需要 GM 接话的玩家间协调。

        注意：这里故意不判断“有没有世界/角色关键词”。第零章已经被显式开启，
        玩家自然说出的方向、偏好、反问和补充都应该能进入 GM 处理。
        """

        if self._directly_addresses_gm(text):
            return False
        if self._looks_like_rules_question(text) or self._looks_like_session_zero(text):
            return False
        if self._looks_like_session_zero_commitment(text):
            return False

        stripped = text.strip()
        if stripped.startswith("@"):
            return True

        side_talk_cues = (
            "你先说",
            "你先来",
            "你来决定",
            "你决定",
            "你们先",
            "谁先",
            "谁来",
            "先等等",
            "先等",
            "等一下",
            "等等",
            "稍等",
            "待会",
            "先听",
            "我们先商量",
            "商量一下",
            "先问问",
            "问问大家",
        )
        if any(cue in text for cue in side_talk_cues):
            return True

        # “要不要”本身也可能是在向 GM 提议设定；只有明显是在安排发言顺序
        # 或暂缓流程时才静默。
        if "要不要" in text and any(cue in text for cue in ("先等", "等等", "你先", "谁先", "白河", "阿凛")):
            return True

        return False

    def _looks_like_soft_discussion(self, text: str) -> bool:
        discussion_cues = (
            "我觉得",
            "要不要",
            "我们要不要",
            "是不是",
            "可能",
            "也许",
            "还是",
            "你们",
            "谁来",
            "商量",
            "讨论",
            "感觉",
            "不如",
            "要是",
        )
        return any(cue in text for cue in discussion_cues)

    def _looks_like_committed_action(self, text: str) -> bool:
        committed_cues = (
            "就这么做",
            "决定",
            "我来",
            "那我",
            "我直接",
            "我马上",
            "我现在",
            "我选择",
            "我要",
            "我打算",
        )
        return any(cue in text for cue in committed_cues)

    def _looks_like_session_zero_commitment(self, text: str) -> bool:
        cues = (
            "就设定",
            "决定设定",
            "确定设定",
            "我设定",
            "我想设定",
            "我希望",
            "我想要",
            "我喜欢",
            "我的角色",
            "我的故乡",
            "我的主题",
            "我的身份",
            "这个世界",
            "我们的小队",
        )
        return any(cue in text for cue in cues)

    def _looks_like_low_content_chatter(self, text: str) -> bool:
        compact = re.sub(r"[\s，。！？!?~～…、,.]+", "", text).lower()
        if not compact:
            return True
        if compact in {
            "哈",
            "哈哈",
            "哈哈哈",
            "哈哈哈哈",
            "笑死",
            "乐",
            "草",
            "好",
            "好的",
            "好哦",
            "嗯",
            "嗯嗯",
            "行",
            "可以",
            "ok",
            "okk",
            "了解",
            "收到",
            "没问题",
            "666",
            "233",
        }:
            return True
        return bool(re.fullmatch(r"(哈|h|w|233|6|草|乐|笑)+", compact))
