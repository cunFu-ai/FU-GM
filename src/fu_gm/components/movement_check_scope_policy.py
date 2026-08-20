from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from fu_gm.gm_evidence import normalize_literal_evidence


@dataclass(frozen=True)
class MovementCheckScopeReview:
    """移动检定的语义范围复核结果。"""

    valid: bool
    error_code: str = ""
    message: str = ""
    correction_hint: str = ""


class MovementCheckScopePolicy:
    """把移动检定限定在玩家授权的一次穿越内。

    这里只读取玩家原句与待提交的检定契约，不根据场景题材猜测
    路线。因此规则可以同样覆盖建筑、地下城、野外、城市和追逐场景。
    """

    RESOLUTION_MODES = frozenset(
        {
            "single_obstacle",
            "abstract_journey",
            "chase",
        }
    )

    _ARRIVAL_VERBS = (
        "前往",
        "去往",
        "赶往",
        "走向",
        "走到",
        "跑到",
        "冲到",
        "撤到",
        "退到",
        "爬到",
        "登上",
        "下到",
        "进入",
        "进到",
        "走进",
        "潜入",
        "闯入",
        "来到",
        "抵达",
        "到达",
        "逃到",
        "追到",
        "追上",
    )
    _EGRESS_MARKERS = (
        "离开",
        "逃离",
        "逃出",
        "撤出",
        "退出",
        "出去",
        "到外面",
        "走到外面",
        "走出",
        "跑出",
        "往外走",
    )
    _EXTERIOR_DESTINATION_MARKERS = (
        "外面",
        "外侧",
        "门外",
        "室外",
        "屋外",
        "馆外",
    )
    _ROUTE_SEARCH_PATTERNS = (
        re.compile(
            r"(?:寻找|找寻|找找|搜索|探索|侦察|摸索|查看|看看|辨认|确认)"
            r".{0,18}(?:路线|路径|方向|入口|出口|通道|去路|走法|道路|路)"
        ),
        re.compile(
            r"(?:寻找|找寻|找找|摸索).{0,18}"
            r"(?:怎么|如何)?(?:离开|出去|到达|前往).{0,12}"
            r"(?:的)?(?:路线|办法|方向|通道|路)"
        ),
        re.compile(
            r"(?:寻找|找寻|找找|摸索|查看|看看|侦察)"
            r".{0,12}(?:哪边|哪里|何处|通往)"
        ),
    )
    _ABSTRACT_JOURNEY_PATTERNS = (
        re.compile(
            r"(?:整段|全程|一路|一口气|直接|快进|略过|概括|抽象|一次性|用一次)"
            r".{0,14}(?:结算|赶到|抵达|走完|穿过|到达|完成|跑完)"
        ),
        re.compile(r"(?:这段|这趟|这一路).{0,12}(?:一起|一次|整体)?结算"),
    )
    _CHASE_MARKERS = (
        "追逐",
        "追赶",
        "追击",
        "追上",
        "紧追",
        "甩开",
        "逃亡",
        "逃跑",
    )
    _ROLLBACK_PATTERNS = (
        re.compile(
            r"(?:此前|之前|先前|原先|原本|已经|已确认|已查明|已取得|已获得|已完成)"
            r".{0,24}(?:成果|线索|路线|入口|出口|通路|进展|发现|物品|装备|承诺)?"
            r".{0,12}(?:作废|失效|消失|清零|重置|撤销|抹去|失去|不再成立)"
        ),
        re.compile(
            r"(?:作废|清零|重置|撤销|抹去).{0,16}"
            r"(?:此前|之前|先前|已经|已确认|已查明|已取得|已完成)"
        ),
    )
    _BROAD_FAILURE_PATTERNS = (
        re.compile(
            r"(?:全员|所有人|众人|整支队伍|整个|整座|全部)"
            r".{0,24}(?:困住|被困|封死|封闭|摧毁|坍塌|消失|失去|无法离开|不能离开)"
        ),
        re.compile(
            r"(?:再也|永远|永久|彻底).{0,18}"
            r"(?:无法|不能|封死|失去|消失|断绝|困住|被困)"
        ),
    )
    _LOCAL_FAILURE_MARKERS = (
        "原地",
        "当前位置",
        "这一侧",
        "眼前",
        "门前",
        "边缘",
        "入口前",
        "未能",
        "没能",
        "无法抵达",
        "只得停下",
        "只能停",
        "退回",
        "暴露",
        "受伤",
        "摔倒",
        "耽搁",
        "延误",
        "消耗",
    )
    _GENERIC_NGRAMS = frozenset(
        {
            "角色",
            "无法",
            "未能",
            "成功",
            "抵达",
            "目的",
            "地点",
            "当前",
            "这次",
            "移动",
            "继续",
            "只能",
            "原地",
        }
    )

    @classmethod
    def validate(
        cls,
        *,
        source_message: str,
        evidence: str,
        actor: str,
        destination: str,
        obstacle: str,
        purpose: str,
        success_observation: str,
        failure_consequence: str,
        resolution_mode: str,
        known_player_characters: Iterable[str] = (),
    ) -> MovementCheckScopeReview:
        source = normalize_literal_evidence(source_message)
        literal_evidence = normalize_literal_evidence(evidence)
        destination = normalize_literal_evidence(destination)
        obstacle = normalize_literal_evidence(obstacle)
        purpose = normalize_literal_evidence(purpose)
        success_observation = normalize_literal_evidence(success_observation)
        failure_consequence = normalize_literal_evidence(failure_consequence)
        mode = str(resolution_mode or "").strip().lower()

        if mode not in cls.RESOLUTION_MODES:
            return cls._failure(
                "MOVEMENT_RESOLUTION_MODE_REQUIRED",
                "移动检定需要声明单一障碍、抽象旅程或追逐结算。",
                "按玩家原句选择resolution_mode；普通现场移动使用single_obstacle。",
            )
        if not source or not literal_evidence or literal_evidence not in source:
            return cls._failure(
                "MOVEMENT_SCOPE_EVIDENCE_REQUIRED",
                "移动范围缺少可复核的玩家原句。",
                "从原始消息复制包含本次移动动作和落点的连续片段。",
            )
        if not destination or not obstacle:
            return cls._failure(
                "MOVEMENT_ATOMIC_SCOPE_REQUIRED",
                "移动检定需要一个明确落点和当前障碍。",
                "把检定收窄为穿越眼前障碍并到达一个紧邻、可验证的落点。",
            )

        abstract_authorized = any(
            pattern.search(literal_evidence)
            for pattern in cls._ABSTRACT_JOURNEY_PATTERNS
        )
        chase_authorized = any(
            marker in literal_evidence for marker in cls._CHASE_MARKERS
        )
        if mode == "abstract_journey" and not abstract_authorized:
            return cls._failure(
                "ABSTRACT_JOURNEY_NOT_AUTHORIZED",
                "玩家原句没有授权把整段路程合并为一次结算。",
                "按当前一个落点或障碍处理；玩家明确要求整段结算时才使用abstract_journey。",
            )
        if mode == "chase" and not chase_authorized:
            return cls._failure(
                "CHASE_RESOLUTION_NOT_AUTHORIZED",
                "玩家原句没有声明追逐、追赶或逃亡行动。",
                "普通移动按single_obstacle处理；追逐结算只承接玩家已声明的追逃意图。",
            )

        aliases = cls._destination_aliases(destination)
        explicit_target = cls._has_explicit_arrival_target(
            literal_evidence,
            aliases,
        )
        egress_target = cls._has_egress_target(
            literal_evidence,
            destination,
        )
        route_search = any(
            pattern.search(literal_evidence)
            for pattern in cls._ROUTE_SEARCH_PATTERNS
        )
        if route_search and not explicit_target and not (
            abstract_authorized and mode == "abstract_journey"
        ):
            return cls._failure(
                "EXPLORATION_EXPANDED_TO_ARRIVAL",
                "玩家声明的是探路、寻找或辨认方向，当前提案却直接结算了远端抵达。",
                "用调查结算路线发现，或把移动落点收窄为玩家原句已指明的紧邻位置。",
            )
        if not (explicit_target or egress_target):
            return cls._failure(
                "MOVEMENT_DESTINATION_OUTRUNS_INTENT",
                "拟议落点超出玩家本次明确的移动目标。",
                "使用原句直接指名的落点，或先结算探路；后续障碍和宏观终点保留给后续行动。",
            )

        rollback_match = cls._first_match(
            failure_consequence,
            cls._ROLLBACK_PATTERNS,
        )
        if rollback_match:
            return cls._failure(
                "MOVEMENT_FAILURE_REVOKES_COMMITTED_RESULT",
                f"失败契约撤销了此前已提交的成果：{rollback_match}。",
                "保留已成立的位置、线索、路线和物品状态；失败只产生本次障碍内的停顿、局部代价或风险暴露。",
            )
        broad_match = cls._first_match(
            failure_consequence,
            cls._BROAD_FAILURE_PATTERNS,
        )
        if broad_match:
            return cls._failure(
                "MOVEMENT_FAILURE_EXCEEDS_OBSTACLE",
                f"失败契约扩大到了本次障碍以外的整体局面：{broad_match}。",
                "失败后果限于行动者和当前障碍，保留其他人物、路线与后续解法。",
            )

        participant_names = {
            str(name or "").strip()
            for name in known_player_characters
            if str(name or "").strip()
        }
        unrelated_players = sorted(
            name
            for name in participant_names
            if name != actor and name in failure_consequence
        )
        if unrelated_players:
            return cls._failure(
                "MOVEMENT_FAILURE_AFFECTS_UNINVOLVED_PC",
                "失败契约同时改变了未参与本次移动的玩家角色："
                + "、".join(unrelated_players)
                + "。",
                "只结算当前行动者穿越这个障碍的结果，其他PC的行动保持独立。",
            )

        if not cls._failure_is_local(
            failure_consequence,
            obstacle=obstacle,
            destination_aliases=aliases,
        ):
            return cls._failure(
                "MOVEMENT_FAILURE_NOT_TIED_TO_OBSTACLE",
                "失败契约与当前障碍或本次落点没有可验证的联系。",
                "将后果写成未能穿过这一障碍、停在当前一侧，或承受该障碍直接造成的局部代价。",
            )
        return MovementCheckScopeReview(valid=True)

    @classmethod
    def _destination_aliases(cls, destination: str) -> tuple[str, ...]:
        clean = re.sub(r"\s+", "", str(destination or ""))
        parts = [
            re.sub(r"[（(].*?[）)]", "", part).strip()
            for part in re.split(r"[·/／>＞→]", clean)
            if part.strip()
        ]
        candidates = [clean, *reversed(parts)]
        return tuple(
            dict.fromkeys(
                item
                for item in candidates
                if len(item) >= 2
            )
        )

    @classmethod
    def _has_explicit_arrival_target(
        cls,
        source: str,
        aliases: tuple[str, ...],
    ) -> bool:
        compact = re.sub(r"\s+", "", source)
        comparison = compact.replace("的", "")
        verbs = "|".join(map(re.escape, cls._ARRIVAL_VERBS))
        for alias in aliases:
            comparable_alias = alias.replace("的", "")
            if comparable_alias not in comparison:
                continue
            if re.search(
                rf"(?:{verbs}).{{0,12}}{re.escape(comparable_alias)}",
                comparison,
            ):
                return True
            if re.search(
                rf"(?:去|到|往|向).{{0,4}}{re.escape(comparable_alias)}",
                comparison,
            ):
                return True
        return False

    @classmethod
    def _has_egress_target(cls, source: str, destination: str) -> bool:
        compact = re.sub(r"\s+", "", source)
        normalized_destination = re.sub(r"\s+", "", destination)
        if not any(
            normalized_destination == marker
            for marker in cls._EXTERIOR_DESTINATION_MARKERS
        ):
            return False
        return any(marker in compact for marker in cls._EGRESS_MARKERS)

    @classmethod
    def _failure_is_local(
        cls,
        failure: str,
        *,
        obstacle: str,
        destination_aliases: tuple[str, ...],
    ) -> bool:
        if any(marker in failure for marker in cls._LOCAL_FAILURE_MARKERS):
            return True
        if any(alias in failure for alias in destination_aliases):
            return True
        return bool(
            cls._semantic_bigrams(failure)
            & cls._semantic_bigrams(obstacle)
        )

    @classmethod
    def _semantic_bigrams(cls, value: str) -> set[str]:
        result: set[str] = set()
        for chunk in re.findall(r"[\u3400-\u9fff]{2,}", str(value or "")):
            result.update(
                chunk[index : index + 2]
                for index in range(len(chunk) - 1)
            )
        return result - set(cls._GENERIC_NGRAMS)

    @staticmethod
    def _first_match(
        value: str,
        patterns: tuple[re.Pattern[str], ...],
    ) -> str:
        for pattern in patterns:
            match = pattern.search(value)
            if match is not None:
                return match.group(0)[:100]
        return ""

    @staticmethod
    def _failure(
        error_code: str,
        message: str,
        correction_hint: str,
    ) -> MovementCheckScopeReview:
        return MovementCheckScopeReview(
            valid=False,
            error_code=error_code,
            message=message,
            correction_hint=correction_hint,
        )
