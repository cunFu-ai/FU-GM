from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from fu_gm.components.scene_frame_manager import SceneFrame
from fu_gm.models import Action, ActionType


@dataclass(frozen=True)
class SceneActionOutcomeReview:
    replan_required: bool = False
    reason: str = ""
    instruction: str = ""


class SceneActionOutcomePolicy:
    """Reject a GM decision that merely restates a committed world action.

    This policy never chooses a difficulty level or a fictional outcome. It
    only asks the semantic tool transaction to finish its adjudication.
    """

    _AUTHORITATIVE_FLAGS = (
        "establish_fact",
        "consume_turn",
        "player_facing_reply",
        "npc_answer_generated",
        "scene_object_response",
        "care_action_response",
        "scene_clarification",
        "scene_open_request",
        "gm_beat_request",
        "out_of_turn_comment",
    )

    def review(
        self,
        action: Action,
        *,
        route_decision: dict[str, object] | None,
        player_message: str,
        frame: SceneFrame | None,
        has_active_pressure: bool,
    ) -> SceneActionOutcomeReview:
        route = dict(route_decision or {})
        if action.action_type != ActionType.NARRATE:
            return SceneActionOutcomeReview()
        summary = str(action.parameters.get("summary") or action.parameters.get("narration") or "").strip()
        if (
            route.get("commitment") == "action"
            and route.get("world_response_required")
            and self._looks_like_generic_scene_opening(summary)
        ):
            return SceneActionOutcomeReview(
                replan_required=True,
                reason="committed_action_was_replaced_by_scene_opening_placeholder",
                instruction=(
                    "上一方案错误地用通用开场占位句替代了玩家刚声明的行动。请只裁定当前行动："
                    "描述具体对象已经如何回应或发生变化；若结果不确定且失败有意义，则选择正确检定。"
                    "不得重新开场，不得写镜头打开、地点人物压力浮出或等待英雄行动。"
                ),
            )
        if any(action.parameters.get(flag) for flag in self._AUTHORITATIVE_FLAGS):
            return SceneActionOutcomeReview()
        if not (
            route.get("commitment") == "action"
            and route.get("world_response_required")
            and route.get("interaction_kind") in {"manipulate", "move"}
        ):
            return SceneActionOutcomeReview()

        pressure_present = bool(
            has_active_pressure
            or (frame and (frame.current_pressure or frame.committed_consequences or frame.open_conditions))
        )
        if summary and not pressure_present and not self._looks_like_action_echo(summary, player_message):
            return SceneActionOutcomeReview()

        return SceneActionOutcomeReview(
            replan_required=True,
            reason="committed_scene_action_has_no_world_outcome",
            instruction=(
                "上一方案只复述了玩家已经声明的行动，没有完成GM裁定。请重新处理同一条玩家消息，不得新增玩家动作。"
                "如果结果显而易见，返回Narrate，并在summary中只描述行动对象、NPC或环境已经发生的具体变化，"
                "同时设置establish_fact=true与consume_turn=true；不要解释玩家的重点或复述其动作。"
                "如果结果不确定且失败会产生有意义后果，返回RequestRoll/Investigate/Objective等正确规则动作，"
                "由你根据当前场况明确选择属性、难度等级和实际失败后果；不要使用程序默认难度，也不要把选择交还给玩家。"
            ),
        )

    @staticmethod
    def _looks_like_generic_scene_opening(summary: str) -> bool:
        clean = " ".join(str(summary or "").split())
        if not clean:
            return False
        exact_markers = (
            "地点、人物和压力同时浮出",
            "等英雄把第一句话或第一步行动落进去",
            "当前地点先露出气味和轮廓",
            "英雄们只要开口或迈步",
        )
        return "镜头打开" in clean or any(marker in clean for marker in exact_markers)

    @classmethod
    def _looks_like_action_echo(cls, summary: str, player_message: str) -> bool:
        left = cls._normalize(summary)
        right = cls._normalize(cls._strip_speaker(player_message))
        if not left or not right:
            return True
        if left in right or right in left:
            return True
        return SequenceMatcher(None, left, right).ratio() >= 0.62

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(value or "")).lower()

    @staticmethod
    def _strip_speaker(value: str) -> str:
        return re.sub(r"^[^\n:：]{1,32}[:：]\s*", "", str(value or "").strip())
