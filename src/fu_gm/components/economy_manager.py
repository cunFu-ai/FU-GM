from __future__ import annotations

import re

from fu_gm.components.character_creation_manager import ARMOR_TABLE, SHIELD_TABLE, WEAPON_TABLE
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.equipment_effect_manager import EquipmentEffectManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.sheet_exporter import DAMAGE_TYPE_LABELS
from fu_gm.components.world_state import WorldState
from fu_gm.equipment_catalog import (
    EquipmentExample,
    get_equipment_example,
    search_equipment_examples,
)
from fu_gm.models import (
    ChestReward,
    DungeonAreaType,
    DungeonRewardPlacement,
    DungeonState,
    EquipmentItemType,
    PersistentChangeType,
    RareItemDesign,
    RareItemQuality,
    RewardBudget,
    ServiceTransaction,
    SessionReward,
    ShopTransaction,
    TransportPurchase,
)
from fu_gm.skill_library import has_skill_name, skill_rank


class EconomyManager:
    """商店、库存补充、宝箱与阶段奖励的第一版经济闭环。"""

    _LOADOUT_FIELDS = {
        "main_hand": "equipped_main_hand",
        "off_hand": "equipped_off_hand",
        "armor": "equipped_armor",
        "shield": "equipped_shield",
        "accessory": "equipped_accessory",
    }

    LODGING_COSTS = {
        "village": 5,
        "村庄": 5,
        "settlement": 10,
        "town": 10,
        "小镇": 10,
        "城镇": 10,
        "city": 20,
        "城市": 20,
    }

    REWARD_BUDGET_TABLE = {
        5: {"max_item": 500, 2: 500, 3: 750, 4: 1000},
        10: {"max_item": 1000, 2: 800, 3: 1200, 4: 1800},
        20: {"max_item": 1500, 2: 1000, 3: 1500, 4: 2000},
        30: {"max_item": 2000, 2: 1600, 3: 2400, 4: 3200},
        40: {"max_item": None, 2: 2000, 3: 3000, 4: 4000},
    }

    RARE_QUALITIES = {
        "状态抗性": RareItemQuality("状态抗性", "any", 500, "你对一种异常状态免疫。", ["defense"]),
        "抵抗": RareItemQuality("抵抗", "any", 700, "你对一种非物理伤害类型获得抵抗相性。", ["defense"]),
        "护身符": RareItemQuality("护身符", "any", 800, "你的魔防获得 +1 修正值。", ["defense"]),
        "坚守": RareItemQuality("坚守", "any", 800, "你的物防获得 +1 修正值。", ["defense"]),
        "双重抵抗": RareItemQuality("双重抵抗", "any", 1000, "你对两种非物理伤害类型获得抵抗相性。", ["defense"]),
        "断剑": RareItemQuality("断剑", "any", 1000, "你对物理伤害获得抵抗相性。", ["defense"]),
        "免疫": RareItemQuality("免疫", "any", 1500, "你对一种非物理伤害类型免疫。", ["defense"]),
        "全能护盾": RareItemQuality("全能护盾", "any", 2000, "你的物防和魔防获得 +1 修正值。", ["defense"]),
        "完美体魄": RareItemQuality("完美体魄", "any", 2000, "你对所有异常状态免疫。", ["defense"]),
        "魔力": RareItemQuality("魔力", EquipmentItemType.WEAPON, 100, "这件武器针对魔防进行攻击。", ["offense"]),
        "猎人": RareItemQuality("猎人", EquipmentItemType.WEAPON, 300, "对一种特定物种造成 5 点额外伤害。", ["offense"]),
        "穿透": RareItemQuality("穿透", EquipmentItemType.WEAPON, 400, "这件武器造成的伤害无视抵抗相性。", ["offense"]),
        "双重猎人": RareItemQuality("双重猎人", EquipmentItemType.WEAPON, 500, "对两种特定物种造成 5 点额外伤害。", ["offense"]),
        "多重攻击": RareItemQuality("多重攻击", EquipmentItemType.WEAPON, 1000, "这件武器的攻击具有多重攻击（2）特性。", ["offense"]),
        "施加异常": RareItemQuality("施加异常", EquipmentItemType.WEAPON, 1500, "命中后施加迟缓、动摇、虚弱或眩晕之一。", ["offense"]),
        "施加强力异常": RareItemQuality("施加强力异常", EquipmentItemType.WEAPON, 2000, "命中后施加激怒或中毒之一。", ["offense"]),
        "先攻强化": RareItemQuality("先攻强化", "protective", 500, "你的先攻获得 +4 修正值。", ["utility"]),
        "命中强化": RareItemQuality("命中强化", "protective", 1000, "你的命中检定获得 +1 修正值。", ["offense"]),
        "魔力强化": RareItemQuality("魔力强化", "protective", 1000, "你的施法检定获得 +1 修正值。", ["offense"]),
        "生命强化": RareItemQuality("生命强化", "protective", 1000, "当你恢复生命值时，额外恢复 5 点生命值。", ["support"]),
        "治疗强化": RareItemQuality("治疗强化", "protective", 1500, "你施放的恢复 HP 法术额外恢复 5 点 HP。", ["support"]),
        "法力强化": RareItemQuality("法力强化", "protective", 2000, "你施放的法术造成 5 点额外伤害。", ["offense"]),
        "武器强化": RareItemQuality("武器强化", "protective", 2000, "你使用一种指定攻击类型造成 5 点额外伤害。", ["offense"]),
        "改变伤害": RareItemQuality("改变伤害", EquipmentItemType.ACCESSORY, 300, "你的武器、法术和技能造成的伤害变为指定类型。", ["offense"]),
    }

    RARE_ITEMS = {
        "星屑罗盘": "在地下城中指向最近的隐藏门或宝箱；每个场景最多触发一次叙事线索。",
        "回声护符": "第一次调查失败时，GM 可以给出一个模糊但有用的回声线索。",
        "雷纹护手": "适合雷属性战斗风格的稀有装备，后续可扩展为完整装备品质。",
        "记忆钥匙": "能打开一处与英雄过去相关的锁或封印。",
        "银爪": "格斗类稀有武器，造成光系伤害，适合预示不死族弱点。",
        "雷霆之弓": "弓类稀有武器，造成雷系伤害，并让持有者对雷系伤害获得抵抗相性。",
        "闪电圣盾": "盾牌类稀有装备，让持有者对雷系伤害获得抵抗相性。",
        "物语之戒": "当持有者掷出大成功时，可以将机会用于获得 1 点物语点。",
    }

    def __init__(
        self,
        character_manager: CharacterManager,
        world_state: WorldState,
        rules_engine: RulesEngine,
    ) -> None:
        self.character_manager = character_manager
        self.world_state = world_state
        self.rules_engine = rules_engine
        self.equipment_effects = EquipmentEffectManager()

    def restock_inventory(self, actor_name: str, quantity: int) -> ShopTransaction:
        actor = self.character_manager.get(actor_name)
        max_ip = actor.max_inventory_points or 6
        quantity = max(0, min(quantity, max_ip - actor.inventory_points))
        total_cost = quantity * 10
        self._spend_zenit(actor_name, total_cost)
        ip_before = actor.inventory_points
        self.character_manager.modify_resource(actor_name, "inventory_points", quantity)
        ip_after = self.character_manager.get(actor_name).inventory_points
        return ShopTransaction(
            actor=actor_name,
            item_name="库存点补充",
            quantity=quantity,
            total_cost=total_cost,
            zenit_before=actor.zenit + total_cost,
            zenit_after=actor.zenit,
            ip_before=ip_before,
            ip_after=ip_after,
            summary=f"{actor_name} 花费 {total_cost}Z 补充 {quantity} 点库存点（{ip_before}->{ip_after}）。",
        )

    def buy_lodging(
        self,
        payer_name: str,
        *,
        settlement_size: str = "town",
        party_size: int = 1,
    ) -> ServiceTransaction:
        unit_cost = self._lodging_unit_cost(settlement_size)
        total_cost = unit_cost * max(1, party_size)
        payer = self.character_manager.get(payer_name)
        before = payer.zenit
        self._spend_zenit(payer_name, total_cost)
        transaction = ServiceTransaction(
            payer=payer_name,
            service_name="旅馆休息",
            service_type="lodging",
            total_cost=total_cost,
            zenit_before=before,
            zenit_after=payer.zenit,
            party_size=max(1, party_size),
            settlement_size=settlement_size,
            summary=(
                f"{payer_name} 支付 {total_cost}Z，为 {max(1, party_size)} 人购买"
                f"{self._settlement_label(settlement_size)}旅馆休息服务。"
            ),
        )
        self.world_state.record_memory_event(
            transaction.summary,
            kind="service",
            entities=[payer_name],
            tags=["lodging", settlement_size],
        )
        return transaction

    def pay_travel_service(
        self,
        payer_name: str,
        transport: str,
        *,
        days: int,
        party_size: int = 1,
    ) -> ServiceTransaction:
        option = self._travel_option(transport)
        if option.owned:
            raise ValueError(f"【{transport}】是可购买交通工具，不是按日雇佣的旅行服务。")
        total_cost = option.price * max(0, days) * max(1, party_size)
        payer = self.character_manager.get(payer_name)
        before = payer.zenit
        self._spend_zenit(payer_name, total_cost)
        transaction = ServiceTransaction(
            payer=payer_name,
            service_name=transport,
            service_type="travel_service",
            total_cost=total_cost,
            zenit_before=before,
            zenit_after=payer.zenit,
            party_size=max(1, party_size),
            days=max(0, days),
            transport=transport,
            summary=f"{payer_name} 支付 {total_cost}Z，雇佣【{transport}】服务 {max(0, days)} 日，覆盖 {max(1, party_size)} 人。",
        )
        self.world_state.record_memory_event(
            transaction.summary,
            kind="service",
            entities=[payer_name, transport],
            tags=["travel_service", option.route_type.value],
        )
        return transaction

    def buy_transport(
        self,
        buyer_name: str,
        transport_name: str,
        *,
        owner: str = "小队",
    ) -> TransportPurchase:
        option = self._travel_option(transport_name)
        if not option.owned:
            raise ValueError(f"【{transport_name}】是按日服务，不能作为长期交通工具购买。")
        buyer = self.character_manager.get(buyer_name)
        before = buyer.zenit
        self._spend_zenit(buyer_name, option.price)
        asset = self.world_state.record_created_asset(
            change_type=PersistentChangeType.TRANSPORT,
            name=option.name,
            description=(
                f"{option.route_type.value}交通工具，可搭载约 {option.passenger_capacity} 人，"
                f"旅行距离倍率 x{option.travel_multiplier}。"
            ),
            source="交通工具购买",
            owner=owner,
            tags=["transport", option.route_type.value],
        )
        purchase = TransportPurchase(
            buyer=buyer_name,
            transport_name=option.name,
            total_cost=option.price,
            zenit_before=before,
            zenit_after=buyer.zenit,
            owner=owner,
            passenger_capacity=option.passenger_capacity,
            travel_multiplier=option.travel_multiplier,
            route_type=option.route_type,
            created_asset=asset,
            summary=f"{buyer_name} 花费 {option.price}Z，为{owner}购买【{option.name}】。",
        )
        self.world_state.record_memory_event(
            purchase.summary,
            kind="transport_purchase",
            entities=[buyer_name, owner, option.name],
            tags=["transport", option.route_type.value],
        )
        return purchase

    def buy_item(self, actor_name: str, item_name: str, *, quantity: int = 1, equip: bool = False) -> ShopTransaction:
        actor = self.character_manager.get(actor_name)
        quantity = max(1, quantity)
        clean_name = self.clean_item_name(item_name)
        unit_price = self.item_price(item_name)
        total_cost = unit_price * quantity
        before = actor.zenit
        # Restricted equipment may be bought and carried by anyone. The class
        # permission applies only when the character actually equips it.
        if equip:
            self._ensure_equipment_permission(actor_name, clean_name)
        self._spend_zenit(actor_name, total_cost)
        for _ in range(quantity):
            actor.equipment.append(clean_name)
        if equip:
            self._equip_if_possible(actor_name, clean_name)
        return ShopTransaction(
            actor=actor_name,
            item_name=clean_name,
            quantity=quantity,
            total_cost=total_cost,
            zenit_before=before,
            zenit_after=actor.zenit,
            added_items=[clean_name] * quantity,
            summary=f"{actor_name} 花费 {total_cost}Z 购买 {quantity} 个【{clean_name}】。",
        )

    def equip_items(self, actor_name: str, item_names: list[str], *, allow_armor: bool = False) -> list[str]:
        actor = self.character_manager.get(actor_name)
        equipped: list[str] = []
        for raw_name in item_names:
            item_name = self.clean_item_name(raw_name)
            if not item_name:
                continue
            template_name = self._template_item_name(actor, item_name)
            if not allow_armor and (template_name in ARMOR_TABLE or self._is_catalog_type(template_name, EquipmentItemType.ARMOR)):
                raise ValueError("冲突中的装备行动不能更换防具。")
            if item_name not in actor.equipment and template_name not in actor.equipment:
                raise ValueError(f"{actor_name} 的背包中没有【{item_name}】。")
            self._ensure_equipment_accessible(actor_name, item_name)
            self._equip_if_possible(actor_name, item_name)
            equipped.append(item_name)
        if equipped:
            self.refresh_equipment_effects(actor_name)
        return equipped

    def configure_loadout(
        self,
        actor_name: str,
        slot_updates: dict[str, str],
        *,
        allow_armor: bool = False,
        require_empty_slots: bool = False,
    ) -> dict[str, str]:
        """Apply an explicit equipment-slot update from the GM tool boundary."""

        actor = self.character_manager.get(actor_name)
        normalized = self.validate_loadout_update(
            actor_name,
            slot_updates,
            allow_armor=allow_armor,
            require_empty_slots=require_empty_slots,
        )

        previous = {
            "main_hand": actor.equipped_main_hand,
            "off_hand": actor.equipped_off_hand,
            "armor": actor.equipped_armor,
            "shield": actor.equipped_shield,
            "accessory": actor.equipped_accessory,
        }
        try:
            for slot, item_name in normalized.items():
                if slot == "main_hand":
                    actor.equipped_main_hand = item_name or "徒手攻击"
                elif slot == "off_hand":
                    if item_name and self._equipment_kind(actor, item_name) == "shield":
                        actor.equipped_shield = item_name
                        actor.equipped_off_hand = ""
                    else:
                        actor.equipped_off_hand = item_name
                        if item_name:
                            actor.equipped_shield = ""
                elif slot == "armor":
                    actor.equipped_armor = item_name or "无防具"
                elif slot == "shield":
                    actor.equipped_shield = item_name
                    if item_name:
                        actor.equipped_off_hand = ""
                elif slot == "accessory":
                    actor.equipped_accessory = item_name

            main_hands = self._weapon_hands(actor, actor.equipped_main_hand)
            if main_hands >= 2:
                actor.equipped_off_hand = ""
                actor.equipped_shield = ""
            elif actor.equipped_off_hand and actor.equipped_shield:
                raise ValueError("副手武器与盾牌不能同时占用同一只手。")
            if (
                actor.equipped_off_hand
                and self._weapon_hands(actor, actor.equipped_off_hand) != 1
            ):
                raise ValueError("副手只能装备单手武器。")

            self._apply_main_weapon_profile(actor)
            self.refresh_equipment_effects(actor_name)
        except (KeyError, TypeError, ValueError):
            actor.equipped_main_hand = previous["main_hand"]
            actor.equipped_off_hand = previous["off_hand"]
            actor.equipped_armor = previous["armor"]
            actor.equipped_shield = previous["shield"]
            actor.equipped_accessory = previous["accessory"]
            self._apply_main_weapon_profile(actor)
            self.refresh_equipment_effects(actor_name)
            raise
        return {
            "main_hand": actor.equipped_main_hand,
            "off_hand": actor.equipped_off_hand,
            "armor": actor.equipped_armor,
            "shield": actor.equipped_shield,
            "accessory": actor.equipped_accessory,
        }

    def validate_loadout_update(
        self,
        actor_name: str,
        slot_updates: dict[str, str],
        *,
        allow_armor: bool = False,
        require_empty_slots: bool = False,
    ) -> dict[str, str]:
        """Normalize and validate a loadout update without mutating the actor."""

        actor = self.character_manager.get(actor_name)
        aliases = {
            "main_hand": "main_hand",
            "主手": "main_hand",
            "off_hand": "off_hand",
            "副手": "off_hand",
            "armor": "armor",
            "防具": "armor",
            "shield": "shield",
            "盾牌": "shield",
            "accessory": "accessory",
            "饰品": "accessory",
        }
        normalized: dict[str, str] = {}
        for raw_slot, raw_item in dict(slot_updates or {}).items():
            slot = aliases.get(str(raw_slot).strip())
            if slot is None:
                raise ValueError(f"未知装备栏位：{raw_slot}")
            if slot == "armor" and not allow_armor:
                raise ValueError("冲突中的装备行动不能更换或卸下防具。")
            item_name = self.clean_item_name(str(raw_item or ""))
            if item_name in {"空", "无", "卸下", "不装备"}:
                item_name = ""
            normalized[slot] = item_name

        for slot, item_name in normalized.items():
            if not item_name:
                continue
            if slot == "main_hand" and item_name == "徒手攻击":
                continue
            if slot == "armor" and item_name == "无防具":
                continue
            template_name = self._template_item_name(actor, item_name)
            if item_name not in actor.equipment and template_name not in actor.equipment:
                raise ValueError(f"{actor_name} 的背包中没有【{item_name}】。")
            self._ensure_equipment_accessible(actor_name, item_name)
            self._ensure_equipment_permission(actor_name, item_name)
            kind = self._equipment_kind(actor, item_name)
            allowed_kinds = {
                "main_hand": {"weapon", "shield"} if has_skill_name(actor.skills, "双盾战士") else {"weapon"},
                "off_hand": {"weapon", "shield"},
                "armor": {"armor"},
                "shield": {"shield"},
                "accessory": {"accessory"},
            }[slot]
            if kind not in allowed_kinds:
                raise ValueError(f"【{item_name}】不能装备到{slot}栏。")
            if slot == "off_hand" and kind == "weapon" and self._weapon_hands(actor, item_name) != 1:
                raise ValueError("副手只能装备单手武器。")

        if require_empty_slots:
            for slot, item_name in normalized.items():
                if not item_name:
                    continue
                occupied = ""
                if slot == "main_hand":
                    if actor.equipped_main_hand not in {"", "徒手攻击"}:
                        occupied = actor.equipped_main_hand
                    elif self._weapon_hands(actor, item_name) >= 2:
                        occupied = actor.equipped_off_hand or actor.equipped_shield
                elif slot in {"off_hand", "shield"}:
                    occupied = actor.equipped_off_hand or actor.equipped_shield
                    if not occupied and self._weapon_hands(actor, actor.equipped_main_hand) >= 2:
                        occupied = actor.equipped_main_hand
                elif slot == "armor" and actor.equipped_armor not in {"", "无防具"}:
                    occupied = actor.equipped_armor
                elif slot == "accessory":
                    occupied = actor.equipped_accessory
                if occupied:
                    raise ValueError(
                        f"拾取后立即装备只能放入空栏位；{slot} 当前由【{occupied}】占用。"
                    )

        return normalized

    def equipped_slots_for_item(self, actor_name: str, item_name: str) -> list[str]:
        """Return every authoritative loadout slot occupied by this exact item."""

        actor = self.character_manager.get(actor_name)
        requested = self.clean_item_name(item_name)
        if not requested:
            return []
        return [
            slot
            for slot, field_name in self._LOADOUT_FIELDS.items()
            if self.clean_item_name(str(getattr(actor, field_name) or "")) == requested
        ]

    def set_equipment_access(
        self,
        actor_name: str,
        item_names: list[str],
        *,
        available: bool,
        reason: str = "",
        location: str = "",
        restore_loadout: bool = False,
        allow_restore_loadout: bool = True,
    ) -> dict[str, object]:
        """Change physical access without changing ownership.

        Items in an evidence locker remain on the character sheet, but cannot
        provide equipment effects or be selected by an Equip action.  The old
        slots are retained privately so an explicit later retrieval can restore
        the prior loadout without guessing which identical weapon went where.
        """

        actor = self.character_manager.get(actor_name)
        resolved = list(
            dict.fromkeys(
                self._resolve_owned_item_name(actor, raw_name)
                for raw_name in item_names
                if self.clean_item_name(str(raw_name or ""))
            )
        )
        if not resolved:
            raise ValueError("必须指定至少一件角色实际拥有的装备。")
        if restore_loadout and (not available or not allow_restore_loadout):
            raise ValueError("当前不能在恢复取用权的同时自动恢复原装备栏位。")

        changed: list[str] = []
        loadout_changed = False
        if not available:
            metadata = {
                "reason": str(reason or "").strip(),
                "location": str(location or "").strip(),
            }
            for item_name in resolved:
                if actor.unavailable_equipment.get(item_name) != metadata:
                    changed.append(item_name)
                actor.unavailable_equipment[item_name] = dict(metadata)
                for slot, field_name in self._LOADOUT_FIELDS.items():
                    if getattr(actor, field_name) != item_name:
                        continue
                    actor.suspended_equipment_slots[slot] = item_name
                    setattr(
                        actor,
                        field_name,
                        "无防具" if slot == "armor" else (
                            "徒手攻击" if slot == "main_hand" else ""
                        ),
                    )
        else:
            for item_name in resolved:
                if item_name in actor.unavailable_equipment:
                    changed.append(item_name)
                actor.unavailable_equipment.pop(item_name, None)
            if restore_loadout:
                slot_updates = {
                    slot: item_name
                    for slot, item_name in list(
                        actor.suspended_equipment_slots.items()
                    )
                    if item_name in resolved
                }
                if slot_updates:
                    loadout_changed = True
                    self.configure_loadout(
                        actor_name,
                        slot_updates,
                        allow_armor=True,
                    )
                    for slot in slot_updates:
                        actor.suspended_equipment_slots.pop(slot, None)
            else:
                for slot, item_name in list(
                    actor.suspended_equipment_slots.items()
                ):
                    if item_name in resolved:
                        actor.suspended_equipment_slots.pop(slot, None)

        self._apply_main_weapon_profile(actor)
        self.refresh_equipment_effects(actor_name)
        return {
            "actor": actor_name,
            "available": bool(available),
            "items": resolved,
            "changed_items": changed,
            "loadout_changed": loadout_changed,
            "reason": str(reason or "").strip(),
            "location": str(location or "").strip(),
            "restored_loadout": bool(restore_loadout),
            "unavailable_equipment": {
                name: dict(value)
                for name, value in actor.unavailable_equipment.items()
            },
            "equipped": {
                slot: getattr(actor, field_name)
                for slot, field_name in self._LOADOUT_FIELDS.items()
            },
        }

    def resolve_owned_equipment_name(self, actor_name: str, item_name: str) -> str:
        """Return the inventory label represented by a concrete name/template."""

        return self._resolve_owned_item_name(
            self.character_manager.get(actor_name),
            item_name,
        )

    def sell_item(self, actor_name: str, item_name: str, *, quantity: int = 1, price_ratio: float = 0.5) -> ShopTransaction:
        actor = self.character_manager.get(actor_name)
        clean_name = self.clean_item_name(item_name)
        quantity = max(1, quantity)
        if not 0 <= float(price_ratio) <= 1:
            raise ValueError("出售价格比例必须在0到1之间。")
        owned = [item for item in actor.equipment if item == clean_name]
        if len(owned) < quantity:
            raise ValueError(f"{actor_name} 的背包中没有足够数量的【{clean_name}】。")
        self._ensure_equipment_accessible(actor_name, clean_name)
        unit_price = self.item_price(clean_name)
        total_gain = max(0, int(unit_price * price_ratio) * quantity)
        before = actor.zenit
        for _ in range(quantity):
            actor.equipment.remove(clean_name)
        actor.zenit += total_gain
        for slot in ("equipped_main_hand", "equipped_off_hand", "equipped_armor", "equipped_shield", "equipped_accessory"):
            if getattr(actor, slot) == clean_name:
                setattr(actor, slot, "" if slot != "equipped_armor" else "无防具")
        self.refresh_equipment_effects(actor_name)
        return ShopTransaction(
            actor=actor_name,
            item_name=clean_name,
            quantity=quantity,
            total_cost=-total_gain,
            zenit_before=before,
            zenit_after=actor.zenit,
            removed_items=[clean_name] * quantity,
            summary=f"{actor_name} 出售 {quantity} 个【{clean_name}】，获得 {total_gain}Z。",
        )

    def open_chest(
        self,
        opener_name: str,
        chest_name: str = "宝箱",
        *,
        rarity: str = "standard",
        fixed_item: str = "",
        fixed_zenit: int | None = None,
    ) -> ChestReward:
        opener = self.character_manager.get(opener_name)
        if fixed_zenit is None:
            base = 30 if rarity == "minor" else 80 if rarity == "standard" else 150
            zenit = base + self.rules_engine.roll_die(6) * 10
        else:
            zenit = max(0, fixed_zenit)

        items: list[str] = []
        rare_items: list[str] = []
        if fixed_item:
            item = self.clean_item_name(fixed_item)
            if not self.is_registered_reward_item(item):
                raise ValueError(
                    f"宝箱固定奖励【{item}】未登记；"
                    "剧情描述不能直接作为角色库存，先配置标准物品或使用专门的剧情物件能力。"
                )
        elif rarity in {"rare", "major", "boss"}:
            item = self._random_rare_item()
        else:
            item = "治疗剂" if self.rules_engine.roll_die(2) == 1 else "元素裂片"

        opener.zenit += zenit
        equipment_example = self.equipment_reference(item)
        if item in self.RARE_ITEMS or equipment_example is not None:
            rare_items.append(item)
            opener.equipment.append(item)
            self.world_state.record_created_asset(
                change_type=PersistentChangeType.EQUIPMENT,
                name=item,
                description=self.RARE_ITEMS.get(item, equipment_example.summary if equipment_example else "稀有装备。"),
                source=f"宝箱：{chest_name}",
                owner=opener_name,
                tags=["chest", "rare"],
            )
        else:
            items.append(item)
            opener.equipment.append(item)

        reward = ChestReward(
            opener=opener_name,
            chest_name=chest_name,
            zenit=zenit,
            items=items,
            rare_items=rare_items,
            summary=f"{opener_name} 打开【{chest_name}】，获得 {zenit}Z 和【{item}】。",
            hard_rule_summary=(
                f"宝箱硬结算：{opener_name} 打开【{chest_name}】；稀有度 {rarity}；"
                f"获得 {zenit}Z；获得物品【{item}】；不得额外增减金币、物品、IP 或装备效果。"
            ),
            llm_narrative_prompt=self._reward_llm_prompt(
                reward_kind="宝箱",
                source=chest_name,
                recipients=[opener_name],
                zenit=zenit,
                items=[item],
                rarity=rarity,
            ),
        )
        self.world_state.record_memory_event(
            reward.summary,
            kind="treasure",
            entities=[opener_name, chest_name],
            tags=["chest", rarity],
        )
        return reward

    def award_session_treasure(
        self,
        recipients: list[str],
        *,
        party_level: int,
        difficulty: str = "normal",
        rare_item: str = "",
    ) -> SessionReward:
        budget = self.reward_budget(party_level, len(recipients))
        total_zenit = budget.average_value
        if difficulty in {"minor", "easy"}:
            total_zenit = max(0, total_zenit // 2)
        share = total_zenit // max(1, len(recipients))
        for name in recipients:
            if self.character_manager.exists(name):
                self.character_manager.get(name).zenit += share
        rare_items: list[str] = []
        if rare_item or difficulty in {"boss", "hard"}:
            item = rare_item or self._random_rare_item()
            rare_items.append(item)
            owner = recipients[0] if recipients else "小队"
            if self.character_manager.exists(owner):
                self.character_manager.get(owner).equipment.append(item)
            self.world_state.record_created_asset(
                change_type=PersistentChangeType.EQUIPMENT,
                name=item,
                description=self.RARE_ITEMS.get(item, "战后获得的稀有装备。"),
                source="阶段奖励",
                owner=owner,
                tags=["reward", difficulty],
            )
        reward = SessionReward(
            party_level=party_level,
            zenit=total_zenit,
            rare_items=rare_items,
            summary=(
                f"阶段奖励：队伍获得总计 {total_zenit}Z"
                + (f"，稀有物品【{rare_items[0]}】。" if rare_items else "。")
                + f"（参考预算：{budget.summary}）"
            ),
            hard_rule_summary=(
                f"阶段奖励硬结算：队伍等级 {party_level}，难度 {difficulty}，"
                f"总金币 {total_zenit}Z，单人份额 {share}Z，稀有物品：{', '.join(rare_items) if rare_items else '无'}。"
                "不得额外增减金币、物品、经验或升级。"
            ),
            llm_narrative_prompt=self._reward_llm_prompt(
                reward_kind="阶段奖励",
                source="本阶段冒险",
                recipients=recipients,
                zenit=total_zenit,
                items=rare_items,
                rarity=difficulty,
            ),
        )
        self.world_state.record_memory_event(reward.summary, kind="session_reward", entities=recipients, tags=["reward"])
        return reward

    def plan_dungeon_rewards(
        self,
        dungeon_state: DungeonState,
        *,
        party_level: int,
        pc_count: int,
        rare_items: list[str] | None = None,
    ) -> list[DungeonRewardPlacement]:
        """把一场地下城的预算拆成多个区域奖励，并写回区域数据。"""

        reward_areas = [
            area
            for area in dungeon_state.areas
            if area.area_type in {DungeonAreaType.TREASURE, DungeonAreaType.BOSS} or area.treasure
        ]
        if not reward_areas:
            return []
        budget = self.reward_budget(party_level, pc_count)
        per_area = max(0, budget.average_value // len(reward_areas))
        rare_queue = list(rare_items or [])
        placements: list[DungeonRewardPlacement] = []

        for index, area in enumerate(reward_areas):
            is_boss = area.area_type == DungeonAreaType.BOSS
            rarity = "boss" if is_boss else "rare" if area.area_type == DungeonAreaType.TREASURE else "standard"
            item = ""
            if rare_queue:
                item = rare_queue.pop(0)
            elif is_boss:
                item = self._pick_catalog_reward_item(max_price=budget.max_item_value)
            elif area.area_type == DungeonAreaType.TREASURE:
                item = "治疗剂" if index % 2 == 0 else "元素裂片"

            zenit = per_area
            if not area.treasure:
                area.treasure = "Boss 战利品" if is_boss else "隐藏宝箱"
            area.reward_item = item
            area.reward_zenit = zenit
            area.reward_rarity = rarity
            placement = DungeonRewardPlacement(
                dungeon_name=dungeon_state.name,
                area_name=area.name,
                reward_item=item,
                reward_zenit=zenit,
                rarity=rarity,
                summary=(
                    f"地下城【{dungeon_state.name}】区域【{area.name}】放置奖励："
                    f"{zenit}Z" + (f" 与【{item}】" if item else "") + f"（{rarity}）。"
                ),
                hard_rule_summary=(
                    f"地下城奖励硬配置：地下城【{dungeon_state.name}】，区域【{area.name}】，"
                    f"预算 {zenit}Z，物品：{item or '无'}，稀有度 {rarity}。"
                    "这是 GM 私密配置，不应直接向玩家透露。"
                ),
                llm_narrative_prompt=self._reward_llm_prompt(
                    reward_kind="地下城奖励伏笔",
                    source=f"{dungeon_state.name}/{area.name}",
                    recipients=["未来取得该奖励的角色"],
                    zenit=zenit,
                    items=[item] if item else [],
                    rarity=rarity,
                    private=True,
                ),
            )
            placements.append(placement)
            self.world_state.record_memory_event(
                placement.summary,
                kind="dungeon_reward_placement",
                visibility="private",
                entities=[dungeon_state.name, area.name, item],
                tags=["dungeon", "reward", rarity],
            )
        return placements

    def reward_budget(self, party_level: int, pc_count: int) -> RewardBudget:
        tier = max(threshold for threshold in self.REWARD_BUDGET_TABLE if party_level >= threshold)
        row = self.REWARD_BUDGET_TABLE[tier]
        pc_bucket = 2 if pc_count <= 2 else 3 if pc_count == 3 else 4
        max_item = row["max_item"]
        average = row[pc_bucket]
        max_text = "不限" if max_item is None else f"{max_item}Z"
        return RewardBudget(
            party_level=party_level,
            pc_count=pc_count,
            max_item_value=max_item,
            average_value=average,
            tier=tier,
            summary=f"{pc_count} 名角色，队伍等级 {party_level}：平均奖励 {average}Z，单件物品参考上限 {max_text}。",
        )

    def _reward_llm_prompt(
        self,
        *,
        reward_kind: str,
        source: str,
        recipients: list[str],
        zenit: int,
        items: list[str],
        rarity: str,
        private: bool = False,
    ) -> str:
        visibility = "这是 GM 私密奖励配置，只能用于未来铺垫，不要直接暴露给玩家。" if private else ""
        item_text = "、".join(item for item in items if item) or "无"
        return (
            f"请 GM LLM 为【{reward_kind}】创作奖励叙事。来源：{source}；"
            f"接受者：{'、'.join(recipients) if recipients else '小队'}；硬结算金币：{zenit}Z；"
            f"硬结算物品：{item_text}；稀有度/难度：{rarity}。"
            "可以自由描写宝箱外观、奖励来历、当地传说、授奖场面、物品和角色主题的呼应，"
            "也可以埋下非数值线索；但不得改变金币数、物品名称、装备归属、物品效果、经验或其他资源。"
            f"{visibility}"
        )

    def rare_quality(self, quality_name: str) -> RareItemQuality:
        if quality_name not in self.RARE_QUALITIES:
            raise ValueError(f"未知稀有物品特效：{quality_name}")
        return self.RARE_QUALITIES[quality_name]

    def design_rare_weapon(
        self,
        name: str,
        base_weapon: str,
        *,
        damage_type: str = "physical",
        accuracy_bonus: int = 0,
        extra_damage_bonus: int = 0,
        hands: int | None = None,
        accuracy_attributes: list[str] | None = None,
        quality_names: list[str] | None = None,
        description: str = "",
    ) -> RareItemDesign:
        clean_base = self.clean_item_name(base_weapon)
        if clean_base not in WEAPON_TABLE or clean_base == "徒手攻击":
            raise ValueError(f"无法以【{base_weapon}】作为稀有武器基础。")
        base = WEAPON_TABLE[clean_base]
        price = base.price
        final_damage = base.damage_bonus
        final_hands = base.hands
        notes: list[str] = []

        if damage_type != "physical":
            price += 100
            damage_label = DAMAGE_TYPE_LABELS.get(damage_type, damage_type)
            notes.append(f"伤害类型由物理改为 {damage_label}，价格 +100Z。")

        if accuracy_bonus:
            if base.accuracy_modifier > 0:
                raise ValueError("已有命中修正的基础武器不能再次获得命中 +1。")
            if accuracy_bonus != 1:
                raise ValueError("当前稀有武器设计器只支持命中 +1。")
            price += 100

        if extra_damage_bonus:
            if extra_damage_bonus % 4 != 0:
                raise ValueError("伤害强化应按 +4 为单位。")
            price += (extra_damage_bonus // 4) * 200
            final_damage += extra_damage_bonus

        if hands is not None and hands != base.hands:
            if base.hands == 2 and hands == 1:
                final_hands = 1
                final_damage = max(0, final_damage - 4)
                notes.append("双手武器改为单手，伤害修正 -4。")
            elif base.hands == 1 and hands == 2 and base.category not in {"格斗", "匕首", "投掷"}:
                final_hands = 2
                final_damage += 4
                notes.append("单手武器改为双手，伤害修正 +4。")
            else:
                raise ValueError("该武器类别不能进行指定的单双手转换。")

        final_accuracy_attributes = list(accuracy_attributes or base.accuracy_attributes)
        if accuracy_attributes is not None:
            if len(final_accuracy_attributes) != 2:
                raise ValueError("命中检定必须由两个属性组成。")
            if final_accuracy_attributes[0] == final_accuracy_attributes[1]:
                price += 50
            notes.append("命中属性组合已更改，GM 需要确认是否符合武器类别体验。")

        qualities = [self.rare_quality(quality_name) for quality_name in (quality_names or [])]
        for quality in qualities:
            if quality.item_type not in {"any", EquipmentItemType.WEAPON}:
                raise ValueError(f"特效【{quality.name}】不能用于武器。")
            price += quality.price_modifier

        required_ability = base.required_ability
        if final_damage >= 10 and not required_ability:
            required_ability = "可装备职业远程武器" if base.range_type == "ranged" else "可装备职业近战武器"
            notes.append("伤害修正达到 +10 或更高，自动视为职业限定武器。")

        if self._has_too_many_numeric_boosts(qualities):
            notes.append("这件物品包含强力数值修正；建议避免让同一角色同时装备超过两件类似物品。")

        return RareItemDesign(
            name=name,
            item_type=EquipmentItemType.WEAPON,
            base_item=clean_base,
            price=price,
            description=description or f"以【{clean_base}】为基础打造的稀有武器。",
            damage_type=damage_type,
            accuracy_attributes=final_accuracy_attributes,
            accuracy_modifier=base.accuracy_modifier + accuracy_bonus,
            damage_bonus=final_damage,
            hands=final_hands,
            range_type=base.range_type,
            required_ability=required_ability,
            qualities=qualities,
            notes=notes,
        )

    def design_rare_protective_item(
        self,
        name: str,
        base_item: str,
        *,
        item_type: EquipmentItemType | str,
        quality_names: list[str] | None = None,
        description: str = "",
    ) -> RareItemDesign:
        item_type = EquipmentItemType(item_type)
        clean_base = self.clean_item_name(base_item)
        if item_type == EquipmentItemType.ARMOR:
            if clean_base not in ARMOR_TABLE:
                raise ValueError(f"未知防具：{base_item}")
            price = ARMOR_TABLE[clean_base].price
            required = ARMOR_TABLE[clean_base].required_ability
        elif item_type == EquipmentItemType.SHIELD:
            if clean_base not in SHIELD_TABLE:
                raise ValueError(f"未知盾牌：{base_item}")
            price = SHIELD_TABLE[clean_base].price
            required = SHIELD_TABLE[clean_base].required_ability
        elif item_type == EquipmentItemType.ACCESSORY:
            price = 0
            required = ""
        else:
            raise ValueError("保护类稀有物品设计器只支持防具、盾牌或饰品。")

        qualities = [self.rare_quality(quality_name) for quality_name in (quality_names or [])]
        for quality in qualities:
            if quality.item_type not in {"any", "protective", EquipmentItemType.ACCESSORY, item_type}:
                raise ValueError(f"特效【{quality.name}】不能用于该物品类型。")
            price += quality.price_modifier

        notes = []
        if self._has_too_many_numeric_boosts(qualities):
            notes.append("这件物品包含强力数值修正；建议避免让同一角色同时装备超过两件类似物品。")
        return RareItemDesign(
            name=name,
            item_type=item_type,
            base_item=clean_base,
            price=price,
            description=description or f"以【{clean_base}】为基础打造的稀有{item_type.value}。",
            required_ability=required,
            qualities=qualities,
            notes=notes,
        )

    def item_price(self, item_name: str) -> int:
        clean_name = self.clean_item_name(item_name)
        if clean_name in WEAPON_TABLE:
            return WEAPON_TABLE[clean_name].price
        if clean_name in ARMOR_TABLE:
            return ARMOR_TABLE[clean_name].price
        if clean_name in SHIELD_TABLE:
            return SHIELD_TABLE[clean_name].price
        if clean_name in self.RARE_ITEMS:
            return 1000
        equipment_example = self.equipment_reference(clean_name)
        if equipment_example is not None and equipment_example.price is not None:
            return equipment_example.price
        prices = {
            "治疗剂": 30,
            "药剂": 30,
            "圣灵水": 30,
            "万能药": 20,
            "元素裂片": 20,
            "大补药": 30,
            "万灵药": 30,
            "滋补药": 20,
            "元素水晶": 20,
            "魔法帐篷": 40,
        }
        if clean_name in prices:
            return prices[clean_name]
        raise ValueError(f"商店暂未登记物品：{item_name}")

    def equipment_reference(self, item_name: str) -> EquipmentExample | None:
        return get_equipment_example(self.clean_item_name(item_name))

    def is_registered_reward_item(self, item_name: str) -> bool:
        """Return whether a name can safely become a concrete inventory item."""

        clean_name = self.clean_item_name(item_name)
        if not clean_name:
            return False
        try:
            self.item_price(clean_name)
        except ValueError:
            return False
        return True

    def search_equipment_references(
        self,
        *,
        item_type: EquipmentItemType | str | None = None,
        category: str = "",
        max_price: int | None = None,
        damage_type: str = "",
        text: str = "",
        include_artifacts: bool = False,
        limit: int = 20,
    ) -> list[EquipmentExample]:
        return search_equipment_examples(
            item_type=item_type,
            category=category,
            max_price=max_price,
            damage_type=damage_type,
            text=text,
            include_artifacts=include_artifacts,
            limit=limit,
        )

    def clean_item_name(self, item_name: str) -> str:
        return item_name.replace("(+)", "").replace("（+）", "").strip()

    def _spend_zenit(self, actor_name: str, amount: int) -> None:
        actor = self.character_manager.get(actor_name)
        if amount < 0:
            raise ValueError("消费金额不能为负数。")
        if actor.zenit < amount:
            raise ValueError(f"{actor_name} 的泽尼特不足：需要 {amount}Z，当前 {actor.zenit}Z。")
        actor.zenit -= amount

    def _equip_if_possible(self, actor_name: str, item_name: str) -> None:
        actor = self.character_manager.get(actor_name)
        self._ensure_equipment_accessible(actor_name, item_name)
        self._ensure_equipment_permission(actor_name, item_name)
        template_name = self._template_item_name(actor, item_name)
        equipment_example = self.equipment_reference(template_name)
        if template_name in WEAPON_TABLE:
            weapon = WEAPON_TABLE[template_name]
            actor.equipped_main_hand = item_name
            if weapon.hands == 2:
                actor.equipped_off_hand = ""
            actor.weapon_accuracy_attributes = list(weapon.accuracy_attributes)
            actor.weapon_accuracy_modifier = weapon.accuracy_modifier
            actor.weapon_damage = weapon.damage_bonus
            actor.weapon_type = "physical"
            actor.weapon_range = weapon.range_type
            self.refresh_equipment_effects(actor_name)
        elif template_name in ARMOR_TABLE:
            actor.equipped_armor = item_name
            self.refresh_equipment_effects(actor_name)
        elif template_name in SHIELD_TABLE:
            self._equip_shield(actor, item_name)
            self.refresh_equipment_effects(actor_name)
        elif equipment_example is not None:
            if equipment_example.item_type == EquipmentItemType.WEAPON:
                actor.equipped_main_hand = item_name
                if equipment_example.hands == 2:
                    actor.equipped_off_hand = ""
            elif equipment_example.item_type == EquipmentItemType.ARMOR:
                actor.equipped_armor = item_name
            elif equipment_example.item_type == EquipmentItemType.SHIELD:
                self._equip_shield(actor, item_name)
            elif equipment_example.item_type == EquipmentItemType.ACCESSORY:
                actor.equipped_accessory = item_name
            else:
                raise ValueError(f"【{item_name}】不是可装备物品。")
            self.refresh_equipment_effects(actor_name)
        else:
            raise ValueError(f"【{item_name}】暂未登记为可装备物品。")

    def _equipment_kind(self, actor, item_name: str) -> str:
        template_name = self._template_item_name(actor, item_name)
        if template_name in WEAPON_TABLE:
            return "weapon"
        if template_name in ARMOR_TABLE:
            return "armor"
        if template_name in SHIELD_TABLE:
            return "shield"
        equipment_example = self.equipment_reference(template_name)
        if equipment_example is None:
            return ""
        return str(equipment_example.item_type.value)

    def _weapon_hands(self, actor, item_name: str) -> int:
        if not item_name:
            return 0
        template_name = self._template_item_name(actor, item_name)
        if template_name in WEAPON_TABLE:
            return int(WEAPON_TABLE[template_name].hands)
        if template_name in SHIELD_TABLE:
            return 1
        equipment_example = self.equipment_reference(template_name)
        if equipment_example is None or equipment_example.item_type != EquipmentItemType.WEAPON:
            return 0
        return int(equipment_example.hands or 1)

    def _apply_main_weapon_profile(self, actor) -> None:
        item_name = actor.equipped_main_hand or "徒手攻击"
        template_name = self._template_item_name(actor, item_name)
        weapon = WEAPON_TABLE.get(template_name)
        if weapon is None:
            return
        actor.weapon_accuracy_attributes = list(weapon.accuracy_attributes)
        actor.weapon_accuracy_modifier = weapon.accuracy_modifier
        actor.weapon_damage = weapon.damage_bonus
        actor.weapon_type = "physical"
        actor.weapon_range = weapon.range_type

    def _is_catalog_type(self, item_name: str, item_type: EquipmentItemType) -> bool:
        example = self.equipment_reference(item_name)
        return bool(example is not None and example.item_type == item_type)

    def refresh_equipment_effects(self, actor_name: str) -> list[str]:
        actor = self.character_manager.get(actor_name)
        self.equipment_effects.refresh_character(actor)
        self._apply_dual_shield_profile(actor)
        self._recalculate_defenses(actor_name)
        return actor.equipment_notes

    def _equip_shield(self, actor, item_name: str) -> None:
        if not has_skill_name(actor.skills, "双盾战士"):
            actor.equipped_shield = item_name
            return
        if not actor.equipped_shield:
            actor.equipped_shield = item_name
            return
        actor.equipped_main_hand = item_name
        actor.equipped_off_hand = ""

    def _apply_dual_shield_profile(self, actor) -> None:
        if not has_skill_name(actor.skills, "双盾战士"):
            return
        if not self._is_shield_name(actor, actor.equipped_main_hand) or not self._is_shield_name(
            actor, actor.equipped_shield
        ):
            return
        actor.weapon_accuracy_attributes = ["MIG", "MIG"]
        actor.weapon_accuracy_modifier = 0
        actor.weapon_damage = 5 + skill_rank(actor.skills, "防御精通")
        actor.weapon_type = "physical"
        actor.weapon_range = "melee"
        actor.equipment_notes.append("双盾按双手格斗武器结算。")

    def _is_shield_name(self, actor, item_name: str) -> bool:
        if not item_name:
            return False
        template_name = self._template_item_name(actor, item_name)
        if template_name in SHIELD_TABLE:
            return True
        example = self.equipment_reference(template_name)
        return bool(example is not None and example.item_type == EquipmentItemType.SHIELD)

    def _ensure_equipment_permission(self, actor_name: str, item_name: str) -> None:
        actor = self.character_manager.get(actor_name)
        template_name = self._template_item_name(actor, item_name)
        required = ""
        if template_name in WEAPON_TABLE:
            required = WEAPON_TABLE[template_name].required_ability
        elif template_name in ARMOR_TABLE:
            required = ARMOR_TABLE[template_name].required_ability
        elif template_name in SHIELD_TABLE:
            required = SHIELD_TABLE[template_name].required_ability
        else:
            equipment_example = self.equipment_reference(template_name)
            if equipment_example is not None:
                required = equipment_example.required_ability
        if not required or required in actor.abilities:
            return
        hints = {
            "可装备职业近战武器": "这件武器需要职业近战武器训练；可以换基础武器，或选择暗刃骑士、怒焰斗士、武器大师。",
            "可装备职业远程武器": "这件武器需要职业远程武器训练；可以换弓/投掷武器，或选择神射手。",
            "可装备职业盔甲": "这件护甲需要职业盔甲权限；可以选择守护者、暗刃骑士或怒焰斗士。",
            "可装备职业盾牌": "这面盾需要职业盾牌权限；可以选择守护者、神射手或武器大师。",
        }
        raise ValueError(f"【{item_name}】暂时无法装备。{hints.get(required, f'缺少权限：{required}')}")

    def _ensure_equipment_accessible(self, actor_name: str, item_name: str) -> None:
        actor = self.character_manager.get(actor_name)
        requested = self.clean_item_name(item_name)
        owned_name = self._resolve_owned_item_name(actor, requested)
        restriction = actor.unavailable_equipment.get(owned_name)
        if restriction is None:
            return
        reason = str(restriction.get("reason") or "").strip()
        location = str(restriction.get("location") or "").strip()
        detail = "；".join(
            part
            for part in (
                f"原因：{reason}" if reason else "",
                f"位置：{location}" if location else "",
            )
            if part
        )
        raise ValueError(
            f"【{owned_name}】仍属于{actor_name}，但当前无法取用"
            + (f"（{detail}）" if detail else "")
            + "。"
        )

    def _resolve_owned_item_name(self, actor, item_name: str) -> str:
        requested = self.clean_item_name(str(item_name or ""))
        if requested in actor.equipment:
            return requested
        matches = [
            owned
            for owned in actor.equipment
            if self._template_item_name(actor, owned) == requested
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(
                f"{actor.name} 有多件采用【{requested}】模板的装备；请使用角色卡上的具体名称。"
            )
        raise ValueError(f"{actor.name} 并不拥有【{requested or '未指定装备'}】。")

    def _template_item_name(self, actor, item_name: str) -> str:
        return actor.equipment_templates.get(item_name, self.clean_item_name(item_name))

    def _recalculate_defenses(self, actor_name: str) -> None:
        actor = self.character_manager.get(actor_name)
        physical, magic, initiative = self._armor_defenses(actor)
        shield_physical, shield_magic = self._shield_defenses(actor)
        physical += shield_physical + actor.equipment_defense_bonuses.get("physical", 0)
        magic += shield_magic + actor.equipment_defense_bonuses.get("magic", 0)
        actor.defenses["physical"] = physical
        actor.defenses["magic"] = magic
        actor.initiative = initiative + actor.equipment_initiative_bonus

    def _resolve_defense_value(self, base: str | int, attributes: dict[str, int]) -> int:
        if isinstance(base, int):
            return base
        text = str(base).strip()
        if not text:
            return 0
        if text.startswith("+") or text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            return int(text)
        match = re.fullmatch(r"(DEX|INS|MIG|WLP)([+-]\d+)?", text)
        if match:
            attribute, modifier = match.groups()
            return attributes[attribute] + (int(modifier) if modifier else 0)
        return attributes[text]

    def _armor_defenses(self, actor) -> tuple[int, int, int]:
        armor_name = self._template_item_name(actor, actor.equipped_armor)
        armor = ARMOR_TABLE.get(armor_name)
        if armor is not None:
            physical = self._resolve_defense_value(armor.physical_base, actor.attributes) + armor.physical_bonus
            magic = self._resolve_defense_value(armor.magic_base, actor.attributes) + armor.magic_bonus
            return physical, magic, armor.initiative_modifier
        equipment_example = self.equipment_reference(armor_name)
        if equipment_example is not None and equipment_example.item_type == EquipmentItemType.ARMOR:
            physical = self._resolve_defense_value(equipment_example.physical_defense, actor.attributes)
            magic = self._resolve_defense_value(equipment_example.magic_defense, actor.attributes)
            return physical, magic, equipment_example.initiative_modifier
        armor = ARMOR_TABLE["无防具"]
        return (
            self._resolve_defense_value(armor.physical_base, actor.attributes) + armor.physical_bonus,
            self._resolve_defense_value(armor.magic_base, actor.attributes) + armor.magic_bonus,
            armor.initiative_modifier,
        )

    def _shield_defenses(self, actor) -> tuple[int, int]:
        names = [actor.equipped_shield]
        if has_skill_name(actor.skills, "双盾战士") and self._is_shield_name(actor, actor.equipped_main_hand):
            names.append(actor.equipped_main_hand)
        physical = 0
        magic = 0
        for item_name in names:
            if not item_name:
                continue
            shield_name = self._template_item_name(actor, item_name)
            shield = SHIELD_TABLE.get(shield_name)
            if shield is not None:
                physical += shield.physical_bonus
                magic += shield.magic_bonus
                continue
            equipment_example = self.equipment_reference(shield_name)
            if equipment_example is not None and equipment_example.item_type == EquipmentItemType.SHIELD:
                physical += self._resolve_defense_value(equipment_example.physical_defense, actor.attributes)
                magic += self._resolve_defense_value(equipment_example.magic_defense, actor.attributes)
        return physical, magic

    def _random_rare_item(self) -> str:
        names = list(self.RARE_ITEMS)
        index = self.rules_engine.roll_die(len(names)) - 1
        return names[index]

    def _pick_catalog_reward_item(self, *, max_price: int | None = None) -> str:
        candidates = self.search_equipment_references(
            max_price=max_price,
            include_artifacts=False,
            limit=80,
        )
        if not candidates:
            return self._random_rare_item()
        index = self.rules_engine.roll_die(len(candidates)) - 1
        return candidates[index].name

    def _travel_option(self, transport: str):
        from fu_gm.components.travel_manager import TravelManager

        if transport not in TravelManager.TRANSPORT_OPTIONS:
            raise ValueError(f"未知交通方式：{transport}")
        return TravelManager.TRANSPORT_OPTIONS[transport]

    def _lodging_unit_cost(self, settlement_size: str) -> int:
        key = (settlement_size or "town").strip()
        if key not in self.LODGING_COSTS:
            raise ValueError(f"未知旅馆服务规模：{settlement_size}")
        return self.LODGING_COSTS[key]

    def _settlement_label(self, settlement_size: str) -> str:
        labels = {
            "village": "村庄",
            "村庄": "村庄",
            "settlement": "小镇",
            "town": "小镇",
            "小镇": "小镇",
            "城镇": "小镇",
            "city": "城市",
            "城市": "城市",
        }
        return labels.get(settlement_size, settlement_size)

    def _has_too_many_numeric_boosts(self, qualities: list[RareItemQuality]) -> bool:
        strong_names = {"护身符", "坚守", "全能护盾", "命中强化", "魔力强化"}
        return any(quality.name in strong_names for quality in qualities)

