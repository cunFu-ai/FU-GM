from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from fu_gm.models import ChapterPackage, ChapterPackageScene


@dataclass(frozen=True)
class NaturalCampaignBeat:
    """One GM-facing session brief derived from confirmed Session 0 state."""

    number: int
    title: str
    arc: str
    location: str
    opening_instruction: str
    expected_focus: tuple[str, ...]
    boss_session: bool
    episode_identity: Mapping[str, Any]


@dataclass(frozen=True)
class NaturalCampaignSource:
    """Structured campaign facts safe to use when preparing a natural long run."""

    continent_name: str
    world_shape: str
    magic_tech_role: str
    group_concept: str
    starting_region: str
    selected_first_act: str
    locations: tuple[tuple[str, str], ...]
    kingdoms: tuple[tuple[str, str], ...]
    historical_events: tuple[str, ...]
    mysteries: tuple[str, ...]
    threats: tuple[str, ...]
    villain_seeds: tuple[str, ...]
    hero_threads: tuple[str, ...]

    @classmethod
    def from_world(
        cls,
        profile: Any,
        *,
        map_locations: Mapping[str, Any] | None = None,
        hero_names: Sequence[str] = (),
    ) -> "NaturalCampaignSource":
        location_descriptions: dict[str, str] = {}
        for name, description in dict(
            getattr(profile, "major_locations", {}) or {}
        ).items():
            _remember_description(location_descriptions, name, description)
        for name, record in dict(map_locations or {}).items():
            description = getattr(record, "description", "")
            _remember_description(location_descriptions, name, description)

        kingdoms = tuple(
            (
                _clean(name),
                _clean(description),
            )
            for name, description in dict(
                getattr(profile, "kingdoms", {}) or {}
            ).items()
            if _clean(name)
        )
        for name, description in kingdoms:
            _remember_description(location_descriptions, name, description)

        hero_threads: list[str] = []
        drafts = dict(getattr(profile, "hero_drafts", {}) or {})
        for hero_name in hero_names:
            draft = next(
                (
                    item
                    for item in drafts.values()
                    if _clean(getattr(item, "hero_name", "")) == _clean(hero_name)
                ),
                None,
            )
            if draft is None:
                continue
            pieces = _ordered_unique(
                (
                    getattr(draft, "hero_name", ""),
                    getattr(draft, "identity", ""),
                    getattr(draft, "theme", ""),
                    getattr(draft, "origin", ""),
                )
            )
            if pieces:
                hero_threads.append("；".join(pieces))

        starting_region = _clean(getattr(profile, "starting_region", ""))
        if not starting_region:
            starting_region = next(iter(location_descriptions), "")
        if not starting_region and kingdoms:
            starting_region = kingdoms[0][0]
        if not starting_region:
            starting_region = _clean(getattr(profile, "continent_name", "")) or "最初的落脚地"

        return cls(
            continent_name=_clean(getattr(profile, "continent_name", "")),
            world_shape=_clean(getattr(profile, "world_shape", "")),
            magic_tech_role=_clean(getattr(profile, "magic_tech_role", "")),
            group_concept=_clean(getattr(profile, "group_concept", "")),
            starting_region=starting_region,
            selected_first_act=_clean(
                getattr(profile, "selected_first_act_summary", "")
            ),
            locations=tuple(location_descriptions.items()),
            kingdoms=kingdoms,
            historical_events=tuple(
                _ordered_unique(getattr(profile, "historical_events", ()) or ())
            ),
            mysteries=tuple(
                _ordered_unique(getattr(profile, "mysteries", ()) or ())
            ),
            threats=tuple(
                _ordered_unique(getattr(profile, "world_threats", ()) or ())
            ),
            villain_seeds=tuple(
                _ordered_unique(getattr(profile, "villain_seeds", ()) or ())
            ),
            hero_threads=tuple(hero_threads),
        )


_SESSION_MOTIFS: tuple[tuple[str, str, str], ...] = (
    ("强开场", "让第一幕共识立刻成为可行动局面", "第一项选择产生公开后果"),
    ("余波上路", "让上一场的选择改变旅行条件", "队伍抵达下一处真实地点"),
    ("公开争议", "让一个制度或共同体必须明确表态", "争议得到暂时但可追踪的裁决"),
    ("危险深处", "用地下城或危险区域承载一条线索", "入口、路线或证据的命运确定"),
    ("第一幕收束", "让最初危机获得阶段性答案", "第一幕留下胜负与新局势"),
    ("陌生岸线", "让新地区以自身日常和矛盾登场", "当地关系不再停留在背景介绍"),
    ("追逐与取舍", "让时间、补给或隐蔽无法全部保住", "玩家的取舍改变对立方优势"),
    ("奥秘入口", "让世界奥秘出现可调查的物证或见证人", "取得一条可行动而非全知的答案"),
    ("角色回声", "让一名英雄的身份、主题或故乡进入局面", "角色选择改变一段关系或地点"),
    ("中盘揭示", "让已有证据改变大家对冲突的理解", "下一幕方向由玩家选择形成"),
    ("威胁前线", "让世界性威胁在一个具体地方伤到具体的人", "当地人获得可见的得失"),
    ("敌境纵深", "让队伍进入对立方掌控的危险区域", "救援、证据与路线至少兑现一项"),
    ("代价钥匙", "让解决方案要求承担已被铺垫的代价", "关键权限、盟友或资源归属确定"),
    ("世界震荡", "让多地后果同时进入视野但只聚焦一处", "联盟明确首要保护对象"),
    ("理念交锋", "让主要对立者用真实成果捍卫其立场", "关系、联盟或计划发生不可逆变化"),
    ("营火喘息", "让恢复、工程和角色关系共同准备终局", "每名英雄留下一个主动承诺"),
    ("会盟", "让盟友各自承担代价而非把风险推给英雄", "支援与责任形成明确清单"),
    ("终局入口", "让突入、撤离和保护普通人彼此牵制", "终局路线与平民处境确定"),
    ("最后门槛", "让对立者和队伍都作出无法撤回的选择", "终局条件被真正打开"),
    ("终幕与余波", "让核心威胁结算并逐一回应长期承诺", "三名英雄与世界获得具体尾声"),
)


def build_natural_campaign_beats(
    source: NaturalCampaignSource,
    *,
    target_sessions: int,
) -> list[NaturalCampaignBeat]:
    """Build a flexible campaign spine without importing an old authored plot."""

    count = max(1, int(target_sessions))
    locations = _ordered_unique(
        (
            source.starting_region,
            *(name for name, _description in source.locations),
            *(name for name, _description in source.kingdoms),
            source.continent_name,
        )
    ) or ["当前地区"]
    pressures = _ordered_unique((*source.villain_seeds, *source.threats)) or [
        "当前尚未解决的对立方计划"
    ]
    mysteries = list(source.mysteries) or ["当前尚未解释的世界奥秘"]
    history = list(source.historical_events) or ["塑造当前局势的旧日事件"]
    hero_threads = list(source.hero_threads) or ["英雄们在第零章确认的身份与主题"]

    beats: list[NaturalCampaignBeat] = []
    for index in range(1, count + 1):
        motif, purpose, payoff = _SESSION_MOTIFS[
            min(
                len(_SESSION_MOTIFS) - 1,
                ((index - 1) * len(_SESSION_MOTIFS)) // count,
            )
        ]
        location = locations[0] if index == 1 else locations[(index - 1) % len(locations)]
        pressure = pressures[(index - 1) % len(pressures)]
        mystery = mysteries[(index - 1) % len(mysteries)]
        old_event = history[(index - 1) % len(history)]
        hero_thread = hero_threads[(index - 1) % len(hero_threads)]
        boss_session = index == count or (
            count >= 5 and index in _scaled_milestones(count)
        )
        arc = _arc_label(index, count)
        first_act = (
            source.selected_first_act
            if index == 1 and source.selected_first_act
            else ""
        )
        opening_instruction = (
            f"第{index:02d}场从【{location}】的一个具体变化开始。"
            f"本场结构目的：{purpose}。"
            f"只使用权威世界事实、已经公开的后果与当前NPC动机；"
            f"把【{pressure}】落实为主动压力，并让【{mystery}】只提供可追查的部分答案。"
            f"旧日背景【{old_event}】可以解释现状，但不能改写已公开事实。"
            f"角色聚光候选为【{hero_thread}】；玩家仍自行决定目标和方法。"
            + (f"第一幕公开共识是【{first_act}】。" if first_act else "")
        )
        identity = {
            "question": (
                f"英雄能否在【{location}】回应【{pressure}】，并让自己的选择留下可追踪后果？"
            ),
            "image": (
                f"【{location}】的一件标志性事物因本场局势出现只属于“{motif}”的可见变化。"
            ),
            "opposition": (
                f"与【{pressure}】有关的行动者有自己的目标，并会在玩家犹豫或失败时继续推进。"
            ),
            "reversal": (
                f"从【{mystery}】准备一项可移动但未公开的解释；它应改变理解，"
                "不能否定玩家已经知道的事实。"
            ),
            "escalation": [
                f"让【{pressure}】首先造成一项局部且可见的变化",
                f"让【{location}】中的NPC为自己的利益作出明确选择",
                f"在收束前迫使桌面回答“{payoff}”",
            ],
            "payoff": [
                payoff,
                f"【{location}】与队伍的关系发生改变",
                "一项资源、承诺、线索或对立方优势进入后续状态",
            ],
        }
        beats.append(
            NaturalCampaignBeat(
                number=index,
                title=f"{location}·{motif}",
                arc=arc,
                location=location,
                opening_instruction=opening_instruction,
                expected_focus=(purpose, pressure, payoff),
                boss_session=boss_session,
                episode_identity=identity,
            )
        )
    return beats


def build_natural_chapter_package(
    source: NaturalCampaignSource,
) -> ChapterPackage:
    """Build the first chapter packet from confirmed shared creation."""

    location = source.starting_region
    first_act = source.selected_first_act or source.group_concept
    pressure = next(iter((*source.villain_seeds, *source.threats)), "当前公开威胁")
    mystery = next(iter(source.mysteries), "当前公开奥秘")
    title = f"{location}的第一幕"
    synopsis = first_act or (
        f"英雄们在{location}首次共同面对{pressure}，并决定为何继续同行。"
    )
    iconic_elements = _ordered_unique(
        (
            location,
            *(name for name, _description in source.locations[:2]),
            *(name for name, _description in source.kingdoms[:1]),
        )
    )
    return ChapterPackage(
        chapter_title=title,
        synopsis=synopsis,
        intro_prompt=(
            f"从【{location}】一个已经在第零章成立的日常画面开始，"
            f"让【{pressure}】造成具体打断；先描述现场，再把决定权交给玩家。"
        ),
        conclusion_prompt=(
            "当第一项公开危机得到阶段性答案、一个选择产生可追踪后果，"
            "且队伍明确下一步去向时收束本章。"
        ),
        timebox_minutes=240,
        shared_creation_slots=[],
        iconic_elements=iconic_elements,
        scenes=[
            ChapterPackageScene(
                title=f"{location}的强开场",
                scene_type="scene",
                location=location,
                purpose="让第零章确认的第一幕立即成为可行动局面。",
                when_to_use="第一章开场。",
                required_elements=[location],
                optional_elements=[pressure, mystery],
                success_condition="玩家作出第一个会改变局面的选择。",
                exit_condition="局面转入调查、交涉、旅行或冲突中的一种。",
            ),
            ChapterPackageScene(
                title="线索与代价",
                scene_type="investigation",
                location=location,
                purpose="让玩家的方法揭示一条可行动线索，并明确失败或拖延的代价。",
                when_to_use="玩家开始追查当前危机时。",
                required_elements=[mystery],
                optional_elements=[pressure],
                success_condition="获得一条基于当前暗线、可以继续行动的线索。",
                exit_condition="线索到手、代价兑现，或对立方主动改变局面。",
            ),
            ChapterPackageScene(
                title="第一幕的选择",
                scene_type="climax",
                location=location,
                purpose="让第一章围绕玩家真正采用的方法形成局部高潮。",
                when_to_use="本章的核心选择已经成熟时。",
                required_elements=[pressure],
                optional_elements=[mystery],
                success_condition="第一项危机获得阶段性答案。",
                exit_condition="一个后果被写入长期状态，且队伍明确下一步去向。",
            ),
        ],
        adversary_notes=[
            f"以【{pressure}】为当前压力来源；为具体行动者准备目标，但不要预写玩家路线。"
        ],
        reward_notes=["按实际场次结算经验，并让本章选择改变后续世界状态。"],
        gm_notes=[
            "这是从权威第零章状态生成的可修改局面，不是必须照顺序演出的剧本。",
            "未公开解释可以随玩家行动移动；已经公开的事实不得改写。",
        ],
    )


def _scaled_milestones(total: int) -> set[int]:
    return {
        max(1, round(total * ratio))
        for ratio in (0.25, 0.5, 0.75, 0.95)
    }


def _arc_label(number: int, total: int) -> str:
    ratio = number / max(1, total)
    if ratio <= 0.25:
        return "第一幕：立足"
    if ratio <= 0.5:
        return "第二幕：追索"
    if ratio <= 0.75:
        return "第三幕：反击"
    if ratio < 1:
        return "终幕：逼近"
    return "终幕：结局"


def _remember_description(
    target: dict[str, str],
    name: object,
    description: object,
) -> None:
    clean_name = _clean(name)
    if clean_name and clean_name not in target:
        target[clean_name] = _clean(description)


def _ordered_unique(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = _clean(value)
        if text and text not in result:
            result.append(text)
    return result


def _clean(value: object) -> str:
    return " ".join(str(value or "").split()).strip()
