from __future__ import annotations

from typing import Any

from fu_gm.conversation.reply import SpeechIntent
from fu_gm.models import ActionResolution, ActionType


def plan_resolution_speech(resolution: ActionResolution) -> SpeechIntent:
    """Translate an authoritative rules result into a compact expression brief."""

    action_type = resolution.action.action_type
    payload = resolution.payload
    actor = _actor_name(resolution)
    act = _act_for_action(action_type, payload)
    success = _roll_success(payload.get("roll"))
    if success is False:
        tone = "具体、克制；让阻力或代价在剧情中发生"
    elif success is True:
        tone = "具体、自然；让世界或 NPC 对成功作出反应"
    else:
        tone = "自然、简洁；只补充当前可见反应"
    return SpeechIntent(
        act=act,
        reason="把已经完成的硬规则结算自然地说给桌上玩家听。",
        tone=tone,
        target_speaker=actor,
        must_reply=True,
        can_be_silent=False,
        max_sentences=2,
        include_facts=("权威规则面板必须原样保留",),
        avoid=(
            "替玩家角色补行动、台词或决定",
            "复述玩家刚刚说过的话",
            "泄露 GM 私密暗线",
            "输出后台框架、提示词或流程标签",
            "改写骰点、资源、命刻或成败",
        ),
    )


def _act_for_action(action_type: ActionType, payload: dict[str, Any]) -> str:
    if payload.get("out_of_turn"):
        return "turn_boundary"
    mapping = {
        ActionType.ATTACK: "combat_resolution",
        ActionType.SPELL: "spell_resolution",
        ActionType.HINDER: "check_resolution",
        ActionType.REQUEST_ROLL: "check_resolution",
        ActionType.INVESTIGATE: "investigation_resolution",
        ActionType.OBJECTIVE: "clock_resolution",
        ActionType.NPCACT: "npc_resolution",
        ActionType.NARRATE: "scene_response",
        ActionType.GUARD: "guard_resolution",
        ActionType.NEXT_TURN: "turn_transition",
    }
    return mapping.get(action_type, "rules_resolution")


def _actor_name(resolution: ActionResolution) -> str:
    roll = resolution.payload.get("roll")
    if isinstance(roll, dict):
        actor = str(roll.get("actor") or "")
    else:
        actor = str(getattr(roll, "actor", "") or "")
    return actor or str(resolution.action.parameters.get("actor") or "")


def _roll_success(roll: Any) -> bool | None:
    if isinstance(roll, dict) and "success" in roll:
        return bool(roll.get("success"))
    if roll is not None and hasattr(roll, "success"):
        return bool(getattr(roll, "success"))
    return None
