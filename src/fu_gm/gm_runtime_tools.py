from __future__ import annotations

from dataclasses import asdict
import time
from typing import Any, Protocol
from uuid import uuid4

from fu_gm.check_difficulty import OPEN_CHECK_DIFFICULTY_GUIDANCE
from fu_gm.components.campaign_state_transaction import (
    CampaignStateSnapshot,
    CampaignStateTransaction,
)
from fu_gm.components.scene_creative_writer import SceneCreativeWriterError
from fu_gm.gm_evidence import is_current_message_evidence, normalize_literal_evidence
from fu_gm.gm_public_state_validation import unexpected_actor_mentions
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolPacingEvent,
    GMToolReceipt,
    GMToolRegistry,
)
from fu_gm.gm_decision_followups import (
    add_gm_opportunity_followups,
    required_followup_mode,
)
from fu_gm.models import Action, ActionType, GamePanel, SceneType
from fu_gm.session_gate import SessionGateSignal


class RuntimeToolHost(Protocol):
    session_gates: Any

    def _runtime(self, campaign_id: str, *, auto_load: bool = True) -> Any: ...

    def _autosave_campaign(self, runtime: Any, campaign_id: str) -> str: ...

    def _player_character_control_map(self, runtime: Any) -> dict[str, list[str]]: ...

    def _handle_gate_signal(
        self,
        payload: dict[str, Any],
        *,
        gate: Any,
        signal: SessionGateSignal,
    ) -> dict[str, Any]: ...

    def _end_session(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def _schedule_end_session_summary_enrichment(
        self,
        runtime: Any,
        result: dict[str, Any],
    ) -> dict[str, Any]: ...


class GMRuntimeToolService:
    """Typed host controls for sessions, scenes and conflict initiative.

    The language model chooses *when* these capabilities are appropriate. This
    service owns only structural validation and atomic state transitions; it
    deliberately performs no keyword or prose-intent recognition.
    """

    _GENERIC_SCENE_TYPES = {
        SceneType.STANDARD,
        SceneType.INTERLUDE,
        SceneType.GM,
    }
    _NPC_IDENTITY_TRAITS = frozenset({"npc", "enemy", "villain", "ally"})
    _FOCUS_SCENE_TYPES = {
        SceneType.STANDARD,
        SceneType.INTERLUDE,
        SceneType.GM,
        SceneType.REST,
        SceneType.TRAVEL,
        SceneType.DUNGEON,
    }
    _FOCUS_FOLLOWUP_TOOLS = [
        "perform_in_scene_action",
        "move_group_within_scene",
        "move_scene_group",
        "pass_in_scene_action",
        "perform_check_action",
        "perform_character_action",
        "perform_scene_action",
        "commit_story_item_action",
        "decide_npc_response",
    ]
    _SYSTEM_FOCUS_FOLLOWUP_TOOLS = ["commit_scene_response"]
    _FRAME_SCALARS = {
        "premise",
        "stakes",
        "current_pressure",
        "dramatic_question",
        "signature_image",
        "opposition_goal",
        "dilemma",
        "reversal",
        "climax_type",
        "closure_requirement",
        "irreversible_change",
        "ending_echo",
    }
    _FRAME_LISTS = {
        "fantastic_details",
        "escalation_ladder",
        "possible_payoffs",
        "visible_elements",
        "npc_functions",
        "clue_pool",
        "secrets",
        "possible_reveals",
        "telegraphed_threats",
        "danger_candidates",
        "discovery_candidates",
        "special_mechanism_candidates",
        "story_outline",
    }
    _HIDDEN_FRAME_FIELDS = {
        "reversal",
        "secrets",
        "possible_reveals",
        "story_outline",
    }
    _OPENING_SCENE_PREP_SCALARS = {
        "premise": "眼前局面的前提",
        "stakes": "玩家选择会改变什么",
        "current_pressure": "此刻正在变化的压力",
        "dramatic_question": "本场可获得局部答案的问题",
        "signature_image": "本场可回收的标志画面",
        "opposition_goal": "对立方当前想达成什么",
        "dilemma": "两个都合理但代价不同的方向",
        "closure_requirement": "本场何时算真正得到局部收束",
        "irreversible_change": "结局后不会无故复原的变化",
        "ending_echo": "收束时如何回收标志画面",
    }
    _OPENING_SCENE_PREP_LISTS = {
        "visible_elements": "开场即可接触的具体人、物或环境变化",
        "clue_pool": "可由不同方法追查的具体线索入口",
        "secrets": "尚未公开且可按玩家路径调整落点的真相",
        "possible_reveals": "行动成功或付出代价后可获得的信息",
        "escalation_ladder": "若局面没有改变时会实际发生的不同升级",
        "possible_payoffs": "玩家选择可能造成的不同局部结果",
    }

    def __init__(self, host: RuntimeToolHost) -> None:
        self.host = host

    def register_tools(self, registry: GMToolRegistry) -> None:
        registry.register(
            GMToolDefinition(
                name="get_runtime_state",
                description="读取当前会话门控、场景、冲突和当前行动者；不修改状态。",
                handler=self.get_runtime_state,
            )
        )
        registry.register(
            GMToolDefinition(
                name="start_session",
                description=(
                    "根据玩家明确同意开启开团前共识、第零章或冒险会话。"
                    "空白单人档里，玩家直接开始提供具体世界、小队或角色共创内容，"
                    "也属于明确开启第零章；先用本工具进入session_zero，再用第零章工具记录内容。"
                    "进入冒险会由规则层检查第零章与角色卡是否完成。"
                ),
                handler=self.start_session,
                parameters=(
                    GMToolParameter(
                        "phase",
                        "string",
                        "要进入的会话阶段。",
                        required=True,
                        enum=("pre_session", "session_zero", "adventure"),
                    ),
                    GMToolParameter("reason", "string", "玩家明确请求或共识的简短说明。", required=True),
                    GMToolParameter("evidence", "string", "当前消息中的逐字依据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="start_adventure",
                description=(
                    "玩家在第零章已完成且GM刚发出第一章邀请后，结合"
                    "conversation_anchor与最近聊天语义上接受邀请时，用一个原子事务"
                    "开启冒险并建立首场。短答无需独立重述第一章；模型只提交当前消息依据；"
                    "地点、角色、装备限制、私有局面与公开开场均由权威场次准备生成。"
                ),
                handler=self.start_adventure,
                parameters=(
                    GMToolParameter(
                        "reason",
                        "string",
                        "结合对话锚点判断玩家接受现在进入第一章的简短说明。",
                        required=True,
                    ),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前玩家消息的逐字依据；其含义由模型结合对话锚点判断。",
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
                name="pause_session",
                description="根据明确请求暂停当前跑团，但保留当前存档与场景。",
                handler=self.pause_session,
                parameters=(
                    GMToolParameter("reason", "string", "暂停原因。", required=True),
                    GMToolParameter("evidence", "string", "当前消息中的逐字依据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="end_session",
                description="根据明确收团请求结算经验、总结本场、保存并关闭当前会话。",
                handler=self.end_session,
                parameters=(
                    GMToolParameter("title", "string", "本场标题；可以留空。"),
                    GMToolParameter("public_reply", "string", "离线模式的可选后备收团话语。"),
                    GMToolParameter(
                        "closing_image",
                        "string",
                        (
                            "冒险场次必填：一至两句当前确实可见的结尾画面，"
                            "须让开场标志意象因本场实际选择发生可见变化，并逐字包含在public_reply中。"
                        ),
                    ),
                    GMToolParameter(
                        "deliberate_cliffhanger",
                        "boolean",
                        "未解决局面被有意停在明确悬念上时填true；普通暂停省略。",
                    ),
                    GMToolParameter(
                        "creative_direction",
                        "string",
                        "可选语义方向；不得把未完成目标写成已经成功。",
                    ),
                    GMToolParameter("evidence", "string", "当前消息中的逐字收团依据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="start_scene",
                description=(
                    "建立一个非冲突场景。核心GM提交地点、在场者和目标等语义事实；"
                    "专用DeepSeek创作作者生成GM私有局面与玩家开场，Python校验后原子写入。"
                    "这是局面准备，不是写死剧情；未公开内容之后可以调整。"
                ),
                handler=self.start_scene,
                parameters=(
                    GMToolParameter("name", "string", "场景名称。", required=True),
                    GMToolParameter(
                        "scene_type",
                        "string",
                        "非冲突场景类型。",
                        required=True,
                        enum=tuple(
                            item.value
                            for item in sorted(
                                self._GENERIC_SCENE_TYPES,
                                key=lambda item: item.value,
                            )
                        ),
                    ),
                    GMToolParameter("location", "string", "场景地点。", required=True),
                    GMToolParameter(
                        "participants",
                        "array",
                        (
                            "实际在场的角色与NPC名称，至少一名。第一章首场默认包含"
                            "current_state_summary.gameplay.characters中的全部PC，除非公开共识明确分队。"
                        ),
                        required=True,
                    ),
                    GMToolParameter(
                        "equipment_access_changes",
                        "array",
                        (
                            "可选；若开场公开处境已经使角色自有装备被收缴、封存或遗失，"
                            "必须在这里同步，不能只写进叙述。每项填写actor、mode、items，"
                            "可选reason、location；开场通常使用mode=restrict。"
                        ),
                        schema_details={
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "actor": {"type": "string"},
                                    "mode": {
                                        "type": "string",
                                        "enum": ["restrict", "restore"],
                                    },
                                    "items": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "reason": {"type": "string"},
                                    "location": {"type": "string"},
                                    "restore_loadout": {"type": "boolean"},
                                },
                                "required": ["actor", "mode", "items"],
                            }
                        },
                    ),
                    GMToolParameter("objective", "string", "当前场景公开目标或问题。"),
                    GMToolParameter(
                        "private_situation",
                        "object",
                        (
                            "离线模式的可选后备局面。在线运行时由专用DeepSeek创作作者根据"
                            "场次契约生成，核心GM不要自行编写。"
                        ),
                        schema_details=self._private_situation_schema_details(),
                    ),
                    GMToolParameter(
                        "public_opening",
                        "string",
                        (
                            "只面向玩家描述地点、眼前变化或压力，以及足以立即行动的具体可观察事物；"
                            "不列动作菜单，不解释幕后设计，也不替玩家角色行动。"
                        ),
                        required=False,
                    ),
                    GMToolParameter(
                        "player_handoff",
                        "string",
                        (
                            "紧接开场、把镜头交给实际在场玩家的一句自然开放问题。"
                            "问题必须立足眼前局面，询问角色此刻怎么做；不提供固定选项，不替角色决定。"
                            "有多名玩家角色在场时面向‘你们’，不要只把决定权交给当前发言者。"
                        ),
                        required=False,
                    ),
                    GMToolParameter(
                        "creative_direction",
                        "string",
                        (
                            "可选的语义方向，只说明这幕要实现的已确认意图；不写成品叙述、"
                            "秘密答案或玩家行动。DeepSeek会据此完成暗线与开场。"
                        ),
                    ),
                    GMToolParameter("evidence", "string", "当前消息中允许进入此场景的逐字依据。", required=True, source="current_message"),
                ),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )
        registry.register(
            GMToolDefinition(
                name="transition_scene",
                description=(
                    "为已经明确完成的整体场景切换建立新局面。普通玩家或分队跨场景移动优先使用"
                    "move_scene_group；只有当前镜头确实随移动者转入需要完整私有局面框架的新场景时才使用。"
                    "未移动者会保留在原并行场景，抵达描述只能出现目的地实际在场者；"
                    "不得提及未抵达角色，即使用‘某人不在这里’等否定句也不行，分离事实只写入transition_summary。"
                    "多人已经分别明确同意前往同一地点时，可在mover_consents中逐项附上其他玩家的原始公开发言，"
                    "一次原子转移全队；不得从沉默、提议或代为转述推断同意。"
                ),
                handler=self.transition_scene,
                parameters=(
                    GMToolParameter("name", "string", "抵达后的场景名称。", required=True),
                    GMToolParameter(
                        "scene_type",
                        "string",
                        "抵达后的非冲突场景类型。",
                        required=True,
                        enum=tuple(
                            item.value
                            for item in sorted(
                                self._GENERIC_SCENE_TYPES,
                                key=lambda item: item.value,
                            )
                        ),
                    ),
                    GMToolParameter("location", "string", "抵达地点。", required=True),
                    GMToolParameter("movers", "array", "本次明确移动的玩家角色名称。", required=True),
                    GMToolParameter(
                        "mover_consents",
                        "array",
                        (
                            "可选；移动非当前发言者控制的PC时逐项提供其本人先前的明确同意。"
                            "每项包含actor、speaker、evidence，evidence逐字摘录近期公开消息。"
                        ),
                        schema_details={
                            "items": {
                                "type": "object",
                                "properties": {
                                    "actor": {"type": "string"},
                                    "speaker": {"type": "string"},
                                    "evidence": {"type": "string"},
                                },
                                "required": ["actor", "speaker", "evidence"],
                                "additionalProperties": False,
                            }
                        },
                    ),
                    GMToolParameter("npc_companions", "array", "从当前场景实际随行的NPC名称。"),
                    GMToolParameter("destination_npcs", "array", "此前已确定在目的地等候的NPC名称。"),
                    GMToolParameter("objective", "string", "新场景当前公开目标或问题。"),
                    GMToolParameter(
                        "private_situation",
                        "object",
                        "离线模式的可选后备局面；在线运行时由DeepSeek创作作者生成。",
                        schema_details=self._private_situation_schema_details(),
                    ),
                    GMToolParameter("transition_summary", "string", "旧场景已经发生的客观收束；不得提前兑现目的地事件。", required=True),
                    GMToolParameter(
                        "public_arrival",
                        "string",
                        (
                            "面向玩家的抵达描述；只呈现抵达时已可观察的事实，且只能提及movers、"
                            "npc_companions和destination_npcs中的人物。未抵达者即使作为否定存在也不得提及。"
                        ),
                        required=False,
                    ),
                    GMToolParameter(
                        "creative_direction",
                        "string",
                        "可选语义方向；只说明抵达场景要承接什么，不写成品叙述或秘密。",
                    ),
                    GMToolParameter("evidence", "string", "当前消息中授权移动的逐字依据。", required=True, source="current_message"),
                ),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )
        registry.register(
            GMToolDefinition(
                name="focus_scene_branch",
                description=(
                    "当玩家角色不在当前镜头、但仍在另一个并行地点行动时，先把镜头切到该角色所在分支。"
                    "此工具只切换镜头并保留原分支，不结束场景、不公开叙事，也不代替随后的行动结算。"
                ),
                handler=self.focus_scene_branch,
                parameters=(
                    GMToolParameter("actor", "string", "需要获得镜头的玩家角色。", required=True),
                    GMToolParameter("name", "string", "该并行镜头的简短场景名。", required=True),
                    GMToolParameter(
                        "scene_type",
                        "string",
                        "并行镜头的非冲突场景类型。",
                        required=True,
                        enum=tuple(
                            item.value
                            for item in sorted(
                                self._GENERIC_SCENE_TYPES,
                                key=lambda item: item.value,
                            )
                        ),
                    ),
                    GMToolParameter("location", "string", "角色本次行动所在的具体地点。", required=True),
                    GMToolParameter("objective", "string", "该分支当前公开问题。"),
                    GMToolParameter(
                        "private_situation",
                        "object",
                        "新建分支时可选的GM私有局面；恢复既有分支时不会覆盖原框架。",
                        schema_details=self._private_situation_schema_details(),
                    ),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前消息中该角色在另一分支行动的逐字依据。",
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
                name="end_scene",
                description="结束当前非冲突场景并统一清理场景级命刻、效果和待决窗口。",
                handler=self.end_scene,
                parameters=(
                    GMToolParameter("summary", "string", "本场景已经发生的客观结果。", required=True),
                    GMToolParameter("public_reply", "string", "离线模式的可选后备收束文本。"),
                    GMToolParameter(
                        "creative_direction",
                        "string",
                        "可选语义方向；不新增summary中尚未成立的结果。",
                    ),
                    GMToolParameter("evidence", "string", "当前消息中的逐字转场依据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="start_conflict",
                description=(
                    "把当前局面切入冲突，使用已经建档的PC、完整回合盟友NPC和敌人进行"
                    "团队先攻检定并建立双方交替回合。NPC会优先使用场景准备期异步生成的"
                    "核心图鉴继承卡；若尚未准备，开战时会同步继承并经确定性规则编译器校验，"
                    "不会临时捏造通用数值。"
                ),
                handler=self.start_conflict,
                parameters=(
                    GMToolParameter("scene_name", "string", "冲突名称。", required=True),
                    GMToolParameter("pcs", "array", "参战玩家角色名称。", required=True),
                    GMToolParameter(
                        "allied_npcs",
                        "array",
                        "可选；会在玩家方完整执行回合的盟友NPC名称。",
                    ),
                    GMToolParameter("enemies", "array", "参战敌人规则实体名称。", required=True),
                    GMToolParameter(
                        "collective_npcs",
                        "array",
                        (
                            "可选；从allied_npcs或enemies中逐字列出作为单一规则实体处理的"
                            "集体角色，例如‘两名看守’或‘矿工巡逻队’。不要用名称关键词猜测。"
                        ),
                    ),
                    GMToolParameter("leader", "string", "进行团队先攻检定的领队。", required=True),
                    GMToolParameter("objective", "string", "双方诉诸武力的当前目标。", required=True),
                    GMToolParameter(
                        "public_opening",
                        "string",
                        "离线模式的可选后备文本；在线冲突开场由DeepSeek创作作者生成。",
                    ),
                    GMToolParameter(
                        "creative_direction",
                        "string",
                        "可选语义方向；说明冲突为何爆发，不写成品叙述。",
                    ),
                    GMToolParameter("evidence", "string", "当前消息或系统GM节拍中的逐字依据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="run_current_npc_turn",
                description=(
                    "当冲突当前行动者是敌方或完整回合盟友NPC时，由核心GM从"
                    "current_state_summary.runtime.conflict.current_npc_tactical_snapshot.legal_actions"
                    "中直接选择一项并结算。规则层会校验目标、技能、法术、命刻与资源，"
                    "并从权威档案回填属性、伤害和消耗；不会再次调用NPC模型。"
                ),
                handler=self.run_current_npc_turn,
                parameters=(
                    GMToolParameter("expected_actor", "string", "GM预计的当前NPC；用于防止状态漂移。", required=True),
                    GMToolParameter(
                        "npc_action_type",
                        "string",
                        "必须逐字选择合法动作目录中的类型。",
                        required=True,
                        enum=(
                            "Attack",
                            "Spell",
                            "Guard",
                            "Hinder",
                            "Investigate",
                            "Objective",
                            "Skill",
                            "OtherAction",
                            "UltimaRecover",
                            "Escape",
                            "Surrender",
                        ),
                    ),
                    GMToolParameter("target", "string", "攻击、法术、妨碍、调查或技能的合法目标。"),
                    GMToolParameter("targets", "array", "多目标法术的合法目标列表。"),
                    GMToolParameter("guarded_target", "string", "防御时要掩护的合法盟友；只防御自己则留空。"),
                    GMToolParameter("spell_name", "string", "施法时逐字填写合法动作目录中的法术名。"),
                    GMToolParameter("chosen_damage_type", "string", "法术要求时选择的伤害类型。"),
                    GMToolParameter("chosen_status", "string", "法术要求时选择的一种异常状态。"),
                    GMToolParameter("chosen_statuses", "array", "法术要求两种异常状态时提交的列表。"),
                    GMToolParameter("attack_target", "string", "【抢攻】等法术提供顺势攻击时的攻击目标。"),
                    GMToolParameter("attack_id", "string", "NPC有多种基础攻击时，逐字填写合法目录中的攻击ID。"),
                    GMToolParameter("attack_name", "string", "NPC有多种基础攻击时，逐字填写合法目录中的招式名。"),
                    GMToolParameter("skill_name", "string", "使用技能时逐字填写合法动作目录中的技能名。"),
                    GMToolParameter("other_action_name", "string", "使用图鉴其他行动时逐字填写其名称。"),
                    GMToolParameter("mp_amount", "integer", "传递魔力等行动本次消耗的精神值。"),
                    GMToolParameter(
                        "status_effect",
                        "string",
                        "妨碍时选择的状态。",
                        enum=("slow", "dazed", "weakened", "shaken"),
                    ),
                    GMToolParameter("clock_name", "string", "推进或倒转目标时逐字填写当前命刻名。"),
                    GMToolParameter(
                        "target_number",
                        "integer",
                        (
                            "推进目标时由GM根据局面选择难度等级，不要用命刻格数代替。"
                            f"{OPEN_CHECK_DIFFICULTY_GUIDANCE}"
                        ),
                    ),
                    GMToolParameter("reasoning", "string", "只供审计的简短战术理由，不会公开。"),
                    GMToolParameter(
                        "action_description",
                        "string",
                        (
                            "离线模式的可选后备1到2句NPC可见动作描述。"
                        ),
                    ),
                    GMToolParameter(
                        "creative_direction",
                        "string",
                        "可选的动作表现方向；不得预告未结算结果。",
                    ),
                    GMToolParameter("scene_brief", "string", "可选；本轮最相关的公开现场事实。"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="end_conflict",
                description="在冲突结果已经成立时结束冲突；可以保留同一地点继续普通场景，也可以结束整个场景。",
                handler=self.end_conflict,
                parameters=(
                    GMToolParameter("outcome", "string", "已经成立的冲突结果。", required=True),
                    GMToolParameter("continue_scene", "boolean", "是否留在同一地点继续普通场景。", required=True),
                    GMToolParameter(
                        "exit_transitions",
                        "array",
                        (
                            "可选；冲突收束同时让当前发言者控制的角色实际离开时，"
                            "必须在这里提交位置变化，不能只写进outcome或public_reply。"
                            "每项包含destination、participants，可另填scene_name和objective；"
                            "participants只能包含当前场景内、由本次发言者控制且已明确撤离的玩家角色。"
                        ),
                        schema_details={
                            "maxItems": 4,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["destination", "participants"],
                                "properties": {
                                    "destination": {"type": "string", "minLength": 1},
                                    "participants": {
                                        "type": "array",
                                        "minItems": 1,
                                        "items": {"type": "string", "minLength": 1},
                                    },
                                    "scene_name": {"type": "string"},
                                    "objective": {"type": "string"},
                                },
                            },
                        },
                    ),
                    GMToolParameter("public_reply", "string", "离线模式的可选后备冲突收束。"),
                    GMToolParameter(
                        "creative_direction",
                        "string",
                        "可选语义方向；不得改变已经成立的outcome。",
                    ),
                    GMToolParameter("evidence", "string", "当前消息或结算结果中的逐字依据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )

    def state_summary(self, context: GMToolExecutionContext) -> dict[str, object]:
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        scene = app.scene_manager.current_scene
        gate = self.host.session_gates.get(context.campaign_id, context.channel_id, context.session_id)
        conflict_payload: dict[str, object] = {
            "active": bool(app.conflict_manager.state.active),
            "scene_name": app.conflict_manager.state.scene_name,
            "round": int(app.conflict_manager.state.round_number or 0),
            "current_actor": str(
                app.conflict_manager.state.current_actor() or ""
            ),
            "turn_order": list(app.conflict_manager.state.turn_order),
            "player_side": list(app.conflict_manager.state.player_side),
            "enemy_side": list(app.conflict_manager.state.enemy_side),
            "fallen_pcs": dict(app.conflict_manager.state.fallen_pcs),
            "sacrificed_pcs": sorted(app.conflict_manager.state.sacrifices),
            "pc_defeat_consequences": {
                name: list(consequences)
                for name, consequences in app.conflict_manager.state.pc_defeat_consequences.items()
            },
            "defeated_npc_fates": dict(
                app.conflict_manager.state.defeated_npc_fates
            ),
            "resolution_status": (
                app.conflict_manager.resolution_status()
            ),
        }
        current_actor = str(conflict_payload["current_actor"] or "")
        if (
            conflict_payload["active"]
            and current_actor
            and app.character_manager.exists(current_actor)
            and "pc" not in app.character_manager.get(current_actor).traits
            and self._NPC_IDENTITY_TRAITS
            & set(app.character_manager.get(current_actor).traits)
            and getattr(app, "npc_combat_rules", None) is not None
        ):
            # State summaries are observations, not scene turns.  Building the
            # full panel would refresh scene frames, pacing plans and dynamic
            # memory recall before the GM has chosen any tool.  NPC combat
            # legality only needs the current clock board and combat state.
            panel = GamePanel(
                game_phase=app.conflict_manager.format_phase(),
                active_clocks=app.clock_manager.formatted(),
                pc_status=[],
                enemy_status=[],
                recent_chat=str(
                    context.metadata.get("recent_public_context") or ""
                ),
                current_actor=current_actor,
            )
            conflict_payload["current_npc_tactical_snapshot"] = (
                app.npc_combat_rules.build_tactical_snapshot(
                    panel,
                    current_actor,
                )
            )
        return {
            "gate": asdict(gate),
            "scene": (
                {
                    "scene_id": scene.scene_id,
                    "name": scene.name,
                    "scene_type": scene.scene_type.value,
                    "location": scene.location,
                    "participants": list(scene.participants),
                    "objective": scene.objective,
                    "recovered_fallen_pcs": list(scene.recovered_fallen_pcs),
                }
                if scene is not None
                else None
            ),
            "suspended_scenes": [
                {
                    "scene_id": item.scene_id,
                    "name": item.name,
                    "scene_type": item.scene_type.value,
                    "location": item.location,
                    "participants": list(item.participants),
                    "objective": item.objective,
                }
                for item in app.scene_manager.suspended_scenes
            ],
            "conflict": conflict_payload,
        }

    def get_runtime_state(
        self,
        context: GMToolExecutionContext,
        _arguments: dict[str, object],
    ) -> GMToolReceipt:
        return GMToolReceipt(
            tool_name="get_runtime_state",
            ok=True,
            result=self.state_summary(context),
        )

    @classmethod
    def _private_situation_schema_details(cls) -> dict[str, object]:
        return {
            "additionalProperties": False,
            "properties": {
                **{
                    name: {
                        "type": "string",
                        "description": cls._OPENING_SCENE_PREP_SCALARS.get(
                            name,
                            "GM私有场景局面字段。",
                        ),
                    }
                    for name in sorted(cls._FRAME_SCALARS)
                },
                **{
                    name: {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": cls._OPENING_SCENE_PREP_LISTS.get(
                            name,
                            "GM私有场景素材；不是固定流程。",
                        ),
                    }
                    for name in sorted(cls._FRAME_LISTS)
                },
            },
        }

    def start_session(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"), "start_session")
        if evidence_error is not None:
            return evidence_error
        phase = self._clean(arguments.get("phase"))
        runtime = self.host._runtime(context.campaign_id)
        target_session_number = max(
            1,
            int(
                runtime.app.story_arc_manager.state.session_count
                or 0
            )
            + 1,
        )
        prep_cache: dict[str, object] = {}
        if (
            phase == "adventure"
            and str(
                getattr(
                    self.host,
                    "adventure_opening_flow_mode",
                    "legacy",
                )
                or "legacy"
            )
            == "optimized"
        ):
            if target_session_number == 1:
                remaining = max(
                    0.0,
                    float(context.agent_deadline_monotonic or 0.0)
                    - time.monotonic(),
                )
                wait_budget = min(65.0, max(0.0, remaining - 20.0))
                prep_cache = (
                    self.host.adventure_opening_prefetcher
                    .prime_for_consumption(
                        runtime,
                        campaign_id=context.campaign_id,
                        session_id=context.session_id,
                        channel_id=context.channel_id,
                        wait_timeout_seconds=wait_budget,
                    )
                )
            else:
                # Later-session preparation was already generated after the
                # previous authoritative end-session commit.  Foreground
                # start performs only an exact semantic validation and primes
                # the transient concretizer cache; it never waits on or starts
                # a second provider request.
                prep_cache = (
                    self.host.adventure_opening_prefetcher
                    .prime_next_session_for_consumption(
                        runtime,
                        campaign_id=context.campaign_id,
                        wait_timeout_seconds=0.0,
                    )
                )
            if (
                target_session_number == 1
                and prep_cache.get("status") == "miss"
                and dict(prep_cache.get("wait") or {}).get("status")
                == "running"
            ):
                return self._failure(
                    "start_session",
                    "OPENING_PREP_STILL_RUNNING",
                    "第一章的私有场次准备仍在生成，还没有改动会话状态。",
                    "保持第零章；等待当前准备任务完成后重试，不要并发生成第二份。",
                    retryable=False,
                )
        ledger = runtime.app.session_ledger
        ledger_session_id = str(ledger.session_id or "").strip()
        if (
            ledger.active
            and ledger_session_id
            and ledger_session_id != context.session_id
        ):
            return self._failure(
                "start_session",
                "SESSION_LEDGER_ID_MISMATCH",
                (
                    f"战役仍有活动场次账本【{ledger_session_id}】，"
                    f"不能用新的场次标识【{context.session_id}】覆盖它。"
                ),
                "先读取当前会话状态，并继续、暂停后继续，或正常结束原场次。",
            )
        if ledger.active and phase != "adventure":
            return self._failure(
                "start_session",
                "ADVENTURE_LEDGER_STILL_ACTIVE",
                "当前冒险场次尚未收团，不能直接切回开团前或第零章阶段。",
                "先暂停以保留现场，或正常收团后再开启新的第零章讨论。",
            )
        gate = self.host.session_gates.get(context.campaign_id, context.channel_id, context.session_id)
        if gate.status == phase:
            return self._failure(
                "start_session",
                "SESSION_ALREADY_IN_PHASE",
                f"当前会话已经处于{phase}阶段。",
                "不要重复开启；根据当前阶段继续回应玩家。",
            )
        result = self.host._handle_gate_signal(
            {
                "campaign_id": context.campaign_id,
                "session_id": context.session_id,
                "channel_id": context.channel_id,
                "speaker": context.speaker,
                "message": str(context.metadata.get("current_message") or ""),
                # The outer GM transaction owns the only player-facing
                # opening, so state setup cannot trigger a nested model call.
                "defer_session_zero_opening": phase == "session_zero",
                "defer_adventure_opening": phase == "adventure",
            },
            gate=gate,
            signal=SessionGateSignal(
                kind="start",
                status=phase,
                reason=self._clean(arguments.get("reason")),
            ),
        )
        blocked = bool(result.get("blocked"))
        result = dict(result)
        player_input_required = bool(
            blocked
            and self._adventure_blockers_require_player_input(
                result.get("blockers")
            )
        )
        if blocked:
            # ``retryable`` means the GM can correct and resubmit in the same
            # message transaction.  A missing hero choice, safety boundary or
            # level-up cannot be repaired by calling the tool again; it must
            # wait for a later player message.  Exposing that distinction lets
            # the intent tracker keep earlier world-building writes instead of
            # looping until the whole message is rolled back.
            result["player_input_required"] = player_input_required
        current_scene = runtime.app.scene_manager.current_scene
        resuming_adventure = bool(
            phase == "adventure"
            and not blocked
            and current_scene is not None
            and current_scene.scene_type != SceneType.SESSION_ZERO
        )
        if phase == "session_zero" and not blocked:
            result["session_zero_opening_required"] = True
            result["opening_instruction"] = str(
                context.metadata.get("current_message") or ""
            ).strip()
        if phase == "adventure" and not blocked:
            result["adventure_opening_required"] = not resuming_adventure
            result["adventure_resumed"] = resuming_adventure
            if resuming_adventure:
                result["resumed_scene"] = {
                    "scene_id": current_scene.scene_id,
                    "name": current_scene.name,
                    "scene_type": current_scene.scene_type.value,
                    "location": current_scene.location,
                    "participants": list(current_scene.participants),
                    "objective": current_scene.objective,
                }
                current_plan_number = int(
                    runtime.app.story_arc_manager.state
                    .current_pacing_plan.session_number
                    or 0
                )
                if target_session_number > current_plan_number:
                    # Ending a tabletop session intentionally preserves an
                    # unfinished scene.  Resuming that scene in the *next*
                    # tabletop session must still adopt the newly prepared
                    # dramatic contract; only a pause/resume of the same
                    # session skips this refresh.
                    with runtime.transaction_lock:
                        cache_primed = prep_cache.get("status") in {
                            "prefetch_hit",
                            "persistent_hit",
                        }
                        pacing_plan = (
                            runtime.app.campaign_pacing_manager.refresh_plan(
                                conflict_active=False,
                                allow_model_prep=cache_primed,
                                deadline=context.agent_deadline_monotonic,
                                preparation_source=(
                                    "next_prefetch_consume"
                                    if cache_primed
                                    else "foreground"
                                ),
                            )
                        )
                        cache_consumed = bool(
                            cache_primed
                            and runtime.app.campaign_pacing_manager
                            .contract_planner.concretizer.last_cache_hit
                        )
                        if cache_consumed:
                            self.host.adventure_opening_prefetcher.consume_next_session(
                                runtime
                            )
                        saved_path = self.host._autosave_campaign(
                            runtime,
                            context.campaign_id,
                        )
                    result["saved_path"] = saved_path
                    result["session_prep_cache"] = {
                        **prep_cache,
                        "consumed": cache_consumed,
                    }
                    result["session_situation_contract"] = (
                        self._session_situation_contract(
                            pacing_plan.dramatic_contract
                        )
                    )
            else:
                opening_contract = self._adventure_opening_contract(runtime)
                # Prepare the complete, flexible table-session situation before
                # the public opening.  The normal prompt refresh happens before
                # start_session, while the gate is still in Session Zero, so it
                # cannot be relied upon to create the first adventure contract.
                with runtime.transaction_lock:
                    cache_primed = prep_cache.get("status") in {
                        "prefetch_hit",
                        "persistent_hit",
                    }
                    later_session_cache_miss = bool(
                        target_session_number > 1 and not cache_primed
                    )
                    pacing_plan = runtime.app.campaign_pacing_manager.refresh_plan(
                        conflict_active=False,
                        allow_model_prep=not (
                            bool(
                                context.metadata.get(
                                    "_gm_prepared_opening_disables_model_prep"
                                )
                            )
                            or later_session_cache_miss
                        ),
                        deadline=context.agent_deadline_monotonic,
                        preparation_source=(
                            (
                                "prefetch_consume"
                                if target_session_number == 1
                                else "next_prefetch_consume"
                            )
                            if cache_primed
                            else "foreground"
                        ),
                    )
                    cache_consumed = bool(
                        cache_primed
                        and runtime.app.campaign_pacing_manager
                        .contract_planner.concretizer.last_cache_hit
                    )
                    if cache_consumed:
                        if target_session_number == 1:
                            self.host.adventure_opening_prefetcher.consume(
                                runtime
                            )
                        else:
                            self.host.adventure_opening_prefetcher.consume_next_session(
                                runtime
                            )
                    saved_path = self.host._autosave_campaign(
                        runtime,
                        context.campaign_id,
                    )
                result["saved_path"] = saved_path
                result["session_prep_cache"] = {
                    **prep_cache,
                    "consumed": cache_consumed,
                }
                result["opening_contract"] = opening_contract
                result["opening_character_state"] = (
                    self._opening_character_state(runtime, opening_contract)
                )
                planned_restrictions = list(
                    getattr(
                        pacing_plan.dramatic_contract,
                        "opening_equipment_restrictions",
                        [],
                    )
                    or []
                )
                result["opening_equipment_restrictions"] = planned_restrictions
                result["opening_equipment_instruction"] = (
                    "场次契约已决定首场装备限制。start_scene必须把"
                    "opening_equipment_restrictions逐项转换为mode=restrict的"
                    "equipment_access_changes；不得只在叙述里缴械。"
                    if planned_restrictions
                    else (
                        "当前场次契约没有预设装备限制；不要仅凭地点类型擅自缴械。"
                        "若公开开场事实在本事务中确实新增了装备限制，必须依据"
                        "opening_character_state中的准确物品名同步equipment_access_changes。"
                    )
                )
                result["session_situation_contract"] = (
                    self._session_situation_contract(
                        pacing_plan.dramatic_contract
                    )
                )
                # This marker is scoped to the outer message transaction.  A
                # provider failure rolls it back together with start_session;
                # a successful start_scene consumes the required follow-up.
                context.metadata["opening_scene_requires_complete_prep"] = True
                context.metadata["adventure_opening_contract"] = opening_contract
                result["allowed_followup_tools"] = ["start_scene"]
                result["required_followup_tools"] = ["start_scene"]
        return GMToolReceipt(
            tool_name="start_session",
            ok=not blocked,
            result=dict(result),
            error_code="ADVENTURE_START_BLOCKED" if blocked else "",
            message="尚未满足进入冒险的规则条件。" if blocked else "",
            correction_hint="第一章状态保持未开始；根据blockers继续完成第零章或角色创建。" if blocked else "",
            retryable=bool(blocked and not player_input_required),
            state_changed=not blocked,
            public_fallback_reply=(
                str(result.get("reply") or "").strip()
                if blocked or phase not in {"session_zero", "adventure"}
                else ""
            ),
            # A successful adventure gate intentionally grants exactly one
            # typed follow-up: start_scene.  Locking the empty receipt lets the
            # capability policy constrain that continuation without publishing
            # a meta acknowledgement between the gate and the scene opening.
            lock_public_reply=(
                blocked
                or (phase == "adventure" and not resuming_adventure)
            ),
        )

    def start_adventure(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        """Atomically consume Chapter One consent and establish its scene."""

        evidence_error = self._validate_evidence(
            context,
            arguments.get("evidence"),
            "start_adventure",
        )
        if evidence_error is not None:
            return evidence_error
        if str(
            getattr(self.host, "adventure_opening_flow_mode", "legacy")
            or "legacy"
        ) != "optimized":
            return self._failure(
                "start_adventure",
                "OPTIMIZED_OPENING_DISABLED",
                "当前服务使用兼容开章流程。",
                "使用start_session并按成功回执继续start_scene。",
                retryable=False,
            )
        runtime = self.host._runtime(context.campaign_id)
        gate = self.host.session_gates.get(
            context.campaign_id,
            context.channel_id,
            context.session_id,
        )
        readiness = self.host._adventure_readiness_snapshot(
            runtime,
            materialize_confirmed_characters=False,
        )
        transition = (
            runtime.app.session_zero_manager.chapter_one_transition_status(
                ready=bool(readiness.get("ready"))
            )
        )
        if (
            str(getattr(gate, "status", "") or "") != "session_zero"
            or not bool(readiness.get("ready"))
            or str(transition.get("status") or "") != "invited"
        ):
            return self._failure(
                "start_adventure",
                "CHAPTER_ONE_INVITATION_REQUIRED",
                "只有第零章已经完成且GM刚发出开章邀请时，才能使用复合开章。",
                "读取第零章缺项；准备完成后先发出第一章邀请，等待玩家明确同意。",
                retryable=False,
            )

        previous_composite = context.metadata.get(
            "_gm_composite_adventure_start"
        )
        previous_gate_status = context.gate_status
        adventure_committed = False
        context.metadata["_gm_composite_adventure_start"] = True
        try:
            started = self.start_session(
                context,
                {
                    "phase": "adventure",
                    "reason": self._clean(arguments.get("reason")),
                    "evidence": arguments.get("evidence"),
                },
            )
            if not started.ok:
                return GMToolReceipt.failure(
                    "start_adventure",
                    started.error_code or "ADVENTURE_START_FAILED",
                    started.message or "第一章没有成功开启。",
                    started.correction_hint or "保持第零章并检查真实阻塞项。",
                    retryable=started.retryable,
                    result={"start_session": started.result},
                )
            gate_payload = started.result.get("gate")
            context.gate_status = (
                str(gate_payload.get("status") or "adventure")
                if isinstance(gate_payload, dict)
                else "adventure"
            )
            scene_arguments, grounding_contract = (
                self._authoritative_adventure_opening_spec(
                    runtime,
                    started,
                    evidence=arguments.get("evidence"),
                )
            )
            if not scene_arguments:
                return self._failure(
                    "start_adventure",
                    "ADVENTURE_OPENING_SPEC_INVALID",
                    "权威场次准备没有形成可执行的首场参数。",
                    "保持整条事务未提交，修复场次准备后重试。",
                    retryable=False,
                )
            previous_grounding = context.metadata.get(
                "_gm_opening_grounding_contract"
            )
            context.metadata["_gm_opening_grounding_contract"] = (
                grounding_contract
            )
            try:
                scene = self.start_scene(context, scene_arguments)
            finally:
                if previous_grounding is None:
                    context.metadata.pop(
                        "_gm_opening_grounding_contract",
                        None,
                    )
                else:
                    context.metadata[
                        "_gm_opening_grounding_contract"
                    ] = previous_grounding
            if not scene.ok:
                return GMToolReceipt.failure(
                    "start_adventure",
                    scene.error_code or "ADVENTURE_OPENING_SCENE_FAILED",
                    scene.message or "第一章已准备，但首场没有完整建立。",
                    scene.correction_hint or "保持整条事务未提交并修复首场。",
                    retryable=False,
                    result={
                        "start_session": started.result,
                        "start_scene": scene.result,
                    },
                )
            adventure_committed = True
            return GMToolReceipt.success(
                "start_adventure",
                result={
                    "gate": started.result.get("gate"),
                    "scene": scene.result.get("scene"),
                    "creative_author": scene.result.get("creative_author"),
                    "session_prep_cache": started.result.get(
                        "session_prep_cache"
                    ),
                    "required_followup_resolved": True,
                    "required_followup_tools": [],
                },
                state_changed=True,
                public_reply=scene.public_fallback_reply,
                lock_public_reply=True,
                pacing_events=list(scene.pacing_events),
                narrative_events=list(scene.narrative_events),
            )
        finally:
            if not adventure_committed:
                context.gate_status = previous_gate_status
            if previous_composite is None:
                context.metadata.pop("_gm_composite_adventure_start", None)
            else:
                context.metadata[
                    "_gm_composite_adventure_start"
                ] = previous_composite

    def _authoritative_adventure_opening_spec(
        self,
        runtime: Any,
        started: GMToolReceipt,
        *,
        evidence: object,
    ) -> tuple[dict[str, object], dict[str, object]]:
        opening = started.result.get("opening_contract")
        opening = dict(opening) if isinstance(opening, dict) else {}
        contract = self._current_contract(runtime.app)
        if contract is None:
            return {}, {}
        situation_contract = self._session_situation_contract(contract)
        location = str(
            situation_contract.get("location")
            or opening.get("starting_region")
            or ""
        ).strip()
        participants = [
            str(item or "").strip()
            for item in list(opening.get("confirmed_heroes") or [])
            if str(item or "").strip()
        ]
        if not location or not participants:
            return {}, {}
        potential_scenes = list(
            getattr(contract, "potential_scenes", []) or []
        )
        first_scene = potential_scenes[0] if potential_scenes else None
        scene_name = str(
            getattr(first_scene, "title", "")
            or getattr(contract, "title", "")
            or f"{location}的开场"
        ).strip()
        objective = str(
            getattr(first_scene, "purpose", "")
            or getattr(contract, "dramatic_question", "")
            or opening.get("selected_first_act_summary")
            or "应对眼前正在变化的局面。"
        ).strip()
        restrictions = [
            {
                "actor": str(item.get("actor") or "").strip(),
                "mode": "restrict",
                "items": [
                    str(name or "").strip()
                    for name in list(item.get("items") or [])
                    if str(name or "").strip()
                ],
                "reason": str(item.get("reason") or "").strip(),
                "location": str(item.get("location") or location).strip(),
            }
            for item in list(
                started.result.get("opening_equipment_restrictions") or []
            )
            if isinstance(item, dict)
            and str(item.get("actor") or "").strip()
            and list(item.get("items") or [])
        ]
        situation = self._deterministic_opening_situation(
            contract,
            location=location,
            objective=objective,
        )
        disruption = str(
            getattr(contract, "opening_disruption", "")
            or "眼前的局面突然发生变化。"
        ).strip()
        signature = str(getattr(contract, "signature_image", "") or "").strip()
        public_opening = f"{location}。{disruption}"
        if signature and signature not in public_opening:
            public_opening = f"{public_opening}{signature}"
        player_handoff = "局面正在变化——你们现在怎么做？"

        required_public_facts = [location, disruption]
        if signature:
            required_public_facts.append(signature)
        authoritative_public_facts: list[str] = []
        first_act = str(
            opening.get("selected_first_act_summary") or ""
        ).strip()
        if first_act:
            authoritative_public_facts.append(first_act)
        for question, answers in dict(opening.get("setup_answers") or {}).items():
            clean_answers = [
                str(item or "").strip()
                for item in list(answers or [])
                if str(item or "").strip()
            ]
            if clean_answers:
                authoritative_public_facts.append(
                    f"{str(question).strip()}：{'；'.join(clean_answers)}"
                )
        forbidden_private_facts = [
            *list(getattr(contract, "flexible_secrets", []) or []),
            str(getattr(contract, "reversal", "") or ""),
            str(getattr(contract, "stinger", "") or ""),
            *[
                str(getattr(role, "private_secret", "") or "")
                for role in list(getattr(contract, "important_npcs", []) or [])
            ],
        ]
        grounding_contract = {
            "required_public_facts": [
                item for item in required_public_facts if str(item).strip()
            ],
            "authoritative_public_facts": [
                item
                for item in authoritative_public_facts
                if str(item).strip()
            ],
            "forbidden_private_facts": [
                str(item).strip()
                for item in forbidden_private_facts
                if str(item).strip()
            ],
            "source_fingerprint": str(
                getattr(contract, "preparation_fingerprint", "") or ""
            ),
        }
        return (
            {
                "name": scene_name[:120],
                "scene_type": "standard",
                "location": location,
                "participants": participants,
                "equipment_access_changes": restrictions,
                "objective": objective,
                "creative_direction": (
                    "直接实现已确认的第一幕，从一个正在变化的具体现场开始；"
                    "保留玩家自主权，不解释GM后台准备。"
                ),
                "private_situation": situation,
                "public_opening": public_opening,
                "player_handoff": player_handoff,
                "evidence": evidence,
            },
            grounding_contract,
        )

    @classmethod
    def _deterministic_opening_situation(
        cls,
        contract: Any,
        *,
        location: str,
        objective: str,
    ) -> dict[str, object]:
        scenes = list(getattr(contract, "potential_scenes", []) or [])
        first_scene = scenes[0] if scenes else None
        visible = [
            *list(getattr(contract, "fantastic_details", []) or []),
            *list(getattr(first_scene, "required_elements", []) or []),
            *list(getattr(first_scene, "entry_points", []) or []),
        ]
        visible = list(
            dict.fromkeys(str(item).strip() for item in visible if str(item).strip())
        )
        while len(visible) < 2:
            visible.append(
                f"{location}中可立即接触的现场变化{len(visible) + 1}"
            )
        routes = list(getattr(contract, "clue_routes", []) or [])
        clues = list(
            dict.fromkeys(
                str(getattr(route, "visible_lead", "") or "").strip()
                for route in routes
                if str(getattr(route, "visible_lead", "") or "").strip()
            )
        )
        while len(clues) < 2:
            clues.append(f"可从{visible[len(clues)]}继续追查的具体痕迹")
        reveals = list(
            dict.fromkeys(
                str(getattr(route, "success_reveal", "") or "").strip()
                for route in routes
                if str(getattr(route, "success_reveal", "") or "").strip()
            )
        )
        while len(reveals) < 2:
            route = routes[len(reveals)] if len(routes) > len(reveals) else None
            source = str(getattr(route, "source", "") or "").strip()
            approach = str(getattr(route, "approach", "") or "").strip()
            lead = str(getattr(route, "visible_lead", "") or "").strip()
            anchor = source or approach or lead or visible[len(reveals)]
            candidate = f"沿着{anchor}追查，可确认与{objective}有关的具体事实"
            if candidate in reveals:
                candidate = f"从{visible[len(reveals)]}入手，可确认另一条与{objective}有关的事实"
            reveals.append(candidate)
        secrets = [
            str(item).strip()
            for item in list(getattr(contract, "flexible_secrets", []) or [])
            if str(item).strip()
        ] or ["眼前异变的完整原因尚未公开，可随玩家实际调查调整落点。"]
        escalations = [
            str(item).strip()
            for item in list(getattr(contract, "escalation_ladder", []) or [])
            if str(item).strip()
        ]
        while len(escalations) < 2:
            escalations.append(f"现场压力出现第{len(escalations) + 1}种可见升级")
        payoffs = [
            str(item).strip()
            for item in list(getattr(contract, "possible_payoffs", []) or [])
            if str(item).strip()
        ]
        while len(payoffs) < 2:
            payoffs.append(f"玩家选择可造成的局部结果{len(payoffs) + 1}")
        return {
            "premise": str(
                (list(getattr(contract, "situation_facts", []) or []) or [objective])[0]
            ).strip(),
            "stakes": str(getattr(contract, "dilemma", "") or objective).strip(),
            "current_pressure": str(
                getattr(contract, "opening_disruption", "") or objective
            ).strip(),
            "dramatic_question": str(
                getattr(contract, "dramatic_question", "") or objective
            ).strip(),
            "signature_image": str(
                getattr(contract, "signature_image", "") or location
            ).strip(),
            "opposition_goal": str(
                getattr(contract, "opposition_goal", "") or objective
            ).strip(),
            "dilemma": str(getattr(contract, "dilemma", "") or objective).strip(),
            "closure_requirement": str(
                getattr(contract, "closure_requirement", "") or objective
            ).strip(),
            "irreversible_change": str(
                getattr(contract, "irreversible_change", "") or objective
            ).strip(),
            "ending_echo": str(
                getattr(contract, "ending_echo", "") or location
            ).strip(),
            "visible_elements": visible[:12],
            "clue_pool": clues[:12],
            "secrets": secrets[:12],
            "possible_reveals": reveals[:12],
            "escalation_ladder": escalations[:12],
            "possible_payoffs": payoffs[:12],
        }

    @staticmethod
    def _adventure_blockers_require_player_input(blockers: object) -> bool:
        """Return whether an adventure gate can only resume after player input."""

        if not isinstance(blockers, dict):
            return False
        hero_creation = blockers.get("hero_creation")
        if isinstance(hero_creation, dict):
            missing_by_player = hero_creation.get("missing_by_player")
            if isinstance(missing_by_player, dict) and any(
                bool(value) for value in missing_by_player.values()
            ):
                return True
        progression = blockers.get("progression")
        if isinstance(progression, dict) and list(
            progression.get("pending_level_ups") or []
        ):
            return True
        session_zero = blockers.get("session_zero")
        if not isinstance(session_zero, dict):
            return False
        if dict(session_zero.get("contribution_gaps") or {}):
            return True
        player_owned_topics = {"界限与帷幕"}
        missing_topics = {
            str(item or "").strip()
            for item in list(session_zero.get("missing") or [])
            if str(item or "").strip()
        }
        return bool(missing_topics & player_owned_topics)

    @staticmethod
    def _adventure_opening_contract(runtime: Any) -> dict[str, object]:
        """Return player-confirmed Session 0 facts the first scene must honor."""

        manager = runtime.app.session_zero_manager
        world = manager.state.world
        answers = {
            str(question): [str(item) for item in list(values or [])]
            for question, values in dict(
                world.first_act_question_answers or {}
            ).items()
            if str(question).strip() and list(values or [])
        }
        heroes = [
            str(draft.hero_name or key or "").strip()
            for key, draft in dict(world.hero_drafts or {}).items()
            if bool(getattr(draft, "confirmed", False))
            and str(draft.hero_name or key or "").strip()
        ]
        return {
            "selected_first_act_id": str(world.selected_first_act_id or ""),
            "selected_first_act_summary": str(
                world.selected_first_act_summary or ""
            ),
            "starting_region": str(world.starting_region or ""),
            "setup_answers": answers,
            "confirmed_heroes": heroes,
            "instruction": (
                "这些是玩家已经确认的公开事实。首场必须直接实现它们；"
                "只能补充未指定细节，不能替换地点、前提、角色处境或开场事件。"
            ),
        }

    @staticmethod
    def _opening_character_state(
        runtime: Any,
        opening_contract: dict[str, object],
    ) -> list[dict[str, object]]:
        """Expose exact loadout labels needed by the atomic opening scene."""

        result: list[dict[str, object]] = []
        for name in list(opening_contract.get("confirmed_heroes") or []):
            hero_name = str(name or "").strip()
            if not hero_name or not runtime.app.character_manager.exists(hero_name):
                continue
            character = runtime.app.character_manager.get(hero_name)
            result.append(
                {
                    "name": hero_name,
                    "equipment_inventory": list(character.equipment),
                    "equipment_templates": dict(character.equipment_templates),
                    "unavailable_equipment": {
                        item_name: dict(details)
                        for item_name, details in character.unavailable_equipment.items()
                    },
                    "equipped": {
                        "main_hand": character.equipped_main_hand,
                        "off_hand": character.equipped_off_hand,
                        "armor": character.equipped_armor,
                        "shield": character.equipped_shield,
                        "accessory": character.equipped_accessory,
                    },
                }
            )
        return result

    @staticmethod
    def _session_situation_contract(contract: Any) -> dict[str, object]:
        """Expose one private, structured brief to the autonomous GM.

        The receipt is model-facing rather than player-facing.  Keeping the
        packet structured lets the GM reuse or revise prepared material without
        treating it as a fixed plot or leaking it in the public opening.
        """

        return {
            "title": str(getattr(contract, "title", "") or "").strip(),
            "location": str(getattr(contract, "location", "") or "").strip(),
            "dramatic_question": str(
                getattr(contract, "dramatic_question", "") or ""
            ).strip(),
            "opening_disruption": str(
                getattr(contract, "opening_disruption", "") or ""
            ).strip(),
            "signature_image": str(
                getattr(contract, "signature_image", "") or ""
            ).strip(),
            "opposition_goal": str(
                getattr(contract, "opposition_goal", "") or ""
            ).strip(),
            "dilemma": str(getattr(contract, "dilemma", "") or "").strip(),
            "reversal": str(getattr(contract, "reversal", "") or "").strip(),
            "closure_requirement": str(
                getattr(contract, "closure_requirement", "") or ""
            ).strip(),
            "irreversible_change": str(
                getattr(contract, "irreversible_change", "") or ""
            ).strip(),
            "ending_echo": str(
                getattr(contract, "ending_echo", "") or ""
            ).strip(),
            "situation_facts": list(
                getattr(contract, "situation_facts", []) or []
            ),
            "flexible_secrets": list(
                getattr(contract, "flexible_secrets", []) or []
            ),
            "opening_equipment_restrictions": [
                dict(item)
                for item in list(
                    getattr(contract, "opening_equipment_restrictions", []) or []
                )
                if isinstance(item, dict)
            ],
            "clue_routes": [
                asdict(item)
                for item in list(getattr(contract, "clue_routes", []) or [])
            ],
            "important_npcs": [
                asdict(item)
                for item in list(getattr(contract, "important_npcs", []) or [])
            ],
            "potential_scenes": [
                asdict(item)
                for item in list(getattr(contract, "potential_scenes", []) or [])
            ],
            "escalation_ladder": list(
                getattr(contract, "escalation_ladder", []) or []
            ),
            "possible_payoffs": list(
                getattr(contract, "possible_payoffs", []) or []
            ),
            "instruction": (
                "这是可修改的GM私有局面，不是固定剧情。已公开事实不可改；"
                "秘密、线索落点、场景顺序和未使用素材可随玩家行动调整或舍弃。"
            ),
        }

    def pause_session(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"), "pause_session")
        if evidence_error is not None:
            return evidence_error
        gate = self.host.session_gates.get(context.campaign_id, context.channel_id, context.session_id)
        if not gate.active:
            return self._failure("pause_session", "SESSION_NOT_ACTIVE", "当前没有正在进行的跑团会话。", "会话状态保持未开始。")
        runtime = self.host._runtime(context.campaign_id)
        with runtime.transaction_lock:
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
            updated = self.host.session_gates.pause(
                context.campaign_id,
                context.channel_id,
                context.session_id,
                reason=self._clean(arguments.get("reason")),
            )
        return GMToolReceipt(
            tool_name="pause_session",
            ok=True,
            result={"gate": asdict(updated), "saved_path": saved_path},
            state_changed=True,
            public_fallback_reply="先停在这里，当前进度已经保存。",
        )

    def end_session(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"), "end_session")
        if evidence_error is not None:
            return evidence_error
        runtime = self.host._runtime(context.campaign_id)
        blocking = [
            window
            for window in runtime.app.interceptor.decision_window_manager.pending()
            if window.blocking
        ]
        if blocking:
            return self._failure(
                "end_session",
                "BLOCKING_DECISION_PENDING",
                "仍有必须由玩家决定的规则窗口，不能替玩家跳过后收团。",
                "先让对应玩家处理归零或其他阻塞选择。",
                result={"pending_windows": [window.window_id for window in blocking]},
            )
        requested_reply = self._clean(arguments.get("public_reply"))
        closing_image = self._clean(arguments.get("closing_image"))
        creative_metadata: dict[str, object] = {}
        creative_writer = getattr(runtime.app, "scene_creative_writer", None)
        if creative_writer is not None and creative_writer.available:
            scene = runtime.app.scene_manager.current_scene
            progress = runtime.app.story_arc_manager.state.current_session_progress
            try:
                composition = creative_writer.compose_public_scene_text(
                    operation="session_closure",
                    facts={
                        "title": self._clean(arguments.get("title")),
                        "scene_name": str(getattr(scene, "name", "") or ""),
                        "location": str(getattr(scene, "location", "") or ""),
                        "participants": list(getattr(scene, "participants", []) or []),
                        "scene_summary": str(getattr(scene, "summary", "") or ""),
                        "opening_image": str(
                            getattr(progress, "memory_image", "") or ""
                        ),
                        "last_event": str(getattr(progress, "last_event", "") or ""),
                        "memory_consequence": str(
                            getattr(progress, "memory_consequence", "") or ""
                        ),
                        "deliberate_cliffhanger": bool(
                            arguments.get("deliberate_cliffhanger")
                        ),
                        "creative_direction": self._clean(
                            arguments.get("creative_direction")
                        ),
                    },
                    recent_public_messages=self._recent_public_messages(context),
                    fallback_public_reply=requested_reply,
                    require_closing_image=context.gate_status == "adventure",
                    deadline=context.agent_deadline_monotonic,
                )
            except SceneCreativeWriterError as exc:
                return self._failure(
                    "end_session",
                    "SCENE_CREATIVE_AUTHOR_FAILED",
                    f"DeepSeek场景作者未能完成本场结尾：{exc}",
                    "不要由核心GM补写成品；场次保持活动状态，稍后重试。",
                )
            requested_reply = composition.public_reply
            closing_image = composition.closing_image
            creative_metadata = {
                "author": "scene_creative_writer",
                "model": composition.model,
                "used_model": composition.used_model,
            }
        if context.gate_status == "adventure":
            if not closing_image:
                return self._failure(
                    "end_session",
                    "CLOSING_IMAGE_REQUIRED",
                    "冒险场次收团前还缺少基于当前局面的结尾画面。",
                    "用一至两句写出当前确实可见的画面，并让开场标志意象因本场选择发生变化。",
                )
            compact_reply = "".join(requested_reply.split())
            compact_image = "".join(closing_image.split())
            if compact_image not in compact_reply:
                return self._failure(
                    "end_session",
                    "CLOSING_IMAGE_NOT_IN_PUBLIC_REPLY",
                    "结尾画面没有完整出现在面向玩家的收团话语中。",
                    "把closing_image逐字放进public_reply，再如实说明保存与下次承接点。",
                )
            opening_image = self._clean(
                runtime.app.story_arc_manager.state.current_session_progress.memory_image
            )
            if opening_image and compact_image == "".join(opening_image.split()):
                return self._failure(
                    "end_session",
                    "CLOSING_IMAGE_NOT_EVOLVED",
                    "结尾画面只是原样复述开场意象，没有呈现本场选择留下的变化。",
                    "保持同一意象锚点，但写出人物去向、损伤、取得物或局面造成的可见改变。",
                )
        with runtime.transaction_lock:
            defer_summary_enrichment = bool(
                context.metadata.get("_gm_message_transaction_id")
            )
            result = self.host._end_session(
                {
                    "campaign_id": context.campaign_id,
                    "session_id": context.session_id,
                    "channel_id": context.channel_id,
                    "title": self._clean(arguments.get("title")),
                    "closing_image": closing_image,
                    "deliberate_cliffhanger": bool(
                        arguments.get("deliberate_cliffhanger")
                    ),
                    "_defer_summary_enrichment_until_commit": (
                        defer_summary_enrichment
                    ),
                }
            )
        if not bool(result.get("ok", True)):
            return self._failure(
                "end_session",
                str(result.get("error_code") or "END_SESSION_FAILED"),
                str(result.get("error") or "本场未能完成收团结算。"),
                "先处理返回的待决窗口或恢复有效会话，再重新收团。",
                result=dict(result),
            )
        result = dict(result)
        if (
            defer_summary_enrichment
            and not bool(result.get("already_ended"))
        ):
            context.defer_post_commit(
                "end_session_summary_enrichment",
                lambda: result.__setitem__(
                    "summary_enrichment",
                    self.host._schedule_end_session_summary_enrichment(
                        runtime,
                        result,
                    ),
                ),
            )
        if requested_reply:
            result["requested_public_reply"] = requested_reply
        if closing_image:
            result["closing_image"] = closing_image
        result["creative_author"] = creative_metadata
        result["public_reply_grounding_instruction"] = (
            "收团表达必须服从final_state_snapshot与closure_ready；"
            "不得把仍在不同地点、仍未完成的撤离或目标写成已经成功；"
            "必须保留已审核的closing_image。"
        )
        return GMToolReceipt(
            tool_name="end_session",
            ok=True,
            result=result,
            state_changed=True,
            public_fallback_reply=self._end_session_fallback(result),
            lock_public_reply=False,
        )

    @classmethod
    def _end_session_fallback(cls, result: dict[str, object]) -> str:
        if bool(result.get("closure_ready")):
            return "今天先到这里，本场已经结算并保存。"
        snapshot = result.get("final_state_snapshot")
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        scene = snapshot.get("scene")
        scene = scene if isinstance(scene, dict) else {}
        scene_name = cls._clean(scene.get("name") or scene.get("location"))
        if scene_name:
            return f"今天先停在【{scene_name}】，本场已经结算并保存；下次从这里继续。"
        locations = list(
            dict.fromkeys(
                cls._clean(item.get("location"))
                for item in list(snapshot.get("player_characters") or [])
                if isinstance(item, dict) and cls._clean(item.get("location"))
            )
        )
        if locations:
            return "今天先停在这里，本场已经结算并保存；角色当前位置是" + "、".join(
                f"【{location}】" for location in locations
            ) + "，下次从当前局面继续。"
        return "今天先停在这里，本场已经结算并保存；当前局面尚未收束，下次接着处理。"

    def start_scene(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"), "start_scene")
        if evidence_error is not None:
            return evidence_error
        gate_error = self._require_adventure(context, "start_scene")
        if gate_error is not None:
            return gate_error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        if (
            bool(context.metadata.get("opening_scene_requires_complete_prep"))
            and not context.metadata.get("adventure_opening_contract")
        ):
            # 正常开团由 start_session 在同一事务中准备场次契约。若阶段由
            # 管理接口或旧存档先行切换，则在首场工具真正执行前补做同一份
            # 私密准备，保证两条入口得到一致的开场质量与规则约束。
            with runtime.transaction_lock:
                app.campaign_pacing_manager.refresh_plan(conflict_active=False)
        if app.conflict_manager.state.active:
            return self._failure(
                "start_scene",
                "CONFLICT_ACTIVE",
                "当前冲突仍在进行，不能直接建立普通场景。",
                "先调用end_conflict提交已经成立的冲突结果；不得用转场跳过回合、归零选择或敌人结局。",
            )
        lifecycle_error = self._active_scene_lifecycle_error(
            app,
            "start_scene",
        )
        if lifecycle_error is not None:
            return lifecycle_error
        blocking_error = self._blocking_window_error(app, "start_scene")
        if blocking_error is not None:
            return blocking_error
        participants, participants_error = self._string_list(
            arguments.get("participants"),
            tool_name="start_scene",
            field_name="participants",
            require_nonempty=True,
        )
        if participants_error is not None:
            return participants_error
        equipment_access_changes, equipment_error = (
            self._validate_equipment_access_changes(
                app,
                arguments.get("equipment_access_changes"),
                tool_name="start_scene",
                participants=participants,
            )
        )
        if equipment_error is not None:
            return equipment_error
        if bool(context.metadata.get("opening_scene_requires_complete_prep")):
            equipment_gaps = self._opening_equipment_restriction_gaps(
                self._current_contract(app),
                equipment_access_changes,
            )
            if equipment_gaps:
                required = [
                    dict(item)
                    for item in list(
                        getattr(
                            self._current_contract(app),
                            "opening_equipment_restrictions",
                            [],
                        )
                        or []
                    )
                    if isinstance(item, dict)
                ]
                return self._failure(
                    "start_scene",
                    "OPENING_EQUIPMENT_PLAN_NOT_APPLIED",
                    "第一场没有完整落实场次契约中的装备限制：" + "；".join(equipment_gaps),
                    (
                        "逐字使用result.required_restrictions中的actor与items，"
                        "以mode=restrict重新调用start_scene；不要只在公开叙述中声称装备被收缴。"
                    ),
                    result={"required_restrictions": required},
                )
        creative_writer = getattr(app, "scene_creative_writer", None)
        creative_metadata: dict[str, object] = {}
        opening_situation_fallback = (
            dict(arguments.get("private_situation") or {})
            if isinstance(arguments.get("private_situation"), dict)
            else {}
        )
        prepared_composition = context.metadata.get(
            "_gm_prepared_scene_composition"
        )
        opening_grounding_contract = context.metadata.get(
            "_gm_opening_grounding_contract"
        )
        if context.metadata.get("_gm_composite_adventure_start") and not (
            isinstance(opening_grounding_contract, dict)
            and list(opening_grounding_contract.get("required_public_facts") or [])
        ):
            return self._failure(
                "start_scene",
                "OPENING_GROUNDING_CONTRACT_REQUIRED",
                "复合开章缺少由权威共识生成的公开事实审校合同。",
                "保持场景未开始状态，重新构造完整的开场审校合同后重试。",
            )
        if (
            isinstance(prepared_composition, dict)
            and prepared_composition.get("source_tool")
            == "prepare_solo_adventure"
        ):
            # 该包只能由受信复合工具写入上下文，不属于模型可见的
            # start_scene参数；复用文本前仍执行与普通创作开场相同的本地校验。
            if creative_writer is None:
                return self._failure(
                    "start_scene",
                    "PREPARED_SCENE_VALIDATOR_UNAVAILABLE",
                    "首场开场包缺少本地校验器。",
                    "保持场景未开始状态，恢复校验器后重试。",
                )
            try:
                composition = creative_writer.validate_prepared_scene_opening(
                    {
                        "private_situation": prepared_composition.get(
                            "private_situation"
                        ),
                        "public_opening": prepared_composition.get(
                            "public_opening"
                        ),
                        "player_handoff": prepared_composition.get(
                            "player_handoff"
                        ),
                    }
                )
            except SceneCreativeWriterError as exc:
                return self._failure(
                    "start_scene",
                    "PREPARED_SCENE_COMPOSITION_INVALID",
                    f"预先生成的首场开场没有通过本地校验：{exc}",
                    "保持场景未开始状态；重新生成完整开场包后重试。",
                )
            arguments = {
                **arguments,
                "private_situation": composition.private_situation,
                "public_opening": composition.public_opening,
                "player_handoff": composition.player_handoff,
            }
            creative_metadata = {
                "author": "solo_session_zero_completer",
                "model": self._clean(prepared_composition.get("model")),
                "used_model": bool(prepared_composition.get("used_model")),
                "reused_prepared_packet": True,
            }
        elif creative_writer is not None and creative_writer.available:
            effective_opening_contract = (
                dict(
                    context.metadata.get("adventure_opening_contract")
                    or self._adventure_opening_contract(runtime)
                )
                if context.metadata.get("opening_scene_requires_complete_prep")
                else {}
            )
            if isinstance(opening_grounding_contract, dict):
                effective_opening_contract.update(
                    dict(opening_grounding_contract)
                )
            try:
                composition = creative_writer.compose_scene_opening(
                    scene_request={
                        "name": self._clean(arguments.get("name")),
                        "scene_type": self._clean(arguments.get("scene_type")),
                        "location": self._clean(arguments.get("location")),
                        "participants": list(participants),
                        "objective": self._clean(arguments.get("objective")),
                        "creative_direction": self._clean(
                            arguments.get("creative_direction")
                        ),
                    },
                    session_contract=self._session_situation_contract(
                        self._current_contract(app)
                    ),
                    opening_contract=effective_opening_contract,
                    current_message=str(
                        context.metadata.get("current_message") or ""
                    ),
                    recent_public_messages=self._recent_public_messages(context),
                    fallback_private_situation=(
                        arguments.get("private_situation")
                        if isinstance(arguments.get("private_situation"), dict)
                        else {}
                    ),
                    fallback_public_opening=self._clean_multiline(
                        arguments.get("public_opening")
                    ),
                    fallback_player_handoff=self._clean_multiline(
                        arguments.get("player_handoff")
                    ),
                    deadline=context.agent_deadline_monotonic,
                )
            except SceneCreativeWriterError as exc:
                return self._failure(
                    "start_scene",
                    "SCENE_CREATIVE_AUTHOR_FAILED",
                    f"DeepSeek场景作者未能完成暗线与开场：{exc}",
                    "不要由核心GM补写成品；保留场景未开始状态，稍后重试同一工具。",
                )
            arguments = {
                **arguments,
                "private_situation": (
                    self._merge_opening_situation_fallback(
                        composition.private_situation,
                        opening_situation_fallback,
                    )
                    if bool(
                        context.metadata.get(
                            "opening_scene_requires_complete_prep"
                        )
                    )
                    else composition.private_situation
                ),
                "public_opening": composition.public_opening,
                "player_handoff": composition.player_handoff,
            }
            creative_metadata = {
                "author": "scene_creative_writer",
                "model": composition.model,
                "used_model": composition.used_model,
            }
        situation, situation_error = self._validate_private_situation(arguments.get("private_situation"))
        if situation_error is not None:
            return situation_error
        if bool(context.metadata.get("opening_scene_requires_complete_prep")):
            prep_gaps = self._opening_scene_prep_gaps(
                situation,
                self._current_contract(app),
            )
            if prep_gaps:
                return self._failure(
                    "start_scene",
                    "OPENING_SCENE_PREP_INCOMPLETE",
                    "第一场的GM私有局面还不足以支撑完整的一场游戏："
                    + "；".join(prep_gaps),
                    (
                        "补齐缺项后重新调用start_scene。准备的是可换序、可修改、可舍弃的局面素材，"
                        "不是固定剧情；不得改动opening_contract中的玩家共识，也不要把私有素材写进公开开场。"
                    ),
                )
        public_opening = self._clean_multiline(arguments.get("public_opening"))
        if not public_opening:
            return self._failure(
                "start_scene",
                "PUBLIC_OPENING_REQUIRED",
                "场景开场不能为空。",
                "先呈现地点、当下压力和玩家能立即接触的具体现场事物。",
            )
        player_handoff = self._clean_multiline(arguments.get("player_handoff"))
        if not player_handoff:
            return self._failure(
                "start_scene",
                "PLAYER_HANDOFF_REQUIRED",
                "场景开场缺少玩家行动窗口。",
                (
                    "补充一句立足眼前局面的开放问题，把镜头交给实际在场玩家；"
                    "不要列动作菜单，也不要替角色行动。"
                ),
            )
        public_reply = "\n".join((public_opening, player_handoff))
        if (
            context.metadata.get("_gm_composite_adventure_start")
            and isinstance(opening_grounding_contract, dict)
            and (creative_writer is None or not creative_writer.available)
        ):
            missing = [
                str(item).strip()
                for item in list(
                    opening_grounding_contract.get("required_public_facts")
                    or []
                )
                if str(item).strip()
                and str(item).strip() not in public_reply
            ]
            leaked = [
                str(item).strip()
                for item in list(
                    opening_grounding_contract.get("forbidden_private_facts")
                    or []
                )
                if str(item).strip()
                and str(item).strip() in public_reply
            ]
            if missing or leaked:
                return self._failure(
                    "start_scene",
                    "OPENING_GROUNDING_FAILED",
                    "复合开章的本地公开事实校验未通过。",
                    "保持整条事务未提交；修复权威开场构造后再重试。",
                )
        leak = self._private_leak(public_reply, situation)
        if leak:
            return self._failure(
                "start_scene",
                "PRIVATE_SCENE_INFORMATION_LEAK",
                f"公开开场泄露了GM私有字段【{leak}】。",
                "从公开开场移除未揭示暗线；可观察事实应放入visible_elements而非secrets。",
            )
        try:
            scene_type = SceneType(self._clean(arguments.get("scene_type")))
        except ValueError:
            return self._failure("start_scene", "INVALID_SCENE_TYPE", "场景类型无效。", "使用工具schema中的非冲突场景类型。")
        managed_type_error = self._generic_scene_type_error(
            scene_type,
            "start_scene",
        )
        if managed_type_error is not None:
            return managed_type_error
        opening_anchor = context.metadata.get("opening_scene_anchor")
        if (
            isinstance(opening_anchor, dict)
            and context.metadata.get("system_gm_beat_request") is True
            and str(context.metadata.get("heartbeat_action") or "").strip()
            == "scene_opening"
            and opening_anchor.get("preserve_until_movement") is True
        ):
            anchored_location = self._clean(opening_anchor.get("location"))
            requested_location = self._clean(arguments.get("location"))
            anchored_participants = {
                self._clean(item)
                for item in list(opening_anchor.get("participants") or [])
                if self._clean(item)
            }
            requested_participants = {
                self._clean(item)
                for item in participants
                if self._clean(item)
            }
            if anchored_location and requested_location != anchored_location:
                return self._failure(
                    "start_scene",
                    "OPENING_LOCATION_ANCHOR_MISMATCH",
                    (
                        f"上一场结束时仍位于【{anchored_location}】，"
                        f"本次开场不能直接改到【{requested_location or '未指定'}】。"
                    ),
                    (
                        "保持location与opening_scene_anchor.location完全一致；"
                        "只有已经由移动或转场工具提交的新位置才能改变该锚点。"
                    ),
                )
            if anchored_participants and requested_participants != anchored_participants:
                missing = sorted(anchored_participants - requested_participants)
                added = sorted(requested_participants - anchored_participants)
                details = []
                if missing:
                    details.append("遗漏：" + "、".join(missing))
                if added:
                    details.append("无依据新增：" + "、".join(added))
                return self._failure(
                    "start_scene",
                    "OPENING_PARTICIPANT_ANCHOR_MISMATCH",
                    "续场人物与上一场结束时不一致（" + "；".join(details) + "）。",
                    (
                        "逐字使用opening_scene_anchor.participants；私有场景候选"
                        "不能把尚未登场的人物直接写进续场。"
                    ),
                )
        current_scene = app.scene_manager.current_scene
        if (
            current_scene is not None
            and current_scene.scene_type != SceneType.SESSION_ZERO
        ):
            return self._failure(
                "start_scene",
                "SCENE_ALREADY_ACTIVE",
                f"当前场景【{current_scene.name}】仍在进行，不能被新场景静默覆盖。",
                (
                    "人物实际抵达新地点时使用transition_scene；"
                    "只结束当前场景则先调用end_scene。"
                ),
            )
        snapshot = self._snapshot(app, context.campaign_id)
        try:
            with runtime.transaction_lock:
                scene = app.start_scene(
                    self._clean(arguments.get("name")),
                    scene_type,
                    location=self._clean(arguments.get("location")),
                    participants=participants,
                    objective=self._clean(arguments.get("objective")),
                )
                frame = app.scene_frame_manager.ensure_frame(
                    scene=scene,
                    recent_chat=public_reply,
                    world_state=app.world_state,
                    character_manager=app.character_manager,
                    contract=self._current_contract(app),
                )
                for key, value in situation.items():
                    if isinstance(value, list):
                        if value:
                            setattr(frame, key, value)
                        continue
                    if str(value or "").strip():
                        setattr(frame, key, value)
                committed_equipment_changes = []
                for change in equipment_access_changes:
                    committed_equipment_changes.append(
                        app.interceptor.economy_manager.set_equipment_access(
                            str(change["actor"]),
                            list(change["items"]),
                            available=change["mode"] == "restore",
                            reason=str(change.get("reason") or ""),
                            location=str(change.get("location") or ""),
                            restore_loadout=bool(
                                change.get("restore_loadout", False)
                            ),
                            allow_restore_loadout=True,
                        )
                    )
                app.scene_frame_manager._touch(frame)
                saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        except Exception as exc:
            self._restore(app, snapshot)
            return self._failure("start_scene", "SCENE_START_FAILED", str(exc), "当前场景保持原状；修正场景参数后重新建立。")
        opening_role = str(
            getattr(frame, "session_opportunity_role", "")
            or getattr(scene, "session_opportunity_role", "")
            or ""
        ).strip()
        is_strong_start = opening_role == "strong_start"
        contract_signature = self._clean(
            getattr(self._current_contract(app), "signature_image", "")
        )
        required_opening_facts = (
            list(opening_grounding_contract.get("required_public_facts") or [])
            if isinstance(opening_grounding_contract, dict)
            else []
        )
        # Composite adventure openings already passed semantic grounding against
        # every required public fact.  That successful receipt is authoritative
        # evidence that the signature image reached the table; no second report
        # model needs to guess from paraphrased prose later.
        opening_signature_realized = (
            contract_signature
            if contract_signature
            and (
                contract_signature in required_opening_facts
                or contract_signature in public_opening
            )
            else ""
        )
        return GMToolReceipt(
            tool_name="start_scene",
            ok=True,
            result={
                "scene": {
                    "scene_id": scene.scene_id,
                    "name": scene.name,
                    "scene_type": scene.scene_type.value,
                    "location": scene.location,
                    "participants": list(scene.participants),
                    "recovered_fallen_pcs": list(scene.recovered_fallen_pcs),
                },
                "equipment_access_changes": committed_equipment_changes,
                "saved_path": saved_path,
                "creative_author": creative_metadata,
            },
            state_changed=True,
            public_fallback_reply=public_reply,
            lock_public_reply=True,
            pacing_events=[
                GMToolPacingEvent(
                    public_image=self._first_sentence(public_opening),
                    opening_signature_realized=opening_signature_realized,
                    awaits_player_response=True,
                    consequence=(
                        self._first_sentence(public_opening)
                        if is_strong_start
                        else ""
                    ),
                    gm_beat_purpose=(
                        str(
                            context.metadata.get("heartbeat_beat_purpose")
                            or context.metadata.get("heartbeat_action")
                            or ""
                        ).strip()
                        if context.metadata.get("system_gm_beat_request")
                        else ("strong_start" if is_strong_start else "")
                    ),
                )
            ],
        )

    def transition_scene(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(
            context,
            arguments.get("evidence"),
            "transition_scene",
        )
        if evidence_error is not None:
            return evidence_error
        gate_error = self._require_adventure(context, "transition_scene")
        if gate_error is not None:
            return gate_error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        current = app.scene_manager.current_scene
        if current is None:
            return self._failure(
                "transition_scene",
                "NO_ACTIVE_SCENE",
                "当前没有可供转出的场景。",
                "首次建立场景请使用start_scene。",
            )
        if app.conflict_manager.state.active or current.scene_type == SceneType.CONFLICT:
            return self._failure(
                "transition_scene",
                "CONFLICT_ACTIVE",
                "冲突仍在进行，不能用普通转场跳过回合或结果。",
                "先使用end_conflict结算冲突，再进行场景转场。",
            )
        lifecycle_error = self._active_scene_lifecycle_error(
            app,
            "transition_scene",
        )
        if lifecycle_error is not None:
            return lifecycle_error
        blocking_error = self._blocking_window_error(app, "transition_scene")
        if blocking_error is not None:
            return blocking_error

        movers, movers_error = self._string_list(
            arguments.get("movers"),
            tool_name="transition_scene",
            field_name="movers",
            require_nonempty=True,
        )
        if movers_error is not None:
            return movers_error
        companions, companions_error = self._string_list(
            arguments.get("npc_companions") or [],
            tool_name="transition_scene",
            field_name="npc_companions",
            require_nonempty=False,
        )
        if companions_error is not None:
            return companions_error
        destination_npcs, destination_error = self._string_list(
            arguments.get("destination_npcs") or [],
            tool_name="transition_scene",
            field_name="destination_npcs",
            require_nonempty=False,
        )
        if destination_error is not None:
            return destination_error

        known_pcs = {
            character.name
            for character in app.character_manager.all()
            if "pc" in character.traits
        }
        non_pc_movers = [name for name in movers if name not in known_pcs]
        if non_pc_movers:
            return self._failure(
                "transition_scene",
                "MOVER_MUST_BE_PLAYER_CHARACTER",
                "movers只能包含玩家角色：" + "、".join(non_pc_movers),
                "随行NPC放入npc_companions；目的地NPC放入destination_npcs。",
            )
        absent_movers = [name for name in movers if name not in current.participants]
        if absent_movers:
            return self._failure(
                "transition_scene",
                "MOVER_NOT_IN_FOCUSED_SCENE",
                "以下角色不在当前聚焦场景，不能从这里转场：" + "、".join(absent_movers),
                "先使用focus_scene_branch切回角色真实所在镜头，再结算移动。",
            )
        if not context.metadata.get("system_gm_beat_request"):
            controls = self.host._player_character_control_map(runtime)
            known_ownership = any(controls.values())
            controlled = set(controls.get(context.speaker, []))
            unauthorized = [
                name for name in movers if known_ownership and name not in controlled
            ]
            if unauthorized:
                consent_error = self._validate_mover_consents(
                    context=context,
                    controls=controls,
                    unauthorized=unauthorized,
                    value=arguments.get("mover_consents"),
                )
                if consent_error is not None:
                    return consent_error
        resolved_companions: list[str] = []
        absent_companions: list[str] = []
        current_participants = set(current.participants)
        for name in companions:
            canonical = app.world_state.resolve_npc_name(name) or name
            if canonical not in current_participants and name not in current_participants:
                absent_companions.append(name)
                continue
            if canonical not in resolved_companions:
                resolved_companions.append(canonical)
        if absent_companions:
            return self._failure(
                "transition_scene",
                "NPC_COMPANION_NOT_PRESENT",
                "以下NPC不在当前场景，不能直接声明随行：" + "、".join(absent_companions),
                "只保留当前在场且已经答应随行的NPC；目的地人物使用destination_npcs。",
            )
        companions = resolved_companions
        resolved_destination_npcs: list[str] = []
        for requested in destination_npcs:
            canonical = app.world_state.resolve_npc_name(requested)
            if not canonical:
                return self._failure(
                    "transition_scene",
                    "UNKNOWN_DESTINATION_NPC",
                    f"没有找到目的地NPC【{requested}】的档案。",
                    "先在其实际登场时建立NPC档案；不要用目的地参数让未知人物凭空出现。",
                )
            authoritative_location = (
                app.scene_manager.location_of(canonical)
                or str(
                    getattr(
                        app.world_state.npc_personas.get(canonical),
                        "current_location",
                        "",
                    )
                    or ""
                ).strip()
            )
            if not app.scene_manager._same_exact_location(
                authoritative_location,
                self._clean(arguments.get("location")),
            ):
                return self._failure(
                    "transition_scene",
                    "DESTINATION_NPC_NOT_PRESENT",
                    f"【{canonical}】的权威位置不是本次目的地。",
                    "只提交此前已确定在目的地的人物，或先结算其移动。",
                )
            if canonical not in resolved_destination_npcs:
                resolved_destination_npcs.append(canonical)
        destination_npcs = resolved_destination_npcs

        name = self._clean(arguments.get("name"))
        location = self._clean(arguments.get("location"))
        transition_summary = self._clean(arguments.get("transition_summary"))
        if not name or not location or not transition_summary:
            return self._failure(
                "transition_scene",
                "TRANSITION_FIELDS_REQUIRED",
                "场景名称、地点和旧场景收束都不能为空。",
                "补充实际目的地与已经发生的离场事实，不预设抵达后的结果。",
            )
        try:
            scene_type = SceneType(self._clean(arguments.get("scene_type")))
        except ValueError:
            return self._failure(
                "transition_scene",
                "INVALID_SCENE_TYPE",
                "场景类型无效。",
                "使用工具schema中的非冲突场景类型。",
            )
        managed_type_error = self._generic_scene_type_error(
            scene_type,
            "transition_scene",
        )
        if managed_type_error is not None:
            return managed_type_error

        participants = list(dict.fromkeys([*movers, *companions, *destination_npcs]))
        creative_metadata: dict[str, object] = {}
        creative_writer = getattr(app, "scene_creative_writer", None)
        if creative_writer is not None and creative_writer.available:
            try:
                composition = creative_writer.compose_transition(
                    transition_request={
                        "name": name,
                        "scene_type": scene_type.value,
                        "location": location,
                        "participants": list(participants),
                        "objective": self._clean(arguments.get("objective")),
                        "transition_summary": transition_summary,
                        "creative_direction": self._clean(
                            arguments.get("creative_direction")
                        ),
                    },
                    session_contract=self._session_situation_contract(
                        self._current_contract(app)
                    ),
                    current_scene={
                        "name": current.name,
                        "location": current.location,
                        "participants": list(current.participants),
                        "objective": current.objective,
                    },
                    recent_public_messages=self._recent_public_messages(context),
                    fallback_private_situation=(
                        arguments.get("private_situation")
                        if isinstance(arguments.get("private_situation"), dict)
                        else {}
                    ),
                    fallback_public_arrival=self._clean_multiline(
                        arguments.get("public_arrival")
                    ),
                    deadline=context.agent_deadline_monotonic,
                )
            except SceneCreativeWriterError as exc:
                return self._failure(
                    "transition_scene",
                    "SCENE_CREATIVE_AUTHOR_FAILED",
                    f"DeepSeek场景作者未能完成新局面与抵达描述：{exc}",
                    "不要由核心GM补写成品；角色位置保持不变，稍后重试同一工具。",
                )
            arguments = {
                **arguments,
                "private_situation": composition.private_situation,
                "public_arrival": composition.public_arrival,
            }
            creative_metadata = {
                "author": "scene_creative_writer",
                "model": composition.model,
                "used_model": composition.used_model,
            }
        situation, situation_error = self._validate_private_situation(
            arguments.get("private_situation"),
            tool_name="transition_scene",
        )
        if situation_error is not None:
            return situation_error
        public_arrival = self._clean_multiline(arguments.get("public_arrival"))
        if not public_arrival:
            return self._failure(
                "transition_scene",
                "PUBLIC_ARRIVAL_REQUIRED",
                "抵达描述不能为空。",
                "只描述移动完成后立即可观察的现场，不提前完成后续行动。",
            )
        leak = self._private_leak(public_arrival, situation)
        if leak:
            return self._failure(
                "transition_scene",
                "PRIVATE_SCENE_INFORMATION_LEAK",
                f"抵达描述泄露了GM私有字段【{leak}】。",
                "从公开描述移除未揭示暗线；可观察事实应放入visible_elements。",
            )
        unexpected_actors = unexpected_actor_mentions(
            app,
            public_arrival,
            allowed_names=participants,
        )
        if unexpected_actors:
            return self._failure(
                "transition_scene",
                "PUBLIC_ARRIVAL_ACTOR_NOT_PRESENT",
                "抵达描述把未在目的地的人物写成了在场者：" + "、".join(unexpected_actors),
                (
                    "public_arrival只描述目的地实际在场者；留在原场景的人物写入transition_summary。"
                    "不要在public_arrival中用‘某人不在这里’等否定句提及未抵达者。"
                    "若人物确实在目的地，先用正确的移动或destination_npcs提交其权威位置。"
                ),
            )
        # ``end_scene`` archives and clears the focused frame. Keep the
        # authoritative situation alive long enough to decide whether the
        # destination is another room of the same physical place.
        previous_frame = app.scene_frame_manager.current_frame
        snapshot = self._snapshot(app, context.campaign_id)
        action_round: dict[str, object] = {}
        action_round_events: list[dict[str, object]] = []
        public_reply = public_arrival
        try:
            with runtime.transaction_lock:
                source_scene = current
                scene, movement_mode = app.scene_manager.move_participants_to_location(
                    participants,
                    location,
                    scene_name=name,
                    objective=self._clean(arguments.get("objective")),
                    departure_summary=transition_summary,
                )
                scene.name = name
                scene.scene_type = scene_type
                scene.objective = self._clean(arguments.get("objective"))
                ended = source_scene if source_scene in app.scene_manager.history else None
                frame = app.scene_frame_manager.ensure_frame(
                    scene=scene,
                    recent_chat=public_arrival,
                    world_state=app.world_state,
                    character_manager=app.character_manager,
                    contract=self._current_contract(app),
                )
                continuity_inherited = (
                    app.scene_frame_manager.inherit_transition_continuity(
                        previous_frame,
                        frame,
                        scene=scene,
                    )
                )
                for key, value in situation.items():
                    setattr(frame, key, value)
                app.scene_frame_manager._touch(frame)
                if not context.metadata.get("system_gm_beat_request"):
                    for mover in movers:
                        event = app.record_free_scene_player_action(mover)
                        if event:
                            action_round = event
                            action_round_events.append(event)
                    clock_lines = app.turn_response_renderer.public_state_lines(
                        action_round,
                        existing_lines=[public_arrival],
                    )
                    if clock_lines:
                        public_reply = "\n".join([public_arrival, *clock_lines])
                saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        except Exception as exc:
            self._restore(app, snapshot)
            return self._failure(
                "transition_scene",
                "SCENE_TRANSITION_FAILED",
                str(exc),
                "角色位置保持原状；修正转场参数后重试。",
            )
        return GMToolReceipt(
            tool_name="transition_scene",
            ok=True,
            result={
                "ended_scene": ended.name if ended else "",
                "scene": {
                    "scene_id": scene.scene_id,
                    "name": scene.name,
                    "scene_type": scene.scene_type.value,
                    "location": scene.location,
                    "participants": list(scene.participants),
                },
                "movers": movers,
                "mover_consents": list(arguments.get("mover_consents") or []),
                "npc_companions": companions,
                "destination_npcs": destination_npcs,
                "location_continuity_inherited": continuity_inherited,
                "movement_mode": movement_mode,
                "creative_author": creative_metadata,
                "action_round": dict(action_round),
                "action_round_events": list(action_round_events),
                "allowed_followup_tools": [
                    "decide_npc_response",
                    "introduce_npc",
                    "start_conflict",
                ],
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply=public_reply,
            lock_public_reply=True,
            pacing_events=[
                GMToolPacingEvent(
                    player_action=not bool(
                        context.metadata.get("system_gm_beat_request")
                    ),
                    action_summary=str(
                        context.metadata.get("current_message") or ""
                    ).strip(),
                    public_image=self._first_sentence(public_arrival),
                    gm_beat_purpose=(
                        str(
                            context.metadata.get("heartbeat_beat_purpose")
                            or context.metadata.get("heartbeat_action")
                            or ""
                        ).strip()
                        if context.metadata.get("system_gm_beat_request")
                        else ""
                    ),
                )
            ],
        )

    def focus_scene_branch(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        """Focus a split-party branch without ending the previous camera."""

        tool_name = "focus_scene_branch"
        evidence_error = self._validate_evidence(
            context,
            arguments.get("evidence"),
            tool_name,
        )
        if evidence_error is not None:
            return evidence_error
        gate_error = self._require_adventure(context, tool_name)
        if gate_error is not None:
            return gate_error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        followup_tools = (
            list(self._SYSTEM_FOCUS_FOLLOWUP_TOOLS)
            if context.metadata.get("system_gm_beat_request")
            else list(self._FOCUS_FOLLOWUP_TOOLS)
        )
        current = app.scene_manager.current_scene
        if current is None:
            return self._failure(
                tool_name,
                "NO_ACTIVE_SCENE",
                "当前没有可暂存的聚焦场景。",
                "首次建立场景使用start_scene；普通物理转场使用transition_scene。",
            )
        if app.conflict_manager.state.active or current.scene_type == SceneType.CONFLICT:
            return self._failure(
                tool_name,
                "CONFLICT_ACTIVE",
                "冲突中不能切换到并行普通镜头绕过回合。",
                "先按当前冲突回合行动或正式结束冲突。",
            )
        blocking_error = self._blocking_window_error(app, tool_name)
        if blocking_error is not None:
            return blocking_error

        actor = self._clean(arguments.get("actor"))
        if not actor or not app.character_manager.exists(actor):
            return self._failure(
                tool_name,
                "UNKNOWN_ACTOR",
                f"没有找到玩家角色【{actor or '未指定'}】。",
                "先读取当前角色与玩家映射。",
            )
        character = app.character_manager.get(actor)
        if "pc" not in character.traits:
            return self._failure(
                tool_name,
                "FOCUS_ACTOR_MUST_BE_PC",
                "并行玩家镜头只能由玩家角色发起。",
                "NPC行动继续使用NPC工具；不要把NPC当成玩家镜头。",
            )
        if not context.metadata.get("system_gm_beat_request"):
            controls = self.host._player_character_control_map(runtime)
            known_ownership = any(controls.values())
            if known_ownership and actor not in set(controls.get(context.speaker, [])):
                return self._failure(
                    tool_name,
                    "PLAYER_CHARACTER_NOT_CONTROLLED",
                    f"发言者不能替【{actor}】切换镜头。",
                    "只使用当前玩家实际控制且本句明确行动的角色。",
                )
        if actor in current.participants:
            if (
                context.metadata.get("heartbeat_action") == "defeat_aftermath"
                and actor in app.conflict_manager.state.fallen_pcs
            ):
                # 镜头虽已对准败北角色，但新场景尚未开始。若只提交叙事，
                # 败北标记会一直残留，并让同一个后果节拍反复触发。
                followup_tools = ["transition_scene"]
            return GMToolReceipt.success(
                tool_name,
                result={
                    "mode": "current",
                    "scene_id": current.scene_id,
                    "actor": actor,
                    "allowed_followup_tools": list(followup_tools),
                    "required_followup_tools": list(followup_tools),
                },
                state_changed=False,
            )

        name = self._clean(arguments.get("name"))
        location = self._clean(arguments.get("location"))
        if not name or not location:
            return self._failure(
                tool_name,
                "FOCUS_FIELDS_REQUIRED",
                "并行镜头必须有场景名和地点。",
                "依据角色最后位置与本次明确行动填写，不要虚构远距离移动。",
            )
        try:
            scene_type = SceneType(self._clean(arguments.get("scene_type")))
        except ValueError:
            return self._failure(
                tool_name,
                "INVALID_SCENE_TYPE",
                "并行镜头场景类型无效。",
                "使用工具schema中的非冲突场景类型。",
            )
        existing_branch = next(
            (
                scene
                for scene in app.scene_manager.suspended_scenes
                if actor in scene.participants
                or app.scene_manager._same_exact_location(scene.location, location)
            ),
            None,
        )
        if existing_branch is not None:
            # The caller is restoring camera authority, not creating a managed
            # travel/dungeon/rest lifecycle.  Preserve the authoritative type
            # already stored on that branch even if the model supplied a
            # generic fallback.
            scene_type = existing_branch.scene_type
        else:
            managed_type_error = self._generic_scene_type_error(
                scene_type,
                tool_name,
            )
            if managed_type_error is not None:
                return managed_type_error
        situation, situation_error = self._validate_private_situation(
            arguments.get("private_situation") or {},
            tool_name=tool_name,
        )
        if situation_error is not None:
            return situation_error

        snapshot = self._snapshot(app, context.campaign_id)
        try:
            with runtime.transaction_lock:
                app.scene_frame_manager.suspend_current_frame()
                scene, mode = app.scene_manager.focus_actor_branch(
                    actor,
                    name=name,
                    scene_type=scene_type,
                    location=location,
                    objective=self._clean(arguments.get("objective")),
                )
                frame = app.scene_frame_manager.restore_suspended_frame(scene)
                if frame is None:
                    frame = app.scene_frame_manager.ensure_frame(
                        scene=scene,
                        recent_chat=str(context.metadata.get("recent_public_context") or ""),
                        world_state=app.world_state,
                        character_manager=app.character_manager,
                        contract=self._current_contract(app),
                    )
                    for key, value in situation.items():
                        setattr(frame, key, value)
                    app.scene_frame_manager._touch(frame)
                saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        except Exception as exc:
            self._restore(app, snapshot)
            return self._failure(
                tool_name,
                "SCENE_FOCUS_FAILED",
                str(exc),
                "修正镜头参数后重试；不要改用普通转场结束另一分支。",
            )
        if (
            context.metadata.get("heartbeat_action") == "defeat_aftermath"
            and mode == "restored"
            and actor in app.conflict_manager.state.fallen_pcs
        ):
            # Refocusing the old conflict branch is only camera work. The
            # fallen hero still needs a new temporal scene so scene-start
            # recovery and the defeat consequence can be applied exactly once.
            followup_tools = ["transition_scene"]
        return GMToolReceipt.success(
            tool_name,
            result={
                "mode": mode,
                "actor": actor,
                "scene_id": scene.scene_id,
                "scene": {
                    "name": scene.name,
                    "scene_type": scene.scene_type.value,
                    "location": scene.location,
                    "participants": list(scene.participants),
                    "objective": scene.objective,
                },
                "suspended_scene_ids": [
                    item.scene_id for item in app.scene_manager.suspended_scenes
                ],
                "allowed_followup_tools": list(followup_tools),
                "required_followup_tools": list(followup_tools),
                # Focusing an existing branch only restores the internal
                # camera needed by the real follow-up action.  It does not
                # independently authorize a silent reply, nor should it force
                # an acknowledgement when that follow-up proves the player's
                # original sentence is already the complete public result.
                "silent_commit_neutral": True,
                "saved_path": saved_path,
            },
            state_changed=True,
        )

    def end_scene(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"), "end_scene")
        if evidence_error is not None:
            return evidence_error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        if app.scene_manager.current_scene is None:
            return self._failure("end_scene", "NO_ACTIVE_SCENE", "当前没有可结束的场景。", "读取runtime state后再决定是否转场。")
        if app.conflict_manager.state.active:
            return self._failure("end_scene", "CONFLICT_ACTIVE", "当前仍在冲突中。", "先调用end_conflict处理冲突结果。")
        lifecycle_error = self._active_scene_lifecycle_error(app, "end_scene")
        if lifecycle_error is not None:
            return lifecycle_error
        blocking_error = self._blocking_window_error(app, "end_scene")
        if blocking_error is not None:
            return blocking_error
        summary = self._clean(arguments.get("summary"))
        public_reply = self._clean_multiline(arguments.get("public_reply"))
        creative_metadata: dict[str, object] = {}
        creative_writer = getattr(app, "scene_creative_writer", None)
        if creative_writer is not None and creative_writer.available:
            scene = app.scene_manager.current_scene
            try:
                composition = creative_writer.compose_public_scene_text(
                    operation="scene_closure",
                    facts={
                        "scene_name": str(getattr(scene, "name", "") or ""),
                        "location": str(getattr(scene, "location", "") or ""),
                        "participants": list(getattr(scene, "participants", []) or []),
                        "summary": summary,
                        "creative_direction": self._clean(
                            arguments.get("creative_direction")
                        ),
                    },
                    recent_public_messages=self._recent_public_messages(context),
                    fallback_public_reply=public_reply,
                    deadline=context.agent_deadline_monotonic,
                )
            except SceneCreativeWriterError as exc:
                return self._failure(
                    "end_scene",
                    "SCENE_CREATIVE_AUTHOR_FAILED",
                    f"DeepSeek场景作者未能完成场景收束：{exc}",
                    "不要由核心GM补写成品；当前场景保持进行中，稍后重试。",
                )
            public_reply = composition.public_reply
            creative_metadata = {
                "author": "scene_creative_writer",
                "model": composition.model,
                "used_model": composition.used_model,
            }
        with runtime.transaction_lock:
            ended = app.end_scene(summary)
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        system_beat = bool(context.metadata.get("system_gm_beat_request"))
        require_resolution = bool(
            context.metadata.get("heartbeat_require_local_resolution")
        )
        return GMToolReceipt(
            tool_name="end_scene",
            ok=True,
            result={
                "ended_scene": ended.name if ended else "",
                "saved_path": saved_path,
                "creative_author": creative_metadata,
            },
            state_changed=True,
            public_fallback_reply=public_reply,
            lock_public_reply=True,
            pacing_events=[
                GMToolPacingEvent(
                    player_action=not system_beat,
                    action_summary=(
                        ""
                        if system_beat
                        else str(context.metadata.get("current_message") or "").strip()
                    ),
                    consequence=summary,
                    local_payoff=summary if require_resolution else "",
                    public_image=self._first_sentence(public_reply),
                    local_question_changed=bool(
                        context.metadata.get("heartbeat_require_local_change")
                    ),
                    local_question_resolved=require_resolution,
                    scene_resolved=require_resolution,
                    session_question_resolved=bool(
                        context.metadata.get("heartbeat_require_session_resolution")
                    ),
                    signature_image_evolved=require_resolution,
                    gm_beat_purpose=(
                        str(
                            context.metadata.get("heartbeat_beat_purpose")
                            or context.metadata.get("heartbeat_action")
                            or ""
                        ).strip()
                        if system_beat
                        else ""
                    ),
                )
            ],
        )

    @staticmethod
    def _initiative_roll_lines(value: object) -> list[str]:
        """Render actual team-initiative dice without exposing rule windows."""

        if not isinstance(value, list):
            return []
        attribute_labels = {
            "DEX": "敏捷",
            "INS": "洞察",
            "MIG": "力量",
            "WLP": "意志",
        }
        lines: list[str] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            roll = item.get("roll")
            dice = list(getattr(roll, "dice", []) or [])
            attributes = list(getattr(roll, "attributes", []) or [])
            if roll is None or not dice:
                continue
            actor = str(getattr(roll, "actor", "") or item.get("actor") or "队伍")
            attribute_text = "+".join(
                attribute_labels.get(str(name), str(name))
                for name in attributes
            ) or "未指定属性"
            dice_text = " + ".join(
                f"d{int(size)}={int(result)}" for size, result in dice
            )
            subtotal = sum(int(result) for _, result in dice)
            modifier = int(getattr(roll, "modifier", 0) or 0)
            outcome = "成功" if bool(getattr(roll, "success", False)) else "失败"
            if bool(getattr(roll, "critical_success", False)):
                outcome += "，大成功"
            elif bool(getattr(roll, "fumble", False)):
                outcome += "，大失败"
            lines.append(
                f"{actor}进行团队先攻检定：属性【{attribute_text}】；"
                f"掷骰 {dice_text} = {subtotal}；修正值 {modifier:+d}；"
                f"结算值 {int(getattr(roll, 'total', 0) or 0)} 对抗难度等级 "
                f"{int(getattr(roll, 'target_number', 0) or 0)}，{outcome}！"
            )
        return lines

    @staticmethod
    def _initiative_roll_actors(value: object) -> list[str]:
        """Return only actors whose initiative roll has a renderable result."""

        if not isinstance(value, list):
            return []
        actors: list[str] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            roll = item.get("roll")
            dice = list(getattr(roll, "dice", []) or [])
            actor = str(
                getattr(roll, "actor", "") or item.get("actor") or ""
            ).strip()
            if actor and dice and actor not in actors:
                actors.append(actor)
        return actors

    def start_conflict(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"), "start_conflict")
        if evidence_error is not None:
            return evidence_error
        gate_error = self._require_adventure(context, "start_conflict")
        if gate_error is not None:
            return gate_error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        if app.conflict_manager.state.active:
            return self._failure("start_conflict", "CONFLICT_ALREADY_ACTIVE", "冲突已经开始。", "继续当前回合，不要重新掷先攻。")
        blocking_error = self._blocking_window_error(app, "start_conflict")
        if blocking_error is not None:
            return blocking_error
        pcs, pcs_error = self._string_list(arguments.get("pcs"), tool_name="start_conflict", field_name="pcs", require_nonempty=True)
        if pcs_error is not None:
            return pcs_error
        allied_npcs, allied_error = self._string_list(
            arguments.get("allied_npcs") or [],
            tool_name="start_conflict",
            field_name="allied_npcs",
            require_nonempty=False,
        )
        if allied_error is not None:
            return allied_error
        enemies, enemy_error = self._string_list(arguments.get("enemies"), tool_name="start_conflict", field_name="enemies", require_nonempty=True)
        if enemy_error is not None:
            return enemy_error
        source_text = " ".join(
            part
            for part in (
                self._clean(context.metadata.get("current_message")),
                self._clean(arguments.get("evidence")),
            )
            if part
        )
        explicitly_named_enemies = sorted(
            character.name
            for character in app.character_manager.all()
            if character.name in source_text
            and {"enemy", "villain"}.intersection(character.traits)
        )
        omitted_named_enemies = sorted(
            set(explicitly_named_enemies) - set(enemies)
        )
        if omitted_named_enemies:
            return self._failure(
                "start_conflict",
                "EXPLICIT_ENEMY_ROSTER_CHANGED",
                (
                    "冲突名单遗漏了玩家明确指名且已有规则卡的敌人："
                    + "、".join(omitted_named_enemies)
                ),
                (
                    "把这些稳定名称逐项保留在enemies中；不要用所属集体替换。"
                    "若它们不应参战，先通过公开场景变化说明离场，再重新建立冲突。"
                ),
                result={
                    "explicitly_named_enemies": explicitly_named_enemies,
                    "omitted_named_enemies": omitted_named_enemies,
                },
            )
        collective_npcs, collective_error = self._string_list(
            arguments.get("collective_npcs") or [],
            tool_name="start_conflict",
            field_name="collective_npcs",
            require_nonempty=False,
        )
        if collective_error is not None:
            return collective_error
        # 支援团队先攻属于每名玩家自己的选择。公开工具不接受支援名单；
        # 只有所有待决窗口完成后，规则层才会通过私有参数回填已确认名单。
        supporters, supporter_error = self._string_list(
            arguments.get("_confirmed_initiative_supporters") or [],
            tool_name="start_conflict",
            field_name="_confirmed_initiative_supporters",
            require_nonempty=False,
        )
        if supporter_error is not None:
            return supporter_error
        side_duplicates = sorted(
            {
                name
                for name in [*pcs, *allied_npcs, *enemies]
                if sum(
                    name in side
                    for side in (pcs, allied_npcs, enemies)
                )
                > 1
            }
        )
        if side_duplicates:
            return self._failure(
                "start_conflict",
                "COMBATANT_ON_MULTIPLE_SIDES",
                "同一参战者不能同时属于多个阵营：" + "、".join(side_duplicates),
                "从pcs、allied_npcs和enemies中只保留一个归属。",
            )
        unknown_collectives = sorted(
            set(collective_npcs) - set([*allied_npcs, *enemies])
        )
        if unknown_collectives:
            return self._failure(
                "start_conflict",
                "COLLECTIVE_COMBATANT_UNKNOWN",
                "集体角色必须同时列在allied_npcs或enemies中："
                + "、".join(unknown_collectives),
                "保留稳定名称，并把每个collective_npcs项目放入对应参战阵营。",
            )
        scene = app.scene_manager.current_scene
        for collective_name in collective_npcs:
            app.world_state.ensure_npc_persona(
                collective_name,
                profile_status="placeholder",
                entity_kind="collective",
                public_identity=collective_name,
                role_in_story="当前冲突中的集体参与者",
                first_scene=str(getattr(scene, "name", "") or ""),
                current_location=str(getattr(scene, "location", "") or ""),
                last_seen_scene=str(
                    getattr(scene, "scene_id", "")
                    or getattr(scene, "name", "")
                    or ""
                ),
            )
        publication_lease_owner = self._clean(
            context.metadata.get("_gm_active_write_lease_owner")
        )
        try:
            auto_prepared = [
                *app.ensure_npc_combat_profiles(
                    allied_npcs,
                    combat_side="ally",
                    deadline=context.agent_deadline_monotonic,
                    publication_lease_owner=publication_lease_owner,
                ),
                *app.ensure_npc_combat_profiles(
                    enemies,
                    combat_side="enemy",
                    deadline=context.agent_deadline_monotonic,
                    publication_lease_owner=publication_lease_owner,
                ),
            ]
        except (TypeError, ValueError) as exc:
            return self._failure(
                "start_conflict",
                "NPC_COMBAT_PROFILE_PREPARATION_FAILED",
                str(exc),
                "保留当前场景；修正NPC人格或独立规则卡设计后重试冲突。",
            )
        missing = [
            name
            for name in [*pcs, *allied_npcs, *enemies]
            if not app.character_manager.exists(name)
        ]
        if missing:
            return self._failure(
                "start_conflict",
                "COMBAT_PROFILE_REQUIRED",
                "以下参战者没有规则战斗档案：" + "、".join(missing),
                "PC先完成建卡；NPC规则卡的自动继承未能完成，请先检查对应NPC档案。",
                result={"missing_combatants": missing},
            )
        invalid_pcs = [name for name in pcs if "pc" not in app.character_manager.get(name).traits]
        invalid_allies = [
            name
            for name in allied_npcs
            if (
                "ally" not in app.character_manager.get(name).traits
                or "pc" in app.character_manager.get(name).traits
                or {"enemy", "villain"}
                & set(app.character_manager.get(name).traits)
            )
        ]
        invalid_enemies = [
            name
            for name in enemies
            if not ({"enemy", "villain"} & set(app.character_manager.get(name).traits))
        ]
        if invalid_pcs or invalid_allies or invalid_enemies:
            return self._failure(
                "start_conflict",
                "COMBAT_SIDE_MISMATCH",
                "参战者阵营与规则档案不一致。",
                (
                    "pcs必须是玩家角色；allied_npcs必须有ally特质且不是PC或敌人；"
                    "enemies必须有enemy或villain特质。"
                ),
                result={
                    "invalid_pcs": invalid_pcs,
                    "invalid_allied_npcs": invalid_allies,
                    "invalid_enemies": invalid_enemies,
                },
            )
        sacrificed_pcs = [
            name for name in pcs if name in app.conflict_manager.state.sacrifices
        ]
        fallen_pcs = [
            name for name in pcs if name in app.conflict_manager.state.fallen_pcs
        ]
        zero_hp_combatants = [
            name
            for name in [*pcs, *allied_npcs, *enemies]
            if app.character_manager.get(name).hp <= 0
            and name not in app.conflict_manager.state.fallen_pcs
        ]
        if sacrificed_pcs:
            return self._failure(
                "start_conflict",
                "SACRIFICED_PC_CANNOT_RETURN",
                "已经牺牲的玩家角色不能再次加入冲突：" + "、".join(sacrificed_pcs),
                "从参战者中移除这些角色；牺牲通常是永久性的。",
                result={"sacrificed_pcs": sacrificed_pcs},
            )
        if fallen_pcs and app.scene_manager.current_scene is not None:
            return self._failure(
                "start_conflict",
                "FALLEN_PC_STILL_UNCONSCIOUS",
                "以下玩家角色在当前场景已经放弃抵抗，不能再次参战：" + "、".join(fallen_pcs),
                "结束当前场景；这些角色只会在下一次实际参与的新场景开始时恢复到危机值。",
                result={"fallen_pcs": fallen_pcs},
            )
        if zero_hp_combatants:
            return self._failure(
                "start_conflict",
                "ZERO_HP_COMBATANT_UNRESOLVED",
                "以下参战者仍为0生命值，不能开始新的冲突：" + "、".join(zero_hp_combatants),
                "先处理生命值归零的待决选择、恢复或敌人的退场结果。",
                result={"zero_hp_combatants": zero_hp_combatants},
            )
        leader = self._clean(arguments.get("leader"))
        if leader not in pcs:
            return self._failure("start_conflict", "INITIATIVE_LEADER_INVALID", "先攻领队不在参战PC中。", "从pcs中选择leader。")
        if any(name not in pcs or name == leader for name in supporters):
            return self._failure("start_conflict", "INITIATIVE_SUPPORTER_INVALID", "先攻协助者必须是除领队外的参战PC。", "修正supporters后重试。")
        support_decisions_confirmed = bool(
            arguments.get("_initiative_support_decisions_confirmed")
        )
        opening = self._clean_multiline(arguments.get("public_opening"))
        creative_metadata: dict[str, object] = {}
        creative_writer = getattr(app, "scene_creative_writer", None)
        opening_already_public = bool(
            arguments.get("_conflict_opening_already_public")
        )
        if (
            creative_writer is not None
            and creative_writer.available
            and not opening_already_public
        ):
            scene = app.scene_manager.current_scene
            try:
                composition = creative_writer.compose_public_scene_text(
                    operation="conflict_opening",
                    facts={
                        "scene_name": self._clean(arguments.get("scene_name")),
                        "location": str(getattr(scene, "location", "") or ""),
                        "pcs": list(pcs),
                        "allied_npcs": list(allied_npcs),
                        "enemies": list(enemies),
                        "objective": self._clean(arguments.get("objective")),
                        "creative_direction": self._clean(
                            arguments.get("creative_direction")
                        ),
                        "current_pressure": str(
                            getattr(app.scene_frame_manager.current_frame, "current_pressure", "")
                            or ""
                        ),
                        "visible_elements": list(
                            getattr(app.scene_frame_manager.current_frame, "visible_elements", [])
                            or []
                        ),
                    },
                    recent_public_messages=self._recent_public_messages(context),
                    fallback_public_reply=opening,
                    deadline=context.agent_deadline_monotonic,
                )
            except SceneCreativeWriterError as exc:
                return self._failure(
                    "start_conflict",
                    "SCENE_CREATIVE_AUTHOR_FAILED",
                    f"DeepSeek场景作者未能完成冲突开场：{exc}",
                    "不要由核心GM补写成品；冲突保持未开始，稍后重试同一工具。",
                )
            opening = composition.public_reply
            arguments = {**arguments, "public_opening": opening}
            creative_metadata = {
                "author": "scene_creative_writer",
                "model": composition.model,
                "used_model": composition.used_model,
            }
        if not opening:
            return self._failure("start_conflict", "CONFLICT_OPENING_REQUIRED", "冲突开场不能为空。", "先说清双方为什么此刻诉诸武力。")
        undecided_supporters = [name for name in pcs if name != leader]
        if undecided_supporters and not support_decisions_confirmed:
            group_id = str(uuid4())
            stored_arguments = {
                key: value
                for key, value in arguments.items()
                if not str(key).startswith("_")
            }
            scene = app.scene_manager.current_scene
            with runtime.transaction_lock:
                windows = [
                    app.interceptor.decision_window_manager.create(
                        kind="initiative_support",
                        owner=name,
                        prompt=f"【{name}】要支援【{leader}】的团队先攻检定吗？",
                        options=[
                            {"choice": "support", "label": "支援"},
                            {"choice": "skip", "label": "跳过"},
                        ],
                        scope_kind="scene" if scene is not None else "session",
                        scope_id=(
                            str(scene.scene_id)
                            if scene is not None
                            else context.session_id
                        ),
                        blocking=True,
                        allowed_responders=[name],
                        action_type=ActionType.RESOLVE_DECISION.value,
                        transaction_id=group_id,
                        resume_point="collect_initiative_support",
                        payload={
                            "initiative_support_group_id": group_id,
                            "start_conflict_arguments": stored_arguments,
                        },
                        dedupe_key=f"initiative-support:{group_id}:{name}",
                    )
                    for name in undecided_supporters
                ]
                saved_path = self.host._autosave_campaign(
                    runtime,
                    context.campaign_id,
                )
            support_line = (
                f"团队先攻由【{leader}】领队；"
                + "、".join(f"【{name}】" for name in undecided_supporters)
                + "分别决定是否支援。"
            )
            return GMToolReceipt(
                tool_name="start_conflict",
                ok=True,
                result={
                    "initiative_support_pending": True,
                    "initiative_support_group_id": group_id,
                    "pending_decisions": [
                        {
                            "window_id": window.window_id,
                            "kind": window.kind,
                            "owner": window.owner,
                            "options": list(window.options),
                        }
                        for window in windows
                    ],
                    "saved_path": saved_path,
                    "creative_author": creative_metadata,
                },
                state_changed=True,
                public_fallback_reply="\n".join((opening, support_line)),
                lock_public_reply=True,
            )
        snapshot = self._snapshot(app, context.campaign_id)
        try:
            with runtime.transaction_lock:
                scene_name = self._clean(arguments.get("scene_name"))
                objective = self._clean(arguments.get("objective"))
                scene = app.scene_manager.current_scene
                parent_scene = {
                    "_parent_scene_id": scene.scene_id if scene is not None else "",
                    "_parent_scene_name": scene.name if scene is not None else "",
                    "_parent_scene_type": (
                        scene.scene_type.value if scene is not None else ""
                    ),
                    "_parent_scene_objective": (
                        scene.objective if scene is not None else ""
                    ),
                    "_parent_scene_summary": (
                        scene.summary if scene is not None else ""
                    ),
                }
                if scene is None:
                    scene = app.start_scene(
                        scene_name,
                        SceneType.CONFLICT,
                        participants=[*pcs, *allied_npcs, *enemies],
                        objective=objective,
                    )
                else:
                    scene.scene_type = SceneType.CONFLICT
                    scene.name = scene_name
                    scene.objective = objective
                    for name in [*pcs, *allied_npcs, *enemies]:
                        app.scene_manager.add_participant(name)
                superseded_npc_questions = (
                    app.npc_response_windows.supersede_for_conflict(
                        app.scene_frame_manager.current_frame,
                        scene=scene,
                    )
                )
                if superseded_npc_questions:
                    app.scene_frame_manager.touch_current_state()
                resolution = app.interceptor.resolve(
                    Action(
                        ActionType.START_CONFLICT,
                        {
                            "scene_name": scene_name,
                            "pcs": pcs,
                            "allied_npcs": allied_npcs,
                            "enemies": enemies,
                            "leader": leader,
                            "_confirmed_supporters": supporters,
                            **parent_scene,
                        },
                    )
                )
                app.resolution_committer.commit(resolution)
                current_actor = str(app.conflict_manager.state.current_actor() or "")
                initiative_pending = bool(
                    resolution.payload.get("initiative_pending")
                    and not app.conflict_manager.state.active
                )
                pending_decisions = self._pending_decision_summaries(
                    app,
                    transaction_id=str(
                        resolution.payload.get("check_batch_id") or ""
                    ),
                )
                required_followup_tools: list[str] = []
                required_followup_calls: list[dict[str, object]] = []
                gm_fumble_required = add_gm_opportunity_followups(
                    pending_decisions=pending_decisions,
                    required_tools=required_followup_tools,
                    required_calls=required_followup_calls,
                )
                followup_mode = required_followup_mode(
                    required_followup_calls,
                    independent_obligation_added=gm_fumble_required,
                )
                initiative_roll_lines = self._initiative_roll_lines(
                    resolution.payload.get("check_batch_rolls")
                )
                check_batch_id = str(
                    resolution.payload.get("check_batch_id") or ""
                ).strip()
                published_roll_actors = self._initiative_roll_actors(
                    resolution.payload.get("check_batch_rolls")
                )
                if check_batch_id and published_roll_actors:
                    app.interceptor.check_batch_manager.mark_rolls_published(
                        check_batch_id,
                        published_roll_actors,
                    )
                decision_prompt = ""
                if initiative_pending and pending_decisions:
                    prompts = []
                    for pending in pending_decisions[:3]:
                        if str(pending.get("kind") or "") == "critical_opportunity":
                            prompts.append(
                                "这次大成功带来一个机会，你想要怎么使用它？"
                            )
                    decision_prompt = "\n".join(prompts)
                resolution_text = (
                    ""
                    if initiative_pending
                    else str(resolution.rules_text or "").strip()
                )
                public_reply = "\n".join(
                    part
                    for part in (
                        (
                            ""
                            if arguments.get("_conflict_opening_already_public")
                            else opening
                        ),
                        *initiative_roll_lines,
                        resolution_text,
                        decision_prompt,
                        f"轮到【{current_actor}】行动。" if current_actor else "",
                    )
                    if part
                )
                saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        except Exception as exc:
            self._restore(app, snapshot)
            return self._failure("start_conflict", "CONFLICT_START_FAILED", str(exc), "冲突状态保持未开始；修正规则实体或先攻参数后重试。")
        return GMToolReceipt(
            tool_name="start_conflict",
            ok=True,
            result={
                "scene_id": scene.scene_id,
                "turn_order": list(app.conflict_manager.state.turn_order),
                "allied_npcs": list(allied_npcs),
                "auto_prepared_npcs": list(auto_prepared),
                "current_actor": current_actor,
                "players_first": bool(resolution.payload.get("players_first")),
                "initiative_pending": initiative_pending,
                "initiative_roll_lines": initiative_roll_lines,
                "superseded_npc_questions": superseded_npc_questions,
                "check_batch_id": check_batch_id,
                "pending_decisions": pending_decisions,
                "allowed_followup_tools": list(required_followup_tools),
                "required_followup_tools": list(required_followup_tools),
                "required_followup_calls": list(required_followup_calls),
                "required_followup_mode": followup_mode,
                "saved_path": saved_path,
                "creative_author": creative_metadata,
            },
            state_changed=True,
            public_fallback_reply=public_reply,
            lock_public_reply=True,
            pacing_events=[
                GMToolPacingEvent(
                    player_action=not bool(
                        context.metadata.get("system_gm_beat_request")
                    ),
                    action_summary=str(
                        context.metadata.get("current_message") or ""
                    ).strip(),
                    consequence=(
                        f"冲突【{scene.name}】正在等待团队先攻定稿。"
                        if initiative_pending
                        else f"冲突【{scene.name}】开始。"
                    ),
                    public_image=self._first_sentence(public_reply),
                    opposition_move=(
                        self._first_sentence(public_reply)
                        if context.metadata.get("system_gm_beat_request")
                        else ""
                    ),
                    gm_beat_purpose=(
                        str(
                            context.metadata.get("heartbeat_beat_purpose")
                            or context.metadata.get("heartbeat_action")
                            or ""
                        ).strip()
                        if context.metadata.get("system_gm_beat_request")
                        else ""
                    ),
                )
            ],
        )

    def run_current_npc_turn(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        if not app.conflict_manager.state.active:
            return self._failure("run_current_npc_turn", "NO_ACTIVE_CONFLICT", "当前没有冲突回合。", "不要生成NPC战斗行动。")
        resolution_status = app.conflict_manager.resolution_status()
        if bool(resolution_status.get("ready_for_natural_end")):
            return self._failure(
                "run_current_npc_turn",
                "CONFLICT_READY_TO_END",
                "冲突的一方已经没有可行动成员，不能继续执行NPC回合。",
                "调用end_conflict结算已经成立的自然结果，不要让剩余角色继续空转。",
                result={"conflict_resolution_status": resolution_status},
            )
        actor = str(app.conflict_manager.state.current_actor() or "")
        expected = self._clean(arguments.get("expected_actor"))
        if actor != expected and expected:
            with runtime.transaction_lock:
                if app.conflict_manager.claim_current_side_turn(expected):
                    actor = expected
        if actor != expected:
            return self._failure(
                "run_current_npc_turn",
                "NPC_TURN_STATE_CHANGED",
                f"当前行动者已经是【{actor or '无'}】，不是【{expected}】。",
                "重新读取get_runtime_state后再决定是否执行NPC回合。",
            )
        if not app.character_manager.exists(actor):
            return self._failure("run_current_npc_turn", "NPC_COMBAT_PROFILE_MISSING", f"【{actor}】没有战斗档案。", "先建立NPC战斗档案。")
        actor_traits = set(app.character_manager.get(actor).traits)
        controls = self.host._player_character_control_map(runtime)
        player_controlled = any(
            actor in list(characters or [])
            for characters in controls.values()
        )
        canonical_npc = app.world_state.resolve_npc_name(actor) or actor
        has_npc_persona = canonical_npc in app.world_state.npc_personas
        declared_npc = bool(
            self._NPC_IDENTITY_TRAITS & actor_traits
        ) or has_npc_persona
        if "pc" in actor_traits or player_controlled:
            return self._failure("run_current_npc_turn", "CURRENT_ACTOR_IS_PLAYER", f"当前轮到玩家角色【{actor}】。", "等待该玩家行动，不得由GM代操。")
        if not declared_npc:
            return self._failure(
                "run_current_npc_turn",
                "CURRENT_ACTOR_NOT_NPC",
                f"【{actor}】既没有NPC身份，也没有玩家控制归属。",
                "先修复参战者身份或建立NPC档案；不要把未知归属角色自动当成玩家或NPC。",
            )
        snapshot = self._snapshot(app, context.campaign_id)
        action_parameters = {
            key: arguments[key]
            for key in (
                "npc_action_type",
                "target",
                "targets",
                "guarded_target",
                "spell_name",
                "chosen_damage_type",
                "chosen_status",
                "chosen_statuses",
                "attack_target",
                "attack_id",
                "attack_name",
                "skill_name",
                "other_action_name",
                "mp_amount",
                "status_effect",
                "clock_name",
                "target_number",
                "reasoning",
                "action_description",
            )
            if arguments.get(key) not in (None, "")
        }
        creative_metadata: dict[str, object] = {}
        creative_writer = getattr(app, "scene_creative_writer", None)
        if creative_writer is not None and creative_writer.available:
            scene = app.scene_manager.current_scene
            actor_profile = app.character_manager.get(actor)
            try:
                composition = creative_writer.compose_public_scene_text(
                    operation="npc_combat_action",
                    facts={
                        "actor": actor,
                        "public_identity": str(
                            getattr(actor_profile, "identity", "") or actor
                        ),
                        "traits": list(getattr(actor_profile, "traits", []) or []),
                        "selected_action": {
                            key: value
                            for key, value in action_parameters.items()
                            if key not in {"action_description", "reasoning"}
                        },
                        "creative_direction": self._clean(
                            arguments.get("creative_direction")
                        ),
                        "scene": {
                            "name": str(getattr(scene, "name", "") or ""),
                            "location": str(
                                getattr(scene, "location", "") or ""
                            ),
                            "participants": list(
                                getattr(scene, "participants", []) or []
                            ),
                        },
                    },
                    recent_public_messages=self._recent_public_messages(context),
                    fallback_public_reply=self._clean_multiline(
                        arguments.get("action_description")
                    ),
                    deadline=context.agent_deadline_monotonic,
                )
            except SceneCreativeWriterError as exc:
                return self._failure(
                    "run_current_npc_turn",
                    "SCENE_CREATIVE_AUTHOR_FAILED",
                    f"DeepSeek场景作者未能完成NPC动作表现：{exc}",
                    "不要由核心GM补写成品；当前回合保持不变，稍后重试。",
                )
            action_parameters["action_description"] = composition.public_reply
            creative_metadata = {
                "author": "scene_creative_writer",
                "model": composition.model,
                "used_model": composition.used_model,
                "operation": "npc_combat_action",
            }
        if not self._clean_multiline(action_parameters.get("action_description")):
            return self._failure(
                "run_current_npc_turn",
                "NPC_ACTION_DESCRIPTION_REQUIRED",
                "NPC回合缺少玩家可见的起手动作。",
                "提交合法动作与可选creative_direction，由场景作者生成。",
            )
        try:
            with runtime.transaction_lock:
                reply = app.run_npc_turn(
                    action_parameters,
                    self._clean(arguments.get("scene_brief")),
                )
                pending_decisions = self._pending_decision_summaries(app)
                required_followup_tools: list[str] = []
                required_followup_calls: list[dict[str, object]] = []
                gm_fumble_required = add_gm_opportunity_followups(
                    pending_decisions=pending_decisions,
                    required_tools=required_followup_tools,
                    required_calls=required_followup_calls,
                )
                followup_mode = required_followup_mode(
                    required_followup_calls,
                    independent_obligation_added=gm_fumble_required,
                )
                saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        except Exception as exc:
            self._restore(app, snapshot)
            return self._failure(
                "run_current_npc_turn",
                "NPC_TURN_FAILED",
                str(exc),
                (
                    "保持当前行动者不变，重新读取current_npc_tactical_snapshot，"
                    "从legal_actions中选择一项并修正目标或名称后重试。"
                ),
            )
        return GMToolReceipt(
            tool_name="run_current_npc_turn",
            ok=True,
            result={
                "actor": actor,
                "selected_action": dict(action_parameters),
                "creative_author": creative_metadata,
                "next_actor": str(app.conflict_manager.state.current_actor() or ""),
                "pending_decisions": pending_decisions,
                "allowed_followup_tools": list(required_followup_tools),
                "required_followup_tools": list(required_followup_tools),
                "required_followup_calls": list(required_followup_calls),
                "required_followup_mode": followup_mode,
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply=self._clean_multiline(reply),
            lock_public_reply=True,
        )

    def end_conflict(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"), "end_conflict")
        if evidence_error is not None:
            return evidence_error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        if not app.conflict_manager.state.active:
            return self._failure("end_conflict", "NO_ACTIVE_CONFLICT", "当前没有进行中的冲突。", "不要重复结束冲突。")
        blocking_error = self._blocking_window_error(app, "end_conflict")
        if blocking_error is not None:
            return blocking_error
        outcome = self._clean(arguments.get("outcome"))
        if not outcome:
            return self._failure(
                "end_conflict",
                "CONFLICT_OUTCOME_REQUIRED",
                "结束冲突时必须提交已经成立的客观结果。",
                "根据当前胜负、撤离、投降、谈判或目标完成情况写明结果；不能留空。",
            )
        continue_scene = bool(arguments.get("continue_scene"))
        public_reply = self._clean_multiline(arguments.get("public_reply"))
        conflict_state = app.conflict_manager.state
        scene = app.scene_manager.current_scene
        pending_exit_transitions = [
            dict(item)
            for item in list(conflict_state.pending_exit_transitions or [])
            if isinstance(item, dict)
        ]
        explicit_exit_transitions, transition_error = (
            self._validated_end_conflict_exit_transitions(
                context,
                app=app,
                scene=scene,
                value=arguments.get("exit_transitions") or [],
            )
        )
        if transition_error is not None:
            return transition_error
        for transition in explicit_exit_transitions:
            identity = (
                str(transition.get("destination") or ""),
                tuple(transition.get("participants") or []),
            )
            if any(
                (
                    str(existing.get("destination") or ""),
                    tuple(existing.get("participants") or []),
                )
                == identity
                for existing in pending_exit_transitions
            ):
                continue
            pending_exit_transitions.append(transition)
        creative_metadata: dict[str, object] = {}
        creative_writer = getattr(app, "scene_creative_writer", None)
        if creative_writer is not None and creative_writer.available:
            try:
                composition = creative_writer.compose_public_scene_text(
                    operation="conflict_closure",
                    facts={
                        "scene_name": str(getattr(scene, "name", "") or ""),
                        "location": str(getattr(scene, "location", "") or ""),
                        "participants": list(getattr(scene, "participants", []) or []),
                        "outcome": outcome,
                        "continue_scene": continue_scene,
                        "exit_transitions": pending_exit_transitions,
                        "fallen_pcs": dict(conflict_state.fallen_pcs),
                        "defeated_npc_fates": dict(
                            conflict_state.defeated_npc_fates
                        ),
                        "creative_direction": self._clean(
                            arguments.get("creative_direction")
                        ),
                    },
                    recent_public_messages=self._recent_public_messages(context),
                    fallback_public_reply=public_reply,
                    deadline=context.agent_deadline_monotonic,
                )
            except SceneCreativeWriterError as exc:
                return self._failure(
                    "end_conflict",
                    "SCENE_CREATIVE_AUTHOR_FAILED",
                    f"DeepSeek场景作者未能完成冲突收束：{exc}",
                    "不要由核心GM补写成品；冲突保持进行中，稍后重试。",
                )
            public_reply = composition.public_reply
            creative_metadata = {
                "author": "scene_creative_writer",
                "model": composition.model,
                "used_model": composition.used_model,
            }
        if not public_reply:
            return self._failure(
                "end_conflict",
                "CONFLICT_CLOSING_REQUIRED",
                "结束冲突时必须给玩家一个可感知的收束。",
                "用自然叙事说明战斗如何停下以及眼前局面，不输出后台状态。",
            )
        parent_scene_id = str(conflict_state.parent_scene_id or "")
        parent_scene_type = str(conflict_state.parent_scene_type or "")
        if parent_scene_id and (
            scene is None or str(scene.scene_id or "") != parent_scene_id
        ):
            return self._failure(
                "end_conflict",
                "CONFLICT_PARENT_SCENE_MISMATCH",
                "冲突所属的父场景与当前场景不一致，不能安全收束。",
                "重新读取当前场景与冲突状态；不要覆盖或跳过原场景。",
                result={
                    "parent_scene_id": parent_scene_id,
                    "current_scene_id": str(scene.scene_id or "") if scene else "",
                },
            )
        if (
            not continue_scene
            and parent_scene_type == SceneType.DUNGEON.value
            and app.dungeon_manager.state.active
        ):
            return self._failure(
                "end_conflict",
                "ACTIVE_DUNGEON_REQUIRES_SCENE_CONTINUATION",
                "这场冲突发生在仍在探索的地下城中，不能连同父场景一起直接结束。",
                (
                    "先用continue_scene=true结束冲突并返回地下城；"
                    "若队伍随后完成或撤离，再调用finish_dungeon_exploration。"
                ),
            )
        active_journey = app.travel_manager.active_journey
        if (
            not continue_scene
            and active_journey is not None
            and scene is not None
            and set(scene.participants).intersection(active_journey.party_names)
        ):
            return self._failure(
                "end_conflict",
                "ACTIVE_JOURNEY_REQUIRES_SCENE_CONTINUATION",
                "这场冲突发生在尚未结束的旅程中，不能连同旅行场景一起直接结束。",
                (
                    "先用continue_scene=true结束冲突并回到途中；"
                    "随后按实际结果调用continue_travel或abort_travel。"
                ),
            )
        landed_transitions: list[dict[str, object]] = []
        snapshot = self._snapshot(app, context.campaign_id)
        try:
            with runtime.transaction_lock:
                if continue_scene:
                    parent_scene_name = str(conflict_state.parent_scene_name or "")
                    parent_scene_objective = str(
                        conflict_state.parent_scene_objective or ""
                    )
                    parent_scene_summary = str(conflict_state.parent_scene_summary or "")
                    app.conflict_manager.end_scene(
                        list(scene.participants) if scene is not None else None
                    )
                    if scene is not None:
                        if parent_scene_id:
                            try:
                                scene.scene_type = SceneType(parent_scene_type)
                            except ValueError:
                                scene.scene_type = SceneType.STANDARD
                            scene.name = parent_scene_name or scene.name
                            scene.objective = parent_scene_objective
                            scene.summary = parent_scene_summary
                            if outcome:
                                scene.summary = (
                                    f"{parent_scene_summary}\n{outcome}".strip()
                                )
                        else:
                            scene.scene_type = SceneType.STANDARD
                            scene.summary = outcome
                elif scene is not None:
                    app.end_scene(outcome)
                else:
                    app.conflict_manager.end_scene()

                for transition in pending_exit_transitions:
                    destination = self._clean(transition.get("destination"))
                    participants = [
                        self._clean(item)
                        for item in list(transition.get("participants") or [])
                        if self._clean(item)
                    ]
                    if not destination or not participants:
                        continue
                    landed_scene, movement_mode = (
                        app.scene_manager.move_participants_to_location(
                            participants,
                            destination,
                            scene_name=self._clean(transition.get("scene_name")),
                            objective=self._clean(transition.get("objective")),
                            departure_summary=outcome,
                        )
                    )
                    app.scene_frame_manager.ensure_frame(
                        scene=landed_scene,
                        recent_chat=public_reply,
                        world_state=app.world_state,
                        character_manager=app.character_manager,
                        contract=self._current_contract(app),
                    )
                    app.scene_frame_manager.synchronize_current_location(destination)
                    for participant in participants:
                        npc_name = app.world_state.resolve_npc_name(participant)
                        if not npc_name:
                            continue
                        app.world_state.update_npc_state(
                            npc_name,
                            location=destination,
                            scene=str(landed_scene.scene_id or ""),
                        )
                    landed_transitions.append(
                        {
                            "destination": destination,
                            "participants": participants,
                            "scene_id": str(landed_scene.scene_id or ""),
                            "movement_mode": movement_mode,
                        }
                    )
                saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        except Exception as exc:
            self._restore(app, snapshot)
            return self._failure(
                "end_conflict",
                "CONFLICT_END_FAILED",
                str(exc) or "冲突结果没有完整提交。",
                "重新读取当前冲突与场景状态后再结束冲突；不要只在公开叙述中声称已经收束。",
            )
        return GMToolReceipt(
            tool_name="end_conflict",
            ok=True,
            result={
                "outcome": outcome,
                "continued_scene": continue_scene,
                "post_conflict_transitions": landed_transitions,
                "saved_path": saved_path,
                "creative_author": creative_metadata,
            },
            state_changed=True,
            public_fallback_reply=public_reply,
            lock_public_reply=True,
            pacing_events=[
                GMToolPacingEvent(
                    climax=outcome,
                    consequence=outcome,
                    public_image=self._first_sentence(public_reply),
                    local_question_resolved=True,
                    scene_resolved=True,
                )
            ],
        )

    def _validated_end_conflict_exit_transitions(
        self,
        context: GMToolExecutionContext,
        *,
        app: Any,
        scene: Any,
        value: object,
    ) -> tuple[list[dict[str, object]], GMToolReceipt | None]:
        if not isinstance(value, list):
            return [], self._failure(
                "end_conflict",
                "EXIT_TRANSITIONS_MUST_BE_ARRAY",
                "exit_transitions必须是数组。",
                "没有实际移动时提交空数组；有撤离时按schema提交目的地与人物。",
            )
        if len(value) > 4:
            return [], self._failure(
                "end_conflict",
                "TOO_MANY_EXIT_TRANSITIONS",
                "一次冲突收束最多提交4项实际转场。",
                "合并目的地相同的人物，或分批处理彼此独立的后续场景。",
            )
        if not value:
            return [], None
        if scene is None:
            return [], self._failure(
                "end_conflict",
                "EXIT_TRANSITION_SCENE_REQUIRED",
                "当前没有可供撤离的冲突父场景。",
                "重新读取冲突与场景状态后再提交。",
            )

        known_pcs = {
            character.name
            for character in app.character_manager.all()
            if "pc" in character.traits
        }
        controls = self.host._player_character_control_map(
            self.host._runtime(context.campaign_id)
        )
        known_ownership = any(controls.values())
        controlled = set(controls.get(context.speaker, []))
        present = set(scene.participants)
        seen_participants: set[str] = set()
        normalized: list[dict[str, object]] = []

        for raw in value:
            if not isinstance(raw, dict):
                return [], self._failure(
                    "end_conflict",
                    "EXIT_TRANSITION_MUST_BE_OBJECT",
                    "exit_transitions中的每一项都必须是JSON对象。",
                    "填写destination、participants，可选scene_name和objective。",
                )
            destination = self._clean(raw.get("destination"))
            participants, participants_error = self._string_list(
                raw.get("participants"),
                tool_name="end_conflict",
                field_name="exit_transitions.participants",
                require_nonempty=True,
            )
            if participants_error is not None:
                return [], participants_error
            if not destination:
                return [], self._failure(
                    "end_conflict",
                    "EXIT_DESTINATION_REQUIRED",
                    "冲突撤离必须写明实际抵达地点。",
                    "不要只写‘离开’或把地点藏在公开叙述里。",
                )
            non_pcs = [name for name in participants if name not in known_pcs]
            if non_pcs:
                return [], self._failure(
                    "end_conflict",
                    "EXIT_TRANSITION_PC_ONLY",
                    "exit_transitions只能替玩家角色提交撤离：" + "、".join(non_pcs),
                    "NPC后续去向应由NPC行动或后续场景工具分别提交。",
                )
            absent = [name for name in participants if name not in present]
            if absent:
                return [], self._failure(
                    "end_conflict",
                    "EXIT_PARTICIPANT_NOT_PRESENT",
                    "以下角色不在当前冲突场景：" + "、".join(absent),
                    "只提交当前确实在场并已成立撤离结果的角色。",
                )
            duplicate = [name for name in participants if name in seen_participants]
            if duplicate:
                return [], self._failure(
                    "end_conflict",
                    "EXIT_PARTICIPANT_DUPLICATED",
                    "同一角色不能在一次收束中抵达多个地点：" + "、".join(duplicate),
                    "只保留该角色实际抵达的一个目的地。",
                )
            if not context.metadata.get("system_gm_beat_request"):
                unauthorized = [
                    name
                    for name in participants
                    if known_ownership and name not in controlled
                ]
                if unauthorized:
                    return [], self._failure(
                        "end_conflict",
                        "EXIT_CHARACTER_NOT_CONTROLLED",
                        "发言者不能替其他玩家角色撤离：" + "、".join(unauthorized),
                        "只保留当前玩家明确控制的角色。",
                    )
            seen_participants.update(participants)
            normalized.append(
                {
                    "destination": destination,
                    "participants": participants,
                    "scene_name": self._clean(raw.get("scene_name")),
                    "objective": self._clean(raw.get("objective")),
                }
            )
        return normalized, None

    @classmethod
    def _active_scene_lifecycle_error(
        cls,
        app: Any,
        tool_name: str,
    ) -> GMToolReceipt | None:
        scene = app.scene_manager.current_scene
        if scene is None:
            return None
        if (
            app.dungeon_manager.state.active
            and scene.scene_type == SceneType.DUNGEON
        ):
            return cls._failure(
                tool_name,
                "ACTIVE_DUNGEON_REQUIRES_DUNGEON_TOOL",
                "当前镜头仍在进行地下城探索，不能用普通场景工具绕过区域、危险命刻或出口。",
                (
                    "区域行动使用ExploreDungeon；真实完成、撤退或放弃后调用"
                    "finish_dungeon_exploration。"
                ),
            )
        journey = app.travel_manager.active_journey
        if journey is not None and set(scene.participants).intersection(
            journey.party_names
        ):
            return cls._failure(
                tool_name,
                "ACTIVE_JOURNEY_REQUIRES_TRAVEL_TOOL",
                "当前镜头中的队伍仍在一段尚未结束的旅程中，不能用普通场景工具跳过旅行状态。",
                (
                    "处理途中事件后调用continue_travel；若玩家决定返程、停留或改道，"
                    "调用abort_travel。途中冲突使用start_conflict。"
                ),
            )
        return None

    @classmethod
    def _generic_scene_type_error(
        cls,
        scene_type: SceneType,
        tool_name: str,
    ) -> GMToolReceipt | None:
        if scene_type in cls._GENERIC_SCENE_TYPES:
            return None
        typed_tool = {
            SceneType.TRAVEL: "travel_party",
            SceneType.DUNGEON: "start_dungeon_exploration",
            SceneType.REST: "perform_character_action（休息）",
        }.get(scene_type, "对应的专用规则工具")
        return cls._failure(
            tool_name,
            "MANAGED_SCENE_TYPE_REQUIRES_TYPED_TOOL",
            f"【{scene_type.value}】场景由专用规则生命周期管理，不能用普通场景工具建立。",
            f"调用{typed_tool}，让场景画面与规则状态在同一事务中生效。",
        )

    @classmethod
    def _validate_private_situation(
        cls,
        value: object,
        *,
        tool_name: str = "start_scene",
    ) -> tuple[dict[str, object], GMToolReceipt | None]:
        if not isinstance(value, dict):
            return {}, cls._failure(tool_name, "PRIVATE_SITUATION_MUST_BE_OBJECT", "private_situation必须是对象。", "按场景框架字段提交。")
        unknown = sorted(set(value) - cls._FRAME_SCALARS - cls._FRAME_LISTS)
        if unknown:
            return {}, cls._failure(tool_name, "UNKNOWN_SCENE_FRAME_FIELD", "场景框架包含未声明字段：" + "、".join(unknown), "只使用工具声明的局面字段。")
        result: dict[str, object] = {}
        for key in cls._FRAME_SCALARS:
            if key in value:
                result[key] = cls._clean(value.get(key))
        for key in cls._FRAME_LISTS:
            if key not in value:
                continue
            raw = value.get(key)
            if not isinstance(raw, list):
                return {}, cls._failure(tool_name, "SCENE_FRAME_LIST_REQUIRED", f"场景框架字段【{key}】必须是数组。", "改为字符串数组后重新提交。")
            result[key] = list(dict.fromkeys(cls._clean(item) for item in raw if cls._clean(item)))[:20]
        return result, None

    @staticmethod
    def _recent_public_messages(
        context: GMToolExecutionContext,
    ) -> list[dict[str, object]]:
        raw = context.metadata.get("recent_public_messages")
        if not isinstance(raw, list):
            return []
        return [dict(item) for item in raw if isinstance(item, dict)][-8:]

    @classmethod
    def _opening_scene_prep_gaps(
        cls,
        situation: dict[str, object],
        contract: Any,
    ) -> list[str]:
        """Validate breadth only; the GM remains the semantic authority."""

        def scalar(name: str, contract_name: str | None = None) -> str:
            direct = cls._clean(situation.get(name))
            if direct:
                return direct
            return cls._clean(
                getattr(contract, contract_name or name, "")
                if contract is not None
                else ""
            )

        def strings(name: str, contract_name: str | None = None) -> list[str]:
            values = [
                cls._clean(item)
                for item in list(situation.get(name) or [])
                if cls._clean(item)
            ]
            if contract is not None:
                values.extend(
                    cls._clean(item)
                    for item in list(
                        getattr(contract, contract_name or name, []) or []
                    )
                    if cls._clean(item)
                )
            return list(dict.fromkeys(values))

        gaps = [
            label
            for name, label in cls._OPENING_SCENE_PREP_SCALARS.items()
            if not scalar(name)
        ]
        secrets = strings("secrets", "flexible_secrets")
        reveals = strings("possible_reveals")
        if contract is not None:
            reveals.extend(
                cls._clean(getattr(item, "success_reveal", ""))
                for item in list(getattr(contract, "clue_routes", []) or [])
                if cls._clean(getattr(item, "success_reveal", ""))
            )
        reveals = list(dict.fromkeys(reveals))
        visible = strings("visible_elements")
        clues = strings("clue_pool")
        if contract is not None:
            clues.extend(
                cls._clean(getattr(item, "visible_lead", ""))
                for item in list(getattr(contract, "clue_routes", []) or [])
                if cls._clean(getattr(item, "visible_lead", ""))
            )
        clues = list(dict.fromkeys(clues))
        escalations = strings("escalation_ladder")
        payoffs = strings("possible_payoffs")
        if not secrets:
            gaps.append("至少一项尚未公开、可随行动调整的真相")
        if len(reveals) < 2:
            gaps.append("至少两条可从不同路径获得的揭示")
        if len(clues) < 2:
            gaps.append("至少两项玩家能实际追查的线索入口")
        if len(visible) < 2:
            gaps.append("至少两个开场即可接触的现场事物")
        if len(escalations) < 2:
            gaps.append("至少两级不会预设玩家失败的局势升级")
        if len(payoffs) < 2:
            gaps.append("至少两个可能由玩家选择造成的局部结果")
        return gaps

    @classmethod
    def _merge_opening_situation_fallback(
        cls,
        authored: dict[str, object],
        fallback: dict[str, object],
    ) -> dict[str, object]:
        """Fill only missing opening breadth from the deterministic base.

        The creative author may legitimately reshape private prep, but a
        successful prose response must not erase the complete deterministic
        situation already signed by ``start_adventure``.  Keep every authored
        value and add only the minimum locally required entries.  The merged
        packet still goes through the normal shape, breadth, private-leak,
        factual-grounding and player-agency checks before publication.
        """

        result = dict(authored or {})
        for name in cls._OPENING_SCENE_PREP_SCALARS:
            if cls._clean(result.get(name)):
                continue
            value = cls._clean(fallback.get(name))
            if value:
                result[name] = value

        required_counts = {
            "visible_elements": 2,
            "clue_pool": 2,
            "secrets": 1,
            "possible_reveals": 2,
            "escalation_ladder": 2,
            "possible_payoffs": 2,
        }
        for name, minimum in required_counts.items():
            values = [
                cls._clean(item)
                for item in list(result.get(name) or [])
                if cls._clean(item)
            ]
            for item in list(fallback.get(name) or []):
                clean = cls._clean(item)
                if clean and clean not in values:
                    values.append(clean)
                if len(values) >= minimum:
                    break
            if values:
                result[name] = values[:20]
        return result

    @classmethod
    def _private_leak(cls, public_reply: str, situation: dict[str, object]) -> str:
        compact_reply = " ".join(public_reply.split())
        for field in cls._HIDDEN_FRAME_FIELDS:
            raw = situation.get(field)
            values = raw if isinstance(raw, list) else [raw]
            for value in values:
                secret = cls._clean(value)
                if len(secret) >= 4 and secret in compact_reply:
                    return field
        return ""

    @staticmethod
    def _current_contract(app: Any) -> Any:
        plan = getattr(app.story_arc_manager.state, "current_pacing_plan", None)
        return getattr(plan, "dramatic_contract", None)

    @staticmethod
    def _snapshot(app: Any, campaign_id: str) -> CampaignStateSnapshot:
        return CampaignStateTransaction.capture(app, campaign_id)

    @staticmethod
    def _restore(app: Any, snapshot: CampaignStateSnapshot) -> None:
        CampaignStateTransaction.restore(app, snapshot)

    @classmethod
    def _opening_equipment_restriction_gaps(
        cls,
        contract: Any,
        submitted: list[dict[str, object]],
    ) -> list[str]:
        """Require every prepared opening restriction in the atomic scene start."""

        expected = [
            dict(item)
            for item in list(
                getattr(contract, "opening_equipment_restrictions", []) or []
            )
            if isinstance(item, dict)
        ]
        if not expected:
            return []
        submitted_by_actor: dict[str, set[str]] = {}
        for item in submitted:
            if str(item.get("mode") or "").strip() != "restrict":
                continue
            actor = cls._clean(item.get("actor"))
            submitted_by_actor.setdefault(actor, set()).update(
                cls._clean(name)
                for name in list(item.get("items") or [])
                if cls._clean(name)
            )

        gaps: list[str] = []
        for item in expected:
            actor = cls._clean(item.get("actor"))
            required_items = {
                cls._clean(name)
                for name in list(item.get("items") or [])
                if cls._clean(name)
            }
            missing = sorted(required_items - submitted_by_actor.get(actor, set()))
            if missing:
                gaps.append(f"【{actor or '未指定角色'}】缺少：{'、'.join(missing)}")
        return gaps

    @classmethod
    def _validate_equipment_access_changes(
        cls,
        app: Any,
        value: object,
        *,
        tool_name: str,
        participants: list[str],
    ) -> tuple[list[dict[str, object]], GMToolReceipt | None]:
        if value in (None, []):
            return [], None
        if not isinstance(value, list) or any(
            not isinstance(item, dict) for item in value
        ):
            return [], cls._failure(
                tool_name,
                "EQUIPMENT_ACCESS_CHANGES_MUST_BE_ARRAY",
                "equipment_access_changes必须是对象数组。",
                "每项填写actor、mode和items；没有变化时省略整个字段。",
            )
        result: list[dict[str, object]] = []
        for index, raw in enumerate(value, start=1):
            actor = cls._clean(raw.get("actor"))
            mode = cls._clean(raw.get("mode")).lower()
            items = raw.get("items")
            if not actor or not app.character_manager.exists(actor):
                return [], cls._failure(
                    tool_name,
                    "UNKNOWN_EQUIPMENT_OWNER",
                    f"第{index}项没有找到装备所属角色【{actor or '未指定'}】。",
                    "从当前角色状态中逐字填写actor。",
                )
            if actor not in participants:
                return [], cls._failure(
                    tool_name,
                    "EQUIPMENT_OWNER_NOT_IN_SCENE",
                    f"【{actor}】不在本场景participants中，不能在开场事务改变其装备取用状态。",
                    "把实际在场角色加入participants，或删除这项变化。",
                )
            if mode not in {"restrict", "restore"}:
                return [], cls._failure(
                    tool_name,
                    "INVALID_EQUIPMENT_ACCESS_MODE",
                    f"第{index}项mode必须是restrict或restore。",
                    "被收缴、封存或遗失使用restrict；取回使用restore。",
                )
            if not isinstance(items, list) or not items or any(
                not isinstance(item, str) or not cls._clean(item)
                for item in items
            ):
                return [], cls._failure(
                    tool_name,
                    "EQUIPMENT_ACCESS_ITEMS_REQUIRED",
                    f"第{index}项items必须包含至少一个具体装备名。",
                    "从角色的equipment_inventory逐字选择。",
                )
            try:
                resolved_items = list(
                    dict.fromkeys(
                        app.interceptor.economy_manager.resolve_owned_equipment_name(
                            actor,
                            cls._clean(item),
                        )
                        for item in items
                    )
                )
            except ValueError as exc:
                return [], cls._failure(
                    tool_name,
                    "EQUIPMENT_ACCESS_ITEM_NOT_OWNED",
                    str(exc),
                    "读取角色装备库存，使用角色实际拥有的具体名称。",
                )
            restore_loadout = bool(raw.get("restore_loadout", False))
            if mode == "restrict" and restore_loadout:
                return [], cls._failure(
                    tool_name,
                    "RESTRICT_CANNOT_RESTORE_LOADOUT",
                    "限制装备取用时不能同时恢复装备栏位。",
                    "将restore_loadout设为false或删除。",
                )
            result.append(
                {
                    "actor": actor,
                    "mode": mode,
                    "items": resolved_items,
                    "reason": cls._clean(raw.get("reason")),
                    "location": cls._clean(raw.get("location")),
                    "restore_loadout": restore_loadout,
                }
            )
        return result, None

    @classmethod
    def _string_list(
        cls,
        value: object,
        *,
        tool_name: str,
        field_name: str,
        require_nonempty: bool,
    ) -> tuple[list[str], GMToolReceipt | None]:
        if not isinstance(value, list):
            return [], cls._failure(tool_name, "STRING_ARRAY_REQUIRED", f"参数【{field_name}】必须是字符串数组。", "按工具schema重新提交。")
        if any(not isinstance(item, str) for item in value):
            return [], cls._failure(tool_name, "STRING_ARRAY_REQUIRED", f"参数【{field_name}】只能包含字符串。", "删除对象或数字元素。")
        result = list(dict.fromkeys(cls._clean(item) for item in value if cls._clean(item)))
        if require_nonempty and not result:
            return [], cls._failure(tool_name, "NONEMPTY_ARRAY_REQUIRED", f"参数【{field_name}】不能为空。", "提供至少一个规则实体名称。")
        return result, None

    @classmethod
    def _validate_evidence(
        cls,
        context: GMToolExecutionContext,
        value: object,
        tool_name: str,
    ) -> GMToolReceipt | None:
        if not is_current_message_evidence(context, value):
            return cls._failure(tool_name, "EVIDENCE_NOT_LITERAL", "evidence不是当前消息中的逐字连续片段。", "从current_message复制原句，不使用摘要或推断。")
        return None

    @classmethod
    def _validate_mover_consents(
        cls,
        *,
        context: GMToolExecutionContext,
        controls: dict[str, list[str]],
        unauthorized: list[str],
        value: object,
    ) -> GMToolReceipt | None:
        """Validate consent provenance; semantic meaning is audited by the LLM.

        Python deliberately does not infer agreement from prose. It only proves
        that every extra PC is controlled by the named speaker and that the
        cited literal came from that speaker in the current public transcript.
        """

        if value is None:
            return cls._failure(
                "transition_scene",
                "PLAYER_CHARACTER_NOT_CONTROLLED",
                "发言者不能在没有本人证据时替其他玩家的角色转场："
                + "、".join(unauthorized),
                "只移动当前玩家控制的角色，或为每个额外mover提供所属玩家本人近期公开发言的actor、speaker与逐字evidence。",
            )
        if not isinstance(value, list):
            return cls._failure(
                "transition_scene",
                "MOVER_CONSENT_INVALID",
                "mover_consents必须是逐角色同意对象数组。",
                "为每个非当前玩家控制的mover提供actor、speaker和近期公开发言的逐字evidence。",
            )
        sources = [
            dict(item)
            for key in ("recent_public_messages", "current_turn_events")
            for item in list(context.metadata.get(key) or [])
            if isinstance(item, dict)
        ]
        by_actor: dict[str, dict[str, str]] = {}
        unauthorized_set = set(unauthorized)
        for raw in value:
            if not isinstance(raw, dict):
                return cls._failure(
                    "transition_scene",
                    "MOVER_CONSENT_INVALID",
                    "mover_consents中的每一项都必须是对象。",
                    "每项只填写actor、speaker和evidence。",
                )
            actor = cls._clean(raw.get("actor"))
            speaker = cls._clean(raw.get("speaker"))
            evidence = normalize_literal_evidence(raw.get("evidence"))
            if not actor or not speaker or not evidence or actor not in unauthorized_set:
                return cls._failure(
                    "transition_scene",
                    "MOVER_CONSENT_INVALID",
                    "mover_consents包含空字段或不属于本次额外移动者的角色。",
                    "只为当前发言者不能控制的mover逐项提供同意证据。",
                )
            if actor in by_actor:
                return cls._failure(
                    "transition_scene",
                    "MOVER_CONSENT_DUPLICATED",
                    f"【{actor}】提交了重复的转场同意证据。",
                    "每名额外移动者只保留一项本人证据。",
                )
            if actor not in set(controls.get(speaker, [])):
                return cls._failure(
                    "transition_scene",
                    "MOVER_CONSENT_SPEAKER_MISMATCH",
                    f"【{speaker}】不是【{actor}】的控制玩家。",
                    "使用角色所属玩家本人近期公开发言，不接受代为同意。",
                )
            found = any(
                cls._clean(item.get("speaker")) == speaker
                and evidence
                in normalize_literal_evidence(item.get("text") or item.get("content"))
                for item in sources
            )
            if not found:
                return cls._failure(
                    "transition_scene",
                    "MOVER_CONSENT_EVIDENCE_NOT_FOUND",
                    f"没有在【{speaker}】的近期公开发言中找到【{actor}】的逐字同意证据。",
                    "从recent_messages复制该玩家本人原话；不要摘要、改写或代填。",
                )
            by_actor[actor] = {
                "actor": actor,
                "speaker": speaker,
                "evidence": evidence,
            }
        missing = [actor for actor in unauthorized if actor not in by_actor]
        if missing:
            return cls._failure(
                "transition_scene",
                "MOVER_CONSENT_REQUIRED",
                "以下角色缺少所属玩家本人的明确转场同意：" + "、".join(missing),
                "补充其近期公开原话；未同意的角色留在原场景。",
            )
        return None

    @classmethod
    def _require_adventure(
        cls,
        context: GMToolExecutionContext,
        tool_name: str,
    ) -> GMToolReceipt | None:
        if context.gate_status == "adventure":
            return None
        return cls._failure(tool_name, "ADVENTURE_NOT_ACTIVE", "当前还未进入冒险阶段。", "先完成会话门控与第零章，再建立冒险场景。")

    @classmethod
    def _blocking_window_error(cls, app: Any, tool_name: str) -> GMToolReceipt | None:
        blocking = [
            window
            for window in app.interceptor.decision_window_manager.pending()
            if window.blocking
        ]
        if not blocking:
            return None
        return cls._failure(
            tool_name,
            "BLOCKING_DECISION_PENDING",
            "仍有必须由玩家本人完成的规则选择，不能切换流程。",
            "先处理对应DecisionWindow。",
            result={"pending_windows": [window.window_id for window in blocking]},
        )

    @staticmethod
    def _pending_decision_summaries(
        app: Any,
        *,
        transaction_id: str = "",
    ) -> list[dict[str, object]]:
        return [
            {
                "window_id": window.window_id,
                "kind": window.kind,
                "owner": window.owner,
                "prompt": window.prompt,
                "options": list(window.options),
                "blocking": bool(window.blocking),
                "allowed_responders": list(window.allowed_responders),
                "payload": dict(window.payload),
            }
            for window in app.interceptor.decision_window_manager.pending()
            if not transaction_id or window.transaction_id == transaction_id
        ]

    @staticmethod
    def _clean(value: object) -> str:
        return " ".join(str(value or "").split()).strip()

    @staticmethod
    def _clean_multiline(value: object) -> str:
        return "\n".join(line.rstrip() for line in str(value or "").strip().splitlines()).strip()

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
        result: dict[str, object] | None = None,
        retryable: bool = True,
    ) -> GMToolReceipt:
        return GMToolReceipt(
            tool_name=tool_name,
            ok=False,
            error_code=code,
            message=message,
            correction_hint=hint,
            retryable=bool(retryable),
            result=dict(result or {}),
            public_fallback_reply="这一步还没有生效，我需要先把当前状态或规则条件确认清楚。",
        )
