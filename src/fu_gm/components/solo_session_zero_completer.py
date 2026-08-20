from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from fu_gm.llm_utils import extract_json_object
from fu_gm.prompt_cache import build_cache_friendly_messages


SOLO_SESSION_ZERO_COMPLETION_PROMPT = (
    "你是FU-GM的单人团第零章创作侧链。玩家已经明确授权GM补全尚未确定的创作内容，"
    "但玩家已经确定的事实绝不能覆盖或改写。你只补空白，不生成地图图片，也不宣称第一章已经开始。"
    "请让新增内容彼此相关，能支持一场英雄主义JRPG冒险；第一幕只准备局面、压力和问题，不写死解法。"
    "角色只需要叙事字段，机械数据由Python使用已验证模板补齐。"
    "只输出一个JSON对象，形状严格为："
    "{\"continent_name\":\"...\",\"world_shape\":\"...\","
    "\"magic_tech_role\":\"...\","
    "\"kingdom\":{\"name\":\"...\",\"description\":\"...\"},"
    "\"historical_event\":\"...\",\"mystery\":\"...\",\"world_threat\":\"...\","
    "\"group_concept\":\"...\",\"starting_region\":\"...\","
    "\"first_act_summary\":\"...\",\"tone_preference\":\"...\","
    "\"description_style\":\"...\","
    "\"supplemental_locations\":[{\"name\":\"...\",\"description\":\"...\","
    "\"feature_type\":\"settlement|forest|mountain_range|region|landmark\","
    "\"terrain\":\"...\",\"position_hint\":\"north|northeast|east|southeast|south|"
    "southwest|west|northwest|center\"}],"
    "\"hero\":{\"name\":\"...\",\"identity\":\"...\","
    "\"theme\":\"慈悲|愤怒|复仇|归属|愧疚|使命|希望|野心|疑虑|正义\","
    "\"origin\":\"...\"},"
    "\"opening_scene\":{\"scene_name\":\"...\",\"location\":\"...\","
    "\"objective\":\"...\",\"private_situation\":{"
    "\"premise\":\"...\",\"stakes\":\"...\",\"current_pressure\":\"...\","
    "\"dramatic_question\":\"...\",\"signature_image\":\"...\","
    "\"opposition_goal\":\"...\",\"dilemma\":\"...\","
    "\"closure_requirement\":\"...\",\"irreversible_change\":\"...\","
    "\"ending_echo\":\"...\",\"visible_elements\":[\"...\",\"...\"],"
    "\"clue_pool\":[\"...\",\"...\"],\"secrets\":[\"...\"],"
    "\"possible_reveals\":[\"...\",\"...\"],"
    "\"escalation_ladder\":[\"...\",\"...\"],"
    "\"possible_payoffs\":[\"...\",\"...\"]},"
    "\"public_opening\":\"...\",\"player_handoff\":\"...\"}}。"
    "supplemental_locations给出三处；名称不要是‘某王国’‘起始村庄’等占位符。"
    "opening_scene与first_act_summary必须描述同一个第一幕。public_opening只写英雄此刻"
    "能够感知的地点、人物和正在变化的局面，不泄露private_situation中的秘密，不解释"
    "设计意图，不替英雄行动；player_handoff只用一句自然的开放问题交还行动权。"
)


@dataclass(frozen=True)
class SoloSessionZeroCompletion:
    world_updates: dict[str, object]
    hero_story: dict[str, str]
    opening_scene: dict[str, object]
    used_model: bool = False
    model: str = ""
    error: str = ""


class SoloSessionZeroCompleter:
    """为玩家已明确委托的单人第零章生成一个小而可校验的补全包。"""

    _THEMES = {
        "慈悲",
        "愤怒",
        "复仇",
        "归属",
        "愧疚",
        "使命",
        "希望",
        "野心",
        "疑虑",
        "正义",
    }
    _FEATURE_TYPES = {
        "settlement",
        "forest",
        "mountain_range",
        "region",
        "landmark",
    }
    _POSITIONS = {
        "north",
        "northeast",
        "east",
        "southeast",
        "south",
        "southwest",
        "west",
        "northwest",
        "center",
    }
    _OPENING_SCALAR_FIELDS = {
        "premise",
        "stakes",
        "current_pressure",
        "dramatic_question",
        "signature_image",
        "opposition_goal",
        "dilemma",
        "closure_requirement",
        "irreversible_change",
        "ending_echo",
    }
    _OPENING_LIST_FIELDS = {
        "visible_elements",
        "clue_pool",
        "secrets",
        "possible_reveals",
        "escalation_ladder",
        "possible_payoffs",
    }

    def __init__(self, *, client: Any | None, model: str) -> None:
        self.client = client
        self.model = str(model or "").strip()

    def complete(
        self,
        *,
        current_world: dict[str, object],
        player_name: str,
        creative_direction: str,
        deadline: float | None = None,
    ) -> SoloSessionZeroCompletion:
        fallback = self._fallback(current_world, player_name)
        if self.client is None or not self.model:
            return fallback
        payload = {
            "player_name": self._clean(player_name),
            "creative_direction": self._clean(creative_direction),
            "confirmed_world_facts": current_world,
            "instruction": "只为仍为空白的字段提供候选补全；既有事实优先。",
        }
        try:
            raw = self.client.create_chat_completion(
                model=self.model,
                messages=build_cache_friendly_messages(
                    static_system_prompt=SOLO_SESSION_ZERO_COMPLETION_PROMPT,
                    user_content=json.dumps(payload, ensure_ascii=False, default=str),
                    cache_family="solo-session-zero-completer",
                ),
                temperature=0.65,
                response_format={"type": "json_object"},
                max_tokens=3200,
                deadline=deadline,
                operation="solo_session_zero_completion",
                thinking_enabled=False,
            )
            parsed = extract_json_object(raw)
            world_updates, hero_story, opening_scene = self._normalize(
                parsed,
                current_world,
            )
            if (
                not world_updates
                or not all(hero_story.values())
                or not opening_scene
            ):
                raise ValueError("创作侧链没有返回完整的补全包。")
            return SoloSessionZeroCompletion(
                world_updates=world_updates,
                hero_story=hero_story,
                opening_scene=opening_scene,
                used_model=True,
                model=self.model,
            )
        except Exception as exc:
            return SoloSessionZeroCompletion(
                world_updates=fallback.world_updates,
                hero_story=fallback.hero_story,
                opening_scene=fallback.opening_scene,
                used_model=False,
                model=self.model,
                error=str(exc)[:500],
            )

    def _normalize(
        self,
        value: object,
        current_world: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, str], dict[str, object]]:
        if not isinstance(value, dict):
            return {}, {}, {}
        continent = self._clean(value.get("continent_name")) or "岚火大陆"
        kingdom = value.get("kingdom")
        kingdom = kingdom if isinstance(kingdom, dict) else {}
        kingdom_name = self._clean(kingdom.get("name")) or "灶脊联邦"
        kingdom_description = self._clean(kingdom.get("description")) or (
            "横跨气候分界线的松散城邦联盟，以山口商路维系彼此。"
        )
        locations = self._normalized_locations(value.get("supplemental_locations"))
        if len(locations) < 3:
            locations = list(self._fallback(current_world, "").world_updates["map_locations"])
        starting_region = self._clean(value.get("starting_region"))
        if not starting_region:
            existing = self._existing_location_names(current_world)
            starting_region = existing[0] if existing else str(locations[0]["name"])
        first_act_summary = self._clean(value.get("first_act_summary")) or (
            f"第一幕从{starting_region}的地脉异响开始；"
            "一场正在扩大的灾变迫使英雄立即作出选择。"
        )
        world_updates: dict[str, object] = {
            "continent_name": continent,
            "world_shape": self._clean(value.get("world_shape")) or "边界清晰、地貌彼此咬合的圆形大陆",
            "map_card": "自定义地图",
            "magic_tech_role": self._clean(value.get("magic_tech_role"))
            or "魔法沿地脉与气候流动，工匠以晶炉和符文把它转化为日常技术。",
            "kingdoms": {kingdom_name: kingdom_description},
            "historical_events": [
                self._clean(value.get("historical_event"))
                or "数十年前的地脉震荡改变了大陆的气候边界，也切断了多条旧商路。"
            ],
            "mysteries": [
                self._clean(value.get("mystery"))
                or "中央山脉深处为何会传出与灵魂之河同频的轰鸣，至今无人知晓。"
            ],
            "world_threats": [
                self._clean(value.get("world_threat"))
                or "气候边界正在失控扩张，森林与荒漠都可能吞没彼此赖以生存的土地。"
            ],
            "group_concept": self._clean(value.get("group_concept"))
            or "追查地脉异变、保护沿途居民的独行英雄",
            "starting_region": starting_region,
            "selected_first_act_summary": first_act_summary,
            "tone_preferences": [
                self._clean(value.get("tone_preference")) or "明快的英雄冒险，危险真实但保留希望"
            ],
            "description_style": self._clean(value.get("description_style"))
            or "具体、有画面感的JRPG式演绎",
            "map_locations": locations,
        }
        hero = value.get("hero")
        hero = hero if isinstance(hero, dict) else {}
        theme = self._clean(hero.get("theme"))
        if theme not in self._THEMES:
            theme = "希望"
        hero_story = {
            "hero_name": self._clean(hero.get("name")) or "岚辛",
            "identity": self._clean(hero.get("identity")) or "追寻失落地脉歌谣的年轻旅者",
            "theme": theme,
            "origin": self._clean(hero.get("origin")) or starting_region,
        }
        opening_scene = self._normalized_opening_scene(
            value.get("opening_scene"),
            starting_region=starting_region,
            first_act_summary=first_act_summary,
            hero_name=hero_story["hero_name"],
        )
        return world_updates, hero_story, opening_scene

    def _fallback(
        self,
        current_world: dict[str, object],
        player_name: str,
    ) -> SoloSessionZeroCompletion:
        existing = self._existing_location_names(current_world)
        starting_region = existing[0] if existing else "椒风驿"
        continent = self._clean(current_world.get("continent_name")) or "岚火大陆"
        locations = [
            {
                "name": "椒风驿",
                "description": "位于西部林缘与中央山道交会处的温泉驿镇。",
                "feature_type": "settlement",
                "terrain": "森林",
                "position_hint": "west",
            },
            {
                "name": "赤盐商路",
                "description": "沿东部荒漠绿洲延伸的古老商道。",
                "feature_type": "region",
                "terrain": "沙漠",
                "position_hint": "east",
            },
            {
                "name": "沸星关",
                "description": "嵌在中央群山隘口中的要塞聚落，地底常传出沉闷轰鸣。",
                "feature_type": "settlement",
                "terrain": "高山",
                "position_hint": "center",
            },
        ]
        first_act_summary = (
            f"第一幕从{starting_region}的地脉异响开始；"
            "一场正在扩大的灾变迫使英雄立即作出选择。"
        )
        hero_name = "岚辛"
        return SoloSessionZeroCompletion(
            world_updates={
                "continent_name": continent,
                "world_shape": "边界清晰、地貌彼此咬合的圆形大陆",
                "map_card": "自定义地图",
                "magic_tech_role": "魔法沿地脉与气候流动，工匠以晶炉和符文把它转化为日常技术。",
                "kingdoms": {
                    "灶脊联邦": "横跨中央山口的松散城邦联盟，以商路和地热工坊维系彼此。"
                },
                "historical_events": [
                    "数十年前的地脉震荡改变了大陆的气候边界，也切断了多条旧商路。"
                ],
                "mysteries": [
                    "中央山脉深处为何会传出与灵魂之河同频的轰鸣，至今无人知晓。"
                ],
                "world_threats": [
                    "气候边界正在失控扩张，森林与荒漠都可能吞没彼此赖以生存的土地。"
                ],
                "group_concept": "追查地脉异变、保护沿途居民的独行英雄",
                "starting_region": starting_region,
                "selected_first_act_summary": first_act_summary,
                "tone_preferences": ["明快的英雄冒险，危险真实但保留希望"],
                "description_style": "具体、有画面感的JRPG式演绎",
                "map_locations": locations,
            },
            hero_story={
                "hero_name": hero_name,
                "identity": "追寻失落地脉歌谣的年轻旅者",
                "theme": "希望",
                "origin": starting_region,
            },
            opening_scene=self._fallback_opening_scene(
                starting_region=starting_region,
                first_act_summary=first_act_summary,
                hero_name=hero_name,
            ),
            used_model=False,
            model=self.model,
            error="",
        )

    def _normalized_opening_scene(
        self,
        value: object,
        *,
        starting_region: str,
        first_act_summary: str,
        hero_name: str,
    ) -> dict[str, object]:
        fallback = self._fallback_opening_scene(
            starting_region=starting_region,
            first_act_summary=first_act_summary,
            hero_name=hero_name,
        )
        if not isinstance(value, dict):
            return fallback
        private = value.get("private_situation")
        private = private if isinstance(private, dict) else {}
        normalized_private: dict[str, object] = {}
        for key in self._OPENING_SCALAR_FIELDS:
            text = self._clean(private.get(key))
            if text:
                normalized_private[key] = text
        for key in self._OPENING_LIST_FIELDS:
            raw = private.get(key)
            if not isinstance(raw, list):
                continue
            items = list(
                dict.fromkeys(
                    clean
                    for item in raw
                    if (clean := self._clean(item))
                )
            )[:8]
            if items:
                normalized_private[key] = items
        fallback_private = dict(fallback["private_situation"])
        for key, fallback_value in fallback_private.items():
            current = normalized_private.get(key)
            if isinstance(fallback_value, list):
                if len(list(current or [])) < len(fallback_value):
                    combined = list(current or []) + [
                        item
                        for item in fallback_value
                        if item not in list(current or [])
                    ]
                    normalized_private[key] = combined[:8]
            elif not self._clean(current):
                normalized_private[key] = fallback_value
        return {
            "scene_name": self._clean(value.get("scene_name"))
            or str(fallback["scene_name"]),
            # The confirmed starting region remains authoritative even if the
            # creative sidechain proposes a nearby but different place name.
            "location": starting_region,
            "objective": self._clean(value.get("objective"))
            or str(fallback["objective"]),
            "private_situation": normalized_private,
            "public_opening": self._clean(value.get("public_opening"))
            or str(fallback["public_opening"]),
            "player_handoff": self._clean(value.get("player_handoff"))
            or str(fallback["player_handoff"]),
        }

    @staticmethod
    def _fallback_opening_scene(
        *,
        starting_region: str,
        first_act_summary: str,
        hero_name: str,
    ) -> dict[str, object]:
        signature = f"{starting_region}上空一线忽明忽暗的地脉辉光"
        pressure = "地底传来的震响正在加剧，附近的人已经来不及把它当成寻常余震。"
        return {
            "scene_name": f"{starting_region}的异响",
            "location": starting_region,
            "objective": first_act_summary,
            "private_situation": {
                "premise": first_act_summary,
                "stakes": "英雄的选择将决定眼前的人能否避开灾变，并留下追查源头的机会。",
                "current_pressure": pressure,
                "dramatic_question": "英雄能否稳住眼前的异变，并找到它并非自然发生的证据？",
                "signature_image": signature,
                "opposition_goal": "引发异变的力量正在掩埋现场痕迹，阻止任何人追到源头。",
                "dilemma": "立即救助受困者更安全，但追查正在消失的痕迹可能找到灾变源头。",
                "closure_requirement": "眼前的危险得到实质改变，并留下一个由玩家选择造成的局部结果。",
                "irreversible_change": "获救者、受损地点或留下的线索至少有一项会永久记录本场选择。",
                "ending_echo": f"收束时再次呈现“{signature}”，让它因玩家选择发生可见变化。",
                "visible_elements": [
                    signature,
                    "一处正在崩裂、玩家可以立即接近的道路或建筑",
                ],
                "clue_pool": [
                    "异响中心残留的非自然刻痕",
                    "目击者对震动发生顺序的矛盾说法",
                ],
                "secrets": ["异变受到人为或有意志的力量推动，但具体来源尚未公开。"],
                "possible_reveals": [
                    "震动沿一条可追踪的地脉方向传来",
                    "现场破坏的先后顺序与自然灾害不符",
                ],
                "escalation_ladder": [
                    "异动波及一个玩家能够立即保护的人或物",
                    "关键痕迹开始被新的震动覆盖",
                ],
                "possible_payoffs": [
                    "保护眼前的人并获得当地人的信任",
                    "保住一条通往异变源头的可靠线索",
                ],
            },
            "public_opening": (
                f"{starting_region}的地面忽然又震了一次。{signature}沿着天际一闪，"
                "街边的石墙随即裂开，惊叫声从尘雾里传来。"
            ),
            "player_handoff": f"{hero_name}，你先做什么？",
        }

    def _normalized_locations(self, value: object) -> list[dict[str, object]]:
        if not isinstance(value, list):
            return []
        result: list[dict[str, object]] = []
        for item in value[:3]:
            if not isinstance(item, dict):
                continue
            name = self._clean(item.get("name"))
            description = self._clean(item.get("description"))
            feature_type = self._clean(item.get("feature_type"))
            position = self._clean(item.get("position_hint"))
            if not name or not description:
                continue
            if feature_type not in self._FEATURE_TYPES:
                feature_type = "landmark"
            if position not in self._POSITIONS:
                position = "center"
            result.append(
                {
                    "name": name,
                    "description": description,
                    "feature_type": feature_type,
                    "terrain": self._clean(item.get("terrain")) or "草原",
                    "position_hint": position,
                }
            )
        return result

    @staticmethod
    def _existing_location_names(current_world: dict[str, object]) -> list[str]:
        locations = current_world.get("major_locations")
        if isinstance(locations, dict):
            return [str(name).strip() for name in locations if str(name).strip()]
        return []

    @staticmethod
    def _clean(value: object) -> str:
        return " ".join(str(value or "").split()).strip()


__all__ = [
    "SOLO_SESSION_ZERO_COMPLETION_PROMPT",
    "SoloSessionZeroCompletion",
    "SoloSessionZeroCompleter",
]
