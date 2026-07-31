from __future__ import annotations

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.portable_device_rules import (
    portable_device_tier_label,
    portable_device_tiers,
)
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.models import (
    Affinity,
    EffectTiming,
    InventoryUseResult,
    ResourceChange,
    StatusEffect,
    TimedEffect,
    TinkererGadgetResult,
)
from fu_gm.skill_library import skill_rank


class TinkererGadgetManager:
    """造物使便携装置与基础库存道具的硬规则结算。"""

    ALCHEMY_TIERS = {
        "basic": (3, 2),
        "基础": (3, 2),
        "advanced": (4, 3),
        "进阶": (4, 3),
        "高级": (4, 3),
        "supreme": (5, 4),
        "顶级": (5, 4),
        "最高": (5, 4),
    }
    DAMAGE_BY_EFFECT_ROLL = {
        3: "wind",
        4: "lightning",
        5: "dark",
        6: "earth",
        7: "fire",
        8: "ice",
    }
    INFUSIONS = {
        "低温": ("ice", 5),
        "霜冻": ("ice", 5),
        "cryo": ("ice", 5),
        "焦火": ("fire", 5),
        "炽焰": ("fire", 5),
        "pyro": ("fire", 5),
        "电压": ("lightning", 5),
        "电击": ("lightning", 5),
        "volt": ("lightning", 5),
        "疾风": ("wind", 5),
        "龙卷": ("wind", 5),
        "cyclone": ("wind", 5),
        "驱邪": ("light", 5),
        "驱魔": ("light", 5),
        "exorcism": ("light", 5),
        "地震": ("earth", 5),
        "seismic": ("earth", 5),
        "暗影": ("dark", 5),
        "shadow": ("dark", 5),
        "毒液": ("poison", 5),
        "猛毒": ("poison", 5),
        "venom": ("poison", 5),
    }

    def __init__(
        self,
        rules_engine: RulesEngine,
        character_manager: CharacterManager,
        conflict_manager: ConflictManager,
    ) -> None:
        self.rules_engine = rules_engine
        self.character_manager = character_manager
        self.conflict_manager = conflict_manager

    def use_inventory_item(
        self,
        actor_name: str,
        item_name: str,
        *,
        target_name: str | None = None,
        damage_type: str = "fire",
        status_effect: StatusEffect | str | None = None,
    ) -> InventoryUseResult:
        target_name = target_name or actor_name
        normalized = item_name.strip()
        if normalized in {"治疗剂", "药剂", "大补药", "Potion", "potion", "Remedy", "remedy"}:
            ip_change = self._spend_ip(actor_name, 3, "使用治疗剂。")
            change = self._modify(target_name, "hp", 50, "治疗剂恢复 HP。")
            return InventoryUseResult(
                actor=actor_name,
                item_name="治疗剂",
                ip_change=ip_change,
                resource_changes=[change],
                summary=f"{actor_name} 消耗 3 IP 使用治疗剂，{target_name} 恢复 50 HP。",
            )
        if normalized in {"圣灵水", "万灵药", "Ether", "ether", "Elixir", "elixir"}:
            ip_change = self._spend_ip(actor_name, 3, "使用圣灵水。")
            change = self._modify(target_name, "mp", 50, "圣灵水恢复 MP。")
            return InventoryUseResult(
                actor=actor_name,
                item_name="圣灵水",
                ip_change=ip_change,
                resource_changes=[change],
                summary=f"{actor_name} 消耗 3 IP 使用圣灵水，{target_name} 恢复 50 MP。",
            )
        if normalized in {"万能药", "滋补药", "Tonic", "tonic"}:
            ip_change = self._spend_ip(actor_name, 2, "使用万能药。")
            removed = self._clear_all_statuses(target_name)
            return InventoryUseResult(
                actor=actor_name,
                item_name="万能药",
                ip_change=ip_change,
                status_changes=removed,
                summary=f"{actor_name} 消耗 2 IP 使用万能药，{target_name} 解除所有异常状态。",
            )
        if normalized in {"元素裂片", "元素水晶", "Elemental Shard", "elemental_shard"}:
            ip_change = self._spend_ip(actor_name, 2, "使用元素裂片。")
            damage = self._apply_fixed_damage(target_name, 10, damage_type)
            return InventoryUseResult(
                actor=actor_name,
                item_name="元素裂片",
                ip_change=ip_change,
                damage_results=[damage],
                summary=f"{actor_name} 消耗 2 IP 使用元素裂片，对 {target_name} 造成 10 点{damage_type}伤害。",
            )
        raise ValueError(f"未知库存道具：{item_name}")

    def use_alchemy(
        self,
        actor_name: str,
        *,
        tier: str = "basic",
        target_roll: int | None = None,
        effect_roll: int | None = None,
        targets: list[str] | None = None,
    ) -> TinkererGadgetResult:
        tier_key = str(tier or "basic").strip().lower()
        if tier_key not in self.ALCHEMY_TIERS:
            raise ValueError("炼金装置的层级只能是基础、进阶或顶级。")
        required_tier = {
            "basic": 1,
            "基础": 1,
            "advanced": 2,
            "高级": 2,
            "进阶": 2,
            "supreme": 3,
            "最高": 3,
            "顶级": 3,
        }[tier_key]
        self.require_portable_device(actor_name, "炼金装置", required_tier)
        cost, dice_count = self.ALCHEMY_TIERS[tier_key]
        ip_change = self._spend_ip(actor_name, cost, "使用炼金术调合药剂。")
        rolls = [self.rules_engine.roll_die(20) for _ in range(dice_count)]
        chosen_target_roll = target_roll or rolls[0]
        chosen_effect_roll = effect_roll or rolls[1 if len(rolls) > 1 else 0]
        resolved_targets = self._resolve_alchemy_targets(actor_name, chosen_target_roll, targets)

        result = TinkererGadgetResult(
            actor=actor_name,
            gadget_type="炼金术",
            mode=tier,
            ip_change=ip_change,
            rolls=rolls,
            target_roll=chosen_target_roll,
            effect_roll=chosen_effect_roll,
            targets=resolved_targets,
        )
        self._apply_alchemy_effect(result)
        result.summary = (
            f"{actor_name} 消耗 {cost} IP 调合{self._tier_text(tier)}炼金药剂，"
            f"骰出 {rolls}，目标骰 {chosen_target_roll}，效果骰 {chosen_effect_roll}。"
        )
        return result

    def prepare_infusion(self, actor_name: str, infusion_name: str) -> TinkererGadgetResult:
        self.require_portable_device(
            actor_name,
            "注魔装置",
            self.infusion_required_tier(infusion_name),
        )
        damage_type, bonus = self.infusion_effect(infusion_name)
        ip_change = self._spend_ip(actor_name, 2, f"使用灌注【{infusion_name}】。")
        result = TinkererGadgetResult(
            actor=actor_name,
            gadget_type="灌注术",
            mode=infusion_name,
            ip_change=ip_change,
            summary=f"{actor_name} 消耗 2 IP 使用【{infusion_name}】灌注，本次攻击改为 {damage_type} 伤害并额外 +{bonus} 伤害。",
        )
        return result

    def infusion_effect(self, infusion_name: str) -> tuple[str, int]:
        key = infusion_name.strip().lower()
        if infusion_name in self.INFUSIONS:
            return self.INFUSIONS[infusion_name]
        if key in self.INFUSIONS:
            return self.INFUSIONS[key]
        raise ValueError(f"未知灌注：{infusion_name}")

    def spend_ip(self, actor_name: str, cost: int, reason: str) -> ResourceChange:
        """给需要组合其他规则动作的魔科技效果使用。"""
        return self._spend_ip(actor_name, cost, reason)

    def require_portable_device(
        self,
        actor_name: str,
        device_name: str,
        minimum_tier: int = 1,
    ) -> int:
        character = self.character_manager.get(actor_name)
        skill_rank = int(character.skills.get("便携装置", 0) or 0)
        if skill_rank <= 0:
            raise ValueError(f"{actor_name} 尚未取得【便携装置】。")
        choices = list(character.skill_options.get("便携装置", []))
        if not choices:
            raise ValueError(
                f"{actor_name} 的【便携装置】还没选定装置类型；请先补选炼金装置、注魔装置或魔导装置。"
            )
        unlocked = portable_device_tiers(choices)
        current_tier = int(unlocked.get(device_name, 0))
        if current_tier < minimum_tier:
            required_label = portable_device_tier_label(minimum_tier)
            if current_tier <= 0:
                raise ValueError(f"{actor_name} 尚未解锁【{device_name}】。")
            raise ValueError(
                f"{actor_name} 的【{device_name}】目前是{portable_device_tier_label(current_tier)}，"
                f"这项功能需要{required_label}增益。"
            )
        return current_tier

    def infusion_required_tier(self, infusion_name: str) -> int:
        damage_type, _bonus = self.infusion_effect(infusion_name)
        if damage_type in {"ice", "fire", "lightning"}:
            return 1
        if damage_type in {"wind", "light", "earth", "dark"}:
            return 2
        return 3

    def create_magicannon(self, actor_name: str, damage_type: str = "physical") -> TinkererGadgetResult:
        self.require_portable_device(actor_name, "魔导装置", 2)
        ip_change = self._spend_ip(actor_name, 2, "创建魔法加农炮。")
        character = self.character_manager.get(actor_name)
        item_name = f"魔法加农炮（{damage_type}）"
        if item_name not in character.equipment:
            character.equipment.append(item_name)
        character.equipped_main_hand = item_name
        character.equipped_off_hand = ""
        character.weapon_damage = 10
        character.weapon_type = damage_type
        character.weapon_range = "ranged"
        character.weapon_accuracy_attributes = ["DEX", "INS"]
        character.weapon_accuracy_modifier = 1
        return TinkererGadgetResult(
            actor=actor_name,
            gadget_type="魔科技",
            mode="魔法加农炮",
            ip_change=ip_change,
            summary=f"{actor_name} 消耗 2 IP 制作并装备【{item_name}】：【敏捷+洞察】+1，【高值+10】，双手远程枪械。",
        )

    def magitech_override(self, actor_name: str, target_name: str, forced_action: str = "指定行动") -> TinkererGadgetResult:
        self.require_portable_device(actor_name, "魔导装置", 1)
        actor = self.character_manager.get(actor_name)
        target = self.character_manager.get(target_name)
        if not ({"construct", "构装体", "构造体", "elemental", "元素"} & set(target.traits)):
            raise ValueError("魔导覆写只能指定构装体或元素敌人。")
        if not ({"mindless", "无心智", "无心智生物"} & set(target.traits)):
            raise ValueError("魔导覆写要求目标无心智。")
        if not target.statuses:
            raise ValueError("魔导覆写要求目标正受到至少一种异常状态影响。")
        before, after = self.character_manager.modify_resource(actor_name, "mp", -10)
        mp_change = ResourceChange(actor_name, "mp", after - before, before, after, "发动魔导覆写。")
        cleared = [status.value for status in target.statuses]
        self.conflict_manager.clear_statuses(target_name)
        self.conflict_manager.register_effect(
            TimedEffect(
                owner=actor_name,
                effect_type="forced_action",
                expires_on=EffectTiming.OWNER_TURN_END,
                target=target_name,
                source="魔导覆写",
                effect_key=f"magitech_override:{target_name}",
                data={"forced_action": forced_action, "revealed": "full_stat_block"},
                note=f"{target_name} 立刻执行 {forced_action}，并向玩家揭示全部数据。",
            )
        )
        return TinkererGadgetResult(
            actor=actor_name,
            gadget_type="魔科技",
            mode="魔导覆写",
            ip_change=mp_change,
            targets=[target_name],
            status_changes=[f"{target_name} 解除异常状态：{', '.join(cleared)}。"],
            summary=(
                f"{actor_name} 消耗 10 MP 对 {target_name} 执行魔导覆写，解除其异常状态，揭示其全部数据，"
                f"并迫使其立即执行：{forced_action}。"
            ),
        )

    def _apply_alchemy_effect(self, result: TinkererGadgetResult) -> None:
        roll = result.effect_roll
        actor = self.character_manager.get(result.actor)
        secret_formula_rank = skill_rank(actor.skills, "秘密配方")
        damage_bonus = secret_formula_rank
        healing_bonus = secret_formula_rank * 5
        if roll == 1:
            for target in result.targets:
                self._register_attribute_buff(result.actor, target, {"DEX": 1, "MIG": 1})
                result.status_changes.append(f"{target} 的 DEX 与 MIG 暂时提升。")
            return
        if roll == 2:
            for target in result.targets:
                self._register_attribute_buff(result.actor, target, {"INS": 1, "WLP": 1})
                result.status_changes.append(f"{target} 的 INS 与 WLP 暂时提升。")
            return
        if roll in self.DAMAGE_BY_EFFECT_ROLL:
            amount = (
                self._level_scaled_amount(actor.level, 20, 30, 40)
                + damage_bonus
            )
            for target in result.targets:
                result.damage_results.append(self._apply_fixed_damage(target, amount, self.DAMAGE_BY_EFFECT_ROLL[roll]))
            return
        if roll in {9, 10, 11}:
            pairs = {
                9: ("wind", "fire"),
                10: ("lightning", "ice"),
                11: ("dark", "earth"),
            }
            for target in result.targets:
                self._register_affinity_buff(result.actor, target, pairs[roll])
                result.status_changes.append(f"{target} 获得 {pairs[roll][0]}/{pairs[roll][1]} 抗性。")
            return
        if roll == 12:
            self._apply_statuses(result, [StatusEffect.ENRAGED])
            return
        if roll == 13:
            self._apply_statuses(result, [StatusEffect.POISONED])
            return
        if roll == 14:
            self._apply_statuses(
                result,
                [StatusEffect.DAZED, StatusEffect.SHAKEN, StatusEffect.SLOW, StatusEffect.WEAKENED],
            )
            return
        if roll == 15:
            for target in result.targets:
                self.conflict_manager.clear_statuses(target)
                result.status_changes.append(f"{target} 清除全部异常状态。")
            return
        if roll in {16, 17}:
            for target in result.targets:
                result.resource_changes.append(self._modify(target, "hp", 50 + healing_bonus, "炼金术恢复 HP。"))
                result.resource_changes.append(self._modify(target, "mp", 50 + healing_bonus, "炼金术恢复 MP。"))
            return
        if roll == 18:
            for target in result.targets:
                result.resource_changes.append(self._modify(target, "hp", 100 + healing_bonus, "炼金术恢复 HP。"))
            return
        if roll == 19:
            for target in result.targets:
                result.resource_changes.append(self._modify(target, "mp", 100 + healing_bonus, "炼金术恢复 MP。"))
            return
        if roll >= 20:
            for target in result.targets:
                result.resource_changes.append(self._modify(target, "hp", 100 + healing_bonus, "炼金术恢复 HP。"))
                result.resource_changes.append(self._modify(target, "mp", 100 + healing_bonus, "炼金术恢复 MP。"))
            return
        for target in result.targets:
            result.resource_changes.append(self._modify(target, "hp", 30 + healing_bonus, "炼金术恢复 HP。"))

    def _resolve_alchemy_targets(self, actor_name: str, roll: int, targets: list[str] | None) -> list[str]:
        if targets:
            return [target for target in targets if self.character_manager.exists(target)]
        actor = self.character_manager.get(actor_name)
        allies = [character.name for character in self.character_manager.all() if "pc" in character.traits]
        enemies = [character.name for character in self.character_manager.all() if "enemy" in character.traits or "villain" in character.traits]
        if 1 <= roll <= 6:
            return [actor_name]
        if 7 <= roll <= 11:
            return enemies[:1]
        if 12 <= roll <= 16:
            return allies or [actor.name]
        return enemies

    def _spend_ip(self, actor_name: str, cost: int, reason: str) -> ResourceChange:
        actor = self.character_manager.get(actor_name)
        if actor.inventory_points < cost:
            raise ValueError(f"{actor_name} 的库存点不足：需要 {cost}，当前 {actor.inventory_points}。")
        before, after = self.character_manager.modify_resource(actor_name, "inventory_points", -cost)
        return ResourceChange(actor_name, "inventory_points", -cost, before, after, reason)

    def _modify(self, target: str, resource: str, amount: int, reason: str) -> ResourceChange:
        before, after = self.character_manager.modify_resource(target, resource, amount)
        return ResourceChange(target, resource, amount, before, after, reason)

    def _apply_fixed_damage(self, target_name: str, amount: int, damage_type: str) -> dict:
        target = self.character_manager.get(target_name)
        damage, affinity = self.rules_engine.compute_damage(0, amount, damage_type, target)
        if damage >= 0:
            before, after = self.character_manager.modify_resource(target_name, "hp", -damage)
        else:
            before, after = self.character_manager.modify_resource(target_name, "hp", abs(damage))
        return {
            "target": target_name,
            "damage_type": damage_type,
            "damage": max(0, damage),
            "healing": abs(damage) if damage < 0 else 0,
            "affinity": affinity.value,
            "hp_before": before,
            "hp_after": after,
        }

    def _clear_one_status(self, target: str, status_effect: StatusEffect | str | None) -> list[str]:
        character = self.character_manager.get(target)
        if not character.statuses:
            return []
        if status_effect is None:
            status = character.statuses[0]
        elif isinstance(status_effect, StatusEffect):
            status = status_effect
        else:
            status = StatusEffect(status_effect)
        removed = self.conflict_manager.remove_status(target, status)
        return [f"{target} 移除 {status.value}。"] if removed else []

    def _clear_all_statuses(self, target: str) -> list[str]:
        character = self.character_manager.get(target)
        statuses = list(character.statuses)
        if not statuses:
            return []
        self.conflict_manager.clear_statuses(target)
        return [f"{target} 移除 {status.value}。" for status in statuses]

    def _apply_statuses(self, result: TinkererGadgetResult, statuses: list[StatusEffect]) -> None:
        for target in result.targets:
            for status in statuses:
                if self.conflict_manager.apply_status(target, status):
                    result.status_changes.append(f"{target} 获得 {status.value}。")

    def _register_attribute_buff(self, owner: str, target: str, bonuses: dict[str, int]) -> None:
        self.conflict_manager.register_effect(
            TimedEffect(
                owner=owner,
                effect_type="attribute_buff",
                expires_on=EffectTiming.OWNER_TURN_END,
                target=target,
                source="炼金术",
                effect_key=f"alchemy_attribute:{target}",
                data={"attribute_bonus": bonuses},
                note="炼金术属性强化持续到使用者回合结束。",
            )
        )

    def _register_affinity_buff(self, owner: str, target: str, damage_types: tuple[str, str]) -> None:
        self.conflict_manager.register_effect(
            TimedEffect(
                owner=owner,
                effect_type="affinity_buff",
                expires_on=EffectTiming.SCENE_END,
                target=target,
                source="炼金术",
                effect_key=f"alchemy_affinity:{target}:{'/'.join(damage_types)}",
                data={"affinity_changes": {damage_type: Affinity.RESIST for damage_type in damage_types}},
                note="炼金术抗性持续到场景结束。",
            )
        )

    def _level_scaled_amount(self, level: int, low: int, mid: int, high: int) -> int:
        if level >= 40:
            return high
        if level >= 20:
            return mid
        return low

    def _tier_text(self, tier: str) -> str:
        return {"basic": "基础", "advanced": "高级", "supreme": "最高"}.get(tier, tier)

