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
    tone_guidance: tuple[str, ...]
    location_guidance: tuple[str, ...]
    character_guidance: tuple[str, ...]
    scene_framework: tuple[str, ...]
    npc_guidance: tuple[str, ...]
    opening_moves: tuple[str, ...]
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
    tone_guidance = _tone_guidance_for(tags)
    location_guidance = _location_guidance_for(tags)
    character_guidance = _character_guidance_for(tags)
    scene_framework = _scene_framework_for(tags)
    npc_guidance = _npc_guidance_for(tags)
    opening_moves = _opening_moves_for(tags)
    questions = _questions_for(tags)
    story_beats = _story_beats_for(tags)
    hero_prompts = _hero_prompts_for(tags)
    seeds = _location_seeds_for(tags, context_text=_world_text(world, extra_text=extra_text))
    return GMGuidanceProfile(
        inspiration_tags=tuple(tags),
        principles=tuple(principles),
        tone_guidance=tuple(tone_guidance),
        location_guidance=tuple(location_guidance),
        character_guidance=tuple(character_guidance),
        scene_framework=tuple(scene_framework),
        npc_guidance=tuple(npc_guidance),
        opening_moves=tuple(opening_moves),
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
        "tone_guidance": list(guidance.tone_guidance[:6]),
        "location_guidance": list(guidance.location_guidance[:6]),
        "character_guidance": list(guidance.character_guidance[:6]),
        "scene_framework": list(guidance.scene_framework[:6]),
        "npc_guidance": list(guidance.npc_guidance[:6]),
        "opening_moves": list(guidance.opening_moves[:6]),
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


def _tone_guidance_for(tags: list[str]) -> list[str]:
    values = [
        "先从玩家已经给出的画面推断基调，不要求玩家选择某个世界类型标签。",
        "基调不是滤镜，而是主持时反复出现的代价、希望、日常和危险的组合。",
    ]
    if "techno_pressure" in tags:
        values.extend(
            [
                "科技奇幻的镜头要同时给出便利与不公：灯光、列车、契约、账本和维护这些东西的人。",
                "压迫不必每次都靠恶人说狠话；制度、债务、能源短缺和信息控制本身就能制造压迫感。",
            ]
        )
    if "natural_home" in tags:
        values.extend(
            [
                "自然奇幻先让玩家闻到家园的气味，再让一处微小失衡打破它。",
                "土地、生物、老人、孩子和季节变化都应有声音；危机最好能被理解，而不只是被消灭。",
            ]
        )
    if "epic_myth" in tags:
        values.extend(
            [
                "史诗奇幻要让奇观与人心同场出现：宏大预言旁边必须有具体的人在承担后果。",
                "神殿、水晶、王国与神器都应服务英雄主题，而不是成为空洞背景板。",
            ]
        )
    if "dungeon_mystery" in tags:
        values.append("地下城基调应像一段能被探索的旧故事：机关、怪物、宝物和墙画都指向同一个旧问题。")
    if "ocean_roads" in tags:
        values.append("海路与群岛故事要重视潮汐、港口传闻、船上关系和远方灯火，不要只把海当作地图空白。")
    return _dedupe(values)


def _location_guidance_for(tags: list[str]) -> list[str]:
    values = [
        "每个地点至少包含三层：玩家一眼能看见的画面、能互动的人或物、暂时不能直接说破的秘密。",
        "地点不要只做地名；它应提供一个选择压力，例如通行权、庇护、交易、危险、仪式或失衡。",
    ]
    if "techno_pressure" in tags:
        values.append("科技地点优先问：谁维护设备、谁被排除在服务之外、故障会先伤害谁。")
    if "natural_home" in tags:
        values.append("自然地点优先问：这里原本的循环是什么，哪个循环正在被打断。")
    if "epic_myth" in tags:
        values.append("史诗地点优先问：它守护何种誓约、血脉、封印、遗物或禁忌。")
    if "dungeon_mystery" in tags:
        values.append("遗迹或地下城优先准备线索链，而不是房间清单；玩家从任意入口调查都应能触到同一真相。")
    if "ocean_roads" in tags:
        values.append("海港、内海、群岛和船路要有方向感：潮流从哪里来，消息和追兵会从哪里靠岸。")
    return _dedupe(values)


def _character_guidance_for(tags: list[str]) -> list[str]:
    values = [
        "角色引导先问身份、主题、故乡如何被当前世界需要或伤害，再谈职业与战术定位。",
        "不要把主题当成选项题；自定义主题也可以，只要玩家能说出它如何支配行动。",
        "故乡应尽量落到地图或关系网上：谁还在那里、它出了什么事、英雄为什么离开或回来。",
    ]
    if "techno_pressure" in tags:
        values.append("科技压迫题材中，问英雄与系统的关系：受害者、维护者、逃亡者、受益者或背叛者。")
    if "natural_home" in tags:
        values.append("自然题材中，问英雄最想保护的日常、老师、生物或土地，以及他不愿承认的改变。")
    if "epic_myth" in tags:
        values.append("史诗题材中，问英雄的信念何时会变得危险，这能自然生出反派镜像。")
    return _dedupe(values)


def _scene_framework_for(tags: list[str]) -> list[str]:
    values = [
        "进入新场景时先给 GM 自己确定三件后台事：眼前可见画面、正在推进的压力、至少一个可互动焦点。",
        "场景开头先由 GM 描述局面，再等待玩家行动；不要让玩家用第一句话替 GM 决定这里发生了什么。",
        "每个调查、交涉或战斗结果都应回扣当前场景暗线；成功给线索或位置，失败给阻碍或代价，而不是只说没看出来。",
        "玩家提出的方向如果合理，就让它接触场景框架中的某个焦点；如果超出框架，先判断是否需要扩展暗线，而不是立刻满足所有剧情请求。",
    ]
    if "techno_pressure" in tags:
        values.append("科技场景的压力源常来自安保、能源、广播、通行权、维修窗口或债务契约。")
    if "natural_home" in tags:
        values.append("自然场景的压力源常来自失衡征兆、迁徙、生病的土地、异常潮汐或守护者误解。")
    if "epic_myth" in tags:
        values.append("史诗场景的压力源常来自誓约、预言、王权、神器反应或古老封印松动。")
    if "dungeon_mystery" in tags:
        values.append("地下城场景要让机关、怪物、宝物、壁画或环境伤害服务同一个旧问题。")
    return _dedupe(values)


def _npc_guidance_for(tags: list[str]) -> list[str]:
    values = [
        "重要 NPC 出场前先定功能位：门槛、诱因、镜像、证人、受害者、误导者、帮手或反派代理。",
        "NPC 回答玩家时要推进场景：给出态度、条件、代价、线索或新压力；不要只复述玩家刚说过的话。",
        "NPC 可以不知道真相，但他的误解也应有来源，并能把玩家引向另一个可行动线索。",
        "反派或敌对 NPC 的行动要服务目标：阻止目标命刻、推进威胁命刻、夺取人质、撤离证据或逼迫英雄选择。",
    ]
    if "techno_pressure" in tags:
        values.append("科技题材 NPC 常能体现系统位置：安保执行者、维修员、债务人、数据审计员、被包装的受益者。")
    if "natural_home" in tags:
        values.append("自然题材 NPC 常能代表土地声音：守林人、老人、孩子、受惊动物、误入歧途的守护者。")
    if "epic_myth" in tags:
        values.append("史诗题材 NPC 常承载誓约与代价：骑士、神官、王族、亡灵见证者或旧时代幸存者。")
    return _dedupe(values)


def _opening_moves_for(tags: list[str]) -> list[str]:
    values = [
        "开场先让事情已经在发生：一个声音、一处异常、一个正在做决定的 NPC，最后自然交给英雄。",
        "不要把后台清单念成导览；把可调查、可交涉、可阻止的对象藏进画面里。",
        "第一幕只公开足够玩家行动的信息；未公开真相用线索、NPC犹豫或命刻压力逐步露出。",
    ]
    if "techno_pressure" in tags:
        values.append("科技开场可从广播故障、检票口、债务契约、停电或安保巡逻切入。")
    if "natural_home" in tags:
        values.append("自然开场可从熟悉日常突然错拍切入：水车倒转、鸟群沉默、海潮不退或村民避谈。")
    if "epic_myth" in tags:
        values.append("史诗开场可从宏大奇观的一处裂缝切入：水晶失声、圣火变色、王庭急召或旧誓约醒来。")
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

