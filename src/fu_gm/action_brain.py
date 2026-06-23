from __future__ import annotations

import json
import re
from typing import Protocol

from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.llm_utils import extract_json_object
from fu_gm.equipment_catalog import EQUIPMENT_EXAMPLES
from fu_gm.models import Action, ActionType, GamePanel
from fu_gm.prompt_cache import build_cache_friendly_messages
from fu_gm.prompts import ACTION_BRAIN_SYSTEM_PROMPT
from fu_gm.skill_library import SKILL_ALIASES, normalize_skill_reference_name


def _looks_like_clock_objective(chat: str) -> bool:
    """识别玩家已经明确要对命刻采取行动，而不是单纯讨论命刻。"""
    if not any(token in chat for token in ["命刻", "时钟", "clock", "Clock", "推进目标", "目标行动"]):
        return False

    direct_markers = [
        "推进命刻",
        "推进目标",
        "目标行动",
        "填充命刻",
        "擦除命刻",
        "倒转命刻",
        "倒退命刻",
        "清空命刻",
        "削减命刻",
        "压制命刻",
    ]
    if any(marker in chat for marker in direct_markers):
        return True

    intent_markers = ["我要", "我想", "我现在", "我决定", "尝试", "试着", "开始", "打算"]
    action_markers = ["推进", "填充", "擦除", "倒转", "倒退", "清空", "压制", "削减", "稳定", "开启", "关闭", "破解", "拆除", "解除"]
    return any(marker in chat for marker in intent_markers) and any(marker in chat for marker in action_markers)


def _active_clock_names(panel: GamePanel) -> list[str]:
    names: list[str] = []
    for entry in panel.active_clocks:
        bracket = re.search(r"\[([^\]]+)\]\s*\d+\s*/\s*\d+", entry)
        if bracket:
            names.append(bracket.group(1).strip())
            continue
        chinese_bracket = re.search(r"【([^】]+)】", entry)
        if chinese_bracket:
            names.append(chinese_bracket.group(1).strip())
    return [name for name in names if name]


def _infer_clock_name(panel: GamePanel) -> str:
    chat = panel.recent_chat
    active_names = _active_clock_names(panel)
    for name in active_names:
        if name and name in chat:
            return name

    for pattern in [r"【([^】]+)】", r"\[([^\]]+)\]"]:
        match = re.search(pattern, chat)
        if match:
            candidate = match.group(1).strip()
            if candidate and "+" not in candidate and len(candidate) <= 30:
                return candidate

    if len(active_names) == 1:
        return active_names[0]
    return active_names[0] if active_names else "场景目标"


def _infer_clock_actor(panel: GamePanel) -> str:
    if panel.current_actor:
        return panel.current_actor
    for line in reversed([line.strip() for line in panel.recent_chat.splitlines() if line.strip()]):
        if "：" in line:
            return line.split("：", 1)[0].strip()
        if ":" in line:
            return line.split(":", 1)[0].strip()
    if panel.pc_status:
        return panel.pc_status[0].split(":", 1)[0].strip()
    return "玩家角色"


def _infer_clock_attributes(chat: str) -> list[str]:
    if any(token in chat for token in ["洞察", "意志", "灵魂", "共鸣", "旋律", "符文", "法术", "魔法", "祈祷"]):
        return ["INS", "WLP"]
    if any(token in chat for token in ["蛮力", "力量", "推开", "砸", "举起", "撬开"]):
        return ["MIG", "MIG"]
    if any(token in chat for token in ["敏捷", "跳", "闪", "潜入", "拆线", "机关"]):
        return ["DEX", "INS"]
    return ["INS", "WLP"]


def _infer_clock_direction(chat: str) -> int:
    if any(token in chat for token in ["擦除", "倒转", "倒退", "清空", "削减", "压制", "降低"]):
        return -1
    return 1


def _strip_speaker_prefix(text: str) -> str:
    for separator in ("：", ":"):
        if separator in text:
            return text.split(separator, 1)[1].strip()
    return text.strip()


class HeuristicActionBrain:
    """
    面向未来 LLM 版本的占位决策器。

    它先用确定性的规则返回与 LLM 相同结构的动作数据，
    这样后续替换模型接入层时，其余模块无需改写。
    """

    def decide(self, panel: GamePanel) -> Action:
        chat = panel.recent_chat
        lowered = chat.lower()
        actor = self._default_actor(panel)

        if self._looks_like_start_conflict(chat):
            return Action(
                action_type=ActionType.START_CONFLICT,
                parameters={
                    "scene_name": self._infer_conflict_scene_name(chat),
                    "pcs": self._infer_recipients(panel),
                    "enemies": self._infer_enemy_targets(chat),
                    "leader": actor,
                    "supporters": [name for name in self._infer_recipients(panel) if name != actor],
                    "reasoning": "玩家明确要求进入冲突场景，规则层应先进行先攻团队检定并建立交替回合。",
                    "in_mind_reply": "镜头骤然拉近，冲突正式开始，每个人的行动都会被命运逐帧放大。",
                },
            )

        if self._looks_like_invoke_trait(chat):
            return Action(
                action_type=ActionType.INVOKE_TRAIT,
                parameters={
                    "actor": actor,
                    "trait_name": self._infer_trait_name(chat),
                    "reroll_indices": self._infer_reroll_indices(chat),
                    "reasoning": "玩家在检定后消耗物语点援用特质重掷。",
                    "in_mind_reply": "英雄把自己的身份、故乡或主题压进这一刻，骰子重新滚动。",
                },
            )

        if self._looks_like_invoke_bond(chat):
            return Action(
                action_type=ActionType.INVOKE_BOND,
                parameters={
                    "actor": actor,
                    "bond_target": self._infer_bond_target(chat),
                    "reasoning": "玩家在检定后消耗物语点援用羁绊加值。",
                    "in_mind_reply": "羁绊像一只手从背后托住英雄，让结果向希望偏移。",
                },
            )

        if self._looks_like_opportunity_choice(chat):
            return Action(
                action_type=ActionType.TRIGGER_OPPORTUNITY,
                parameters={
                    "actor": actor,
                    "effect": self._infer_opportunity_effect(chat),
                    "target": self._infer_target(chat, actor),
                    "clock_name": _infer_clock_name(panel),
                    "status_effect": self._infer_status(chat) or "shaken",
                    "emotions": self._infer_bond_emotions(chat),
                    "fact": _strip_speaker_prefix(chat),
                    "reasoning": "玩家或 GM 正在选择大成功/大失败产生的机会效果。",
                    "in_mind_reply": "命运给了一个额外的转角，桌上所有人都看见故事轻轻偏航。",
                },
            )

        if self._looks_like_manage_bond(chat):
            return Action(
                action_type=ActionType.MANAGE_BOND,
                parameters={
                    "actor": actor,
                    "target": self._infer_bond_target(chat),
                    "emotions": self._infer_bond_emotions(chat),
                    "mode": "erase" if any(token in chat for token in ["抹除羁绊", "删除羁绊", "移除羁绊"]) else "upsert",
                    "reasoning": "玩家在休息或剧情中管理羁绊。",
                    "in_mind_reply": "英雄心中的关系被重新命名，那也是力量的一部分。",
                },
            )

        if self._looks_like_sell_item(chat):
            return Action(
                action_type=ActionType.SELL_ITEM,
                parameters={
                    "actor": actor,
                    "item_name": self._infer_item_name(chat),
                    "quantity": self._first_number(chat) or 1,
                    "reasoning": "玩家出售物品，按规则获得半价金币。",
                    "in_mind_reply": "柜台边金币轻响，一件旧装备换成了继续旅行的余裕。",
                },
            )

        if self._looks_like_equip(chat):
            return Action(
                action_type=ActionType.EQUIP,
                parameters={
                    "actor": actor,
                    "items": self._infer_equipment_items(chat),
                    "allow_armor": not any(token in panel.game_phase for token in ["冲突", "战斗"]),
                    "reasoning": "玩家更换已持有装备。",
                    "in_mind_reply": "英雄重新调整武器与护具，下一次镜头切来时，姿态已经不同。",
                },
            )

        if self._looks_like_pvp(chat):
            return Action(
                action_type=ActionType.PLAYER_VS_PLAYER,
                parameters={
                    "actor": actor,
                    "target": self._infer_bond_target(chat),
                    "consent_confirmed": any(token in chat for token in ["双方同意", "都同意", "确认同意"]),
                    "reasoning": "玩家对玩家冲突需要先确认同意与边界。",
                    "in_mind_reply": "这不是普通检定，桌面先把边界放在最亮的地方。",
                },
            )

        if self._looks_like_absent_player(chat):
            return Action(
                action_type=ActionType.ABSENT_PLAYER,
                parameters={
                    "actor": self._infer_bond_target(chat) or actor,
                    "mode": "fade_out",
                    "note": _strip_speaker_prefix(chat),
                    "reasoning": "玩家缺席，需要让角色淡出或作为背景协助。",
                    "in_mind_reply": "镜头给缺席的英雄留了一盏灯，但本场焦点交给在座的人。",
                },
            )

        if self._looks_like_player_story_change(chat):
            return Action(
                action_type=ActionType.ACCEPT_STORY_CHANGE,
                parameters={
                    "target": actor,
                    "fabula_cost": 1,
                    "fact": self._extract_story_change_fact(chat),
                    "in_mind_reply": "命运为英雄让开了一道裂缝，故事接受了这份改写。",
                },
            )

        if any(token in chat for token in ["下一回合", "结束回合", "推进回合", "下一个行动者", "next turn"]):
            return Action(
                action_type=ActionType.NEXT_TURN,
                parameters={
                    "actor": actor,
                    "reasoning": "玩家或系统请求推进冲突轮转。",
                    "in_mind_reply": "镜头切换，战场的聚光灯落到下一位行动者身上。",
                },
            )

        if self._looks_like_soft_scene_attention(chat):
            return self._soft_narrate(
                "玩家正在观察、讨论或补充场景信息；不触发硬规则结算，等待更明确的行动声明。",
                "镜头贴近那些细节：灰尘、光影、沉默的物件，以及英雄们正在形成的判断。",
            )

        if self._looks_like_dungeon_exploration(chat):
            mode = self._infer_dungeon_action(chat)
            return Action(
                action_type=ActionType.EXPLORE_DUNGEON,
                parameters={
                    "actor": actor,
                    "area_name": self._infer_dungeon_area(chat),
                    "mode": mode,
                    "collect_treasure": mode == "open_treasure",
                    "trigger_trap": any(token in chat for token in ["强行", "硬闯", "不管陷阱", "冲进去"]),
                    "reasoning": "玩家正在探索地下城区域，交由地下城事件系统处理房间、陷阱、宝箱、危险命刻或 Boss 房。",
                    "in_mind_reply": "石壁后的回声轻轻一跳，像是地图上的小旗子被插到了新格子。",
                },
            )

        if self._looks_like_open_chest(chat):
            return Action(
                action_type=ActionType.OPEN_CHEST,
                parameters={
                    "actor": actor,
                    "chest_name": self._infer_chest_name(chat),
                    "rarity": "rare" if any(token in chat for token in ["稀有", "boss", "Boss", "首领"]) else "standard",
                    "reasoning": "玩家打开宝箱，交由经济规则结算奖励。",
                    "in_mind_reply": "锁扣轻响，宝箱里像是藏着一小块命运。",
                },
            )

        if any(token in chat for token in ["阶段奖励", "战后奖励", "发奖励", "结算奖励", "宝藏奖励"]):
            return Action(
                action_type=ActionType.AWARD_REWARD,
                parameters={
                    "recipients": self._infer_recipients(panel),
                    "party_level": 5,
                    "difficulty": "boss" if any(token in chat for token in ["boss", "Boss", "首领"]) else "normal",
                    "reasoning": "阶段或战后宝藏奖励需要统一发放。",
                    "in_mind_reply": "胜利后的宝藏清点，是冒险者最快乐也最吵闹的环节。",
                },
            )

        if any(token in chat for token in ["旅馆", "住宿", "住店", "投宿"]):
            return Action(
                action_type=ActionType.SHOP,
                parameters={
                    "actor": actor,
                    "mode": "lodging",
                    "settlement_size": "city" if "城市" in chat else "village" if "村庄" in chat else "town",
                    "party_size": self._first_number(chat) or max(1, len(self._infer_recipients(panel))),
                    "reasoning": "玩家购买旅馆休息服务，金币由规则层扣除。",
                    "in_mind_reply": "旅馆门口的灯摇摇晃晃，像是在给疲惫的冒险者打招呼。",
                },
            )

        if any(token in chat for token in ["购买交通", "购买载具", "购买坐骑", "买载具", "买坐骑", "买飞空艇"]):
            return Action(
                action_type=ActionType.SHOP,
                parameters={
                    "actor": actor,
                    "mode": "buy_transport",
                    "transport": self._infer_transport_name(chat),
                    "owner": "小队",
                    "reasoning": "玩家购买长期交通工具，金币和世界资产由规则层处理。",
                    "in_mind_reply": "终于到了冒险者最容易露出坏笑的购物环节：买交通工具！",
                },
            )

        if any(token in chat for token in ["雇佣交通", "租交通", "旅行服务", "搭乘", "包船", "包飞艇"]):
            return Action(
                action_type=ActionType.SHOP,
                parameters={
                    "actor": actor,
                    "mode": "travel_service",
                    "transport": self._infer_travel_service_name(chat),
                    "days": self._first_number(chat) or 1,
                    "party_size": max(1, len(self._infer_recipients(panel))),
                    "reasoning": "玩家雇佣按日旅行服务，费用由规则层处理。",
                    "in_mind_reply": "车夫、船长或飞艇技师开始报价，空气里飘着一点点被宰的预感。",
                },
            )

        if self._looks_like_shop_transaction(chat):
            restock = any(token in chat for token in ["补充库存", "补充IP", "补充 ip", "库存点"])
            return Action(
                action_type=ActionType.SHOP,
                parameters={
                    "actor": actor,
                    "mode": "restock" if restock else "buy",
                    "item_name": "库存点" if restock else self._infer_item_name(chat),
                    "quantity": self._first_number(chat) or (1 if not restock else None),
                    "equip": any(token in chat for token in ["装备", "穿上", "拿上"]),
                    "reasoning": "玩家与商店交互，购买物品或补充库存点。",
                    "in_mind_reply": "柜台后的铃铛叮当作响，冒险的补给清单摊开在木桌上。",
                },
            )

        if any(token in chat for token in ["炼金", "调合", "混合药剂"]):
            return Action(
                action_type=ActionType.TINKERER_GADGET,
                parameters={
                    "actor": actor,
                    "gadget_type": "alchemy",
                    "tier": "supreme" if "最高" in chat else "advanced" if "高级" in chat else "basic",
                    "targets": self._infer_targets(chat),
                    "reasoning": "造物使使用炼金术，需由规则层掷 d20 并结算目标与效果。",
                    "in_mind_reply": "药瓶中的颜色开始乱跳，像一段过度兴奋的片头曲。",
                },
            )

        if any(token in chat for token in ["魔法加农炮", "魔加农", "魔科技篡夺", "魔科天球", "天球"]):
            mode = "魔科技篡夺" if "篡夺" in chat else "魔科天球" if "天球" in chat else "魔法加农炮"
            return Action(
                action_type=ActionType.TINKERER_GADGET,
                parameters={
                    "actor": actor,
                    "gadget_type": "magitech",
                    "mode": mode,
                    "target": "帝国机甲" if "机甲" in chat else "",
                    "spell_name": "落雷" if "雷" in chat else "火焰弹" if "火" in chat else "落雷",
                    "damage_type": "lightning" if "雷" in chat or "电" in chat else "physical",
                    "reasoning": "造物使使用魔导装置。",
                    "in_mind_reply": "齿轮咬合，魔导回路亮起一串很不妙但很帅的光。",
                },
            )

        if any(
            token in chat
            for token in [
                "治疗剂",
                "药剂",
                "圣灵水",
                "万能药",
                "元素裂片",
                "大补药",
                "万灵药",
                "滋补药",
                "元素水晶",
                "使用库存",
                "用药水",
            ]
        ):
            return Action(
                action_type=ActionType.USE_INVENTORY,
                parameters={
                    "actor": actor,
                    "item_name": self._infer_item_name(chat),
                    "target": self._infer_target(chat, actor),
                    "damage_type": "lightning" if "雷" in chat or "电" in chat else "fire" if "火" in chat else "ice" if "冰" in chat else "fire",
                    "status_effect": self._infer_status(chat),
                    "reasoning": "玩家使用库存道具，交由库存点规则结算。",
                    "in_mind_reply": "背包里总有那么一件东西，能在关键时刻发出救世主般的玻璃瓶声。",
                },
            )

        if any(token in chat for token in ["推进仪式", "仪式命刻", "为仪式供能", "维持仪式"]):
            ritual_name = self._ritual_name_from_chat(chat) or "未命名仪式"
            return Action(
                action_type=ActionType.CONTRIBUTE_RITUAL,
                parameters={
                    "actor": actor,
                    "clock_name": f"仪式：{ritual_name}" if not ritual_name.startswith("仪式：") else ritual_name,
                    "attributes": ["INS", "WLP"],
                    "reasoning": "玩家试图在冲突中推进仪式命刻。",
                    "in_mind_reply": "灵魂之流被牵引成细线，仪式圆阵一点点亮起。",
                },
            )

        if any(token in chat for token in ["完成仪式", "释放仪式", "结算仪式", "执行仪式"]):
            ritual_name = self._ritual_name_from_chat(chat) or "未命名仪式"
            return Action(
                action_type=ActionType.CAST_RITUAL,
                parameters={
                    "actor": actor,
                    "name": ritual_name,
                    "require_completed_clock": "命刻" in chat or "冲突" in panel.game_phase,
                    "reasoning": "玩家尝试完成已准备的仪式。",
                    "in_mind_reply": "空气像钟面一样绷紧，所有咒文在最后一个音节上合拢。",
                },
            )

        if any(token in chat for token in ["仪式", "ritual"]) and not any(token in chat for token in ["法术", "咒语"]):
            return Action(
                action_type=ActionType.PLAN_RITUAL,
                parameters={
                    "caster": actor,
                    "name": self._infer_ritual_name(chat),
                    "discipline": self._infer_ritual_discipline(chat),
                    "potency": self._infer_potency(chat),
                    "scope": self._infer_scope(chat),
                    "effect": self._infer_effect(chat),
                    "rare_material": self._infer_rare_material(chat),
                    "forbidden_tags": self._infer_forbidden_ritual_tags(chat),
                    "start_conflict_clock": "冲突" in panel.game_phase or "战斗" in panel.game_phase or "命刻" in chat,
                    "reasoning": "玩家提出自由形式仪式，需由规则层计算效力、范围、MP 与 DL。",
                    "in_mind_reply": "这不是一段固定咒文，而是英雄把愿望压进灵魂之流的尝试。",
                },
            )

        if any(token in chat for token in ["雇佣帮手", "请帮手", "雇帮手"]):
            return Action(
                action_type=ActionType.HIRE_PROJECT_HELPERS,
                parameters={
                    "actor": actor,
                    "project_name": self._infer_project_name(chat),
                    "count": self._first_number(chat) or 1,
                    "reasoning": "玩家为项目雇佣帮手。",
                    "in_mind_reply": "新的工匠加入桌边，图纸上多了几道稳健的笔迹。",
                },
            )

        if any(token in chat for token in ["推进项目", "继续项目", "继续制作", "工作一天", "花一天做"]):
            return Action(
                action_type=ActionType.WORK_PROJECT,
                parameters={
                    "actor": actor,
                    "project_name": self._infer_project_name(chat),
                    "workers": [actor],
                    "days": self._first_number(chat) or 1,
                    "reasoning": "玩家花费时间推进项目进度。",
                    "in_mind_reply": "齿轮、符文与汗水在一天里慢慢咬合成形。",
                },
            )

        if any(token in chat for token in ["发明", "制造", "项目", "工程", "修复", "建造", "改造", "造一", "做一", "魔科技", "造物使"]):
            return Action(
                action_type=ActionType.START_PROJECT,
                parameters={
                    "inventor": actor,
                    "name": self._infer_project_name(chat),
                    "potency": self._infer_potency(chat),
                    "scope": self._infer_scope(chat),
                    "use": "permanent" if any(token in chat for token in ["永久", "长期", "反复", "装备", "载具"]) else "consumable",
                    "output_type": self._infer_project_output_type(chat),
                    "owner": actor,
                    "location": self._infer_location(chat),
                    "effect": self._infer_effect(chat),
                    "flaw": self._infer_flaw(chat),
                    "material_credit": 0,
                    "reasoning": "玩家提出造物使项目或自定义发明，需由规则层计算成本与进度。",
                    "in_mind_reply": "一张不安分的蓝图被摊开，世界规则开始被螺丝刀轻轻撬动。",
                },
            )

        if any(token in chat for token in ["阿卡纳", "奥灵", "奥秘召唤", "奥秘解除", "召唤奥秘", "解除奥秘", "召唤奥灵", "遣散奥灵"]):
            mode = "dismiss" if any(token in chat for token in ["解除", "遣散", "释放", "解放"]) else "summon"
            return Action(
                action_type=ActionType.SKILL,
                parameters={
                    "actor": actor,
                    "skill_name": "契约与召唤",
                    "mode": mode,
                    "arcanum": self._infer_arcanum_name(chat),
                    "targets": self._infer_enemy_targets(chat),
                    "damage_type": "fire" if "火" in chat else "ice" if "冰" in chat else "lightning" if "雷" in chat or "电" in chat else None,
                    "reasoning": "玩家声明召唤或遣散奥灵，交由奥灵使规则结算。",
                    "in_mind_reply": "古老神秘的投影贴近灵魂边缘，像一张牌在命运手中翻面。",
                },
            )

        known_skills = [
            "契约与召唤",
            "暗影击",
            "挑衅",
            "谴责",
            "鼓舞",
            "窃取时间",
            "窃取灵魂",
            "回见了您呐",
            "碎骨",
            "威慑射击",
            "破防打击",
            "挺身守护",
            "快速评估",
            "意外盟友",
            "卸甲真言",
            "我算到了",
            "消失",
            "薄情者",
            "希望",
            "火山",
            "彗星",
        ]
        known_skill_triggers = set(known_skills)
        known_skill_triggers.update(alias for alias, canonical in SKILL_ALIASES.items() if canonical in known_skills)
        for skill_trigger in sorted(known_skill_triggers, key=len, reverse=True):
            if skill_trigger in chat:
                skill_name = normalize_skill_reference_name(skill_trigger)
                return Action(
                    action_type=ActionType.SKILL,
                    parameters={
                        "actor": actor,
                        "target": "帝国机甲" if "机甲" in chat else "帝国暗骑士",
                        "skill_name": skill_name,
                        "reasoning": f"玩家声明使用职业/英雄技能【{skill_name}】，交由规则层结算。",
                        "in_mind_reply": "英雄把训练、羁绊与命运压进这一瞬间，战场节奏随之改变。",
                    },
                )

        if any(token in chat for token in ["防御", "守住", "掩护", "guard"]):
            return Action(
                action_type=ActionType.GUARD,
                parameters={
                    "actor": actor,
                    "guarded_target": "同伴" if "掩护" in chat else None,
                    "reasoning": "玩家选择防御，本轮对伤害获得抵抗。",
                    "in_mind_reply": "瓦莉亚横起剑锋，雷光在她身前织出一道防壁。",
                },
            )

        if any(token in chat for token in ["妨碍", "干扰", "虚弱", "迟缓", "动摇", "中毒"]):
            status = "weakened"
            if "迟缓" in chat:
                status = "slow"
            elif "动摇" in chat:
                status = "shaken"
            elif "中毒" in chat:
                status = "poisoned"
            return Action(
                action_type=ActionType.HINDER,
                parameters={
                    "actor": actor,
                    "target": "帝国机甲" if "机甲" in chat else "帝国暗骑士",
                    "attributes": ["INS", "WLP"],
                    "target_number": 10,
                    "status_effect": status,
                    "reasoning": "玩家试图施加异常状态。",
                    "in_mind_reply": "她的咏唱像钉子一样刺进敌人的动作节奏。",
                },
            )

        if self._looks_like_enemy_investigation(chat):
            return Action(
                action_type=ActionType.INVESTIGATE,
                parameters={
                    "actor": actor,
                    "target": "帝国机甲" if "机甲" in chat else "帝国暗骑士",
                    "attributes": ["INS", "INS"],
                    "reasoning": "玩家尝试调查敌人的数据与弱点。",
                    "in_mind_reply": "瓦莉亚的目光掠过机体接缝与魔导回路，寻找致命破绽。",
                },
            )

        if _looks_like_clock_objective(chat):
            clock_name = _infer_clock_name(panel)
            return Action(
                action_type=ActionType.OBJECTIVE,
                parameters={
                    "actor": actor,
                    "target": clock_name,
                    "attributes": _infer_clock_attributes(chat),
                    "target_number": 10,
                    "clock_name": clock_name,
                    "clock_direction": _infer_clock_direction(chat),
                    "reasoning": "玩家明确声明要推进或擦除场景目标命刻，交由规则层进行检定与命刻结算。",
                    "in_mind_reply": "镜头压近目标命刻：这一刻，英雄的行动会让进度真正改变。",
                },
            )

        if any(token in chat for token in ["护体", "护盾", "屏障", "结界"]):
            spell_name = "元素护体" if "雷" in chat or "电" in chat else "守护咏唱"
            return Action(
                action_type=ActionType.SPELL,
                parameters={
                    "actor": actor,
                    "target": actor,
                    "spell_name": spell_name,
                    "reasoning": "玩家尝试施放持续型增益法术。",
                    "in_mind_reply": "咒文化作护环与光纹，缠绕在目标周身不散。",
                },
            )

        if any(token in chat for token in ["施法", "魔法", "咒文", "法术", "spell"]):
            spell_name = "落雷" if "雷" in chat else "火焰弹" if "火" in chat else "落雷"
            return Action(
                action_type=ActionType.SPELL,
                parameters={
                    "actor": actor,
                    "target": "帝国机甲" if "机甲" in chat else "帝国暗骑士",
                    "spell_name": spell_name,
                    "reasoning": "玩家施放攻击法术，需要进行施法检定。",
                    "in_mind_reply": "咒文在空气里炸开，雷与火被她拽成一束直线。",
                },
            )

        if any(token in chat for token in ["灌注", "低温", "焦火", "电压", "毒液"]) and any(token in chat for token in ["攻击", "射击", "劈砍", "打"]):
            return Action(
                action_type=ActionType.ATTACK,
                parameters={
                    "actor": actor,
                    "attributes": ["DEX", "MIG"],
                    "target": self._infer_target(chat, "帝国机甲" if "机甲" in chat else "帝国暗骑士"),
                    "infusion_name": self._infer_infusion_name(chat),
                    "reasoning": "玩家使用造物使灌注并发动攻击，灌注由规则层扣除 IP 并改写本次攻击伤害类型。",
                    "in_mind_reply": "造物使的符文涂层在武器边缘亮起，下一击带着危险的元素嗡鸣。",
                },
            )

        if any(token in chat for token in ["攻击", "劈砍", "魔法剑"]) or "attack" in lowered:
            damage_type = "lightning" if "雷" in chat else "physical"
            return Action(
                action_type=ActionType.ATTACK,
                parameters={
                    "actor": actor,
                    "attributes": ["DEX", "MIG"],
                    "target": "帝国机甲" if "机甲" in chat else "帝国暗骑士",
                    "damage_type": damage_type,
                    "reasoning": "玩家发起攻击，需要进行命中检定。",
                    "in_mind_reply": "敌人摆出防御姿态，但英雄的攻势像闪电一样撕开僵局。",
                },
            )

        return Action(
            action_type=ActionType.NARRATE,
            parameters={
                "summary": "场景继续推进，等待玩家声明更明确的行动。",
                "in_mind_reply": "风穿过断桥与钢铁残骸，战场在短暂屏息。",
            },
        )

    def _soft_narrate(self, summary: str, in_mind_reply: str) -> Action:
        return Action(
            action_type=ActionType.NARRATE,
            parameters={
                "summary": summary,
                "in_mind_reply": in_mind_reply,
            },
        )

    def _default_actor(self, panel: GamePanel) -> str:
        if panel.current_actor:
            return panel.current_actor
        for line in reversed([line.strip() for line in panel.recent_chat.splitlines() if line.strip()]):
            if "：" in line:
                return line.split("：", 1)[0].strip()
            if ":" in line:
                return line.split(":", 1)[0].strip()
        if panel.pc_status:
            return panel.pc_status[0].split(":", 1)[0].strip()
        return "玩家角色"

    def _looks_like_start_conflict(self, chat: str) -> bool:
        return any(token in chat for token in ["进入冲突", "开始冲突", "冲突场景", "开始战斗", "开战", "追逐开始", "开始追逐"])

    def _infer_conflict_scene_name(self, chat: str) -> str:
        explicit = self._named_after(chat, ["冲突场景：", "冲突场景:", "场景：", "场景:"])
        if explicit:
            return explicit
        if "追逐" in chat:
            return "追逐冲突"
        if "谈判" in chat or "审判" in chat:
            return "社交冲突"
        return "冲突场景"

    def _looks_like_invoke_trait(self, chat: str) -> bool:
        return "援用" in chat and any(token in chat for token in ["特质", "身份", "主题", "故乡"])

    def _looks_like_invoke_bond(self, chat: str) -> bool:
        return "援用" in chat and "羁绊" in chat

    def _infer_trait_name(self, chat: str) -> str:
        for marker in ["特质", "身份", "主题", "故乡"]:
            if marker in chat:
                explicit = self._named_after(chat, [f"{marker}【", f"{marker}：", f"{marker}:"])
                if explicit:
                    return explicit
                return marker
        return "特质"

    def _infer_reroll_indices(self, chat: str) -> list[int]:
        if any(token in chat for token in ["两颗", "两枚", "两个", "全部", "双骰"]):
            return [0, 1]
        if any(token in chat for token in ["第二颗", "第二枚", "第二个", "第2"]):
            return [1]
        return [0]

    def _looks_like_opportunity_choice(self, chat: str) -> bool:
        if "机会" not in chat and not any(token in chat for token in ["大成功", "大失败"]):
            return False
        effects = ["揭示", "进展", "纽带", "情报", "青睐", "审视", "失态", "失物", "受苦", "优势", "转折"]
        return any(effect in chat for effect in effects)

    def _infer_opportunity_effect(self, chat: str) -> str:
        for effect in ["揭示", "进展", "纽带", "情报", "青睐", "审视", "失态", "失物", "受苦", "优势", "转折"]:
            if effect in chat:
                return effect
        return "情报"

    def _looks_like_manage_bond(self, chat: str) -> bool:
        return "羁绊" in chat and any(token in chat for token in ["建立", "强化", "添加", "改变", "更改", "抹除", "删除", "移除"])

    def _infer_bond_target(self, chat: str) -> str:
        for pattern in [r"对[【\[]([^】\]]+)[】\]]", r"与[【\[]([^】\]]+)[】\]]", r"目标[：:]\s*([^，,。；;\s]+)"]:
            match = re.search(pattern, chat)
            if match:
                return self._trim_phrase(match.group(1))
        explicit = self._named_after(chat, ["对", "与", "目标：", "目标:"])
        return explicit

    def _infer_bond_emotions(self, chat: str) -> list[str]:
        emotions = []
        for emotion in ["钦佩", "自卑", "信赖", "猜忌", "喜爱", "憎恨"]:
            if emotion in chat:
                emotions.append(emotion)
        return emotions or ["信赖"]

    def _looks_like_sell_item(self, chat: str) -> bool:
        return any(token in chat for token in ["出售", "卖掉", "卖出", "卖给商人"])

    def _looks_like_equip(self, chat: str) -> bool:
        if any(token in chat for token in ["购买", "买下", "出售", "卖掉"]):
            return False
        return any(token in chat for token in ["装备", "换上", "拿出", "拔出", "换武器", "换盾", "佩戴"])

    def _infer_equipment_items(self, chat: str) -> list[str]:
        matches = []
        for example in sorted(EQUIPMENT_EXAMPLES, key=lambda item: len(item.name), reverse=True):
            if example.name in chat and example.name not in matches:
                matches.append(example.name)
            for alias in example.aliases:
                if alias in chat and example.name not in matches:
                    matches.append(example.name)
        explicit = self._named_after(chat, ["装备", "换上", "拿出", "佩戴"])
        if explicit and explicit not in matches:
            matches.append(explicit)
        return matches or [self._infer_item_name(chat)]

    def _looks_like_pvp(self, chat: str) -> bool:
        return any(token in chat for token in ["玩家对玩家", "PVP", "pvp", "我要攻击队友", "攻击同伴", "和队友对抗"])

    def _looks_like_absent_player(self, chat: str) -> bool:
        return any(token in chat for token in ["缺席", "请假", "今天不在", "玩家不在", "角色淡出"])

    def _looks_like_player_story_change(self, chat: str) -> bool:
        text = str(chat or "")
        if "物语点" in text and any(token in text for token in ("密道", "设定", "改写", "补充")):
            return True
        markers = (
            "我补充一个世界细节",
            "我补充一个地点细节",
            "我补充一个设定",
            "我设定",
            "这里其实",
            "这里有一条",
            "这里有个",
            "这个地方由",
            "这个地点由",
        )
        if not any(marker in text for marker in markers):
            return False
        world_fact_tokens = (
            "管理",
            "旧路",
            "密道",
            "据点",
            "驿站",
            "组织",
            "守望会",
            "关卡",
            "传闻",
            "遗迹",
            "神殿",
            "村庄",
            "城市",
        )
        return any(token in text for token in world_fact_tokens)

    def _extract_story_change_fact(self, chat: str) -> str:
        fact = _strip_speaker_prefix(str(chat or ""))
        fact = re.sub(r"^(?:我补充一个世界细节|我补充一个地点细节|我补充一个设定|我设定)[：:，,]?\s*", "", fact)
        return fact.strip(" 。！？；;") or "玩家消耗物语点，为当前场景加入了一个有利的新故事元素。"

    def _ritual_name_from_chat(self, chat: str) -> str:
        bracket = re.search(r"仪式\s*(?:名为|叫做|叫|[:：])?\s*[【\[](?P<name>[^】\]]+)[】\]]", chat)
        if bracket:
            return self._trim_phrase(bracket.group("name"))
        explicit = self._named_after(chat, ["名为", "叫做", "叫", "仪式：", "仪式:"])
        if explicit:
            return explicit
        return ""

    def _infer_ritual_name(self, chat: str) -> str:
        explicit = self._ritual_name_from_chat(chat)
        if explicit:
            return explicit
        if "安抚" in chat:
            return "安抚灵魂"
        if "封" in chat and "裂隙" in chat:
            return "封住裂隙"
        if "水晶" in chat:
            return "唤醒水晶"
        return "未命名仪式"

    def _infer_project_name(self, chat: str) -> str:
        explicit = self._named_after(chat, ["名为", "叫做", "叫", "项目：", "项目:", "工程：", "工程:"])
        if explicit:
            return explicit
        for marker in ["发明", "制造", "修复", "建造", "改造", "工程", "造一台", "造一个", "做一台", "做一个"]:
            if marker in chat:
                return self._trim_phrase(chat.split(marker, 1)[1])
        return "未命名项目"

    def _infer_item_name(self, chat: str) -> str:
        for example in sorted(EQUIPMENT_EXAMPLES, key=lambda item: len(item.name), reverse=True):
            if example.name in chat:
                return example.name
            for alias in example.aliases:
                if alias in chat:
                    return example.name
        known_items = [
            "治疗剂",
            "药剂",
            "圣灵水",
            "万能药",
            "元素裂片",
            "大补药",
            "万灵药",
            "滋补药",
            "元素水晶",
            "魔法帐篷",
            "钢匕首",
            "青铜剑",
            "细剑",
            "手枪",
            "短弓",
            "十字弩",
            "青铜盾",
            "符文盾",
            "旅行装束",
            "丝质衬衫",
            "武道服",
            "贤者之袍",
        ]
        for item_name in known_items:
            if item_name in chat:
                return item_name
        return "治疗剂"

    def _infer_transport_name(self, chat: str) -> str:
        known_transports = [
            "飞行载具",
            "水下载具",
            "水面载具",
            "地面载具",
            "飞行坐骑",
            "水下坐骑",
            "水面坐骑",
            "地面坐骑",
        ]
        for transport_name in known_transports:
            if transport_name in chat:
                return transport_name
        if "飞艇" in chat or "飞空艇" in chat:
            return "飞行载具"
        if "船" in chat:
            return "水面载具"
        if "坐骑" in chat:
            return "地面坐骑"
        return "地面载具"

    def _infer_travel_service_name(self, chat: str) -> str:
        if "空" in chat or "飞艇" in chat or "飞空艇" in chat:
            return "空中旅行服务"
        if "水" in chat or "船" in chat:
            return "水面旅行服务"
        return "陆地旅行服务"

    def _infer_chest_name(self, chat: str) -> str:
        explicit = self._named_after(chat, ["宝箱：", "宝箱:", "打开"])
        return explicit or "地下城宝箱"

    def _looks_like_open_chest(self, chat: str) -> bool:
        if not any(token in chat for token in ["宝箱", "箱子", "宝藏箱"]):
            return False
        hard_open_tokens = [
            "打开",
            "开宝箱",
            "开启",
            "撬开",
            "砸开",
            "解锁",
            "拿走",
            "取得",
            "收下",
            "搜刮",
            "领取",
        ]
        return any(token in chat for token in hard_open_tokens)

    def _looks_like_shop_transaction(self, chat: str) -> bool:
        transaction_tokens = [
            "补充库存",
            "补充IP",
            "补充 ip",
            "购买",
            "买下",
            "买一个",
            "买一件",
            "买",
            "付款",
            "结账",
            "补货",
            "补给",
        ]
        return any(token in chat for token in transaction_tokens)

    def _looks_like_soft_scene_attention(self, chat: str) -> bool:
        hard_tokens = [
            "攻击",
            "施法",
            "法术",
            "咒语",
            "防御",
            "妨碍",
            "推进目标",
            "命刻",
            "打开",
            "撬开",
            "取得",
            "购买",
            "补充库存",
            "进入",
            "前往",
            "搜索",
            "解除陷阱",
            "拆除陷阱",
            "面对Boss",
            "面对boss",
            "面对首领",
            "强行",
            "硬闯",
        ]
        if any(token in chat for token in hard_tokens):
            return False
        soft_tokens = [
            "看看",
            "看一眼",
            "观察",
            "打量",
            "研究一下",
            "讨论",
            "商量",
            "回忆",
            "询问",
            "问问",
            "听听",
            "闻闻",
            "检查一下",
            "调查",
            "描述",
            "是什么样",
            "有没有线索",
        ]
        scene_targets = [
            "宝箱",
            "箱子",
            "墙画",
            "壁画",
            "门",
            "走廊",
            "房间",
            "遗迹",
            "雕像",
            "机关",
            "地图",
            "酒馆",
            "商店",
            "村庄",
            "线索",
        ]
        return any(token in chat for token in soft_tokens) and any(target in chat for target in scene_targets)

    def _looks_like_dungeon_exploration(self, chat: str) -> bool:
        area_tokens = ["入口", "前厅", "侧室", "走廊", "门厅", "Boss房", "boss房", "首领房", "房间", "区域", "地下城"]
        action_tokens = ["进入", "前往", "去", "探索", "搜索", "调查", "解除陷阱", "拆除陷阱", "清理", "面对Boss", "面对首领"]
        if not any(token in chat for token in action_tokens):
            return False
        return any(token in chat for token in area_tokens)

    def _infer_dungeon_action(self, chat: str) -> str:
        if any(token in chat for token in ["解除陷阱", "拆除陷阱", "排除陷阱"]):
            return "disarm_trap"
        if any(token in chat for token in ["开宝箱", "打开宝箱", "取得宝藏", "拿宝藏"]):
            return "open_treasure"
        if any(token in chat for token in ["搜索", "调查", "探索"]):
            return "search"
        if any(token in chat for token in ["面对Boss", "面对boss", "面对首领", "Boss房", "boss房", "首领房"]):
            return "confront_boss"
        if any(token in chat for token in ["清理", "解决"]):
            return "clear"
        return "enter"

    def _infer_dungeon_area(self, chat: str) -> str:
        known_areas = [
            "Boss房",
            "boss房",
            "首领房",
            "核心门厅",
            "短暂避风处",
            "危险走廊",
            "宝箱侧室",
            "前厅",
            "入口",
        ]
        for area in known_areas:
            if area in chat:
                if area == "boss房":
                    return "Boss房"
                if area == "首领房":
                    return "Boss房"
                return area
        explicit = self._named_after(chat, ["区域：", "区域:", "房间：", "房间:"])
        return explicit

    def _looks_like_enemy_investigation(self, chat: str) -> bool:
        if not any(token in chat for token in ["调查", "洞察", "看穿", "分析", "investigate"]):
            return False
        enemy_tokens = ["敌人", "怪物", "魔物", "机甲", "暗骑士", "Boss", "boss", "首领", "反派", "目标"]
        return any(token in chat for token in enemy_tokens)

    def _infer_target(self, chat: str, fallback: str) -> str:
        if "机甲" in chat:
            return "帝国机甲"
        if "暗骑士" in chat:
            return "帝国暗骑士"
        if "自己" in chat or "我" in chat:
            return fallback
        return fallback

    def _infer_targets(self, chat: str) -> list[str]:
        targets = []
        if "机甲" in chat:
            targets.append("帝国机甲")
        if "暗骑士" in chat:
            targets.append("帝国暗骑士")
        return targets

    def _infer_infusion_name(self, chat: str) -> str:
        mapping = {
            "低温": "低温",
            "冰": "低温",
            "焦火": "焦火",
            "火": "焦火",
            "电压": "电压",
            "雷": "电压",
            "电": "电压",
            "毒液": "毒液",
            "毒": "毒液",
            "疾风": "疾风",
            "风": "疾风",
            "暗影": "暗影",
            "地震": "地震",
            "驱邪": "驱邪",
            "光": "驱邪",
        }
        for token, infusion_name in mapping.items():
            if token in chat:
                return infusion_name
        return "焦火"

    def _infer_status(self, chat: str) -> str | None:
        if "迟缓" in chat:
            return "slow"
        if "眩晕" in chat:
            return "dazed"
        if "虚弱" in chat:
            return "weakened"
        if "动摇" in chat:
            return "shaken"
        if "激怒" in chat:
            return "enraged"
        if "中毒" in chat:
            return "poisoned"
        return None

    def _infer_recipients(self, panel: GamePanel) -> list[str]:
        names = []
        for status in panel.pc_status:
            if ":" in status:
                names.append(status.split(":", 1)[0].strip())
        return names

    def _named_after(self, chat: str, markers: list[str]) -> str:
        for marker in markers:
            if marker in chat:
                return self._trim_phrase(chat.split(marker, 1)[1])
        return ""

    def _trim_phrase(self, value: str) -> str:
        for separator in ["，", ",", "。", "！", "？", "；", ";", "\n"]:
            if separator in value:
                value = value.split(separator, 1)[0]
        return value.strip(" ：:「」『』【】[]")

    def _infer_ritual_discipline(self, chat: str) -> str:
        if any(token in chat for token in ["奥术", "阿卡纳", "奥灵"]):
            return "arcanism"
        if any(token in chat for token in ["嵌合", "野兽", "怪物"]):
            return "chimerism"
        if any(token in chat for token in ["熵", "时间", "空间", "传送", "衰变"]):
            return "entropism"
        if any(token in chat for token in ["御魂", "灵魂", "精神", "情绪", "安抚", "鼓舞"]):
            return "spiritism"
        if any(token in chat for token in ["元素", "火", "冰", "雷", "风", "土", "水", "天气", "暴雨"]):
            return "elementalism"
        return "ritualism"

    def _infer_potency(self, chat: str) -> str:
        if any(token in chat for token in ["极强", "灾难", "城市", "世界", "神"]):
            return "extreme"
        if any(token in chat for token in ["强大", "大型", "长期", "整片", "城堡", "飞艇"]):
            return "major"
        if any(token in chat for token in ["中等", "小队", "房间", "短期"]):
            return "moderate"
        return "minor"

    def _infer_scope(self, chat: str) -> str:
        if any(token in chat for token in ["城市", "村庄", "堡垒", "湖泊", "山顶", "巨大", "巨大范围"]):
            return "huge"
        if any(token in chat for token in ["森林", "区域", "大厅", "飞艇", "一群", "大型", "大范围"]):
            return "large"
        if any(token in chat for token in ["房间", "小型", "小范围", "几个人", "小队"]):
            return "small"
        return "individual"

    def _infer_effect(self, chat: str) -> str:
        return chat.strip()

    def _infer_rare_material(self, chat: str) -> str:
        for token in ["稀有材料", "材料", "媒介"]:
            if token in chat:
                return self._trim_phrase(chat.split(token, 1)[1])
        return ""

    def _infer_flaw(self, chat: str) -> str:
        for token in ["缺陷", "代价", "副作用"]:
            if token in chat:
                return self._trim_phrase(chat.split(token, 1)[1])
        return ""

    def _infer_project_output_type(self, chat: str) -> str:
        if any(token in chat for token in ["设施", "工坊", "基地", "炮台", "装置安装", "放在", "建在", "信号塔", "灯塔"]):
            return "facility"
        if any(token in chat for token in ["装备", "武器", "护甲", "盾牌", "饰品", "配件"]):
            return "equipment"
        if any(token in chat for token in ["一次性", "消耗", "炸弹", "药剂", "药水"]):
            return "consumable"
        return "consumable" if any(token in chat for token in ["临时", "用完"]) else "world_fact"

    def _infer_arcanum_name(self, chat: str) -> str:
        mapping = {
            "锻造": "锻造",
            "熔炉": "锻造",
            "霜": "霜",
            "寒霜": "霜",
            "冰霜": "霜",
            "门": "门",
            "门径": "门",
            "传送": "门",
            "魔典": "魔典",
            "书": "魔典",
            "橡树": "橡树",
            "树": "橡树",
            "天空": "天空",
            "风暴": "天空",
            "剑": "剑",
            "塔": "塔",
            "轮": "轮",
            "时间": "轮",
        }
        for token, arcanum in mapping.items():
            if token in chat:
                return arcanum
        return "锻造"

    def _infer_enemy_targets(self, chat: str) -> list[str]:
        if "机甲" in chat:
            return ["帝国机甲"]
        if "暗骑士" in chat:
            return ["帝国暗骑士"]
        return []

    def _infer_location(self, chat: str) -> str:
        for token in ["地点", "位置", "放在", "建在", "安装在"]:
            if token in chat:
                return self._trim_phrase(chat.split(token, 1)[1])
        return ""

    def _infer_forbidden_ritual_tags(self, chat: str) -> list[str]:
        tags: list[str] = []
        avoids_direct_damage = any(
            token in chat
            for token in [
                "不直接伤害",
                "不会直接伤害",
                "不造成直接伤害",
                "不伤害任何人",
                "不伤害目标",
                "不伤害敌人",
                "不造成伤害",
            ]
        )
        if not avoids_direct_damage and any(token in chat for token in ["直接造成伤害", "直接伤害", "伤害敌人", "烧伤敌人"]):
            tags.append("direct_damage")
        if any(token in chat for token in ["施加异常", "施加状态", "中毒", "迟缓", "眩晕"]):
            tags.append("apply_status")
        if any(token in chat for token in ["恢复HP", "恢复MP", "治疗"]):
            tags.append("hp_change")
        return tags

    def _first_number(self, chat: str) -> int:
        for token in chat.split():
            if token.isdigit():
                return int(token)
        return 0


class ActionBrain(Protocol):
    def decide(self, panel: GamePanel) -> Action:
        ...


class LLMActionBrain:
    """调用真实 LLM 生成结构化动作。"""

    def __init__(
        self,
        client: OpenAICompatibleClient,
        model: str,
        fallback: ActionBrain | None = None,
        allow_fallback: bool = False,
    ) -> None:
        self.client = client
        self.model = model
        self.fallback = fallback or HeuristicActionBrain()
        self.allow_fallback = allow_fallback
        self.last_raw_content = ""
        self.last_error = ""
        self.last_used_fallback = False
        self.last_recovery_attempts: list[dict[str, object]] = []
        self.recent_recoveries: list[dict[str, object]] = []

    def decide(self, panel: GamePanel) -> Action:
        try:
            self.last_used_fallback = False
            self.last_error = ""
            self.last_recovery_attempts = []
            user_content = (
                "请根据以下游戏面板，输出一个动作 JSON。\n"
                "返回格式示例："
                '{"action_type":"Attack","parameters":{"actor":"瓦莉亚","attributes":["DEX","MIG"],'
                '"target":"帝国机甲","damage_type":"lightning",'
                '"reasoning":"玩家发起攻击，需要检定。","in_mind_reply":"你的叙事内心台词"}}\n'
                f"游戏面板：\n{json.dumps(panel.__dict__, ensure_ascii=False, indent=2)}"
            )
            recovery_limit = (
                max(0, int(self.client.config.reactive_recovery_max_retries))
                if self.client.config.reactive_recovery_enabled
                else 0
            )
            parse_error: Exception | None = None
            for attempt in range(recovery_limit + 1):
                reminders = []
                if parse_error is not None:
                    reminders.append(
                        (
                            "结构化输出错误恢复",
                            "上一次响应无法解析为合法动作。请重新判断同一个面板，只输出符合 schema 的 JSON；"
                            f"错误摘要：{str(parse_error)[:300]}",
                        )
                    )
                content = self.client.create_chat_completion(
                    model=self.model,
                    messages=build_cache_friendly_messages(
                        static_system_prompt=ACTION_BRAIN_SYSTEM_PROMPT,
                        reminders=reminders,
                        user_content=user_content,
                    ),
                    temperature=0.1,
                    response_format={"type": "json_object"},
                )
                self.last_raw_content = content
                try:
                    data = extract_json_object(content)
                    action = Action(
                        action_type=ActionType(data["action_type"]),
                        parameters=data["parameters"],
                    )
                    action = self._postprocess_action(panel, action)
                    if self.last_recovery_attempts:
                        self.last_recovery_attempts[-1]["recovered"] = True
                    self.last_error = ""
                    return action
                except Exception as exc:
                    parse_error = exc
                    self.last_error = str(exc)
                    recovery = {
                        "attempt": attempt + 1,
                        "kind": "structured_output",
                        "error": str(exc)[:300],
                        "recovered": False,
                    }
                    self.last_recovery_attempts.append(recovery)
                    self.recent_recoveries.append(recovery)
                    self.recent_recoveries = self.recent_recoveries[-20:]
                    if attempt >= recovery_limit:
                        raise
            raise RuntimeError("LLMActionBrain structured recovery exhausted.")
        except Exception as exc:
            self.last_error = str(exc)
            if self.allow_fallback:
                self.last_used_fallback = True
                return self.fallback.decide(panel)
            self.last_used_fallback = False
            raise RuntimeError("LLMActionBrain failed and heuristic fallback is disabled.") from exc

    def _postprocess_action(self, panel: GamePanel, action: Action) -> Action:
        chat = self._current_player_chat(str(panel.recent_chat or ""))
        if self._looks_like_scene_close(chat) and action.action_type != ActionType.NARRATE:
            return Action(
                action_type=ActionType.NARRATE,
                parameters={
                    "summary": chat.strip(),
                    "reasoning": "玩家正在请求收束当前冲突或转入撤离叙事，不应继续推进上一条仪式命刻。",
                    "in_mind_reply": "这一刻的重点不再是继续拉扯命刻，而是确认场景是否已经抵达收束点。",
                },
            )
        action = self._normalize_action_subjects(panel, action, chat)
        if self._looks_like_conditional_invocation_noop(chat) and any(token in chat for token in ["援用", "重掷", "物语点"]):
            return Action(
                action_type=ActionType.INVOKE_TRAIT,
                parameters={
                    "actor": self._actor_from_chat(panel, action, chat),
                    "trait_name": self._trait_name_from_chat(chat) or action.parameters.get("trait_name") or "主题",
                    "reroll_indices": action.parameters.get("reroll_indices") or [1],
                    "skip_if_pending_roll_success": True,
                    "reasoning": "玩家声明的是带前置条件的援用窗口；若上一检定已成功，本次不触发也不消耗物语点。",
                    "in_mind_reply": action.parameters.get(
                        "in_mind_reply",
                        "命运的余光被按住了：如果结果已经够好，就不必再让代价落下。",
                    ),
                },
            )
        if action.action_type == ActionType.INVOKE_TRAIT and self._looks_like_conditional_invocation_noop(chat):
            parameters = dict(action.parameters)
            parameters["skip_if_pending_roll_success"] = True
            return Action(action_type=ActionType.INVOKE_TRAIT, parameters=parameters)
        if action.action_type == ActionType.EQUIP:
            return self._normalized_equip_action(panel, action, chat)
        if action.action_type == ActionType.START_CONFLICT and self._looks_like_guard(chat):
            return Action(
                action_type=ActionType.GUARD,
                parameters={
                    "actor": self._actor_from_chat(panel, action, chat),
                    "guarded_target": self._guard_target_from_chat(chat),
                    "reasoning": "当前已经是冲突语境，玩家声明的是防御/掩护行动，不应重新开始冲突。",
                    "in_mind_reply": action.parameters.get("in_mind_reply", "英雄稳住阵线，把危险挡在同伴之前。"),
                },
            )
        if action.action_type == ActionType.START_CONFLICT and self._looks_like_equip(chat):
            return Action(
                action_type=ActionType.EQUIP,
                parameters={
                    "actor": self._actor_from_chat(panel, action, chat),
                    "items": self._equipment_items_from_chat(chat),
                    "allow_armor": False,
                    "reasoning": "当前已经是冲突语境，玩家声明的是装备行动，不应重新开始冲突。",
                    "in_mind_reply": action.parameters.get("in_mind_reply", "英雄迅速调整手中装备，准备处理下一次压力。"),
                },
            )
        if self._looks_like_project_start(chat):
            planned = HeuristicActionBrain().decide(
                GamePanel(
                    game_phase=panel.game_phase,
                    active_clocks=panel.active_clocks,
                    pc_status=panel.pc_status,
                    enemy_status=panel.enemy_status,
                    recent_chat=chat,
                    current_actor=panel.current_actor,
                    table_status=panel.table_status,
                    safety_guidance=panel.safety_guidance,
                    retrieved_public_memory=panel.retrieved_public_memory,
                    gm_private_memory=panel.gm_private_memory,
                    memory_guidance=panel.memory_guidance,
                )
            )
            if planned.action_type == ActionType.START_PROJECT:
                inventor = self._actor_from_chat(panel, action, chat)
                planned.parameters["inventor"] = inventor
                if planned.parameters.get("owner") not in self._status_names(panel):
                    planned.parameters["owner"] = inventor
                elif planned.parameters.get("owner") != inventor and "帮工" in chat:
                    planned.parameters["owner"] = inventor
                return planned
        if self._looks_like_ritual_cast(chat):
            clock_name = self._ritual_clock_name_from_action_or_chat(panel, action, chat)
            name = clock_name.removeprefix("仪式：") if clock_name else "未命名仪式"
            actor = self._actor_from_chat(panel, action, chat)
            return Action(
                action_type=ActionType.CAST_RITUAL,
                parameters={
                    "actor": actor,
                    "name": name,
                    "clock_name": clock_name or f"仪式：{name}",
                    "require_completed_clock": "命刻" in chat or "冲突" in panel.game_phase,
                    "reasoning": "玩家尝试完成已准备的仪式。",
                    "in_mind_reply": action.parameters.get(
                        "in_mind_reply",
                        "仪式的最后一个音节停在风里，等待命刻确认是否已经抵达终点。",
                    ),
                },
            )
        if self._looks_like_ritual_contribution(chat):
            clock_name = self._ritual_clock_name_from_action_or_chat(panel, action, chat)
            if not clock_name:
                return action
            attributes = self._attributes_from_chat(chat, action.parameters.get("attributes") or ["INS", "WLP"])
            actor = self._actor_from_chat(panel, action, chat)
            return Action(
                action_type=ActionType.CONTRIBUTE_RITUAL,
                parameters={
                    "actor": actor,
                    "clock_name": clock_name,
                    "attributes": attributes,
                    "reasoning": "玩家明确是在推进或协助已有仪式命刻，按仪式贡献结算。",
                    "in_mind_reply": action.parameters.get(
                        "in_mind_reply",
                        "仪式的回声被新的手势接住，命刻上的光又向前推了一格。",
                    ),
                },
            )
        if self._looks_like_ritual_plan(chat):
            planned = HeuristicActionBrain().decide(
                GamePanel(
                    game_phase=panel.game_phase,
                    active_clocks=panel.active_clocks,
                    pc_status=panel.pc_status,
                    enemy_status=panel.enemy_status,
                    recent_chat=chat,
                    current_actor=panel.current_actor,
                    table_status=panel.table_status,
                    safety_guidance=panel.safety_guidance,
                    retrieved_public_memory=panel.retrieved_public_memory,
                    gm_private_memory=panel.gm_private_memory,
                    memory_guidance=panel.memory_guidance,
                )
            )
            actor = self._actor_from_chat(panel, action, chat)
            if planned.action_type == ActionType.PLAN_RITUAL and actor:
                planned.parameters["caster"] = actor
            return planned
        if self._looks_like_objective_clock(chat):
            return self._objective_clock_action_from_chat(panel, action, chat)
        if action.action_type == ActionType.OBJECTIVE and self._objective_missing_required_fields(action):
            return self._recover_incomplete_objective(panel, action, chat)
        if not self._looks_like_ritual_contribution(chat):
            return action
        return action

    def _normalize_action_subjects(self, panel: GamePanel, action: Action, chat: str) -> Action:
        parameters = dict(action.parameters)
        actor = self._actor_from_chat(panel, action, chat)
        actor_keys_by_type = {
            ActionType.ATTACK: ("actor",),
            ActionType.SPELL: ("actor",),
            ActionType.GUARD: ("actor",),
            ActionType.EQUIP: ("actor",),
            ActionType.HINDER: ("actor",),
            ActionType.INVESTIGATE: ("actor",),
            ActionType.OBJECTIVE: ("actor",),
            ActionType.SKILL: ("actor",),
            ActionType.USE_INVENTORY: ("actor",),
            ActionType.TINKERER_GADGET: ("actor",),
            ActionType.SHOP: ("actor",),
            ActionType.OPEN_CHEST: ("actor",),
            ActionType.EXPLORE_DUNGEON: ("actor",),
            ActionType.CONTRIBUTE_RITUAL: ("actor",),
            ActionType.CAST_RITUAL: ("actor", "caster"),
            ActionType.START_PROJECT: ("inventor", "actor"),
            ActionType.HIRE_PROJECT_HELPERS: ("actor", "payer"),
            ActionType.WORK_PROJECT: ("actor",),
            ActionType.REQUEST_ROLL: ("actor",),
            ActionType.INVOKE_TRAIT: ("actor",),
            ActionType.INVOKE_BOND: ("actor",),
            ActionType.MANAGE_BOND: ("actor",),
            ActionType.SELL_ITEM: ("actor",),
            ActionType.PLAYER_VS_PLAYER: ("actor",),
        }
        status_names = set(self._status_names(panel))
        for key in actor_keys_by_type.get(action.action_type, ()):
            value = str(parameters.get(key) or "").strip()
            if not value or value not in status_names:
                parameters[key] = actor
        if action.action_type == ActionType.PLAN_RITUAL:
            caster = str(parameters.get("caster") or parameters.get("actor") or "").strip()
            if not caster or caster not in status_names:
                parameters["caster"] = actor
        return Action(action_type=action.action_type, parameters=parameters)

    def _normalized_equip_action(self, panel: GamePanel, action: Action, chat: str) -> Action:
        parameters = dict(action.parameters)
        parameters["actor"] = self._actor_from_chat(panel, action, chat)
        raw_items = parameters.get("items", parameters.get("item_names", parameters.get("item_name", [])))
        if isinstance(raw_items, dict):
            raw_items = [raw_items]
        items: list[str] = []
        if isinstance(raw_items, str):
            items.extend(piece.strip() for piece in re.split(r"[、,，/]+", raw_items) if piece.strip())
        elif isinstance(raw_items, list):
            for item in raw_items:
                if isinstance(item, dict):
                    candidate = (
                        item.get("item_name")
                        or item.get("name")
                        or item.get("item")
                        or item.get("weapon")
                        or item.get("equipment")
                    )
                    if candidate:
                        items.append(str(candidate).strip())
                    continue
                text = str(item).strip()
                if text:
                    items.append(text)
        inferred = self._equipment_items_from_chat(chat)
        if inferred:
            items = inferred
        clean_items: list[str] = []
        for item in items:
            clean = str(item).strip(" ：:「」『』【】[]")
            clean = re.sub(r"[（(].*?[）)]", "", clean).strip()
            if clean and clean not in clean_items and clean not in {"空", "空手", "副手空出来"}:
                clean_items.append(clean)
        parameters["items"] = clean_items
        parameters.pop("item_names", None)
        parameters.pop("item_name", None)
        parameters.setdefault("allow_armor", False)
        return Action(action_type=ActionType.EQUIP, parameters=parameters)

    def _objective_missing_required_fields(self, action: Action) -> bool:
        params = action.parameters
        return not params.get("actor") or not (params.get("clock_name") or params.get("target"))

    def _recover_incomplete_objective(self, panel: GamePanel, action: Action, chat: str) -> Action:
        actor = self._actor_from_chat(panel, action, chat)
        attributes = self._attributes_from_chat(chat, action.parameters.get("attributes") or ["INS", "WLP"])
        if self._looks_like_investigation_request(chat):
            return Action(
                action_type=ActionType.INVESTIGATE,
                parameters={
                    "actor": actor,
                    "target": self._scene_target_from_chat(chat),
                    "attributes": attributes,
                    "target_number": action.parameters.get("target_number", 7),
                    "reasoning": action.parameters.get("reasoning")
                    or "LLM 输出了不完整目标行动；玩家实际是在进行普通调查。",
                    "in_mind_reply": action.parameters.get(
                        "in_mind_reply",
                        "镜头贴近线索，先把可公开的痕迹一一摆到桌面上。",
                    ),
                },
            )
        if self._looks_like_explicit_check_request(chat):
            return Action(
                action_type=ActionType.REQUEST_ROLL,
                parameters={
                    "actor": actor,
                    "target": self._scene_target_from_chat(chat),
                    "attributes": attributes,
                    "target_number": action.parameters.get("target_number", 10),
                    "non_damage": True,
                    "reasoning": action.parameters.get("reasoning")
                    or "LLM 输出了不完整目标行动；改为一次普通属性检定。",
                    "in_mind_reply": action.parameters.get(
                        "in_mind_reply",
                        "这一刻先交给一次清楚的检定，而不是凭空创建命刻。",
                    ),
                },
            )
        return Action(
            action_type=ActionType.NARRATE,
            parameters={
                "summary": chat.strip(),
                "reasoning": "LLM 输出了不完整目标行动；玩家并未明确推进命刻，改为叙事承接。",
                "in_mind_reply": action.parameters.get(
                    "in_mind_reply",
                    "先让场景继续呼吸，等玩家声明明确目标后再进入硬规则。",
                ),
            },
        )

    def _current_player_chat(self, text: str) -> str:
        marker = "当前玩家输入"
        if marker not in text:
            return text
        tail = text.rsplit(marker, 1)[1]
        if "\n" in tail:
            tail = tail.split("\n", 1)[1]
        return tail.strip()

    def _actor_from_chat(self, panel: GamePanel, action: Action, chat: str) -> str:
        status_names = self._status_names(panel)
        speaker_match = re.match(r"^[^\n:：]{1,12}[:：]\s*(?P<body>.*)$", chat.strip(), flags=re.S)
        chat_body = speaker_match.group("body") if speaker_match else chat
        appearances = [(chat_body.find(name), name) for name in status_names if name and chat_body.find(name) >= 0]
        if appearances:
            appearances.sort(key=lambda item: item[0])
            return appearances[0][1]
        for key in ("actor", "caster", "target"):
            value = str(action.parameters.get(key) or "").strip()
            if value in status_names:
                return value
        if panel.current_actor:
            return panel.current_actor
        speaker_prefix = re.split(r"[：:]", chat, maxsplit=1)[0].strip()
        if speaker_prefix in status_names:
            return speaker_prefix
        return status_names[0] if status_names else "玩家角色"

    def _status_names(self, panel: GamePanel) -> list[str]:
        names: list[str] = []
        for status in [*panel.pc_status, *panel.enemy_status]:
            name = str(status).split(":", 1)[0].strip()
            if name and name not in names:
                names.append(name)
        return names

    def _looks_like_scene_close(self, chat: str) -> bool:
        return any(token in chat for token in ["结束冲突场景", "结束冲突", "撤入旧路", "撤离当前场景", "收束场景"])

    def _looks_like_objective_clock(self, chat: str) -> bool:
        if any(token in chat for token in ["推进目标命刻", "目标命刻", "推进威胁命刻", "威胁命刻"]):
            return True
        if not any(token in chat for token in ["推进", "协助", "稳定", "开启", "撑住", "拆开", "压制", "擦除", "倒转"]):
            return False
        return bool(re.search(r"[【\[]([^】\]]+)[】\]]", chat))

    def _objective_clock_action_from_chat(self, panel: GamePanel, action: Action, chat: str) -> Action:
        clock_name = (
            self._generic_clock_name_from_chat(chat)
            or self._active_clock_name_from_chat(panel, chat)
            or action.parameters.get("clock_name")
            or action.parameters.get("target")
        )
        clock_name = str(clock_name or "当前目标命刻").strip(" ：:「」『』【】[]")
        target_number = self._safe_target_number(action.parameters.get("target_number"), default=10)
        if "威胁命刻" in chat:
            return Action(
                action_type=ActionType.ADVANCE_CLOCK,
                parameters={
                    "clock_name": clock_name,
                    "delta": 1,
                    "max_segments": action.parameters.get("max_segments", 6),
                    "clock_type": "threat",
                    "reason": action.parameters.get("reasoning") or chat.strip(),
                    "in_mind_reply": action.parameters.get(
                        "in_mind_reply",
                        "威胁向前压近一格，场景里的时间也随之收紧。",
                    ),
                },
            )
        return Action(
            action_type=ActionType.OBJECTIVE,
            parameters={
                "actor": self._actor_from_chat(panel, action, chat),
                "target": clock_name,
                "clock_name": clock_name,
                "attributes": self._attributes_from_chat(chat, action.parameters.get("attributes") or ["INS", "DEX"]),
                "target_number": target_number,
                "max_segments": action.parameters.get("max_segments", 6),
                "cooperative_progress": any(token in chat for token in ["协助", "支援", "帮忙", "团队合作", "配合"]),
                "reasoning": action.parameters.get("reasoning") or "玩家推进当前目标命刻。",
                "in_mind_reply": action.parameters.get(
                    "in_mind_reply",
                    "目标命刻向前压去，胜利或撤离的窗口正在被一点点撬开。",
                ),
            },
        )

    def _generic_clock_name_from_chat(self, chat: str) -> str:
        match = re.search(r"(?:目标命刻|威胁命刻|命刻)\s*[【\[](?P<name>[^】\]]+)[】\]]", chat)
        if match:
            return match.group("name")
        bracket = re.search(r"[【\[](?P<name>[^】\]]+)[】\]]", chat)
        if bracket:
            return bracket.group("name")
        return ""

    def _safe_target_number(self, value, *, default: int = 10) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default
        if number <= 0:
            return default
        return number

    def _active_clock_name_from_chat(self, panel: GamePanel, chat: str) -> str:
        for name in _active_clock_names(panel):
            if name and name in chat:
                return name
        return ""

    def _looks_like_conditional_invocation_noop(self, chat: str) -> bool:
        text = str(chat or "")
        return "如果" in text and any(token in text for token in ["已经成功", "已成功", "成功了"]) and any(
            token in text for token in ["不触发", "当作不触发", "不消耗", "作废"]
        )

    def _trait_name_from_chat(self, chat: str) -> str:
        match = re.search(r"(?:援用(?:主题|特质)?|主题|特质)\s*[【\[](?P<name>[^】\]]+)[】\]]", chat)
        if match:
            return match.group("name").strip()
        return ""

    def _looks_like_guard(self, chat: str) -> bool:
        return any(token in chat for token in ["防御", "掩护", "保护", "守住"])

    def _guard_target_from_chat(self, chat: str) -> str:
        for pattern in (r"掩护([^，,。；;\s]+)", r"保护([^，,。；;\s]+)"):
            match = re.search(pattern, chat)
            if match:
                return match.group(1).strip("【】[]")
        return ""

    def _looks_like_equip(self, chat: str) -> bool:
        return any(token in chat for token in ["装备动作", "更换装备", "换成", "主手", "副手", "装备"])

    def _equipment_items_from_chat(self, chat: str) -> list[str]:
        items: list[str] = []
        for example in sorted(EQUIPMENT_EXAMPLES, key=lambda item: len(item.name), reverse=True):
            if example.name in chat and example.name not in items:
                items.append(example.name)
            for alias in example.aliases:
                if alias in chat and example.name not in items:
                    items.append(example.name)
        for match in re.finditer(r"换成([^，,。；;\s]+)", chat):
            candidate = match.group(1).strip("【】[]")
            if candidate and candidate not in items:
                items.append(candidate)
        return items

    def _looks_like_investigation_request(self, chat: str) -> bool:
        return any(token in chat for token in ["调查", "观察", "检查", "判断", "确认", "分析", "研究"])

    def _looks_like_explicit_check_request(self, chat: str) -> bool:
        return any(token in chat for token in ["检定", "如果需要", "若需要", "用洞察", "用意志", "用力量", "用敏捷"]) or any(
            token in chat for token in ["说服", "请求", "交涉", "劝说", "套取"]
        )

    def _scene_target_from_chat(self, chat: str) -> str:
        bracket = re.search(r"[【\[](?P<name>[^】\]]+)[】\]]", chat)
        if bracket:
            return bracket.group("name").strip()
        priority_targets = [
            "失忆旅人",
            "旅人",
            "守望会会长",
            "白花守望会",
            "风铃廊",
            "风铃",
            "旧钟",
            "财团车辙",
            "潮汐下的钟塔遗迹入口",
            "钟塔遗迹入口",
        ]
        for target in priority_targets:
            if target in chat:
                return target
        return "当前线索"

    def _looks_like_project_start(self, chat: str) -> bool:
        if not any(token in chat for token in ["项目", "工程", "发明", "制造", "建造", "修复", "改造"]):
            return False
        if "仪式" in chat and not any(token in chat for token in ["项目", "工程", "发明", "制造", "建造", "修复", "改造"]):
            return False
        return any(token in chat for token in ["启动", "开始", "发起", "修复", "建造", "制造", "做一个", "造一个"])

    def _looks_like_ritual_cast(self, chat: str) -> bool:
        return any(token in chat for token in ["完成仪式", "释放仪式", "结算仪式", "执行仪式"])

    def _looks_like_ritual_plan(self, chat: str) -> bool:
        if "仪式" not in chat and "ritual" not in chat.lower():
            return False
        if self._looks_like_ritual_contribution(chat) or self._looks_like_ritual_cast(chat):
            return False
        return any(token in chat for token in ["计划", "准备", "举行", "启动", "设计", "发起"])

    def _looks_like_ritual_contribution(self, chat: str) -> bool:
        return any(
            token in chat
            for token in [
                "推进仪式",
                "协助推进仪式",
                "仪式命刻",
                "为仪式供能",
                "给仪式供能",
                "维持仪式",
                "协助仪式",
            ]
        )

    def _ritual_clock_name_from_action_or_chat(self, panel: GamePanel, action: Action, chat: str) -> str:
        bracket = re.search(r"(?:仪式命刻|仪式)\s*[【\[](?P<name>[^】\]]+)[】\]]", chat)
        raw_name = bracket.group("name") if bracket else ""
        if not raw_name:
            raw_name = action.parameters.get("clock_name") or action.parameters.get("name") or ""
        if raw_name:
            clock_name = str(raw_name).strip(" ：:「」『』【】[]")
            if not clock_name.startswith("仪式："):
                clock_name = f"仪式：{clock_name}"
            return clock_name

        active_rituals = [str(item) for item in panel.active_clocks if "仪式：" in str(item)]
        if len(active_rituals) == 1:
            return active_rituals[0].split(" ", 1)[0].strip()
        return ""

    def _attributes_from_chat(self, chat: str, default: list[str]) -> list[str]:
        attr_names = {
            "敏捷": "DEX",
            "洞察": "INS",
            "力量": "MIG",
            "意志": "WLP",
            "DEX": "DEX",
            "INS": "INS",
            "MIG": "MIG",
            "WLP": "WLP",
        }
        positioned: list[tuple[int, str]] = []
        for label, code in attr_names.items():
            index = chat.find(label)
            if index >= 0:
                positioned.append((index, code))
        positioned.sort(key=lambda item: item[0])
        found: list[str] = []
        for _, code in positioned:
            if code not in found:
                found.append(code)
        if len(found) >= 2:
            return found[:2]
        if isinstance(default, list) and len(default) == 2:
            return default
        return ["INS", "DEX"]
