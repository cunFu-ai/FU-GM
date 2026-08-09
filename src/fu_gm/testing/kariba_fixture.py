from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fu_gm.http_server import FUGMHttpService
from fu_gm.models import HeroDraft


KARIBA_PLAYERS = ("测试玩家甲", "loading")
KARIBA_HEROES = ("诺艾尔", "艾丽妮")
KARIBA_INVITATION = "第零章已经准备好了。现在进入第一章吗？"


@dataclass(frozen=True)
class KaribaReplayMessage:
    speaker: str
    text: str
    expectation: str = "reply"
    addressed: bool = False
    reply_to_gm: bool = False
    quoted_text: str = ""


def seed_kariba_ready_campaign(
    service: FUGMHttpService,
    *,
    campaign_id: str,
    session_id: str,
    channel_id: str,
    skip_map_render: bool = True,
) -> Any:
    """Prepare the exact table state immediately before Chapter One consent.

    This is deliberately a test fixture rather than an alternate game setup
    path. Production still reaches this state through the normal Session Zero
    tools and validators.
    """

    runtime = service._runtime(campaign_id)
    app = runtime.app
    app.initialize_session_zero(participants=list(KARIBA_PLAYERS))
    manager = app.session_zero_manager
    world = manager.state.world
    world.continent_name = "宁姆格福大陆"
    world.map_card = "宁姆格福大陆世界地图"
    world.magic_tech_role = (
        "科技与魔法彼此对立；两百年前的禁忌仪式令藤蔓赋予钢铁生命。"
    )
    world.kingdoms = {
        "托伦王国": "卡里巴村所在的人类王国。",
        "索朗帝国": "两百年前以蒸汽飞艇和机械巨兽发动战争的旧帝国。",
    }
    world.historical_events = [
        "两百年前，自然联邦以禁忌仪式瘫痪索朗帝国的战争机械；部分钢铁由此获得意识。"
    ]
    world.mysteries = [
        "重叠日会让建筑与区域短暂变成森林、海洋或古楼。",
        "部分失踪灵魂似乎被转存进钢铁或藤蔓。",
    ]
    world.world_threats = [
        "有人正在秘密抽取囚犯的灵魂残留，并把成果运往卡里巴庄园。"
    ]
    world.group_concept = "因同囚卡里巴村监狱而被迫合作的两名逃亡者"
    world.safety_lines = ["不出现性暴力", "不细致描写酷刑"]
    world.safety_veils = ["严重伤势淡出处理"]
    world.starting_region = "卡里巴村"
    world.major_locations.update(
        {
            "卡里巴村": "托伦王国南方、男爵庄园控制下的村庄。",
            "星落尖塔": "艾丽妮被放逐前生活的魔法高塔。",
            "旧索朗第七工程遗址": "与钢铁生命起源有关的战争遗址。",
        }
    )
    world.selected_first_act_summary = (
        "第一幕从卡里巴村监狱的雨夜越狱开始；两名英雄被关在相邻牢房，"
        "地下封印的异常为逃生制造机会。"
    )
    world.first_act_questions = [
        "你们为什么会被关起来？",
        "你们是无辜的还是有罪的？",
        "你们能独自逃离吗，还是需要他人的帮助？",
    ]
    world.first_act_question_answers = {
        "你们为什么会被关起来？": [
            "测试玩家甲：诺艾尔洗劫卡里巴村男爵的藏品时被法阵困住。",
            "loading：艾丽妮在托伦市集偷吃魔法水果充饥，被卫兵逮捕。",
        ],
        "你们是无辜的还是有罪的？": [
            "两人都确实触犯了当地法律，但惩罚背后另有隐情。"
        ],
        "你们能独自逃离吗，还是需要他人的帮助？": [
            "牢房封印异常时，两人需要合作才能逃离。"
        ],
    }
    world.first_act_opening_equipment_restrictions = [
        {
            "actor": "诺艾尔",
            "items": ["钢匕首", "细剑"],
            "reason": "入狱时被守卫收缴",
            "location": "卡里巴村监狱值班室证物柜",
        },
        {
            "actor": "艾丽妮",
            "items": ["法杖", "魔典", "贤者之袍"],
            "reason": "入狱时被守卫收缴",
            "location": "卡里巴村监狱值班室证物柜",
        },
    ]
    world.hero_drafts = {
        "测试玩家甲": HeroDraft(
            player_name="测试玩家甲",
            hero_name="诺艾尔",
            identity="离家出走的猫耳秘宝猎人",
            theme="野心",
            origin="托伦王国",
            classes={"武器大师": 2, "旅人": 1, "元素使": 2},
            attributes={"敏捷": 8, "洞察": 8, "力量": 8, "意志": 8},
            skills={
                "碎骨": 1,
                "破防打击": 1,
                "宝物猎人": 1,
                "元素魔法": 1,
                "元素系仪式": 1,
            },
            spells=["元素武器"],
            equipment=["钢匕首", "细剑", "丝质衬衫"],
            confirmed=True,
        ),
        "loading": HeroDraft(
            player_name="loading",
            hero_name="艾丽妮",
            identity="被放逐的学徒",
            theme="归属",
            origin="星落尖塔",
            classes={"元素使": 2, "旅人": 1, "博学家": 2},
            attributes={"敏捷": 6, "洞察": 10, "力量": 8, "意志": 8},
            skills={
                "元素魔法": 1,
                "元素系仪式": 1,
                "见多识广": 1,
                "集中心智": 1,
                "知识就是力量": 1,
            },
            spells=["元素武器"],
            equipment=["法杖", "魔典", "贤者之袍"],
            confirmed=True,
        ),
    }
    contribution_topics = (
        "kingdom_contributions",
        "historical_event_contributions",
        "mystery_contributions",
        "threat_contributions",
    )
    for participant in manager.state.participants:
        for topic in contribution_topics:
            if topic not in participant.answered_topics:
                participant.answered_topics.append(topic)
    manager.refresh_stage_from_state()
    manager.set_chapter_one_transition(
        "invited",
        speaker="时悠",
        evidence=KARIBA_INVITATION,
    )
    service.session_gates.activate(
        campaign_id,
        channel_id,
        session_id,
        status="session_zero",
        reason="卡里巴村第一章回放夹具",
    )
    runtime.log_manager.append_message(
        campaign_id,
        session_id,
        speaker="时悠",
        content=KARIBA_INVITATION,
        role="assistant",
        channel_id=channel_id,
        message_id="kariba-invitation",
        metadata={"mode": "chapter_one_invitation"},
    )
    if skip_map_render:
        app.ensure_world_map_for_adventure = lambda **_kwargs: {
            "status": "existing",
            "reason": "model comparison fixture",
        }
    service._autosave_campaign(runtime, campaign_id)
    return runtime


def kariba_opening_probe_messages() -> list[KaribaReplayMessage]:
    """Small shared probe that tolerates different but valid opening fiction."""

    return [
        KaribaReplayMessage(
            speaker="测试玩家甲",
            text="嗯",
            reply_to_gm=True,
            quoted_text=KARIBA_INVITATION,
        ),
        KaribaReplayMessage(
            speaker="测试玩家甲",
            text="诺艾尔先看看牢门、走廊和自己身上还剩什么。",
        ),
        KaribaReplayMessage(
            speaker="loading",
            text="你在牢里哪来的剑",
            expectation="silent",
        ),
        KaribaReplayMessage(
            speaker="loading",
            text="艾丽妮观察自己和牢门上的魔力变化，想判断两者有没有关联。",
        ),
        KaribaReplayMessage(
            speaker="测试玩家甲",
            text="诺艾尔问艾丽妮：你看出什么了吗？",
            expectation="silent",
        ),
        KaribaReplayMessage(
            speaker="loading",
            text="艾丽妮把刚发现的现象告诉诺艾尔，然后寻找牢里能利用的东西。",
        ),
    ]


__all__ = [
    "KARIBA_HEROES",
    "KARIBA_INVITATION",
    "KARIBA_PLAYERS",
    "KaribaReplayMessage",
    "kariba_opening_probe_messages",
    "seed_kariba_ready_campaign",
]
