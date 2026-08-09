from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from fu_gm.conversation.events import MessageEvent
from fu_gm.conversation.reply import SpeechIntent


@dataclass(frozen=True)
class PresenceDecision:
    action: str
    should_speak: bool
    reason: str
    priority: str = "normal"
    reply: str = ""
    instruction: str = ""
    intent: SpeechIntent | None = None
    telemetry: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.intent is not None:
            data["intent"] = self.intent.to_dict()
        return data


class TablePresenceScheduler:
    """Phase-aware policy for deciding when the GM should enter the chat."""

    def message_policy(
        self,
        event: MessageEvent,
        *,
        gate_status: str,
        route_target: str,
        route_mode: str,
        reply_required: bool,
    ) -> PresenceDecision:
        if event.directly_addresses_gm:
            return PresenceDecision(
                action="reply",
                should_speak=True,
                reason="玩家通过艾特或引用直接呼叫 GM。",
                priority="mandatory",
                intent=SpeechIntent(
                    act="direct_reply",
                    reason="直接呼叫必须得到回应。",
                    target_message_id=event.message_id,
                    target_speaker=event.speaker,
                    must_reply=True,
                    can_be_silent=False,
                ),
            )
        if route_target != "fu_gm":
            return PresenceDecision(
                action="silent" if route_target == "silent" else "delegate",
                should_speak=False,
                reason="消息不需要 GM 可见回应。",
            )
        if route_mode in {"safety", "game"} or reply_required:
            act = "safety_reply" if route_mode == "safety" else "game_resolution" if route_mode == "game" else "reply"
            return PresenceDecision(
                action=act,
                should_speak=True,
                reason="当前消息承担安全、规则或明确主持义务。",
                priority="mandatory" if route_mode in {"safety", "game"} else "normal",
                intent=SpeechIntent(
                    act=act,
                    target_message_id=event.message_id,
                    target_speaker=event.speaker,
                    must_reply=route_mode in {"safety", "game"},
                    can_be_silent=route_mode not in {"safety", "game"},
                ),
            )
        return PresenceDecision(action="observe", should_speak=False, reason=f"{gate_status} 阶段暂时只观察。")

    def heartbeat_policy(
        self,
        *,
        gate_status: str,
        idle_seconds: int,
        cooldown_remaining: int,
        has_public_entries: bool,
        last_entry_role: str,
        current_actor: str,
        conflict_active: bool,
        current_actor_is_pc: bool,
        held_action_summary: str,
        thresholds: dict[str, int],
        force: bool,
        recent_gm_ratio: float,
        recent_message_count: int,
        heartbeat_instruction: str = "",
        player_idle_seconds: int | None = None,
        setup_nudge_count: int = 0,
        setup_nudge_limit: int = 1,
        seconds_since_setup_nudge: int | None = None,
        setup_nudge_followup_seconds: int = 1200,
        adventure_nudge_count: int = 0,
        adventure_nudge_limit: int = 1,
    ) -> PresenceDecision:
        # Setup nudges are invitations, not alarms. A delivered invitation
        # exhausts the current idle episode until the table makes real setup
        # progress.
        setup_nudge_limit = min(1, max(0, int(setup_nudge_limit)))
        setup_idle_seconds = (
            idle_seconds
            if player_idle_seconds is None
            else max(0, player_idle_seconds)
        )
        telemetry = {
            "idle_seconds": idle_seconds,
            "player_idle_seconds": setup_idle_seconds,
            "cooldown_remaining_seconds": cooldown_remaining,
            "recent_gm_ratio": round(max(0.0, min(1.0, recent_gm_ratio)), 3),
            "recent_message_count": recent_message_count,
            "setup_nudge_count": max(0, setup_nudge_count),
            "setup_nudge_limit": max(0, setup_nudge_limit),
            "seconds_since_setup_nudge": seconds_since_setup_nudge,
            "setup_nudge_followup_seconds": max(0, setup_nudge_followup_seconds),
            "adventure_nudge_count": max(0, adventure_nudge_count),
            "adventure_nudge_limit": max(0, adventure_nudge_limit),
        }
        if gate_status not in {"pre_session", "session_zero", "adventure"}:
            return self._silent("会话未处于 FU-GM 接管状态。", telemetry)
        if cooldown_remaining > 0:
            return self._silent("仍在主动发言冷却时间内。", telemetry)
        if not force and not has_public_entries:
            return self._silent("尚无公开对话，主动调度不抢先开场。", telemetry)

        if gate_status == "adventure" and conflict_active and current_actor:
            threshold_name = "pc_turn" if current_actor_is_pc else "npc_turn"
            if not force and idle_seconds < thresholds[threshold_name]:
                return self._silent("冲突场景尚未达到当前行动者的等待阈值。", telemetry)
            if current_actor_is_pc:
                reply = (
                    f"现在轮到【{current_actor}】。刚才缓存的动作是：{held_action_summary}。"
                    "你可以确认照此结算，也可以改动作。"
                    if held_action_summary
                    else f"现在轮到【{current_actor}】。说出你要做什么就好。"
                )
                return PresenceDecision(
                    action="pc_turn_reminder",
                    should_speak=True,
                    reason="轮到玩家角色，仅提醒而不代替行动。",
                    priority="required_turn",
                    reply=reply,
                    intent=SpeechIntent(
                        act="turn_reminder",
                        reason="当前行动者等待过久。",
                        target_speaker=current_actor,
                        must_reply=True,
                        can_be_silent=False,
                        max_sentences=2,
                        avoid=("替玩家选择行动", "列出完整动作菜单"),
                    ),
                    telemetry=telemetry,
                )
            return PresenceDecision(
                action="npc_turn",
                should_speak=True,
                reason="非玩家角色回合等待超时，必须推进以防冲突死锁。",
                priority="mandatory",
                intent=SpeechIntent(
                    act="npc_turn",
                    must_reply=True,
                    can_be_silent=False,
                    avoid=("替玩家角色行动",),
                ),
                telemetry=telemetry,
            )

        if gate_status in {"pre_session", "session_zero"}:
            threshold_name = "pre_session" if gate_status == "pre_session" else "session_zero"
            if not force and setup_idle_seconds < thresholds[threshold_name]:
                return self._silent("开团共识尚未达到等待阈值。", telemetry)
            if not force and setup_nudge_limit <= 0:
                return self._silent("当前配置不允许在开团阶段主动提醒。", telemetry)
            if not force and setup_nudge_count >= setup_nudge_limit:
                return self._silent("本轮静默周期的主动提醒次数已用完，等待玩家回来。", telemetry)
            reply = (
                "要继续时，先说一个现在最想定下来的共识就好。"
                if gate_status == "pre_session"
                else "要继续时，从刚才悬着的那一项接着说就好。"
            )
            return PresenceDecision(
                action="session_zero_nudge",
                should_speak=True,
                reason="玩家讨论长时间没有新消息，做本轮唯一一次轻量续接。",
                reply=reply,
                intent=SpeechIntent(
                    act="gentle_nudge",
                    reason="第零章讨论进入静默周期。",
                    tone="轻松、不催促",
                    max_sentences=1,
                    must_reply=False,
                    can_be_silent=True,
                    avoid=("重复完整清单", "连续追问刚才最活跃的玩家", "机械汇报进度"),
                ),
                telemetry=telemetry,
            )

        if self._presence_is_too_high(recent_gm_ratio, recent_message_count) and not force:
            return self._silent("近期 GM 发言占比偏高，继续给玩家留出讨论空间。", telemetry)

        if not force and idle_seconds < thresholds["adventure"]:
            return self._silent("自由场景尚未达到等待阈值。", telemetry)
        if last_entry_role != "assistant" and not force:
            return self._silent("玩家仍在讨论或声明行动，GM 不抢话。", telemetry)
        if force:
            instruction = heartbeat_instruction or (
                "这是明确要求主持人介入的强制节拍。先核对当前权威触发，"
                "只在确有依据时让局面发生一个具体变化。"
            )
            return PresenceDecision(
                action="free_scene_beat",
                should_speak=True,
                reason="收到明确的强制主动节拍请求。",
                priority="explicit_request",
                instruction=instruction,
                intent=SpeechIntent(
                    act="scene_beat",
                    reason="主持人被明确要求介入当前局面。",
                    must_reply=False,
                    can_be_silent=True,
                    avoid=("替玩家行动", "幕后框架术语", "列出两三个选项"),
                ),
                telemetry=telemetry,
            )
        adventure_nudge_limit = min(1, max(0, int(adventure_nudge_limit)))
        if adventure_nudge_limit <= 0:
            return self._silent("当前配置不允许在冒险冷场时主动招呼。", telemetry)
        if adventure_nudge_count >= adventure_nudge_limit:
            return self._silent("本轮冒险静默周期已经招呼过一次，等待玩家回来。", telemetry)
        instruction = heartbeat_instruction or (
            "玩家在现实群聊中暂时沉默。这不表示游戏内时间经过，也不授权NPC、环境、"
            "命刻或威胁继续行动。只作为同桌的时悠用一句符合当前场况的轻松招呼、"
            "短吐槽或等候语把话头递回来；没有自然说法就保持静默。"
        )
        return PresenceDecision(
            action="adventure_table_nudge",
            should_speak=True,
            reason="冒险场景在 GM 输出后冷场，只做一次不推进虚构时间的桌边招呼。",
            instruction=instruction,
            intent=SpeechIntent(
                act="table_nudge",
                reason="现实群聊暂时停顿。",
                tone="轻松、像同桌GM、不过度催促",
                must_reply=False,
                can_be_silent=True,
                max_sentences=1,
                avoid=(
                    "推进游戏内时间",
                    "新增场景事实",
                    "让NPC或环境行动",
                    "替玩家行动",
                    "复述上一段场景描写",
                    "列出两三个选项",
                ),
            ),
            telemetry=telemetry,
        )

    @staticmethod
    def _presence_is_too_high(recent_gm_ratio: float, recent_message_count: int) -> bool:
        return recent_message_count >= 6 and recent_gm_ratio >= 0.55

    @staticmethod
    def _silent(reason: str, telemetry: dict[str, Any]) -> PresenceDecision:
        return PresenceDecision(action="none", should_speak=False, reason=reason, telemetry=telemetry)
