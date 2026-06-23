from __future__ import annotations

from dataclasses import dataclass

from fu_gm.models import SceneRecord, SceneType


@dataclass(frozen=True)
class PlayProcessGuidance:
    current_focus: str
    principles: tuple[str, ...]
    scene_flow: tuple[str, ...]
    scene_end_triggers: tuple[str, ...]
    scene_type_guidance: tuple[str, ...]
    session_guidance: tuple[str, ...]
    campaign_guidance: tuple[str, ...]


CORE_PRINCIPLES: tuple[str, ...] = (
    "场景是一段有头有尾的游戏过程，应围绕一个具体角色、难题、地点或冲突展开。",
    "GM 负责宣布场景开始和结束，但玩家可以要求设置他们想看的特定场景。",
    "场景外的角色不能直接干预叙事；若要介入，应先让其进入场景并成为镜头重点。",
    "不要预设剧情路线；围绕玩家的行动和目标编织情境，多提问题并运用答案。",
)

SCENE_FLOW: tuple[str, ...] = (
    "开场铺设要简短，通常一两句即可，说明登场角色、时间地点、可互动的人物/物品/危险。",
    "让玩家角色与场景内容互动；GM 描述 NPC 与环境回应，并在需要时加入细节。",
    "只有行动有风险、阻碍或规则要求时才检定；复杂目标用命刻，激烈对抗用冲突场景。",
    "当局面解决或镜头转移到新的时间/地点时，收束场景并简述结果。",
)

SCENE_END_TRIGGERS: tuple[str, ...] = (
    "当前问题已经解决，无论结果好坏。",
    "玩家明确转向新的地点、时间段、目标或镜头焦点。",
    "冲突已经达成胜负、妥协、逃离，或转化为另一种冲突。",
    "幕间活动已经概括完成，需要放大成标准场景或进入下一段旅程。",
)

SESSION_GUIDANCE: tuple[str, ...] = (
    "一场游戏由多个场景组成，通常像一集动画：聚焦一个地点、迫切事件或清晰目标。",
    "每场游戏不必塞满战斗；冲突场景应留给戏剧性强、目标对立且每一秒重要的时刻。",
    "场次结束时应整理公开总结、未解悬念、奖励、世界变化和 GM 私密备注，供下次接续。",
    "适合在问题解决、资源需要刷新、重要悬念出现或现实时间接近结束时收场。",
)

CAMPAIGN_GUIDANCE: tuple[str, ...] = (
    "战役是多场游戏共同形成的史诗，不是预写剧本；英雄选择和反派目的共同推动发展。",
    "反派若长期不被阻止，应推进目标、改变计划或造成世界层面的代价，并让玩家看见后果。",
    "失败通常不应只是死亡；更常见的代价是失去机会、牺牲关系、地点恶化或反派升格。",
    "长期节奏应让英雄主题、羁绊、故乡、谜团和反派镜像不断回到镜头中。",
)


def build_play_process_guidance(
    scene: SceneRecord | None,
    *,
    conflict_active: bool = False,
) -> PlayProcessGuidance:
    return PlayProcessGuidance(
        current_focus=_current_focus(scene, conflict_active=conflict_active),
        principles=CORE_PRINCIPLES,
        scene_flow=SCENE_FLOW,
        scene_end_triggers=SCENE_END_TRIGGERS,
        scene_type_guidance=tuple(_scene_type_guidance(scene, conflict_active=conflict_active)),
        session_guidance=SESSION_GUIDANCE,
        campaign_guidance=CAMPAIGN_GUIDANCE,
    )


def summarize_play_process_for_prompt(
    scene: SceneRecord | None,
    *,
    conflict_active: bool = False,
) -> dict[str, object]:
    guidance = build_play_process_guidance(scene, conflict_active=conflict_active)
    return {
        "current_focus": guidance.current_focus,
        "principles": list(guidance.principles[:3]),
        "scene_flow": list(guidance.scene_flow),
        "scene_end_triggers": list(guidance.scene_end_triggers),
        "scene_type_guidance": list(guidance.scene_type_guidance),
        "session_guidance": list(guidance.session_guidance[:3]),
        "campaign_guidance": list(guidance.campaign_guidance[:3]),
    }


def _current_focus(scene: SceneRecord | None, *, conflict_active: bool) -> str:
    if conflict_active:
        return "当前是冲突场景；保持回合、行动者、目标和命刻清晰，直到一方达成目标、撤退或达成妥协。"
    if scene is None:
        return "当前没有明确场景；下一次 GM 回应应先铺设一个简短场景，或询问玩家想看哪类场景。"
    type_name = _scene_type_label(scene.scene_type)
    location = f"；地点：{scene.location}" if scene.location else ""
    objective = f"；目标：{scene.objective}" if scene.objective else ""
    return f"当前镜头是{type_name}【{scene.name}】{location}{objective}。"


def _scene_type_guidance(scene: SceneRecord | None, *, conflict_active: bool) -> list[str]:
    if conflict_active or (scene and scene.scene_type == SceneType.CONFLICT):
        return [
            "冲突场景只用于战斗、追逐、审判、谈判等目标强烈对立的高压局面。",
            "每轮交替行动；不能跳过回合；人数不等时先交替，再让人数较多方完成剩余回合。",
            "强敌行动应给玩家足够战术信息；蓄力、相性变化、环境机制和目标命刻应清楚提示。",
        ]
    if scene is None:
        return [
            "自由场景不是长期状态；如果玩家开始行动，应尽快明确时间、地点、登场角色和焦点问题。",
            "若玩家只是讨论计划，可保持轻量叙事；若他们决定执行计划，切入标准场景、幕间或冲突场景。",
        ]
    if scene.scene_type == SceneType.INTERLUDE:
        return [
            "幕间场景用于旅行、等待、休整、长期工程或洞穴穿行等较慢节奏。",
            "让每名玩家概括自己的任务；若有人想详细互动，就放大成标准场景。",
            "幕间适合推进工程、旅途、关系、线索和长期目标，不必逐秒描写。",
        ]
    if scene.scene_type == SceneType.GM:
        return [
            "主持人场景不包含玩家角色，适合短暂铺垫反派行动、预告威胁或制造戏剧悬念。",
            "保持像过场动画一样简短，不让它替代玩家选择，也不要泄露不该公开的暗线。",
        ]
    if scene.scene_type == SceneType.REST:
        return [
            "休息场景应处理恢复、羁绊、补给、界限与帷幕后的安顿，以及反派/威胁是否趁机推进。",
            "若休息地点不安全，可用威胁命刻或代价表现压力。",
        ]
    if scene.scene_type == SceneType.TRAVEL:
        return [
            "旅行场景应按旅行日、危险、发现和路线选择推进；不要把每一步都变成检定。",
            "发现可以引入地点、NPC、捷径、宝藏或预备地点线索；危险应带来有意义的代价或选择。",
        ]
    if scene.scene_type == SceneType.DUNGEON:
        return [
            "地下城场景应围绕探索目的、危险命刻、奖励分布和地点故事推进。",
            "不要预设唯一解法；陷阱、谜题、敌人和宝箱都应服务于这个地点要讲的旧故事。",
        ]
    if scene.scene_type == SceneType.SESSION_ZERO:
        return [
            "Session 0 是共创流程，不是冒险场景；按开团共识、创建世界、小队、界限与帷幕、角色、第一幕推进。",
            "允许玩家跳着提出点子，但主动引导时回到最早未完成的必要项。",
        ]
    return [
        "标准场景应围绕一个具体问题展开；只在风险、阻碍或规则要求时检定。",
        "当问题解决或镜头切换时结束场景，不要把所有对话无限拖在同一个场景里。",
    ]


def _scene_type_label(scene_type: SceneType) -> str:
    return {
        SceneType.STANDARD: "标准场景",
        SceneType.SESSION_ZERO: "Session 0",
        SceneType.CONFLICT: "冲突场景",
        SceneType.INTERLUDE: "幕间场景",
        SceneType.GM: "主持人场景",
        SceneType.REST: "休息场景",
        SceneType.TRAVEL: "旅行场景",
        SceneType.DUNGEON: "地下城场景",
    }[scene_type]
