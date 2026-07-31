from __future__ import annotations

from collections import Counter
import copy
from typing import Any, Protocol

from fu_gm.components.clock_narrative_boundary import ClockNarrativeBoundary
from fu_gm.components.encounter_manager import EncounterManager
from fu_gm.components.npc_response_window_manager import NPCResponseWindowManager
from fu_gm.components.npc_speech_plan import (
    PUBLIC_SEGMENT_INPUT_TAGS,
    normalize_public_segments,
    normalize_speech_plan,
    render_public_segments,
)
from fu_gm.gm_evidence import is_current_message_evidence
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolPacingEvent,
    GMToolReceipt,
    GMToolRegistry,
)
from fu_gm.gm_tool_receipts import GMToolReceiptPolicy
from fu_gm.models import (
    Affinity,
    Character,
    EnemyRank,
    EscalationStage,
    SpellEffectType,
)
from fu_gm.npc_design_library import (
    NPC_SKILL_INDEX,
    normalize_affinity,
    normalize_damage_type,
    normalize_status,
)
from fu_gm.skill_library import (
    SKILL_COVERAGE_HARD_RULE,
    SKILL_COVERAGE_PASSIVE_HARD,
    get_skill_reference,
    normalize_skill_reference_name,
    skill_implementation_coverage,
)
from fu_gm.spellbook import (
    get_spell_definition,
    is_known_spell,
    normalize_spell_name,
)


class NPCToolHost(Protocol):
    def _runtime(self, campaign_id: str, *, auto_load: bool = True) -> Any: ...

    def _autosave_campaign(self, runtime: Any, campaign_id: str) -> str: ...


class GMNPCToolService:
    """NPC profile, state and dialogue transactions for the GM agent."""

    _PROFILE_SCALARS = {
        "entity_kind",
        "public_identity",
        "role_in_story",
        "core_drive",
        "manner",
        "speech_style",
        "combat_style",
        "npc_rank",
        "leverage",
        "authority_scope",
        "knowledge_scope",
        "refusal_move",
        "active_goal",
        "current_mood",
        "current_stance",
    }
    _PROFILE_LISTS = {
        "aliases",
        "goals",
        "taboos",
        "secrets",
        "voice_examples",
        "known_skills",
        "combat_actions",
    }
    _RANKS = {"minor", "supporting", "elite", "villain", "boss"}
    _ENTITY_KINDS = {"individual", "collective"}
    _ATTRIBUTE_ALIASES = {
        "敏捷": "DEX",
        "洞察": "INS",
        "力量": "MIG",
        "意志": "WLP",
        "DEX": "DEX",
        "INS": "INS",
        "MIG": "MIG",
        "WLP": "WLP",
    }
    _ATTRIBUTE_LABELS = {"DEX": "敏捷", "INS": "洞察", "MIG": "力量", "WLP": "意志"}

    def __init__(self, host: NPCToolHost) -> None:
        self.host = host

    @classmethod
    def _profile_schema_details(cls) -> dict[str, object]:
        return {
            "properties": {
                **{
                    field: {"type": "string"}
                    for field in cls._PROFILE_SCALARS
                    if field not in {"entity_kind", "npc_rank"}
                },
                "entity_kind": {
                    "type": "string",
                    "enum": sorted(cls._ENTITY_KINDS),
                },
                "npc_rank": {
                    "type": "string",
                    "enum": sorted(cls._RANKS),
                },
                **{
                    field: {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 20,
                    }
                    for field in cls._PROFILE_LISTS
                },
            },
            "additionalProperties": False,
        }

    @classmethod
    def _profile_parameter(cls) -> GMToolParameter:
        return GMToolParameter(
            "profile",
            "object",
            "结构化人格、权限、目标及可选战斗能力；只能使用schema列出的字段。",
            required=True,
            schema_details=cls._profile_schema_details(),
        )

    @classmethod
    def _direct_npc_output_parameters(cls) -> tuple[GMToolParameter, ...]:
        """Schema for one NPC transaction authored by the core GM."""

        return (
            GMToolParameter(
                "public_segments",
                "array",
                (
                    "核心GM已经决定好的完整公开回应，程序按顺序直接拼接发送。"
                    "动作与台词分段；同一事实只写一次。tags只标记结构意义，不会展示给玩家。"
                    "简单答复可以省略tags。新条件优先用gate_requirement；"
                    "若同时把new_gate写成tag，规则层会规范化为gate_requirement。"
                    "player_request只标记NPC直接要求某个PC或整队回答的短问题，"
                    "不能标记NPC自己的答复、对另一NPC的要求或未来条件。"
                ),
                required=True,
                schema_details={
                    "minItems": 1,
                    "maxItems": 12,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "minLength": 1},
                            "text": {"type": "string", "minLength": 1},
                            "tags": {
                                "type": "array",
                                "items": {
                                    "type": "string",
                                    "enum": sorted(
                                        PUBLIC_SEGMENT_INPUT_TAGS
                                    ),
                                },
                                "uniqueItems": True,
                            },
                        },
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                },
            ),
            GMToolParameter(
                "speech_act",
                "string",
                "本次主要言语行为。",
                enum=("answer", "refuse", "new_gate", "admit_unknown", "deflect"),
            ),
            GMToolParameter(
                "condition_outcome",
                "string",
                "只表示传入condition_id对应的既有公开条件状态。",
                enum=("none", "fulfilled", "incomplete", "rejected"),
            ),
            GMToolParameter(
                "proposal_outcome",
                "string",
                "NPC对玩家本轮完整方案的态度。",
                enum=("none", "accepted", "rejected", "countered"),
            ),
            GMToolParameter("stance", "string", "回应后NPC当前公开立场。"),
            GMToolParameter("intent", "string", "回应后NPC当前目标。"),
            GMToolParameter("emotion", "string", "回应后NPC当前情绪。"),
            GMToolParameter(
                "promise_kind",
                "string",
                "只有本轮确实产生承诺时填写其类型。",
                enum=("none", "access", "escort", "disclose", "item", "aid", "other"),
            ),
            GMToolParameter("promise_subject", "string", "承诺涉及的人、地点或物件。"),
            GMToolParameter(
                "commitment_outcome",
                "string",
                "只表示commitment_id对应承诺的处理结果。",
                enum=("none", "fulfilled", "cancelled"),
            ),
            GMToolParameter(
                "response_addressee",
                "string",
                "只有player_request必须由某个已知玩家角色回答时填写；问整队则留空。",
            ),
            GMToolParameter(
                "introduced_npcs",
                "array",
                "可选；仅限该NPC有权立即带入现场、且公开文本确实点名的普通部属或随从。",
                schema_details={
                    "maxItems": 2,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "minLength": 1},
                            "profile": cls._profile_schema_details(),
                        },
                        "required": ["name", "profile"],
                        "additionalProperties": False,
                    },
                },
            ),
        )

    def register_tools(self, registry: GMToolRegistry) -> None:
        registry.register(
            GMToolDefinition(
                name="get_npc_profiles",
                description="读取当前场景NPC档案、当前动机、权限、知识边界与近期公开答复。",
                handler=self.get_npc_profiles,
                parameters=(
                    GMToolParameter("names", "array", "可选；只读取这些NPC或别名。"),
                    GMToolParameter("include_private", "boolean", "是否给GM决策层返回秘密和内部动机。"),
                ),
            )
        )
        registry.register(
            GMToolDefinition(
                name="create_npc_profile",
                description=(
                    "当一个NPC已经实际进入当前场景或确定即将登场时建立持久档案。"
                    "如果场景启动只留下了占位档案，本工具会原子补全它。"
                    "不要为玩家随口假设、举例或尚未出现的人物建档。"
                ),
                handler=self.create_npc_profile,
                parameters=(
                    GMToolParameter("name", "string", "NPC稳定名称。", required=True),
                    self._profile_parameter(),
                    GMToolParameter("present_in_scene", "boolean", "是否已在当前场景中出现。", required=True),
                    GMToolParameter("planned_entry", "boolean", "是否为GM已确定会登场、但尚未公开出现的私有准备。"),
                    GMToolParameter("evidence", "string", "当前消息中触发其登场或建档的逐字证据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="introduce_npc",
                description=(
                    "让一个尚未在场的NPC实际进入当前场景，并在同一事务中建立持久人格档案、"
                    "加入在场名单、记录公开登场与写入场景记忆。适合GM主动节拍；"
                    "不能用来预建未登场人物，也不能只建档而不让玩家看见其出现。"
                ),
                handler=self.introduce_npc,
                parameters=(
                    GMToolParameter("name", "string", "NPC稳定名称。", required=True),
                    self._profile_parameter(),
                    GMToolParameter(
                        "public_reply",
                        "string",
                        "将原样发给玩家的完整登场描述；必须出现NPC稳定名称或公开身份。",
                        required=True,
                        schema_details={"minLength": 1},
                    ),
                    GMToolParameter(
                        "public_facts",
                        "array",
                        (
                            "可选；只填写从public_reply逐字复制的持久事实句。"
                            "无法逐字复制时提交空数组。"
                        ),
                        schema_details={
                            "items": {"type": "string", "minLength": 1},
                            "maxItems": 8,
                        },
                    ),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前消息中触发登场的逐字证据；系统主动节拍由调度器授权。",
                        required=True,
                        source="current_message",
                    ),
                ),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )
        registry.register(
            GMToolDefinition(
                name="preview_npc_combatant",
                description=(
                    "按核心规则预览NPC战斗骨架和技能预算，不写入状态。"
                    "先预览，再按返回的skill_budget选择恰好对应数量的NPC或职业技能。"
                ),
                handler=self.preview_npc_combatant,
                parameters=self._combatant_parameters(include_skills=False),
            )
        )
        registry.register(
            GMToolDefinition(
                name="create_npc_combatant",
                description=(
                    "把已有NPC人格档案建立为可参与规则结算的敌人或完整回合盟友。"
                    "属性、HP/MP、防御、相性、等级修正和阶级行动数由规则层计算。"
                ),
                handler=self.create_npc_combatant,
                parameters=self._combatant_parameters(include_skills=True),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="configure_boss_phases",
                description=(
                    "为已有规则战斗档案的反派配置一到两个尚未公开的后续首领形态。"
                    "每个形态会在当前形态生命值降为0时恢复生命值和精神值，并可改变相性、"
                    "施加状态、增加已知法术、改变每轮行动次数；默认给予英雄一轮准备行动。"
                    "这不是反派升级，不补充终结点，也不奖励物语点。"
                    "只能配置仍未开始转阶段的反派；独特新技能不能只写名称，须由专门规则机制实现。"
                ),
                handler=self.configure_boss_phases,
                parameters=(
                    GMToolParameter(
                        "name",
                        "string",
                        "已有规则战斗档案且拥有终结点的反派名称或别名。",
                        required=True,
                    ),
                    GMToolParameter(
                        "phases",
                        "array",
                        "按触发顺序排列的一到两个后续形态。",
                        required=True,
                        schema_details={
                            "minItems": 1,
                            "maxItems": 2,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string", "minLength": 1},
                                    "public_cue": {
                                        "type": "string",
                                        "minLength": 1,
                                        "description": "转阶段时向玩家公开的可观察变化。",
                                    },
                                    "hp_restore": {
                                        "type": "integer",
                                        "minimum": 1,
                                        "description": "省略则恢复至最大生命值。",
                                    },
                                    "mp_restore": {
                                        "type": "integer",
                                        "minimum": 0,
                                        "description": "省略则恢复至最大精神值。",
                                    },
                                    "added_statuses": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "maxItems": 6,
                                    },
                                    "affinity_changes": {
                                        "type": "object",
                                        "additionalProperties": {
                                            "type": "string",
                                            "enum": [
                                                "normal",
                                                "weak",
                                                "resist",
                                                "immune",
                                                "absorb",
                                            ],
                                        },
                                    },
                                    "added_spells": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "maxItems": 8,
                                    },
                                    "action_count": {
                                        "type": "integer",
                                        "minimum": 1,
                                        "maximum": 10,
                                    },
                                    "preferred_actions": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "maxItems": 8,
                                    },
                                    "tactic_hints": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "maxItems": 8,
                                    },
                                    "preparation_round": {
                                        "type": "boolean",
                                        "description": "默认true；转阶段后英雄各获得一次准备行动。",
                                    },
                                },
                                "required": ["name", "public_cue"],
                                "additionalProperties": False,
                            },
                        },
                    ),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前消息或GM准备节拍中支持设计多阶段首领的逐字依据。",
                        required=True,
                        source="current_message",
                    ),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="update_npc_state",
                description="根据已经发生的局面更新现有NPC的当前位置、情绪、立场、当前目标、关系或状态。",
                handler=self.update_npc_state,
                parameters=(
                    GMToolParameter("name", "string", "现有NPC名称或别名。", required=True),
                    GMToolParameter("patch", "object", "只提交本轮确实改变的动态字段。", required=True),
                    GMToolParameter("evidence", "string", "当前消息中的逐字依据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="revise_npc_profile",
                description=(
                    "根据玩家明确纠正或已经发生的揭示，修订现有NPC的稳定档案。"
                    "标量放在set中；技能、目标、秘密等新增条目放在add中。"
                ),
                handler=self.revise_npc_profile,
                parameters=(
                    GMToolParameter("name", "string", "现有NPC名称或别名。", required=True),
                    GMToolParameter("set", "object", "要替换的稳定标量字段。"),
                    GMToolParameter("add", "object", "要追加的列表字段及条目数组。"),
                    GMToolParameter("evidence", "string", "当前消息中的逐字依据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="decide_npc_response",
                description=(
                    "由核心GM直接决定并提交当前场景中一个现有NPC的完整回应。"
                    "current_state_summary.npcs已提供NPC私有人格、动机、权限、知识边界、近期言行与现场权威状态；"
                    "本工具不会再次调用NPC模型，只校验公开片段、承诺、条件、待答窗口和场景权限后落地。"
                    "此工具处理无需先检定、且玩家已经实际说出口或提交的直接对话，也处理伸手搀扶、示意跟上、"
                    "引导退避或递出物品等需要NPC本人接受的非语言请求。"
					"公开任务条件只有在玩家直接要求NPC判断，或本条行动确实完成全部条件、NPC应兑现承诺时才由本工具回应；"
					"相关条件只完成一部分时应使用原本行动工具静默记录，不能让NPC逐项点评。"
                    "若玩家明确先赶到当前聚焦场景、再立即与其中NPC交谈，可用join_current_focus在同一事务中"
                    "提交到场与NPC答复，不能先用普通行动消耗并截断这条消息。"
					"position_note只能作为直接NPC交互的附属站位，不能替代普通场景移动工具。"
                    "玩家彼此讨论准备怎样对NPC说时不能调用。需要规则检定的请求交给跑团裁定流程。"
                    "public_segments是唯一公开文本来源：简单回答通常一至两句，复杂回答最多四句；"
                    "不要复述玩家动作、解释叙事作用、重复NPC刚说过的安排或使用后台措辞。"
                ),
                handler=self.decide_npc_response,
                parameters=(
                    GMToolParameter("name", "string", "必须回应的单个NPC名称或别名。", required=True),
                    GMToolParameter("actor", "string", "可选：正在与NPC互动的玩家角色；处理公开条件时应填写。"),
                    GMToolParameter(
                        "position_note",
                        "string",
                        (
                            "可选：当前玩家角色在同一场景内为了本次交互而明确到达的新站位。"
                            "只能记录PC自己的简短静态位置短语，不含说话、意图、动作过程或NPC位置；不是新地点或转场。"
                        ),
                    ),
                    GMToolParameter(
                        "join_current_focus",
                        "boolean",
                        (
                            "可选；仅当actor当前不在聚焦镜头，而玩家本句明确让其进入当前场景并立即与本NPC交互时为true。"
                            "它只移动该PC，不移动其他PC或NPC。"
                        ),
                    ),
                    GMToolParameter(
                        "condition_id",
                        "string",
                        "可选：只能填写scene.open_conditions中的当前公开条件ID；绝不能填写pending_npc_questions的question_id。",
                    ),
                    GMToolParameter(
                        "pending_question_id",
                        "string",
                        (
                            "可选：玩家本轮正在回应的scene.pending_npc_questions中的准确question_id；"
                            "与condition_id是不同类型。回应开放事项时必须填写，不能凭对象唯一性省略。"
                        ),
                    ),
                    GMToolParameter(
                        "response_items",
                        "array",
                        (
                            "与pending_question_id配套：逐项列出本句实际回应的待答项目及回应类型。"
                            "item_id必须从窗口remaining_items逐项选择；kind只能是answer、refuse或cannot_answer。"
                            "同一句可对不同项目使用不同kind，不得补齐玩家未回应的项目。"
                        ),
                        schema_details={
                            "items": {
                                "type": "object",
                                "properties": {
                                    "item_id": {"type": "string", "minLength": 1},
                                    "kind": {
                                        "type": "string",
                                        "enum": [
                                            "answer",
                                            "refuse",
                                            "cannot_answer",
                                        ],
                                    },
                                },
                                "required": ["item_id", "kind"],
                                "additionalProperties": False,
                            },
                            "maxItems": 6,
                        },
                    ),
                    GMToolParameter(
                        "commitment_id",
                        "string",
                        (
                            "可选：只能填写scene.pending_npc_commitments中trigger_status=reached、"
                            "且trigger_responder正是本NPC的准确ID；用于当场兑现已经到期的短期承诺。"
                        ),
                    ),
                    *self._direct_npc_output_parameters(),
                    GMToolParameter("evidence", "string", "当前玩家消息中与NPC交流的逐字证据。", required=True, source="current_message"),
                ),
                side_effect="write",
                # One player message can ask one present NPC for one response.
                # A successful answer may be followed by a different tool
                # (for example start_scene after the NPC agrees to lead), but
                # rephrasing response_instruction must never make the same NPC
                # answer and advance the action round repeatedly.
                max_successful_calls_per_message=1,
            )
        )
        registry.register(
            GMToolDefinition(
                name="decide_collective_response",
                description=(
                    "由核心GM直接决定并提交当前场景中已建档集体角色（如巡逻队、议会、守卫群或人群）的完整回应。"
                    "不会再次调用集体或NPC模型；public_segments是唯一公开文本来源。"
                    "新集体必须先调用create_npc_profile或introduce_npc建档；本工具不会从场景散文猜测身份，"
                    "也不会生成领队、代表姓名或额外权限。只适用于集体本身已经在现场或邻近位置可直接交流的情况；"
                    "单个具名人物仍使用decide_npc_response。"
                ),
                handler=self.decide_collective_response,
                parameters=(
                    GMToolParameter(
                        "name",
                        "string",
                        "已有类型化档案、实际在场且本轮被直接询问的集体稳定名称。",
                        required=True,
                    ),
                    GMToolParameter("actor", "string", "可选：正在与集体互动的玩家角色。"),
                    GMToolParameter(
                        "position_note",
                        "string",
                        (
                            "可选：当前玩家角色为这次交流明确到达的同场景静态站位；"
                            "只能记录PC自己的位置，不能移动集体。只有actor已经列在当前聚焦场景的"
                            "participants中才填写；否则省略，不要为了交谈伪造加入镜头或移动。"
                        ),
                    ),
                    GMToolParameter(
                        "condition_id",
                        "string",
                        "可选：规则工具已确认玩家履约后，逐字填写该集体所拥有的开放条件ID。",
                    ),
                    *self._direct_npc_output_parameters(),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前玩家消息中向该集体交流的逐字证据。",
                        required=True,
                        source="current_message",
                    ),
                ),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )
        registry.register(
            GMToolDefinition(
                name="decide_npc_action",
                description=(
                    "仅供受信任的GM主动节拍使用：核心GM依据状态快照中的人格、目标、权限与现场压力，"
                    "直接提交当前在场具名NPC此刻公开执行的一个具体动作。不会再次调用NPC模型。"
                ),
                handler=self.decide_npc_action,
                parameters=(
                    GMToolParameter(
                        "name",
                        "string",
                        "当前聚焦场景中实际在场的单个NPC名称或别名。",
                        required=True,
                    ),
                    *self._direct_npc_output_parameters(),
                ),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )
        registry.register(
            GMToolDefinition(
                name="decide_collective_action",
                description=(
                    "仅供受信任的GM主动节拍使用：核心GM直接提交当前在场集体依据其职责与现场压力"
                    "执行的一个公开动作。不会再次调用NPC模型，也不会生成领队、代表姓名或额外权限。"
                ),
                handler=self.decide_collective_action,
                parameters=(
                    GMToolParameter(
                        "name",
                        "string",
                        "当前聚焦场景中实际在场的集体稳定名称。",
                        required=True,
                    ),
                    *self._direct_npc_output_parameters(),
                ),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )

    def state_summary(self, context: GMToolExecutionContext) -> dict[str, object]:
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        scene = app.scene_manager.current_scene
        participants = set(getattr(scene, "participants", []) or [])
        location = str(getattr(scene, "location", "") or "")
        rows: list[dict[str, object]] = []
        relevant_rows: list[dict[str, object]] = []
        known_index: list[dict[str, object]] = []
        for persona in app.world_state.npc_personas.values():
            present = bool(
                persona.name in participants
                or any(alias in participants for alias in persona.aliases)
                or (location and persona.current_location == location)
            )
            payload = self._persona_payload(app, persona, include_private=True)
            known_index.append(
                {
                    key: payload.get(key)
                    for key in (
                        "npc_id",
                        "name",
                        "entity_kind",
                        "aliases",
                        "public_identity",
                        "role_in_story",
                        "current_location",
                        "status",
                    )
                }
            )
            if present:
                rows.append(payload)
                continue
            # Do not scan player prose to decide which NPC "must" be meant.
            # The semantic agent receives a stable index and chooses a typed
            # NPC capability; rules later validate the submitted canonical
            # identity and scene access. Keep a bounded set of full off-camera
            # records for persistent consent and split-party continuity.
            if len(relevant_rows) < 24:
                relevant_rows.append(payload)
        authority: dict[str, object] = {}
        if scene is not None:
            authority_packet = self._npc_authority_state(
                app,
                scene,
                npc_name=str(rows[0].get("name") or "") if rows else "",
            )
            authority = {
                "scene_state": dict(authority_packet.get("scene_state") or {}),
                "story_items": list(authority_packet.get("story_items") or []),
                "public_constraints": list(
                    authority_packet.get("public_constraints") or []
                ),
                "clock_boundaries": list(
                    authority_packet.get("clock_boundaries") or []
                ),
                "authority_facts": list(
                    authority_packet.get("authority_facts") or []
                ),
                "usage_rule": str(authority_packet.get("usage_rule") or ""),
            }
        return {
            "scene_id": str(getattr(scene, "scene_id", "") or ""),
            "location": location,
            "present_npcs": rows,
            "known_npc_index": known_index[:80],
            # A split-party camera can focus a destination before the speaker
            # and a previously consenting companion physically join it. These
            # are authority records, not a claim that each NPC is relevant to
            # the current sentence or present in the focused scene.
            "relevant_npcs": relevant_rows,
            "dialogue_authority": authority,
            "present_collectives": [
                row for row in rows if str(row.get("entity_kind") or "") == "collective"
            ],
        }

    def get_npc_profiles(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        requested = [self._clean(item) for item in (arguments.get("names") or []) if self._clean(item)]
        include_private = bool(arguments.get("include_private"))
        personas = []
        if requested:
            for name in requested:
                canonical = app.world_state.resolve_npc_name(name)
                if canonical and canonical in app.world_state.npc_personas:
                    persona = app.world_state.npc_personas[canonical]
                    if persona not in personas:
                        personas.append(persona)
        else:
            present_names = {
                str(item.get("name") or "")
                for item in self.state_summary(context).get("present_npcs", [])
                if isinstance(item, dict)
            }
            personas = [
                persona
                for persona in app.world_state.npc_personas.values()
                if persona.name in present_names
            ]
        return GMToolReceipt(
            tool_name="get_npc_profiles",
            ok=True,
            result={
                "npcs": [
                    self._persona_payload(app, persona, include_private=include_private)
                    for persona in personas
                ]
            },
        )

    def create_npc_profile(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        if not context.metadata.get("system_gm_beat_request"):
            evidence_error = self._validate_evidence(context, arguments.get("evidence"), "create_npc_profile")
            if evidence_error is not None:
                return evidence_error
        name = self._clean(arguments.get("name"))
        profile, profile_error = self._validate_profile(arguments.get("profile"))
        if profile_error is not None:
            return profile_error
        if not name:
            return self._failure("create_npc_profile", "NPC_NAME_REQUIRED", "NPC名称不能为空。", "提供当前场景使用的稳定名称。")
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        canonical = app.world_state.resolve_npc_name(name)
        existing = app.world_state.npc_personas.get(canonical) if canonical else None
        if (
            existing is not None
            and str(getattr(existing, "profile_status", "established"))
            != "placeholder"
        ):
            return self._failure("create_npc_profile", "NPC_ALREADY_EXISTS", f"NPC【{name}】已经有档案。", "读取现有档案；动态变化使用update_npc_state。")
        scene = app.scene_manager.current_scene
        if scene is None:
            return self._failure("create_npc_profile", "SCENE_REQUIRED", "NPC建档需要一个当前场景。", "先建立场景，或等人物真正登场时再建档。")
        present = bool(arguments.get("present_in_scene"))
        planned_entry = bool(arguments.get("planned_entry"))
        if not present and not planned_entry:
            return self._failure("create_npc_profile", "NPC_NOT_YET_PRESENT", "这个人物尚未实际进入当前场景。", "不要为假设人物预建公开NPC；等确定登场后再调用。")

        with runtime.transaction_lock:
            persona = self._ensure_persona_from_profile(
                app,
                scene,
                existing.name if existing is not None else name,
                profile,
            )
            if present:
                app.scene_manager.add_participant(persona.name)
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        return GMToolReceipt(
            tool_name="create_npc_profile",
            ok=True,
            result={
                "npc": self._persona_payload(app, persona, include_private=True),
                "present_in_scene": present,
                "planned_entry": planned_entry,
                "profile_created": existing is None,
                "profile_enriched": existing is not None,
                "saved_path": saved_path,
            },
            state_changed=True,
        )

    def introduce_npc(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        system_gm_beat = bool(context.metadata.get("system_gm_beat_request"))
        if not system_gm_beat:
            evidence_error = self._validate_evidence(
                context,
                arguments.get("evidence"),
                "introduce_npc",
            )
            if evidence_error is not None:
                return evidence_error

        name = self._clean(arguments.get("name"))
        if not name:
            return self._failure(
                "introduce_npc",
                "NPC_NAME_REQUIRED",
                "NPC名称不能为空。",
                "提供本次实际登场人物的稳定名称。",
            )
        profile, profile_error = self._validate_profile(arguments.get("profile"))
        if profile_error is not None:
            profile_error.tool_name = "introduce_npc"
            return profile_error

        scene_tools = getattr(self.host, "gm_scene_tools", None)
        clean_reply = getattr(scene_tools, "_clean_multiline", self._clean)(
            arguments.get("public_reply")
        )
        if not clean_reply:
            return self._failure(
                "introduce_npc",
                "PUBLIC_REPLY_REQUIRED",
                "NPC登场必须有公开描述。",
                "写出玩家实际看见或听见的登场，再提交工具。",
            )
        private_markers = tuple(getattr(scene_tools, "_PRIVATE_MARKERS", ()))
        if any(marker in clean_reply for marker in private_markers):
            return self._failure(
                "introduce_npc",
                "PRIVATE_CONTEXT_LEAK",
                "NPC登场描述包含后台控制字段。",
                "只保留玩家能看见或听见的内容。",
            )
        public_identity = self._clean(profile.get("public_identity")) or name
        if name not in clean_reply and public_identity not in clean_reply:
            return self._failure(
                "introduce_npc",
                "NPC_IDENTITY_NOT_PUBLIC",
                "公开描述没有出现NPC稳定名称或公开身份。",
                f"在public_reply中逐字写出【{name}】或【{public_identity}】，让在场人物可被明确指认。",
            )
        facts_validator = getattr(scene_tools, "_validated_public_facts", None)
        if not callable(facts_validator):
            return self._failure(
                "introduce_npc",
                "SCENE_FACT_VALIDATOR_UNAVAILABLE",
                "当前无法验证公开场景事实。",
                "不要建立半完成NPC；等场景工具恢复后重试。",
                retryable=False,
            )
        facts, facts_error = facts_validator(arguments.get("public_facts"), clean_reply)
        if facts_error is not None:
            facts_error.tool_name = "introduce_npc"
            return facts_error

        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        scene = app.scene_manager.current_scene
        if scene is None:
            return self._failure(
                "introduce_npc",
                "SCENE_REQUIRED",
                "NPC登场需要一个当前场景。",
                "先建立或恢复场景。",
            )
        canonical = app.world_state.resolve_npc_name(name)
        existing = app.world_state.npc_personas.get(canonical) if canonical else None
        if existing is not None and self._persona_is_present(existing, scene):
            if (
                str(getattr(existing, "profile_status", "established"))
                == "placeholder"
            ):
                return self._failure(
                    "introduce_npc",
                    "NPC_PRESENT_PROFILE_PLACEHOLDER",
                    f"NPC【{existing.name}】已经在当前场景，但只有占位档案。",
                    (
                        "先调用create_npc_profile并设present_in_scene=true补齐档案；"
                        "若玩家正在与其交互，再调用decide_npc_response。"
                    ),
                )
            return self._failure(
                "introduce_npc",
                "NPC_ALREADY_PRESENT",
                f"NPC【{existing.name}】已经在当前场景。",
                "让现有NPC行动时使用decide_npc_response或update_npc_state。",
            )

        with runtime.transaction_lock:
            frame = scene_tools._ensure_frame(runtime, context)
            if frame is None:
                return self._failure(
                    "introduce_npc",
                    "SCENE_FRAME_UNAVAILABLE",
                    "当前场景框架无法建立。",
                    "不要写入半完成NPC；先恢复场景框架。",
                )
            persona = self._ensure_persona_from_profile(
                app,
                scene,
                existing.name if existing is not None else name,
                profile,
            )
            app.scene_manager.add_participant(persona.name)
            for fact in facts:
                app.scene_frame_manager.record_public_fact(fact)
            app.scene_frame_manager.record_gm_beat(clean_reply)
            app.world_state.remember_npc_event(
                persona.name,
                f"我在当前场景公开登场：{clean_reply[:500]}",
                scene_id=str(scene.scene_id or ""),
                source="scene_introduction",
                salience=2,
                witnessed=True,
            )
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)

        return GMToolReceipt(
            tool_name="introduce_npc",
            ok=True,
            result={
                "npc": self._persona_payload(app, persona, include_private=True),
                "introduced": True,
                "profile_created": existing is None,
                "scene_id": str(scene.scene_id or ""),
                "public_facts": facts,
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply=clean_reply,
            lock_public_reply=True,
            pacing_events=[
                GMToolPacingEvent(
                    player_action=not bool(
                        context.metadata.get("system_gm_beat_request")
                    ),
                    action_summary=str(
                        context.metadata.get("current_message") or ""
                    ).strip(),
                    consequence=(facts[0] if facts else self._first_sentence(clean_reply)),
                    opposition_move=(
                        self._first_sentence(clean_reply)
                        if context.metadata.get("system_gm_beat_request")
                        and any(
                            marker
                            in str(context.metadata.get("heartbeat_action") or "")
                            for marker in (
                                "opposition",
                                "villain",
                                "threat",
                                "敌",
                                "反派",
                                "威胁",
                            )
                        )
                        else ""
                    ),
                    public_image=self._first_sentence(clean_reply),
                    gm_beat_purpose=(
                        str(context.metadata.get("heartbeat_action") or "").strip()
                        if context.metadata.get("system_gm_beat_request")
                        else ""
                    ),
                )
            ],
        )

    @classmethod
    def _combatant_parameters(
        cls,
        *,
        include_skills: bool,
    ) -> tuple[GMToolParameter, ...]:
        parameters: list[GMToolParameter] = [
            GMToolParameter("name", "string", "已有NPC人格档案的稳定名称。", required=True),
            GMToolParameter("level", "integer", "NPC等级，5到60。", required=True),
            GMToolParameter(
                "species",
                "string",
                "NPC物种。",
                required=True,
                enum=("beast", "construct", "demon", "elemental", "humanoid", "monster", "plant", "undead"),
            ),
            GMToolParameter(
                "rank",
                "string",
                "战斗阶级；反派身份另由is_villain表示。",
                required=True,
                enum=("soldier", "elite", "champion"),
            ),
            GMToolParameter(
                "combat_side",
                "string",
                "冲突阵营；省略时为enemy，盟友NPC填写ally。",
                enum=("enemy", "ally"),
            ),
            GMToolParameter("champion_value", "integer", "悍将等效小兵数；非悍将可填1。", required=True),
            GMToolParameter("is_villain", "boolean", "是否是拥有终结点和反派规则的反派。", required=True),
            GMToolParameter("ultima_points", "integer", "反派终结点；非反派必须为0。", required=True),
            GMToolParameter("traits", "array", "恰好四个能描述并援用的特质。", required=True),
            GMToolParameter(
                "attribute_spread",
                "string",
                "属性分配模板。",
                required=True,
                enum=("versatile", "standard", "specialized", "extreme"),
            ),
            GMToolParameter("attribute_order", "array", "按高到低分配的四项中文属性，必须各出现一次。", required=True),
            GMToolParameter("weaknesses", "array", "额外弱点伤害类型。", required=True),
            GMToolParameter("additional_affinities", "object", "其他伤害相性：normal/weak/resist/immune/absorb。"),
            GMToolParameter("status_immunities", "array", "额外异常状态免疫。"),
            GMToolParameter(
                "skill_options",
                "object",
                (
                    "NPC技能的结构化选项。伤害抵抗每级2种、伤害免疫/吸收每级1种、"
                    "异常状态免疫每级2种、专精每级1项、施法者每级1到2个法术、"
                    "强化伤害每级指定“基础攻击”或一个已学法术。"
                ),
            ),
            GMToolParameter(
                "spell_attributes",
                "object",
                (
                    "可选：逐个指定攻击性法术使用的检定属性。"
                    "键为已学法术名，值只能是[力量,意志]或[洞察,意志]；"
                    "未指定时沿用该法术的默认属性。"
                ),
            ),
            GMToolParameter("attack", "object", "基础攻击名称、属性、伤害类型、伤害加值、范围与附加规则。", required=True),
        ]
        if include_skills:
            parameters.extend(
                [
                    GMToolParameter("selected_skills", "array", "数量必须恰好等于预览返回的技能预算。", required=True),
                    GMToolParameter("evidence", "string", "当前消息或GM准备节拍中的逐字依据。", required=True, source="current_message"),
                ]
            )
        return tuple(parameters)

    def preview_npc_combatant(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        canonical = app.world_state.resolve_npc_name(self._clean(arguments.get("name")))
        if not canonical:
            return self._failure(
                "preview_npc_combatant",
                "NPC_PROFILE_REQUIRED",
                "必须先建立NPC人格档案，才能设计规则战斗档案。",
                "人物已确定登场时调用create_npc_profile；尚未确定的人物不要建档。",
            )
        draft, error = self._build_combat_draft(app, canonical, arguments, selected_skills=[])
        if error is not None:
            return error
        return GMToolReceipt(
            tool_name="preview_npc_combatant",
            ok=True,
            result=self._combat_draft_payload(draft),
        )

    def create_npc_combatant(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"), "create_npc_combatant")
        if evidence_error is not None:
            return evidence_error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        requested = self._clean(arguments.get("name"))
        canonical = app.world_state.resolve_npc_name(requested)
        if not canonical:
            return self._failure(
                "create_npc_combatant",
                "NPC_PROFILE_REQUIRED",
                f"NPC【{requested}】还没有人格档案。",
                "先调用create_npc_profile，不能只创建数值壳。",
            )
        if app.character_manager.exists(canonical):
            return self._failure(
                "create_npc_combatant",
                "NPC_COMBATANT_ALREADY_EXISTS",
                f"NPC【{canonical}】已经有规则战斗档案。",
                "需要成长或阶段变化时使用专门的规则更新能力，不要覆盖现有HP与状态。",
            )
        raw_skills = arguments.get("selected_skills")
        if not isinstance(raw_skills, list) or any(not isinstance(item, str) for item in raw_skills):
            return self._failure("create_npc_combatant", "NPC_SKILLS_MUST_BE_ARRAY", "selected_skills必须是字符串数组。", "先预览技能预算，再提交技能名。")
        selected_skills = [self._clean(item) for item in raw_skills if self._clean(item)]
        draft, error = self._build_combat_draft(app, canonical, arguments, selected_skills=selected_skills)
        if error is not None:
            return error
        if len(selected_skills) != draft.skill_budget:
            return self._failure(
                "create_npc_combatant",
                "NPC_SKILL_BUDGET_MISMATCH",
                f"【{canonical}】应选择{draft.skill_budget}项技能，当前提交{len(selected_skills)}项。",
                "按preview_npc_combatant返回的skill_budget补齐或删减；重复学习也占用名额。",
                result={"skill_budget": draft.skill_budget, "selected_count": len(selected_skills)},
            )
        attack, attack_error = self._validate_attack(arguments.get("attack"))
        if attack_error is not None:
            return attack_error
        skill_configuration_error = self._validate_combat_skill_configuration(
            draft=draft,
            selected_skills=selected_skills,
            skill_options=arguments.get("skill_options") or {},
            attack=attack,
        )
        if skill_configuration_error is not None:
            return skill_configuration_error
        spell_attributes, spell_attributes_error = self._validate_spell_attributes(
            arguments.get("spell_attributes"),
            known_spells=draft.known_spells,
        )
        if spell_attributes_error is not None:
            return spell_attributes_error
        is_villain = bool(arguments.get("is_villain"))
        ultima_points = int(arguments.get("ultima_points") or 0)
        combat_side = self._clean(arguments.get("combat_side") or "enemy").lower()
        if combat_side not in {"enemy", "ally"}:
            return self._failure(
                "create_npc_combatant",
                "NPC_COMBAT_SIDE_INVALID",
                "combat_side只能是enemy或ally。",
                "敌对NPC使用enemy；会完整执行回合的友方NPC使用ally。",
            )
        if combat_side == "ally" and (is_villain or ultima_points):
            return self._failure(
                "create_npc_combatant",
                "ALLY_CANNOT_BE_VILLAIN",
                "友方NPC不能同时作为持有终结点的反派。",
                "将combat_side改为enemy，或取消反派身份与终结点。",
            )
        if is_villain and ultima_points < 1:
            return self._failure("create_npc_combatant", "VILLAIN_ULTIMA_REQUIRED", "反派至少需要1点终结点。", "根据反派重要程度设置终结点。")
        if not is_villain and ultima_points:
            return self._failure("create_npc_combatant", "NON_VILLAIN_ULTIMA_FORBIDDEN", "普通NPC不能持有终结点。", "将ultima_points设为0，或明确设为反派。")

        skill_counts = Counter(normalize_skill_reference_name(name) for name in selected_skills)
        enhanced_damage_targets = self._skill_option_list(
            arguments.get("skill_options") or {},
            "强化伤害",
        )
        base_level_damage_bonus = max(
            0,
            draft.extra_damage - skill_counts["强化伤害"] * 5,
        )
        attack_damage_bonus = 5 * sum(
            1
            for target in enhanced_damage_targets
            if target in {"基础攻击", "攻击", str(attack["name"])}
        )
        spell_damage_bonuses: dict[str, int] = {}
        for target in enhanced_damage_targets:
            if target in {"基础攻击", "攻击", str(attack["name"])}:
                continue
            canonical_spell = normalize_spell_name(target)
            spell_damage_bonuses[canonical_spell] = (
                spell_damage_bonuses.get(canonical_spell, 0) + 5
            )
        traits = [combat_side, draft.species.slug, *draft.traits]
        if is_villain:
            traits.append("villain")
        character = Character(
            name=canonical,
            attributes=dict(draft.attributes),
            max_hp=draft.max_hp,
            hp=draft.max_hp,
            max_mp=draft.max_mp,
            mp=draft.max_mp,
            level=draft.level,
            crisis_threshold=draft.crisis_threshold,
            defenses=dict(draft.defenses),
            affinities=dict(draft.affinities),
            traits=list(dict.fromkeys(traits)),
            weapon_damage=(
                5
                + base_level_damage_bonus
                + attack_damage_bonus
                + int(attack["damage_bonus"])
            ),
            weapon_type=str(attack["damage_type"]),
            weapon_accuracy_attributes=list(attack["attributes"]),
            weapon_accuracy_modifier=(
                draft.check_bonus
                + int(attack["accuracy_modifier"])
                + draft.specialty_bonuses.get("命中检定", 0)
            ),
            weapon_range=str(attack["range"]),
            initiative=draft.initiative,
            abilities=list(dict.fromkeys(selected_skills)),
            spells=list(draft.known_spells),
            skills=dict(skill_counts),
            skill_options={
                key: self._skill_option_list(arguments.get("skill_options") or {}, key)
                for key in (arguments.get("skill_options") or {})
            },
            npc_specialty_bonuses=dict(draft.specialty_bonuses),
            npc_skill_effects=copy.deepcopy(draft.skill_effects),
            npc_spell_check_bonus=(
                draft.check_bonus
                + draft.specialty_bonuses.get("施法检定", 0)
            ),
            npc_spell_damage_bonus=base_level_damage_bonus,
            npc_spell_specific_damage_bonuses=spell_damage_bonuses,
            npc_spell_attributes=spell_attributes,
            permanent_status_immunities=set(draft.status_immunities),
            equipped_main_hand=str(attack["name"]),
            equipment_attack_targets_magic_defense=bool(attack["targets_magic_defense"]),
            equipment_multi_attack=int(attack["multi_attack"]),
            equipment_on_hit_status=attack["status_effect_on_hit"],
            equipment_notes=list(attack["notes"]),
        )
        with runtime.transaction_lock:
            app.character_manager.add(character)
            app.conflict_manager.register_enemy(
                canonical,
                draft.rank,
                ultima_points=ultima_points,
                action_count=draft.action_count,
                is_villain=is_villain,
            )
            persona = app.world_state.npc_personas[canonical]
            persona.known_skills = list(dict.fromkeys([*persona.known_skills, *selected_skills]))
            attack_label = f"{attack['name']}·【{'+'.join(attack['attribute_labels'])}】·【高值+{character.weapon_damage}】{attack['damage_type']}"
            if attack_label not in persona.combat_actions:
                persona.combat_actions.append(attack_label)
            persona.npc_rank = (
                "boss"
                if is_villain and draft.rank == EnemyRank.CHAMPION
                else "villain"
                if is_villain
                else "elite"
                if draft.rank in {EnemyRank.ELITE, EnemyRank.CHAMPION}
                else "minor"
            )
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        return GMToolReceipt(
            tool_name="create_npc_combatant",
            ok=True,
            result={
                **self._combat_draft_payload(draft),
                "attack": dict(attack),
                "is_villain": is_villain,
                "combat_side": combat_side,
                "ultima_points": ultima_points,
                "saved_path": saved_path,
            },
            state_changed=True,
        )

    def configure_boss_phases(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(
            context,
            arguments.get("evidence"),
            "configure_boss_phases",
        )
        if evidence_error is not None:
            return evidence_error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        requested = self._clean(arguments.get("name"))
        canonical = app.world_state.resolve_npc_name(requested) or requested
        if not canonical or not app.character_manager.exists(canonical):
            return self._failure(
                "configure_boss_phases",
                "NPC_COMBATANT_REQUIRED",
                f"NPC【{requested}】还没有规则战斗档案。",
                "先建立NPC人格档案与战斗档案，再配置后续首领形态。",
            )
        if not app.conflict_manager.is_villain(canonical):
            return self._failure(
                "configure_boss_phases",
                "BOSS_PHASES_REQUIRE_VILLAIN",
                f"NPC【{canonical}】不是拥有终结点的反派。",
                "只有反派可以使用多阶段首领规则；先按反派身份建立战斗档案。",
            )
        if app.conflict_manager.state.current_escalation_stage.get(canonical, -1) >= 0:
            return self._failure(
                "configure_boss_phases",
                "BOSS_PHASE_ALREADY_STARTED",
                f"【{canonical}】已经进入后续阶段，不能追溯改写阶段结构。",
                "保留当前战斗事实；新的形态变化应在下一场首领战前准备。",
                retryable=False,
            )

        raw_phases = arguments.get("phases")
        if not isinstance(raw_phases, list) or not 1 <= len(raw_phases) <= 2:
            return self._failure(
                "configure_boss_phases",
                "BOSS_PHASE_COUNT_INVALID",
                "phases必须包含一到两个后续形态。",
                "按触发顺序提交一到两个形态对象。",
            )
        character = app.character_manager.get(canonical)
        stages: list[EscalationStage] = []
        stage_names: set[str] = set()
        allowed_fields = {
            "name",
            "public_cue",
            "hp_restore",
            "mp_restore",
            "added_statuses",
            "affinity_changes",
            "added_spells",
            "action_count",
            "preferred_actions",
            "tactic_hints",
            "preparation_round",
        }
        for index, raw_phase in enumerate(raw_phases, start=1):
            if not isinstance(raw_phase, dict):
                return self._failure(
                    "configure_boss_phases",
                    "BOSS_PHASE_MUST_BE_OBJECT",
                    f"第{index}个阶段不是对象。",
                    "每个阶段至少提交name与public_cue。",
                )
            unknown = sorted(set(raw_phase) - allowed_fields)
            if unknown:
                return self._failure(
                    "configure_boss_phases",
                    "BOSS_PHASE_FIELD_UNKNOWN",
                    f"第{index}个阶段包含未知字段：" + "、".join(unknown),
                    "删除未知字段；独特规则不能只用自由文本技能名代替。",
                )
            stage_name = self._clean(raw_phase.get("name"))
            public_cue = self._clean(raw_phase.get("public_cue"))
            if not stage_name or not public_cue:
                return self._failure(
                    "configure_boss_phases",
                    "BOSS_PHASE_PUBLIC_CUE_REQUIRED",
                    f"第{index}个阶段缺少名称或公开变形征兆。",
                    "为阶段提供稳定名称，并写出玩家在转阶段时能观察到的变化。",
                )
            if stage_name in stage_names:
                return self._failure(
                    "configure_boss_phases",
                    "BOSS_PHASE_NAME_DUPLICATED",
                    f"阶段名称【{stage_name}】重复。",
                    "每个后续形态使用不同名称。",
                )
            stage_names.add(stage_name)

            hp_restore, numeric_error = self._optional_bounded_integer(
                raw_phase,
                "hp_restore",
                minimum=1,
                maximum=character.max_hp,
            )
            if numeric_error:
                return self._failure(
                    "configure_boss_phases",
                    "BOSS_PHASE_HP_INVALID",
                    f"第{index}个阶段的hp_restore无效：{numeric_error}",
                    f"省略以恢复至最大生命值，或填写1到{character.max_hp}。",
                )
            mp_restore, numeric_error = self._optional_bounded_integer(
                raw_phase,
                "mp_restore",
                minimum=0,
                maximum=character.max_mp,
            )
            if numeric_error:
                return self._failure(
                    "configure_boss_phases",
                    "BOSS_PHASE_MP_INVALID",
                    f"第{index}个阶段的mp_restore无效：{numeric_error}",
                    f"省略以恢复至最大精神值，或填写0到{character.max_mp}。",
                )
            action_count, numeric_error = self._optional_bounded_integer(
                raw_phase,
                "action_count",
                minimum=1,
                maximum=10,
            )
            if numeric_error:
                return self._failure(
                    "configure_boss_phases",
                    "BOSS_PHASE_ACTION_COUNT_INVALID",
                    f"第{index}个阶段的action_count无效：{numeric_error}",
                    "省略以保持原行动次数，或填写1到10。",
                )

            try:
                statuses = [
                    normalize_status(value)
                    for value in self._string_list(
                        raw_phase.get("added_statuses"),
                        field_name="added_statuses",
                    )
                ]
                affinity_changes = {
                    normalize_damage_type(str(damage_type)): normalize_affinity(
                        str(affinity)
                    )
                    for damage_type, affinity in self._string_map(
                        raw_phase.get("affinity_changes"),
                        field_name="affinity_changes",
                    ).items()
                }
            except ValueError as exc:
                return self._failure(
                    "configure_boss_phases",
                    "BOSS_PHASE_RULE_VALUE_INVALID",
                    f"第{index}个阶段包含无效规则值：{exc}",
                    "异常状态、伤害类型和伤害相性必须使用规则库中的名称。",
                )

            added_spells: list[str] = []
            try:
                raw_spells = self._string_list(
                    raw_phase.get("added_spells"),
                    field_name="added_spells",
                )
                preferred_actions = self._string_list(
                    raw_phase.get("preferred_actions"),
                    field_name="preferred_actions",
                )
                tactic_hints = self._string_list(
                    raw_phase.get("tactic_hints"),
                    field_name="tactic_hints",
                )
            except ValueError as exc:
                return self._failure(
                    "configure_boss_phases",
                    "BOSS_PHASE_LIST_INVALID",
                    f"第{index}个阶段包含无效列表：{exc}",
                    "这些字段必须是非空字符串数组。",
                )
            for raw_spell in raw_spells:
                spell = normalize_spell_name(raw_spell)
                if not is_known_spell(spell):
                    return self._failure(
                        "configure_boss_phases",
                        "BOSS_PHASE_SPELL_UNKNOWN",
                        f"第{index}个阶段包含未知法术【{raw_spell}】。",
                        "使用规则库中的标准法术名；自定义法术需要先建立独立规则定义。",
                    )
                definition = get_spell_definition(spell)
                if character.level < int(definition.minimum_level or 0):
                    return self._failure(
                        "configure_boss_phases",
                        "BOSS_PHASE_SPELL_LEVEL_TOO_LOW",
                        f"【{canonical}】等级不足以在阶段中获得法术【{spell}】。",
                        f"该法术最低需要{definition.minimum_level}级。",
                    )
                added_spells.append(spell)

            stages.append(
                EscalationStage(
                    name=stage_name,
                    ultima_points=0,
                    transition_kind="boss_phase",
                    preparation_round=bool(
                        raw_phase.get("preparation_round", True)
                    ),
                    hp_restore=hp_restore,
                    mp_restore=mp_restore,
                    added_statuses=list(dict.fromkeys(statuses)),
                    affinity_changes=affinity_changes,
                    added_spells=list(dict.fromkeys(added_spells)),
                    action_count=action_count,
                    preferred_actions=list(dict.fromkeys(preferred_actions)),
                    tactic_hints=list(dict.fromkeys(tactic_hints)),
                    public_cue=public_cue,
                )
            )

        with runtime.transaction_lock:
            app.conflict_manager.state.escalation_stages[canonical] = stages
            app.conflict_manager.state.current_escalation_stage[canonical] = -1
            app.conflict_manager.state.villains.add(canonical)
            saved_path = self.host._autosave_campaign(
                runtime,
                context.campaign_id,
            )
        return GMToolReceipt(
            tool_name="configure_boss_phases",
            ok=True,
            result={
                "name": canonical,
                "phases": [
                    {
                        "name": stage.name,
                        "public_cue": stage.public_cue,
                        "hp_restore": stage.hp_restore,
                        "mp_restore": stage.mp_restore,
                        "added_statuses": [
                            status.value for status in stage.added_statuses
                        ],
                        "affinity_changes": {
                            damage_type: affinity.value
                            for damage_type, affinity in stage.affinity_changes.items()
                        },
                        "added_spells": list(stage.added_spells),
                        "action_count": stage.action_count,
                        "preferred_actions": list(stage.preferred_actions),
                        "tactic_hints": list(stage.tactic_hints),
                        "preparation_round": stage.preparation_round,
                    }
                    for stage in stages
                ],
                "saved_path": saved_path,
            },
            state_changed=True,
        )

    def update_npc_state(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"), "update_npc_state")
        if evidence_error is not None:
            return evidence_error
        name = self._clean(arguments.get("name"))
        patch = arguments.get("patch")
        if not isinstance(patch, dict):
            return self._failure("update_npc_state", "NPC_PATCH_MUST_BE_OBJECT", "NPC状态patch必须是对象。", "只提交本轮变化的动态字段。")
        allowed = {
            "location",
            "mood",
            "stance",
            "active_goal",
            "completed_goal",
            "relationship_target",
            "relationship",
            "status",
        }
        unknown = sorted(set(patch) - allowed)
        if unknown:
            return self._failure("update_npc_state", "UNKNOWN_NPC_STATE_FIELD", "NPC状态包含未声明字段：" + "、".join(unknown), "稳定人格不在此工具中改写；删除未知字段。")
        cleaned = {key: self._clean(value) for key, value in patch.items() if self._clean(value)}
        if not cleaned:
            return self._failure("update_npc_state", "EMPTY_NPC_STATE_PATCH", "没有可提交的NPC状态变化。", "只在局面确实改变时调用。")
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        canonical = app.world_state.resolve_npc_name(name)
        if not canonical:
            return self._failure("update_npc_state", "NPC_PROFILE_REQUIRED", f"没有找到NPC【{name}】的档案。", "人物已登场时先调用create_npc_profile。")
        with runtime.transaction_lock:
            persona = app.world_state.update_npc_state(canonical, **cleaned)
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        return GMToolReceipt(
            tool_name="update_npc_state",
            ok=True,
            result={"npc": self._persona_payload(app, persona, include_private=True), "saved_path": saved_path},
            state_changed=True,
        )

    def revise_npc_profile(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"), "revise_npc_profile")
        if evidence_error is not None:
            return evidence_error
        name = self._clean(arguments.get("name"))
        scalar_patch = arguments.get("set") or {}
        list_patch = arguments.get("add") or {}
        if not isinstance(scalar_patch, dict) or not isinstance(list_patch, dict):
            return self._failure(
                "revise_npc_profile",
                "NPC_PROFILE_PATCH_MUST_BE_OBJECT",
                "NPC档案修订的set与add必须是对象。",
                "标量放入set，列表新增项放入add。",
            )
        unknown_scalars = sorted(set(scalar_patch) - self._PROFILE_SCALARS)
        unknown_lists = sorted(set(list_patch) - self._PROFILE_LISTS)
        if unknown_scalars or unknown_lists:
            return self._failure(
                "revise_npc_profile",
                "UNKNOWN_NPC_PROFILE_FIELD",
                "NPC档案包含未声明字段：" + "、".join([*unknown_scalars, *unknown_lists]),
                "只能修改工具声明的稳定字段；动态变化使用update_npc_state。",
            )
        cleaned_scalars = {
            key: self._clean(value)
            for key, value in scalar_patch.items()
            if self._clean(value)
        }
        if "npc_rank" in cleaned_scalars and cleaned_scalars["npc_rank"] not in self._RANKS:
            return self._failure(
                "revise_npc_profile",
                "INVALID_NPC_RANK",
                f"不支持NPC阶级【{cleaned_scalars['npc_rank']}】。",
                "使用minor、supporting、elite、villain或boss。",
            )
        if (
            "entity_kind" in cleaned_scalars
            and cleaned_scalars["entity_kind"] not in self._ENTITY_KINDS
        ):
            return self._failure(
                "revise_npc_profile",
                "INVALID_NPC_ENTITY_KIND",
                f"不支持NPC主体类型【{cleaned_scalars['entity_kind']}】。",
                "单个人物使用individual；队伍、议会、守卫群或人群使用collective。",
            )
        cleaned_lists: dict[str, list[str]] = {}
        for key, raw_values in list_patch.items():
            if not isinstance(raw_values, list):
                return self._failure(
                    "revise_npc_profile",
                    "NPC_PROFILE_LIST_REQUIRED",
                    f"NPC档案字段【{key}】必须提交数组。",
                    "把每个新增条目作为数组元素提交。",
                )
            values = [self._clean(value) for value in raw_values if self._clean(value)]
            if values:
                cleaned_lists[key] = list(dict.fromkeys(values))
        if not cleaned_scalars and not cleaned_lists:
            return self._failure(
                "revise_npc_profile",
                "EMPTY_NPC_PROFILE_PATCH",
                "没有可提交的NPC稳定档案变化。",
                "只提交本轮明确纠正或揭示的字段。",
            )

        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        canonical = app.world_state.resolve_npc_name(name)
        if not canonical:
            return self._failure(
                "revise_npc_profile",
                "NPC_PROFILE_REQUIRED",
                f"没有找到NPC【{name}】的档案。",
                "人物真正登场时先调用create_npc_profile。",
            )
        with runtime.transaction_lock:
            persona = app.world_state.npc_personas[canonical]
            scalar_aliases = {
                "public_identity": "public_identity",
                "role_in_story": "role_in_story",
                "core_drive": "core_drive",
                "manner": "manner",
                "speech_style": "speech_style",
                "combat_style": "combat_style",
                "npc_rank": "npc_rank",
                "leverage": "leverage",
                "authority_scope": "authority_scope",
                "knowledge_scope": "knowledge_scope",
                "refusal_move": "refusal_move",
                "active_goal": "active_goal",
                "current_mood": "current_mood",
                "current_stance": "current_stance",
                "entity_kind": "entity_kind",
            }
            for key, value in cleaned_scalars.items():
                setattr(persona, scalar_aliases[key], value)
            for key, values in cleaned_lists.items():
                target = getattr(persona, key)
                for value in values:
                    if value not in target:
                        target.append(value)
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        return GMToolReceipt(
            tool_name="revise_npc_profile",
            ok=True,
            result={
                "npc": self._persona_payload(app, persona, include_private=True),
                "changed_scalars": sorted(cleaned_scalars),
                "added_lists": {key: list(values) for key, values in cleaned_lists.items()},
                "saved_path": saved_path,
            },
            state_changed=True,
        )

    def decide_npc_response(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        system_gm_beat = bool(context.metadata.get("system_gm_beat_request"))
        if not system_gm_beat:
            evidence_error = self._validate_evidence(
                context,
                arguments.get("evidence"),
                "decide_npc_response",
            )
            if evidence_error is not None:
                return evidence_error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        requested_name = self._clean(arguments.get("name"))
        canonical = app.world_state.resolve_npc_name(requested_name)
        if not canonical:
            return self._failure(
                "decide_npc_response",
                "NPC_PROFILE_REQUIRED",
                f"没有找到NPC【{requested_name}】的档案。",
                "若人物已实际登场，先调用create_npc_profile；不要为假设人物建档。",
            )
        persona = app.world_state.npc_personas[canonical]
        collective_dispatch = str(
            context.metadata.get("_collective_response_dispatch") or ""
        ).strip()
        if (
            str(getattr(persona, "entity_kind", "individual") or "individual")
            == "collective"
            and collective_dispatch != persona.name
        ):
            return self._failure(
                "decide_npc_response",
                "COLLECTIVE_TOOL_REQUIRED",
                f"【{persona.name}】是集体角色，不能作为单个NPC处理。",
                "改用decide_collective_response；不要替集体捏造领队或代表人物。",
            )
        scene = app.scene_manager.current_scene
        if scene is None:
            return self._failure("decide_npc_response", "SCENE_REQUIRED", "NPC回应需要一个当前场景。", "先恢复或建立当前场景。")
        if not self._persona_is_present(persona, scene):
            return self._failure(
                "decide_npc_response",
                "NPC_NOT_PRESENT",
                f"NPC【{persona.name}】不在当前场景。",
                (
                    "不要让缺席人物越过场景边界发言。若玩家本句明确前往该NPC所在地点并在抵达后立即交谈，"
                    "改用move_scene_group，并填写followup_npc_name与followup_response_instruction；"
                    "否则先完成移动或通讯裁定。"
                ),
            )
        # A scheduler-owned GM beat is not a player utterance. Treating the
        # synthetic beat as player evidence would pollute persistent NPC memory.
        player_message = (
            ""
            if system_gm_beat
            else str(context.metadata.get("current_message") or "").strip()
        )
        actor = self._dialogue_actor(runtime, context, arguments)
        join_current_focus = bool(arguments.get("join_current_focus"))
        if join_current_focus:
            if not actor or not app.character_manager.exists(actor):
                return self._failure(
                    "decide_npc_response",
                    "NPC_INTERACTION_ACTOR_REQUIRED",
                    "加入当前镜头并交谈时必须指定当前玩家控制的角色。",
                    "填写actor，并保留玩家本句明确声明的进入动作。",
                )
            if actor in scene.participants:
                return self._failure(
                    "decide_npc_response",
                    "ACTOR_ALREADY_IN_FOCUSED_SCENE",
                    f"【{actor}】已经在当前场景，不需要再次加入镜头。",
                    "删除join_current_focus，直接结算NPC交互。",
                )
            if not app.scene_manager.location_of(actor):
                return self._failure(
                    "decide_npc_response",
                    "ACTOR_LOCATION_UNKNOWN",
                    f"没有【{actor}】进入当前场景前的权威位置。",
                    "先恢复该角色实际所在分支；不能用NPC交互把位置不明的角色传送进来。",
                )
            if app.conflict_manager.state.active or scene.scene_type.value == "conflict":
                return self._failure(
                    "decide_npc_response",
                    "CONFLICT_TURN_REQUIRED",
                    "冲突中不能借NPC交互绕过移动与回合顺序。",
                    "在当前角色的合法回合中先完成移动或对应规则行动。",
                )
        position_note = self._clean(arguments.get("position_note"))
        if position_note:
            if not actor or not app.character_manager.exists(actor):
                return self._failure(
                    "decide_npc_response",
                    "NPC_INTERACTION_ACTOR_REQUIRED",
                    "记录交谈站位时必须指定当前玩家控制的角色。",
                    "填写actor；若玩家没有移动则删除position_note。",
                )
            if actor not in scene.participants and not join_current_focus:
                return self._failure(
                    "decide_npc_response",
                    "NPC_INTERACTION_ACTOR_NOT_PRESENT",
                    f"【{actor}】不在当前场景，不能用交谈站位让其直接出现。",
                    "先完成真实转场或加入当前镜头；若玩家没有移动则删除position_note。",
                )
        frame = app.scene_frame_manager.current_frame
        requested_commitment = self._requested_pending_commitment(
            app,
            persona.name,
            self._clean(arguments.get("commitment_id")),
        )
        if isinstance(requested_commitment, GMToolReceipt):
            return requested_commitment
        requested_question = (
            None
            if system_gm_beat
            else self._requested_pending_question(
                app,
                persona.name,
                self._clean(arguments.get("pending_question_id")),
            )
        )
        if isinstance(requested_question, GMToolReceipt):
            return requested_question
        requested_condition = (
            None
            if system_gm_beat
            else self._requested_open_condition(
                app,
                persona.name,
                self._clean(arguments.get("condition_id")),
            )
        )
        if isinstance(requested_condition, GMToolReceipt):
            return requested_condition
        condition_already_fulfilled = bool(
            requested_condition is not None
            and str(
                requested_condition.get("player_fulfillment") or "pending"
            )
            == "fulfilled"
        )
        working_frame = copy.deepcopy(frame) if frame is not None else None
        player_response_updates: list[dict[str, object]] = []
        if requested_question is not None:
            response_items = [
                {
                    "item_id": self._clean(item.get("item_id")),
                    "kind": self._clean(item.get("kind")).lower(),
                }
                for item in list(arguments.get("response_items") or [])
                if isinstance(item, dict)
            ]
            update = NPCResponseWindowManager.record_player_response(
                working_frame,
                question_id=self._clean(requested_question.get("question_id")),
                actor=actor,
                response_items=response_items,
                evidence=player_message,
            )
            if update is None:
                return self._failure(
                    "decide_npc_response",
                    "NPC_PLAYER_RESPONSE_INVALID",
                    "玩家回应没有匹配该待答窗口的响应者、回应类型或待答项目。",
                    (
                        "从scene.pending_npc_questions读取准确question_id与remaining_items，"
                        "按item_id逐项填写response_items；不得替玩家补完未回应的项目。"
                    ),
                )
            player_response_updates.append(update)
        elif arguments.get("response_items"):
            return self._failure(
                "decide_npc_response",
                "NPC_PENDING_QUESTION_ID_REQUIRED",
                "response_items必须绑定准确的pending_question_id。",
                "从scene.pending_npc_questions选择本轮实际回应的开放窗口ID。",
            )
        try:
            public_segments = normalize_public_segments(
                arguments.get("public_segments")
            )
            plan = normalize_speech_plan(
                arguments,
                public_segments=public_segments,
            )
            public_reply = render_public_segments(public_segments)
            if not public_reply:
                raise ValueError("public_segments没有形成可公开的NPC回应")
        except (TypeError, ValueError) as exc:
            return self._failure(
                "decide_npc_response",
                "NPC_RESPONSE_TRANSACTION_INVALID",
                str(exc),
                (
                    "保留同一NPC和玩家原意，修正public_segments或结构字段后重试；"
                    "不要改用final、通用叙事或另一个NPC代答。"
                ),
                retryable=True,
                result={
                    "retry_same_tool": True,
                    "npc": persona.name,
                },
            )
        if requested_commitment is not None:
            expected_commitment_id = self._clean(
                requested_commitment.get("commitment_id")
            )
            if (
                self._clean(plan.get("commitment_id"))
                != expected_commitment_id
                or self._clean(plan.get("commitment_outcome")).lower()
                != "fulfilled"
            ):
                return self._failure(
                    "decide_npc_response",
                    "NPC_IGNORED_TRIGGERED_COMMITMENT",
                    "短期承诺已经到期，但NPC计划没有按准确ID在本轮兑现。",
                    (
                        "保留同一NPC与commitment_id重试；commitment_outcome必须为fulfilled，"
                        "并让原promised_result在本轮公开答复中实际发生。"
                    ),
                    retryable=True,
                    result={
                        "retry_same_tool": True,
                        "npc": persona.name,
                        "commitment_id": expected_commitment_id,
                    },
                )
        if (
            condition_already_fulfilled
            and str(plan.get("condition_outcome") or "none") != "fulfilled"
        ):
            return self._failure(
                "decide_npc_response",
                "NPC_IGNORED_FULFILLED_CONDITION",
                "玩家义务已经由权威工具确认完成，但NPC计划没有兑现既有承诺。",
                (
                    "保留同一NPC与condition_id重试；condition_outcome必须为fulfilled，"
                    "并让原promised_result在本轮公开答复中实际发生，不得要求玩家重复履约。"
                ),
                retryable=True,
                result={"retry_same_tool": True, "npc": persona.name},
            )
        condition_fulfilled = bool(
            requested_condition is not None
            and str(plan.get("condition_outcome") or "none") == "fulfilled"
        )
        commitment_fulfilled = bool(
            requested_commitment is not None
            and self._clean(plan.get("commitment_outcome")).lower()
            == "fulfilled"
        )
        if condition_fulfilled:
            plan.update(
                {
                    "speech_act": "answer",
                    "condition": "",
                    "promised_result": "",
                    "promise_kind": "none",
                    "promise_subject": "",
                    "required_outcome": self._clean(
                        requested_condition.get("promised_result")
                    ),
                }
            )
        if commitment_fulfilled:
            plan["required_outcome"] = self._clean(
                requested_commitment.get("promised_result")
            )
        if (
            system_gm_beat
            and context.metadata.get("heartbeat_require_material_change")
            and str(plan.get("speech_act") or "").strip() == "admit_unknown"
        ):
            return self._failure(
                "decide_npc_response",
                "NPC_BEAT_NOT_MATERIAL",
                "NPC只表示不知道，不能满足本轮必须改变局面的主动节拍。",
                (
                    "改用introduce_npc让有权限或知识的人物登场，或用commit_scene_response推进环境、"
                    "对手或已经建立的现场压力；不要让无信息答复占用主动节拍。"
                ),
            )
        introduced_specs, introduction_error = self._validated_introduced_npcs(
            app,
            scene,
            plan.get("introduced_npcs"),
            tool_name="decide_npc_response",
        )
        if introduction_error is not None:
            return introduction_error
        public_plan = dict(plan)
        public_plan.pop("facts_to_withhold", None)
        # Profiles for newly assigned guides or attendants remain part of the
        # transaction, not the public response plan.
        public_plan.pop("introduced_npcs", None)
        introduced_specs = self._publicly_visible_introduced_npcs(
            introduced_specs,
            public_reply,
        )

        opened_player_request: dict[str, str] | None = None
        if (
            working_frame is not None
            and not condition_fulfilled
            and not commitment_fulfilled
        ):
            request_plan = plan.get("player_response_request")
            if isinstance(request_plan, dict):
                addressed_actor = self._clean(request_plan.get("addressed_actor"))
                known_players = set(app._known_player_character_names())
                if addressed_actor and addressed_actor not in known_players:
                    return self._failure(
                        "decide_npc_response",
                        "NPC_RESPONSE_WINDOW_ACTOR_INVALID",
                        f"NPC待答窗口指定了未知玩家角色【{addressed_actor}】。",
                        "只能使用当前权威角色名；问整队时addressed_actor留空。",
                    )
                opened_player_request = NPCResponseWindowManager.open_request(
                    working_frame,
                    npc=persona.name,
                    summary=self._clean(request_plan.get("summary")),
                    required_items=[
                        {
                            "item_id": self._clean(item.get("item_id")),
                            "prompt": self._clean(item.get("prompt")),
                        }
                        for item in list(request_plan.get("required_items") or [])
                        if isinstance(item, dict)
                        and self._clean(item.get("item_id"))
                        and self._clean(item.get("prompt"))
                    ],
                    addressed_actor=addressed_actor,
                    scene=scene,
                )
            else:
                opened_player_request = None

        introduced_personas: list[Any] = []
        with runtime.transaction_lock:
            if join_current_focus and actor not in scene.participants:
                app.scene_manager.add_participant(
                    actor,
                    location=str(scene.location or scene.name or ""),
                )
            app.scene_manager.add_participant(persona.name)
            if actor:
                app.scene_manager.record_participant_activity(
                    actor,
                    player_message or f"与{persona.name}交谈",
                )
                if position_note:
                    app.scene_manager.set_participant_position(actor, position_note)
            app.world_state.update_npc_state(
                persona.name,
                location=str(scene.location or scene.name or ""),
                mood=str(plan.get("emotion") or ""),
                stance=str(plan.get("stance") or ""),
                active_goal=str(plan.get("intent") or ""),
                scene=str(scene.scene_id or ""),
            )
            if system_gm_beat:
                memory_text = f"我主动采取行动并公开表示：{public_reply[:500]}"
                memory_source = "autonomous_scene_beat"
            else:
                memory_text = (
                    f"玩家询问：{player_message[:160]}；我的答复：{public_reply[:500]}"
                )
                memory_source = "direct_dialogue"
            app.world_state.remember_npc_event(
                persona.name,
                memory_text,
                scene_id=str(scene.scene_id or ""),
                source=memory_source,
                salience=2,
                witnessed=True,
            )
            for spec in introduced_specs:
                canonical_introduced = app.world_state.resolve_npc_name(spec["name"])
                introduced = (
                    app.world_state.npc_personas[canonical_introduced]
                    if canonical_introduced
                    else self._ensure_persona_from_profile(
                        app,
                        scene,
                        spec["name"],
                        spec["profile"],
                    )
                )
                app.scene_manager.add_participant(introduced.name)
                app.world_state.update_npc_state(
                    introduced.name,
                    location=str(scene.location or scene.name or ""),
                    active_goal=str(
                        spec["profile"].get("active_goal")
                        or spec["profile"].get("core_drive")
                        or "完成眼前受命之事"
                    ),
                    scene=str(scene.scene_id or ""),
                )
                app.world_state.remember_npc_event(
                    introduced.name,
                    f"我由{persona.name}在公开答复中指派并进入当前场景：{public_reply[:500]}",
                    scene_id=str(scene.scene_id or ""),
                    source="npc_response_introduction",
                    salience=2,
                    witnessed=True,
                )
                introduced_personas.append(introduced)
            resolved_condition: dict[str, str] | None = None
            if frame is not None:
                if working_frame is not None:
                    frame.pending_npc_questions = copy.deepcopy(
                        working_frame.pending_npc_questions
                    )
                    app.scene_frame_manager.touch_current_state()
                app.scene_frame_manager.record_npc_answer(persona.name, public_reply)
                contract_changes = self._commit_speech_contracts(
                    app,
                    persona.name,
                    player_message,
                    public_reply,
                    plan,
                )
                settled_exchange = contract_changes.get("settled_exchange")
                recorded_condition = contract_changes.get("recorded_condition")
                resolved_commitments = list(
                    contract_changes.get("resolved_commitments") or []
                )
                if (
                    opened_player_request
                    and isinstance(recorded_condition, dict)
                    and str(recorded_condition.get("status") or "open") == "open"
                ):
                    linked = NPCResponseWindowManager.link_condition(
                        frame,
                        question_id=str(opened_player_request.get("question_id") or ""),
                        condition_id=str(recorded_condition.get("condition_id") or ""),
                        scene=scene,
                    )
                    if linked:
                        opened_player_request["condition_id"] = str(
                            recorded_condition.get("condition_id") or ""
                        )
                if str(plan.get("condition_outcome") or "none") == "fulfilled":
                    condition_id = str(
                        (requested_condition or {}).get("condition_id") or ""
                    ).strip()
                    if condition_id:
                        if requested_question is not None:
                            # Explicitly supplying both ids is the safe migration
                            # path for saves created before request-condition links
                            # were persisted. Ownership was validated above.
                            NPCResponseWindowManager.link_condition(
                                frame,
                                question_id=str(requested_question.get("question_id") or ""),
                                condition_id=condition_id,
                                scene=scene,
                            )
                        resolved_condition = app.scene_frame_manager.resolve_condition(
                            condition_id,
                            scene=scene,
                            actor=self._clean(arguments.get("actor")),
                            public_evidence=public_reply,
                        )
                        if resolved_condition is not None:
                            linked_updates = (
                                NPCResponseWindowManager.resolve_linked_condition_request(
                                    frame,
                                    condition_id=condition_id,
                                    npc=persona.name,
                                    actor=self._clean(arguments.get("actor")),
                                    public_evidence=public_reply,
                                )
                            )
                            if linked_updates:
                                player_response_updates = [
                                    *player_response_updates,
                                    *linked_updates,
                                ]
                                app.scene_frame_manager.touch_current_state()
            else:
                settled_exchange = None
                resolved_commitments = []
            if actor and GMToolReceiptPolicy.action_already_committed(context, actor):
                action_round = GMToolReceiptPolicy.committed_action_round(
                    context,
                    actor,
                )
            else:
                action_round = app.record_free_scene_player_action(actor) if actor else {}
            clock_lines = app.turn_response_renderer.public_state_lines(action_round)
            if clock_lines:
                public_reply = "\n".join([public_reply, *clock_lines])
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        allowed_followup_tools = self._allowed_response_followups(
            context,
            public_plan=public_plan,
            resolved_condition=resolved_condition,
        )
        settled_payload = dict(settled_exchange or {})
        local_payoff = ""
        if (
            str(settled_payload.get("outcome") or "") == "accepted"
            and str(settled_payload.get("player_performance") or "") == "complete"
        ):
            local_payoff = self._clean(settled_payload.get("settled_terms"))
        if resolved_condition:
            local_payoff = self._clean(
                resolved_condition.get("promised_result")
                or resolved_condition.get("public_evidence")
                or local_payoff
            )
        if resolved_commitments:
            local_payoff = self._clean(
                resolved_commitments[-1].get("promised_result")
                or resolved_commitments[-1].get("resolution")
                or local_payoff
            )
        shared_facts = [
            self._clean(item)
            for item in list(public_plan.get("facts_to_share") or [])
            if self._clean(item)
        ]
        if requested_question is not None:
            direct_answer = self._clean(public_plan.get("direct_answer"))
            if direct_answer:
                shared_facts.insert(0, direct_answer)
        reveal = "；".join(dict.fromkeys(shared_facts))[:500]
        public_image = self._first_sentence(public_reply)
        heartbeat_action = str(context.metadata.get("heartbeat_action") or "").strip()
        is_opposition_beat = bool(
            system_gm_beat
            and any(
                marker in heartbeat_action
                for marker in ("opposition", "villain", "threat", "敌", "反派", "威胁")
            )
        )
        material_consequence = ""
        if (
            system_gm_beat
            and context.metadata.get("heartbeat_require_material_change")
            and not reveal
            and not local_payoff
            and not is_opposition_beat
        ):
            material_consequence = public_image
        return GMToolReceipt(
            tool_name="decide_npc_response",
            ok=True,
            result={
                "npc": persona.name,
                "actor_position": position_note,
                "joined_current_focus": join_current_focus,
                "public_plan": public_plan,
                "settled_exchange": settled_payload,
                "resolved_condition": dict(resolved_condition or {}),
                "resolved_commitments": [
                    dict(item) for item in resolved_commitments
                ],
                "player_response_updates": list(player_response_updates),
                "opened_player_request": dict(opened_player_request or {}),
                "introduced_npcs": [
                    self._persona_payload(app, item, include_private=True)
                    for item in introduced_personas
                ],
                "action_round": dict(action_round),
                "public_state_lines": list(clock_lines),
                "allowed_followup_tools": allowed_followup_tools,
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply=public_reply,
            lock_public_reply=True,
            pacing_events=[
                GMToolPacingEvent(
                    player_action=not system_gm_beat,
                    action_summary=player_message,
                    consequence=material_consequence,
                    local_payoff=local_payoff,
                    reveal=reveal,
                    opposition_move=public_image if is_opposition_beat else "",
                    public_image=public_image,
                    local_question_changed=bool(
                        context.metadata.get("heartbeat_require_local_change")
                    ),
                    local_question_resolved=bool(
                        context.metadata.get("heartbeat_require_local_resolution")
                        or resolved_condition
                        or resolved_commitments
                    ),
                    gm_beat_purpose=heartbeat_action if system_gm_beat else "",
                )
            ],
        )

    def decide_npc_action(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        """Let one present NPC own a scheduler-requested scene beat."""

        if not context.metadata.get("system_gm_beat_request"):
            return self._failure(
                "decide_npc_action",
                "TRUSTED_GM_BEAT_REQUIRED",
                "NPC自主行动只能由受信任的GM主动节拍触发。",
                "玩家直接与NPC交互时使用decide_npc_response；不要把普通消息伪装成系统节拍。",
                retryable=False,
            )
        name = self._clean(arguments.get("name"))
        delegated_arguments = dict(arguments)
        delegated_arguments["name"] = name
        receipt = self.decide_npc_response(context, delegated_arguments)
        return self._rename_autonomous_receipt(
            receipt,
            source_tool="decide_npc_response",
            target_tool="decide_npc_action",
        )

    def decide_collective_action(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        """Let one present collective own a scheduler-requested scene beat."""

        if not context.metadata.get("system_gm_beat_request"):
            return self._failure(
                "decide_collective_action",
                "TRUSTED_GM_BEAT_REQUIRED",
                "集体自主行动只能由受信任的GM主动节拍触发。",
                "玩家直接与集体交互时使用decide_collective_response。",
                retryable=False,
            )
        name = self._clean(arguments.get("name"))
        delegated_arguments = dict(arguments)
        delegated_arguments["name"] = name
        receipt = self.decide_collective_response(context, delegated_arguments)
        return self._rename_autonomous_receipt(
            receipt,
            source_tool="decide_collective_response",
            target_tool="decide_collective_action",
        )

    @staticmethod
    def _rename_autonomous_receipt(
        receipt: GMToolReceipt,
        *,
        source_tool: str,
        target_tool: str,
    ) -> GMToolReceipt:
        receipt.tool_name = target_tool
        if not receipt.ok:
            receipt.correction_hint = str(receipt.correction_hint or "").replace(
                source_tool,
                target_tool,
            )
            if isinstance(receipt.result, dict) and receipt.result.get("retry_same_tool"):
                receipt.result["retry_tool_name"] = target_tool
        return receipt

    def decide_collective_response(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        """Resolve one already-established group without inventing a leader.

        The collective must already have a typed persistent profile. The GM
        agent creates or introduces it through a separate capability; this
        response tool never infers an identity from scene prose.
        """

        if not context.metadata.get("system_gm_beat_request"):
            evidence_error = self._validate_evidence(
                context,
                arguments.get("evidence"),
                "decide_collective_response",
            )
            if evidence_error is not None:
                return evidence_error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        scene = app.scene_manager.current_scene
        if scene is None:
            return self._failure(
                "decide_collective_response",
                "SCENE_REQUIRED",
                "集体回应需要一个当前场景。",
                "先恢复或建立当前场景。",
            )
        requested_name = self._clean(arguments.get("name"))
        if not requested_name:
            return self._failure(
                "decide_collective_response",
                "COLLECTIVE_NAME_REQUIRED",
                "集体名称不能为空。",
                "填写当前场景中已经公开出现的集体稳定名称。",
            )

        canonical = app.world_state.resolve_npc_name(requested_name)
        existing = app.world_state.npc_personas.get(canonical) if canonical else None
        if existing is not None and str(
            getattr(existing, "entity_kind", "individual") or "individual"
        ) != "collective":
            return self._failure(
                "decide_collective_response",
                "INDIVIDUAL_NPC_TOOL_REQUIRED",
                f"【{existing.name}】是单个NPC，不是集体角色。",
                "改用decide_npc_response让该人物本人回应。",
            )
        if existing is not None and not self._persona_is_present(existing, scene):
            return self._failure(
                "decide_collective_response",
                "COLLECTIVE_NOT_PRESENT",
                f"集体角色【{existing.name}】不在当前场景。",
                "不要让缺席集体远程发言；先完成移动、通讯或场景切换。",
            )
        if existing is None:
            return self._failure(
                "decide_collective_response",
                "COLLECTIVE_PROFILE_REQUIRED",
                f"集体【{requested_name}】尚无类型化NPC档案，不能由规则层从场景散文猜测身份。",
                (
                    "若该集体已经实际登场，先调用create_npc_profile并设entity_kind=collective、"
                    "present_in_scene=true；若它现在才登场，改用introduce_npc。"
                ),
            )

        persona = existing
        previous_dispatch = context.metadata.get("_collective_response_dispatch")
        context.metadata["_collective_response_dispatch"] = persona.name
        delegated_arguments = dict(arguments)
        delegated_arguments["name"] = persona.name
        try:
            receipt = self.decide_npc_response(context, delegated_arguments)
        finally:
            if previous_dispatch is None:
                context.metadata.pop("_collective_response_dispatch", None)
            else:
                context.metadata["_collective_response_dispatch"] = previous_dispatch

        receipt.tool_name = "decide_collective_response"
        if not receipt.ok:
            receipt.correction_hint = str(receipt.correction_hint or "").replace(
                "decide_npc_response",
                "decide_collective_response",
            )
            if isinstance(receipt.result, dict) and receipt.result.get("retry_same_tool"):
                receipt.result["retry_tool_name"] = "decide_collective_response"
        if receipt.ok:
            receipt.result["collective"] = True
            receipt.result["collective_name"] = persona.name
        return receipt

    @classmethod
    def _validated_introduced_npcs(
        cls,
        app: Any,
        scene: Any,
        value: object,
        *,
        tool_name: str,
    ) -> tuple[list[dict[str, Any]], GMToolReceipt | None]:
        if value in (None, []):
            return [], None
        if not isinstance(value, list):
            return [], cls._failure(
                tool_name,
                "NPC_INTRODUCTIONS_MUST_BE_ARRAY",
                "NPC回应中的introduced_npcs必须是数组。",
                "只在NPC本轮明确让普通部属立即进入现场时提交至多两项结构化档案。",
            )
        if len(value) > 2:
            return [], cls._failure(
                tool_name,
                "TOO_MANY_NPC_INTRODUCTIONS",
                "一次NPC回应最多只能让两名普通配角进入现场。",
                "只保留本轮真正立即登场且与当前答复直接相关的人物。",
            )
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, dict) or set(item) - {"name", "profile"}:
                return [], cls._failure(
                    tool_name,
                    "NPC_INTRODUCTION_INVALID",
                    "NPC登场项只能包含name与profile。",
                    "按工具约定提交稳定名称和普通配角人格档案。",
                )
            name = cls._clean(item.get("name"))
            profile, profile_error = cls._validate_profile(item.get("profile"))
            if profile_error is not None:
                profile_error.tool_name = tool_name
                return [], profile_error
            if not name:
                return [], cls._failure(
                    tool_name,
                    "NPC_INTRODUCTION_NAME_REQUIRED",
                    "被指派登场的NPC必须有稳定名称。",
                    "使用能在后续场景中再次识别的名称或职务称呼。",
                )
            rank = str(profile.get("npc_rank") or "minor")
            if rank not in {"minor", "supporting"}:
                return [], cls._failure(
                    tool_name,
                    "NPC_INTRODUCTION_RANK_TOO_HIGH",
                    "直接回应只能顺带引入普通或支援配角。",
                    "反派、精英或首领必须由GM使用专门NPC登场与战斗设计工具建立。",
                )
            forbidden_fields = [
                field
                for field in ("secrets", "known_skills", "combat_actions")
                if profile.get(field)
            ]
            if forbidden_fields:
                return [], cls._failure(
                    tool_name,
                    "NPC_INTRODUCTION_PROFILE_TOO_POWERFUL",
                    "顺带登场的普通配角不能在NPC答复中获得秘密或战斗能力："
                    + "、".join(forbidden_fields),
                    "只保留公开身份、现场职责、朴素动机、权限与知识边界。",
                )
            canonical = app.world_state.resolve_npc_name(name) or name
            if canonical in seen:
                continue
            seen.add(canonical)
            existing = app.world_state.npc_personas.get(canonical)
            if existing is not None and cls._persona_is_present(existing, scene):
                # Re-identifying someone already present is harmless but not a
                # second introduction or state mutation.
                continue
            result.append({"name": canonical, "profile": profile})
        return result, None

    @classmethod
    def _publicly_visible_introduced_npcs(
        cls,
        introductions: list[dict[str, Any]],
        public_reply: str,
    ) -> list[dict[str, Any]]:
        """Commit only supporting NPCs actually introduced at the table.

        An NPC planner may decide that a named attendant should carry out a
        generic promise such as “安排一名守望者带路”. If the rendered answer
        never gives that attendant a name or stable public identity, creating
        a persistent persona would leak private planning into world state. It
        also must not invalidate the speaker's otherwise valid reply. Omit the
        private draft and let the character receive a profile when they are
        actually introduced or speak later.
        """

        visible: list[dict[str, Any]] = []
        for item in introductions:
            name = cls._clean(item.get("name"))
            profile = item.get("profile") if isinstance(item.get("profile"), dict) else {}
            public_identity = cls._clean(profile.get("public_identity")) or name
            if name in public_reply or public_identity in public_reply:
                visible.append(item)
        return visible

    @staticmethod
    def _allowed_response_followups(
        context: GMToolExecutionContext,
        *,
        public_plan: dict[str, object],
        resolved_condition: dict[str, object] | None,
    ) -> list[str]:
        """Grant only an explicitly justified post-dialogue transition.

        The NPC transaction can unlock one possible scene transition only
        after accepting a proposal. The follow-up tool still validates the
        destination and scene lifecycle preconditions.
        """

        accepted = str(public_plan.get("proposal_outcome") or "none").strip() == "accepted"
        # Fulfilling a condition may merely grant permission to inspect an item
        # or perform another action in the same scene. It must not implicitly
        # authorize a scene transition. A player movement proposal accepted by
        # the NPC remains the one dialogue result that can unlock that follow-up;
        # the transition tool still validates the destination.
        return ["transition_scene"] if accepted else []

    def _dialogue_actor(
        self,
        runtime: Any,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> str:
        explicit = self._clean(arguments.get("actor"))
        if explicit and runtime.app.character_manager.exists(explicit):
            return explicit
        control_map = getattr(self.host, "_player_character_control_map", None)
        if not callable(control_map):
            return ""
        controlled = list(control_map(runtime).get(context.speaker, []) or [])
        return str(controlled[0] or "").strip() if len(controlled) == 1 else ""

    def _build_combat_draft(
        self,
        app: Any,
        name: str,
        arguments: dict[str, object],
        *,
        selected_skills: list[str],
    ) -> tuple[Any, GMToolReceipt | None]:
        try:
            level = int(arguments.get("level"))
        except (TypeError, ValueError):
            level = 0
        if not 5 <= level <= 60:
            return None, self._failure("create_npc_combatant", "NPC_LEVEL_OUT_OF_RANGE", "NPC等级必须在5到60之间。", "按核心规则重新选择等级。")
        traits = arguments.get("traits")
        if not isinstance(traits, list) or any(not isinstance(item, str) for item in traits):
            return None, self._failure("create_npc_combatant", "NPC_TRAITS_MUST_BE_ARRAY", "NPC特质必须是字符串数组。", "提供恰好四个特质。")
        clean_traits = list(dict.fromkeys(self._clean(item) for item in traits if self._clean(item)))
        if len(clean_traits) != 4:
            return None, self._failure(
                "create_npc_combatant",
                "NPC_REQUIRES_FOUR_TRAITS",
                f"NPC需要恰好四个不同特质，当前为{len(clean_traits)}个。",
                "补充或删减为四个能体现性格、需求、本能或习性的特质。",
            )
        order = arguments.get("attribute_order")
        if not isinstance(order, list) or len(order) != 4:
            return None, self._failure("create_npc_combatant", "NPC_ATTRIBUTE_ORDER_REQUIRED", "attribute_order必须包含四项属性。", "用中文提交敏捷、洞察、力量、意志，并各出现一次。")
        normalized_order = [self._ATTRIBUTE_ALIASES.get(self._clean(item), "") for item in order]
        if sorted(normalized_order) != ["DEX", "INS", "MIG", "WLP"]:
            return None, self._failure("create_npc_combatant", "NPC_ATTRIBUTE_ORDER_INVALID", "attribute_order必须让四项属性各出现一次。", "使用敏捷、洞察、力量、意志。")
        weaknesses = arguments.get("weaknesses")
        if not isinstance(weaknesses, list) or any(not isinstance(item, str) for item in weaknesses):
            return None, self._failure("create_npc_combatant", "NPC_WEAKNESSES_MUST_BE_ARRAY", "weaknesses必须是字符串数组。", "没有额外弱点时提交空数组。")
        affinities = arguments.get("additional_affinities") or {}
        if not isinstance(affinities, dict):
            return None, self._failure("create_npc_combatant", "NPC_AFFINITIES_MUST_BE_OBJECT", "additional_affinities必须是对象。", "用伤害类型到相性的键值对提交。")
        immunities = arguments.get("status_immunities") or []
        if not isinstance(immunities, list) or any(not isinstance(item, str) for item in immunities):
            return None, self._failure("create_npc_combatant", "NPC_IMMUNITIES_MUST_BE_ARRAY", "status_immunities必须是字符串数组。", "没有额外免疫时提交空数组。")
        skill_options = arguments.get("skill_options") or {}
        if not isinstance(skill_options, dict):
            return None, self._failure("create_npc_combatant", "NPC_SKILL_OPTIONS_MUST_BE_OBJECT", "skill_options必须是对象。", "没有额外选项时提交空对象。")
        rank_text = self._clean(arguments.get("rank"))
        try:
            rank = EnemyRank(rank_text)
        except ValueError:
            return None, self._failure("create_npc_combatant", "NPC_RANK_INVALID", "NPC战斗阶级无效。", "使用soldier、elite或champion。")
        try:
            champion_value = int(arguments.get("champion_value") or 1)
        except (TypeError, ValueError):
            champion_value = 1
        if rank == EnemyRank.CHAMPION and champion_value < 2:
            return None, self._failure("create_npc_combatant", "CHAMPION_VALUE_TOO_LOW", "悍将至少等效2名小兵。", "将champion_value设为2或更高。")
        if rank != EnemyRank.CHAMPION:
            champion_value = 1
        try:
            draft = EncounterManager(
                app.character_manager,
                app.conflict_manager,
            ).design_npc(
                name,
                level=level,
                species=self._clean(arguments.get("species")),
                traits=clean_traits,
                attribute_spread=self._clean(arguments.get("attribute_spread")),
                attribute_order=tuple(normalized_order),
                weaknesses=[self._clean(item) for item in weaknesses if self._clean(item)],
                additional_affinities={self._clean(key): self._clean(value) for key, value in affinities.items()},
                status_immunities=[self._clean(item) for item in immunities if self._clean(item)],
                rank=rank,
                champion_value=champion_value,
                selected_skill_names=selected_skills,
                skill_options=dict(skill_options),
            )
        except (TypeError, ValueError) as exc:
            return None, self._failure("create_npc_combatant", "NPC_DESIGN_INVALID", str(exc), "根据规则错误修正物种、相性、属性或技能选项。")
        return draft, None

    @classmethod
    def _validate_spell_attributes(
        cls,
        value: object,
        *,
        known_spells: list[str],
    ) -> tuple[dict[str, list[str]], GMToolReceipt | None]:
        if value in (None, {}):
            return {}, None
        if not isinstance(value, dict):
            return {}, cls._failure(
                "create_npc_combatant",
                "NPC_SPELL_ATTRIBUTES_MUST_BE_OBJECT",
                "spell_attributes必须是法术名到两项属性的对象。",
                "例如：{\"诅咒吐息\":[\"力量\",\"意志\"]}。",
            )

        known = {
            normalize_spell_name(name)
            for name in known_spells
            if str(name or "").strip()
        }
        result: dict[str, list[str]] = {}
        allowed_pairs = {
            ("MIG", "WLP"),
            ("INS", "WLP"),
        }
        for raw_name, raw_attributes in value.items():
            spell_name = normalize_spell_name(str(raw_name or ""))
            if spell_name not in known:
                return {}, cls._failure(
                    "create_npc_combatant",
                    "NPC_SPELL_ATTRIBUTES_UNKNOWN_SPELL",
                    f"【{spell_name or raw_name}】不在该NPC已学法术中。",
                    "只为施法者技能已选定的法术指定属性。",
                )
            definition = get_spell_definition(spell_name)
            performs_check = (
                (
                    definition.effect_type
                    in {
                        SpellEffectType.DAMAGE,
                        SpellEffectType.MP_DAMAGE,
                        SpellEffectType.STATUS_APPLY,
                    }
                    and not definition.automatic_effect
                    and not definition.fixed_damage_only
                )
                or definition.requires_check
            )
            if not performs_check:
                return {}, cls._failure(
                    "create_npc_combatant",
                    "NPC_SPELL_ATTRIBUTES_NOT_APPLICABLE",
                    f"【{spell_name}】不进行施法检定，不能指定检定属性。",
                    "删除这个法术的spell_attributes条目。",
                )
            if (
                not isinstance(raw_attributes, list)
                or len(raw_attributes) != 2
                or any(not isinstance(item, str) for item in raw_attributes)
            ):
                return {}, cls._failure(
                    "create_npc_combatant",
                    "NPC_SPELL_ATTRIBUTES_INVALID",
                    f"【{spell_name}】必须提交两项属性。",
                    "使用[力量,意志]或[洞察,意志]。",
                )
            normalized = tuple(
                cls._ATTRIBUTE_ALIASES.get(cls._clean(item), "")
                for item in raw_attributes
            )
            if normalized not in allowed_pairs:
                return {}, cls._failure(
                    "create_npc_combatant",
                    "NPC_SPELL_ATTRIBUTES_INVALID",
                    f"【{spell_name}】只能使用【力量+意志】或【洞察+意志】。",
                    "按该NPC的法术表现选择一组规则允许的属性。",
                )
            result[spell_name] = list(normalized)
        return result, None

    @classmethod
    def _validate_attack(
        cls,
        value: object,
    ) -> tuple[dict[str, object], GMToolReceipt | None]:
        if not isinstance(value, dict):
            return {}, cls._failure("create_npc_combatant", "NPC_ATTACK_MUST_BE_OBJECT", "基础攻击必须是对象。", "提交名称、两项属性、伤害类型和范围。")
        allowed = {
            "name",
            "attributes",
            "damage_type",
            "damage_bonus",
            "accuracy_modifier",
            "range",
            "targets_magic_defense",
            "multi_attack",
            "status_effect_on_hit",
            "notes",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            return {}, cls._failure("create_npc_combatant", "UNKNOWN_NPC_ATTACK_FIELD", "基础攻击包含未声明字段：" + "、".join(unknown), "删除未知字段；复杂效果写入notes或NPC技能。")
        name = cls._clean(value.get("name"))
        raw_attributes = value.get("attributes")
        if not name:
            return {}, cls._failure("create_npc_combatant", "NPC_ATTACK_NAME_REQUIRED", "基础攻击必须有招式名称。", "使用符合NPC概念的表现名称。")
        if not isinstance(raw_attributes, list) or len(raw_attributes) != 2:
            return {}, cls._failure("create_npc_combatant", "NPC_ATTACK_ATTRIBUTES_REQUIRED", "基础攻击必须使用两项属性。", "从敏捷、洞察、力量、意志中选择，可重复。")
        attributes = [cls._ATTRIBUTE_ALIASES.get(cls._clean(item), "") for item in raw_attributes]
        if not all(attributes):
            return {}, cls._failure("create_npc_combatant", "NPC_ATTACK_ATTRIBUTE_INVALID", "基础攻击包含未知属性。", "使用中文属性：敏捷、洞察、力量、意志。")
        try:
            damage_type = normalize_damage_type(cls._clean(value.get("damage_type")) or "physical")
        except ValueError as exc:
            return {}, cls._failure("create_npc_combatant", "NPC_ATTACK_DAMAGE_TYPE_INVALID", str(exc), "使用九种标准伤害类型之一。")
        attack_range = cls._clean(value.get("range")) or "melee"
        if attack_range not in {"melee", "ranged"}:
            return {}, cls._failure("create_npc_combatant", "NPC_ATTACK_RANGE_INVALID", "攻击范围必须是melee或ranged。", "根据招式选择近战或远程。")
        try:
            damage_bonus = int(value.get("damage_bonus") or 0)
            accuracy_modifier = int(value.get("accuracy_modifier") or 0)
            multi_attack = int(value.get("multi_attack") or 1)
        except (TypeError, ValueError):
            return {}, cls._failure("create_npc_combatant", "NPC_ATTACK_NUMBER_INVALID", "攻击数值字段必须是整数。", "修正damage_bonus、accuracy_modifier或multi_attack。")
        if not -5 <= damage_bonus <= 20 or not -3 <= accuracy_modifier <= 6 or not 1 <= multi_attack <= 3:
            return {}, cls._failure("create_npc_combatant", "NPC_ATTACK_NUMBER_OUT_OF_RANGE", "攻击修正超出安全范围。", "伤害修正在-5到20、命中修正在-3到6、多重攻击在1到3。")
        notes = value.get("notes") or []
        if not isinstance(notes, list) or any(not isinstance(item, str) for item in notes):
            return {}, cls._failure("create_npc_combatant", "NPC_ATTACK_NOTES_MUST_BE_ARRAY", "攻击notes必须是字符串数组。", "没有附加说明时提交空数组。")
        raw_status = cls._clean(value.get("status_effect_on_hit"))
        try:
            on_hit_status = normalize_status(raw_status) if raw_status else None
        except ValueError as exc:
            return {}, cls._failure(
                "create_npc_combatant",
                "NPC_ATTACK_STATUS_INVALID",
                str(exc),
                "使用迟缓、眩晕、虚弱、动摇、激怒或中毒；没有附加状态时留空。",
            )
        return {
            "name": name,
            "attributes": attributes,
            "attribute_labels": [cls._ATTRIBUTE_LABELS[item] for item in attributes],
            "damage_type": damage_type,
            "damage_bonus": damage_bonus,
            "accuracy_modifier": accuracy_modifier,
            "range": attack_range,
            "targets_magic_defense": bool(value.get("targets_magic_defense")),
            "multi_attack": multi_attack,
            "status_effect_on_hit": on_hit_status,
            "notes": [cls._clean(item) for item in notes if cls._clean(item)],
        }, None

    @classmethod
    def _validate_combat_skill_configuration(
        cls,
        *,
        draft: Any,
        selected_skills: list[str],
        skill_options: object,
        attack: dict[str, object],
    ) -> GMToolReceipt | None:
        if not isinstance(skill_options, dict):
            return cls._failure(
                "create_npc_combatant",
                "NPC_SKILL_OPTIONS_MUST_BE_OBJECT",
                "skill_options必须是对象。",
                "按技能名分别提交结构化选项。",
            )
        canonical = [normalize_skill_reference_name(name) for name in selected_skills]
        counts = Counter(canonical)

        exact_lengths = {
            "伤害抵抗": counts["伤害抵抗"] * 2,
            "伤害免疫": counts["伤害免疫"],
            "伤害吸收": counts["伤害吸收"],
            "异常状态免疫": counts["异常状态免疫"] * 2,
            "专精": counts["专精"],
            "强化防御": counts["强化防御"],
            "强化伤害": counts["强化伤害"],
        }
        for skill_name, expected in exact_lengths.items():
            if expected <= 0:
                continue
            actual = cls._skill_option_list(skill_options, skill_name)
            if len(actual) != expected:
                return cls._failure(
                    "create_npc_combatant",
                    "NPC_SKILL_OPTION_COUNT_INVALID",
                    f"技能【{skill_name}】需要 {expected} 个结构化选项，当前为 {len(actual)} 个。",
                    "按工具说明补齐选项；不要用自由文本代替规则参数。",
                    result={
                        "skill_name": skill_name,
                        "expected": expected,
                        "actual": len(actual),
                    },
                )

        defense_choices = {
            item.lower()
            for item in cls._skill_option_list(skill_options, "强化防御")
        }
        if defense_choices - {"physical", "物防", "物理", "magic", "魔防", "魔法"}:
            return cls._failure(
                "create_npc_combatant",
                "NPC_DEFENSE_OPTION_INVALID",
                "强化防御只能选择physical/物防或magic/魔防。",
                "修正强化防御的选项。",
            )

        specialties = cls._skill_option_list(skill_options, "专精")
        allowed_specialties = {
            "命中检定",
            "施法检定",
            "妨碍检定",
            "调查检定",
            "推进目标检定",
        }
        invalid_specialties = [
            item for item in specialties if item not in allowed_specialties
        ]
        if invalid_specialties:
            return cls._failure(
                "create_npc_combatant",
                "NPC_SPECIALTY_UNSUPPORTED",
                "专精选项当前无法由规则层执行：" + "、".join(invalid_specialties),
                "使用命中检定、施法检定、妨碍检定、调查检定或推进目标检定。",
            )
        if len(set(specialties)) != len(specialties):
            return cls._failure(
                "create_npc_combatant",
                "NPC_SPECIALTY_DUPLICATE",
                "专精不能重复用于同一种检定。",
                "为每次专精选择不同的检定。",
            )

        spell_count = counts["施法者"]
        spell_options = cls._skill_option_list(skill_options, "施法者")
        if spell_count:
            minimum = spell_count
            maximum = spell_count * 2
            if not minimum <= len(spell_options) <= maximum:
                return cls._failure(
                    "create_npc_combatant",
                    "NPC_SPELLCASTER_OPTIONS_INVALID",
                    f"施法者选择 {spell_count} 次时必须学习 {minimum} 到 {maximum} 个可执行法术。",
                    "每次施法者选择一个法术并获得10最大MP，或选择两个法术。",
                )
            unknown_spells = [
                name
                for name in spell_options
                if not is_known_spell(normalize_spell_name(name))
            ]
            if unknown_spells:
                return cls._failure(
                    "create_npc_combatant",
                    "NPC_SPELL_NOT_EXECUTABLE",
                    "以下法术尚无硬规则定义：" + "、".join(unknown_spells),
                    "从规则检索工具返回的可执行法术中选择；自定义法术需先建立结构化规则。",
                )

        enhanced_targets = cls._skill_option_list(skill_options, "强化伤害")
        invalid_damage_targets = [
            target
            for target in enhanced_targets
            if target not in {"基础攻击", "攻击", str(attack.get("name") or "")}
            and (
                normalize_spell_name(target) not in draft.known_spells
                or not is_known_spell(normalize_spell_name(target))
            )
        ]
        if invalid_damage_targets:
            return cls._failure(
                "create_npc_combatant",
                "NPC_ENHANCED_DAMAGE_TARGET_INVALID",
                "强化伤害必须绑定基础攻击或该NPC已学会的法术：" + "、".join(invalid_damage_targets),
                "逐项填写“基础攻击”、基础攻击名称或一个已学会法术名。",
            )

        special_attack_count = counts["特殊攻击"]
        if special_attack_count:
            hard_effect_count = sum(
                (
                    int(attack.get("multi_attack") or 1) > 1,
                    bool(attack.get("targets_magic_defense")),
                    attack.get("status_effect_on_hit") is not None,
                )
            )
            if hard_effect_count < special_attack_count:
                return cls._failure(
                    "create_npc_combatant",
                    "NPC_SPECIAL_ATTACK_EFFECT_MISSING",
                    f"选择了 {special_attack_count} 次特殊攻击，但基础攻击只有 {hard_effect_count} 项可执行特殊效果。",
                    "用multi_attack、targets_magic_defense或status_effect_on_hit为每次选择提供一项硬规则效果。",
                )

        unsupported_dynamic = [
            skill_name
            for skill_name in ("危机效果", "最后一搏", "反应", "特殊行动")
            if counts[skill_name]
        ]
        if unsupported_dynamic:
            return cls._failure(
                "create_npc_combatant",
                "NPC_DYNAMIC_SKILL_REQUIRES_TYPED_PROFILE",
                "这些技能需要尚未提交的触发与效果配置：" + "、".join(unsupported_dynamic),
                "暂时改选已有硬规则的NPC或职业技能；不要让空壳能力进入战斗。",
            )

        for skill_name in canonical:
            if skill_name in NPC_SKILL_INDEX:
                continue
            reference = get_skill_reference(skill_name)
            coverage = skill_implementation_coverage(skill_name)
            if reference is None or reference.kind != "class":
                return cls._failure(
                    "create_npc_combatant",
                    "UNKNOWN_NPC_OR_CLASS_SKILL",
                    f"未找到可用技能【{skill_name}】。",
                    "从NPC技能或职业技能检索结果中选择。",
                )
            if coverage is None or coverage.category not in {
                SKILL_COVERAGE_HARD_RULE,
                SKILL_COVERAGE_PASSIVE_HARD,
            }:
                return cls._failure(
                    "create_npc_combatant",
                    "NPC_CLASS_SKILL_NOT_EXECUTABLE",
                    f"职业技能【{skill_name}】尚不能由规则层完整结算。",
                    "改选标记为hard_rule或passive_hard的职业技能。",
                )
        return None

    @staticmethod
    def _skill_option_list(
        skill_options: object,
        skill_name: str,
    ) -> list[str]:
        if not isinstance(skill_options, dict):
            return []
        raw = skill_options.get(skill_name, [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        return [str(item).strip() for item in raw if str(item).strip()]

    @classmethod
    def _combat_draft_payload(cls, draft: Any) -> dict[str, object]:
        return {
            "name": draft.name,
            "level": draft.level,
            "species": draft.species.slug,
            "species_name": draft.species.name,
            "rank": draft.rank.value,
            "traits": list(draft.traits),
            "attributes": {
                cls._ATTRIBUTE_LABELS.get(key, key): value
                for key, value in draft.attributes.items()
            },
            "max_hp": draft.max_hp,
            "crisis_threshold": draft.crisis_threshold,
            "max_mp": draft.max_mp,
            "initiative": draft.initiative,
            "defenses": dict(draft.defenses),
            "affinities": {key: value.value for key, value in draft.affinities.items()},
            "status_immunities": [status.value for status in draft.status_immunities],
            "check_bonus": draft.check_bonus,
            "extra_damage": draft.extra_damage,
            "skill_budget": draft.skill_budget,
            "selected_skills": [skill.name for skill in draft.selected_skills],
            "specialty_bonuses": dict(draft.specialty_bonuses),
            "skill_effects": copy.deepcopy(draft.skill_effects),
            "known_spells": list(draft.known_spells),
            "action_count": draft.action_count,
            "soldier_equivalent": draft.soldier_equivalent,
            "rank_notes": list(draft.rank_notes),
            "design_checklist": list(draft.design_checklist),
            "notes": list(draft.notes),
        }

    @staticmethod
    def _response_contracts(app: Any, npc_name: str) -> list[str]:
        frame = app.scene_frame_manager.current_frame
        if frame is None:
            return []
        contracts: list[str] = []
        for item in frame.open_conditions:
            if str(item.get("status") or "open") != "open":
                continue
            if str(item.get("npc") or "").strip() != npc_name:
                continue
            condition = str(item.get("condition") or "").strip()
            result = str(item.get("promised_result") or "").strip()
            if condition and result:
                if str(item.get("player_fulfillment") or "pending") == "fulfilled":
                    contracts.append(
                        f"玩家已完成公开条件：{condition}；本轮必须实际兑现：{result}"
                    )
                else:
                    contracts.append(f"公开条件：{condition}；满足后必须兑现：{result}")
        return contracts[-4:]

    @staticmethod
    def _commit_speech_contracts(
        app: Any,
        npc_name: str,
        player_message: str,
        public_reply: str,
        plan: dict[str, Any],
    ) -> dict[str, object]:
        condition = str(plan.get("condition") or "").strip()
        promised_result = str(plan.get("promised_result") or "").strip()
        recorded_condition: dict[str, str] | None = None
        if condition and promised_result:
            recorded_condition = app.scene_frame_manager.record_condition(
                npc=npc_name,
                condition=condition,
                promised_result=promised_result,
                promise_kind=str(plan.get("promise_kind") or ""),
                promise_subject=str(plan.get("promise_subject") or ""),
                scene=app.scene_manager.current_scene,
            )
        proposal_outcome = str(plan.get("proposal_outcome") or "none")
        settled_terms = str(plan.get("settled_terms") or "").strip()
        settlement: dict[str, str] | None = None
        if proposal_outcome in {"accepted", "rejected"} and settled_terms:
            settlement = app.scene_frame_manager.record_settled_exchange(
                npc=npc_name,
                player_offer=player_message,
                npc_response=public_reply,
                outcome=proposal_outcome,
                settled_terms=settled_terms,
            )
        commitment_manager = (
            app.scene_frame_manager.npc_deferred_commitment_manager
        )
        resolved_commitments = commitment_manager.resolve_from_public_answer(
            app.scene_frame_manager.current_frame,
            npc=npc_name,
            public_statement=public_reply,
            speech_plan=plan,
        )
        recorded_commitment = commitment_manager.record_from_public_answer(
            app.scene_frame_manager.current_frame,
            npc=npc_name,
            public_statement=public_reply,
            speech_plan=plan,
        )
        return {
            "settled_exchange": settlement,
            "recorded_condition": recorded_condition,
            "resolved_commitments": resolved_commitments,
            "recorded_commitment": recorded_commitment,
        }

    @classmethod
    def _requested_open_condition(
        cls,
        app: Any,
        npc_name: str,
        condition_id: str,
    ) -> dict[str, str] | GMToolReceipt | None:
        if not condition_id:
            return None
        frame = app.scene_frame_manager.current_frame
        if frame is None:
            return cls._failure(
                "decide_npc_response",
                "SCENE_CONDITION_NOT_FOUND",
                f"没有找到仍开放的条件【{condition_id}】。",
                "先调用get_scene_state并使用当前open_conditions中的condition_id。",
            )
        condition = next(
            (
                item
                for item in frame.open_conditions
                if str(item.get("condition_id") or "").strip() == condition_id
                and str(item.get("status") or "open") == "open"
            ),
            None,
        )
        if condition is None:
            return cls._failure(
                "decide_npc_response",
                "SCENE_CONDITION_NOT_FOUND",
                f"条件【{condition_id}】不存在或已经结束。",
                "重新读取场景状态，不要兑现过期条件。",
            )
        owner = cls._clean(condition.get("npc"))
        if owner and owner != npc_name:
            return cls._failure(
                "decide_npc_response",
                "SCENE_CONDITION_NPC_MISMATCH",
                f"条件【{condition_id}】属于【{owner}】，不是【{npc_name}】。",
                "让条件记录中的NPC本人判断并兑现。",
            )
        return condition

    @classmethod
    def _requested_pending_commitment(
        cls,
        app: Any,
        npc_name: str,
        commitment_id: str,
    ) -> dict[str, str] | GMToolReceipt | None:
        if not commitment_id:
            return None
        frame = app.scene_frame_manager.current_frame
        commitment = (
            app.scene_frame_manager.npc_deferred_commitment_manager.find_pending(
                frame,
                commitment_id,
            )
        )
        if commitment is None:
            return cls._failure(
                "decide_npc_response",
                "NPC_COMMITMENT_NOT_FOUND",
                f"短期承诺【{commitment_id}】不存在或已经结束。",
                "重新读取scene.pending_npc_commitments；不要拼接、猜测或复用过期ID。",
            )
        if cls._clean(commitment.get("trigger_status")).lower() != "reached":
            return cls._failure(
                "decide_npc_response",
                "NPC_COMMITMENT_TRIGGER_NOT_REACHED",
                f"短期承诺【{commitment_id}】尚未到达公开触发点。",
                "先用实际行动抵达或满足trigger；不能让NPC提前兑现，也不能用对话伪造抵达。",
            )
        responder = cls._clean(commitment.get("trigger_responder"))
        if not responder or responder != npc_name:
            return cls._failure(
                "decide_npc_response",
                "NPC_COMMITMENT_RESPONDER_MISMATCH",
                (
                    f"短期承诺【{commitment_id}】应由【{responder or '未指定'}】兑现，"
                    f"不是【{npc_name}】。"
                ),
                "使用承诺记录中的trigger_responder；最初作出安排的人不一定是现场执行者。",
            )
        return commitment

    @classmethod
    def _requested_pending_question(
        cls,
        app: Any,
        npc_name: str,
        question_id: str,
    ) -> dict[str, str] | GMToolReceipt | None:
        frame = app.scene_frame_manager.current_frame
        open_questions = [
            item
            for item in list(getattr(frame, "pending_npc_questions", []) or [])
            if str(item.get("status") or "open") == "open"
        ]
        if not question_id:
            return None
        question = next(
            (
                item
                for item in open_questions
                if str(item.get("question_id") or "").strip() == question_id
            ),
            None,
        )
        if question is None:
            return cls._failure(
                "decide_npc_response",
                "NPC_PENDING_QUESTION_NOT_FOUND",
                f"待答事项【{question_id}】不存在或已经结束。",
                "重新读取scene.pending_npc_questions并使用精确ID。",
            )
        owner = cls._clean(question.get("npc"))
        if owner and owner != "未具名发问者" and owner != npc_name:
            return cls._failure(
                "decide_npc_response",
                "NPC_PENDING_QUESTION_OWNER_MISMATCH",
                f"待答事项【{question_id}】由【{owner}】提出，不属于【{npc_name}】。",
                "让实际提出该事项且仍在场的NPC回应。",
            )
        return question

    @classmethod
    def _validate_profile(
        cls,
        value: object,
    ) -> tuple[dict[str, Any], GMToolReceipt | None]:
        if not isinstance(value, dict):
            return {}, cls._failure("create_npc_profile", "NPC_PROFILE_MUST_BE_OBJECT", "NPC profile必须是对象。", "按工具schema提交人格字段。")
        unknown = sorted(set(value) - cls._PROFILE_SCALARS - cls._PROFILE_LISTS)
        if unknown:
            return {}, cls._failure("create_npc_profile", "UNKNOWN_NPC_PROFILE_FIELD", "NPC档案包含未声明字段：" + "、".join(unknown), "删除未知字段；规则数值应由NPC设计工具管理。")
        result: dict[str, Any] = {}
        for key in cls._PROFILE_SCALARS:
            if key in value:
                result[key] = cls._clean(value.get(key))
        for key in cls._PROFILE_LISTS:
            if key not in value:
                continue
            raw = value.get(key)
            if not isinstance(raw, list):
                return {}, cls._failure("create_npc_profile", "NPC_PROFILE_LIST_REQUIRED", f"字段{key}必须是数组。", "改成字符串数组后重新提交。")
            result[key] = list(dict.fromkeys(cls._clean(item) for item in raw if cls._clean(item)))[:20]
        rank = str(result.get("npc_rank") or "minor")
        if rank not in cls._RANKS:
            return {}, cls._failure("create_npc_profile", "INVALID_NPC_RANK", "NPC阶级不在允许值中。", "使用minor、supporting、elite、villain或boss。")
        result["npc_rank"] = rank
        entity_kind = str(result.get("entity_kind") or "individual")
        if entity_kind not in cls._ENTITY_KINDS:
            return {}, cls._failure(
                "create_npc_profile",
                "INVALID_NPC_ENTITY_KIND",
                f"不支持NPC主体类型【{entity_kind}】。",
                "单个人物使用individual；队伍、议会、守卫群或人群使用collective。",
            )
        result["entity_kind"] = entity_kind
        if rank in {"villain", "boss"} and not result.get("active_goal") and not result.get("core_drive"):
            return {}, cls._failure("create_npc_profile", "MAJOR_NPC_GOAL_REQUIRED", "重要反派或首领必须有当前目标。", "补充active_goal或core_drive，避免其只剩数值。")
        return result, None

    @staticmethod
    def _ensure_persona_from_profile(
        app: Any,
        scene: Any,
        name: str,
        profile: dict[str, Any],
    ) -> Any:
        return app.world_state.ensure_npc_persona(
            name,
            profile_status="established",
            entity_kind=str(profile.get("entity_kind") or "individual"),
            aliases=profile.get("aliases"),
            public_identity=str(profile.get("public_identity") or name),
            role_in_story=str(profile.get("role_in_story") or "当前场景人物"),
            core_drive=str(profile.get("core_drive") or "根据自身处境作出选择"),
            manner=str(profile.get("manner") or "自然、具体"),
            speech_style=str(profile.get("speech_style") or "直接回答，不复述对方"),
            combat_style=str(profile.get("combat_style") or ""),
            npc_rank=str(profile.get("npc_rank") or "minor"),
            leverage=str(profile.get("leverage") or ""),
            authority_scope=str(profile.get("authority_scope") or "只能决定自身行动"),
            knowledge_scope=str(profile.get("knowledge_scope") or "只知道亲历和当前可见信息"),
            refusal_move=str(profile.get("refusal_move") or "明确拒绝并采取符合目标的行动"),
            known_skills=profile.get("known_skills"),
            combat_actions=profile.get("combat_actions"),
            first_scene=str(scene.name or ""),
            goals=profile.get("goals"),
            taboos=profile.get("taboos"),
            secrets=profile.get("secrets"),
            current_location=str(scene.location or scene.name or ""),
            current_mood=str(profile.get("current_mood") or ""),
            current_stance=str(profile.get("current_stance") or ""),
            active_goal=str(profile.get("active_goal") or profile.get("core_drive") or ""),
            last_seen_scene=str(scene.scene_id or ""),
            voice_examples=profile.get("voice_examples"),
        )

    @staticmethod
    def _persona_is_present(persona: Any, scene: Any) -> bool:
        participants = set(getattr(scene, "participants", []) or [])
        location = str(getattr(scene, "location", "") or getattr(scene, "name", "") or "")
        return bool(
            persona.name in participants
            or any(alias in participants for alias in persona.aliases)
            or (location and persona.current_location == location)
        )

    @staticmethod
    def _npc_authority_state(app: Any, scene: Any, *, npc_name: str) -> dict[str, object]:
        """Expose typed rule authority to the explicitly invoked NPC agent."""

        scene_location = str(
            getattr(scene, "location", "") or getattr(scene, "name", "") or ""
        ).strip()
        frame = getattr(app.scene_frame_manager, "current_frame", None)
        if frame is not None and str(getattr(frame, "source_scene_id", "") or "") != str(
            getattr(scene, "scene_id", "") or ""
        ):
            frame = None
        participant_locations = dict(
            getattr(scene, "participant_locations", {}) or {}
        )
        participant_positions = dict(
            getattr(scene, "participant_positions", {}) or {}
        )
        canonical_npc_name = (
            app.world_state.resolve_npc_name(str(npc_name or "").strip())
            or str(npc_name or "").strip()
        )
        npc_position = str(
            participant_positions.get(canonical_npc_name)
            or participant_positions.get(str(npc_name or "").strip())
            or ""
        ).strip()
        story_items: list[dict[str, object]] = []
        public_constraints: list[dict[str, object]] = []
        authority_facts: list[str] = [
            f"NPC【{str(npc_name or '').strip()}】当前位置={scene_location or '未记录'}"
        ]
        if npc_position:
            authority_facts.append(
                f"NPC【{str(npc_name or '').strip()}】当前场内精确站位={npc_position}"
            )
        for item in app.world_state.story_items.values():
            status = str(getattr(item.status, "value", item.status) or "")
            related_facts: list[str] = []
            for subject, facts in app.world_state.subject_facts.items():
                clean_subject = str(subject or "").strip()
                clean_facts = [str(fact or "").strip() for fact in facts if str(fact or "").strip()]
                if not clean_subject or not clean_facts:
                    continue
                if item.name in clean_subject or any(item.name in fact for fact in clean_facts):
                    for fact in clean_facts:
                        if fact not in related_facts:
                            related_facts.append(fact)
                        if len(related_facts) >= 8:
                            break
                if len(related_facts) >= 8:
                    break
            payload = {
                "item_id": item.item_id,
                "name": item.name,
                "description": item.description,
                "holder": item.holder,
                "location": item.location,
                "status": status,
                "tags": list(item.tags),
                "related_public_facts": related_facts,
            }
            story_items.append(payload)
            authority_facts.append(
                f"剧情物件【{item.name}】：状态={status or '未知'}；"
                f"持有者={item.holder or '无'}；位置={item.location or '未记录'}"
            )
            for fact in related_facts:
                public_constraints.append(
                    {
                        "subject": item.name,
                        "fact": fact,
                        "source": "world_subject_fact",
                    }
                )
                authority_facts.append(f"剧情物件【{item.name}】相关公开事实：{fact}")

        open_conditions = [
            dict(item)
            for item in list(getattr(frame, "open_conditions", []) or [])
        ]
        scene_condition_ids = {
            str(item.get("condition_id") or "").strip()
            for item in open_conditions
            if str(item.get("condition_id") or "").strip()
        }
        for item in list(getattr(scene, "open_conditions", []) or []):
            payload = dict(item)
            condition_id = str(payload.get("condition_id") or "").strip()
            if condition_id and condition_id in scene_condition_ids:
                continue
            open_conditions.append(payload)
            if condition_id:
                scene_condition_ids.add(condition_id)
        unresolved_conditions = [
            dict(item)
            for item in open_conditions
            if str(item.get("status") or "open").strip().lower() == "open"
        ]
        for item in unresolved_conditions:
            condition = str(item.get("condition") or "").strip()
            promised_result = str(item.get("promised_result") or "").strip()
            if str(item.get("player_fulfillment") or "pending") == "fulfilled":
                authority_facts.append(
                    f"玩家已完成条件【{condition or item.get('condition_id') or '未命名'}】；"
                    f"NPC尚欠且必须兑现={promised_result or '未记录'}"
                )
            else:
                authority_facts.append(
                    f"未完成条件【{condition or item.get('condition_id') or '未命名'}】；"
                    f"完成后结果={promised_result or '未记录'}"
                )
        pending_commitments = [
            dict(item)
            for item in (
                app.scene_frame_manager.npc_deferred_commitment_manager.pending(
                    frame
                )
                if frame is not None
                else []
            )
        ][-8:]
        for item in pending_commitments:
            commitment_id = str(item.get("commitment_id") or "").strip()
            trigger_status = str(
                item.get("trigger_status") or "waiting"
            ).strip()
            responder = str(item.get("trigger_responder") or "").strip()
            authority_facts.append(
                f"NPC短期承诺【{commitment_id or '未命名'}】："
                f"触发={item.get('trigger') or '未记录'}；"
                f"状态={trigger_status}；"
                f"兑现者={responder or item.get('npc') or '未记录'}；"
                f"应兑现={item.get('promised_result') or '未记录'}"
            )
            if trigger_status == "reached":
                authority_facts.append(
                    f"规则工具已确认短期承诺【{commitment_id or '未命名'}】"
                    f"在【{item.get('trigger_location') or item.get('trigger') or '触发点'}】"
                    "到期；较早公开文本中描述的途中位置已被本次类型化移动更新。"
                )

        clock_boundaries = ClockNarrativeBoundary.packet(app.clock_manager.all())
        for boundary in clock_boundaries:
            authority_facts.append(
                f"命刻【{boundary.get('name') or '未命名'}】"
                f"={boundary.get('current') or 0}/{boundary.get('maximum') or 0}；"
                f"本轮允许叙事阶段={boundary.get('authorized_stage') or '未记录'}；"
                "填满后果尚未发生"
            )

        scene_state = {
            "public_facts": list(getattr(frame, "public_facts", []) or [])[-12:],
            "established_facts": list(getattr(frame, "established_facts", []) or [])[-12:],
            "committed_consequences": list(
                getattr(frame, "committed_consequences", []) or []
            )[-8:],
            "open_conditions": open_conditions[-8:],
            "unresolved_conditions": unresolved_conditions[-8:],
            "settled_exchanges": [
                dict(item)
                for item in list(getattr(frame, "settled_exchanges", []) or [])[-8:]
            ],
            "pending_npc_questions": [
                NPCResponseWindowManager.public_question(item)
                for item in list(getattr(frame, "pending_npc_questions", []) or [])
                if str(item.get("status") or "open").strip().lower() == "open"
                and str(item.get("kind") or "") == "player_response"
            ][-6:],
            "pending_npc_commitments": pending_commitments,
        }
        return {
            "npc": {
                "name": str(npc_name or "").strip(),
                "location": scene_location,
                "position": npc_position,
            },
            "scene": {
                "name": str(getattr(scene, "name", "") or "").strip(),
                "location": scene_location,
                "participants": list(getattr(scene, "participants", []) or []),
                "participant_locations": participant_locations,
                "participant_positions": participant_positions,
            },
            "scene_state": scene_state,
            "character_locations": {
                character.name: app.scene_manager.location_of(character.name)
                for character in app.character_manager.all()
            },
            "story_items": story_items,
            "public_constraints": public_constraints[-24:],
            "clock_boundaries": clock_boundaries,
            "authority_facts": authority_facts,
            "usage_rule": (
                "仅用于约束动作合法性，不自动赋予NPC对未公开物件的知识；"
                "holder存在时，只有该holder能够接触、转交或使用物件。"
                "GM节拍目的和NPC权限不是完成事实；未完成条件、公开机关步骤与剧情物件用法不得被普通叙事绕过。"
                "clock_boundaries中的authorized_stage是当前命刻叙事上限；命刻填满前不得声称completion_consequence已经发生。"
            ),
        }

    @staticmethod
    def _persona_payload(app: Any, persona: Any, *, include_private: bool) -> dict[str, object]:
        public_interaction_memories = [
            str(record.get("note") or "").strip()
            for record in list(getattr(persona, "memory_records", []) or [])[-12:]
            if bool(record.get("witnessed"))
            and str(record.get("source") or "") in {"direct_dialogue", "public_statement"}
            and str(record.get("note") or "").strip()
        ][-4:]
        payload: dict[str, object] = {
            "npc_id": persona.npc_id,
            "name": persona.name,
            "profile_status": str(
                getattr(persona, "profile_status", "established")
                or "established"
            ),
            "entity_kind": str(getattr(persona, "entity_kind", "individual") or "individual"),
            "aliases": list(persona.aliases),
            "public_identity": persona.public_identity,
            "role_in_story": persona.role_in_story,
            "npc_rank": persona.npc_rank,
            "manner": persona.manner,
            "speech_style": persona.speech_style,
            "current_location": persona.current_location,
            "current_mood": persona.current_mood,
            "current_stance": persona.current_stance,
            "status": persona.status,
            "recent_public_statements": [
                str(item.get("statement") or "")
                for item in app.world_state.npc_public_statement_history(persona.name)[-4:]
            ],
            "recent_public_interactions": public_interaction_memories,
        }
        if include_private:
            payload.update(
                {
                    "core_drive": persona.core_drive,
                    "active_goal": persona.active_goal,
                    "goals": list(persona.goals),
                    "taboos": list(persona.taboos),
                    "secrets": list(persona.secrets),
                    "leverage": persona.leverage,
                    "authority_scope": persona.authority_scope,
                    "knowledge_scope": persona.knowledge_scope,
                    "refusal_move": persona.refusal_move,
                    "combat_style": persona.combat_style,
                    "known_skills": list(persona.known_skills),
                    "combat_actions": list(persona.combat_actions),
                    "relationships": dict(persona.relationships),
                }
            )
        return payload

    @classmethod
    def _validate_evidence(
        cls,
        context: GMToolExecutionContext,
        value: object,
        tool_name: str,
    ) -> GMToolReceipt | None:
        if not is_current_message_evidence(context, value):
            return cls._failure(tool_name, "EVIDENCE_NOT_IN_CURRENT_MESSAGE", "evidence不是当前消息中的逐字连续片段。", "从current_message逐字复制依据，不使用摘要。")
        return None

    @staticmethod
    def _clean(value: object) -> str:
        return " ".join(str(value or "").split()).strip()

    @staticmethod
    def _optional_bounded_integer(
        payload: dict[str, object],
        field_name: str,
        *,
        minimum: int,
        maximum: int,
    ) -> tuple[int | None, str]:
        if field_name not in payload or payload.get(field_name) is None:
            return None, ""
        value = payload.get(field_name)
        if isinstance(value, bool) or not isinstance(value, int):
            return None, "必须是整数"
        if not minimum <= value <= maximum:
            return None, f"必须位于{minimum}到{maximum}之间"
        return value, ""

    @classmethod
    def _string_list(
        cls,
        value: object,
        *,
        field_name: str,
    ) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError(f"{field_name}必须是数组")
        result: list[str] = []
        for item in value:
            if not isinstance(item, str) or not cls._clean(item):
                raise ValueError(f"{field_name}只能包含非空字符串")
            result.append(cls._clean(item))
        return result

    @classmethod
    def _string_map(
        cls,
        value: object,
        *,
        field_name: str,
    ) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(f"{field_name}必须是对象")
        result: dict[str, str] = {}
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not cls._clean(key)
                or not isinstance(item, str)
                or not cls._clean(item)
            ):
                raise ValueError(f"{field_name}的键和值都必须是非空字符串")
            result[cls._clean(key)] = cls._clean(item)
        return result

    @staticmethod
    def _first_sentence(value: object) -> str:
        text = " ".join(str(value or "").split()).strip()
        if not text:
            return ""
        for marker in ("。", "！", "？", "!", "?"):
            if marker in text:
                return text.split(marker, 1)[0].strip() + marker
        return text[:300]

    @staticmethod
    def _failure(
        tool_name: str,
        code: str,
        message: str,
        hint: str,
        *,
        retryable: bool = True,
        result: dict[str, object] | None = None,
    ) -> GMToolReceipt:
        return GMToolReceipt(
            tool_name=tool_name,
            ok=False,
            error_code=code,
            message=message,
            correction_hint=hint,
            retryable=retryable,
            result=dict(result or {}),
            public_fallback_reply="",
        )
