from __future__ import annotations

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.models import ClockChange, RestResult, RestType


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
    ) -> RestResult:
        ip_spent = 0
        if rest_type == RestType.WILDERNESS and safe_source == "魔法帐篷":
            if payer is None:
                raise ValueError("野外使用魔法帐篷休息时需要指定支付 IP 的角色。")
            character = self.character_manager.get(payer)
            tent_cost = self.WILDERNESS_TENT_COST
            if "大口袋" in character.hero_skills:
                tent_cost = max(1, tent_cost - 1)
            if character.inventory_points < tent_cost:
                raise ValueError(f"{payer} 的物资点不足，无法搭建魔法帐篷。")
            before, after = self.character_manager.modify_resource(
                payer,
                "inventory_points",
                -tent_cost,
            )
            ip_spent = before - after

        recovered = []
        for character in self.character_manager.all():
            if "pc" not in character.traits:
                continue
            character.hp = character.max_hp
            character.mp = character.max_mp
            character.trigger_cooldowns.clear()
            self.character_manager.clear_statuses(character.name)
            recovered.append(character.name)

        clock_changes = []
        for clock_name in threat_clocks or []:
            before, after = self.clock_manager.advance(clock_name, 1)
            clock = self.clock_manager.get(clock_name)
            clock_changes.append(
                ClockChange(
                    clock_name=clock.name,
                    before=before,
                    after=after,
                    delta=after - before,
                    max_segments=clock.max_segments,
                    reason="休息让迫近威胁推进 1 格。",
                )
            )

        return RestResult(
            rest_type=rest_type,
            safe_source=safe_source,
            recovered_characters=recovered,
            ip_spent=ip_spent,
            threat_clock_changes=clock_changes,
            summary=f"队伍完成休息：{safe_source}。全部 PC 恢复 HP/MP，并解除异常状态。",
        )
