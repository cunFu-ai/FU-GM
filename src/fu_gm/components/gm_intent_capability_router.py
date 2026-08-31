from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

from fu_gm.gm_tool_contracts import GMToolExecutionContext
from fu_gm.components.gm_supervisor import GMCapabilityBroker
from fu_gm.skill_library import CORE_CLASS_NAMES


@dataclass(frozen=True)
class GMIntentCapabilityProfile:
    """A fixed, cache-friendly bundle of tools and state projections."""

    profile_id: str
    tool_names: frozenset[str]
    state_scopes: frozenset[str]


@dataclass(frozen=True)
class GMIntentCapabilityPlan:
    """Conservative schema and state plan for one core-model request.

    The router grants no authority.  ``tool_names`` is only a model-visible
    subset and is always capped by both the trusted phase policy and the live
    registry.  The execution layer must continue to enforce its normal
    admission, argument, transaction, and receipt checks.
    """

    profile_ids: tuple[str, ...]
    tool_names: tuple[str, ...]
    state_scopes: tuple[str, ...]
    subjects: tuple[str, ...]
    confidence: float
    proofs: tuple[str, ...]
    fallback_discovery: bool


def _profile(
    profile_id: str,
    *,
    tools: Iterable[str],
    scopes: Iterable[str],
) -> GMIntentCapabilityProfile:
    return GMIntentCapabilityProfile(
        profile_id=profile_id,
        tool_names=frozenset(tools),
        state_scopes=frozenset(scopes),
    )


_PROFILES = (
    _profile(
        "ambiguous_hot",
        tools={
            "commit_scene_response",
            "commit_story_item_action",
            "create_npc_profile",
            "declare_check_action",
            "declare_movement_check",
            "decide_collective_response",
            "decide_npc_response",
            "discover_capabilities",
            "move_group_within_scene",
            "move_scene_group",
            "transition_scene",
            "perform_character_action",
            "perform_in_scene_action",
            "perform_ritual_project_action",
            "perform_scene_action",
        },
        scopes={"decisions", "gameplay", "kernel", "npcs", "scene", "speaker"},
    ),
    _profile(
        "bootstrap",
        tools={
            "discover_capabilities",
            "get_rule_reference",
            "inspect_supervisor_state",
            "search_rule_references",
        },
        scopes={"capability_catalog", "kernel", "speaker"},
    ),
    _profile(
        "campaign_admin",
        tools={
            "create_campaign",
            "delete_save",
            "end_session",
            "get_session_status",
            "inspect_campaign",
            "list_saves",
            "load_campaign",
            "pause_session",
            "record_safety_boundary",
            "save_campaign",
            "set_player_attendance",
            "start_adventure",
            "start_session",
        },
        scopes={"campaign", "kernel", "session", "speaker"},
    ),
    _profile(
        "check_action",
        tools={
            "declare_check_action",
            "perform_character_action",
            "perform_in_scene_action",
            "perform_ritual_project_action",
            "perform_scene_action",
        },
        scopes={"decisions", "gameplay", "kernel", "scene", "speaker"},
    ),
    _profile(
        "check_declare",
        tools={"declare_check_action"},
        scopes={"decisions", "gameplay", "kernel", "scene", "speaker"},
    ),
    _profile(
        "conflict",
        tools={
            "declare_check_action",
            "declare_movement_check",
            "end_conflict",
            "get_gameplay_state",
            "prepare_npc_combatant",
            "perform_character_action",
            "resolve_gm_opportunity",
            "resolve_rule_window",
            "run_current_npc_turn",
            "start_conflict",
        },
        scopes={"decisions", "gameplay", "kernel", "npcs", "scene", "speaker"},
    ),
    _profile(
        "movement",
        tools={
            "declare_movement_check",
            "focus_scene_branch",
            "move_group_within_scene",
            "move_scene_group",
            "perform_in_scene_action",
            "transition_scene",
        },
        scopes={"gameplay", "kernel", "map", "scene", "speaker"},
    ),
    _profile(
        "npc_response",
        tools={
            "create_npc_profile",
            "decide_collective_response",
            "decide_npc_response",
            "get_npc_profiles",
        },
        scopes={"decisions", "kernel", "npcs", "scene", "speaker"},
    ),
    _profile(
        "pending_window",
        tools={"get_gameplay_state", "resolve_rule_window"},
        scopes={"decisions", "gameplay", "kernel", "speaker"},
    ),
    _profile(
        "reply_only",
        tools=set(),
        scopes={"decisions", "kernel", "scene", "speaker"},
    ),
    _profile(
        "rule_read",
        tools={"get_rule_reference", "search_rule_references"},
        scopes={"kernel", "rules", "speaker"},
    ),
    _profile(
        "scene_lifecycle",
        tools={
            "end_scene",
            "focus_scene_branch",
            "get_scene_state",
            "start_scene",
            "transition_scene",
        },
        scopes={"gameplay", "kernel", "scene", "speaker"},
    ),
    _profile(
        "session_zero_ambiguous",
        tools=GMCapabilityBroker.session_zero_core_tool_names()
        | {"discover_capabilities"},
        scopes={"capability_catalog", "campaign", "kernel", "session", "speaker"},
    ),
    _profile(
        "session_zero_hero",
        tools={
            "confirm_hero_draft",
            "get_hero_drafts",
            "get_hero_state",
            "update_hero_draft",
        },
        scopes={"campaign", "kernel", "speaker"},
    ),
    _profile(
        "session_zero_nudge",
        tools={
            "mark_session_zero_topic_complete",
            "pause_session_zero_nudges",
            "set_session_zero_nudge_preference",
        },
        scopes={"campaign", "kernel", "speaker"},
    ),
    _profile(
        "session_zero_opening",
        tools={
            "get_session_zero_contributions",
            "get_session_zero_readiness",
            "record_prologue_setup_answer",
            "select_first_act",
            "set_chapter_one_transition",
            "start_adventure",
            "start_session",
        },
        scopes={"campaign", "kernel", "session", "speaker"},
    ),
    _profile(
        "session_zero_proposal",
        tools={
            "confirm_session_zero_proposal",
            "propose_session_zero_update",
            "query_world_settings",
        },
        scopes={"campaign", "kernel", "speaker"},
    ),
    _profile(
        "session_zero_safety",
        tools={"record_safety_boundary"},
        scopes={"campaign", "kernel", "speaker"},
    ),
    _profile(
        "session_zero_world",
        tools={
            "create_world_setting",
            "delete_world_setting",
            "find_map_location_candidates",
            "generate_world_map_preview",
            "get_session_zero_contributions",
            "mark_session_zero_topic_complete",
            "place_world_map_locations",
            "propose_session_zero_update",
            "query_world_settings",
            "rename_world_setting",
            "update_world_setting",
        },
        scopes={"campaign", "kernel", "map", "speaker"},
    ),
)

_PROFILE_BY_ID: Mapping[str, GMIntentCapabilityProfile] = MappingProxyType(
    {profile.profile_id: profile for profile in _PROFILES}
)


class GMIntentCapabilityRouter:
    """Select a small fixed capability profile without calling an LLM.

    Classification deliberately prefers an ambiguous/bootstrap fallback over
    guessing.  It treats the authoritative snapshot as an entity index, never
    as permission to execute the selected tools.
    """

    _NEGATIONS = (
        "并不是",
        "并非",
        "不是",
        "不要",
        "不用",
        "无需",
        "别",
        "不",
        "取消",
        "停止",
    )
    _NEGATION_LINKERS = ("", "要", "想", "会", "能", "再", "准备")
    # ``别`` is also the final character of ordinary lexical words.  Treating
    # those suffixes as the imperative negation sends genuine actions such as
    # “分别攻击” into the ambiguous fallback profile.
    _NON_NEGATING_BIE_WORDS = (
        "分别",
        "个别",
        "区别",
        "识别",
        "告别",
        "特别",
        "类别",
        "级别",
        "性别",
        "派别",
    )
    _QUESTION_CUES = (
        "?",
        "？",
        "吗",
        "么",
        "是否",
        "怎么",
        "如何",
        "多少",
        "什么",
        "哪",
        "能否",
        "可以不",
    )
    _ADMIN_TERMS = (
        "存档",
        "读档",
        "读取存档",
        "加载存档",
        "存档槽",
        "战役列表",
        "新建战役",
        "创建战役",
        "删除战役",
        "删除存档",
        "暂停游戏",
        "暂停本场",
        "结束本场",
        "结束游戏",
        "开始游戏",
        "开始冒险",
        "出席",
        "缺席",
        "安全边界",
        "界限与帷幕",
    )
    _ADMIN_ACTION_TERMS = (
        "帮我",
        "请",
        "现在",
        "立刻",
        "保存",
        "查看",
        "列出",
        "读取",
        "加载",
        "删除",
        "新建",
        "创建",
        "暂停",
        "结束",
        "开始",
        "设置",
        "记录",
        "切换",
    )
    _RULE_TERMS = (
        "规则",
        "检定公式",
        "怎么算",
        "如何计算",
        "职业技能",
        "英雄技能",
        "法术规则",
        "装备规则",
        "属性规则",
        "伤害规则",
        "命中规则",
        "规则书",
    )
    _RULE_READ_TERMS = ("查", "查询", "查看", "解释", "说明", "告诉我", "是什么")
    _SCENE_LIFECYCLE_TERMS = (
        "开始场景",
        "建立场景",
        "结束场景",
        "切换场景",
        "转场",
        "下一幕",
        "下一个场景",
        "聚焦场景",
        "切换镜头",
    )
    _MOVEMENT_TERMS = (
        "移动",
        "走到",
        "走过去",
        "跑到",
        "前往",
        "去往",
        "进入",
        "离开",
        "靠近",
        "撤退",
        "追上",
        "绕到",
        "退到",
        "赶到",
        "换到",
    )
    _CONFLICT_TERMS = (
        "攻击",
        "进攻",
        "开战",
        "战斗",
        "打他",
        "打她",
        "砍",
        "射击",
        "施放攻击",
        "结束冲突",
        "结束战斗",
    )
    _ACTION_TERMS = (
        "观察",
        "调查",
        "检查",
        "检定",
        "尝试",
        "搜索",
        "开锁",
        "潜行",
        "偷窃",
        "施法",
        "施放",
        "使用技能",
        "使用物品",
        "说服",
        "威胁",
        "治疗",
        "休息",
        "制作",
        "仪式",
    )
    _DIALOGUE_TERMS = (
        "询问",
        "问",
        "问问",
        "问他",
        "问她",
        "问它",
        "告诉",
        "对话",
        "交谈",
        "谈话",
        "聊聊",
        "回答",
        "回应",
        "喊话",
        "打招呼",
        "对他说",
        "对她说",
        "跟他说",
        "跟她说",
    )
    _FACT_TERMS = (
        "开始了吗",
        "结束了吗",
        "现在是",
        "当前是",
        "当前状态",
        "现在状态",
        "在哪里",
        "在哪儿",
        "谁在场",
        "有哪些人",
        "还在吗",
        "已经",
    )
    _SESSION_ZERO_SAFETY_TERMS = (
        "安全边界",
        "界限",
        "帷幕",
        "雷点",
        "不能接受",
        "淡出处理",
        "避开描写",
    )
    _SESSION_ZERO_HERO_TERMS = (
        "玩家名",
        "角色草稿",
        "角色卡",
        "正式建卡",
        "确认角色",
        "我的角色",
        "角色名字",
        "初始装备",
        "初始技能",
        "职业",
        "主题",
        "故乡",
        "属性",
        "技能",
        "法术",
        "羁绊",
        "背景",
        "仪式",
        "d6",
        "d8",
        "d10",
        "d12",
    )
    _SESSION_ZERO_HERO_ACTION_TERMS = (
        "我选",
        "选择",
        "先选",
        "再选",
        "技能选",
        "属性骰",
        "职业分配",
        "起始契约",
        "初始装备",
        "背景钩子",
        "羁绊：",
        "改成",
        "设为",
        "填写",
        "补上",
        "确认",
        "正式",
        "建卡",
    )
    _SESSION_ZERO_HERO_READ_SUBJECT_TERMS = (
        "角色草稿",
        "角色卡",
        "我的角色",
        "角色属性",
        "我的属性",
        "我的技能",
        "我的职业",
        "我的装备",
    )
    _SESSION_ZERO_HERO_READ_ACTION_TERMS = (
        "查看",
        "看看",
        "看一下",
        "查询",
        "显示",
        "列出",
        "给我看",
        "是什么",
        "有哪些",
        "多少",
    )
    _SESSION_ZERO_PROPOSAL_TERMS = (
        "我提议",
        "我的提议",
        "这个提案",
        "地图提案",
        "小队提案",
        "大家觉得",
        "你们觉得",
        "行不行",
        "要不要",
    )
    _SESSION_ZERO_CONFIRM_PROPOSAL_TERMS = (
        "我赞成",
        "我同意",
        "大家都赞成",
        "大家都同意",
        "就按",
        "确认这个提案",
        "确认该提案",
    )
    _SESSION_ZERO_OPENING_TERMS = (
        "进入第一章",
        "开始第一章",
        "开进第一章",
        "现在开团",
        "开始冒险",
        "第一幕",
        "开场共创",
        "开场地点",
        "开场目标",
        "序章",
        "准备好了吗",
        "还缺什么",
        "能开团了吗",
    )
    _SESSION_ZERO_NUDGE_TERMS = (
        "别再问",
        "不要再问",
        "停止点名",
        "暂停提问",
        "稍后回答",
        "让我想想",
        "先想想",
        "恢复提问",
        "继续问我",
    )
    _SESSION_ZERO_WORLD_TERMS = (
        "世界设定",
        "我贡献",
        "大陆",
        "国家",
        "王国",
        "公国",
        "地区",
        "地点",
        "地图",
        "历史事件",
        "重大历史",
        "奥秘",
        "谜团",
        "世界威胁",
        "危机",
        "魔法与科技",
        "魔法和科技",
        "魔法科技",
    )
    _SESSION_ZERO_WORLD_ACTION_TERMS = (
        "我贡献",
        "定为",
        "设为",
        "改成",
        "叫做",
        "位于",
        "记录",
        "写入",
        "跳过",
        "是",
        "可以并存",
    )
    @classmethod
    def profiles(cls) -> tuple[GMIntentCapabilityProfile, ...]:
        """Return the immutable profile catalog in stable id order."""

        return tuple(sorted(_PROFILES, key=lambda item: item.profile_id))

    @classmethod
    def route(
        cls,
        context: GMToolExecutionContext,
        full_state: Mapping[str, object],
        phase_tools: Iterable[str],
        registered_tools: Iterable[str],
    ) -> GMIntentCapabilityPlan:
        message = cls._current_message(context)
        state = full_state if isinstance(full_state, Mapping) else {}
        phase = cls._clean_names(phase_tools)
        registered = cls._clean_names(registered_tools)
        allowed = phase & registered

        pc_names = cls._player_character_names(context, state)
        controlled = cls._controlled_character_names(context, state)
        npc_aliases = cls._npc_aliases(state, pc_names)

        blocking = cls._owned_blocking_decision(
            context,
            state,
            controlled=controlled,
        )
        if blocking is not None:
            owner = str(blocking.get("owner") or "").strip()
            return cls._plan(
                ("pending_window",),
                allowed=allowed,
                subjects=(() if not owner or owner == "__gm__" else (owner,)),
                confidence=1.0,
                proofs=("authority:owned_blocking_decision",),
            )

        if not message:
            return cls._fallback_plan(
                state,
                allowed=allowed,
                proof="fallback:missing_message",
            )

        normalized = cls._normalize(message)
        negated_categories: set[str] = set()

        session_zero_unmatched = False
        if str(context.gate_status or "").strip() == "session_zero":
            session_zero_plan = cls._route_session_zero(
                normalized,
                allowed=allowed,
            )
            if session_zero_plan is not None:
                return session_zero_plan
            session_zero_unmatched = True

        if cls._positive_term_hit(normalized, cls._ADMIN_TERMS):
            # Campaign/table operations are only considered explicit when the
            # message also carries a request/action cue.  A sentence merely
            # mentioning an old save remains in the conservative fallback.
            if cls._term_hit(normalized, cls._ADMIN_ACTION_TERMS):
                return cls._plan(
                    ("campaign_admin",),
                    allowed=allowed,
                    subjects=(context.campaign_id,),
                    confidence=0.96,
                    proofs=("message:explicit_campaign_or_table_admin",),
                )
        elif cls._term_hit(normalized, cls._ADMIN_TERMS):
            negated_categories.add("campaign_admin")

        if cls._positive_term_hit(normalized, cls._RULE_TERMS) and (
            cls._question_like(normalized)
            or cls._positive_term_hit(normalized, cls._RULE_READ_TERMS)
        ):
            return cls._plan(
                ("rule_read",),
                allowed=allowed,
                confidence=0.95,
                proofs=("message:explicit_rule_lookup",),
            )
        if cls._term_hit(normalized, cls._RULE_TERMS):
            negated_categories.add("rule_read")

        if session_zero_unmatched:
            # Adventure verbs such as "冲突" or "移动" can occur while the
            # table is discussing tone and preferences.  They must not select
            # adventure-only schemas during Session Zero.  Preserve the full
            # legacy setup toolbox unless a setup-specific or read/admin
            # intent above was classified with high confidence.
            return cls._plan(
                ("session_zero_ambiguous",),
                allowed=allowed,
                confidence=0.3,
                proofs=("fallback:no_high_confidence_session_zero_intent",),
                fallback_discovery=True,
            )

        if cls._positive_term_hit(normalized, cls._SCENE_LIFECYCLE_TERMS):
            return cls._plan(
                ("scene_lifecycle",),
                allowed=allowed,
                confidence=0.94,
                proofs=("message:explicit_scene_lifecycle",),
            )
        if cls._term_hit(normalized, cls._SCENE_LIFECYCLE_TERMS):
            negated_categories.add("scene_lifecycle")

        mentioned_npcs = cls._mentioned_entities(normalized, npc_aliases)
        mentioned_pcs = cls._mentioned_player_characters(normalized, pc_names)
        conflict_hit = cls._positive_term_hit(normalized, cls._CONFLICT_TERMS)
        movement_hit = cls._positive_term_hit(normalized, cls._MOVEMENT_TERMS)
        action_hit = cls._positive_term_hit(normalized, cls._ACTION_TERMS)
        explicit_check_declaration = bool(
            "检定" in normalized
            and cls._positive_term_hit(
                normalized,
                ("声明", "等我确认", "不要替我投骰", "不要投骰"),
            )
        )
        dialogue_hit = cls._positive_term_hit(normalized, cls._DIALOGUE_TERMS)

        if not conflict_hit and cls._term_hit(normalized, cls._CONFLICT_TERMS):
            negated_categories.add("conflict")
        if not movement_hit and cls._term_hit(normalized, cls._MOVEMENT_TERMS):
            negated_categories.add("movement")
        if not action_hit and cls._term_hit(normalized, cls._ACTION_TERMS):
            negated_categories.add("check_action")
        if not dialogue_hit and cls._term_hit(normalized, cls._DIALOGUE_TERMS):
            negated_categories.add("npc_response")

        profiles: list[str] = []
        proofs: list[str] = []
        subjects: set[str] = set(mentioned_pcs)

        if conflict_hit:
            profiles.append("conflict")
            proofs.append("message:explicit_conflict_action")
            subjects.update(mentioned_npcs)
        elif action_hit:
            profiles.append(
                "check_declare"
                if explicit_check_declaration
                else "check_action"
            )
            proofs.append(
                "message:explicit_check_declaration"
                if explicit_check_declaration
                else "message:explicit_check_or_scene_action"
            )
            subjects.update(mentioned_npcs)

        if movement_hit:
            profiles.append("movement")
            proofs.append("message:explicit_movement")

        # Only identities already present in the authoritative NPC index (or
        # authoritative scene participants after removing every PC identity)
        # can select an NPC profile.  Prose that merely looks like a name is
        # intentionally insufficient.
        if dialogue_hit and mentioned_npcs:
            profiles.append("npc_response")
            proofs.append("authority:mentioned_npc_entity")
            proofs.append("message:explicit_npc_interaction")
            subjects.update(mentioned_npcs)

        if profiles:
            if not subjects and controlled:
                subjects.update(controlled)
            unique_profiles = tuple(sorted(set(profiles)))
            confidence = 0.93 if len(unique_profiles) == 1 else 0.84
            return cls._plan(
                unique_profiles,
                allowed=allowed,
                subjects=subjects,
                confidence=confidence,
                proofs=proofs,
            )

        if dialogue_hit and mentioned_pcs and not mentioned_npcs:
            return cls._plan(
                ("reply_only",),
                allowed=allowed,
                subjects=mentioned_pcs,
                confidence=0.92,
                proofs=("authority:player_character_not_npc",),
            )

        fact_confirmation = bool(
            cls._positive_term_hit(normalized, ("确认",))
            and cls._term_hit(normalized, cls._FACT_TERMS)
        )
        if (
            (cls._question_like(normalized) or fact_confirmation)
            and not (dialogue_hit and not mentioned_npcs and not mentioned_pcs)
            and (
            cls._term_hit(normalized, cls._FACT_TERMS)
            or bool(mentioned_npcs)
            or bool(mentioned_pcs)
            )
        ):
            return cls._plan(
                ("reply_only",),
                allowed=allowed,
                subjects=(*mentioned_pcs, *mentioned_npcs),
                confidence=0.88,
                proofs=("message:authoritative_fact_question",),
            )

        fallback_proof = (
            "fallback:negated_candidate_intent"
            if negated_categories
            else "fallback:no_high_confidence_intent"
        )
        if str(context.gate_status or "").strip() == "session_zero":
            return cls._plan(
                ("session_zero_ambiguous",),
                allowed=allowed,
                confidence=0.3,
                proofs=(fallback_proof,),
                fallback_discovery=True,
            )
        return cls._fallback_plan(state, allowed=allowed, proof=fallback_proof)

    @classmethod
    def _route_session_zero(
        cls,
        message: str,
        *,
        allowed: set[str],
    ) -> GMIntentCapabilityPlan | None:
        """Select only high-confidence, fixed Session Zero schema bundles.

        This classifier intentionally leaves ambiguous contributions to the
        legacy Session Zero hot set.  It is a prompt-size optimization, never
        an authority or completeness classifier; the normal tool admission,
        message-integrity, proposal, and transaction policies remain decisive.
        """

        profile_ids: list[str] = []
        proofs: list[str] = []

        def select(profile_id: str, proof: str) -> None:
            profile_ids.append(profile_id)
            proofs.append(proof)

        class_rule_hit = (
            any(class_name in message for class_name in CORE_CLASS_NAMES)
            and any(term in message for term in ("技能", "法术", "职业"))
            and cls._question_like(message)
        )
        if class_rule_hit:
            return cls._plan(
                ("rule_read",),
                allowed=allowed,
                confidence=0.96,
                proofs=("message:explicit_core_class_rule_lookup",),
            )

        safety_hit = cls._positive_term_hit(
            message,
            cls._SESSION_ZERO_SAFETY_TERMS,
        )
        opening_hit = cls._positive_term_hit(
            message,
            cls._SESSION_ZERO_OPENING_TERMS,
        )
        explicit_proposal_hit = cls._positive_term_hit(
            message,
            cls._SESSION_ZERO_PROPOSAL_TERMS,
        )
        proposal_confirmation_hit = cls._positive_term_hit(
            message,
            cls._SESSION_ZERO_CONFIRM_PROPOSAL_TERMS,
        )
        # Phrases such as "大家都同意，现在进入第一章" confirm the table
        # transition, not necessarily an old proposal.  Only retain the
        # proposal bundle alongside an opening when the message also names a
        # proposal-like subject.
        if opening_hit and proposal_confirmation_hit and not any(
            term in message
            for term in ("提案", "轮廓", "地图", "小队方向", "世界方向")
        ):
            proposal_confirmation_hit = False

        if safety_hit:
            select(
                "session_zero_safety",
                "message:explicit_session_zero_safety",
            )
        if opening_hit:
            select(
                "session_zero_opening",
                "message:explicit_session_zero_opening",
            )
        if explicit_proposal_hit or proposal_confirmation_hit:
            select(
                "session_zero_proposal",
                "message:explicit_session_zero_proposal",
            )
        if cls._positive_term_hit(message, cls._SESSION_ZERO_NUDGE_TERMS):
            select(
                "session_zero_nudge",
                "message:explicit_session_zero_nudge_preference",
            )

        hero_read_hit = cls._positive_term_hit(
            message,
            cls._SESSION_ZERO_HERO_READ_SUBJECT_TERMS,
        ) and cls._positive_term_hit(
            message,
            cls._SESSION_ZERO_HERO_READ_ACTION_TERMS,
        )
        if hero_read_hit:
            select(
                "session_zero_hero",
                "message:explicit_session_zero_hero_read",
            )

        if (
            cls._declarative_term_pair(
                message,
                subject_terms=cls._SESSION_ZERO_HERO_TERMS,
                action_terms=cls._SESSION_ZERO_HERO_ACTION_TERMS,
            )
        ):
            select(
                "session_zero_hero",
                "message:explicit_session_zero_hero_edit",
            )

        if (
            cls._declarative_term_pair(
                message,
                subject_terms=cls._SESSION_ZERO_WORLD_TERMS,
                action_terms=cls._SESSION_ZERO_WORLD_ACTION_TERMS,
            )
        ):
            select(
                "session_zero_world",
                "message:explicit_session_zero_world_contribution",
            )

        if not profile_ids:
            return None
        return cls._plan(
            profile_ids,
            allowed=allowed,
            confidence=0.94 if len(set(profile_ids)) == 1 else 0.9,
            proofs=proofs,
        )

    @classmethod
    def _declarative_term_pair(
        cls,
        message: str,
        *,
        subject_terms: Iterable[str],
        action_terms: Iterable[str],
    ) -> bool:
        """Match a declared edit without letting another clause's question hide it."""

        clauses = [
            clause.strip()
            for clause in re.split(r"[。；;！!\n]+", str(message or ""))
            if clause.strip()
        ]
        for clause in clauses or [str(message or "")]:
            if cls._question_like(clause):
                continue
            if cls._positive_term_hit(
                clause,
                subject_terms,
            ) and cls._positive_term_hit(clause, action_terms):
                return True
        return False

    @classmethod
    def _plan(
        cls,
        profile_ids: Iterable[str],
        *,
        allowed: set[str],
        subjects: Iterable[str] = (),
        confidence: float,
        proofs: Iterable[str],
        fallback_discovery: bool = False,
    ) -> GMIntentCapabilityPlan:
        selected_ids = tuple(
            sorted(
                {
                    str(profile_id or "").strip()
                    for profile_id in profile_ids
                    if str(profile_id or "").strip() in _PROFILE_BY_ID
                }
            )
        )
        requested_tools: set[str] = set()
        state_scopes: set[str] = set()
        for profile_id in selected_ids:
            profile = _PROFILE_BY_ID[profile_id]
            requested_tools.update(profile.tool_names)
            state_scopes.update(profile.state_scopes)
        effective_tools = requested_tools & allowed

        clean_proofs = {
            str(proof or "").strip()
            for proof in proofs
            if str(proof or "").strip()
        }
        needs_discovery = bool(fallback_discovery)
        if requested_tools and not effective_tools:
            needs_discovery = True
            clean_proofs.add("fallback:selected_profile_unavailable")
        if needs_discovery and "discover_capabilities" in allowed:
            effective_tools.add("discover_capabilities")

        clean_subjects = {
            str(subject or "").strip()
            for subject in subjects
            if str(subject or "").strip()
        }
        return GMIntentCapabilityPlan(
            profile_ids=selected_ids,
            tool_names=tuple(sorted(effective_tools)),
            state_scopes=tuple(sorted(state_scopes)),
            subjects=tuple(sorted(clean_subjects, key=lambda item: (item.casefold(), item))),
            confidence=max(0.0, min(1.0, float(confidence))),
            proofs=tuple(sorted(clean_proofs)),
            fallback_discovery=needs_discovery,
        )

    @classmethod
    def _fallback_plan(
        cls,
        state: Mapping[str, object],
        *,
        allowed: set[str],
        proof: str,
    ) -> GMIntentCapabilityPlan:
        profile_id = "ambiguous_hot" if cls._has_authoritative_scene(state) else "bootstrap"
        return cls._plan(
            (profile_id,),
            allowed=allowed,
            confidence=(0.35 if profile_id == "ambiguous_hot" else 0.2),
            proofs=(proof,),
            fallback_discovery=True,
        )

    @staticmethod
    def _clean_names(values: Iterable[str]) -> set[str]:
        return {
            str(value or "").strip()
            for value in values
            if str(value or "").strip()
        }

    @staticmethod
    def _mapping(value: object) -> Mapping[str, object]:
        return value if isinstance(value, Mapping) else {}

    @classmethod
    def _current_message(cls, context: GMToolExecutionContext) -> str:
        message = str(context.metadata.get("current_message") or "").strip()
        if message:
            return message
        raw_events = context.metadata.get("current_turn_events")
        if not isinstance(raw_events, list):
            return ""
        for event in reversed(raw_events):
            if not isinstance(event, Mapping):
                continue
            text = str(event.get("text") or "").strip()
            if text:
                return text
        return ""

    @staticmethod
    def _normalize(value: object) -> str:
        return "".join(str(value or "").casefold().split())

    @staticmethod
    def _term_hit(text: str, terms: Iterable[str]) -> bool:
        return any(GMIntentCapabilityRouter._normalize(term) in text for term in terms)

    @classmethod
    def _positive_term_hit(cls, text: str, terms: Iterable[str]) -> bool:
        for raw_term in terms:
            term = cls._normalize(raw_term)
            offset = text.find(term)
            while offset >= 0:
                prefix = text[max(0, offset - 6) : offset]
                negated = cls._prefix_ends_with_negation(prefix)
                if not negated:
                    return True
                offset = text.find(term, offset + len(term))
        return False

    @classmethod
    def _prefix_ends_with_negation(cls, prefix: str) -> bool:
        for raw_marker in cls._NEGATIONS:
            marker = cls._normalize(raw_marker)
            for raw_linker in cls._NEGATION_LINKERS:
                linker = cls._normalize(raw_linker)
                if not prefix.endswith(marker + linker):
                    continue
                if marker == "别":
                    lexical_prefix = prefix[: len(prefix) - len(linker)] if linker else prefix
                    if any(
                        lexical_prefix.endswith(cls._normalize(word))
                        for word in cls._NON_NEGATING_BIE_WORDS
                    ):
                        continue
                return True
        return False

    @classmethod
    def _question_like(cls, text: str) -> bool:
        return cls._term_hit(text, cls._QUESTION_CUES)

    @classmethod
    def _player_character_names(
        cls,
        context: GMToolExecutionContext,
        state: Mapping[str, object],
    ) -> set[str]:
        names: set[str] = {str(context.speaker or "").strip()}
        gameplay = cls._mapping(state.get("gameplay"))
        for value in list(gameplay.get("controlled_characters") or []):
            names.add(str(value or "").strip())
        for row in list(gameplay.get("characters") or []):
            if isinstance(row, Mapping):
                names.add(str(row.get("name") or "").strip())
        for player, values in cls._mapping(
            gameplay.get("player_character_aliases")
        ).items():
            names.add(str(player or "").strip())
            if isinstance(values, (list, tuple, set, frozenset)):
                names.update(str(value or "").strip() for value in values)

        turn = cls._mapping(state.get("turn_participants"))
        for key in (
            "controlled_characters_by_speaker",
            "player_character_aliases",
        ):
            for player, values in cls._mapping(turn.get(key)).items():
                names.add(str(player or "").strip())
                if isinstance(values, (list, tuple, set, frozenset)):
                    names.update(str(value or "").strip() for value in values)

        draft_rows: list[object] = list(state.get("hero_drafts") or [])
        session_zero = cls._mapping(state.get("session_zero"))
        draft_rows.extend(list(session_zero.get("hero_drafts") or []))
        for row in draft_rows:
            if not isinstance(row, Mapping):
                continue
            names.add(str(row.get("player_name") or "").strip())
            names.add(str(row.get("hero_name") or "").strip())
        return {name for name in names if name}

    @classmethod
    def _controlled_character_names(
        cls,
        context: GMToolExecutionContext,
        state: Mapping[str, object],
    ) -> set[str]:
        gameplay = cls._mapping(state.get("gameplay"))
        controlled = {
            str(value or "").strip()
            for value in list(gameplay.get("controlled_characters") or [])
            if str(value or "").strip()
        }
        aliases = cls._mapping(gameplay.get("player_character_aliases"))
        for value in list(aliases.get(context.speaker) or []):
            if str(value or "").strip():
                controlled.add(str(value or "").strip())
        turn = cls._mapping(state.get("turn_participants"))
        controls = cls._mapping(turn.get("controlled_characters_by_speaker"))
        for value in list(controls.get(context.speaker) or []):
            if str(value or "").strip():
                controlled.add(str(value or "").strip())
        return controlled

    @classmethod
    def _npc_aliases(
        cls,
        state: Mapping[str, object],
        pc_names: set[str],
    ) -> dict[str, str]:
        pc_keys = {cls._normalize(name) for name in pc_names if cls._normalize(name)}
        aliases: dict[str, str] = {}
        npc_state = cls._mapping(state.get("npcs"))
        rows: list[object] = []
        for key in ("present_npcs", "known_npc_index", "relevant_npcs"):
            raw_rows = npc_state.get(key)
            if isinstance(raw_rows, list):
                rows.extend(raw_rows)
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            canonical = str(row.get("name") or "").strip()
            candidates = [canonical]
            raw_aliases = row.get("aliases")
            if isinstance(raw_aliases, (list, tuple, set, frozenset)):
                candidates.extend(str(value or "").strip() for value in raw_aliases)
            normalized_candidates = {
                cls._normalize(candidate)
                for candidate in candidates
                if cls._normalize(candidate)
            }
            # A legacy persona that collides by stable name or alias with a PC
            # is never eligible for an NPC capability profile.
            if not canonical or normalized_candidates & pc_keys:
                continue
            for candidate in candidates:
                key = cls._normalize(candidate)
                if key:
                    aliases[key] = canonical

        scene_names: set[str] = set()
        scene = cls._mapping(state.get("scene"))
        scene_names.update(
            str(value or "").strip()
            for value in list(scene.get("participants") or [])
        )
        gameplay = cls._mapping(state.get("gameplay"))
        current_scene = cls._mapping(gameplay.get("current_scene"))
        scene_names.update(
            str(value or "").strip()
            for value in list(current_scene.get("participants") or [])
        )
        for name in scene_names:
            key = cls._normalize(name)
            if key and key not in pc_keys:
                aliases.setdefault(key, name)
        return aliases

    @classmethod
    def _mentioned_entities(
        cls,
        message: str,
        aliases: Mapping[str, str],
    ) -> tuple[str, ...]:
        matched = {
            canonical
            for alias, canonical in aliases.items()
            if cls._contains_identity(message, alias)
        }
        return tuple(sorted(matched, key=lambda item: (item.casefold(), item)))

    @classmethod
    def _mentioned_player_characters(
        cls,
        message: str,
        pc_names: set[str],
    ) -> tuple[str, ...]:
        matched = {
            name
            for name in pc_names
            if cls._contains_identity(message, cls._normalize(name))
        }
        return tuple(sorted(matched, key=lambda item: (item.casefold(), item)))

    @staticmethod
    def _contains_identity(message: str, identity: str) -> bool:
        if not identity or identity not in message:
            return False
        if not identity.isascii() or not identity.isalnum():
            return True
        start = message.find(identity)
        while start >= 0:
            before = message[start - 1] if start > 0 else ""
            end = start + len(identity)
            after = message[end] if end < len(message) else ""
            if not before.isalnum() and not after.isalnum():
                return True
            start = message.find(identity, start + len(identity))
        return False

    @classmethod
    def _owned_blocking_decision(
        cls,
        context: GMToolExecutionContext,
        state: Mapping[str, object],
        *,
        controlled: set[str],
    ) -> Mapping[str, object] | None:
        candidates: list[Mapping[str, object]] = []
        gameplay = cls._mapping(state.get("gameplay"))
        for item in list(gameplay.get("pending_decisions") or []):
            if isinstance(item, Mapping):
                candidates.append(item)
        processes = cls._mapping(state.get("processes"))
        decisions = cls._mapping(processes.get("decisions"))
        for item in list(decisions.get("pending") or []):
            if isinstance(item, Mapping):
                candidates.append(item)

        eligible = {context.speaker, *controlled}
        for item in sorted(
            candidates,
            key=lambda row: (
                str(row.get("window_id") or ""),
                str(row.get("kind") or ""),
            ),
        ):
            if not bool(item.get("blocking")):
                continue
            owner = str(item.get("owner") or "").strip()
            allowed: set[str] = set()
            for key in ("allowed_responders", "allowed_speakers"):
                raw = item.get(key)
                if isinstance(raw, (list, tuple, set, frozenset)):
                    allowed.update(str(value or "").strip() for value in raw)
            if owner in eligible or bool(allowed & eligible):
                return item
        return None

    @classmethod
    def _has_authoritative_scene(cls, state: Mapping[str, object]) -> bool:
        scene = cls._mapping(state.get("scene"))
        if bool(scene.get("active")) or str(scene.get("scene_id") or "").strip():
            return True
        gameplay = cls._mapping(state.get("gameplay"))
        current_scene = cls._mapping(gameplay.get("current_scene"))
        if (
            str(current_scene.get("name") or "").strip()
            or str(current_scene.get("location") or "").strip()
            or bool(current_scene.get("participants"))
        ):
            return True
        processes = cls._mapping(state.get("processes"))
        process_scene = cls._mapping(processes.get("scene"))
        return bool(process_scene.get("authoritative_active"))


__all__ = [
    "GMIntentCapabilityPlan",
    "GMIntentCapabilityProfile",
    "GMIntentCapabilityRouter",
]
