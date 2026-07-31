from __future__ import annotations

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.world_state import WorldState


class SoloPlayManager:
    """Adapts table guidance when one player character carries the campaign."""

    def __init__(self, character_manager: CharacterManager, world_state: WorldState) -> None:
        self.character_manager = character_manager
        self.world_state = world_state

    def is_active(self) -> bool:
        explicit = self.world_state.world_profile.optional_rules.get("solo_play")
        if explicit is not None:
            return bool(explicit.enabled)
        return len(self.player_characters()) == 1

    def player_characters(self):
        return [character for character in self.character_manager.all() if "pc" in character.traits]

    def prompt_guidance(self) -> str:
        if not self.is_active():
            return ""
        return (
            "单人跑团档位：场景仍由玩家决定目标和行动，GM负责让世界与NPC主动回应。"
            "不要用缺少其他职业能力堵死进展；重要信息至少准备两条可行路径。"
            "冲突避免连续剥夺唯一PC的行动，并明确提供撤退、谈判、环境或命刻解法。"
            "轻量伙伴可以补叙事与有限支援，但不能替主角作决定或抢走完整独立回合。"
        )

    def encounter_notes(self, *, boss: bool = False) -> list[str]:
        if not self.is_active():
            return []
        notes = [
            "单人遭遇不要用多名敌人的行动经济围死主角；普通场面优先一名小兵或一名削弱后的精英。",
            "控制效果必须有可观察的解除途径，失败也要保留撤退、投降、谈判或改变环境的路线。",
            "不要假设主角拥有治疗、远程、仪式和调查等所有能力；关键推进至少提供两种不同属性或叙事路径。",
        ]
        if boss:
            notes.append("单人首领战应电报强攻并减少不可逆连锁；可用阶段间准备轮、盟友支援或可倒转命刻调节压力。")
        return notes
