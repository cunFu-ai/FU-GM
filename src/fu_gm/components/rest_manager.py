from __future__ import annotations

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.models import Clock, ClockChange, RestResult, RestType
from fu_gm.skill_library import has_skill_name


class RestManager:
    """执行休息恢复，并让长期威胁在休息时推进。"""

    WILDERNESS_TENT_COST = 4

    def __init__(self, character_manager: CharacterManager, clock_manager: ClockManager) -> None:
        self.character_manager = character_manager
        self.clock_manager = clock_manager

    def rest(
        self,
        rest_type: RestType,
        *,
        safe_source: str,
        payer: str | None = None,
        threat_clocks: list[str] | None = None,
        participants: list[str] | None = None,
    ) -> RestResult:
        recovered_names, clocks_to_advance, tent_cost = self.validate(
            rest_type,
            safe_source=safe_source,
            payer=payer,
            threat_clocks=threat_clocks,
            participants=participants,
        )

        ip_spent = 0
        if tent_cost:
            before, after = self.character_manager.modify_resource(
                payer,
                "inventory_points",
                -tent_cost,
            )
            ip_spent = before - after

        recovered = []
        for name in recovered_names:
            character = self.character_manager.get(name)
            character.hp = character.max_hp
            character.mp = character.max_mp
            character.trigger_cooldowns.clear()
            self.character_manager.clear_statuses(character.name)
            recovered.append(character.name)

        clock_changes = []
        for clock in clocks_to_advance:
            before, after = self.clock_manager.advance(clock.name, 1)
            clock_changes.append(
                ClockChange(
                    clock_name=clock.name,
                    before=before,
                    after=after,
                    delta=after - before,
                    max_segments=clock.max_segments,
                    reason="休息让迫近威胁推进 1 格。",
                    clock_type=clock.clock_type,
                    stakes=clock.stakes,
                    completion_consequence=clock.completion_consequence,
                )
            )

        recovered_text = "、".join(recovered)
        return RestResult(
            rest_type=rest_type,
            safe_source=safe_source,
            recovered_characters=recovered,
            ip_spent=ip_spent,
            threat_clock_changes=clock_changes,
            summary=f"【{recovered_text}】在{safe_source}完成休息，恢复全部生命值与精神值，并解除异常状态。",
        )

    def validate(
        self,
        rest_type: RestType,
        *,
        safe_source: str,
        payer: str | None = None,
        threat_clocks: list[str] | None = None,
        participants: list[str] | None = None,
    ) -> tuple[list[str], list[Clock], int]:
        """Validate every rest precondition before mutating any resource."""

        recovered_names = self._resting_pc_names(participants)
        if not recovered_names:
            raise ValueError("休息至少需要一名实际参与休息的玩家角色。")
        clocks_to_advance = []
        for clock_name in threat_clocks or []:
            clock = self.clock_manager.get(clock_name)
            if (
                not bool(clock.advance_on_rest)
                or str(clock.clock_type or "").strip().lower()
                not in {"threat", "villain", "dungeon", "boss"}
                or str(clock.scope or "").strip().lower()
                not in {"session", "campaign"}
                or str(clock.status or "").strip().lower() != "active"
                or clock.current >= clock.max_segments
            ):
                raise ValueError(
                    f"命刻【{clock.name}】没有登记为会随休息推进的活动跨场景压力。"
                )
            clocks_to_advance.append(clock)

        tent_cost = 0
        if rest_type == RestType.WILDERNESS and safe_source == "魔法帐篷":
            if payer is None:
                raise ValueError("野外使用魔法帐篷休息时需要指定支付 IP 的角色。")
            if payer not in recovered_names:
                raise ValueError(f"{payer} 不在本次休息队伍中，不能为此处的魔法帐篷支付物资点。")
            character = self.character_manager.get(payer)
            tent_cost = self.WILDERNESS_TENT_COST
            if has_skill_name(character.hero_skills, "深藏不露"):
                tent_cost = max(1, tent_cost - 1)
            if character.inventory_points < tent_cost:
                raise ValueError(f"{payer} 的物资点不足，无法搭建魔法帐篷。")
        return recovered_names, clocks_to_advance, tent_cost

    def _resting_pc_names(self, participants: list[str] | None) -> list[str]:
        requested = (
            list(participants)
            if participants is not None
            else [
                character.name
                for character in self.character_manager.all()
                if "pc" in character.traits
            ]
        )
        result: list[str] = []
        for value in requested:
            name = str(value or "").strip()
            if not name or name in result or not self.character_manager.exists(name):
                continue
            if "pc" not in self.character_manager.get(name).traits:
                continue
            result.append(name)
        return result
