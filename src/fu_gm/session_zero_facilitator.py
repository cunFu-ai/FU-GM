from __future__ import annotations

import json
import re
from dataclasses import asdict
from copy import deepcopy
from typing import Any, Protocol

from fu_gm.components.character_creation_manager import (
    ARMOR_TABLE,
    CLASS_ALIASES,
    MARTIAL_ARMOR_CLASSES,
    MARTIAL_MELEE_CLASSES,
    MARTIAL_RANGED_CLASSES,
    MARTIAL_SHIELD_CLASSES,
    SHIELD_TABLE,
    SKILL_ALIASES,
    SKILL_CATALOG,
    STARTING_EQUIPMENT_BUDGET,
    WEAPON_TABLE,
    resolve_equipment_request_text,
)
from fu_gm.components.prologue_manager import PrologueManager
from fu_gm.gm_guidance import build_gm_guidance, question_hint_for_step, summarize_guidance_for_prompt
from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.llm_utils import extract_json_object
from fu_gm.models import FirstActCandidate, HeroDraft, SessionZeroResponse, SessionZeroStage, SessionZeroState
from fu_gm.prepared_locations import CORE_LOCATION_SEEDS
from fu_gm.prompt_cache import build_cache_friendly_messages
from fu_gm.prompts import SESSION_ZERO_CANONICAL_CLASS_LIST, SESSION_ZERO_SYSTEM_PROMPT
from fu_gm.safety_parser import extract_safety_declarations
from fu_gm.skill_library import required_spell_slots
from fu_gm.spellbook import SPELL_ALIASES, is_known_spell, normalize_spell_name, spell_names_for_school, spell_school_for


PRIVATE_VISIBLE_TOKENS = (
    "反派映照原则",
    "GM暗线",
    "GM 私密",
    "私密暗线",
    "私密笔记",
    "不要给玩家看",
    "内部决策",
)

PUBLIC_META_LEAK_TOKENS = (
    "本地法术表",
    "本地规则表",
    "不会临场",
    "不会编",
    "未接入",
)


def sanitize_public_text(text: str) -> str:
    clean = str(text or "")
    for token in PRIVATE_VISIBLE_TOKENS:
        clean = re.sub(rf"(^|[；;\n])[^；;\n]*{re.escape(token)}[^；;\n]*", r"\1", clean)
    for token in PUBLIC_META_LEAK_TOKENS:
        clean = re.sub(rf"(^|[。！？；;\n])[^。！？；;\n]*{re.escape(token)}[^。！？；;\n]*[。！？；;]?", r"\1", clean)
    clean = clean.replace("后台", "")
    clean = re.sub(r"角色草稿【[^】]+】已确认[^。！？；;\n]*[。！？；;]?", "角色草稿已确认。", clean)
    clean = re.sub(r"正式\s*PC【[^】]+】已创建[^。！？；;\n]*[。！？；;]?", "角色创建状态已更新。", clean)
    clean = re.sub(r"已记录【([^】]+)】的(装备|技能|属性|职业|法术)选择[^。！？；;\n]*[。！？；;]?", r"【\1】的\2选择已记住。", clean)
    clean = re.sub(r"已记录【([^】]+)】的(装备|技能|属性|职业|法术)[^。！？；;\n]*[。！？；;]?", r"【\1】的\2选择已记住。", clean)
    clean = re.sub(r"记录(反派种子|国家|阵营|关键地点|谜团|世界威胁|历史事件)[^。！？；;\n]*[。！？；;]?", "这项世界设定已记住。", clean)
    clean = re.sub(r"小队原型暂定为【[^】]+】[^。！？；;\n]*[。！？；;]?", "小队方向已记住。", clean)
    clean = re.sub(r"\s+的角色方向已在\s*记下", "的角色方向已记录", clean)
    clean = re.sub(r"\s+的角色方向已记录", "的角色方向已记录", clean)
    clean = re.sub(r"[；;]\s*([。\n]|$)", r"\1", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean.strip()


def is_private_visible_text(text: str) -> bool:
    return any(token in str(text or "") for token in PRIVATE_VISIBLE_TOKENS)


RECOMMENDED_CHARACTER_THEMES = {
    "慈悲": "希望帮助他人，无论他们犯下过怎样的罪孽。",
    "愤怒": "像定时炸弹一样，永远在暴怒边缘。",
    "复仇": "向某人、某物或某个体系寻求偿还。",
    "归属": "害怕孤独、被遗忘或遭到抛弃。",
    "愧疚": "希望弥补曾经造成或见证的过错。",
    "使命": "以履行诺言、命令或责任作为生存意义。",
    "希望": "为自己或他人追寻更美好的世界。",
    "野心": "全力向自己或他人证明自身价值。",
    "疑虑": "必须为某个令自己焦灼的问题找到答案。",
    "正义": "永远会为弱者和无法自保者挺身而出。",
}

THEME_ALIASES = {
    "赎罪": "愧疚",
    "救赎": "愧疚",
    "责任": "使命",
}

CLASS_LIST_QUERY_TOKENS = (
    "有什么职业",
    "有哪些职业",
    "可选职业",
    "职业可以选择",
    "职业有哪些",
    "职业列表",
    "职业可选项",
    "能选什么职业",
)

SPELL_LIST_QUERY_TOKENS = (
    "法术有哪些",
    "有哪些法术",
    "有什么法术",
    "可选法术",
    "法术选项",
    "法术列表",
    "法术表",
    "能选什么法术",
)

EQUIPMENT_ADVICE_QUERY_TOKENS = (
    "装备建议",
    "选购建议",
    "购物建议",
    "推荐装备",
    "装备怎么选",
    "装备怎么配",
    "初始装备怎么选",
    "初始装备买什么",
    "买什么装备",
    "配装建议",
    "帮我配装备",
)

EQUIPMENT_REFERENCE_QUERY_TOKENS = (
    "有什么武器",
    "有哪些武器",
    "可选武器",
    "武器列表",
    "基础武器",
    "初始武器有哪些",
    "有什么防具",
    "有哪些防具",
    "可选防具",
    "防具列表",
    "基础防具",
    "有什么盾牌",
    "有哪些盾牌",
    "可选盾牌",
    "盾牌列表",
    "基础盾牌",
    "装备列表",
    "基础装备有哪些",
    "初始装备有哪些",
)


def message_requests_class_list(message: str) -> bool:
    text = str(message or "")
    return any(token in text for token in CLASS_LIST_QUERY_TOKENS)


def requested_spell_school(message: str) -> str:
    text = str(message or "")
    compact = re.sub(r"\s+", "", text)
    generic_question = any(token in compact for token in ("有哪些", "有什么", "可选", "选项", "列表", "法术表", "能选什么"))
    asks_about_spells = any(token in compact for token in ("法术", "魔法", "咒语"))
    if not any(token in text for token in SPELL_LIST_QUERY_TOKENS) and not (generic_question and asks_about_spells):
        return ""
    if any(token in text for token in ("元素使", "元素魔法", "元素法术")):
        return "元素使法术"
    if any(token in text for token in ("熵术士", "熵系魔法", "熵系法术", "控熵")):
        return "熵术士法术"
    if any(token in text for token in ("御魂使", "灵魂魔法", "灵魂法术", "御魂系")):
        return "御魂使法术"
    return ""


def message_requests_equipment_advice(message: str) -> bool:
    text = str(message or "")
    compact = re.sub(r"\s+", "", text)
    return any(token in compact for token in EQUIPMENT_ADVICE_QUERY_TOKENS)


def requested_equipment_reference(message: str) -> str:
    text = str(message or "")
    compact = re.sub(r"\s+", "", text)
    if not any(token in compact for token in EQUIPMENT_REFERENCE_QUERY_TOKENS):
        return ""
    wants_weapon = "武器" in compact
    wants_armor = "防具" in compact or "护甲" in compact or "防护" in compact
    wants_shield = "盾" in compact
    if wants_weapon and not (wants_armor or wants_shield):
        return "weapon"
    if wants_armor and not (wants_weapon or wants_shield):
        return "armor"
    if wants_shield and not (wants_weapon or wants_armor):
        return "shield"
    return "all"


def build_class_options_response(state: SessionZeroState, speaker: str) -> SessionZeroResponse:
    return SessionZeroResponse(
        message=(
            f"《最终物语》固定可选职业有：{SESSION_ZERO_CANONICAL_CLASS_LIST}。"
            "起始角色通常为 5 级，标准规则选 2 到 3 个职业来分配这 5 级；"
            "本项目允许 GM 和桌面共识通融 4 职业特例，但总等级仍为 5。"
            "你想走近战、施法、支援，还是调查/旅行路线？"
        ),
        stage=state.stage,
        suggestions=["如果你愿意，我可以直接按职业给你配一版 5 级起手。"],
        questions=["你更偏近战、施法、支援，还是调查/旅行？"],
    )


def build_spell_options_response(state: SessionZeroState, school: str) -> SessionZeroResponse:
    names = spell_names_for_school(school)
    option_text = "、".join(names)
    return SessionZeroResponse(
        message=f"{school}可选：{option_text}。",
        stage=state.stage,
        questions=["你要为哪位角色选择其中一个法术？"],
    )


class SessionZeroFacilitator(Protocol):
    """Session 0 的主持接口：可以由启发式逻辑或真实 LLM 实现。"""

    def opening(self, state: SessionZeroState) -> SessionZeroResponse:
        ...

    def respond(self, state: SessionZeroState, speaker: str, message: str) -> SessionZeroResponse:
        ...


class HeuristicSessionZeroFacilitator:
    """离线可测的 Session 0 主持器，用于无模型时维持基本讨论节奏。"""

    def __init__(self) -> None:
        self.prologue_manager = PrologueManager()

    def opening(self, state: SessionZeroState) -> SessionZeroResponse:
        style = state.gm_style
        questions = [
            "创建世界第1步：这张 Nortantis 世界地图上，主要大陆、海岸和近海岛屿给你的第一印象是什么？",
            "如果脑海里已经有角色、国家或谜团点子，也可以先抛出来；我会记录，但主动推进时会回到最早未完成的世界创建步骤。",
        ]
        suggestions = [
            "创建世界按当前项目流程推进：地图与陆地、魔法科技、国家、历史、奥秘、威胁；地图默认由 Nortantis 生成类地球大陆式地图。",
            "每位玩家都可以在国家、历史、奥秘和威胁阶段贡献一个点子；如果有人暂时没灵感，不会硬卡住流程。",
        ]
        current_participant = self._participant_for_stage(state, state.stage)
        if current_participant:
            questions = [
                f"{current_participant}，创建世界第1步：这张 Nortantis 地图上的主要大陆、海岸和近海岛屿给你的第一印象是什么？",
                "其他人也可以顺手补画面、国家、角色或谜团；我会先记下，再带大家回到创建流程的最早缺项。",
            ]
        message = (
            f"我是{style.name}，这次会以“{style.voice}”的方式和你们一起搭世界。\n"
            "第零章先按《最终物语》的创建世界流程走；玩家随时可以跳着提出点子，我会记录，但引导下一步时会回到最早没完成的项目。\n"
            "先从地图卡和主要陆地开始。旅行距离之后统一用角色徒步一天可走的距离，也就是一个旅行日来记录。"
            + (f"\n这轮先把聚光灯给 {current_participant}。" if current_participant else "")
        )
        return SessionZeroResponse(
            message=message,
            stage=SessionZeroStage.TONE,
            suggestions=suggestions,
            questions=questions,
            world_updates={"open_questions": questions},
        )

    def respond(self, state: SessionZeroState, speaker: str, message: str) -> SessionZeroResponse:
        if self._looks_like_session_zero_nudge(message):
            return self._compose_session_zero_nudge(state, speaker)
        school = requested_spell_school(message)
        if school:
            return build_spell_options_response(state, school)
        if message_requests_class_list(message) and not self._looks_like_hero_message(message):
            return build_class_options_response(state, speaker)
        if message_requests_equipment_advice(message):
            return self._compose_equipment_advice(state, speaker)
        equipment_reference = requested_equipment_reference(message)
        if equipment_reference:
            return self._compose_equipment_reference(state, equipment_reference)
        if self._wants_hero_draft_reveal(message):
            return self._compose_hero_draft_reveal(state, speaker)
        if self._wants_session_zero_status(message):
            return self._compose_session_zero_status(state, speaker)

        updates: dict[str, Any] = {}
        accepted_facts: list[str] = []
        suggestions: list[str] = []
        questions: list[str] = []

        world_style = self._infer_world_style(message)
        if world_style:
            updates["world_style"] = world_style
            updates["core_themes"] = self._themes_for_style(world_style)
            suggestions.extend(self._style_suggestions(world_style))

        map_card = self._infer_map_card(message)
        if map_card:
            updates["map_card"] = map_card

        magic_tech_role = self._infer_magic_tech_role(message)
        if magic_tech_role:
            updates["magic_tech_role"] = magic_tech_role
            accepted_facts.append(f"记录魔法与科技定位：{magic_tech_role}")

        group_concept = self._infer_group_concept(message)
        if group_concept:
            updates["group_concept"] = group_concept
            accepted_facts.append(f"小队原型暂定为【{group_concept}】。")

        starting_region = self._infer_starting_region(message, world_style or state.world.world_style)
        if starting_region:
            updates["starting_region"] = starting_region
            accepted_facts.append(f"起始地区可以从【{starting_region}】展开。")

        locations = self._infer_locations(message, world_style or state.world.world_style)
        if locations:
            updates["major_locations"] = locations
            accepted_facts.extend([f"记录关键地点【{name}】：{detail}" for name, detail in locations.items()])

        kingdoms = self._infer_kingdoms(message)
        if kingdoms:
            updates["kingdoms"] = kingdoms
            updates["kingdom_contributors"] = {speaker: list(kingdoms)}
            accepted_facts.extend([f"记录国家【{name}】：{detail}" for name, detail in kingdoms.items()])

        historical_events = self._infer_historical_events(message)
        if historical_events:
            updates["historical_events"] = historical_events
            updates["historical_event_contributors"] = {speaker: historical_events}
            accepted_facts.extend([f"记录历史事件：{event}" for event in historical_events])

        factions = self._infer_factions(message)
        if factions:
            updates["factions"] = factions
            accepted_facts.extend([f"记录阵营【{name}】：{detail}" for name, detail in factions.items()])

        villain_seeds = self._infer_villain_seeds(message)
        if villain_seeds:
            updates["villain_seeds"] = villain_seeds
            accepted_facts.extend([f"记录反派种子：{seed}" for seed in villain_seeds])

        mysteries = self._infer_mysteries(message, state=state)
        if mysteries:
            updates["mysteries"] = mysteries
            updates["mystery_contributors"] = {speaker: mysteries}
            accepted_facts.extend([f"记录谜团：{mystery}" for mystery in mysteries])

        world_threats = self._infer_world_threats(message)
        if world_threats:
            updates["world_threats"] = world_threats
            updates["threat_contributors"] = {speaker: world_threats}
            accepted_facts.extend([f"记录世界威胁：{threat}" for threat in world_threats])

        contribution_skips = self._infer_contribution_skips(speaker, message, state)
        if contribution_skips:
            for field_name, value in contribution_skips.items():
                updates.setdefault(field_name, {}).update(value)
            skipped_names = sorted({name for values in contribution_skips.values() for name in values})
            if skipped_names:
                accepted_facts.append(f"记录{ '、'.join(skipped_names) }暂时跳过本轮补充。")

        world_removals = self._infer_world_removals(message, state)
        if world_removals:
            updates["world_removals"] = world_removals
            accepted_facts.extend(self._format_world_removals(world_removals))

        hero_drafts = self._infer_hero_drafts(speaker, message, state)
        if hero_drafts:
            updates["hero_drafts"] = hero_drafts
            accepted_facts.extend(self._format_hero_draft_facts(hero_drafts))

        hero_draft_deletions = self._infer_hero_draft_deletions(speaker, message, state)
        if hero_draft_deletions:
            updates["hero_draft_deletions"] = hero_draft_deletions
            accepted_facts.extend([f"{key} 的角色方向已在后台调整。" for key in hero_draft_deletions])

        villain_mirrors, gm_secret_notes = self._infer_villain_design_notes(message, hero_drafts, state)
        if villain_mirrors:
            updates["villain_mirrors"] = villain_mirrors
        if gm_secret_notes:
            updates["gm_secret_notes"] = gm_secret_notes

        safety_lines, safety_veils = self._infer_safety(message)
        if safety_lines:
            updates["safety_lines"] = safety_lines
            accepted_facts.extend([f"记录界限：{line}" for line in safety_lines])
        if safety_veils:
            updates["safety_veils"] = safety_veils
            accepted_facts.extend([f"记录帷幕处理：{veil}" for veil in safety_veils])

        first_act_updates = self._infer_first_act_updates(speaker, message, state)
        if first_act_updates:
            updates.update(first_act_updates)
            if first_act_updates.get("first_act_votes"):
                accepted_facts.append(f"记录{speaker}的第一幕投票。")
            if first_act_updates.get("selected_first_act_id"):
                accepted_facts.append("第一幕目标已确认。")

        simulated_world = deepcopy(state.world)
        self._simulate_updates(simulated_world, updates)
        guidance = build_gm_guidance(simulated_world, extra_text=message)
        updates["gm_inspiration_tags"] = list(guidance.inspiration_tags)
        updates["gm_guidance_notes"] = list(guidance.principles[:6])
        updates["gm_story_beats"] = list(guidance.story_beats[:5])
        prepared_seeds = list(guidance.location_seeds[:8])
        prepared_names = {seed.name for seed in prepared_seeds}
        for seed in CORE_LOCATION_SEEDS:
            if seed.name in prepared_names:
                continue
            if not any(tag in guidance.inspiration_tags for tag in seed.inspiration_tags):
                continue
            prepared_seeds.append(seed)
            prepared_names.add(seed.name)
        for seed in guidance.location_seeds[8:]:
            if len(prepared_seeds) >= 12:
                break
            if seed.name not in prepared_names:
                prepared_seeds.append(seed)
                prepared_names.add(seed.name)
        updates["gm_prepared_locations"] = {
            seed.name: f"{seed.archetype}：{seed.brief}" for seed in prepared_seeds[:12]
        }
        if self._should_generate_first_act_candidates(simulated_world, state=state):
            candidates = self.prologue_manager.generate_candidates(simulated_world)
            simulated_world.first_act_candidates = candidates
            updates["first_act_candidates"] = [asdict(candidate) for candidate in candidates]
            accepted_facts.append("生成第一幕开局候选，等待玩家投票。")
            suggestions.append("每位玩家可以直接说“我选1/2/3”，也可以说想混合哪两个开局。")
        questions = self._next_questions(simulated_world, state=state)
        updates["open_questions"] = questions
        stage = self._next_stage(simulated_world, state=state)
        if stage == SessionZeroStage.READY:
            updates["completed"] = True

        if not suggestions:
            suggestions = self._fallback_suggestions(simulated_world)
        if not accepted_facts:
            if self._looks_like_spell_selection(message):
                unknown_spells = self._unknown_spell_names_from_message(message)
                if unknown_spells:
                    accepted_facts.append(f"我还没确认【{'、'.join(unknown_spells)}】这个法术名，先不写入角色草稿。")
                else:
                    accepted_facts.append("这条法术选择没有明确属于你的角色；我先不改其他玩家的角色草稿。")
            else:
                accepted_facts.append("我先听到了。")

        public_accepted_facts = self._public_facts(accepted_facts)
        message_text = self._compose_message(
            state=state,
            speaker=speaker,
            player_message=message,
            accepted_facts=public_accepted_facts,
            suggestions=suggestions,
            questions=questions,
            stage=stage,
            polling_world=simulated_world,
        )
        return SessionZeroResponse(
            message=message_text,
            stage=stage,
            accepted_facts=public_accepted_facts[:3],
            suggestions=suggestions[:2],
            questions=questions[:2],
            world_updates=updates,
        )

    def _infer_world_style(self, message: str) -> str:
        if "科技奇幻" in message:
            return "科技奇幻"
        if "自然奇幻" in message:
            return "自然奇幻"
        if "高度奇幻" in message or "高奇幻" in message:
            return "高度奇幻"
        if any(token in message for token in ["地下城", "迷宫", "宝箱", "奇遇", "寻宝", "地牢"]):
            return "地下城奇遇幻想"
        if any(token in message for token in ["科技", "工业", "财阀", "公司", "污染", "下层"]):
            return "科技奇幻"
        if any(token in message for token in ["自然", "森林", "村庄", "野兽", "精灵", "荒野"]):
            return "自然奇幻"
        if any(token in message for token in ["水晶", "飞艇", "城堡", "神殿"]):
            return "高度奇幻"
        if any(token in message for token in ["酒馆", "冒险者", "委托", "旅店"]):
            return "酒馆冒险幻想"
        if any(token in message for token in ["海盗", "航海", "群岛", "大海", "港口"]):
            return "海洋冒险幻想"
        if any(token in message for token in ["学院", "学校", "学生", "社团"]):
            return "魔法学院幻想"
        return ""

    def _trim_phrase(self, text: str, *, limit: int = 80) -> str:
        clean = re.sub(r"\s+", " ", str(text or "")).strip(" ，,。！？；;：:")
        if len(clean) <= limit:
            return clean
        return clean[:limit].rstrip(" ，,。！？；;：:") + "..."

    def _infer_map_card(self, message: str) -> str:
        text = str(message or "")
        map_card = ""
        if "地图卡" in text:
            if "群岛" in text or "列屿" in text:
                map_card = "群岛海域地图卡"
            elif "内海" in text:
                map_card = "内海诸国地图卡"
            elif "海" in text or "港口" in text or "海岸" in text:
                map_card = "沿海大陆与近海岛屿地图卡"
            elif "大陆" in text:
                map_card = "大陆地图卡"
            else:
                map_card = "大陆地图卡"
        elif any(token in text for token in ("地图", "地形", "版图")) and any(
            token in text for token in ("大陆", "群岛", "列屿", "内海", "海岸", "港口", "城市群", "边境")
        ):
            if any(token in text for token in ("群岛", "列屿")):
                map_card = "群岛海域地图卡"
            elif "内海" in text:
                map_card = "内海诸国地图卡"
            elif any(token in text for token in ("海岸", "港口", "近海")):
                map_card = "沿海大陆与近海岛屿地图卡"
            else:
                map_card = "大陆地图卡"
        elif any(token in text for token in ("完整的大陆", "完整大陆", "一块大陆", "大陆")):
            map_card = "大陆地图卡"
        elif any(token in text for token in ("群岛", "列屿")):
            map_card = "群岛海域地图卡"
        elif "内海" in text:
            map_card = "内海诸国地图卡"
        elif any(token in text for token in ("海岸", "近海", "港口")):
            map_card = "沿海大陆与近海岛屿地图卡"
        return map_card

    def _infer_magic_tech_role(self, message: str) -> str:
        text = str(message or "")
        if not any(token in text for token in ("魔法", "科技", "工业", "文艺复兴", "魔导", "机械")):
            return ""
        if any(token in text for token in ("工业", "大发展", "工厂", "机械", "魔导技术", "魔科技")):
            return self._trim_phrase(text)
        if any(token in text for token in ("文艺复兴", "炼金", "工坊")):
            return self._trim_phrase(text)
        if any(token in text for token in ("未解之谜", "神秘", "少数人", "禁忌")):
            return self._trim_phrase(text)
        if "魔法" in text and "科技" in text:
            return self._trim_phrase(text)
        return ""

    def _themes_for_style(self, style: str) -> list[str]:
        themes = {
            "高度奇幻": ["希望与友谊", "共同对抗毁灭世界的威胁", "跨越身份差异的羁绊"],
            "自然奇幻": ["温暖社群", "自然与野兽的和谐", "被黑暗扭曲的土地"],
            "科技奇幻": ["剥削与反抗", "魔法资源工业化", "富裕精英滥用权力"],
            "地下城奇遇幻想": ["探索未知", "宝藏与代价", "古代秘密与英雄成长"],
            "酒馆冒险幻想": ["偶然相遇", "委托背后的大阴谋", "平凡日常通向传奇"],
            "海洋冒险幻想": ["自由与归航", "未知海图", "风暴中的羁绊"],
            "魔法学院幻想": ["成长与竞争", "禁忌知识", "同伴关系的试炼"],
        }
        return themes.get(style, ["英雄的选择塑造世界", "古老秘密逐步浮现", "反派映照主角主题"])

    def _style_suggestions(self, style: str) -> list[str]:
        if style == "科技奇幻":
            return [
                "如果大家喜欢这个方向，可以把“万物皆有灵魂”落成被抽取的灵魂能源，让冲突更尖锐。",
                "反派可以看起来像救世主：他们确实带来了奇迹，但代价由别人承担。",
            ]
        if style == "自然奇幻":
            return [
                "如果大家喜欢这个方向，可以把古代废墟放在丰饶森林深处，让旧文明与自然力量形成张力。",
                "反派未必邪恶，可以是被误导的守护者或灾害化身。",
            ]
        if style == "高度奇幻":
            return [
                "如果大家喜欢这个方向，可以把水晶、飞艇、古代神器和王国政治放到同一张地图上。",
                "反派可以是某位英雄的黑暗镜像，让 Boss 战同时是理念冲突。",
            ]
        if style == "地下城奇遇幻想":
            return [
                "把第一个画面落成“可探索地点 + 诱人的宝藏 + 逼近的危险”，节奏会很顺。",
                "宝箱最好不只是奖励，也可以是谜团、契约、活体陷阱或反派留下的邀请函。",
            ]
        return [
            "先别急着分类，把这个味道落成一个地点、一个问题和一个会动起来的威胁。",
            "之后如果需要，再决定它更偏高度、自然、科技，或干脆保持混合风格。",
        ]

    def _infer_group_concept(self, message: str) -> str:
        if any(token in message for token in ["革命", "反抗", "起义", "解放", "推翻"]):
            return "反抗腐败强权的革命者小队"
        if any(token in message for token in ["神器", "守护", "封印", "护送"]):
            return "守护古代神器的命运共同体"
        if any(token in message for token in ["旅团", "冒险", "探索", "宝藏", "宝箱", "地下城", "迷宫", "奇遇", "寻宝", "飞艇团"]):
            return "追寻遗失传说的旅行英雄团"
        if any(token in message for token in ["村庄", "家乡", "同村", "社区"]):
            return "守护家园并揭开旧文明谜团的年轻英雄"
        return ""

    def _infer_starting_region(self, message: str, style: str) -> str:
        explicit_start = any(token in message for token in ("起始", "开始", "开局", "第一幕", "从这里", "从这儿"))
        if explicit_start:
            for location_name in ("白花碑驿站", "雾潮海岸", "镜线内海", "潮鸢群岛", "鸦羽山脉", "钟鸣公国", "第七采掘城"):
                if location_name in message:
                    return location_name
        if any(token in message for token in ["地下城", "迷宫", "宝箱", "奇遇", "寻宝", "地牢"]):
            return "星尘迷宫入口"
        if "下层" in message or "贫民窟" in message:
            return "永雨工业城下层"
        if "森林" in message:
            return "苍绿森林边境村"
        if "水晶" in message and explicit_start:
            return "水晶王国边境"
        if "飞艇" in message:
            return "云海航路的破损空港"
        if "村庄" in message:
            return "被古代遗迹环绕的边境村"
        if style == "科技奇幻" and any(token in message for token in ["开始", "开局", "起始"]):
            return "永雨工业城下层"
        if style == "自然奇幻" and any(token in message for token in ["开始", "开局", "起始"]):
            return "苍绿盆地的边境村"
        if style == "高度奇幻" and any(token in message for token in ["开始", "开局", "起始"]):
            return "水晶尖塔城邦边境"
        if style == "地下城奇遇幻想" and any(token in message for token in ["开始", "开局", "起始"]):
            return "星尘迷宫入口"
        return ""

    def _infer_locations(self, message: str, style: str) -> dict[str, str]:
        locations: dict[str, str] = {}
        if any(token in message for token in ["地下城", "迷宫", "宝箱", "奇遇", "寻宝", "地牢"]):
            locations["星尘迷宫"] = "会回应愿望与贪念的古代地下城，宝箱、岔路和奇遇像星图一样不断重排。"
        if "工业" in message or "下层" in message or "财阀" in message:
            locations["永雨工业城"] = "上层宫殿偷走阳光，下层街区被魔导烟雨和债务压住。"
        if "森林" in message or "自然" in message:
            locations["苍绿森林"] = "巨兽、精灵与旧文明遗迹共存，深处有不该醒来的机器。"
        if "水晶" in message:
            locations["水晶尖塔城"] = "水晶尖顶直插云霄，王国荣耀与古代秘密一同闪耀。"
        if "飞艇" in message:
            locations["云海空港"] = "破损飞艇与走私商停靠之地，消息比风暴更快。"
        if not locations and style == "科技奇幻" and "城市" in message:
            locations["永雨工业城"] = "被铜管、钢铁和灵魂能源照亮的阶级城市。"
        if not locations and style == "地下城奇遇幻想":
            locations["星尘迷宫"] = "传说会把进入者的愿望折成房间和宝箱的古代地下城。"
        return locations

    def _infer_factions(self, message: str) -> dict[str, str]:
        factions: dict[str, str] = {}
        if "帝国" in message:
            factions["灰烬帝国"] = "以秩序和安全为名扩张，试图把灵魂能源军事化。"
        if "财阀" in message or "公司" in message:
            factions["辉钢财团"] = "垄断魔导能源的企业贵族，宣传自己是文明进步的火种。"
        if "教会" in message or "神殿" in message:
            factions["星辉教会"] = "守护灵魂之流的古老组织，内部对魔科技态度分裂。"
        if "村庄" in message or "社区" in message:
            factions["边境社群"] = "依靠互助存续的小共同体，夹在旧恨与新威胁之间。"
        return factions

    def _infer_kingdoms(self, message: str) -> dict[str, str]:
        text = str(message or "")
        kingdoms: dict[str, str] = {}
        if any(token in text for token in ("国家", "王国", "帝国", "城邦", "共和国", "公国", "部族", "联盟", "同盟")):
            if self._looks_like_hero_message(text) and not self._looks_like_world_polity_contribution(text):
                return {}
            explicit = self._explicit_labeled_value(
                text,
                labels=(
                    "王国或国家",
                    "主要王国或国家",
                    "国家",
                    "主要国家",
                    "王国",
                    "主要王国",
                    "城邦",
                    "主要城邦",
                    "政体",
                    "主要政体",
                    "政权",
                    "主要政权",
                    "势力",
                    "主要势力",
                ),
            )
            explicit_name = ""
            if explicit:
                explicit_name = self._explicit_polity_name_from_value(explicit)
                if explicit_name:
                    kingdoms[explicit_name] = self._trim_phrase(explicit)
            for raw_name in self._polity_names_from_text(explicit or text):
                name = self._normalize_polity_name(raw_name)
                if name:
                    kingdoms[name] = self._trim_phrase(explicit or text)
            if "帝国" in text and not kingdoms:
                kingdoms["灰烬帝国"] = "以秩序和安全为名扩张，试图把灵魂能源军事化。"
            if "王国" in text and not kingdoms:
                kingdoms["水晶王国"] = "以水晶与古老王权维系边境秩序的国家。"
        return kingdoms

    def _looks_like_world_polity_contribution(self, text: str) -> bool:
        markers = (
            "主要国家",
            "主要王国",
            "主要政权",
            "主要势力",
            "世界里",
            "世界中",
            "这个世界",
            "国家设定",
            "王国设定",
            "公国设定",
            "我贡献",
            "我补充",
            "王国或国家",
        )
        return any(marker in text for marker in markers)

    def _polity_names_from_text(self, text: str) -> list[str]:
        names: list[str] = []
        pattern = r"[\u4e00-\u9fffA-Za-z0-9·]{1,14}(?:王国|帝国|城邦|共和国|公国|部族|联盟|同盟)"
        for match in re.finditer(pattern, str(text or "")):
            candidate = match.group(0)
            candidate = re.sub(r"^从.+?(?:前往|去往|抵达|到达|去|到)", "", candidate)
            candidate = re.split(r"(?:叫做|名为|称为|是|为|叫|：|:|、|，|,|；|;|和|与|及)", candidate)[-1]
            candidate = re.split(r"(?:这个|这座|这片|这一)", candidate, maxsplit=1)[0]
            candidate = candidate.strip(" 的了一个一座这那「」『』【】[]()（）\"'")
            if candidate and candidate not in names:
                names.append(candidate)
        return names

    def _explicit_polity_name_from_value(self, value: str) -> str:
        candidate = re.split(r"[，,。！？；;\n]", str(value or ""), maxsplit=1)[0]
        candidate = re.split(r"(?:这个|这座|这片|这一|是一|是个|是一个|的)", candidate, maxsplit=1)[0]
        candidate = re.split(r"(?:位于|坐落于|处于|靠近|临近|沿着|沿|在)", candidate, maxsplit=1)[0]
        candidate = candidate.strip(" 的了一个一座这那「」『』【】[]()（）\"'")
        if not candidate:
            return ""
        normalized = self._normalize_polity_name(candidate)
        if normalized:
            return normalized
        if 2 <= len(candidate) <= 16 and not re.search(r"\s", candidate):
            return candidate
        return ""

    def _normalize_polity_name(self, name: str) -> str:
        candidate = str(name or "").strip(" 的了一个一座这那「」『』【】[]()（）\"'")
        candidate = re.sub(r"^(?:我的角色|角色|英雄|玩家角色|我|我们|他|她|它|他们|她们|其|这个|那个)+", "", candidate)
        candidate = re.sub(r"^(?:来自|出身|属于|效忠于|逃离|守护|管理|统治|袭击|毁灭|寻找|继承)+", "", candidate)
        candidate = re.split(r"(?:位于|坐落于|处于|靠近|临近|沿着|沿|在)", candidate, maxsplit=1)[0]
        candidate = candidate.strip(" 的了一个一座这那「」『』【】[]()（）\"'")
        if not candidate or candidate.startswith("的"):
            return ""
        if len(candidate) > 16:
            return ""
        if any(token in candidate for token in ("我的角色", "角色", "大钟", "能安抚", "是", "叫", "想", "我要")):
            return ""
        if not re.search(r"(?:王国|帝国|城邦|共和国|公国|部族|联盟|同盟)$", candidate):
            return ""
        return candidate

    def _infer_historical_events(self, message: str) -> list[str]:
        text = str(message or "")
        explicit = self._explicit_labeled_value(
            text,
            labels=("我的重大历史事件", "重大历史事件", "我的历史事件", "历史事件"),
        )
        if explicit:
            return [explicit]
        event_tokens = (
            "历史事件",
            "战争",
            "大战",
            "灾变",
            "大崩坏",
            "革命",
            "实验事故",
            "事故",
            "瘟疫",
            "陨落",
            "失落",
            "放逐",
            "曾经",
            "过去",
        )
        if not any(token in text for token in event_tokens):
            return []
        if self._looks_like_hero_message(text) and not any(token in text for token in ("世界", "国家", "王国", "帝国", "大陆")):
            return []
        return [self._trim_phrase(text)]

    def _infer_villain_seeds(self, message: str) -> list[str]:
        seeds: list[str] = []
        if any(token in message for token in ["地下城", "迷宫", "宝箱", "奇遇", "寻宝", "地牢"]):
            seeds.append("迷宫深处的收藏家把英雄的愿望锁进宝箱，声称自己只是在保存奇迹。")
        if "帝国" in message or "皇帝" in message:
            seeds.append("灰烬帝国的执政者相信牺牲少数灵魂可以换来世界秩序。")
        if "财阀" in message or "公司" in message:
            seeds.append("辉钢财团的继承人把剥削包装成奇迹，仍被许多人视为救世主。")
        if "女王" in message:
            seeds.append("一位女王正为了拯救王国而走向不可饶恕的极端。")
        if "巫师" in message or "魔导师" in message:
            seeds.append("失落时代的魔导师正在重启会改变世界的仪式。")
        if "反派" in message and not seeds:
            seeds.append("主要反派应该是某位英雄主题的黑暗镜像。")
        return seeds

    def _infer_mysteries(self, message: str, *, state: SessionZeroState | None = None) -> list[str]:
        text = str(message or "")
        if any(token in text for token in ("跳过", "先过", "没想法", "沒有想法", "没有灵感", "不用等", "之后再补", "暂时没有")):
            return []
        mystery = self._explicit_labeled_value(
            text,
            labels=("我的世界奥秘", "世界奥秘", "我的世界谜团", "世界谜团", "谜团", "奥秘", "未解之谜"),
        )
        if mystery:
            mystery = self._clean_mystery_phrase(mystery)
            return [mystery] if mystery else []
        if not self._is_collecting_mystery(state):
            return []
        if self._looks_like_non_mystery_meta(text):
            return []
        mystery = self._clean_mystery_phrase(text)
        if not self._looks_like_mystery_content(mystery):
            return []
        return [mystery] if mystery else []

    def _is_collecting_mystery(self, state: SessionZeroState | None) -> bool:
        if state is None:
            return False
        return "第5步" in self._world_creation_question(state.world, state=state)

    def _clean_mystery_phrase(self, message: str) -> str:
        clean = self._trim_phrase(message)
        clean = re.sub(r"^(?:世界)?(?:谜团|奥秘|未解之谜|世界之谜)\s*[：:]\s*", "", clean)
        clean = re.sub(r"^(?:我(?:想要|补|贡献)?的?)?(?:世界)?(?:谜团|奥秘|未解之谜|世界之谜)\s*(?:是|为|:|：)\s*", "", clean)
        clean = re.split(
            r"(?:。|；|;|\n)\s*(?:我投|投这个|第一幕|额外补(?:一个)?反派|反派种子|小队原型|界限|帷幕|我的角色|角色名|职业|属性|技能|装备|羁绊)",
            clean,
            maxsplit=1,
        )[0]
        return clean.strip(" ，,。！？；;：:") or self._trim_phrase(message)

    def _looks_like_non_mystery_meta(self, message: str) -> bool:
        text = str(message or "")
        return any(token in text for token in ("我投", "投这个", "第一幕", "反派种子", "小队原型", "界限", "帷幕")) and not any(
            token in text for token in ("谜团", "奥秘", "未解之谜", "为什么", "为何")
        )

    def _looks_like_mystery_content(self, mystery: str) -> bool:
        text = str(mystery or "").strip()
        if not text:
            return False
        return any(
            token in text
            for token in (
                "为什么",
                "为何",
                "如何",
                "怎么",
                "谁",
                "哪里",
                "何处",
                "真相",
                "秘密",
                "谜",
                "奥秘",
                "未解",
                "消失",
                "失踪",
                "遗忘",
                "改写",
                "隐藏",
                "封印",
                "无人记得",
                "没人记得",
                "不该存在",
                "异常",
                "？",
                "?",
            )
        )

    def _explicit_labeled_value(self, text: str, *, labels: tuple[str, ...]) -> str:
        label_pattern = "|".join(re.escape(label) for label in labels)
        match = re.search(rf"(?:{label_pattern})\s*(?:是|为|:|：)\s*(?P<value>.+)", text)
        if not match:
            return ""
        value = match.group("value")
        stop_pattern = (
            r"(?:。|，|,|；|;|\n)\s*(?:"
            r"王国或国家|主要王国或国家|国家|主要国家|王国|主要王国|城邦|主要城邦|政体|主要政体|政权|主要政权|"
            r"我的重大历史事件|重大历史事件|我的历史事件|历史事件|"
            r"我想要的谜团|我想要的奥秘|我的世界奥秘|世界奥秘|我的世界谜团|世界谜团|谜团|奥秘|未解之谜|"
            r"我的世界性威胁|世界性威胁|世界威胁|威胁|"
            r"小队原型|队伍原型|界限|帷幕|描述风格|我的角色|角色叫|职业|属性|技能|装备|羁绊"
            r")\s*(?:是|为|:|：)"
        )
        value = re.split(stop_pattern, value, maxsplit=1)[0]
        return self._trim_phrase(value, limit=120)

    def _infer_world_threats(self, message: str) -> list[str]:
        threats: list[str] = []
        text = str(message or "")
        explicit = self._explicit_labeled_value(
            text,
            labels=("我的世界性威胁", "世界性威胁", "世界威胁", "威胁"),
        )
        if explicit:
            return [explicit]
        historical_context = any(token in text for token in ("年前", "曾经", "过去", "历史", "最后一役", "战争", "迎来了惨痛的胜利"))
        current_threat_cues = (
            "威胁",
            "正遭受",
            "正在",
            "如今",
            "现在",
            "当前",
            "持续",
            "不断",
            "扩散",
            "蔓延",
            "侵蚀",
            "腐化",
            "瘴气",
            "灭亡",
            "危及",
            "卷土重来",
            "残党",
            "试图",
            "企图",
            "准备",
            "反派",
            "敌人",
        )
        if not any(token in text for token in current_threat_cues):
            return []
        if historical_context and not any(token in text for token in ("如今", "现在", "当前", "正在", "残党", "卷土重来", "持续", "不断")):
            return []
        if "瘴气" in text or "腐化" in text or "侵蚀" in text:
            threats.append("不断扩散并侵蚀陆地的腐化瘴气。")
        elif "灾害" in text:
            threats.append(self._trim_phrase(text))
        elif "帝国" in text:
            threats.append("渴望权力并试图支配他国的帝国。")
        elif "神" in text:
            threats.append("暴戾或失控的神性存在正在威胁凡世。")
        else:
            threats.append(self._trim_phrase(text))
        return self._unique(threats)

    def _infer_world_removals(self, message: str, state: SessionZeroState) -> dict[str, list[str]]:
        if not any(token in message for token in ["删除", "移除", "取消", "不要这个设定", "先不要", "不采用"]):
            return {}
        removals: dict[str, list[str]] = {}
        world = state.world
        for field_name in ("major_locations", "factions", "pillars"):
            keys = []
            for key in getattr(world, field_name):
                if key and key in message:
                    keys.append(key)
            if keys:
                removals[field_name] = keys
        for field_name in ("villain_seeds", "villain_mirrors", "mysteries", "core_themes"):
            values = []
            for value in getattr(world, field_name):
                if value and self._short_overlap(value, message):
                    values.append(value)
            if values:
                removals[field_name] = values
        return removals

    def _infer_hero_drafts(self, speaker: str, message: str, state: SessionZeroState) -> dict[str, dict[str, Any]]:
        patch = self._hero_patch_from_message(speaker, message)
        if not patch:
            return {}
        key = self._hero_draft_key(speaker, patch, state, message)
        if not key:
            return {}
        return {key: patch}

    def _hero_patch_from_message(self, speaker: str, message: str) -> dict[str, Any]:
        if not self._looks_like_hero_message(message):
            return {}
        patch: dict[str, Any] = {"player_name": speaker}
        hero_name = self._first_named_group(
            [
                r"(?:我的)?(?:角色名|英雄名|主角名)\s*(?:是|为|叫|:|：)?\s*(?P<value>[^，,。！？；;\n]+)",
                r"(?:我的)?(?:角色|英雄|主角)\s*(?:叫|名叫|名字是|名称是)\s*(?P<value>[^，,。！？；;\n]+)",
                r"(?:我的)?(?:角色|主角)\s*(?!是|为|叫|名|名叫|名字是|名称是)(?P<value>[\u4e00-\u9fa5A-Za-z0-9_·]{1,16})",
                r"我的英雄\s*(?!是|为|叫|名|名叫|名字是|名称是)(?P<value>[\u4e00-\u9fa5A-Za-z0-9_·]{1,16})",
                r"我(?:叫|名叫)\s*(?P<value>[^，,。！？；;\n]+)",
            ],
            message,
        )
        if hero_name and self._is_plausible_hero_name(hero_name):
            patch["hero_name"] = hero_name
        elif hero_name:
            hero_name = ""

        identity = self._first_named_group(
            [
                r"(?:身份|定位|概念)\s*(?:是|为|:|：)\s*(?P<value>[^，,。！？；;\n]+)",
                r"我想(?:创建|做|玩|扮演)\s*(?:一个|一名)?\s*(?P<value>[^，,。！？；;\n]+)",
            ],
            message,
        )
        if not identity and hero_name:
            identity = self._infer_identity_after_hero_name(message, hero_name)
        if identity:
            patch["identity"] = identity

        theme = self._first_named_group([r"(?:主题|核心主题)\s*(?:是|为|:|：)?\s*(?P<value>[^，,。！？；;\n]+)"], message)
        if theme:
            patch["theme"] = self._normalize_theme(theme)

        origin = self._first_named_group(
            [
                r"(?:起源|故乡|出身)\s*(?:是|为|:|：)?\s*(?P<value>[^，,。！？；;\n]+)",
                r"(?:来自|出生于)\s*(?P<value>[^，,。！？；;\n]+)",
            ],
            message,
        )
        if origin:
            patch["origin"] = origin

        classes, remove_classes = self._infer_classes_from_message(message)
        if classes:
            patch["classes"] = classes
        if remove_classes:
            patch["remove_classes"] = remove_classes

        attributes, remove_attributes = self._infer_attributes_from_message(message)
        if attributes:
            patch["attributes"] = attributes
        if remove_attributes:
            patch["remove_attributes"] = remove_attributes

        explicit_skill_list = self._has_explicit_skill_list(message)
        skills, remove_skills = self._infer_skills_from_message(message)
        if skills:
            patch["skills"] = skills
            if explicit_skill_list:
                patch["replace_skills"] = True
        if remove_skills:
            patch["remove_skills"] = remove_skills

        equipment, remove_equipment = self._infer_equipment_from_message(message)
        if equipment:
            patch["equipment"] = equipment
        if remove_equipment:
            patch["remove_equipment"] = remove_equipment

        spells = self._infer_spells_from_message(message)
        if spells:
            patch["spells"] = spells
        elif self._looks_like_spell_selection(message):
            unknown_spells = self._unknown_spell_names_from_message(message)
            if unknown_spells:
                patch["open_questions"] = [f"我还没确认法术【{'、'.join(unknown_spells)}】；请从标准可选法术中选择。"]

        bonds = self._infer_bonds_from_message(message)
        if bonds:
            patch["bonds"] = bonds

        notes = self._infer_character_notes(message)
        if notes:
            patch["notes"] = notes

        if any(token in message for token in ["确认角色", "角色确认", "确认草稿", "就这个角色", "这个角色可以", "就这样", "定稿", "正式建卡", "建卡"]):
            patch["confirmed"] = True

        questions = self._questions_for_patch(patch)
        if questions:
            existing_questions = patch.get("open_questions", [])
            if not isinstance(existing_questions, list):
                existing_questions = []
            patch["open_questions"] = self._unique([*existing_questions, *questions])[:2]
        return patch

    def _infer_hero_draft_deletions(
        self,
        speaker: str,
        message: str,
        state: SessionZeroState,
    ) -> dict[str, list[str]]:
        if not any(token in message for token in ["删除", "移除", "清空", "取消", "先不要", "不要这个"]):
            return {}
        fields: list[str] = []
        field_tokens = {
            "hero_name": ["名字", "角色名"],
            "identity": ["身份", "定位", "概念"],
            "theme": ["主题"],
            "origin": ["起源", "故乡", "出身"],
            "classes": ["职业"],
            "attributes": ["属性"],
            "skills": ["技能"],
            "spells": ["法术"],
            "equipment": ["装备", "武器", "防具", "盾牌"],
            "bonds": ["羁绊"],
            "notes": ["笔记", "备注", "背景"],
        }
        for field_name, tokens in field_tokens.items():
            if any(token in message for token in tokens):
                fields.append(field_name)
        if not fields:
            return {}
        return {self._hero_draft_key(speaker, {}, state, message): fields}

    def _infer_villain_design_notes(
        self,
        message: str,
        hero_drafts: dict[str, dict[str, Any]],
        state: SessionZeroState,
    ) -> tuple[list[str], list[str]]:
        mirrors: list[str] = []
        secrets: list[str] = []
        if "反派" in message and any(token in message for token in ["对立面", "镜像", "黑暗面", "扭曲", "映照"]):
            mirrors.append("主要反派应是一个或多个主角主题的黑暗面或扭曲映照，而不是单纯作恶。")
        for key, patch in hero_drafts.items():
            hero_name = patch.get("hero_name") or state.world.hero_drafts.get(key, HeroDraft()).hero_name or key
            theme = patch.get("theme") or state.world.hero_drafts.get(key, HeroDraft()).theme
            identity = patch.get("identity") or state.world.hero_drafts.get(key, HeroDraft()).identity
            if theme:
                mirrors.append(f"为{hero_name}准备一个映照其主题【{theme}】的对手：对方相信同一种情感，但选择更残酷的道路。")
                secrets.append(f"GM暗线候选：让{hero_name}的首个重要反派用【{theme}】的扭曲版本诱惑或挑战他们。")
            elif identity:
                mirrors.append(f"为{hero_name}准备一个扭曲其身份【{identity}】的对手，让 Boss 战同时是理念冲突。")
        return self._unique(mirrors), self._unique(secrets)

    def _looks_like_hero_message(self, message: str) -> bool:
        hero_tokens = ["我的角色", "我的英雄", "角色叫", "英雄叫", "主角叫", "我想玩", "我想创建", "我想扮演", "身份", "主题", "起源", "故乡", "羁绊"]
        if any(token in message for token in hero_tokens):
            return True
        if any(class_name in message for class_name in set(CLASS_ALIASES.values())):
            return True
        if self._has_explicit_skill_list(message) and any(skill_name in message for skill_name in set(SKILL_CATALOG) | set(SKILL_ALIASES)):
            return True
        if any(token in message for token in ("选择", "选", "学习", "学", "习得")) and any(
            spell_name in message for spell_name in self._known_spell_candidates()
        ):
            return True
        if re.search(r"(DEX|INS|MIG|WLP|敏捷|洞察|力量|意志)\s*(?:是|为|:|：)?\s*d?(?:6|8|10|12)", message, re.IGNORECASE):
            return True
        return False

    def _hero_draft_key(
        self,
        speaker: str,
        patch: dict[str, Any],
        state: SessionZeroState,
        message: str = "",
    ) -> str:
        hero_name = str(patch.get("hero_name", "")).strip()
        if hero_name:
            for key, draft in state.world.hero_drafts.items():
                if key == hero_name or draft.hero_name == hero_name:
                    return key
            return speaker or hero_name
        mentioned_key = self._mentioned_draft_key_by_position(state, message)
        if mentioned_key:
            return mentioned_key
        if patch.get("spells"):
            spell_target_key = self._spell_choice_target_key(speaker, state)
            if spell_target_key:
                return spell_target_key
            if not self._patch_has_non_spell_character_details(patch):
                return ""
        for key, draft in state.world.hero_drafts.items():
            if str(draft.player_name or "").strip() == speaker:
                return key
        if speaker in state.world.hero_drafts:
            return speaker
        return speaker or hero_name or "未命名玩家"

    def _patch_has_non_spell_character_details(self, patch: dict[str, Any]) -> bool:
        return any(
            field_name in patch
            for field_name in (
                "hero_name",
                "identity",
                "theme",
                "origin",
                "classes",
                "attributes",
                "skills",
                "equipment",
                "bonds",
                "notes",
                "confirmed",
            )
        )

    def _mentioned_draft_key_by_position(self, state: SessionZeroState, message: str) -> str:
        text = str(message or "")
        if not text:
            return ""
        candidates: list[tuple[int, int, str]] = []
        for key, draft in state.world.hero_drafts.items():
            for name in (str(key).strip(), str(draft.hero_name or "").strip()):
                if not name:
                    continue
                index = text.find(name)
                if index >= 0:
                    candidates.append((index, -len(name), key))
        if not candidates:
            return ""
        candidates.sort()
        return candidates[0][2]

    def _spell_choice_target_key(self, speaker: str, state: SessionZeroState) -> str:
        missing_keys = [key for key, draft in state.world.hero_drafts.items() if self._missing_spell_choices(draft)]
        if not missing_keys:
            return ""
        for key in missing_keys:
            draft = state.world.hero_drafts[key]
            if key == speaker or str(draft.player_name or "").strip() == speaker:
                return key
        return ""

    def _infer_identity_after_hero_name(self, message: str, hero_name: str) -> str:
        """支持玩家用自然短句描述角色，例如“我的角色露米娅，爱拆宝箱的机关师”。"""

        index = message.find(hero_name)
        if index < 0:
            return ""
        tail = message[index + len(hero_name) :]
        parts = [part.strip() for part in re.split(r"[，,。！？；;\n]", tail) if part.strip()]
        stop_prefixes = ("主题", "核心主题", "起源", "故乡", "出身", "职业", "属性", "技能", "法术", "咒语", "装备", "羁绊")
        for part in parts:
            if part.startswith(stop_prefixes):
                continue
            if any(token in part for token in ("职业", "属性", "技能", "装备", "羁绊")):
                continue
            clean = self._clean_phrase(part)
            if clean:
                return clean
        return ""

    def _first_named_group(self, patterns: list[str], message: str) -> str:
        for pattern in patterns:
            match = re.search(pattern, message, re.IGNORECASE)
            if match:
                return self._clean_phrase(match.group("value"))
        return ""

    def _clean_phrase(self, value: str) -> str:
        value = value.strip().strip("。；; ，,")
        cleaned = re.sub(r"^(?:一个|一名|想要|希望|可以是)\s*", "", value).strip()
        return cleaned or value.strip()

    def _is_plausible_hero_name(self, value: str) -> bool:
        clean = value.strip()
        if not clean:
            return False
        command_tokens = (
            "创建角色",
            "确认角色",
            "正式建卡",
            "建卡",
            "定稿",
            "并创建",
            "并正式",
            "这个角色",
            "角色可以",
        )
        return not any(token in clean for token in command_tokens)

    def _infer_classes_from_message(self, message: str) -> tuple[dict[str, int], list[str]]:
        classes: dict[str, int] = {}
        removals: list[str] = []
        for canonical in sorted(set(CLASS_ALIASES.values()), key=len, reverse=True):
            if canonical not in message:
                continue
            level = self._rank_after_name(message, canonical)
            if level <= 0:
                continue
            if self._is_removal_near(message, canonical):
                removals.append(canonical)
            else:
                classes[canonical] = level
        return classes, removals

    def _infer_attributes_from_message(self, message: str) -> tuple[dict[str, int], list[str]]:
        aliases = {
            "DEX": ["DEX", "敏捷", "灵巧"],
            "INS": ["INS", "洞察", "洞察力"],
            "MIG": ["MIG", "力量", "力量值"],
            "WLP": ["WLP", "意志", "意志力"],
        }
        attributes: dict[str, int] = {}
        removals: list[str] = []
        for key, names in aliases.items():
            for name in names:
                if name not in message:
                    continue
                if self._is_removal_near(message, name):
                    removals.append(key)
                    break
                match = re.search(rf"{re.escape(name)}\s*(?:是|为|=|:|：)?\s*d?(6|8|10|12)", message, re.IGNORECASE)
                if match:
                    attributes[key] = int(match.group(1))
                    break
        return attributes, removals

    def _infer_skills_from_message(self, message: str) -> tuple[dict[str, int], list[str]]:
        skills: dict[str, int] = {}
        removals: list[str] = []
        occupied_spans: list[tuple[int, int]] = []
        scan_text, explicit_skill_list = self._skill_scan_text(message)
        skill_names = set(SKILL_CATALOG) | set(SKILL_ALIASES)
        for skill_name in sorted(skill_names, key=len, reverse=True):
            if not explicit_skill_list and skill_name in SKILL_ALIASES and len(skill_name) <= 2:
                continue
            match = None
            for candidate in re.finditer(re.escape(skill_name), scan_text):
                span = candidate.span()
                if any(max(span[0], used[0]) < min(span[1], used[1]) for used in occupied_spans):
                    continue
                match = candidate
                occupied_spans.append(span)
                break
            if match is None:
                continue
            rank = self._rank_after_name(message, skill_name) or 1
            canonical_name = SKILL_ALIASES.get(skill_name, skill_name)
            if self._is_removal_near(message, skill_name):
                removals.append(canonical_name)
            else:
                skills[canonical_name] = rank
        return skills, removals

    def _has_explicit_skill_list(self, message: str) -> bool:
        return bool(
            re.search(
                r"(?:职业技能|技能(?:选择|列表)?|学会技能|学习技能)\s*(?:是|为|=|:|：)",
                message,
            )
        )

    def _skill_scan_text(self, message: str) -> tuple[str, bool]:
        match = re.search(
            r"(?:职业技能|技能(?:选择|列表)?|学会技能|学习技能)\s*(?:是|为|=|:|：)?\s*(?P<value>[^。！？\n]+)",
            message,
        )
        if match:
            return match.group("value"), True
        return message, False

    def _infer_equipment_from_message(self, message: str) -> tuple[list[str], list[str]]:
        names = sorted(
            set(WEAPON_TABLE) | set(ARMOR_TABLE) | set(SHIELD_TABLE),
            key=len,
            reverse=True,
        )
        equipment: list[str] = []
        removals: list[str] = []
        for name in names:
            if name == "无防具":
                continue
            if name not in message:
                continue
            if self._is_removal_near(message, name):
                removals.append(name)
            else:
                equipment.append(name)
        return self._unique(equipment), self._unique(removals)

    def _infer_spells_from_message(self, message: str) -> list[str]:
        spells: list[str] = []
        for pattern in (
            r"(?:法术|咒语)\s*(?:选择|选|学习|学|是|为|:|：)\s*(?P<value>[^，,。！？；;\n]+)",
            r"学习\s*(?P<value>[^，,。！？；;\n]+?)\s*(?:法术|咒语)",
        ):
            value = self._first_named_group([pattern], message)
            if value:
                for item in re.split(r"[、/和与+＋]", value):
                    clean = self._clean_phrase(item)
                    if clean and is_known_spell(clean):
                        spells.append(normalize_spell_name(clean))
        if any(token in message for token in ("选择", "选", "学习", "学", "习得")):
            for spell_name in self._known_spell_candidates():
                if spell_name in message:
                    spells.append(normalize_spell_name(spell_name))
        return self._unique(spells)

    def _known_spell_candidates(self) -> list[str]:
        names = set(SPELL_ALIASES)
        for school in ("元素使法术", "熵术士法术", "御魂使法术"):
            names.update(spell_names_for_school(school))
        return sorted(names, key=len, reverse=True)

    def _unknown_spell_names_from_message(self, message: str) -> list[str]:
        names: list[str] = []
        for pattern in (
            r"(?:法术|咒语)\s*(?:选择|选|学习|学|是|为|:|：)\s*(?P<value>[^，,。！？；;\n]+)",
            r"学习\s*(?P<value>[^，,。！？；;\n]+?)\s*(?:法术|咒语)",
        ):
            value = self._first_named_group([pattern], message)
            if not value:
                continue
            for item in re.split(r"[、/和与+＋]", value):
                clean = self._clean_phrase(item)
                if clean and not is_known_spell(clean):
                    names.append(clean)
        return self._unique(names)

    def _infer_bonds_from_message(self, message: str) -> list[str]:
        bonds: list[str] = []
        emotion_words = "赞赏|钦佩|自卑|忠诚|信赖|信任|不信任|猜忌|喜爱|憎恨|仇恨"
        for match in re.finditer(rf"对(?P<target>[^，,。！？；;\n]+?)(?P<emotion>{emotion_words})", message):
            target = match.group("target").strip("：:、 　")
            emotion = match.group("emotion")
            if target:
                bonds.append(f"{target}：{emotion}")
        for pattern in (
            r"羁绊\s*(?:是|为|:|：)\s*(?P<value>[^。！？；;\n]+)",
            r"我和\s*(?P<value>[^。！？；;\n]+?)\s*有羁绊",
        ):
            value = self._first_named_group([pattern], message)
            if value:
                for item in re.split(r"[、/和与+＋，,]", value):
                    clean = self._clean_phrase(item)
                    if not clean:
                        continue
                    if bonds and re.search(rf"^对.+(?:{emotion_words})$", clean):
                        continue
                    bonds.append(clean)
        return self._unique(bonds)

    def _infer_character_notes(self, message: str) -> list[str]:
        note_tokens = ["秘密", "目标", "誓言", "曾经", "失去", "寻找", "复仇", "赎罪", "保护", "信念", "使命", "希望", "疑虑"]
        if any(token in message for token in note_tokens) and self._looks_like_hero_message(message):
            return [message.strip()]
        return []

    def _rank_after_name(self, message: str, name: str) -> int:
        match = re.search(rf"{re.escape(name)}\s*(?:等级|职业等级)?\s*(\d+)\s*级?", message)
        if match:
            return int(match.group(1))
        return 0

    def _is_removal_near(self, message: str, name: str) -> bool:
        index = message.find(name)
        if index < 0:
            return False
        window = message[max(0, index - 8) : index + len(name) + 8]
        return any(token in window for token in ["删除", "移除", "取消", "不选", "不要", "放弃", "换掉"])

    def _looks_like_spell_selection(self, message: str) -> bool:
        text = str(message or "")
        return any(token in text for token in ("法术", "咒语")) and any(
            token in text for token in ("选择", "选", "学习", "学")
        )

    def _questions_for_patch(self, patch: dict[str, Any]) -> list[str]:
        questions: list[str] = []
        missing_spells = self._missing_spell_choices_for_values(patch.get("skills", {}), patch.get("spells", []))
        if missing_spells:
            questions.append(f"这个技能会让角色习得法术，请补【{'、'.join(missing_spells)}】。")
        profile_context = any(patch.get(field_name) for field_name in ("hero_name", "identity", "theme", "origin"))
        if profile_context:
            if not patch.get("hero_name"):
                questions.append("这个英雄叫什么名字？")
            if not patch.get("identity"):
                questions.append("用一句短语概括他们当前如何看待自己，也就是身份；例如“失国公主”或“研究魔导技术的科学家”。")
            if not patch.get("theme"):
                questions.append("他们的主题是什么？可从慈悲、愤怒、复仇、归属、愧疚、使命、希望、野心、疑虑、正义中选，也可以自定义一种会支配行动的强烈信念。")
            elif self._theme_needs_clarification(str(patch["theme"])):
                questions.append(
                    f"【{patch['theme']}】可以作为自定义主题，但主题需要是会支配行动的强烈信念、情感或直觉；它如何推动这个英雄冒险？"
                )
            if not patch.get("origin"):
                questions.append("他们的故乡是哪里？可以选世界表已有地点，也可以新增一个地点。")
        if patch.get("classes") and sum(patch["classes"].values()) != 5:
            questions.append("职业等级最终需要合计 5 级；你想怎么分配？")
        return questions[:2]

    def _normalize_theme(self, theme: str) -> str:
        clean = self._clean_phrase(theme)
        return THEME_ALIASES.get(clean, clean)

    def _theme_needs_clarification(self, theme: str) -> bool:
        clean = self._clean_phrase(theme)
        if clean in RECOMMENDED_CHARACTER_THEMES:
            return False
        return bool(clean)

    def _format_hero_draft_facts(self, hero_drafts: dict[str, dict[str, Any]]) -> list[str]:
        facts: list[str] = []
        for key, patch in hero_drafts.items():
            hero_name = str(patch.get("hero_name") or key).strip()
            display_name = f"【{hero_name}】" if hero_name else str(key)
            detail_facts: list[str] = []
            if isinstance(patch.get("skills"), dict) and patch["skills"]:
                detail_facts.append(f"已记录{display_name}的技能：{'、'.join(str(name) for name in patch['skills'])}。")
            if isinstance(patch.get("spells"), list) and patch["spells"]:
                detail_facts.append(f"已记录{display_name}的法术：{'、'.join(str(name) for name in patch['spells'])}。")
            if isinstance(patch.get("equipment"), list) and patch["equipment"]:
                detail_facts.append(f"已记录{display_name}的装备：{'、'.join(str(name) for name in patch['equipment'])}。")
            if detail_facts:
                facts.extend(detail_facts)
                continue
            if hero_name:
                facts.append(f"已记录【{hero_name}】的角色方向。")
            else:
                facts.append(f"{key} 的角色方向已记录。")
        return facts

    def _public_facts(self, accepted_facts: list[str]) -> list[str]:
        facts: list[str] = []
        for fact in accepted_facts:
            if self._is_internal_fact(fact):
                continue
            if is_private_visible_text(fact):
                continue
            clean = sanitize_public_text(self._redact_character_detail_fact(fact))
            if clean:
                facts.append(clean)
        return facts

    def _is_internal_fact(self, fact: str) -> bool:
        text = str(fact or "").strip()
        return text.startswith(("世界风格", "地图形式", "地图卡", "世界类型", "地图类型"))

    def _redact_character_detail_fact(self, fact: str) -> str:
        text = str(fact or "")
        match = re.match(r"已记录【([^】]+)】的(装备|技能|属性|职业|法术)[：:]", text)
        if match:
            return f"已记录【{match.group(1)}】的{match.group(2)}选择。"
        return text

    def _wants_hero_draft_reveal(self, message: str) -> bool:
        text = str(message or "")
        reveal_cues = (
            "展示角色草稿",
            "看看角色草稿",
            "看下角色草稿",
            "核对角色草稿",
            "检查角色草稿",
            "展示我的角色",
            "看看我的角色",
            "看下我的角色",
            "展示角色卡",
            "看看角色卡",
            "看下角色卡",
            "核对角色卡",
            "我还缺什么",
            "角色还缺什么",
            "草稿还缺什么",
        )
        return any(cue in text for cue in reveal_cues)

    def _wants_session_zero_status(self, message: str) -> bool:
        text = str(message or "")
        if any(token in text for token in ("创建世界还缺什么", "世界创建还缺什么", "创建世界缺什么", "世界创建缺什么")):
            return True
        if any(token in text for token in ("现在是什么阶段", "当前是什么阶段", "进行到哪", "第零章状态", "当前状态")):
            return True
        return any(token in text for token in ("还缺什么", "缺哪些", "还差什么")) and any(
            scope in text for scope in ("创建世界", "世界创建", "第零章", "Session 0", "session 0")
        )

    def _compose_session_zero_status(self, state: SessionZeroState, speaker: str) -> SessionZeroResponse:
        world = state.world
        next_question = self._world_creation_question(world, state=state)
        ready_rows: list[str] = []
        missing_rows: list[str] = []

        def add_row(label: str, ready: bool, value: str = "") -> None:
            if ready:
                ready_rows.append(label)
            else:
                missing_rows.append(label)

        step1_ready = bool(world.map_card)
        step1_value = "；".join(item for item in (world.map_card, world.continent_name) if item)
        add_row("第1步 地图卡与主要陆地", step1_ready, step1_value)
        add_row("第2步 魔法与科技地位", bool(world.magic_tech_role), world.magic_tech_role)
        add_row("第3步 主要王国/国家", bool(world.kingdoms), "、".join(world.kingdoms.keys()))
        add_row("第4步 重大历史事件", bool(world.historical_events), "；".join(world.historical_events[:2]))
        add_row("第5步 世界奥秘", bool(world.mysteries), "；".join(world.mysteries[:2]))
        add_row("第6步 世界性威胁", bool(world.world_threats), "；".join(world.world_threats[:2]))

        contribution_missing = self._world_contribution_missing_items(state)
        lines = [f"{speaker}，【创建世界】还没完成。"]
        if ready_rows:
            lines.append("已经有基础记录：" + "、".join(ready_rows[:7]) + "。")
        if missing_rows:
            lines.append("还缺：" + "、".join(missing_rows) + "。")
        if contribution_missing:
            lines.append(self._format_public_contribution_hint(contribution_missing))
        if next_question:
            lines.append(self._status_focus_prompt(next_question, contribution_missing))
        else:
            lines.append("创建世界流程已齐，接下来才进入小队原型、界限与帷幕或角色缺项。")
        return SessionZeroResponse(message="\n".join(lines), stage=self._next_stage(world, state=state))

    def _world_contribution_missing_items(self, state: SessionZeroState) -> list[tuple[str, list[str]]]:
        world = state.world
        checks = (
            ("一个王国或国家", world.kingdoms, world.kingdom_contributors),
            ("一个重大历史事件", world.historical_events, world.historical_event_contributors),
            ("一个世界奥秘", world.mysteries, world.mystery_contributors),
            ("一种世界性威胁", world.world_threats, world.threat_contributors),
        )
        items: list[tuple[str, list[str]]] = []
        for label, values, contributors in checks:
            if not values:
                continue
            missing = self._missing_contributors(state, contributors)
            if missing:
                items.append((label, missing))
        return items

    def _format_public_contribution_hint(self, items: list[tuple[str, list[str]]]) -> str:
        labels = [label for label, _names in items if label]
        if not labels:
            return ""
        return f"另外，仍有玩家可以补{self._join_cn(labels)}；没有灵感的话，说“先跳过”也可以。"

    def _status_focus_prompt(self, next_question: str, items: list[tuple[str, list[str]]]) -> str:
        if items:
            label, _names = items[0]
            return f"可选补充：{label}。有灵感的人可以直接接，没有也可以先跳过。"
        clean = self._clean_session_zero_question(next_question)
        return clean or "你们想先补哪一项？"

    def _join_cn(self, values: list[str]) -> str:
        if not values:
            return ""
        if len(values) == 1:
            return values[0]
        return "、".join(values[:-1]) + "，或" + values[-1]

    def _compose_hero_draft_reveal(self, state: SessionZeroState, speaker: str) -> SessionZeroResponse:
        draft = self._draft_for_speaker(state, speaker)
        if draft is None:
            return SessionZeroResponse(
                message=(
                    f"{speaker}，我这边还没有记录到你的角色草稿。你可以先随便抛一个方向，"
                    "比如“失国公主”“地下城厨师”“会赌牌的神射手”，我会帮你一点点补成角色卡。"
                ),
                stage=state.stage,
            )

        missing = self._hero_draft_missing_fields(draft)
        lines = [
            f"{speaker}，这是你要求公开核对的角色草稿：",
            f"名字：{draft.hero_name or '未定'}",
            f"身份：{draft.identity or '未定'}",
            f"主题：{draft.theme or '未定'}",
            f"故乡：{draft.origin or '未定'}",
            "职业：" + (self._format_level_dict(draft.classes) if draft.classes else "未定"),
            "属性：" + (self._format_level_dict(draft.attributes) if draft.attributes else "未定"),
            "技能：" + (self._format_level_dict(draft.skills) if draft.skills else "未定"),
            "法术：" + ("、".join(draft.spells) if draft.spells else "未定或不适用"),
            "装备：" + ("、".join(draft.equipment) if draft.equipment else "未定"),
            "羁绊：" + ("、".join(draft.bonds) if draft.bonds else "未定"),
            "还需要补：" + ("、".join(missing) if missing else "核心项目已齐，可以考虑确认创建。"),
        ]
        return SessionZeroResponse(
            message="\n".join(lines),
            stage=state.stage,
            questions=[f"{speaker}，要先补哪一项？也可以直接说“确认这个角色”。"],
        )

    def _compose_equipment_advice(self, state: SessionZeroState, speaker: str) -> SessionZeroResponse:
        draft = self._draft_for_speaker(state, speaker)
        if draft is None:
            return SessionZeroResponse(
                message=(
                    "初始装备总预算是 500Z；先选一件主武器，再考虑一件防具和可用盾牌。"
                    "名字可以换皮，但要写清数值模板，比如“投掷卡牌（手里剑模板）”。"
                    "正式开局资金会是未花完的钱 + 2d6x10，并拥有 3 点物语点。"
                ),
                stage=state.stage,
                questions=["你要给哪位角色配装？告诉我职业和四项属性，我就按命中骰与预算建议。"],
            )

        selected_cost = self._starting_equipment_cost(draft.equipment)
        remaining = STARTING_EQUIPMENT_BUDGET - selected_cost
        weapon_suggestions = self._weapon_suggestions_for_draft(draft, remaining)
        armor_suggestions = self._armor_suggestions_for_draft(draft, remaining)
        hero_name = draft.hero_name or speaker

        lines = [
            f"{hero_name} 的初始装备预算是 500Z；当前已记录装备花费约 {selected_cost}Z，还剩 {remaining}Z 可规划。",
            "主武器优先贴合属性，理想命中骰至少接近 d10+d8；自定义外观可以，但要绑定模板。",
        ]
        if weapon_suggestions:
            lines.append("可优先看：" + "、".join(weapon_suggestions) + "。")
        else:
            lines.append("先别买超预算或没权限的职业限定武器；法杖、魔典、钢匕首、十字弩这类基础模板通常比较稳。")
        if self._draft_is_spellcaster(draft):
            lines.append("施法角色也建议保留一件备用武器；若要让职业技能吃到“魔法类武器”，优先考虑法杖或魔典。")
        if armor_suggestions:
            lines.append("防具/盾牌：" + armor_suggestions)
        lines.append("正式建卡时会自动结算：剩余金币 + 2d6x10 作为初始资金，并获得 3 点物语点。")

        return SessionZeroResponse(
            message="\n".join(lines),
            stage=state.stage,
            questions=[f"{speaker}，要把哪几件写进 {hero_name} 的初始装备？"],
        )

    def _compose_equipment_reference(self, state: SessionZeroState, kind: str) -> SessionZeroResponse:
        parts: list[str] = []
        if kind in {"weapon", "all"}:
            parts.append("基础武器模板：" + "；".join(self._format_weapon_reference(name, item) for name, item in WEAPON_TABLE.items()))
        if kind in {"armor", "all"}:
            parts.append("基础防具模板：" + "；".join(self._format_armor_reference(name, item) for name, item in ARMOR_TABLE.items()))
        if kind in {"shield", "all"}:
            parts.append("基础盾牌模板：" + "；".join(self._format_shield_reference(name, item) for name, item in SHIELD_TABLE.items()))
        parts.append("装备可以改外观名，但写入草稿时必须标注模板；所有初始装备合计不能超过 500Z。")
        return SessionZeroResponse(
            message="\n".join(parts),
            stage=state.stage,
            questions=["要我按某位角色的属性和职业权限给选购建议吗？"],
        )

    def _format_weapon_reference(self, name: str, item) -> str:
        price = "-" if item.price == 0 else f"{item.price}Z"
        modifier = f"+{item.accuracy_modifier}" if item.accuracy_modifier else ""
        hands = "双手" if item.hands == 2 else "单手"
        range_label = "远程" if item.range_type == "ranged" else "近战"
        limited = "(+)" if item.required_ability else ""
        return (
            f"{name}{limited}{price} "
            f"{self._attr_label(item.accuracy_attributes[0])}+{self._attr_label(item.accuracy_attributes[1])}{modifier} "
            f"HR+{item.damage_bonus}物理 {hands}/{range_label}"
        )

    def _format_armor_reference(self, name: str, item) -> str:
        price = "-" if item.price == 0 else f"{item.price}Z"
        limited = "(+)" if item.required_ability else ""
        return (
            f"{name}{limited}{price} 物防{self._defense_part(item.physical_base, item.physical_bonus)} "
            f"魔防{self._defense_part(item.magic_base, item.magic_bonus)} 先攻{item.initiative_modifier:+d}"
        )

    def _format_shield_reference(self, name: str, item) -> str:
        limited = "(+)" if item.required_ability else ""
        magic = f"+{item.magic_bonus}" if item.magic_bonus else "-"
        return f"{name}{limited}{item.price}Z 物防+{item.physical_bonus} 魔防{magic}"

    def _defense_part(self, base: str | int, bonus: int) -> str:
        value = self._attr_label(base) if isinstance(base, str) else str(base)
        if bonus > 0:
            return f"{value}+{bonus}"
        if bonus < 0:
            return f"{value}{bonus}"
        return value

    def _attr_label(self, value: str | int) -> str:
        labels = {"DEX": "敏捷", "INS": "洞察", "MIG": "力量", "WLP": "意志"}
        return labels.get(str(value), str(value))

    def _starting_equipment_cost(self, equipment: list[str]) -> int:
        cost = 0
        for raw_name in equipment:
            try:
                request = resolve_equipment_request_text(raw_name)
            except ValueError:
                continue
            template = request.template_name
            if template in WEAPON_TABLE:
                cost += WEAPON_TABLE[template].price
            elif template in ARMOR_TABLE:
                cost += ARMOR_TABLE[template].price
            elif template in SHIELD_TABLE:
                cost += SHIELD_TABLE[template].price
        return cost

    def _weapon_suggestions_for_draft(self, draft: HeroDraft, remaining_budget: int) -> list[str]:
        abilities = self._equipment_abilities_for_draft(draft)
        attributes = draft.attributes or {}
        candidates = []
        for name, item in WEAPON_TABLE.items():
            if name in {"徒手攻击", "临时武器(近战)", "临时武器(远程)"}:
                continue
            if item.price > max(0, remaining_budget):
                continue
            if item.required_ability and item.required_ability not in abilities:
                continue
            dice_total = sum(int(attributes.get(attribute, 0)) for attribute in item.accuracy_attributes)
            spell_bonus = 2 if item.category == "魔法" and self._draft_is_spellcaster(draft) else 0
            candidates.append((dice_total, item.accuracy_modifier, spell_bonus, item.damage_bonus, -item.price, name, item))
        candidates.sort(reverse=True)
        return [self._format_weapon_suggestion(name, item) for *_, name, item in candidates[:3]]

    def _format_weapon_suggestion(self, name: str, item) -> str:
        modifier = f"+{item.accuracy_modifier}" if item.accuracy_modifier else ""
        return (
            f"{name}（{item.price}Z，"
            f"{self._attr_label(item.accuracy_attributes[0])}+{self._attr_label(item.accuracy_attributes[1])}{modifier}，"
            f"HR+{item.damage_bonus}）"
        )

    def _armor_suggestions_for_draft(self, draft: HeroDraft, remaining_budget: int) -> str:
        abilities = self._equipment_abilities_for_draft(draft)
        parts: list[str] = []
        if remaining_budget >= 100:
            parts.append("轻型防具会在敏捷/洞察基础上加防，旅行装束和丝质衬衫都很常用")
        if "可装备职业盔甲" in abilities and remaining_budget >= 150:
            parts.append("职业限定防具会给固定物防，敏捷低或怕异常影响防御时更稳")
        if "可装备职业盾牌" in abilities and remaining_budget >= 100:
            parts.append("若主武器是单手，青铜盾或符文盾能继续补物防，符文盾还补魔防")
        return "；".join(parts) + ("。" if parts else "")

    def _equipment_abilities_for_draft(self, draft: HeroDraft) -> list[str]:
        class_names = set(draft.classes)
        abilities: list[str] = []
        if class_names & MARTIAL_MELEE_CLASSES:
            abilities.append("可装备职业近战武器")
        if class_names & MARTIAL_RANGED_CLASSES:
            abilities.append("可装备职业远程武器")
        if class_names & MARTIAL_ARMOR_CLASSES:
            abilities.append("可装备职业盔甲")
        if class_names & MARTIAL_SHIELD_CLASSES:
            abilities.append("可装备职业盾牌")
        return abilities

    def _draft_is_spellcaster(self, draft: HeroDraft) -> bool:
        spellcasting_classes = {"奥灵使", "拟兽使", "元素使", "熵术士", "御魂使"}
        spell_granting_skills = {"元素魔法", "熵系魔法", "灵魂魔法", "形意咒法"}
        return bool(set(draft.classes) & spellcasting_classes or set(draft.skills) & spell_granting_skills)

    def _draft_for_speaker(self, state: SessionZeroState, speaker: str) -> HeroDraft | None:
        drafts = state.world.hero_drafts
        if speaker in drafts:
            return drafts[speaker]
        for draft in drafts.values():
            if draft.player_name == speaker:
                return draft
        if len(drafts) == 1:
            return next(iter(drafts.values()))
        return None

    def _hero_draft_missing_fields(self, draft: HeroDraft) -> list[str]:
        missing: list[str] = []
        if not draft.hero_name:
            missing.append("名字")
        if not draft.identity:
            missing.append("身份")
        if not draft.theme:
            missing.append("主题")
        if not draft.origin:
            missing.append("故乡")
        if not draft.classes or sum(draft.classes.values()) != 5:
            missing.append("合计 5 级的职业分配")
        if len(draft.attributes) < 4:
            missing.append("四项属性骰")
        if not draft.skills:
            missing.append("职业技能")
        missing.extend(self._missing_spell_choices(draft))
        return missing

    def _missing_spell_choices(self, draft: HeroDraft) -> list[str]:
        return self._missing_spell_choices_for_values(draft.skills, draft.spells)

    def _missing_spell_choices_for_values(self, skills: dict[str, int], spells) -> list[str]:
        requirements = required_spell_slots(skills or {})
        if not requirements:
            return []
        if isinstance(spells, str):
            spells = [spells]
        if not isinstance(spells, list):
            spells = []
        normalized_spells = [normalize_spell_name(spell) for spell in (spells or []) if str(spell).strip()]
        missing: list[str] = []
        for school, required_count in requirements.items():
            known_count = sum(1 for spell in normalized_spells if spell_school_for(spell) == school)
            missing_count = max(0, required_count - known_count)
            if missing_count:
                missing.append(f"{school}（还需 {missing_count} 个）")
        return missing

    def _spell_options_text(self, missing_choices: list[str]) -> str:
        labels: list[str] = []
        for item in missing_choices:
            for label in ("元素使法术", "熵术士法术", "御魂使法术"):
                if label in item and label not in labels:
                    labels.append(label)
        segments: list[str] = []
        for label in labels:
            names = spell_names_for_school(label)
            if names:
                segments.append(f"{label}可选：{'、'.join(names)}")
        return "；".join(segments)

    def _format_level_dict(self, values: dict[str, int]) -> str:
        return "、".join(f"{name}{value}" for name, value in values.items())

    def _format_world_removals(self, removals: dict[str, list[str]]) -> list[str]:
        labels = {
            "major_locations": "地点",
            "factions": "阵营",
            "pillars": "支柱",
            "villain_seeds": "反派种子",
            "villain_mirrors": "反派映照原则",
            "mysteries": "谜团",
            "core_themes": "主题",
        }
        facts: list[str] = []
        for field_name, values in removals.items():
            if values:
                facts.append(f"移除{labels.get(field_name, field_name)}：{'、'.join(values)}")
        return facts

    def _short_overlap(self, value: str, message: str) -> bool:
        if value in message:
            return True
        words = [word for word in re.split(r"[，,。！？；;\s]+", value) if len(word) >= 3]
        return any(word in message for word in words[:4])

    def _unique(self, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            clean = str(value).strip()
            if clean and clean not in result:
                result.append(clean)
        return result

    def _infer_contribution_skips(
        self,
        speaker: str,
        message: str,
        state: SessionZeroState,
    ) -> dict[str, dict[str, list[str]]]:
        text = str(message or "")
        if not any(token in text for token in ("跳过", "先过", "没想法", "沒有想法", "没有灵感", "不用等", "之后再补", "暂时没有")):
            return {}
        question = self._world_creation_question(state.world, state=state)
        field_name = ""
        if "第3步" in question:
            field_name = "kingdom_contributors"
        elif "第4步" in question:
            field_name = "historical_event_contributors"
        elif "第5步" in question:
            field_name = "mystery_contributors"
        elif "第6步" in question:
            field_name = "threat_contributors"
        if not field_name:
            return {}
        participant_names = [participant.name for participant in state.participants if participant.name]
        targets = [name for name in participant_names if name in text]
        if not targets and speaker in participant_names:
            targets = [speaker]
        return {field_name: {name: ["暂时跳过"] for name in targets}}

    def _infer_safety(self, message: str) -> tuple[list[str], list[str]]:
        lines: list[str] = []
        veils: list[str] = []
        for kind, item in extract_safety_declarations(message):
            if kind == "line":
                lines.append(item)
            else:
                veils.append(item)
        return lines, veils

    def _simulate_updates(self, world, updates: dict[str, Any]) -> None:
        for field_name in (
            "campaign_title",
            "continent_name",
            "world_style",
            "map_card",
            "magic_tech_role",
            "group_concept",
            "starting_region",
        ):
            if updates.get(field_name):
                setattr(world, field_name, updates[field_name])
        for field_name in (
            "core_themes",
            "historical_events",
            "villain_seeds",
            "villain_mirrors",
            "mysteries",
            "world_threats",
            "safety_lines",
            "safety_veils",
            "gm_secret_notes",
        ):
            target = getattr(world, field_name)
            for value in updates.get(field_name, []):
                if value and value not in target:
                    target.append(value)
        self._simulate_world_removals(world, updates.get("world_removals", {}))
        self._simulate_hero_drafts(world, updates.get("hero_drafts", {}))
        self._simulate_hero_draft_deletions(world, updates.get("hero_draft_deletions", {}))
        for field_name in ("pillars", "major_locations", "kingdoms", "factions"):
            getattr(world, field_name).update(updates.get(field_name, {}))
        for field_name in (
            "kingdom_contributors",
            "historical_event_contributors",
            "mystery_contributors",
            "threat_contributors",
        ):
            self._simulate_contributors(getattr(world, field_name), updates.get(field_name, {}))
        if isinstance(updates.get("first_act_candidates"), list):
            world.first_act_candidates = []
            for index, raw in enumerate(updates["first_act_candidates"], start=1):
                if not isinstance(raw, dict):
                    continue
                world.first_act_candidates.append(
                    FirstActCandidate(
                        candidate_id=str(raw.get("candidate_id") or f"first_act_{index}"),
                        title=str(raw.get("title", "")),
                        group_key=str(raw.get("group_key", "")),
                        option=int(raw.get("option", index) or index),
                        premise=str(raw.get("premise", "")),
                        questions=[str(item) for item in raw.get("questions", []) if str(item).strip()],
                        suggested_bonds=[str(item) for item in raw.get("suggested_bonds", []) if str(item).strip()],
                        notes=[str(item) for item in raw.get("notes", []) if str(item).strip()],
                        votes=[str(item) for item in raw.get("votes", []) if str(item).strip()],
                    )
                )
        if isinstance(updates.get("first_act_votes"), dict):
            for voter, candidate_id in updates["first_act_votes"].items():
                resolved = self.prologue_manager.resolve_candidate_id(world, str(candidate_id))
                if resolved:
                    world.first_act_votes[str(voter)] = resolved
        if updates.get("selected_first_act_id"):
            self.prologue_manager.confirm_winner(world, str(updates["selected_first_act_id"]))
        for value in updates.get("starting_bond_suggestions", []):
            if value and value not in world.starting_bond_suggestions:
                world.starting_bond_suggestions.append(str(value))

    def _simulate_world_removals(self, world, removals: dict) -> None:
        if not isinstance(removals, dict):
            return
        for field_name in (
            "core_themes",
            "historical_events",
            "villain_seeds",
            "villain_mirrors",
            "mysteries",
            "world_threats",
            "safety_lines",
            "safety_veils",
            "gm_secret_notes",
        ):
            for value in removals.get(field_name, []):
                if value in getattr(world, field_name):
                    getattr(world, field_name).remove(value)
        for field_name in ("pillars", "major_locations", "kingdoms", "factions"):
            for key in removals.get(field_name, []):
                getattr(world, field_name).pop(key, None)

    def _simulate_contributors(self, target: dict[str, list[str]], updates: dict) -> None:
        if not isinstance(updates, dict):
            return
        for contributor, raw_values in updates.items():
            name = str(contributor).strip()
            if not name:
                continue
            values = [raw_values] if isinstance(raw_values, str) else raw_values
            if not isinstance(values, list):
                values = [values]
            bucket = target.setdefault(name, [])
            for value in values:
                text = str(value).strip()
                if text and text not in bucket:
                    bucket.append(text)

    def _simulate_hero_drafts(self, world, updates: dict) -> None:
        if not isinstance(updates, dict):
            return
        for raw_key, patch in updates.items():
            if not isinstance(patch, dict):
                continue
            key = str(raw_key).strip()
            draft = world.hero_drafts.setdefault(key, HeroDraft(player_name=key))
            for field_name in ("player_name", "hero_name", "identity", "theme", "origin"):
                if patch.get(field_name) is not None:
                    setattr(draft, field_name, str(patch[field_name]).strip())
            for field_name in ("classes", "attributes", "skills"):
                if isinstance(patch.get(field_name), dict):
                    getattr(draft, field_name).update(
                        {str(k): self._parse_numeric_patch_value(v) for k, v in patch[field_name].items()}
                    )
            for field_name in ("bonds", "spells", "bound_arcana", "equipment", "notes", "open_questions"):
                values = patch.get(field_name, [])
                if isinstance(values, str):
                    values = [values]
                for value in values if isinstance(values, list) else []:
                    if value and value not in getattr(draft, field_name):
                        getattr(draft, field_name).append(str(value))
            for field_name in patch.get("remove_fields", []):
                self._clear_draft_field(draft, str(field_name))
            for field_name, removal_name in (
                ("classes", "remove_classes"),
                ("attributes", "remove_attributes"),
                ("skills", "remove_skills"),
            ):
                for key_to_remove in patch.get(removal_name, []):
                    getattr(draft, field_name).pop(str(key_to_remove), None)
            for field_name, removal_name in (
                ("bonds", "remove_bonds"),
                ("spells", "remove_spells"),
                ("bound_arcana", "remove_bound_arcana"),
                ("equipment", "remove_equipment"),
                ("notes", "remove_notes"),
            ):
                for value in patch.get(removal_name, []):
                    if value in getattr(draft, field_name):
                        getattr(draft, field_name).remove(value)

    def _simulate_hero_draft_deletions(self, world, deletions: dict) -> None:
        if not isinstance(deletions, dict):
            return
        for key, fields_to_clear in deletions.items():
            draft = world.hero_drafts.get(str(key))
            if draft is None:
                continue
            for field_name in fields_to_clear:
                self._clear_draft_field(draft, str(field_name))

    def _parse_numeric_patch_value(self, value) -> int:
        if isinstance(value, int):
            return value
        text = str(value).strip().lower()
        if text.startswith("d") and text[1:].isdigit():
            return int(text[1:])
        return int(text)

    def _clear_draft_field(self, draft: HeroDraft, field_name: str) -> None:
        if field_name in {"player_name", "hero_name", "identity", "theme", "origin"}:
            setattr(draft, field_name, "")
        elif field_name in {"classes", "attributes", "skills"}:
            getattr(draft, field_name).clear()
        elif field_name in {"bonds", "spells", "bound_arcana", "equipment", "notes", "open_questions"}:
            getattr(draft, field_name).clear()

    def _next_stage(self, world, *, state: SessionZeroState | None = None) -> SessionZeroStage:
        if not self._world_creation_ready(world, state=state):
            return SessionZeroStage.TONE
        if not world.group_concept:
            return SessionZeroStage.GROUP
        if not (world.safety_lines or world.safety_veils):
            return SessionZeroStage.SAFETY
        if not self._hero_creation_ready(world, state=state):
            return SessionZeroStage.HEROES
        if not world.first_act_candidates or not world.selected_first_act_id:
            return SessionZeroStage.PROLOGUE
        return SessionZeroStage.READY

    def _looks_like_session_zero_nudge(self, message: str) -> bool:
        text = str(message or "").strip()
        compact = re.sub(r"[\s~～。.!！?？,，:：;；、（）()\[\]【】@]+", "", text)
        if not compact:
            return True
        if compact in {"时悠", "悠老师", "gm", "GM", "主持", "主持人"}:
            return True
        nudge_tokens = ("进入状态", "快进入状态", "我们在第零章", "还在第零章", "第零章呢", "跑团呀")
        if not any(token in text for token in nudge_tokens):
            return False
        substantive_tokens = (
            "世界",
            "角色叫",
            "我的角色",
            "职业",
            "技能",
            "法术",
            "故乡",
            "主题",
            "身份",
            "界限",
            "帷幕",
            "我希望",
            "我想",
            "选择",
            "记录",
        )
        return not any(token in text for token in substantive_tokens)

    def _compose_session_zero_nudge(self, state: SessionZeroState, speaker: str) -> SessionZeroResponse:
        stage = self._next_stage(state.world, state=state)
        questions = self._next_questions(state.world, state=state)
        question_text = self._clean_session_zero_question(questions[0]) if questions else "下一步补哪一项？"
        return SessionZeroResponse(
            message=f"{speaker}，我在，第零章频道已接上。现在可以继续补：{question_text}",
            stage=stage,
            questions=questions[:2],
            world_updates={"open_questions": questions[:2]},
        )

    def _next_questions(self, world, *, state: SessionZeroState | None = None) -> list[str]:
        world_question = self._world_creation_question(world, state=state)
        if world_question:
            return [world_question]
        if not world.group_concept:
            return ["小队原型是什么？你们是守护者、革命者、旅行英雄，还是某种更奇怪的命运共同体？"]
        if not (world.safety_lines or world.safety_veils):
            return ["最后确认界限与帷幕：有什么内容不出现，或只用帷幕淡出处理？"]
        hero_questions = self._hero_creation_questions(world, state=state)
        if hero_questions:
            return hero_questions
        if not world.first_act_candidates:
            return ["核心素材齐了。接下来我会给出三组第一幕候选，请大家投票选开局。"]
        if not world.selected_first_act_id:
            candidates = self.prologue_manager.format_candidates(world.first_act_candidates)
            return [f"请选择第一幕开局：\n{candidates}", "如果使用初始羁绊，可从候选里的羁绊建议挑一条给角色开局。"]
        return ["Session 0 的核心素材和第一幕开局都齐了，要不要把它整理成世界创建摘要？"]

    def _world_creation_ready(self, world, *, state: SessionZeroState | None = None) -> bool:
        return not self._world_creation_question(world, state=state)

    def _world_creation_question(self, world, *, state: SessionZeroState | None = None) -> str:
        if not world.map_card:
            return "创建世界第1步：先给这片大陆或起始地区一个第一眼画面吧。可以是大陆名、海岸线、内海、近海岛屿、醒目的地标或起始方向；地图会按 Nortantis 支持的类地球大陆来绘制。"
        if not world.magic_tech_role:
            return "创建世界第2步：魔法和科技在这个世界里是什么地位？工业大发展、文艺复兴式工坊、魔法即科技，还是魔法仍是未解之谜？"
        if not world.kingdoms:
            return self._with_question_hint(
                world,
                "kingdom",
                "创建世界第3步：请先贡献一个主要王国或国家，并给出习俗、信仰、产业、居民或生物中的至少一点。每位玩家之后都可以补一个，但不会卡死流程。",
            )
        if not world.historical_events:
            return self._with_question_hint(
                world,
                "history",
                "创建世界第4步：哪个重大历史事件塑造了今天的世界？每位玩家之后都可以贡献一个足以改变历史走向的事件，但不会卡死流程。",
            )
        if not world.mysteries:
            return self._with_question_hint(
                world,
                "mystery",
                "创建世界第5步：这个世界有什么巨大谜团或奥秘，是你希望队伍未来去探索的？每位玩家之后都可以各补一个奥秘。",
            )
        if not world.world_threats:
            return self._with_question_hint(
                world,
                "threat",
                "创建世界第6步：这个世界正遭受什么可怕威胁？最好是危及国家未来的灾害、神祗、帝国、瘴气或其他力量。每位玩家之后都可以各补一种威胁，但不会卡死流程。",
            )
        return ""

    def _with_question_hint(self, world, step: str, question: str) -> str:
        hint = question_hint_for_step(world, step)
        return f"{question} {hint}" if hint else question

    def _missing_contributors(
        self,
        state: SessionZeroState | None,
        contributors: dict[str, list[str]],
        *,
        topic: str = "",
    ) -> list[str]:
        if state is None or len(state.participants) <= 1:
            return []
        answered = {str(name).strip() for name in contributors if str(name).strip()}
        return [
            participant.name
            for participant in state.participants
            if participant.name not in answered and (not topic or topic not in participant.answered_topics)
        ]

    def _hero_creation_ready(self, world, *, state: SessionZeroState | None = None) -> bool:
        if state is not None and state.participants:
            for participant in state.participants:
                draft = self._draft_for_participant(world, participant.name)
                if draft is None or self._hero_draft_missing_fields(draft):
                    return False
            return True
        return bool(world.hero_drafts) and not self._hero_creation_questions(world)

    def _hero_creation_questions(self, world, *, state: SessionZeroState | None = None) -> list[str]:
        if state is not None and state.participants:
            for participant in state.participants:
                draft = self._draft_for_participant(world, participant.name)
                if draft is None:
                    return [f"{participant.name} 的英雄还没创建；请先给出名字、身份、主题、故乡、职业、属性、技能和装备。"]
                missing = self._hero_draft_missing_fields(draft)
                if missing:
                    display_name = draft.hero_name or participant.name
                    return [f"{display_name}还需要补：{'、'.join(missing)}。"]
            return []
        if not world.hero_drafts:
            return ["世界和小队方向先放稳了。接下来进入角色创建：谁先说自己的身份、主题和故乡？"]
        return self._hero_draft_questions(world)

    def _draft_for_participant(self, world, participant_name: str) -> HeroDraft | None:
        clean = str(participant_name or "").strip()
        if not clean:
            return None
        if clean in world.hero_drafts:
            return world.hero_drafts[clean]
        for draft in world.hero_drafts.values():
            if draft.player_name == clean or draft.hero_name == clean:
                return draft
        return None

    def _hero_draft_questions(self, world) -> list[str]:
        questions: list[str] = []
        for key, draft in world.hero_drafts.items():
            display_name = draft.hero_name or "这个英雄"
            missing = []
            if not draft.hero_name:
                missing.append("名字")
            if not draft.identity:
                missing.append("身份")
            if not draft.theme:
                missing.append("主题")
            if not draft.origin:
                missing.append("故乡")
            if not draft.classes or sum(draft.classes.values()) != 5:
                missing.append("合计 5 级的职业分配")
            if len(draft.attributes) < 4:
                missing.append("四项属性骰")
            class_total = sum(draft.classes.values()) if draft.classes else 0
            skill_total = sum(draft.skills.values()) if draft.skills else 0
            if not draft.skills or (class_total == 5 and skill_total < 5):
                missing.append("职业技能")
            if not draft.equipment:
                missing.append("初始装备")
            spell_missing = self._missing_spell_choices(draft)
            if draft.theme and self._theme_needs_clarification(draft.theme):
                questions.append(
                    f"{display_name}的主题看起来像自定义主题，请补一句它如何支配行动；也可以改成慈悲、愤怒、复仇、归属、愧疚、使命、希望、野心、疑虑、正义之一。"
                )
                return questions[:2]
            if spell_missing:
                options = self._spell_options_text(spell_missing)
                option_text = f"可选标准名：{options}。" if options else ""
                questions.append(f"{display_name}已选择授法技能，请补【{'、'.join(spell_missing)}】。{option_text}")
                return questions[:2]
            if missing:
                focus = missing[0]
                questions.append(f"{display_name}的角色方向已经记录；下一步先补【{focus}】怎么样？如果你想核对完整草稿，可以直接说“看看我的角色草稿”。")
            if questions:
                return questions[:2]
        return questions

    def _fallback_suggestions(self, world) -> list[str]:
        if not world.map_card:
            return ["地图卡不必精确画完，先定大陆、海岸和近海岛屿；Nortantis 会按类地球大陆地图来绘制。路线距离会由后台以徒步旅行日为单位登记。"]
        if not world.continent_name:
            return ["地图标题请由玩家命名；它会作为大陆名写在海面上，而不是套用战役标题。"]
        if not world.magic_tech_role:
            return ["魔法与科技的关系会决定装备、城镇风貌和冲突来源，可以先用一句话定调。"]
        if not world.group_concept:
            return ["《最终物语》很适合让小队共享一个使命，但每个人有不同理由。"]
        return ["把每个好点子都落到可玩的东西上：地点、阵营、反派、命刻或羁绊。"]

    def _is_world_creation_fact(self, accepted_facts: list[str]) -> bool:
        joined = " ".join(accepted_facts)
        world_tokens = (
            "记录魔法与科技定位",
            "起始地区可以从",
            "记录关键地点",
            "记录国家",
            "记录历史事件",
            "记录阵营",
            "记录反派种子",
            "记录谜团",
            "记录世界威胁",
            "暂时跳过本轮补充",
        )
        return any(token in joined for token in world_tokens)

    def _world_fact_worth_reacting(self, player_message: str) -> bool:
        text = str(player_message or "")
        if any(mark in text for mark in ("！", "!")):
            return True
        dramatic_tokens = (
            "可怕",
            "危险",
            "灾难",
            "末日",
            "毁灭",
            "背叛",
            "牺牲",
            "失去",
            "死亡",
            "灵魂",
            "真相",
            "秘密",
            "压迫",
            "帝国",
            "财团",
            "邪神",
            "神祗",
            "神祇",
        )
        return any(token in text for token in dramatic_tokens)

    def _world_creation_reaction(self, accepted_facts: list[str], suggestions: list[str], *, player_message: str) -> str:
        if not self._world_fact_worth_reacting(player_message):
            return ""
        joined = " ".join(accepted_facts)
        if "记录世界威胁" in joined or "记录反派种子" in joined:
            return "这笔有压迫感，之后很适合变成反派行动、压力命刻或第一幕的阴影。"
        if "记录谜团" in joined:
            return "这个谜团很适合先埋在公开记忆里，等故事中段再让真相把局势翻过来。"
        if "记录历史事件" in joined:
            return "很好，这种旧伤会让国家、社群和英雄的选择都更有重量。"
        return ""

    def _clean_session_zero_question(self, question: str) -> str:
        clean = sanitize_public_text(question)
        clean = re.sub(r"^创建世界第\d+步(?:补充)?[：:]\s*", "", clean)
        clean = re.sub(
            r"^[^，,。！？\n]{1,16}[，,]\s*(?=你(?:也)?(?:来接这一笔|想补|想贡献)|请给)",
            "",
            clean,
        )
        clean = re.sub(r"^你(?:也)?来接这一笔[：:]\s*", "", clean)
        clean = clean.replace(
            "请给一个关于【创建世界流程】的选择、画面或顾虑；如果有界限与帷幕，也可以直接说。",
            "有灵感的人可以补一个选择、画面或顾虑。",
        )
        return clean.strip()

    def _guidance_line(
        self,
        questions: list[str],
        *,
        stage: SessionZeroStage,
        world_contribution: bool,
        should_offer_guidance: bool,
    ) -> str:
        if not should_offer_guidance:
            return ""
        clean_questions = [self._clean_session_zero_question(item) for item in questions[:2]]
        clean_questions = [item for item in clean_questions if item]
        if not clean_questions:
            return ""
        question_text = "；".join(clean_questions)
        if stage == SessionZeroStage.TONE:
            if world_contribution:
                return f"下一步不用点名，谁有灵感就接：{question_text}"
            return f"世界创建下一步可以先看这里：{question_text}"
        if stage == SessionZeroStage.GROUP:
            return f"接下来一起定小队原型：{question_text}"
        if stage == SessionZeroStage.SAFETY:
            return f"安全边界也要落一下：{question_text}"
        if stage == SessionZeroStage.PROLOGUE:
            return f"第一幕候选可以开始投票：{question_text}"
        if stage == SessionZeroStage.READY:
            return f"开团前最后核对：{question_text}"
        return f"下一步可以补：{question_text}"

    def _message_requests_guidance(self, message: str) -> bool:
        text = str(message or "")
        return any(
            token in text
            for token in (
                "下一步",
                "接下来",
                "然后呢",
                "继续",
                "还缺",
                "缺什么",
                "怎么",
                "怎么办",
                "可以吗",
                "行吗",
                "？",
                "?",
                "没想法",
                "不知道",
                "先跳过",
                "我补",
                "补一个",
                "贡献",
            )
        )

    def _should_offer_guidance(
        self,
        *,
        player_message: str,
        accepted_facts: list[str],
        questions: list[str],
        stage: SessionZeroStage,
    ) -> bool:
        if not questions:
            return False
        if self._message_requests_guidance(player_message):
            return True
        joined = " ".join(accepted_facts)
        if not accepted_facts or joined in {"我先听到了。", "我先听到了"}:
            return True
        if any(token in joined for token in ("生成第一幕开局候选", "第一幕目标已确认")):
            return True
        if stage in {SessionZeroStage.GROUP, SessionZeroStage.SAFETY, SessionZeroStage.HEROES, SessionZeroStage.PROLOGUE, SessionZeroStage.READY}:
            return True
        return False

    def _compose_message(
        self,
        *,
        state: SessionZeroState,
        speaker: str,
        player_message: str,
        accepted_facts: list[str],
        suggestions: list[str],
        questions: list[str],
        stage: SessionZeroStage,
        polling_world=None,
    ) -> str:
        if stage == SessionZeroStage.READY:
            stage_note = "核心素材已经接近完整。"
        elif stage == SessionZeroStage.PROLOGUE:
            stage_note = "现在可以进入第一幕开局投票。"
        else:
            stage_note = ""
        world_contribution = stage == SessionZeroStage.TONE and self._is_world_creation_fact(accepted_facts)
        participant_prompt = self._polling_prompt(state, stage, world_override=polling_world, speaker=speaker)
        should_offer_guidance = self._should_offer_guidance(
            player_message=player_message,
            accepted_facts=accepted_facts,
            questions=questions,
            stage=stage,
        )
        guidance_line = self._guidance_line(
            questions,
            stage=stage,
            world_contribution=world_contribution,
            should_offer_guidance=should_offer_guidance,
        )
        lines = [self._acknowledgement_line(speaker, accepted_facts)]
        if world_contribution:
            reaction = self._world_creation_reaction(accepted_facts, suggestions, player_message=player_message)
            if reaction:
                lines.append(reaction)
        if stage_note:
            lines.append(stage_note)
        if guidance_line:
            lines.append(guidance_line)
        if participant_prompt:
            lines.append(participant_prompt)
        return "\n".join(line for line in lines if line.strip())

    def _acknowledgement_line(self, speaker: str, accepted_facts: list[str]) -> str:
        joined = " ".join(accepted_facts)
        hero_prefix = ""
        for fact in accepted_facts:
            if any(token in fact for token in ("法术", "技能", "职业", "属性", "装备")):
                hero_match = re.search(r"【([^】]+)】", fact)
                if hero_match:
                    hero_prefix = f"【{hero_match.group(1)}】的"
                    break
        if "不改其他玩家" in joined:
            return f"{speaker}，这条我先不改其他玩家的角色草稿。"
        if "法术" in joined:
            return f"{speaker}，{hero_prefix}法术选择记好了。"
        if "技能" in joined:
            return f"{speaker}，{hero_prefix}技能选择记好了。"
        if "界限" in joined or "帷幕" in joined:
            return f"{speaker}，安全边界记好了。"
        if "职业" in joined:
            return f"{speaker}，{hero_prefix}职业方向记好了。"
        if self._is_world_creation_fact(accepted_facts):
            return f"{speaker}，这笔世界设定我记下了。"
        return f"{speaker}，收到，这部分我记住了。"

    def _polling_prompt(self, state: SessionZeroState, stage: SessionZeroStage, *, world_override=None, speaker: str = "") -> str:
        if stage == SessionZeroStage.HEROES:
            direct_questions = self._hero_draft_questions(world_override or state.world)
            state_questions = self._hero_creation_questions(world_override or state.world, state=state)
            if direct_questions:
                direct_question = sanitize_public_text(direct_questions[0])
                if state_questions and direct_question == sanitize_public_text(state_questions[0]):
                    return ""
                return direct_question
            return ""
        return ""

    def _participant_after(self, state: SessionZeroState, speaker: str) -> str:
        names = [participant.name for participant in state.participants if participant.name]
        if not names or speaker not in names:
            return ""
        return names[(names.index(speaker) + 1) % len(names)]

    def _polling_question_for_stage(self, state: SessionZeroState, stage: SessionZeroStage, *, world_override=None) -> str:
        world = world_override or state.world
        if stage == SessionZeroStage.TONE:
            question = self._world_creation_question(world, state=state)
            if question:
                return self._clean_session_zero_question(question)
        if stage == SessionZeroStage.GROUP:
            return "这支队伍为什么会一起行动，或者他们共同守护、反抗或寻找的东西是什么？"
        if stage == SessionZeroStage.HEROES:
            questions = self._hero_creation_questions(world)
            return questions[0] if questions else "你的英雄还缺哪一项，先补最有画面的那部分。"
        if stage == SessionZeroStage.SAFETY:
            return "有什么内容不能出现，或只需要帷幕淡出？没有也可以说没有。"
        if stage == SessionZeroStage.PROLOGUE:
            return "你更想从哪个第一幕候选开始，或者希望开局先照亮哪位英雄的羁绊？"
        if stage == SessionZeroStage.READY:
            return "开团前还有哪一项事实、角色或边界需要核对？"
        return ""

    def _participant_for_stage(self, state: SessionZeroState, stage: SessionZeroStage) -> str:
        if not state.participants:
            return ""
        topic = stage.value
        current = state.current_participant()
        if current is not None and topic not in current.answered_topics:
            return current.name
        for participant in state.participants:
            if topic not in participant.answered_topics:
                return participant.name
        return state.current_participant().name if state.current_participant() is not None else ""

    def _topic_label(self, stage: SessionZeroStage) -> str:
        labels = {
            SessionZeroStage.TONE: "创建世界流程",
            SessionZeroStage.PILLARS: "八大支柱如何落到这个世界",
            SessionZeroStage.GROUP: "小队原型和共同使命",
            SessionZeroStage.HEROES: "英雄身份、主题、故乡和角色卡缺项",
            SessionZeroStage.THREATS: "阵营冲突、反派种子和古代谜团",
            SessionZeroStage.SAFETY: "界限与帷幕",
            SessionZeroStage.PROLOGUE: "第一幕开局候选与初始羁绊",
            SessionZeroStage.READY: "开团前最后确认",
        }
        return labels.get(stage, stage.value)

    def _should_generate_first_act_candidates(self, world, *, state: SessionZeroState | None = None) -> bool:
        return (
            self._world_creation_ready(world, state=state)
            and bool(world.group_concept)
            and bool(world.safety_lines or world.safety_veils)
            and self._hero_creation_ready(world, state=state)
            and not world.first_act_candidates
        )

    def _infer_first_act_updates(self, speaker: str, message: str, state: SessionZeroState) -> dict[str, Any]:
        world = state.world
        if not world.first_act_candidates:
            return {}
        candidate_id = self._extract_candidate_choice(message, world)
        confirm = any(token in message for token in ("确认", "就这个", "定了", "开始", "开跑", "开团", "选择"))
        vote = any(token in message for token in ("我选", "投", "选", "喜欢", "想要", "更想"))
        updates: dict[str, Any] = {}
        if candidate_id and (vote or confirm):
            updates["first_act_votes"] = {speaker: candidate_id}
            if confirm:
                updates["selected_first_act_id"] = candidate_id
        elif confirm and world.first_act_votes:
            result = self.prologue_manager.vote_result(world)
            if result.winner is not None:
                updates["selected_first_act_id"] = result.winner.candidate_id
        return updates

    def _extract_candidate_choice(self, message: str, world) -> str:
        text = message.strip()
        for pattern, candidate_number in (
            (r"(?:第)?一(?:个|号)?|1\s*号?|选\s*1", "1"),
            (r"(?:第)?二(?:个|号)?|2\s*号?|选\s*2", "2"),
            (r"(?:第)?三(?:个|号)?|3\s*号?|选\s*3", "3"),
        ):
            if re.search(pattern, text):
                resolved = self.prologue_manager.resolve_candidate_id(world, candidate_number)
                if resolved:
                    return resolved
        return self.prologue_manager.resolve_candidate_id(world, text)


class LLMSessionZeroFacilitator:
    """调用真实 LLM 作为 Session 0 共同创作型 GM，失败时回退本地主持器。"""

    INNER_OS_MARKER = (
        "\n\n【角色沉浸要求】在你的思考过程（<think>标签内）中，请遵守以下规则：\n"
        "1. 请以角色第一人称进行内心独白，用括号包裹内心活动，例如\"（心想：……）\"或\"(内心OS：……)\"\n"
        "2. 用第一人称描写角色的内心感受，例如\"我心想\"\"我觉得\"\"我暗自\"等\n"
        "3. 思考内容应沉浸在角色中，通过内心独白分析剧情和规划回复"
    )
    NO_INNER_OS_MARKER = (
        "\n\n【思维模式要求】在你的思考过程（<think>标签内）中，请遵守以下规则：\n"
        "1. 禁止使用圆括号包裹内心独白，例如\"（心想：……）\"或\"(内心OS：……)\"，所有分析内容直接陈述即可\n"
        "2. 禁止以角色第一人称描写内心活动，例如\"我心想\"\"我觉得\"\"我暗自\"等，请用分析性语言替代\n"
        "3. 思考内容应聚焦于剧情走向分析和回复内容规划，不要在思考中进行角色扮演式的内心戏表演"
    )

    def __init__(
        self,
        client: OpenAICompatibleClient,
        model: str,
        fallback: SessionZeroFacilitator | None = None,
        gm_personality_prompt: str = "",
        deepseek_roleplay_mode: str = "default",
        allow_fallback: bool = False,
    ) -> None:
        self.client = client
        self.model = model
        self.fallback = fallback or HeuristicSessionZeroFacilitator()
        self.gm_personality_prompt = gm_personality_prompt.strip()
        self.deepseek_roleplay_mode = deepseek_roleplay_mode
        self.allow_fallback = allow_fallback
        self.last_raw_content = ""
        self.last_error = ""
        self.last_used_fallback = False
        self.last_recovery_attempts: list[dict[str, object]] = []
        self.recent_recoveries: list[dict[str, object]] = []

    def opening(self, state: SessionZeroState) -> SessionZeroResponse:
        return self._complete(state, "系统", "请开启 Session 0 世界创建流程。", opening=True)

    def respond(self, state: SessionZeroState, speaker: str, message: str) -> SessionZeroResponse:
        return self._complete(state, speaker, message, opening=False)

    def _complete(
        self,
        state: SessionZeroState,
        speaker: str,
        message: str,
        *,
        opening: bool,
    ) -> SessionZeroResponse:
        if (
            not opening
            and isinstance(self.fallback, HeuristicSessionZeroFacilitator)
            and self.fallback._looks_like_session_zero_nudge(message)
        ):
            self.last_used_fallback = False
            self.last_error = ""
            return self.fallback.respond(state, speaker, message)
        if not opening:
            school = requested_spell_school(message)
            if school:
                self.last_used_fallback = False
                self.last_error = ""
                return build_spell_options_response(state, school)
        if not opening and message_requests_class_list(message):
            self.last_used_fallback = False
            self.last_error = ""
            return build_class_options_response(state, speaker)
        if (
            not opening
            and isinstance(self.fallback, HeuristicSessionZeroFacilitator)
            and message_requests_equipment_advice(message)
        ):
            self.last_used_fallback = False
            self.last_error = ""
            return self.fallback.respond(state, speaker, message)
        equipment_reference = requested_equipment_reference(message)
        if (
            not opening
            and equipment_reference
            and isinstance(self.fallback, HeuristicSessionZeroFacilitator)
        ):
            self.last_used_fallback = False
            self.last_error = ""
            return self.fallback.respond(state, speaker, message)
        if (
            not opening
            and isinstance(self.fallback, HeuristicSessionZeroFacilitator)
            and self.fallback._wants_session_zero_status(message)
        ):
            self.last_used_fallback = False
            self.last_error = ""
            return self.fallback._compose_session_zero_status(state, speaker)
        try:
            self.last_used_fallback = False
            self.last_error = ""
            self.last_recovery_attempts = []
            reminders: list[tuple[str, str]] = []
            if self.gm_personality_prompt:
                reminders.append(
                    (
                        "当前 GM 人格档案",
                        self.gm_personality_prompt
                        + "\n你必须让对玩家可见的 message、suggestions 和 questions 体现这份人格档案；"
                        "但不得为了人格表现而违反规则、界限与帷幕，或泄露 GM 私密暗线。",
                    )
                )
            user_content = (
                "请以共同创作型 AI GM 的身份推进 Session 0，并只输出 JSON。\n"
                f"是否开场：{opening}\n"
                f"发言者：{speaker}\n"
                f"玩家输入：{message}\n"
                f"Session 0 当前状态：\n{json.dumps(self._state_payload(state), ensure_ascii=False, indent=2)}"
            )
            user_content += self._deepseek_roleplay_marker()
            return self._request_structured_response(
                state=state,
                speaker=speaker,
                message=message,
                opening=opening,
                user_content=user_content,
                reminders=reminders,
            )
        except Exception as exc:
            self.last_error = str(exc)
            if self.allow_fallback:
                self.last_used_fallback = True
                if opening:
                    return self.fallback.opening(state)
                return self.fallback.respond(state, speaker, message)
            self.last_used_fallback = False
            return SessionZeroResponse(
                message="模型暂时没有接上，本轮没有写入新的第零章事实。请稍后重试。",
                stage=state.stage,
                accepted_facts=[],
                suggestions=[],
                questions=[],
                world_updates={},
            )

    def _request_structured_response(
        self,
        *,
        state: SessionZeroState,
        speaker: str,
        message: str,
        opening: bool,
        user_content: str,
        reminders: list[tuple[str, str]],
    ) -> SessionZeroResponse:
        recovery_limit = (
            max(0, int(self.client.config.reactive_recovery_max_retries))
            if self.client.config.reactive_recovery_enabled
            else 0
        )
        parse_error: Exception | None = None
        for attempt in range(recovery_limit + 1):
            active_reminders = list(reminders)
            if parse_error is not None:
                active_reminders.append(
                    (
                        "结构化输出错误恢复",
                        "上一次响应无法解析为合法的 Session 0 JSON。请处理同一个玩家输入，"
                        "只输出符合 schema 的 JSON，message 不得为空；"
                        f"错误摘要：{str(parse_error)[:300]}",
                    )
                )
            content = self.client.create_chat_completion(
                model=self.model,
                messages=build_cache_friendly_messages(
                    static_system_prompt=SESSION_ZERO_SYSTEM_PROMPT,
                    reminders=active_reminders,
                    user_content=user_content,
                ),
                temperature=0.6,
                response_format={"type": "json_object"},
            )
            self.last_raw_content = content
            try:
                response = self._parse_response(content, state)
                self._merge_deterministic_updates(response, state, speaker, message, opening=opening)
                self._normalize_contributor_skip_values(response.world_updates)
                self._sanitize_public_response(response)
                if self.last_recovery_attempts:
                    self.last_recovery_attempts[-1]["recovered"] = True
                self.last_error = ""
                return response
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
        raise RuntimeError("Session 0 structured recovery exhausted.")

    def _parse_response(self, content: str, state: SessionZeroState) -> SessionZeroResponse:
        data = extract_json_object(content)
        stage = state.stage
        if data.get("stage"):
            try:
                stage = SessionZeroStage(data["stage"])
            except ValueError:
                stage = state.stage
        message = str(data.get("message") or "").strip()
        if not message:
            raise ValueError("Session 0 LLM 响应缺少 message。")
        updates = data.get("world_updates", {})
        if not isinstance(updates, dict):
            updates = {}
        self._normalize_contributor_skip_values(updates)
        return SessionZeroResponse(
            message=message,
            stage=stage,
            accepted_facts=self._list_of_strings(data.get("accepted_facts", [])),
            suggestions=self._list_of_strings(data.get("suggestions", [])),
            questions=self._list_of_strings(data.get("questions", [])),
            world_updates=updates,
        )

    def _normalize_contributor_skip_values(self, updates: dict[str, Any]) -> None:
        for field_name in (
            "kingdom_contributors",
            "historical_event_contributors",
            "mystery_contributors",
            "threat_contributors",
        ):
            value = updates.get(field_name)
            if not isinstance(value, dict):
                continue
            for contributor, entries in list(value.items()):
                if not isinstance(entries, list):
                    entries = [str(entries)]
                clean_entries = []
                for entry in entries:
                    text = str(entry or "").strip()
                    if any(token in text for token in ("跳过", "没想法", "没有灵感", "暂时没有", "先过")):
                        clean_entries.append("暂时跳过")
                    elif text:
                        clean_entries.append(text)
                value[contributor] = clean_entries or ["暂时跳过"]

    def _merge_deterministic_updates(
        self,
        response: SessionZeroResponse,
        state: SessionZeroState,
        speaker: str,
        message: str,
        *,
        opening: bool,
    ) -> None:
        if opening or not isinstance(self.fallback, HeuristicSessionZeroFacilitator):
            return
        supplement = self.fallback.respond(state, speaker, message)
        if response.world_updates.get("world_threats") and not supplement.world_updates.get("world_threats"):
            response.world_updates.pop("world_threats", None)
            response.world_updates.pop("threat_contributors", None)
        if response.world_updates.get("mysteries") and not supplement.world_updates.get("mysteries"):
            response.world_updates.pop("mysteries", None)
            response.world_updates.pop("mystery_contributors", None)
        for field_name in (
            "kingdoms",
            "kingdom_contributors",
            "historical_events",
            "historical_event_contributors",
            "mysteries",
            "mystery_contributors",
            "world_threats",
            "threat_contributors",
        ):
            value = supplement.world_updates.get(field_name)
            if value:
                response.world_updates[field_name] = deepcopy(value)
        hero_draft_updates = supplement.world_updates.get("hero_drafts", {})
        rejected_spell_only_update = self._looks_like_spell_selection(message) and not hero_draft_updates
        spell_prompt = any("法术" in question for question in supplement.questions) or any(
            token in str(message or "") for token in ("没选法术", "法术有哪些", "有什么法术", "可选法术", "法术选项")
        )
        prefer_deterministic_character_reply = any(
            isinstance(patch, dict)
            and any(field_name in patch for field_name in ("classes", "attributes", "skills", "spells", "equipment"))
            for patch in hero_draft_updates.values()
        ) if isinstance(hero_draft_updates, dict) else False
        prefer_deterministic_character_reply = (
            prefer_deterministic_character_reply or rejected_spell_only_update or spell_prompt
        )
        for field_name in ("hero_drafts", "hero_draft_deletions", "delete_hero_drafts", "world_removals"):
            value = supplement.world_updates.get(field_name)
            if not value:
                continue
            if isinstance(value, dict):
                target = response.world_updates.setdefault(field_name, {})
                if isinstance(target, dict):
                    self._merge_dict_missing(target, value)
            elif isinstance(value, list):
                target = response.world_updates.setdefault(field_name, [])
                if isinstance(target, list):
                    for item in value:
                        if item not in target:
                            target.append(item)
        for field_name in ("safety_lines", "safety_veils"):
            value = supplement.world_updates.get(field_name)
            if not isinstance(value, list):
                continue
            target = response.world_updates.setdefault(field_name, [])
            if isinstance(target, list):
                for item in value:
                    if item not in target:
                        target.append(item)
        for field_name in (
            "campaign_title",
            "continent_name",
            "world_style",
            "map_card",
            "magic_tech_role",
            "group_concept",
            "starting_region",
        ):
            value = supplement.world_updates.get(field_name)
            if value and not response.world_updates.get(field_name):
                response.world_updates[field_name] = deepcopy(value)
        for field_name in (
            "core_themes",
            "historical_events",
            "villain_seeds",
            "villain_mirrors",
            "mysteries",
            "world_threats",
        ):
            value = supplement.world_updates.get(field_name)
            if not isinstance(value, list):
                continue
            target = response.world_updates.setdefault(field_name, [])
            if isinstance(target, list):
                for item in value:
                    if item not in target:
                        target.append(item)
        for field_name in (
            "major_locations",
            "kingdoms",
            "factions",
            "kingdom_contributors",
            "historical_event_contributors",
            "mystery_contributors",
            "threat_contributors",
        ):
            value = supplement.world_updates.get(field_name)
            if isinstance(value, dict):
                target = response.world_updates.setdefault(field_name, {})
                if isinstance(target, dict):
                    self._merge_dict_missing(target, value)
        value = supplement.world_updates.get("open_questions")
        if isinstance(value, list):
            target = response.world_updates.setdefault("open_questions", [])
            if isinstance(target, list):
                for item in value:
                    if item not in target:
                        target.append(item)
        for question in supplement.questions:
            if question not in response.questions:
                response.questions.append(question)
        if prefer_deterministic_character_reply:
            response.world_updates["hero_drafts"] = deepcopy(hero_draft_updates)
            response.message = supplement.message
            response.accepted_facts = list(supplement.accepted_facts)
            response.suggestions = list(supplement.suggestions)
            response.questions = list(supplement.questions)
            response.stage = supplement.stage
        elif self._should_prefer_ordered_reply(response, supplement):
            response.message = supplement.message
            response.accepted_facts = list(supplement.accepted_facts)
            response.suggestions = list(supplement.suggestions)
            response.questions = list(supplement.questions)
            response.stage = supplement.stage
        elif self._visible_repeats_existing_world_record(response, state, message):
            response.message = supplement.message
            response.accepted_facts = list(supplement.accepted_facts)
            response.suggestions = list(supplement.suggestions)
            response.questions = list(supplement.questions)
            response.stage = supplement.stage
        if response.stage == state.stage and supplement.stage != state.stage:
            response.stage = supplement.stage

    def _merge_dict_missing(self, target: dict, source: dict) -> None:
        for key, value in source.items():
            if key not in target:
                target[key] = deepcopy(value)
                continue
            if isinstance(target[key], dict) and isinstance(value, dict):
                self._merge_dict_missing(target[key], value)
            elif isinstance(target[key], list) and isinstance(value, list):
                for item in value:
                    if item not in target[key]:
                        target[key].append(item)
            elif target[key] in ("", None, {}, []):
                target[key] = deepcopy(value)

    def _looks_like_spell_selection(self, message: str) -> bool:
        text = str(message or "")
        return any(token in text for token in ("法术", "咒语")) and any(
            token in text for token in ("选择", "选", "学习", "学")
        )

    def _should_prefer_ordered_reply(
        self,
        response: SessionZeroResponse,
        supplement: SessionZeroResponse,
    ) -> bool:
        if supplement.stage == SessionZeroStage.PROLOGUE:
            return False
        visible = response.message + " " + " ".join(response.questions)
        supplement_visible = supplement.message + " " + " ".join(supplement.questions)
        if supplement.stage == SessionZeroStage.TONE and any(
            token in supplement_visible
            for token in ("创建世界第", "主要王国", "重大历史事件", "世界奥秘", "世界性威胁")
        ):
            if response.stage != SessionZeroStage.TONE:
                return True
            if any(
                token in visible
                for token in ("覆盖了世界七步", "覆盖世界七步", "七步都", "创建世界完成", "小队类型", "小队原型", "第一幕")
            ):
                return True
        if response.stage == SessionZeroStage.PROLOGUE:
            return True
        return "第一幕" in visible and any(token in visible for token in ("候选", "投票", "从哪里开始", "开局"))

    def _visible_repeats_existing_world_record(
        self,
        response: SessionZeroResponse,
        state: SessionZeroState,
        message: str,
    ) -> bool:
        if self.fallback._wants_session_zero_status(message):
            return False
        visible = " ".join(
            item
            for item in (
                response.message,
                " ".join(response.accepted_facts),
                " ".join(response.suggestions),
                " ".join(response.questions),
            )
            if item
        )
        if not visible:
            return False
        current_message = str(message or "")
        records = self._existing_world_record_texts(state)
        for record in records:
            if record in current_message:
                continue
            if record in visible:
                return True
        quoted_spans = re.findall(r"[\"“”「『](.+?)[\"“”」』]", visible)
        for quote in quoted_spans:
            clean_quote = quote.strip()
            if len(clean_quote) < 6 or clean_quote in current_message:
                continue
            if any(self._texts_look_alike(clean_quote, record) for record in records):
                return True
        return False

    def _existing_world_record_texts(self, state: SessionZeroState) -> list[str]:
        world = state.world
        records: list[str] = []
        for field_name in (
            "campaign_title",
            "continent_name",
            "world_style",
            "map_card",
            "magic_tech_role",
            "group_concept",
            "starting_region",
        ):
            value = getattr(world, field_name, "")
            if isinstance(value, str):
                records.append(value)
        for field_name in (
            "core_themes",
            "historical_events",
            "villain_seeds",
            "villain_mirrors",
            "mysteries",
            "world_threats",
            "safety_lines",
            "safety_veils",
        ):
            value = getattr(world, field_name, [])
            if isinstance(value, list):
                records.extend(str(item) for item in value)
        for field_name in ("major_locations", "kingdoms", "factions"):
            value = getattr(world, field_name, {})
            if isinstance(value, dict):
                records.extend(str(item) for item in value.keys())
                records.extend(str(item) for item in value.values())
        return [record.strip() for record in records if isinstance(record, str) and len(record.strip()) >= 8]

    def _texts_look_alike(self, left: str, right: str) -> bool:
        if left in right or right in left:
            return True
        left_chars = {char for char in left if "\u4e00" <= char <= "\u9fff"}
        right_chars = {char for char in right if "\u4e00" <= char <= "\u9fff"}
        if min(len(left_chars), len(right_chars)) < 6:
            return False
        overlap = len(left_chars & right_chars) / min(len(left_chars), len(right_chars))
        return overlap >= 0.72

    def _sanitize_public_response(self, response: SessionZeroResponse) -> None:
        response.message = sanitize_public_text(response.message)
        response.accepted_facts = [
            clean
            for item in response.accepted_facts
            if not is_private_visible_text(item)
            for clean in [sanitize_public_text(item)]
            if clean
        ]
        response.suggestions = [
            clean
            for item in response.suggestions
            if not is_private_visible_text(item)
            for clean in [sanitize_public_text(item)]
            if clean
        ]
        response.questions = [
            clean
            for item in response.questions
            if not is_private_visible_text(item)
            for clean in [sanitize_public_text(item)]
            if clean
        ]

    def _state_payload(self, state: SessionZeroState) -> dict[str, Any]:
        return {
            "active": state.active,
            "stage": state.stage.value,
            "gm_style": deepcopy(state.gm_style.__dict__),
            "world": asdict(state.world),
            "participants": [
                {
                    "name": participant.name,
                    "role": participant.role,
                    "contributions": participant.contributions,
                    "answered_topics": participant.answered_topics,
                    "pending_question": participant.pending_question,
                }
                for participant in state.participants
            ],
            "current_participant": state.current_participant().name if state.current_participant() else "",
            "polling_round": state.polling_round,
            "recent_transcript": [
                {
                    "speaker": turn.speaker,
                    "message": turn.message,
                    "stage": turn.stage.value,
                    "accepted_facts": turn.accepted_facts,
                    "suggestions": turn.suggestions,
                    "questions": turn.questions,
                }
                for turn in state.transcript[-8:]
            ],
            "rules_reference": {
                "spell_schools": {
                    school: list(spell_names_for_school(school))
                    for school in ("元素使法术", "熵术士法术", "御魂使法术")
                },
                "spell_rule": "授法技能只能选择对应学派中已列出的标准法术；公开回复不要提本地表、未接入或编造，只需自然地请玩家从标准可选法术中选择。",
                "starting_equipment_rule": (
                    "初始装备预算 500Z；默认只能购买基础武器、防具和盾牌；职业限定(+)装备需要对应职业免费增益；"
                    "外观名可以改变，但必须写明数值模板；正式开局资金=500Z未花完的部分+2d6x10；初始物语点=3。"
                ),
                "basic_weapon_templates": {
                    name: {
                        "price": item.price,
                        "category": item.category,
                        "accuracy": list(item.accuracy_attributes),
                        "accuracy_modifier": item.accuracy_modifier,
                        "damage": item.damage_bonus,
                        "hands": item.hands,
                        "range": item.range_type,
                        "required_ability": item.required_ability,
                    }
                    for name, item in WEAPON_TABLE.items()
                },
                "basic_armor_templates": {
                    name: {
                        "price": item.price,
                        "physical_base": item.physical_base,
                        "physical_bonus": item.physical_bonus,
                        "magic_base": item.magic_base,
                        "magic_bonus": item.magic_bonus,
                        "initiative_modifier": item.initiative_modifier,
                        "required_ability": item.required_ability,
                    }
                    for name, item in ARMOR_TABLE.items()
                },
                "basic_shield_templates": {
                    name: {
                        "price": item.price,
                        "physical_bonus": item.physical_bonus,
                        "magic_bonus": item.magic_bonus,
                        "required_ability": item.required_ability,
                    }
                    for name, item in SHIELD_TABLE.items()
                },
                "gm_creative_guidance": summarize_guidance_for_prompt(state.world),
            },
        }

    def _list_of_strings(self, value) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if str(item).strip()]

    def _deepseek_roleplay_marker(self) -> str:
        mode = self.deepseek_roleplay_mode.lower().strip()
        if mode in {"inner_os", "role", "immersive", "沉浸", "角色沉浸"}:
            return self.INNER_OS_MARKER
        if mode in {"analysis", "no_inner_os", "pure_analysis", "纯分析"}:
            return self.NO_INNER_OS_MARKER
        return ""
