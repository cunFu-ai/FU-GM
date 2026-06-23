from __future__ import annotations

from dataclasses import dataclass

from fu_gm.models import WorldCreationProfile
from fu_gm.prepared_locations import (
    PREPARED_LOCATION_SEEDS as LOCATION_LIBRARY,
    PreparedLocationSeed,
)


@dataclass(frozen=True)
class GMGuidanceProfile:
    inspiration_tags: tuple[str, ...]
    principles: tuple[str, ...]
    questions: tuple[str, ...]
    story_beats: tuple[str, ...]
    hero_creation_prompts: tuple[str, ...]
    location_seeds: tuple[PreparedLocationSeed, ...]


TAG_KEYWORDS: dict[str, tuple[str, ...]] = {
    "techno_pressure": (
        "科技",
        "工业",
        "工厂",
        "机械",
        "魔导",
        "公司",
        "财阀",
        "企业",
        "污染",
        "下层",
        "上层",
        "网络",
        "星球",
        "太空",
        "实验室",
        "能源",
    ),
    "natural_home": (
        "自然",
        "森林",
        "村庄",
        "家乡",
        "故乡",
        "野兽",
        "精灵",
        "荒野",
        "山脉",
        "海湾",
        "岛",
        "生态",
        "污染",
        "平衡",
        "丰饶",
    ),
    "epic_myth": (
        "王国",
        "城堡",
        "水晶",
        "神",
        "神殿",
        "恶魔",
        "帝国",
        "飞艇",
        "天空",
        "世界树",
        "预言",
        "圣地",
        "神器",
        "封印",
    ),
    "dungeon_mystery": (
        "地下城",
        "迷宫",
        "遗迹",
        "宝箱",
        "古代",
        "废墟",
        "神庙",
        "祭坛",
        "塔",
        "方尖碑",
        "禁地",
    ),
    "ocean_roads": (
        "海",
        "航海",
        "群岛",
        "港口",
        "海盗",
        "船",
        "潮",
        "湾",
    ),
}


PREPARED_LOCATION_SEEDS = LOCATION_LIBRARY


def build_gm_guidance(world: WorldCreationProfile, *, extra_text: str = "") -> GMGuidanceProfile:
    tags = infer_inspiration_tags(world, extra_text=extra_text)
    principles = _principles_for(tags)
    questions = _questions_for(tags)
    story_beats = _story_beats_for(tags)
    hero_prompts = _hero_prompts_for(tags)
    seeds = _location_seeds_for(tags, context_text=_world_text(world, extra_text=extra_text))
    return GMGuidanceProfile(
        inspiration_tags=tuple(tags),
        principles=tuple(principles),
        questions=tuple(questions),
        story_beats=tuple(story_beats),
        hero_creation_prompts=tuple(hero_prompts),
        location_seeds=tuple(seeds),
    )


def infer_inspiration_tags(world: WorldCreationProfile, *, extra_text: str = "") -> list[str]:
    text = _world_text(world, extra_text=extra_text)
    scores: dict[str, int] = {tag: 0 for tag in TAG_KEYWORDS}
    for tag, keywords in TAG_KEYWORDS.items():
        scores[tag] += sum(text.count(keyword) for keyword in keywords)

    if "科技奇幻" in text:
        scores["techno_pressure"] += 6
    if "自然奇幻" in text:
        scores["natural_home"] += 6
    if any(token in text for token in ("高度奇幻", "史诗奇幻", "高奇幻")):
        scores["epic_myth"] += 6
    if "污染" in text:
        scores["techno_pressure"] += 1
        scores["natural_home"] += 1
    if "帝国" in text:
        scores["epic_myth"] += 1
        scores["techno_pressure"] += 1

    ranked = [tag for tag, score in sorted(scores.items(), key=lambda item: item[1], reverse=True) if score > 0]
    if not ranked:
        ranked = ["epic_myth", "dungeon_mystery"]
    if "dungeon_mystery" not in ranked and any(
        item for item in (world.mysteries, world.major_locations, world.historical_events) if item
    ):
        ranked.append("dungeon_mystery")
    return ranked[:4]


def question_hint_for_step(world: WorldCreationProfile, step: str) -> str:
    tags = infer_inspiration_tags(world)
    if step == "kingdom":
        if "techno_pressure" in tags:
            return "可以顺手想：谁控制资源、媒体或交通，谁被这套秩序压低声音？"
        if "natural_home" in tags:
            return "可以顺手想：这个国家或聚落依赖哪片土地、哪种生物或哪条古老习俗？"
        if "epic_myth" in tags:
            return "可以顺手想：它守护哪种奇迹、誓约、血脉或禁忌？"
    if step == "history":
        if "epic_myth" in tags:
            return "优先找那种会改变力量平衡的旧真相，而不只是年代久远的背景。"
        if "techno_pressure" in tags:
            return "优先找一次技术或制度胜利背后的代价。"
        if "natural_home" in tags:
            return "优先找一次自然循环被打断、误解或修复的事件。"
    if step == "mystery":
        if "techno_pressure" in tags:
            return "这个谜团最好能让玩家怀疑进步、能源或记忆的真实来源。"
        if "natural_home" in tags:
            return "这个谜团最好能改变大家对某个熟悉地点或家园的看法。"
        if "epic_myth" in tags:
            return "这个谜团最好有中期揭示时能震动王国、神祇或英雄使命的重量。"
    if step == "threat":
        if "techno_pressure" in tags:
            return "威胁可以是一个看似合理的系统，而不只是某个坏人。"
        if "natural_home" in tags:
            return "威胁可以是失衡、诅咒或灾害化身，未必有清晰人脸。"
        if "epic_myth" in tags:
            return "威胁应有终局规模，但最好先从英雄能触碰的小裂缝出现。"
    return ""


def summarize_guidance_for_prompt(
    world: WorldCreationProfile,
    *,
    extra_text: str = "",
    location_limit: int | None = 5,
    detailed_locations: bool = False,
    include_all_locations: bool = False,
) -> dict[str, object]:
    guidance = build_gm_guidance(world, extra_text=extra_text)
    available_seeds = PREPARED_LOCATION_SEEDS if include_all_locations else guidance.location_seeds
    seeds = available_seeds if location_limit is None else available_seeds[: max(0, location_limit)]
    return {
        "inspiration_tags": list(guidance.inspiration_tags),
        "principles": list(guidance.principles[:4]),
        "question_angles": list(guidance.questions[:4]),
        "story_beats": list(guidance.story_beats[:4]),
        "hero_creation_prompts": list(guidance.hero_creation_prompts[:4]),
        "prepared_locations": [seed.prompt_payload(detailed=detailed_locations) for seed in seeds],
    }


def _world_text(world: WorldCreationProfile, *, extra_text: str = "") -> str:
    parts: list[str] = [extra_text, world.world_style, world.magic_tech_role, world.group_concept, world.starting_region]
    parts.extend(world.tone_preferences)
    parts.extend(world.playstyle_themes)
    parts.extend(world.core_themes)
    parts.extend(world.historical_events)
    parts.extend(world.villain_seeds)
    parts.extend(world.villain_mirrors)
    parts.extend(world.mysteries)
    parts.extend(world.world_threats)
    parts.extend(world.major_locations.keys())
    parts.extend(world.major_locations.values())
    parts.extend(world.kingdoms.keys())
    parts.extend(world.kingdoms.values())
    parts.extend(world.factions.keys())
    parts.extend(world.factions.values())
    for draft in world.hero_drafts.values():
        parts.extend([draft.identity, draft.theme, draft.origin])
        parts.extend(draft.notes)
    return "\n".join(str(part or "") for part in parts)


def _principles_for(tags: list[str]) -> list[str]:
    values = [
        "不要要求玩家选择世界类型；先接住画面，再把它转化为地点、阵营、谜团、威胁或角色钩子。",
        "每个新地点都至少带一个可回答的问题：谁在这里生活、这里隐藏什么、英雄介入会改变谁的命运。",
    ]
    if "epic_myth" in tags:
        values.extend(
            [
                "史诗感来自规模和情感同时升级：中期揭示能颠覆力量平衡，终局战斗则回应英雄主题。",
                "奇观必须可玩：水晶尖塔、天空国度或神殿都应附带冲突、代价和一个能行动的势力。",
            ]
        )
    if "techno_pressure" in tags:
        values.extend(
            [
                "科技奇幻的压迫最好体现在制度、能源、债务、媒体和基础设施中，而不只是坏人的残忍。",
                "反派可以像救世主一样出现：他们确实带来便利，但代价由看不见的人承担。",
            ]
        )
    if "natural_home" in tags:
        values.extend(
            [
                "自然奇幻应重视重复回访：同一个村庄、森林或海湾每次都因玩家选择发生一点变化。",
                "威胁可以是环境失衡、诅咒或误入歧途的守护者，解决它常常需要理解而不只是击败。",
            ]
        )
    if "dungeon_mystery" in tags:
        values.append("地下城不只是房间列表；它应讲述某个地点、文明、反派或英雄内心问题的故事。")
    return _dedupe(values)


def _questions_for(tags: list[str]) -> list[str]:
    values = [
        "这个地区最先让镜头看见的画面是什么？",
        "如果英雄什么都不做，这里会在下一场或下一章变得怎样？",
    ]
    if "techno_pressure" in tags:
        values.extend(
            [
                "谁从这套城市、技术或能源系统中获利？谁承担代价？",
                "这个看似先进的事物拿走了人们的什么：时间、记忆、灵魂、阳光，还是选择权？",
            ]
        )
    if "natural_home" in tags:
        values.extend(
            [
                "这里最像家的日常是什么？第一个异样会从哪里冒出来？",
                "哪个生物、老人、导师或孩童最能代表这片土地的声音？",
            ]
        )
    if "epic_myth" in tags:
        values.extend(
            [
                "什么真相一旦揭开，会让王国、神祇或英雄使命立刻改写？",
                "终局战斗的规模可以很大，但它最终要证明哪位英雄的主题？",
            ]
        )
    if "dungeon_mystery" in tags:
        values.append("这个遗迹留下的奖励、机关和怪物分别在讲同一个旧故事的哪一面？")
    return _dedupe(values)


def _story_beats_for(tags: list[str]) -> list[str]:
    values = ["前期用小地点和具体人物承载世界问题，避免一开始就只谈抽象设定。"]
    if "natural_home" in tags:
        values.append("前期让玩家爱上一个可回访地点；中期揭示它为何失衡；后期让修复代价落到英雄关系上。")
    if "techno_pressure" in tags:
        values.append("前期展现便利与压迫并存；中期揭示能源、网络或制度的真实代价；后期让英雄攻击系统核心。")
    if "epic_myth" in tags:
        values.append("前期给出奇观和使命；中期揭示足以颠覆力量平衡的真相；后期用宏大战斗回应英雄主题。")
    if "dungeon_mystery" in tags:
        values.append("每个地下城至少回答一个旧问题，同时提出一个更危险的新问题。")
    return _dedupe(values)


def _hero_prompts_for(tags: list[str]) -> list[str]:
    values = [
        "创建角色时追问身份、主题、故乡如何与一个地点或事件相连，而不只问职业分配。",
        "每名英雄最好带一个会推动提问的缺口：欠谁一句话、害怕什么真相、想证明什么。",
    ]
    if "techno_pressure" in tags:
        values.append("问英雄曾被哪个系统伤害、帮助或利用；也可以问他是否曾从不公中受益。")
    if "natural_home" in tags:
        values.append("问英雄把哪里称作家、谁教会他第一件重要的事、他不愿看见什么被改变。")
    if "epic_myth" in tags:
        values.append("问英雄最崇高的信念在什么情况下会变得危险，这能成为反派镜像。")
    return _dedupe(values)


def _location_seeds_for(tags: list[str], *, context_text: str = "") -> list[PreparedLocationSeed]:
    ranked = [
        seed
        for seed in PREPARED_LOCATION_SEEDS
        if any(tag in tags for tag in seed.inspiration_tags)
    ]
    if not ranked:
        ranked = list(PREPARED_LOCATION_SEEDS)
    ranked.sort(key=lambda seed: _seed_score(seed, tags, context_text=context_text), reverse=True)
    return ranked


def _seed_score(seed: PreparedLocationSeed, tags: list[str], *, context_text: str = "") -> int:
    score = sum(3 - min(index, 2) for index, tag in enumerate(tags) if tag in seed.inspiration_tags)
    searchable = (
        seed.name,
        seed.archetype,
        *seed.keywords,
        *seed.terrain,
        *seed.themes,
        *seed.typical_features,
    )
    score += sum(2 for token in searchable if len(token) >= 2 and token in context_text)
    return score


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result

