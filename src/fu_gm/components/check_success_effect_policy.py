from __future__ import annotations

from dataclasses import dataclass
import re

from fu_gm.gm_evidence import normalize_literal_evidence


@dataclass(frozen=True)
class CheckSuccessEffectReview:
    """检定成功叙述与结构化效果的一致性复核。"""

    valid: bool
    error_code: str = ""
    message: str = ""
    correction_hint: str = ""


class CheckSuccessEffectPolicy:
    """识别只写在成功叙述中的权威状态变化。

    本策略只判断结果类型，不依赖地点名、剧情标签或题材。
    可感知线索和局部操作结果仍是普通检定的合法成功答案；
    人物位置变化由 ``success_transition`` 承载，会改变区域
    通行性或环境作用范围的结果则交给对应的结构化工具。
    """

    _CLAUSE_SPLIT = re.compile(r"[。！？!?;；，,\n]+")
    _LEADING_MODIFIERS = re.compile(
        r"^(?:(?:已经|已|终于|顺利|成功|实际|随即|立刻|"
        r"马上|迅速|悄然|小心地|直接|一同|一起|转身|继而)地?)*"
    )
    _MOVEMENT_VERBS = (
        "实际抵达",
        "顺利抵达",
        "成功抵达",
        "抵达",
        "到达",
        "来到",
        "进入",
        "进到",
        "走进",
        "跑进",
        "冲进",
        "钻进",
        "潜入",
        "闯入",
        "离开",
        "逃离",
        "撤离",
        "走出",
        "跑出",
        "冲出",
        "逃出",
        "退出",
        "穿过",
        "前往",
        "去往",
        "赶到",
        "走到",
        "跑到",
        "冲到",
        "退到",
        "撤到",
        "闪到",
        "爬到",
        "登上",
        "下到",
        "回到",
        "身处",
        "位于",
        "站在",
        "出现在",
    )
    _IMPLICIT_MOVEMENT_VERBS = _MOVEMENT_VERBS[:-4]
    _ROUTE_DESCRIPTION_AT_CLAUSE_START = re.compile(
        r"^(?:抵达|到达|进入|离开|穿过|前往|去往).{0,14}"
        r"的(?:入口|出口|通道|通路|路线|道路|路径|楼梯|"
        r"门|方法|办法|方向|位置|痕迹|线索)"
    )
    _INFORMATION_CUES = (
        "发现",
        "看见",
        "看到",
        "看清",
        "辨认",
        "确认",
        "查明",
        "得知",
        "判断",
        "察觉",
        "注意到",
        "认出",
        "推断",
        "找到",
        "找到了",
    )
    _REVEAL_STATE_CUES = (
        "表明",
        "显示",
        "说明",
        "可见",
        "可以看出",
        "原来",
        "早已",
        "已经",
        "此前",
        "目前",
        "仍然",
        "正在",
        "痕迹",
        "迹象",
    )
    _NEW_EVENT_CUES = (
        "骤然",
        "突然",
        "顿时",
        "随即",
        "立刻",
        "当场",
        "轰然",
        "一下子",
        "转眼",
        "继而",
        "马上",
        "顷刻",
        "开始",
    )
    _BROAD_SCOPE = (
        r"(?:整座|整个|整片|整条|整段|整层|全部|所有|"
        r"全数|全城|全镇|全村|全域|各处|四周|周围|"
        r"两岸|多处|大片|整支队伍|众人|全员)"
    )
    _MATERIAL_CHANGE = (
        r"(?:封死|封闭|封锁|隔断|切断|断绝|坍塌|崩塌|"
        r"倒塌|摧毁|毁坏|烧毁|焚毁|淹没|冻结|瘫痪|"
        r"消失|蔓延|扩散|席卷|覆盖|吞没|困住|被困|"
        r"无法离开|不能离开|无法通行|不能通行|无路可走)"
    )
    _BROAD_CHANGE_PATTERNS = (
        re.compile(_BROAD_SCOPE + r".{0,32}" + _MATERIAL_CHANGE),
        re.compile(_MATERIAL_CHANGE + r".{0,32}" + _BROAD_SCOPE),
        re.compile(
            r"(?:桥梁|吊桥|石桥|木桥|道路|通路|通道|出口|"
            r"入口|城门|大门|隧道|渡口|港口|街区|区域|"
            r"山谷|建筑|楼层).{0,18}"
            r"(?:被)?(?:封死|封闭|封锁|隔断|断绝|坍塌|崩塌|"
            r"倒塌|摧毁|烧毁|焚毁|淹没|瘫痪|消失)"
        ),
        re.compile(
            r"(?:火势|大火|烈火|洪水|毒雾|风暴|裂隙).{0,24}"
            r"(?:蔓延|扩散|席卷|覆盖|吞没|淹没).{0,20}"
            r"(?:全|整|区域|街区|楼层|建筑|城市|镇|村|"
            r"森林|山谷|两岸|四周)"
        ),
    )

    @classmethod
    def validate(
        cls,
        *,
        action_type: str,
        actor: str,
        success_observation: str,
        has_success_transition: bool,
    ) -> CheckSuccessEffectReview:
        observation = normalize_literal_evidence(success_observation)
        actor = normalize_literal_evidence(actor)
        if not observation:
            return CheckSuccessEffectReview(True)

        if cls._asserts_position_change(observation, actor) and not bool(
            has_success_transition
        ):
            return cls._invalid(
                "CHECK_SUCCESS_TRANSITION_UNCOMMITTED",
                "成功叙述声称人物已改变位置，但本次检定没有结构化的成功转场。",
                (
                    "有阻碍的移动使用declare_movement_check承载落点；"
                    "调查行动把success_observation收束为可感知的线索或路线答案。"
                ),
            )

        broad_change = cls._broad_material_change(observation)
        if broad_change and not cls._is_investigation_reveal(
            action_type,
            observation,
        ):
            return cls._invalid(
                "CHECK_SUCCESS_WORLD_CHANGE_UNCOMMITTED",
                "成功叙述包含会改变区域通行性或环境作用范围的结果，但本次检定没有对应的结构化成功效果。",
                (
                    "将success_observation收束为本次手段直接造成的局部结果；"
                    "区域封锁、通路坍塌或环境蔓延由能提交该状态的专用规则或场景工具承载。"
                ),
            )

        return CheckSuccessEffectReview(True)

    @classmethod
    def _asserts_position_change(cls, text: str, actor: str) -> bool:
        subjects = tuple(
            item for item in (actor, "他", "她", "他们", "她们") if item
        )
        for raw_clause in cls._CLAUSE_SPLIT.split(text):
            clause = raw_clause.strip()
            if not clause:
                continue
            direct = cls._LEADING_MODIFIERS.sub("", clause)
            if (
                direct.startswith(cls._IMPLICIT_MOVEMENT_VERBS)
                and not cls._ROUTE_DESCRIPTION_AT_CLAUSE_START.search(direct)
            ):
                return True
            for subject in subjects:
                index = clause.find(subject)
                if index < 0:
                    continue
                remainder = clause[index + len(subject) :]
                verb_positions = [
                    remainder.find(verb)
                    for verb in cls._MOVEMENT_VERBS
                    if remainder.find(verb) >= 0
                ]
                if not verb_positions:
                    continue
                first_verb = min(verb_positions)
                prefix = remainder[:first_verb]
                if len(prefix) > 18:
                    continue
                if any(cue in prefix for cue in cls._INFORMATION_CUES):
                    continue
                return True
        return False

    @classmethod
    def _broad_material_change(cls, text: str) -> bool:
        return any(pattern.search(text) for pattern in cls._BROAD_CHANGE_PATTERNS)

    @classmethod
    def _is_investigation_reveal(cls, action_type: str, text: str) -> bool:
        if str(action_type or "").strip().lower() != "investigate":
            return False
        if any(cue in text for cue in cls._NEW_EVENT_CUES):
            return False
        return any(
            cue in text
            for cue in (*cls._INFORMATION_CUES, *cls._REVEAL_STATE_CUES)
        )

    @staticmethod
    def _invalid(
        error_code: str,
        message: str,
        correction_hint: str,
    ) -> CheckSuccessEffectReview:
        return CheckSuccessEffectReview(
            False,
            error_code=error_code,
            message=message,
            correction_hint=correction_hint,
        )
