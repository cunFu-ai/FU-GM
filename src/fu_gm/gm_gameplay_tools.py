from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Protocol

from fu_gm.components.campaign_state_transaction import CampaignStateTransaction
from fu_gm.components.portable_device_rules import portable_device_tiers
from fu_gm.components.scene_transition_coordinator import SceneTransitionCoordinator
from fu_gm.equipment_catalog import get_equipment_example
from fu_gm.gm_evidence import is_current_message_evidence
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolPacingEvent,
    GMToolReceipt,
    GMToolRegistry,
)
from fu_gm.gm_decision_followups import (
    add_gm_fumble_followups,
    required_followup_mode,
)
from fu_gm.models import Action, ActionType, Character, SceneType
from fu_gm.components.spell_parameter_manager import normalize_spell_damage_type
from fu_gm.skill_library import (
    SKILL_COVERAGE_PASSIVE_HARD,
    SPELL_GRANTING_SKILLS,
    get_skill_reference,
    normalize_skill_reference_name,
    skill_implementation_coverage,
    skill_rank,
)
from fu_gm.spellbook import (
    get_spell_definition,
    is_known_spell,
    normalize_spell_name,
)


class GameplayToolHost(Protocol):
    def _runtime(self, campaign_id: str, *, auto_load: bool = True) -> Any: ...

    def _autosave_campaign(self, runtime: Any, campaign_id: str) -> str: ...

    @staticmethod
    def _player_character_control_map(runtime: Any) -> dict[str, list[str]]: ...


class GMGameplayToolService:
    """Typed player-action boundary used by the GM tool agent.

    Semantic interpretation belongs to the model.  This component validates
    ownership, declared parameters and rule-domain boundaries, then delegates
    to the single ActionInterceptor lifecycle.
    """

    _CHECK_ACTIONS = {
        ActionType.HINDER,
        ActionType.INVESTIGATE,
        ActionType.OBJECTIVE,
        ActionType.REQUEST_ROLL,
        ActionType.PLAYER_VS_PLAYER,
    }
    _CHARACTER_ACTIONS = {
        ActionType.ATTACK,
        ActionType.SPELL,
        ActionType.GUARD,
        ActionType.EQUIP,
        ActionType.SKILL,
        ActionType.USE_INVENTORY,
        ActionType.TINKERER_GADGET,
    }
    _SCENE_ACTIONS = {
        ActionType.SHOP,
        ActionType.REST,
        ActionType.OPEN_CHEST,
        ActionType.EXPLORE_DUNGEON,
        ActionType.SELL_ITEM,
        ActionType.MANAGE_BOND,
    }
    _RITUAL_PROJECT_ACTIONS = {
        ActionType.PLAN_RITUAL,
        ActionType.CONTRIBUTE_RITUAL,
        ActionType.CAST_RITUAL,
        ActionType.START_PROJECT,
        ActionType.HIRE_PROJECT_HELPERS,
        ActionType.WORK_PROJECT,
    }
    _DECISION_ACTIONS = {
        ActionType.ATTACK,
        ActionType.SPELL,
        ActionType.HINDER,
        ActionType.OBJECTIVE,
        ActionType.USE_INVENTORY,
        ActionType.INVOKE_TRAIT,
        ActionType.INVOKE_BOND,
        ActionType.TRIGGER_OPPORTUNITY,
        ActionType.ACCEPT_STORY_CHANGE,
        ActionType.RESOLVE_ZERO_HP,
        ActionType.RESOLVE_DECISION,
        ActionType.SKILL,
        ActionType.NARRATE,
    }
    _ZERO_HP_CONSEQUENCE_TYPES = {
        "黑暗",
        "绝望",
        "损失",
        "怨恨",
        "分离",
    }
    _DARKNESS_THEMES = {"愤怒", "疑虑", "愧疚", "复仇"}
    _RESENTMENT_EMOTIONS = {"憎恨", "自卑", "猜忌"}
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
    _PROTECTED_PARAMETER_KEYS = {
        "player_facing_reply",
        "npc_speech_plan",
        "multi_npc_speech_plans",
        "npc_answer_generated",
        "routed_npc_dialogue",
        "routed_world_response",
        "roll",
        "rules_text",
        "payload",
        "_enforce_turn_order",
        "_strict_tool_transaction",
        "_decision_owner",
        "_fate_owner",
    }
    _FORBIDDEN_CHECK_CLOCK_KEYS = {
        "establish_threat_clock_name",
        "establish_threat_clock_delta",
        "threat_clock_delta",
        "advance_threat_on_failure",
        "threat_clock_advance_on_failure",
        "advance_established_threat_on_failure",
        "establish_threat_clock_advance_on_failure",
        "allow_advance_threat_on_success",
    }

    def __init__(self, host: GameplayToolHost) -> None:
        self.host = host

    def register_tools(self, registry: GMToolRegistry) -> None:
        registry.register(
            GMToolDefinition(
                name="get_gameplay_state",
                description="读取当前角色、回合、公开场景和待决窗口，供GM选择合法行动；不修改状态。",
                handler=self.get_gameplay_state,
            )
        )
        registry.register(
            GMToolDefinition(
                name="create_loyal_companion",
                description=(
                    "在拥有【忠诚伙伴】的玩家与团友已经共同确定伙伴设计后，"
                    "创建并保存这名5级伙伴。伙伴只能是野兽、构装体、元素或植物，"
                    "拥有一至两种基础攻击，不进入独立先攻回合；之后玩家用自己的"
                    "行动通过perform_character_action的【忠诚伙伴】技能指挥它。"
                    "玩家尚未确认设计时不要调用。"
                ),
                handler=self.create_loyal_companion,
                parameters=(
                    GMToolParameter(
                        "owner",
                        "string",
                        "拥有【忠诚伙伴】的玩家角色。",
                        required=True,
                    ),
                    GMToolParameter(
                        "name",
                        "string",
                        "伙伴的稳定名称。",
                        required=True,
                    ),
                    GMToolParameter(
                        "species",
                        "string",
                        "物种：野兽、构装体、元素或植物。",
                        required=True,
                        enum=("野兽", "构装体", "元素", "植物"),
                    ),
                    GMToolParameter(
                        "traits",
                        "array",
                        "恰好四个不同特质。",
                        required=True,
                    ),
                    GMToolParameter(
                        "attribute_spread",
                        "string",
                        "NPC属性分配：多面手、标准、专精或超级专精。",
                        required=True,
                        enum=("多面手", "标准", "专精", "超级专精"),
                    ),
                    GMToolParameter(
                        "attribute_order",
                        "array",
                        "按属性分配从高到低排列敏捷、洞察、力量、意志，各出现一次。",
                        required=True,
                    ),
                    GMToolParameter(
                        "selected_skills",
                        "array",
                        "按物种NPC技能预算共同选定的技能；重复学习也重复填写。",
                        required=True,
                    ),
                    GMToolParameter(
                        "skill_options",
                        "object",
                        "技能所需选项，例如强化伤害的攻击名、施法者的法术或专精选项。",
                        required=True,
                    ),
                    GMToolParameter(
                        "attacks",
                        "array",
                        (
                            "一至两种基础攻击。每项包含name、attributes、damage_type、range；"
                            "只有已选择足够次数【特殊攻击】时，才能加入multi_attack、"
                            "targets_magic_defense或status_effect_on_hit。"
                        ),
                        required=True,
                    ),
                    GMToolParameter(
                        "spell_attributes",
                        "object",
                        "可选；已学法术到两项中文施法属性的映射，省略时使用洞察+意志。",
                    ),
                    GMToolParameter(
                        "profile",
                        "object",
                        "可选的人格资料：public_identity、core_drive、manner、speech_style、combat_style、active_goal、voice_examples。",
                    ),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前消息中确认创建或确认最后一项伙伴设计的逐字原话。",
                        required=True,
                        source="current_message",
                    ),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="learn_chimerist_spell",
                description=(
                    "当拥有【形意咒法】的角色刚刚亲眼目睹同场的野兽、怪物或植物施放"
                    "一个已登记法术，并明确选择记忆它时使用。规则层会核对施法者物种、"
                    "法术、记忆上限与替换项，并持久记录该拟兽法术的来源物种。"
                    "这不是角色行动，不会推进冲突回合。"
                ),
                handler=self.learn_chimerist_spell,
                parameters=(
                    GMToolParameter(
                        "actor",
                        "string",
                        "学习法术的玩家角色。",
                        required=True,
                    ),
                    GMToolParameter(
                        "source",
                        "string",
                        "刚刚施放该法术、且当前同场的规则生物。",
                        required=True,
                    ),
                    GMToolParameter(
                        "spell_name",
                        "string",
                        "角色亲眼目睹的标准法术名。",
                        required=True,
                    ),
                    GMToolParameter(
                        "replace_spell",
                        "string",
                        "记忆已满时，玩家明确选择遗忘的现有拟兽使法术。",
                    ),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前玩家消息中明确选择学习或替换法术的原话。",
                        required=True,
                        source="current_message",
                    ),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="recall_scene_memory",
                description=(
                    "供拥有【记忆训练】的角色完美回忆自己参与过的近期场景。"
                    "不填scene_name时列出可回忆场景；填写后返回该场景已经公开的地点、"
                    "参与者、目标与收束，不会泄露GM暗线。若玩家要从记忆中继续推理，"
                    "随后再用perform_check_action进行调查。"
                ),
                handler=self.recall_scene_memory,
                parameters=(
                    GMToolParameter(
                        "actor",
                        "string",
                        "进行回忆的玩家角色。",
                        required=True,
                    ),
                    GMToolParameter(
                        "scene_name",
                        "string",
                        "要回忆的近期场景准确名称；省略时只列出可选场景。",
                    ),
                ),
            )
        )
        registry.register(
            GMToolDefinition(
                name="resolve_tavern_talk",
                description=(
                    "结算拥有【酒馆攀谈】的角色在旅店或酒馆休息后提出的一条本地问题。"
                    "必须提供GM依据当前世界与地点给出的公开答复；规则层会消耗一次剩余提问、"
                    "保存问答并限制每次休息最多使用技能等级次。这不是新的角色行动。"
                ),
                handler=self.resolve_tavern_talk,
                parameters=(
                    GMToolParameter(
                        "actor",
                        "string",
                        "提出问题的玩家角色。",
                        required=True,
                    ),
                    GMToolParameter(
                        "question",
                        "string",
                        "玩家关于周边地区或本地居民的实际问题。",
                        required=True,
                    ),
                    GMToolParameter(
                        "public_answer",
                        "string",
                        "GM面向玩家的自然答复；可保留传闻的不确定性。",
                        required=True,
                    ),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前玩家消息中的问题原话。",
                        required=True,
                        source="current_message",
                    ),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="perform_check_action",
                description=(
                    "提交调查、妨碍、推进目标、普通属性检定或玩家对抗。"
                    "主要目的是获得信息时使用Investigate；Hinder只用于直接削弱、延误、分散、压制或妨碍目标的行动与进度。"
                    "GM必须自行选择中文属性和难度等级；成功结果必须具体回答本次检定问题，"
                    "不能只说‘确认哪一个’或‘找出线索’；观察本身不能改变客观威胁命刻。"
                ),
                handler=self.perform_check_action,
                parameters=(
                    GMToolParameter(
                        "action_type",
                        "string",
                        "规则动作类型；获得信息选Investigate，直接阻碍目标才选Hinder。",
                        required=True,
                        enum=tuple(item.value for item in sorted(self._CHECK_ACTIONS, key=lambda item: item.value)),
                    ),
                    GMToolParameter("actor", "string", "执行动作的玩家角色。", required=True),
                    GMToolParameter("target", "string", "检定对象；观察环境时写具体环境范围。"),
                    GMToolParameter("attributes", "array", "两项中文属性：敏捷、洞察、力量、意志。", required=True),
                    GMToolParameter("difficulty", "integer", "GM裁定的难度等级，最低7。", required=True),
                    GMToolParameter("purpose", "string", "角色具体想做到什么。", required=True),
                    GMToolParameter("check_label", "string", "面向玩家的简短自然检定名称，不得复制后台目标列表。", required=True),
                    GMToolParameter(
                        "success_observation",
                        "string",
                        (
                            "成功后可原样公开的具体答案。比较多个方案时必须点明哪一个及可观察依据；"
                            "复合目的须逐项回答或明确仍未知的部分。直接写可感知事实或变化，"
                            "不要用‘角色确认了/找到了’概括动作，也禁止只把purpose改成过去时。"
                        ),
                        required=True,
                    ),
                    GMToolParameter("failure_consequence", "string", "失败后立即发生的具体反馈；只在结算失败后公开。", required=True),
                    GMToolParameter(
                        "success_transition",
                        "object",
                        (
                            "可选；仅当检定成功本身会让角色或随行NPC实际抵达另一地点时填写。"
                            "对象必须包含destination和participants；participants只能包含本次行动者及"
                            "行动前与其同处一地的随行NPC。可另填scene_name和objective。"
                            "只在最终骰面成功并公开成功结果后提交，失败或待重掷时不会移动任何人。"
                        ),
                    ),
                    GMToolParameter(
                        "condition_id",
                        "string",
                        (
                            "可选；仅当这次检定成功会完整履行scene.open_conditions中的一项公开条件时，"
                            "逐字填写该条件ID。只完成一部分或与条件无关时省略。"
                        ),
                    ),
                    GMToolParameter(
                        "details",
                        "object",
                        (
                            "可选规则参数，如成功线索、状态或现有命刻名。"
                            "若本检定用于结算地下城区域动作，填写dungeon_area为当前区域标准名，"
                            "最终检定回执才可被后续ExploreDungeon消费。"
                        ),
                    ),
                    GMToolParameter("evidence", "string", "当前玩家消息中的逐字行动证据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="perform_character_action",
                description=(
                    "提交攻击、施法、防御、装备、职业技能、消耗物资或造物使装置行动。"
                    "玩家要更换或卸下装备时选择Equip，并在details.slots中明确主手、副手、"
                    "防具、盾牌或饰品；规则层会查询库存、职业权限和手部占用。"
                ),
                handler=self.perform_character_action,
                parameters=(
                    GMToolParameter(
                        "action_type",
                        "string",
                        "规则动作类型。",
                        required=True,
                        enum=tuple(item.value for item in sorted(self._CHARACTER_ACTIONS, key=lambda item: item.value)),
                    ),
                    GMToolParameter("actor", "string", "执行动作的玩家角色。", required=True),
                    GMToolParameter("target", "string", "可选目标。"),
                    GMToolParameter(
                        "details",
                        "object",
                        (
                            "武器、法术、技能及其他规则参数。Skill必须填写skill_name；"
                            "契约与召唤在角色有多个契约时填写玩家选择的arcanum，唯一契约可省略。"
                            "Equip使用slots对象，键为main_hand、off_hand、armor、shield、accessory，"
                            "值为库存中的装备名；空字符串表示卸下。冲突中不能更换防具。"
                        ),
                        required=True,
                    ),
                    GMToolParameter("evidence", "string", "当前玩家消息中的逐字行动证据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="perform_scene_action",
                description=(
                    "提交休息、购物、开箱、地下城探索、出售或羁绊管理。"
                    "只在玩家已明确执行或GM已确认规则结果时调用；缺少付款者、物品、休息地点、"
                    "地下城区域等必要信息时先追问。"
                ),
                handler=self.perform_scene_action,
                parameters=(
                    GMToolParameter(
                        "action_type",
                        "string",
                        "规则动作类型。",
                        required=True,
                        enum=tuple(item.value for item in sorted(self._SCENE_ACTIONS, key=lambda item: item.value)),
                    ),
                    GMToolParameter("actor", "string", "执行动作的玩家角色。", required=True),
                    GMToolParameter("target", "string", "可选目标。"),
                    GMToolParameter(
                        "details",
                        "object",
                        (
                            "按action_type填写：Rest需要rest_type(wilderness/settlement)、safe_source、"
                            "rest_source_kind(tent/hospitality/lodging)；魔法帐篷还需payer，旅馆还需"
                            "settlement_size(village/town/city)和payer。participants仅可列同处一地且"
                            "明确一起休息的PC，通常省略让规则层推导。Shop购买需mode=buy、item_name、"
                            "quantity，可选equip；补充物资使用mode=restock；购买载具使用mode=buy_transport。"
                            "雇佣旅行服务只能在travel_party中结算，旅馆费只能随Rest结算。OpenChest需chest_name，奖励必须由GM已确认，"
                            "可填rarity/fixed_item/fixed_zenit。ExploreDungeon需area_name和"
                            "mode(enter/search/disarm_trap/open_treasure/clear/confront_boss)。"
                            "success不能由模型直接宣告；结算检定结果时必须提交perform_check_action返回的"
                            "check_receipt_id。SellItem需item_name和quantity。"
                            "ManageBond需target、emotions及mode。"
                        ),
                        required=True,
                        schema_details={
                            "additionalProperties": True,
                            "properties": {
                                "rest_type": {"type": "string"},
                                "safe_source": {"type": "string"},
                                "rest_source_kind": {"type": "string"},
                                "settlement_size": {"type": "string"},
                                "payer": {"type": "string"},
                                "participants": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "threat_clocks": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "mode": {"type": "string"},
                                "item_name": {"type": "string"},
                                "quantity": {"type": "integer", "minimum": 1},
                                "equip": {"type": "boolean"},
                                "chest_name": {"type": "string"},
                                "chest_id": {"type": "string"},
                                "rarity": {"type": "string"},
                                "fixed_item": {"type": "string"},
                                "fixed_zenit": {"type": "integer", "minimum": 0},
                                "area_name": {"type": "string"},
                                "check_receipt_id": {"type": "string"},
                                "success": {"type": "boolean"},
                                "collect_treasure": {"type": "boolean"},
                                "trigger_trap": {"type": "boolean"},
                                "danger_segments": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 6,
                                },
                                "clear_area": {"type": "boolean"},
                                "target": {"type": "string"},
                                "emotions": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "note": {"type": "string"},
                            },
                        },
                    ),
                    GMToolParameter("evidence", "string", "当前玩家消息中的逐字行动证据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="perform_in_scene_action",
                description=(
                    "结算同一普通场景内无需检定、不改变任何NPC位置/选择、也不构成场景边界的明确玩家行动，"
                    "例如跟随到同一走廊的另一处、站位守望、照看环境、完成简单操作，"
                    "或已经说出口的角色内发言与个人承诺。即使发言对象是另一名PC，也只记录当前PC自身"
                    "已经说或做的部分，不代表对方回应、接受、转告或履行。"
                    "本工具只记录PC自身动作、站位与行动轮，不生成环境结果或公开事实。"
                    "不用于未知结果、跨场景移动、NPC答复、命刻、仪式、冲突或规则技能。"
                ),
                handler=self.perform_in_scene_action,
                parameters=(
                    GMToolParameter("actor", "string", "执行动作的玩家角色。", required=True),
                    GMToolParameter(
                        "action_summary",
                        "string",
                        "忠实概括玩家已经明确执行的动作；只能决定该PC，不得把NPC配合或提议写成完成。",
                        required=True,
                    ),
                    GMToolParameter(
                        "position_note",
                        "string",
                        "可选：动作完成后角色在当前场景内的具体位置。",
                    ),
                    GMToolParameter(
                        "join_current_focus",
                        "boolean",
                        "仅当玩家明确从别处赶来、跟上或加入当前镜头中的队伍时为true。",
                    ),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前玩家消息中的逐字行动证据。",
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
                name="commit_story_item_action",
                description=(
                    "提交无需检定且已经明确执行的关键剧情物件操作，并持久记录物件的当前持有者、地点与状态。"
                    "用于取得、转交、放置、销毁或消耗会在后续剧情中继续重要的唯一物件；"
                    "普通装备和物资点仍使用规则行动工具。"
                ),
                handler=self.commit_story_item_action,
                parameters=(
                    GMToolParameter("actor", "string", "执行操作的玩家角色。", required=True),
                    GMToolParameter(
                        "operation",
                        "string",
                        "剧情物件操作。",
                        required=True,
                        enum=("acquire", "transfer", "place", "destroy", "consume"),
                    ),
                    GMToolParameter("item_name", "string", "剧情物件的公开名称。", required=True),
                    GMToolParameter(
                        "item_id",
                        "string",
                        "可选：已有剧情物件账本中的ID；首次取得时省略。",
                    ),
                    GMToolParameter(
                        "description",
                        "string",
                        "可选：首次取得时已经公开的简短物件描述，不得写入秘密。",
                    ),
                    GMToolParameter(
                        "to_holder",
                        "string",
                        "仅transfer使用：已经明确接受物件的新持有者。",
                    ),
                    GMToolParameter(
                        "to_location",
                        "string",
                        "place或transfer后的具体地点；省略时使用当前场景。",
                    ),
                    GMToolParameter(
                        "public_result",
                        "string",
                        "原样发给玩家的即时结果，只写已经发生的物件变化。",
                        required=True,
                    ),
                    GMToolParameter(
                        "public_fact",
                        "string",
                        "从public_result逐字复制的一句完整持久事实，必须明确物件目前由谁持有、位于何处或已被怎样处理。",
                        required=True,
                    ),
                    GMToolParameter(
                        "tags",
                        "array",
                        "可选的简短分类标签，例如线索、凭证、钥匙。",
                        schema_details={"items": {"type": "string", "minLength": 1}, "maxItems": 6},
                    ),
                    GMToolParameter(
                        "continue_with_check",
                        "boolean",
                        (
                            "仅当同一句玩家行动先明确完成本物件操作、随后还要进行一次不确定检定时为true。"
                            "本工具会先提交物件状态但暂不消耗普通场景行动轮，并强制后续调用perform_check_action；"
                            "没有后续检定时省略或设为false。"
                        ),
                    ),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前玩家消息中逐字支持该物件操作的行动证据。",
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
                name="move_group_within_scene",
                description=(
                    "提交已经解决、无需检定的同场景结伴移动：玩家角色带着此前已明确同意同行的NPC"
                    "在当前现场内调整站位，例如退到白花碑旁、走到门边或沿同一走廊移动。"
                    "本工具不会新建或切换场景，也不能移动其他PC。进入另一个独立地点使用move_scene_group；"
                    "NPC尚未同意时先调用decide_npc_response。若本次抵达使一项NPC短期承诺到期，"
                    "必须提交准确commitment_id和实际在场的commitment_responder，随后由NPC工具兑现。"
                ),
                handler=self.move_group_within_scene,
                parameters=(
                    GMToolParameter("actor", "string", "执行移动的玩家角色。", required=True),
                    GMToolParameter(
                        "companions",
                        "array",
                        "本次在同一场景中随行动者移动的具名NPC；不得包含其他PC。",
                        required=True,
                        schema_details={
                            "items": {"type": "string", "minLength": 1},
                            "minItems": 1,
                            "maxItems": 8,
                        },
                    ),
                    GMToolParameter(
                        "destination_position",
                        "string",
                        "当前场景内部实际移动到的简短静态站位。",
                        required=True,
                    ),
                    GMToolParameter(
                        "action_summary",
                        "string",
                        "忠实概括玩家明确执行的同场景结伴移动。",
                        required=True,
                    ),
                    GMToolParameter(
                        "public_result",
                        "string",
                        "可选：只有环境或已裁定角色产生新外部结果时才填写；不得仅复述玩家移动。",
                    ),
                    GMToolParameter(
                        "condition_id",
                        "string",
                        (
                            "可选：当本次完整移动正好履行scene.open_conditions中的公开要求时，"
                            "填写对应condition_id。工具只提交玩家履约，NPC承诺仍须由NPC回应工具实际兑现。"
                        ),
                    ),
                    GMToolParameter(
                        "commitment_id",
                        "string",
                        (
                            "可选：本次移动确实抵达scene.pending_npc_commitments中所写触发点时，"
                            "填写对应的准确commitment_id。不得根据相似措辞自行拼接。"
                        ),
                    ),
                    GMToolParameter(
                        "commitment_responder",
                        "string",
                        (
                            "与commitment_id配套：已经在companions中、抵达后会当场执行承诺的单个NPC。"
                            "可以不同于最初公开作出安排的NPC。"
                        ),
                    ),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前玩家消息中明确执行结伴移动的逐字证据。",
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
                name="move_scene_group",
                description=(
                    "提交已经解决、无需检定的跨场景移动：玩家角色明确抵达另一地点，"
                    "可以独自加入已经活动的目的地场景，也可以带着此前已同意同行的NPC抵达。"
                    "只在道路已经开放、没有未知障碍时使用；若包含NPC，其档案或公开答复还必须明确同意跟随。"
                    "若NPC尚未决定，先调用decide_npc_response；若结果不确定，使用perform_check_action并填写success_transition。"
                    "若玩家明确前往另一个场景并在抵达后立即询问当地NPC，填写followup_npc_name和"
                    "followup_response_instruction；移动成功后工具会强制在同一消息事务中继续NPC答复。"
                    "若抵达地点正是一项NPC短期承诺的触发点，必须提交准确commitment_id和同行的commitment_responder，"
                    "工具会强制后续NPC当场兑现。"
                ),
                handler=self.move_scene_group,
                parameters=(
                    GMToolParameter("actor", "string", "执行移动的玩家角色。", required=True),
                    GMToolParameter(
                        "companions",
                        "array",
                        (
                            "本次从同一来源场景随行动者一起抵达的具名NPC；不得包含其他PC。"
                            "只有行动者本人移动时提交空数组。"
                        ),
                        required=True,
                        schema_details={
                            "items": {"type": "string", "minLength": 1},
                            "minItems": 0,
                            "maxItems": 8,
                        },
                    ),
                    GMToolParameter("destination", "string", "实际抵达的完整地点名称。", required=True),
                    GMToolParameter(
                        "action_summary",
                        "string",
                        "忠实概括玩家明确执行的跨场景移动。",
                        required=True,
                    ),
                    GMToolParameter(
                        "public_result",
                        "string",
                        "原样发给玩家的即时结果，必须明确写出行动者及实际同行者已抵达目的地。",
                        required=True,
                    ),
                    GMToolParameter("position_note", "string", "可选：行动者抵达后的具体站位。"),
                    GMToolParameter(
                        "companion_positions",
                        "object",
                        "可选：以NPC名为键、抵达后站位为值。",
                    ),
                    GMToolParameter(
                        "public_facts",
                        "array",
                        "可选：从public_result逐字复制、值得持续索引的完整公开事实句。",
                        schema_details={
                            "items": {"type": "string", "minLength": 1},
                            "maxItems": 8,
                        },
                    ),
                    GMToolParameter(
                        "followup_npc_name",
                        "string",
                        (
                            "可选：玩家本句明确要在目的地立即询问或请求回应的单个NPC。"
                            "该NPC必须已经建档，且权威位置必须与destination一致。"
                        ),
                    ),
                    GMToolParameter(
                        "followup_response_instruction",
                        "string",
                        (
                            "与followup_npc_name配套：忠实列出玩家抵达后仍在等待NPC回答的问题或请求；"
                            "不得替NPC决定答案。"
                        ),
                    ),
                    GMToolParameter(
                        "commitment_id",
                        "string",
                        (
                            "可选：本次实际抵达scene.pending_npc_commitments所写触发点时，"
                            "填写对应的准确commitment_id。"
                        ),
                    ),
                    GMToolParameter(
                        "commitment_responder",
                        "string",
                        (
                            "与commitment_id配套：必须属于companions，并会在目的地当场执行承诺的单个NPC。"
                        ),
                    ),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前玩家消息中明确执行同行移动的逐字证据。",
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
                name="pass_in_scene_action",
                description=(
                    "记录玩家角色在具有完整行动轮压力的普通场景中明确选择本轮暂不行动、等待或让出行动。"
                    "这不会虚构角色动作或叙事；只有全体在场PC都已行动或略过后，自动命刻才推进一次。"
                    "没有活动行动轮命刻时应直接静默，不必调用。"
                ),
                handler=self.pass_in_scene_action,
                parameters=(
                    GMToolParameter("actor", "string", "明确选择本轮略过的玩家角色。", required=True),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前玩家消息中明确表示暂不行动、等待或让出行动的逐字证据。",
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
                name="perform_ritual_project_action",
                description="提交仪式的启动/推进/施放，或工程的启动/雇佣/工作行动。",
                handler=self.perform_ritual_project_action,
                parameters=(
                    GMToolParameter(
                        "action_type",
                        "string",
                        "规则动作类型。",
                        required=True,
                        enum=tuple(item.value for item in sorted(self._RITUAL_PROJECT_ACTIONS, key=lambda item: item.value)),
                    ),
                    GMToolParameter("actor", "string", "执行动作的玩家角色。", required=True),
                    GMToolParameter("details", "object", "仪式或工程的规则参数。", required=True),
                    GMToolParameter("evidence", "string", "当前玩家消息中的逐字行动证据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="resolve_rule_window",
                description=(
                    "处理归零、机会、特质/羁绊重掷、法术参数或其他当前待决窗口。"
                    "必须使用get_gameplay_state返回的window_id和合法选项，不得猜。"
                    "held_action缓存行动若玩家放弃或准备改选，使用ResolveDecision与"
                    "choice=discard/revise；若玩家确认，则不要关闭窗口，而应按payload中的"
                    "action_type与action_parameters提交原本的实际行动。"
                    "接受暂定检定结果时固定使用ResolveDecision与choice=accept_result；"
                    "不得把decline当作特质名或羁绊目标。"
                    "援用特质重掷两枚骰时无需重复填写原检定参数；原检定的成功、失败和转场事务由窗口保存。"
                    "只重掷一枚时才在details中明确reroll_indices与reroll_index_base=0。"
                    "spell_parameter窗口固定使用ResolveDecision与choice=cast_spell；"
                    "将玩家选择写入details.targets、details.chosen_damage_type、"
                    "details.chosen_status或details.chosen_attribute。"
                    "skill_parameter窗口若授予顺势行动，必须直接提交完整的Attack、Spell、"
                    "Hinder、Objective或UseInventory，不能先用ResolveDecision关闭窗口；"
                    "疾速身法的妨碍/推进目标还需在details中提供属性、难度等级、行动目的、"
                    "成功结果与失败后果。"
                    "zero_hp窗口中，玩家选择放弃抵抗时，GM必须在details中提交"
                    "consequence_type（黑暗/绝望/损失/怨恨/分离）与一项具体consequence；"
                    "选择牺牲时必须提交具体heroic_outcome，并明确两项场景条件布尔值。"
                ),
                handler=self.resolve_rule_window,
                parameters=(
                    GMToolParameter(
                        "action_type",
                        "string",
                        "处理窗口的规则动作。",
                        required=True,
                        enum=tuple(item.value for item in sorted(self._DECISION_ACTIONS, key=lambda item: item.value)),
                    ),
                    GMToolParameter("actor", "string", "窗口所属角色。", required=True),
                    GMToolParameter("window_id", "string", "当前待决窗口ID。", required=True),
                    GMToolParameter(
                        "choice",
                        "string",
                        "玩家明确选择；spell_parameter窗口使用cast_spell。",
                        required=True,
                    ),
                    GMToolParameter(
                        "details",
                        "object",
                        "目标、情感、机会效果或法术参数；法术目标使用targets数组。",
                    ),
                    GMToolParameter("evidence", "string", "当前玩家消息中的逐字选择证据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="resolve_gm_opportunity",
                description=(
                    "处理大失败后只属于GM的机会窗口。只能使用get_gameplay_state中"
                    "owner为__gm__且kind为fumble_opportunity的待决窗口；无需从玩家消息"
                    "伪造行动依据。"
                ),
                handler=self.resolve_gm_opportunity,
                parameters=(
                    GMToolParameter("window_id", "string", "GM机会待决窗口ID。", required=True),
                    GMToolParameter(
                        "choice",
                        "string",
                        "从窗口options逐字选择核心规则列出的机会效果。",
                        required=True,
                        enum=(
                            "揭示",
                            "进展",
                            "纽带",
                            "情报",
                            "青睐",
                            "审视",
                            "失态",
                            "失物",
                            "受苦",
                            "优势",
                            "转折",
                            "自定义",
                        ),
                    ),
                    GMToolParameter(
                        "details",
                        "object",
                        "该机会所需的目标、异常状态、现有命刻或具体不利转折。",
                        required=True,
                    ),
                ),
                side_effect="write",
            )
        )

    def state_summary(self, context: GMToolExecutionContext) -> dict[str, object]:
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        controls = self.host._player_character_control_map(runtime)
        current_actor = str(app.conflict_manager.state.current_actor() or "")
        pending_zero_hp = {
            window.owner
            for window in app.interceptor.decision_window_manager.pending(
                kind="zero_hp"
            )
        }
        characters = []
        for character in app.character_manager.all():
            if "pc" not in character.traits:
                continue
            if character.name in app.conflict_manager.state.sacrifices:
                defeat_state = "sacrificed"
            elif character.name in app.conflict_manager.state.fallen_pcs:
                defeat_state = "gave_up_resistance"
            elif character.name in pending_zero_hp:
                defeat_state = "awaiting_zero_hp_choice"
            else:
                defeat_state = ""
            characters.append(
                {
                    "name": character.name,
                    "level": character.level,
                    "hp": character.hp,
                    "max_hp": character.max_hp,
                    "mp": character.mp,
                    "max_mp": character.max_mp,
                    "inventory_points": character.inventory_points,
                    "max_inventory_points": character.max_inventory_points,
                    "fabula_points": character.fabula_points,
                    "zenit": character.zenit,
                    "experience_points": character.experience_points,
                    "conscious": not bool(defeat_state),
                    "can_act": not bool(defeat_state),
                    "defeat_state": defeat_state,
                    "active_defeat_consequence": app.conflict_manager.state.fallen_pcs.get(
                        character.name,
                        "",
                    ),
                    "defeat_consequences": list(
                        app.conflict_manager.state.pc_defeat_consequences.get(
                            character.name,
                            [],
                        )
                    ),
                    "attributes": {
                        "敏捷": character.attributes.get("DEX"),
                        "洞察": character.attributes.get("INS"),
                        "力量": character.attributes.get("MIG"),
                        "意志": character.attributes.get("WLP"),
                    },
                    "skills": sorted(character.skills),
                    "skill_options": {
                        name: list(choices)
                        for name, choices in character.skill_options.items()
                    },
                    "skill_counters": dict(character.skill_counters),
                    "spells": list(character.spells),
                    "chimerist_spells": dict(
                        character.chimerist_spell_species
                    ),
                    "loyal_companion": app.loyal_companion_manager.public_state(
                        character.name
                    ),
                    "defenses": dict(character.defenses),
                    "statuses": [
                        str(getattr(status, "value", status))
                        for status in character.statuses
                    ],
                    "equipment_inventory": list(character.equipment),
                    "equipment_templates": dict(character.equipment_templates),
                    "equipped": {
                        "main_hand": character.equipped_main_hand,
                        "off_hand": character.equipped_off_hand,
                        "armor": character.equipped_armor,
                        "shield": character.equipped_shield,
                        "accessory": character.equipped_accessory,
                    },
                }
            )
        windows = [
            {
                "window_id": window.window_id,
                "kind": window.kind,
                "owner": window.owner,
                "prompt": window.prompt,
                "options": list(window.options),
                "resolution_options": self._agent_decision_options(window),
                "blocking": window.blocking,
                "allowed_responders": list(window.allowed_responders),
                "payload": dict(window.payload),
            }
            for window in app.interceptor.decision_window_manager.pending()
        ]
        return {
            "speaker": context.speaker,
            "controlled_characters": list(controls.get(context.speaker, [])),
            "characters": characters,
            "character_locations": {
                character["name"]: app.scene_manager.location_of(character["name"])
                for character in characters
            },
            "conflict": {
                "active": bool(app.conflict_manager.state.active),
                "round": int(app.conflict_manager.state.round_number or 0),
                "current_actor": current_actor,
                "turn_order": list(app.conflict_manager.state.turn_order),
                "fallen_pcs": dict(app.conflict_manager.state.fallen_pcs),
                "sacrificed_pcs": sorted(app.conflict_manager.state.sacrifices),
                "pc_defeat_consequences": {
                    name: list(consequences)
                    for name, consequences in app.conflict_manager.state.pc_defeat_consequences.items()
                },
                "defeated_npc_fates": dict(
                    app.conflict_manager.state.defeated_npc_fates
                ),
            },
            "pending_decisions": windows,
            "story_items": [
                {
                    "item_id": item.item_id,
                    "name": item.name,
                    "description": item.description,
                    "holder": item.holder,
                    "location": item.location,
                    "status": item.status.value,
                    "tags": list(item.tags),
                }
                for item in app.world_state.story_items.values()
            ],
        }

    @staticmethod
    def _agent_decision_options(window: Any) -> list[dict[str, object]]:
        """Expose legal typed replies without mutating the rules window.

        Trait and bond windows store only their invocation choices because
        those options feed the corresponding rule action.  The tool agent also
        needs the distinct transaction decision that keeps the rolled result;
        presenting it explicitly prevents ``decline`` from being guessed as a
        trait name.
        """

        options: list[dict[str, object]] = []
        if window.kind == "trait_invocation":
            options.extend(
                {
                    "action_type": ActionType.INVOKE_TRAIT.value,
                    "choice": str(option.get("trait") or ""),
                }
                for option in window.options
                if str(option.get("trait") or "").strip()
            )
        elif window.kind == "bond_invocation":
            options.extend(
                {
                    "action_type": ActionType.INVOKE_BOND.value,
                    "choice": str(option.get("target") or ""),
                }
                for option in window.options
                if str(option.get("target") or "").strip()
            )
        elif window.kind == "skill_judgement" and str(window.payload.get("label") or "") == "幸运七":
            options.append(
                {
                    "action_type": ActionType.SKILL.value,
                    "choice": "幸运七",
                }
            )
        elif window.kind == "zero_hp":
            options.extend(
                {
                    "action_type": ActionType.RESOLVE_ZERO_HP.value,
                    "choice": str(option.get("choice") or ""),
                    "label": str(option.get("label") or ""),
                }
                for option in window.options
                if str(option.get("choice") or "").strip()
            )
        elif window.kind == "npc_fate":
            options.extend(
                {
                    "action_type": ActionType.RESOLVE_DECISION.value,
                    "choice": str(option.get("choice") or ""),
                    "label": str(option.get("label") or ""),
                }
                for option in window.options
                if str(option.get("choice") or "").strip()
            )
        elif window.kind == "held_action":
            options.extend(
                {
                    "action_type": (
                        str(window.payload.get("action_type") or "")
                        if str(option.get("choice") or "") == "confirm"
                        else ActionType.RESOLVE_DECISION.value
                    ),
                    "choice": str(option.get("choice") or ""),
                    "label": str(option.get("label") or ""),
                    "requires_actual_action": (
                        str(option.get("choice") or "") == "confirm"
                    ),
                }
                for option in window.options
                if str(option.get("choice") or "").strip()
            )
        elif window.kind == "skill_parameter":
            skill = str(
                window.payload.get("skill")
                or window.payload.get("label")
                or ""
            ).strip()
            action_types = {
                ("疾速身法", "attack"): (ActionType.ATTACK.value,),
                ("疾速身法", "hinder_or_objective"): (
                    ActionType.HINDER.value,
                    ActionType.OBJECTIVE.value,
                ),
                ("奥灵回响", "cast_spell"): (ActionType.SPELL.value,),
                ("鹰眼", "immediate_ranged_attack"): (
                    ActionType.ATTACK.value,
                ),
                ("应急用品", "use_inventory_action"): (
                    ActionType.USE_INVENTORY.value,
                ),
                ("快速评估", "declare_assessment"): (
                    ActionType.SKILL.value,
                ),
            }
            for option in window.options:
                choice = str(option.get("choice") or "").strip()
                if not choice:
                    continue
                mapped = action_types.get(
                    (skill, choice),
                    (ActionType.RESOLVE_DECISION.value,),
                )
                options.extend(
                    {
                        "action_type": action_type,
                        "choice": choice,
                        "label": str(option.get("label") or choice),
                    }
                    for action_type in mapped
                )
        if window.kind in {"trait_invocation", "bond_invocation"} or (
            window.kind == "skill_judgement"
            and str(window.payload.get("label") or "") == "幸运七"
        ):
            options.append(
                {
                    "action_type": ActionType.RESOLVE_DECISION.value,
                    "choice": "accept_result",
                    "label": "接受当前检定结果，不重掷",
                }
            )
        return options

    def get_gameplay_state(
        self,
        context: GMToolExecutionContext,
        _arguments: dict[str, object],
    ) -> GMToolReceipt:
        return GMToolReceipt(
            tool_name="get_gameplay_state",
            ok=True,
            result=self.state_summary(context),
        )

    def create_loyal_companion(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        tool_name = "create_loyal_companion"
        evidence_error = self._validate_evidence(
            context,
            arguments.get("evidence"),
            tool_name,
        )
        if evidence_error is not None:
            return evidence_error
        if context.gate_status not in {"session_zero", "adventure"}:
            return self._failure(
                tool_name,
                "COMPANION_CREATION_PHASE_INVALID",
                "忠诚伙伴只能在第零章建卡或实际冒险中完成创建。",
                "先进入第零章或载入正在冒险的战役。",
            )
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        owner_name = self._clean(arguments.get("owner"))
        if not owner_name or not app.character_manager.exists(owner_name):
            return self._failure(
                tool_name,
                "UNKNOWN_COMPANION_OWNER",
                f"没有找到伙伴主人【{owner_name or '未指定'}】。",
                "先确认玩家角色已经建卡，并使用其准确角色名。",
            )
        ownership_error = self._validate_actor_ownership(
            runtime,
            context,
            owner_name,
        )
        if ownership_error is not None:
            return ownership_error
        list_fields = {
            "traits": arguments.get("traits"),
            "attribute_order": arguments.get("attribute_order"),
            "selected_skills": arguments.get("selected_skills"),
            "attacks": arguments.get("attacks"),
        }
        invalid_lists = [
            name for name, value in list_fields.items() if not isinstance(value, list)
        ]
        if invalid_lists:
            return self._failure(
                tool_name,
                "COMPANION_ARRAY_REQUIRED",
                "忠诚伙伴字段必须是数组：" + "、".join(invalid_lists),
                "按工具schema提交完整数组，不要用逗号字符串代替。",
            )
        for key in ("skill_options", "spell_attributes", "profile"):
            value = arguments.get(key)
            if value not in (None, {}) and not isinstance(value, dict):
                return self._failure(
                    tool_name,
                    "COMPANION_OBJECT_REQUIRED",
                    f"忠诚伙伴字段【{key}】必须是对象。",
                    "按工具schema改成键值对象后重试。",
                )
        transaction_snapshot = CampaignStateTransaction.capture(
            app,
            context.campaign_id,
        )
        try:
            with runtime.transaction_lock:
                companion = app.loyal_companion_manager.create(
                    owner_name,
                    self._clean(arguments.get("name")),
                    species=self._clean(arguments.get("species")),
                    traits=[
                        self._clean(item)
                        for item in list(arguments.get("traits") or [])
                    ],
                    attribute_spread=self._clean(
                        arguments.get("attribute_spread")
                    ),
                    attribute_order=[
                        self._clean(item)
                        for item in list(arguments.get("attribute_order") or [])
                    ],
                    selected_skills=[
                        self._clean(item)
                        for item in list(arguments.get("selected_skills") or [])
                    ],
                    skill_options=dict(arguments.get("skill_options") or {}),
                    attacks=[
                        dict(item)
                        for item in list(arguments.get("attacks") or [])
                        if isinstance(item, dict)
                    ],
                    spell_attributes={
                        str(key): list(value)
                        for key, value in dict(
                            arguments.get("spell_attributes") or {}
                        ).items()
                        if isinstance(value, list)
                    },
                    profile=dict(arguments.get("profile") or {}),
                )
                app.world_state.record_memory_event(
                    f"{owner_name}与团友共同创建忠诚伙伴【{companion.name}】。",
                    kind="loyal_companion_created",
                    entities=[owner_name, companion.name],
                    tags=["skill", "wayfarer", "companion"],
                    source="GMGameplayToolService",
                    payload={
                        "owner": owner_name,
                        "companion": companion.name,
                    },
                )
                saved_path = self.host._autosave_campaign(
                    runtime,
                    context.campaign_id,
                )
        except Exception as exc:
            CampaignStateTransaction.restore(app, transaction_snapshot)
            return self._failure(
                tool_name,
                "LOYAL_COMPANION_COMMIT_FAILED",
                str(exc) or "忠诚伙伴没有成功写入存档。",
                "根据规则错误修正伙伴设计后重试；本次没有创建任何规则实体。",
            )
        return GMToolReceipt.success(
            tool_name,
            result={
                "owner": owner_name,
                "companion": app.loyal_companion_manager.public_state(
                    owner_name
                ),
                "saved_path": saved_path,
            },
            state_changed=True,
            public_reply=(
                f"【{companion.name}】的伙伴资料已经定下来了。"
            ),
        )

    def learn_chimerist_spell(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(
            context,
            arguments.get("evidence"),
            "learn_chimerist_spell",
        )
        if evidence_error is not None:
            return evidence_error
        if context.gate_status != "adventure":
            return self._failure(
                "learn_chimerist_spell",
                "ADVENTURE_NOT_ACTIVE",
                "只有实际冒险中目睹的法术才能通过【形意咒法】学习。",
                "角色创建时只记录技能与固定施法属性；进入冒险并亲眼目睹后再学习。",
            )
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        actor_name = self._clean(arguments.get("actor"))
        if not actor_name or not app.character_manager.exists(actor_name):
            return self._failure(
                "learn_chimerist_spell",
                "UNKNOWN_ACTOR",
                f"没有找到学习者【{actor_name or '未指定'}】。",
                "先调用get_gameplay_state并使用实际玩家角色名。",
            )
        ownership_error = self._validate_actor_ownership(
            runtime,
            context,
            actor_name,
        )
        if ownership_error is not None:
            return ownership_error
        actor = app.character_manager.get(actor_name)
        rank = skill_rank(actor.skills, "形意咒法")
        if rank <= 0:
            return self._failure(
                "learn_chimerist_spell",
                "CHIMERIST_SKILL_REQUIRED",
                f"【{actor_name}】没有技能【形意咒法】。",
                "不能把目睹法术直接写进角色卡。",
            )

        source_name = self._clean(arguments.get("source"))
        canonical_source = (
            source_name
            if app.character_manager.exists(source_name)
            else app.world_state.resolve_npc_name(source_name)
        )
        if (
            not canonical_source
            or not app.character_manager.exists(canonical_source)
        ):
            return self._failure(
                "learn_chimerist_spell",
                "SPELL_SOURCE_COMBATANT_REQUIRED",
                f"没有找到施法者【{source_name or '未指定'}】的规则档案。",
                "只有已经实际施放标准法术的规则生物才能作为模仿对象。",
            )
        scene = app.scene_manager.current_scene
        present = set(getattr(scene, "participants", []) or [])
        present.update(app.conflict_manager.state.turn_order)
        if canonical_source not in present:
            return self._failure(
                "learn_chimerist_spell",
                "SPELL_SOURCE_NOT_PRESENT",
                f"【{canonical_source}】不在当前场景，角色无法亲眼目睹其施法。",
                "使用当前场景中刚刚施放该法术的生物。",
            )
        source = app.character_manager.get(canonical_source)
        source_species = self._character_species(source)
        if source_species not in {"野兽", "怪物", "植物"}:
            return self._failure(
                "learn_chimerist_spell",
                "INVALID_CHIMERIST_SOURCE_SPECIES",
                f"【形意咒法】不能模仿物种为【{source_species or '未知'}】的生物。",
                "模仿对象必须是野兽、怪物或植物。",
            )

        spell_name = normalize_spell_name(
            self._clean(arguments.get("spell_name"))
        )
        if not spell_name or not is_known_spell(spell_name):
            return self._failure(
                "learn_chimerist_spell",
                "UNKNOWN_CHIMERIST_SPELL",
                f"没有找到标准法术【{spell_name or '未指定'}】。",
                "使用刚才规则结算中出现的标准法术名。",
            )
        source_spells = {
            normalize_spell_name(name)
            for name in source.spells
            if str(name).strip()
        }
        if spell_name not in source_spells:
            return self._failure(
                "learn_chimerist_spell",
                "SOURCE_DID_NOT_KNOW_SPELL",
                f"【{canonical_source}】的规则档案没有法术【{spell_name}】。",
                "不能根据叙事印象给生物补造法术；先修正其战斗档案或使用实际法术。",
            )

        learned = dict(actor.chimerist_spell_species)
        if spell_name in learned:
            return GMToolReceipt.success(
                "learn_chimerist_spell",
                result={
                    "actor": actor_name,
                    "spell_name": spell_name,
                    "source_species": learned[spell_name],
                    "already_known": True,
                    "capacity": rank + 2,
                },
            )
        capacity = rank + 2
        replace_spell = normalize_spell_name(
            self._clean(arguments.get("replace_spell"))
        )
        if len(learned) >= capacity:
            if not replace_spell:
                return self._failure(
                    "learn_chimerist_spell",
                    "CHIMERIST_REPLACEMENT_REQUIRED",
                    f"【{actor_name}】已经记忆了 {capacity} 个拟兽使法术。",
                    "由玩家从现有拟兽使法术中明确选择一个replace_spell后重试。",
                    result={"known_chimerist_spells": sorted(learned)},
                )
            if replace_spell not in learned:
                return self._failure(
                    "learn_chimerist_spell",
                    "INVALID_CHIMERIST_REPLACEMENT",
                    f"【{replace_spell}】不是【{actor_name}】当前记忆的拟兽使法术。",
                    "只能遗忘known_chimerist_spells中的一个法术。",
                    result={"known_chimerist_spells": sorted(learned)},
                )

        transaction_snapshot = CampaignStateTransaction.capture(
            app,
            context.campaign_id,
        )
        try:
            with runtime.transaction_lock:
                if replace_spell:
                    actor.chimerist_spell_species.pop(replace_spell, None)
                    if replace_spell in actor.spells:
                        actor.spells.remove(replace_spell)
                actor.chimerist_spell_species[spell_name] = source_species
                if spell_name not in actor.spells:
                    actor.spells.append(spell_name)
                app.world_state.record_memory_event(
                    (
                        f"{actor_name}目睹{canonical_source}施放【{spell_name}】，"
                        f"将其记忆为来源物种【{source_species}】的拟兽使法术。"
                    ),
                    kind="chimerist_spell_learned",
                    entities=[actor_name, canonical_source],
                    tags=["skill", "chimerist", "spell"],
                    source="GMGameplayToolService",
                    payload={
                        "actor": actor_name,
                        "source": canonical_source,
                        "source_species": source_species,
                        "spell_name": spell_name,
                        "replaced_spell": replace_spell,
                    },
                )
                saved_path = self.host._autosave_campaign(
                    runtime,
                    context.campaign_id,
                )
        except Exception as exc:
            CampaignStateTransaction.restore(app, transaction_snapshot)
            return self._failure(
                "learn_chimerist_spell",
                "CHIMERIST_SPELL_COMMIT_FAILED",
                str(exc) or "拟兽使法术没有成功写入存档。",
                "保持玩家原本的法术选择，修复存档后重试；本次没有学会或遗忘任何法术。",
            )
        return GMToolReceipt.success(
            "learn_chimerist_spell",
            result={
                "actor": actor_name,
                "spell_name": spell_name,
                "source": canonical_source,
                "source_species": source_species,
                "replaced_spell": replace_spell,
                "known_chimerist_spells": sorted(
                    actor.chimerist_spell_species
                ),
                "capacity": capacity,
                "saved_path": saved_path,
            },
            state_changed=True,
            public_reply=(
                f"【{actor_name}】记住了【{spell_name}】的施法方式。"
            ),
        )

    def recall_scene_memory(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        actor_name = self._clean(arguments.get("actor"))
        if not actor_name or not app.character_manager.exists(actor_name):
            return self._failure(
                "recall_scene_memory",
                "UNKNOWN_ACTOR",
                f"没有找到回忆者【{actor_name or '未指定'}】。",
                "先调用get_gameplay_state并使用实际玩家角色名。",
            )
        ownership_error = self._validate_actor_ownership(
            runtime,
            context,
            actor_name,
        )
        if ownership_error is not None:
            return ownership_error
        actor = app.character_manager.get(actor_name)
        if skill_rank(actor.skills, "记忆训练") <= 0:
            return self._failure(
                "recall_scene_memory",
                "MEMORY_TRAINING_REQUIRED",
                f"【{actor_name}】没有技能【记忆训练】。",
                "普通回忆可按公开记录回答；只有该技能能调用完整场景回放。",
            )
        eligible = [
            scene
            for scene in reversed(app.scene_manager.history)
            if actor_name in scene.participants
        ][:20]
        requested = self._clean(arguments.get("scene_name"))
        if not requested:
            return GMToolReceipt.success(
                "recall_scene_memory",
                result={
                    "actor": actor_name,
                    "available_scenes": [
                        {
                            "name": scene.name,
                            "location": scene.location,
                            "summary": scene.summary,
                        }
                        for scene in eligible
                    ],
                    "note": (
                        "这里只列出该角色实际参与过的已结束场景；"
                        "是否仍在故事内一周范围，由当前旅行与叙事时间判断。"
                    ),
                },
            )
        matched = next(
            (scene for scene in eligible if scene.name == requested),
            None,
        )
        if matched is None:
            partial = [
                scene for scene in eligible if requested in scene.name
            ]
            if len(partial) == 1:
                matched = partial[0]
        if matched is None:
            return self._failure(
                "recall_scene_memory",
                "SCENE_MEMORY_NOT_AVAILABLE",
                f"【{actor_name}】近期参与记录中没有场景【{requested}】。",
                "从available_scenes中选择准确场景；更早的内容由GM按战役日志判断。",
                result={
                    "available_scenes": [scene.name for scene in eligible]
                },
            )
        return GMToolReceipt.success(
            "recall_scene_memory",
            result={
                "actor": actor_name,
                "scene": {
                    "name": matched.name,
                    "scene_type": matched.scene_type.value,
                    "location": matched.location,
                    "participants": list(matched.participants),
                    "objective": matched.objective,
                    "summary": matched.summary,
                },
                "rules_followup": (
                    "角色可直接准确回忆这些公开内容；若要从记忆中重新调查或推导新答案，"
                    "使用perform_check_action，并允许【灵光洞见】照常触发。"
                ),
            },
        )

    def resolve_tavern_talk(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(
            context,
            arguments.get("evidence"),
            "resolve_tavern_talk",
        )
        if evidence_error is not None:
            return evidence_error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        actor_name = self._clean(arguments.get("actor"))
        if not actor_name or not app.character_manager.exists(actor_name):
            return self._failure(
                "resolve_tavern_talk",
                "UNKNOWN_ACTOR",
                f"没有找到提问者【{actor_name or '未指定'}】。",
                "先调用get_gameplay_state并使用实际玩家角色名。",
            )
        ownership_error = self._validate_actor_ownership(
            runtime,
            context,
            actor_name,
        )
        if ownership_error is not None:
            return ownership_error
        actor = app.character_manager.get(actor_name)
        rank = skill_rank(actor.skills, "酒馆攀谈")
        if rank <= 0:
            return self._failure(
                "resolve_tavern_talk",
                "TAVERN_TALK_REQUIRED",
                f"【{actor_name}】没有技能【酒馆攀谈】。",
                "普通交谈按当前NPC与场景处理，不能获得此技能的保证提问次数。",
            )
        if app.conflict_manager.state.active:
            return self._failure(
                "resolve_tavern_talk",
                "TAVERN_TALK_DURING_CONFLICT",
                "冲突中不能结算休息后的【酒馆攀谈】。",
                "先结束或脱离冲突。",
            )
        remaining = int(actor.skill_counters.get("酒馆攀谈", 0) or 0)
        if remaining <= 0:
            return self._failure(
                "resolve_tavern_talk",
                "NO_TAVERN_TALK_QUESTIONS",
                "当前没有尚未使用的【酒馆攀谈】提问次数。",
                "角色需要先在旅店或酒馆完成一次新的休息。",
            )
        question = self._clean_multiline(arguments.get("question"))
        answer = self._clean_multiline(arguments.get("public_answer"))
        if not question or not answer:
            return self._failure(
                "resolve_tavern_talk",
                "TAVERN_TALK_CONTENT_REQUIRED",
                "【酒馆攀谈】需要玩家的具体问题和GM的公开答复。",
                "问题应关于周边地区或本地居民；答复可以明确是传闻。",
            )
        transaction_snapshot = CampaignStateTransaction.capture(
            app,
            context.campaign_id,
        )
        try:
            with runtime.transaction_lock:
                actor.skill_counters["酒馆攀谈"] = remaining - 1
                app.world_state.record_memory_event(
                    f"{actor_name}在酒馆攀谈中问：「{question}」答复：「{answer}」",
                    kind="tavern_talk",
                    entities=[actor_name],
                    tags=["skill", "tavern", "local_information"],
                    source="GMGameplayToolService",
                    payload={
                        "actor": actor_name,
                        "question": question,
                        "public_answer": answer,
                        "remaining": remaining - 1,
                    },
                )
                saved_path = self.host._autosave_campaign(
                    runtime,
                    context.campaign_id,
                )
        except Exception as exc:
            CampaignStateTransaction.restore(app, transaction_snapshot)
            return self._failure(
                "resolve_tavern_talk",
                "TAVERN_TALK_COMMIT_FAILED",
                str(exc) or "酒馆攀谈没有成功写入存档。",
                "修复存档后以同一个问题重试；本次没有消耗提问次数。",
            )
        return GMToolReceipt.success(
            "resolve_tavern_talk",
            result={
                "actor": actor_name,
                "question": question,
                "remaining": remaining - 1,
                "maximum": rank,
                "saved_path": saved_path,
            },
            state_changed=True,
            public_reply=answer,
            lock_public_reply=True,
        )

    def perform_check_action(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        action_type, error = self._validated_action_type(arguments, self._CHECK_ACTIONS, "perform_check_action")
        if error is not None:
            return error
        attributes = arguments.get("attributes")
        if not isinstance(attributes, list) or len(attributes) != 2:
            return self._failure(
                "perform_check_action",
                "TWO_ATTRIBUTES_REQUIRED",
                "属性检定必须提交两项属性。",
                "从敏捷、洞察、力量、意志中选择两项；可以相同。",
            )
        normalized_attributes: list[str] = []
        for value in attributes:
            normalized = self._ATTRIBUTE_ALIASES.get(self._clean(value))
            if not normalized:
                return self._failure(
                    "perform_check_action",
                    "INVALID_ATTRIBUTE",
                    f"未知属性【{self._clean(value)}】。",
                    "使用中文属性：敏捷、洞察、力量、意志。",
                )
            normalized_attributes.append(normalized)
        try:
            difficulty = int(arguments.get("difficulty"))
        except (TypeError, ValueError):
            difficulty = 0
        if difficulty < 7:
            return self._failure(
                "perform_check_action",
                "INVALID_DIFFICULTY",
                "检定必须有GM裁定且不低于7的难度等级。",
                "根据当前局面重新选择难度等级，不能用命刻格数代替。",
            )
        details, detail_error = self._validated_details(
            arguments.get("details"),
            tool_name="perform_check_action",
            forbidden=self._FORBIDDEN_CHECK_CLOCK_KEYS,
        )
        if detail_error is not None:
            return detail_error
        actor = self._clean(arguments.get("actor"))
        runtime = self.host._runtime(context.campaign_id)
        dungeon_area = self._clean(details.get("dungeon_area"))
        if dungeon_area:
            dungeon = runtime.app.dungeon_manager.state
            if not dungeon.active:
                return self._failure(
                    "perform_check_action",
                    "DUNGEON_CHECK_WITHOUT_ACTIVE_DUNGEON",
                    "检定声明了地下城区域，但当前没有进行中的地下城。",
                    "删除dungeon_area，或先使用start_dungeon_exploration建立地下城。",
                )
            canonical_area = next(
                (
                    area.name
                    for area in dungeon.areas
                    if area.name == dungeon_area
                ),
                "",
            )
            if not canonical_area:
                return self._failure(
                    "perform_check_action",
                    "DUNGEON_CHECK_AREA_NOT_FOUND",
                    f"当前地下城没有区域【{dungeon_area}】。",
                    "调用get_dungeon_state并逐字使用现有区域名。",
                )
            details["dungeon_area"] = canonical_area
        condition_id = self._clean(arguments.get("condition_id"))
        if condition_id:
            frame = runtime.app.scene_frame_manager.current_frame
            condition = next(
                (
                    item
                    for item in list(getattr(frame, "open_conditions", []) or [])
                    if str(item.get("condition_id") or "").strip() == condition_id
                ),
                None,
            )
            if condition is None or str(condition.get("status") or "open") != "open":
                return self._failure(
                    "perform_check_action",
                    "SCENE_CONDITION_NOT_FOUND",
                    f"条件【{condition_id}】不存在或已经结束。",
                    "重新读取scene.open_conditions，并逐字使用仍开放的condition_id。",
                )
            if not runtime.app.scene_frame_manager.condition_is_available_to_actor(
                condition,
                actor,
            ):
                return self._failure(
                    "perform_check_action",
                    "SCENE_CONDITION_ACTOR_MISMATCH",
                    f"【{actor or '未指定角色'}】不能履行条件【{condition_id}】。",
                    "由required_actor对应的角色亲自行动；不要替其他玩家完成条件。",
                )
            if str(condition.get("player_fulfillment") or "pending") == "fulfilled":
                owner = self._clean(condition.get("npc"))
                return self._failure(
                    "perform_check_action",
                    "SCENE_CONDITION_ALREADY_FULFILLED",
                    f"条件【{condition_id}】的玩家义务已经完成。",
                    (
                        f"让【{owner}】兑现promised_result，不要再次检定。"
                        if owner
                        else "等待有权限的场景角色兑现promised_result。"
                    ),
                )
        target = self._clean(arguments.get("target")) or (
            "周边环境" if action_type in {ActionType.INVESTIGATE, ActionType.REQUEST_ROLL} else "当前目标"
        )
        purpose = self._clean(arguments.get("purpose"))
        check_label = self._clean(arguments.get("check_label"))
        success_observation = self._clean(arguments.get("success_observation"))
        failure_consequence = self._clean(arguments.get("failure_consequence"))
        missing_fields = [
            label
            for label, value in (
                ("purpose", purpose),
                ("check_label", check_label),
                ("success_observation", success_observation),
                ("failure_consequence", failure_consequence),
            )
            if not value
        ]
        if missing_fields:
            return self._failure(
                "perform_check_action",
                "CHECK_OUTCOME_CONTRACT_REQUIRED",
                "检定缺少完整裁定字段：" + "、".join(missing_fields) + "。",
                "先决定检定在问什么、成功具体看到什么、失败具体发生什么，再重新提交；失败后果不会提前公开。",
            )
        if action_type == ActionType.OBJECTIVE:
            requested_clock = self._clean(details.get("clock_name")) or target
            if not runtime.app.clock_manager.exists(requested_clock):
                return self._failure(
                    "perform_check_action",
                    "OBJECTIVE_CLOCK_NOT_FOUND",
                    f"当前没有名为【{requested_clock}】的活动命刻。",
                    "普通的一步式不确定行动使用RequestRoll；只有GM已明确建立复杂命刻后，才使用Objective并逐字提交现有clock_name。",
                    result={"requested_clock": requested_clock},
                )
            details["clock_name"] = requested_clock
        success_transition, transition_error = self._validated_check_success_transition(
            context,
            actor=actor,
            value=arguments.get("success_transition"),
        )
        if transition_error is not None:
            return transition_error
        parameters = {
            **details,
            "actor": actor,
            "target": target,
            "attributes": normalized_attributes,
            "target_number": difficulty,
            "reasoning": purpose,
            "declared_action_goal": purpose,
            "failure_consequence": failure_consequence,
            "success_observation": success_observation,
            "success_answer": success_observation,
            "scene_check_planned": True,
            "scene_check_interaction_kind": (
                "observe" if action_type == ActionType.INVESTIGATE else "manipulate"
            ),
            "scene_investigation_label": check_label,
            "non_damage": True,
        }
        if condition_id:
            parameters["scene_condition_id"] = condition_id
        if success_transition:
            destination = self._clean(success_transition.get("destination"))
            if not SceneTransitionCoordinator.public_reply_names_destination(
                success_observation,
                destination,
            ):
                return self._failure(
                    "perform_check_action",
                    "SUCCESS_TRANSITION_PUBLIC_DESTINATION_REQUIRED",
                    f"成功叙述没有明确写出角色将抵达【{destination}】。",
                    "保留success_transition，并在success_observation中自然写出该完整地点或末级地点名；不要等到重掷或接受结果时再补。",
                )
            parameters["success_transition"] = success_transition
        return self._execute(
            context,
            tool_name="perform_check_action",
            action=Action(action_type, parameters),
            evidence=arguments.get("evidence"),
        )

    def _validated_check_success_transition(
        self,
        context: GMToolExecutionContext,
        *,
        actor: str,
        value: object,
    ) -> tuple[dict[str, object], GMToolReceipt | None]:
        """Validate a movement outcome without deciding whether the check succeeds."""

        if value in (None, "", {}):
            return {}, None
        if not isinstance(value, dict):
            return {}, self._failure(
                "perform_check_action",
                "SUCCESS_TRANSITION_MUST_BE_OBJECT",
                "success_transition必须是JSON对象。",
                "填写destination、participants；没有成功转场时删除该字段。",
            )
        destination = self._clean(value.get("destination"))
        raw_participants = value.get("participants")
        if not destination or not isinstance(raw_participants, list):
            return {}, self._failure(
                "perform_check_action",
                "SUCCESS_TRANSITION_FIELDS_REQUIRED",
                "成功转场必须同时指定destination和participants数组。",
                "只提交检定成功时真正抵达的地点，以及本次实际移动的人物。",
            )
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        participants: list[str] = []
        for item in raw_participants:
            clean = self._clean(item)
            if not clean:
                continue
            canonical = app.world_state.resolve_npc_name(clean) or clean
            if canonical not in participants:
                participants.append(canonical)
        if actor not in participants:
            return {}, self._failure(
                "perform_check_action",
                "SUCCESS_TRANSITION_ACTOR_REQUIRED",
                f"成功转场必须包含行动者【{actor or '未指定'}】。",
                "把actor加入participants；不要用该字段替其他角色单独移动。",
            )

        known_pcs = {
            character.name
            for character in app.character_manager.all()
            if "pc" in character.traits
        }
        other_pcs = [name for name in participants if name in known_pcs and name != actor]
        if other_pcs:
            return {}, self._failure(
                "perform_check_action",
                "SUCCESS_TRANSITION_CANNOT_MOVE_OTHER_PCS",
                "不能替其他玩家角色提交成功转场：" + "、".join(other_pcs),
                "只保留当前行动者；其他玩家应由自己声明移动。",
            )

        absent_companions: list[str] = []
        for name in participants:
            if name == actor:
                continue
            if not app.world_state.resolve_npc_name(name):
                absent_companions.append(name)
                continue
            if not app.scene_manager.actors_share_movement_origin(actor, name):
                absent_companions.append(name)
        if absent_companions:
            return {}, self._failure(
                "perform_check_action",
                "SUCCESS_TRANSITION_COMPANION_NOT_COLOCATED",
                "以下随行者在行动前未与行动者同处一地：" + "、".join(absent_companions),
                "只提交当前确实在场的NPC；缺席人物必须先建立合法通讯或移动。",
            )

        return {
            "destination": destination,
            "participants": participants,
            "scene_name": self._clean(value.get("scene_name")),
            "objective": self._clean(value.get("objective")),
        }, None

    def perform_character_action(self, context: GMToolExecutionContext, arguments: dict[str, object]) -> GMToolReceipt:
        return self._execute_generic(context, arguments, self._CHARACTER_ACTIONS, "perform_character_action")

    def perform_scene_action(self, context: GMToolExecutionContext, arguments: dict[str, object]) -> GMToolReceipt:
        return self._execute_generic(context, arguments, self._SCENE_ACTIONS, "perform_scene_action")

    def perform_in_scene_action(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        """Commit one deterministic local action without abusing Narrate."""

        evidence_error = self._validate_evidence(
            context,
            arguments.get("evidence"),
            "perform_in_scene_action",
        )
        if evidence_error is not None:
            return evidence_error
        if context.gate_status != "adventure":
            return self._failure(
                "perform_in_scene_action",
                "ADVENTURE_NOT_ACTIVE",
                "当前还没有进入可结算跑团行动的阶段。",
                "第零章内容使用第零章工具；进入第一章后再提交场景行动。",
            )
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        scene = app.scene_manager.current_scene
        if scene is None:
            return self._failure(
                "perform_in_scene_action",
                "NO_ACTIVE_SCENE",
                "当前没有可承载这个行动的场景。",
                "先建立或恢复当前场景。",
            )
        if app.conflict_manager.state.active or scene.scene_type.value == "conflict":
            return self._failure(
                "perform_in_scene_action",
                "CONFLICT_ACTION_REQUIRED",
                "冲突中不能绕过回合和规则动作结算普通场景行动。",
                "使用perform_character_action、perform_check_action或当前合法冲突工具。",
            )
        actor = self._clean(arguments.get("actor"))
        if not actor or not app.character_manager.exists(actor):
            return self._failure(
                "perform_in_scene_action",
                "UNKNOWN_ACTOR",
                f"没有找到可结算角色【{actor or '未指定'}】。",
                "先调用get_gameplay_state并使用实际玩家角色名。",
            )
        ownership_error = self._validate_actor_ownership(runtime, context, actor)
        if ownership_error is not None:
            return ownership_error
        join_current_focus = bool(arguments.get("join_current_focus"))
        if actor not in scene.participants and not join_current_focus:
            known_location = app.scene_manager.location_of(actor)
            return self._failure(
                "perform_in_scene_action",
                "ACTOR_NOT_IN_FOCUSED_SCENE",
                f"【{actor}】不在当前镜头场景中。",
                (
                    f"该角色最后位置为【{known_location}】；先完成真实转场或切回其所在场景。"
                    if known_location
                    else "先完成真实转场或把镜头切回该角色所在场景。"
                ),
            )
        blocking = [
            window
            for window in app.interceptor.decision_window_manager.pending()
            if bool(window.blocking)
        ]
        if blocking:
            return self._failure(
                "perform_in_scene_action",
                "BLOCKING_DECISION_PENDING",
                "当前仍有必须先回答的规则选择。",
                "先使用resolve_rule_window处理待决窗口，再提交新的场景行动。",
            )
        action_summary = self._clean(arguments.get("action_summary"))
        position_note = self._clean(arguments.get("position_note"))
        if not action_summary:
            return self._failure(
                "perform_in_scene_action",
                "LOCAL_ACTION_RESULT_REQUIRED",
                "场景内行动必须包含动作摘要。",
                "忠实概括玩家角色已经明确执行的动作。",
            )
        if self._clean_multiline(arguments.get("public_result")) or arguments.get(
            "public_facts"
        ):
            return self._failure(
                "perform_in_scene_action",
                "LOCAL_ACTION_EXTERNAL_RESULT_NOT_ALLOWED",
                "普通场景内行动只能提交玩家角色自身动作与站位，不能同时写入外部结果或公开事实。",
                (
                    "删除public_result与public_facts；环境变化使用commit_scene_response，"
                    "NPC回应使用NPC工具，不确定结果使用perform_check_action。"
                ),
            )

        transaction_snapshot = CampaignStateTransaction.capture(app, context.campaign_id)
        try:
            with runtime.transaction_lock:
                if join_current_focus and actor not in scene.participants:
                    app.scene_manager.add_participant(
                        actor,
                        location=str(scene.location or scene.name or ""),
                    )
                frame = app.scene_frame_manager.current_frame
                if frame is None:
                    frame = app.scene_frame_manager.ensure_frame(
                        scene=scene,
                        recent_chat=str(context.metadata.get("recent_public_context") or ""),
                        world_state=app.world_state,
                        character_manager=app.character_manager,
                        contract=getattr(
                            getattr(app.story_arc_manager.state, "current_pacing_plan", None),
                            "dramatic_contract",
                            None,
                        ),
                    )
                app.scene_manager.record_participant_activity(actor, action_summary)
                if position_note:
                    app.scene_manager.set_participant_position(actor, position_note)
                action_round = app.record_free_scene_player_action(actor)
                clock_lines = app.turn_response_renderer.public_state_lines(action_round)
                reply = "\n".join(
                    line for line in clock_lines if str(line or "").strip()
                )
                saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        except Exception as exc:
            CampaignStateTransaction.restore(app, transaction_snapshot)
            return self._failure(
                "perform_in_scene_action",
                "LOCAL_ACTION_COMMIT_FAILED",
                str(exc) or "场景内行动未能提交。",
                "保持同一玩家动作，修正场景状态后重试。",
            )

        return GMToolReceipt(
            tool_name="perform_in_scene_action",
            ok=True,
            result={
                "actor": actor,
                "action_summary": action_summary,
                "position_note": position_note,
                "joined_current_focus": join_current_focus,
                "action_round": dict(action_round),
                "public_state_lines": list(clock_lines),
                # The player's own message already makes this purely local
                # movement public. When no clock or other state line is due,
                # the GM may record it without paraphrasing the player.
                "silent_commit_allowed": not bool(reply),
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply=reply,
            lock_public_reply=bool(reply),
            pacing_events=[
                GMToolPacingEvent(
                    player_action=True,
                    action_summary=str(context.metadata.get("current_message") or "").strip(),
                    consequence="",
                    public_image="",
                )
            ],
        )

    def commit_story_item_action(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        """Commit one already-resolved unique story-item state transition."""

        tool_name = "commit_story_item_action"
        evidence_error = self._validate_evidence(
            context,
            arguments.get("evidence"),
            tool_name,
        )
        if evidence_error is not None:
            return evidence_error
        if context.gate_status != "adventure":
            return self._failure(
                tool_name,
                "ADVENTURE_NOT_ACTIVE",
                "当前还没有进入可结算跑团行动的阶段。",
                "进入第一章后再提交剧情物件操作。",
            )

        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        scene = app.scene_manager.current_scene
        if scene is None:
            return self._failure(
                tool_name,
                "NO_ACTIVE_SCENE",
                "当前没有可承载剧情物件操作的场景。",
                "先建立或恢复当前场景。",
            )
        if app.conflict_manager.state.active or scene.scene_type.value == "conflict":
            return self._failure(
                tool_name,
                "CONFLICT_ACTION_REQUIRED",
                "冲突中不能绕过回合与规则行动直接改变剧情物件状态。",
                "先使用合法冲突行动完成取得、放置或破坏，再提交相应结果。",
            )

        actor = self._clean(arguments.get("actor"))
        if not actor or not app.character_manager.exists(actor):
            return self._failure(
                tool_name,
                "UNKNOWN_ACTOR",
                f"没有找到可结算角色【{actor or '未指定'}】。",
                "先读取游戏状态并使用实际玩家角色名。",
            )
        ownership_error = self._validate_actor_ownership(runtime, context, actor)
        if ownership_error is not None:
            return ownership_error
        if actor not in scene.participants:
            return self._failure(
                tool_name,
                "ACTOR_NOT_IN_FOCUSED_SCENE",
                f"【{actor}】不在当前聚焦场景中。",
                "先完成真实移动或切回该角色所在场景。",
            )
        if any(
            bool(window.blocking)
            for window in app.interceptor.decision_window_manager.pending()
        ):
            return self._failure(
                tool_name,
                "BLOCKING_DECISION_PENDING",
                "当前仍有必须先回答的规则选择。",
                "先处理待决窗口，再提交新的剧情物件行动。",
            )

        operation = self._clean(arguments.get("operation")).lower()
        item_name = self._clean(arguments.get("item_name"))
        item_id = self._clean(arguments.get("item_id"))
        description = self._clean(arguments.get("description"))
        to_holder = self._clean(arguments.get("to_holder"))
        to_location = self._clean(arguments.get("to_location"))
        public_result = self._clean_multiline(arguments.get("public_result"))
        public_fact = self._clean_multiline(arguments.get("public_fact"))
        continue_with_check = arguments.get("continue_with_check") is True
        if operation not in {"acquire", "transfer", "place", "destroy", "consume"}:
            return self._failure(
                tool_name,
                "INVALID_STORY_ITEM_OPERATION",
                f"不支持的剧情物件操作【{operation or '未指定'}】。",
                "从acquire、transfer、place、destroy、consume中选择。",
            )
        if not item_name or not public_result or not public_fact:
            return self._failure(
                tool_name,
                "STORY_ITEM_RESULT_REQUIRED",
                "剧情物件操作必须包含物件名、公开结果与持久事实。",
                "公开说明物件现在由谁持有、位于何处或已被怎样处理。",
            )
        if public_fact not in public_result:
            return self._failure(
                tool_name,
                "FACT_NOT_PUBLICLY_SPOKEN",
                f"事实「{public_fact[:80]}」没有逐字出现在公开结果中。",
                "保留原意，将public_fact逐字写入public_result后重试；不得省略这项持久状态。",
            )
        if operation == "transfer":
            if not to_holder:
                return self._failure(
                    tool_name,
                    "STORY_ITEM_RECIPIENT_REQUIRED",
                    "转交剧情物件时没有指定新持有者。",
                    "只有对方已经明确接受时才填写to_holder；否则先取得其回应。",
                )
            canonical_recipient = (
                to_holder
                if app.character_manager.exists(to_holder)
                else app.world_state.resolve_npc_name(to_holder)
            )
            known_recipient = bool(canonical_recipient)
            if not known_recipient:
                return self._failure(
                    tool_name,
                    "UNKNOWN_STORY_ITEM_RECIPIENT",
                    f"没有找到新持有者【{to_holder}】。",
                    "使用当前场景中的实际角色或NPC名称。",
                )
            if canonical_recipient not in scene.participants:
                return self._failure(
                    tool_name,
                    "STORY_ITEM_RECIPIENT_NOT_PRESENT",
                    f"新持有者【{to_holder}】不在当前聚焦场景中。",
                    "先让接收者真实到场并明确接受；不能隔空转交剧情物件。",
                )
            to_holder = canonical_recipient

        tags_value = arguments.get("tags") or []
        if not isinstance(tags_value, list):
            return self._failure(
                tool_name,
                "STORY_ITEM_TAGS_MUST_BE_ARRAY",
                "tags必须是字符串数组。",
                "没有标签时提交空数组。",
            )
        tags = list(
            dict.fromkeys(
                self._clean(item)
                for item in tags_value[:6]
                if self._clean(item)
            )
        )
        location = str(scene.location or scene.name or "").strip()
        evidence = self._clean(arguments.get("evidence"))
        transaction_snapshot = CampaignStateTransaction.capture(app, context.campaign_id)
        try:
            with runtime.transaction_lock:
                item = app.world_state.commit_story_item_action(
                    operation=operation,
                    item_name=item_name,
                    actor=actor,
                    scene_location=location,
                    public_fact=public_fact,
                    source=f"GM工具:{tool_name}:{evidence}",
                    item_id=item_id,
                    description=description,
                    to_holder=to_holder,
                    to_location=to_location,
                    tags=tags,
                )
                if app.scene_frame_manager.current_frame is None:
                    app.scene_frame_manager.ensure_frame(
                        scene=scene,
                        recent_chat=str(context.metadata.get("recent_public_context") or ""),
                        world_state=app.world_state,
                        character_manager=app.character_manager,
                        contract=getattr(
                            getattr(app.story_arc_manager.state, "current_pacing_plan", None),
                            "dramatic_contract",
                            None,
                        ),
                    )
                app.scene_frame_manager.record_public_fact(public_fact)
                app.scene_frame_manager.record_gm_beat(public_result)
                action_labels = {
                    "acquire": "取得",
                    "transfer": "转交",
                    "place": "放置",
                    "destroy": "销毁",
                    "consume": "消耗",
                }
                app.scene_manager.record_participant_activity(
                    actor,
                    f"{action_labels[operation]}剧情物件【{item.name}】",
                )
                action_round = (
                    {}
                    if continue_with_check
                    else app.record_free_scene_player_action(actor)
                )
                clock_lines = app.turn_response_renderer.public_state_lines(action_round)
                reply = "\n".join([public_result, *clock_lines]) if clock_lines else public_result
                saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        except Exception as exc:
            CampaignStateTransaction.restore(app, transaction_snapshot)
            return self._failure(
                tool_name,
                "STORY_ITEM_COMMIT_FAILED",
                str(exc) or "剧情物件状态未能提交。",
                "读取story_items中的当前持有者、地点与状态后修正参数；不要用普通叙事绕过。",
            )

        followup_result = (
            {
                "allowed_followup_tools": ["perform_check_action"],
                "required_followup_tools": ["perform_check_action"],
            }
            if continue_with_check
            else {}
        )
        return GMToolReceipt(
            tool_name=tool_name,
            ok=True,
            result={
                "story_item": {
                    "item_id": item.item_id,
                    "name": item.name,
                    "holder": item.holder,
                    "location": item.location,
                    "status": item.status.value,
                },
                "public_fact": public_fact,
                "action_round": dict(action_round),
                **followup_result,
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply=reply,
            lock_public_reply=True,
            pacing_events=[
                GMToolPacingEvent(
                    player_action=True,
                    action_summary=str(context.metadata.get("current_message") or "").strip(),
                    consequence=public_fact,
                    public_image=self._first_sentence(public_result),
                )
            ],
        )

    def move_group_within_scene(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        """Move a PC and consenting NPCs without splitting the current scene."""

        tool_name = "move_group_within_scene"
        evidence_error = self._validate_evidence(
            context,
            arguments.get("evidence"),
            tool_name,
        )
        if evidence_error is not None:
            return evidence_error
        if context.gate_status != "adventure":
            return self._failure(
                tool_name,
                "ADVENTURE_NOT_ACTIVE",
                "当前还没有进入可结算同行移动的阶段。",
                "第零章内容使用第零章工具；进入第一章后再提交移动。",
            )
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        scene = app.scene_manager.current_scene
        if scene is None:
            return self._failure(
                tool_name,
                "NO_ACTIVE_SCENE",
                "当前没有可承载这次移动的场景。",
                "先建立或恢复当前场景。",
            )
        if app.conflict_manager.state.active or scene.scene_type.value == "conflict":
            return self._failure(
                tool_name,
                "CONFLICT_ACTION_REQUIRED",
                "冲突中不能用普通场景移动绕过回合与规则行动。",
                "使用当前合法冲突行动结算移动。",
            )
        actor = self._clean(arguments.get("actor"))
        if not actor or not app.character_manager.exists(actor):
            return self._failure(
                tool_name,
                "UNKNOWN_ACTOR",
                f"没有找到可结算角色【{actor or '未指定'}】。",
                "先调用get_gameplay_state并使用实际玩家角色名。",
            )
        ownership_error = self._validate_actor_ownership(runtime, context, actor)
        if ownership_error is not None:
            return ownership_error
        if actor not in scene.participants:
            return self._failure(
                tool_name,
                "ACTOR_NOT_IN_CURRENT_SCENE",
                f"【{actor}】不在当前聚焦场景，不能提交同场景站位移动。",
                "先切回角色真实所在镜头；跨场景移动使用对应转场工具。",
            )
        blocking = [
            window
            for window in app.interceptor.decision_window_manager.pending()
            if bool(window.blocking)
        ]
        if blocking:
            return self._failure(
                tool_name,
                "BLOCKING_DECISION_PENDING",
                "当前仍有必须先回答的规则选择。",
                "先使用resolve_rule_window处理待决窗口，再提交移动。",
            )

        companions_value = arguments.get("companions")
        if not isinstance(companions_value, list):
            return self._failure(
                tool_name,
                "COMPANIONS_MUST_BE_ARRAY",
                "同行NPC必须使用名称数组提交。",
                "把本次实际同行的具名NPC放入companions。",
            )
        companions: list[str] = []
        for item in companions_value:
            requested = self._clean(item)
            canonical = app.world_state.resolve_npc_name(requested)
            if not canonical:
                return self._failure(
                    tool_name,
                    "UNKNOWN_COMPANION",
                    f"没有找到同行NPC【{requested or '未指定'}】。",
                    "先读取NPC档案；人物尚未登场时不能把他作为同行者移动。",
                )
            if canonical not in companions:
                companions.append(canonical)
        if not companions:
            return self._failure(
                tool_name,
                "COMPANION_REQUIRED",
                "这个工具至少需要一名已同意同行的NPC。",
                "只有玩家角色自身移动时使用perform_in_scene_action。",
            )
        for companion in companions:
            if companion not in scene.participants:
                return self._failure(
                    tool_name,
                    "COMPANION_NOT_IN_CURRENT_SCENE",
                    f"【{companion}】不在当前聚焦场景。",
                    "不要跨场景更新站位；先切回双方实际同处的镜头。",
                )
            if not app.scene_manager.actors_share_movement_origin(actor, companion):
                return self._failure(
                    tool_name,
                    "COMPANION_NOT_AT_ORIGIN",
                    f"【{actor}】与【{companion}】不在同一个有效来源场景。",
                    "先完成会合，再提交同场景结伴移动。",
                )

        destination_position = self._clean(arguments.get("destination_position"))
        action_summary = self._clean(arguments.get("action_summary"))
        public_result = self._clean_multiline(arguments.get("public_result"))
        if not destination_position or not action_summary:
            return self._failure(
                tool_name,
                "LOCAL_MOVEMENT_FIELDS_REQUIRED",
                "同场景结伴移动必须包含目标站位与动作摘要。",
                "填写当前场景内的简短静态站位，并忠实概括玩家动作。",
            )

        frame = app.scene_frame_manager.current_frame
        condition_id = self._clean(arguments.get("condition_id"))
        condition: dict[str, str] | None = None
        if condition_id:
            condition = next(
                (
                    item
                    for item in list(getattr(frame, "open_conditions", []) or [])
                    if str(item.get("condition_id") or "").strip() == condition_id
                ),
                None,
            )
            if condition is None or str(condition.get("status") or "open") != "open":
                return self._failure(
                    tool_name,
                    "SCENE_CONDITION_NOT_FOUND",
                    f"条件【{condition_id}】不存在或已经结束。",
                    "重新读取scene.open_conditions；不要把移动绑定到过期条件。",
                )
            if not app.scene_frame_manager.condition_is_available_to_actor(
                condition,
                actor,
            ):
                return self._failure(
                    tool_name,
                    "SCENE_CONDITION_ACTOR_MISMATCH",
                    f"【{actor}】不能替指定角色履行条件【{condition_id}】。",
                    "由required_actor对应的玩家角色亲自完成；不要代替其他玩家行动。",
                )
            if str(condition.get("player_fulfillment") or "pending") == "fulfilled":
                owner = self._clean(condition.get("npc"))
                return self._failure(
                    tool_name,
                    "SCENE_CONDITION_ALREADY_FULFILLED",
                    f"条件【{condition_id}】的玩家义务已经完成，不能重复提交移动。",
                    (
                        f"让【{owner}】兑现promised_result。"
                        if owner
                        else "等待有权限的NPC兑现promised_result。"
                    ),
                )

        commitment, commitment_responder, commitment_error = (
            self._validated_triggered_commitment(
                app,
                frame=frame,
                companions=companions,
                arguments=arguments,
                tool_name=tool_name,
            )
        )
        if commitment_error is not None:
            return commitment_error

        required_followup_tools: list[str] = []
        required_followup_calls: list[dict[str, object]] = []
        condition_owner = ""
        if condition is not None and self._clean(condition.get("promised_result")):
            requested_owner = self._clean(condition.get("npc"))
            condition_owner = app.world_state.resolve_npc_name(requested_owner) or requested_owner
            persona = app.world_state.npc_personas.get(condition_owner)
            if (
                condition_owner
                and condition_owner in scene.participants
                and persona is not None
                and str(getattr(persona, "entity_kind", "individual") or "individual")
                == "individual"
            ):
                required_followup_tools.append("decide_npc_response")
                required_followup_calls.append(
                    {
                        "tool_name": "decide_npc_response",
                        "arguments": {
                            "name": condition_owner,
                            "actor": actor,
                            "condition_id": condition_id,
                        },
                        "authority_reason": (
                            "本次移动完成了公开条件中的玩家义务，"
                            "现在由条件所有者决定并兑现promised_result。"
                        ),
                    }
                )
        if commitment is not None:
            required_followup_tools.append("decide_npc_response")
            commitment_id = self._clean(commitment.get("commitment_id"))
            existing_followup = next(
                (
                    item
                    for item in required_followup_calls
                    if self._clean(item.get("tool_name")) == "decide_npc_response"
                    and self._clean(
                        dict(item.get("arguments") or {}).get("name")
                    )
                    == commitment_responder
                ),
                None,
            )
            if existing_followup is not None:
                existing_followup["arguments"]["commitment_id"] = commitment_id
                existing_followup["authority_reason"] = (
                    f"{existing_followup.get('authority_reason', '')}"
                    " 本次移动也抵达了该NPC短期承诺的触发点，须同时完成promised_result。"
                ).strip()
            else:
                required_followup_calls.append(
                    {
                        "tool_name": "decide_npc_response",
                        "arguments": {
                            "name": commitment_responder,
                            "actor": actor,
                            "commitment_id": commitment_id,
                        },
                        "authority_reason": (
                            "本次移动抵达了NPC短期承诺的公开触发点，"
                            "现在由随行兑现者当场完成promised_result。"
                        ),
                    }
                )
        required_followup_tools = list(dict.fromkeys(required_followup_tools))

        transaction_snapshot = CampaignStateTransaction.capture(app, context.campaign_id)
        try:
            with runtime.transaction_lock:
                triggered_commitment = None
                if commitment is not None:
                    triggered_commitment = (
                        app.scene_frame_manager.npc_deferred_commitment_manager.mark_trigger_reached(
                            frame,
                            commitment_id=self._clean(
                                commitment.get("commitment_id")
                            ),
                            actor=actor,
                            evidence=str(
                                context.metadata.get("current_message")
                                or action_summary
                            ),
                            location=destination_position,
                            responder=commitment_responder,
                        )
                    )
                    if triggered_commitment is None:
                        raise RuntimeError(
                            "NPC短期承诺未能提交触发状态；当前移动没有被部分写入。"
                        )
                    app.scene_frame_manager.touch_current_state()
                app.scene_manager.set_participant_position(actor, destination_position)
                for companion in companions:
                    app.scene_manager.set_participant_position(
                        companion,
                        destination_position,
                    )
                    app.world_state.update_npc_state(
                        companion,
                        location=str(scene.location or scene.name or ""),
                        scene=str(scene.scene_id or ""),
                    )
                app.scene_manager.record_participant_activity(actor, action_summary)
                if public_result:
                    app.scene_frame_manager.record_gm_beat(public_result)
                fulfilled_condition = None
                if condition_id:
                    fulfilled_condition = app.scene_frame_manager.mark_condition_fulfilled(
                        condition_id,
                        scene=scene,
                        actor=actor,
                        public_evidence=str(
                            context.metadata.get("current_message") or action_summary
                        ),
                    )
                    if fulfilled_condition is None:
                        raise RuntimeError(
                            f"条件【{condition_id}】未能提交玩家履约状态。"
                        )
                action_round = app.record_free_scene_player_action(actor)
                clock_lines = app.turn_response_renderer.public_state_lines(action_round)
                reply = "\n".join(
                    part for part in (public_result, *clock_lines) if str(part).strip()
                )
                saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        except Exception as exc:
            CampaignStateTransaction.restore(app, transaction_snapshot)
            return self._failure(
                tool_name,
                "LOCAL_GROUP_MOVEMENT_COMMIT_FAILED",
                str(exc) or "同场景结伴移动未能提交。",
                "保持同一玩家动作，修正场景状态后重试。",
            )

        return GMToolReceipt(
            tool_name=tool_name,
            ok=True,
            result={
                "actor": actor,
                "companions": companions,
                "destination_position": destination_position,
                "scene_id": str(scene.scene_id or ""),
                "fulfilled_condition": dict(fulfilled_condition or {}),
                "condition_payoff_due_from": condition_owner,
                "triggered_commitment": dict(triggered_commitment or {}),
                "commitment_payoff_due_from": commitment_responder,
                "action_round": dict(action_round),
                "public_state_lines": list(clock_lines),
                "allowed_followup_tools": list(required_followup_tools),
                "required_followup_tools": list(required_followup_tools),
                "required_followup_calls": list(required_followup_calls),
                "required_followup_mode": required_followup_mode(
                    required_followup_calls
                ),
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply=reply,
            lock_public_reply=bool(reply),
            pacing_events=[
                GMToolPacingEvent(
                    player_action=True,
                    action_summary=str(context.metadata.get("current_message") or "").strip(),
                    public_image=self._first_sentence(public_result),
                )
            ],
        )

    def move_scene_group(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        """Commit resolved cross-scene movement for one PC and optional NPCs."""

        evidence_error = self._validate_evidence(
            context,
            arguments.get("evidence"),
            "move_scene_group",
        )
        if evidence_error is not None:
            return evidence_error
        if context.gate_status != "adventure":
            return self._failure(
                "move_scene_group",
                "ADVENTURE_NOT_ACTIVE",
                "当前还没有进入可结算同行移动的阶段。",
                "第零章内容使用第零章工具；进入第一章后再提交移动。",
            )
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        scene = app.scene_manager.current_scene
        if scene is None:
            return self._failure(
                "move_scene_group",
                "NO_ACTIVE_SCENE",
                "当前没有可承载这次移动的场景。",
                "先建立或恢复当前场景。",
            )
        if app.conflict_manager.state.active or scene.scene_type.value == "conflict":
            return self._failure(
                "move_scene_group",
                "CONFLICT_ACTION_REQUIRED",
                "冲突中不能用普通场景移动绕过回合与规则行动。",
                "使用当前合法冲突行动结算移动。",
            )
        actor = self._clean(arguments.get("actor"))
        if not actor or not app.character_manager.exists(actor):
            return self._failure(
                "move_scene_group",
                "UNKNOWN_ACTOR",
                f"没有找到可结算角色【{actor or '未指定'}】。",
                "先调用get_gameplay_state并使用实际玩家角色名。",
            )
        ownership_error = self._validate_actor_ownership(runtime, context, actor)
        if ownership_error is not None:
            return ownership_error
        blocking = [
            window
            for window in app.interceptor.decision_window_manager.pending()
            if bool(window.blocking)
        ]
        if blocking:
            return self._failure(
                "move_scene_group",
                "BLOCKING_DECISION_PENDING",
                "当前仍有必须先回答的规则选择。",
                "先使用resolve_rule_window处理待决窗口，再提交移动。",
            )

        companions_value = arguments.get("companions")
        if not isinstance(companions_value, list):
            return self._failure(
                "move_scene_group",
                "COMPANIONS_MUST_BE_ARRAY",
                "同行NPC必须使用名称数组提交；只有行动者本人移动时也要提交空数组。",
                "把本次实际同行的具名NPC放入companions，或在没有同行NPC时提交[]。",
            )
        companions: list[str] = []
        for item in companions_value:
            requested = self._clean(item)
            canonical = app.world_state.resolve_npc_name(requested)
            if not canonical:
                return self._failure(
                    "move_scene_group",
                    "UNKNOWN_COMPANION",
                    f"没有找到同行NPC【{requested or '未指定'}】。",
                    "先读取NPC档案；人物尚未登场时不能把他作为同行者移动。",
                )
            if canonical not in companions:
                companions.append(canonical)
        for companion in companions:
            if not app.scene_manager.actors_share_movement_origin(actor, companion):
                return self._failure(
                    "move_scene_group",
                    "COMPANION_NOT_AT_ORIGIN",
                    f"【{actor}】与【{companion}】不在同一个有效来源场景。",
                    "不要让人物跨越场景边界同行；先切回其真实所在场景或完成会合。",
                )

        destination = self._clean(arguments.get("destination"))
        action_summary = self._clean(arguments.get("action_summary"))
        public_result = self._clean_multiline(arguments.get("public_result"))
        position_note = self._clean(arguments.get("position_note"))
        if not destination or not action_summary or not public_result:
            return self._failure(
                "move_scene_group",
                "MOVEMENT_RESULT_REQUIRED",
                "跨场景移动必须包含目的地、动作摘要与即时公开结果。",
                "明确写出行动者及实际同行者抵达了哪里。",
            )
        followup_npc_requested = self._clean(arguments.get("followup_npc_name"))
        followup_instruction = self._clean(
            arguments.get("followup_response_instruction")
        )
        followup_npc = ""
        if bool(followup_npc_requested) != bool(followup_instruction):
            return self._failure(
                "move_scene_group",
                "NPC_FOLLOWUP_INCOMPLETE",
                "抵达后的NPC交互必须同时指定人物与待回应事项。",
                "同时填写followup_npc_name和followup_response_instruction，或同时删除二者。",
            )
        if followup_npc_requested:
            followup_npc = app.world_state.resolve_npc_name(
                followup_npc_requested
            )
            if not followup_npc:
                return self._failure(
                    "move_scene_group",
                    "NPC_PROFILE_REQUIRED",
                    f"没有找到目的地NPC【{followup_npc_requested}】的档案。",
                    "人物已经实际登场时先建立档案；不要为假设人物创建抵达后答复。",
                )
            npc_location = app.scene_manager.location_of(followup_npc)
            if npc_location != destination:
                return self._failure(
                    "move_scene_group",
                    "NPC_NOT_AT_DESTINATION",
                    f"【{followup_npc}】的权威位置不是【{destination}】。",
                    "使用NPC当前实际位置作为destination，或删除抵达后的NPC答复契约。",
                )
        if destination not in public_result:
            return self._failure(
                "move_scene_group",
                "DESTINATION_NOT_PUBLIC",
                f"公开结果没有明确写出目的地【{destination}】。",
                "在public_result中明确说明行动者及实际同行者已抵达该地点。",
            )
        facts_value = arguments.get("public_facts")
        if facts_value is None:
            facts_value = []
        if not isinstance(facts_value, list):
            return self._failure(
                "move_scene_group",
                "PUBLIC_FACTS_MUST_BE_ARRAY",
                "public_facts必须是字符串数组。",
                "没有需要持续索引的事实时提交空数组。",
            )
        public_facts: list[str] = []
        for item in facts_value[:8]:
            fact = self._clean_multiline(item)
            if not fact:
                continue
            if fact not in public_result:
                return self._failure(
                    "move_scene_group",
                    "FACT_NOT_PUBLICLY_SPOKEN",
                    f"事实「{fact[:80]}」没有逐字出现在公开结果中。",
                    "删除该事实，或让它逐字出现在public_result中。",
                )
            if fact not in public_facts:
                public_facts.append(fact)
        companion_positions = arguments.get("companion_positions")
        if companion_positions is None:
            companion_positions = {}
        if not isinstance(companion_positions, dict):
            return self._failure(
                "move_scene_group",
                "COMPANION_POSITIONS_MUST_BE_OBJECT",
                "companion_positions必须是以NPC名称为键的对象。",
                "没有具体站位时提交空对象。",
            )

        source_frame = app.scene_frame_manager.current_frame
        commitment, commitment_responder, commitment_error = (
            self._validated_triggered_commitment(
                app,
                frame=source_frame,
                companions=companions,
                arguments=arguments,
                tool_name="move_scene_group",
            )
        )
        if commitment_error is not None:
            return commitment_error
        required_followup_tools: list[str] = []
        required_followup_calls: list[dict[str, object]] = []
        commitment_id = ""
        if followup_npc:
            required_followup_tools = ["decide_npc_response"]
            required_followup_calls = [
                {
                    "tool_name": "decide_npc_response",
                    "arguments": {
                        "name": followup_npc,
                        "actor": actor,
                        "response_instruction": followup_instruction,
                    },
                    "authority_reason": (
                        "玩家本句已经明确抵达目的地并立即向当地NPC提出问题或请求；"
                        "移动只是准备步骤，NPC答复才完成这条消息。"
                    ),
                }
            ]
        if commitment is not None:
            commitment_id = self._clean(commitment.get("commitment_id"))
            commitment_followup = [
                {
                    "tool_name": "decide_npc_response",
                    "arguments": {
                        "name": commitment_responder,
                        "actor": actor,
                        "commitment_id": commitment_id,
                    },
                    "authority_reason": (
                        "本次同行移动已经抵达NPC短期承诺的公开触发地点，"
                        "随行兑现者必须在目的地当场完成promised_result。"
                    ),
                }
            ]
            if followup_npc and followup_npc != commitment_responder:
                return self._failure(
                    "move_scene_group",
                    "MULTIPLE_NPC_FOLLOWUPS",
                    "同一次跨场景移动不能同时要求两个不同NPC立即回应。",
                    "只保留玩家本句真正要求完成的一个NPC回应。",
                )
            required_followup_tools = ["decide_npc_response"]
            if required_followup_calls:
                required_followup_calls[0]["arguments"]["commitment_id"] = (
                    commitment_id
                )
            else:
                required_followup_calls = commitment_followup

        transaction_snapshot = CampaignStateTransaction.capture(app, context.campaign_id)
        try:
            with runtime.transaction_lock:
                triggered_commitment = None
                if commitment is not None:
                    triggered_commitment = (
                        app.scene_frame_manager.npc_deferred_commitment_manager.mark_trigger_reached(
                            source_frame,
                            commitment_id=commitment_id,
                            actor=actor,
                            evidence=str(
                                context.metadata.get("current_message")
                                or action_summary
                            ),
                            location=destination,
                            responder=commitment_responder,
                        )
                    )
                    if triggered_commitment is None:
                        raise RuntimeError(
                            "NPC短期承诺未能提交触发状态；当前移动没有被部分写入。"
                        )
                    app.scene_frame_manager.touch_current_state()
                moved_scene, movement_mode = app.scene_manager.move_participants_to_location(
                    [actor, *companions],
                    destination,
                )
                app.scene_frame_manager.synchronize_current_location(destination)
                if app.scene_frame_manager.current_frame is None:
                    app.scene_frame_manager.ensure_frame(
                        scene=moved_scene,
                        recent_chat=str(context.metadata.get("recent_public_context") or ""),
                        world_state=app.world_state,
                        character_manager=app.character_manager,
                        contract=getattr(
                            getattr(app.story_arc_manager.state, "current_pacing_plan", None),
                            "dramatic_contract",
                            None,
                        ),
                    )
                if commitment_id:
                    current_commitment = (
                        app.scene_frame_manager.npc_deferred_commitment_manager.find_pending(
                            app.scene_frame_manager.current_frame,
                            commitment_id,
                        )
                    )
                    if current_commitment is None or self._clean(
                        current_commitment.get("trigger_status")
                    ).lower() != "reached":
                        raise RuntimeError(
                            "抵达新场景后没有继承已触发的NPC短期承诺。"
                        )
                    triggered_commitment = current_commitment
                for companion in companions:
                    app.world_state.update_npc_state(
                        companion,
                        location=destination,
                        scene=str(moved_scene.scene_id or ""),
                    )
                for fact in public_facts:
                    app.scene_frame_manager.record_public_fact(fact)
                app.scene_frame_manager.record_gm_beat(public_result)
                app.scene_manager.record_participant_activity(actor, action_summary)
                if position_note:
                    app.scene_manager.set_participant_position(actor, position_note)
                for name, note in companion_positions.items():
                    canonical = app.world_state.resolve_npc_name(self._clean(name))
                    clean_note = self._clean(note)
                    if canonical in companions and clean_note:
                        app.scene_manager.set_participant_position(canonical, clean_note)
                action_round = app.record_free_scene_player_action(actor)
                clock_lines = app.turn_response_renderer.public_state_lines(action_round)
                reply = "\n".join([public_result, *clock_lines]) if clock_lines else public_result
                saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        except Exception as exc:
            CampaignStateTransaction.restore(app, transaction_snapshot)
            return self._failure(
                "move_scene_group",
                "SCENE_MOVEMENT_COMMIT_FAILED",
                str(exc) or "同行移动未能提交。",
                "保持同一玩家动作，修正场景状态后重试。",
            )

        return GMToolReceipt(
            tool_name="move_scene_group",
            ok=True,
            result={
                "actor": actor,
                "companions": companions,
                "destination": destination,
                "movement_mode": movement_mode,
                "scene_id": str(moved_scene.scene_id or ""),
                "action_round": dict(action_round),
                "public_facts": public_facts,
                "triggered_commitment": dict(triggered_commitment or {}),
                "commitment_payoff_due_from": commitment_responder,
                "allowed_followup_tools": list(required_followup_tools),
                "required_followup_tools": list(required_followup_tools),
                "required_followup_calls": list(required_followup_calls),
                "required_followup_mode": required_followup_mode(
                    required_followup_calls
                ),
                "public_state_lines": list(clock_lines),
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply=reply,
            lock_public_reply=True,
            pacing_events=[
                GMToolPacingEvent(
                    player_action=True,
                    action_summary=str(context.metadata.get("current_message") or "").strip(),
                    consequence=public_facts[0] if public_facts else "",
                    public_image=self._first_sentence(public_result),
                )
            ],
        )

    def pass_in_scene_action(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        """Record an explicit pass without inventing an in-fiction action."""

        tool_name = "pass_in_scene_action"
        evidence_error = self._validate_evidence(
            context,
            arguments.get("evidence"),
            tool_name,
        )
        if evidence_error is not None:
            return evidence_error
        if context.gate_status != "adventure":
            return self._failure(
                tool_name,
                "ADVENTURE_NOT_ACTIVE",
                "当前还没有进入可结算行动轮的跑团阶段。",
                "第零章或开团前的等待直接静默，不要写入场景行动轮。",
            )
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        scene = app.scene_manager.current_scene
        if scene is None:
            return self._failure(
                tool_name,
                "NO_ACTIVE_SCENE",
                "当前没有活动场景可记录本轮略过。",
                "没有当前场景时直接静默；不要虚构行动轮。",
            )
        if app.conflict_manager.state.active or scene.scene_type.value == "conflict":
            return self._failure(
                tool_name,
                "CONFLICT_TURN_REQUIRED",
                "冲突场景不能用普通场景略过工具跳过回合。",
                "使用冲突回合中合法的规则行动或专用回合处理。",
            )
        actor = self._clean(arguments.get("actor"))
        if not actor or not app.character_manager.exists(actor):
            return self._failure(
                tool_name,
                "UNKNOWN_ACTOR",
                f"没有找到可记录角色【{actor or '未指定'}】。",
                "先调用get_gameplay_state并使用当前玩家实际控制的角色名。",
            )
        ownership_error = self._validate_actor_ownership(runtime, context, actor)
        if ownership_error is not None:
            return ownership_error
        if actor not in scene.participants:
            return self._failure(
                tool_name,
                "ACTOR_NOT_IN_FOCUSED_SCENE",
                f"【{actor}】不在当前镜头场景中，不能占用这里的行动轮。",
                "切回该角色实际所在场景；不要把缺席角色算作本轮已行动。",
            )
        blocking = [
            window
            for window in app.interceptor.decision_window_manager.pending()
            if bool(window.blocking)
        ]
        if blocking:
            return self._failure(
                tool_name,
                "BLOCKING_DECISION_PENDING",
                "当前仍有必须先回答的规则选择。",
                "先使用resolve_rule_window处理待决窗口，再决定是否略过行动。",
            )

        transaction_snapshot = CampaignStateTransaction.capture(app, context.campaign_id)
        try:
            with runtime.transaction_lock:
                action_round = app.record_free_scene_player_action(actor)
                if action_round:
                    saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
                else:
                    saved_path = ""
        except Exception as exc:
            CampaignStateTransaction.restore(app, transaction_snapshot)
            return self._failure(
                tool_name,
                "SCENE_PASS_COMMIT_FAILED",
                str(exc) or "本轮略过未能提交。",
                "保持玩家原意，恢复场景行动轮状态后重试。",
            )

        if not action_round:
            return GMToolReceipt(
                tool_name=tool_name,
                ok=True,
                result={
                    "actor": actor,
                    "recorded": False,
                    "reason": "当前没有需要完整行动轮推进的活动命刻。",
                },
                state_changed=False,
            )

        clock_changed = bool(action_round.get("auto_clock_changes"))
        clock_lines = (
            app.turn_response_renderer.public_state_lines(action_round)
            if clock_changed
            else []
        )
        reply = "\n".join(clock_lines)
        return GMToolReceipt(
            tool_name=tool_name,
            ok=True,
            result={
                "actor": actor,
                "recorded": True,
                "action_round": dict(action_round),
                # A pass before the end of the shared action round must still
                # persist even though a human GM would not echo it. The final
                # pass that advances a clock remains publicly locked below.
                "silent_commit_allowed": not bool(reply),
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply=reply,
            lock_public_reply=bool(reply),
        )

    def perform_ritual_project_action(self, context: GMToolExecutionContext, arguments: dict[str, object]) -> GMToolReceipt:
        return self._execute_generic(context, arguments, self._RITUAL_PROJECT_ACTIONS, "perform_ritual_project_action")

    def resolve_rule_window(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        action_type, error = self._validated_action_type(arguments, self._DECISION_ACTIONS, "resolve_rule_window")
        if error is not None:
            return error
        actor = self._clean(arguments.get("actor"))
        window_id = self._clean(arguments.get("window_id"))
        choice = self._clean(arguments.get("choice"))
        details, detail_error = self._validated_details(arguments.get("details"), tool_name="resolve_rule_window")
        if detail_error is not None:
            return detail_error
        runtime = self.host._runtime(context.campaign_id)
        window = runtime.app.interceptor.decision_window_manager.find_pending(window_id=window_id)
        if window is None:
            return self._failure(
                "resolve_rule_window",
                "DECISION_WINDOW_NOT_FOUND",
                "没有找到这个仍待处理的规则窗口。",
                "先调用get_gameplay_state并使用当前window_id。",
            )
        if window.owner != actor:
            return self._failure(
                "resolve_rule_window",
                "DECISION_WINDOW_OWNER_MISMATCH",
                f"这个窗口属于【{window.owner}】，不是【{actor}】。",
                "使用窗口记录的owner，并确认当前玩家有权代其选择。",
            )
        is_pre_final_check_window = window.kind in {"trait_invocation", "bond_invocation"} or (
            window.kind == "skill_judgement"
            and str(window.payload.get("label") or "") == "幸运七"
        )
        if is_pre_final_check_window and choice == "accept_result":
            if action_type != ActionType.RESOLVE_DECISION:
                return self._failure(
                    "resolve_rule_window",
                    "POST_CHECK_ACCEPTANCE_ACTION_REQUIRED",
                    "接受当前检定结果必须使用【ResolveDecision】，不能伪装成特质、羁绊或叙事动作。",
                    "保留actor与window_id，使用action_type=ResolveDecision、choice=accept_result。",
                )
        elif is_pre_final_check_window and action_type == ActionType.RESOLVE_DECISION:
            return self._failure(
                "resolve_rule_window",
                "POST_CHECK_ACCEPTANCE_CHOICE_REQUIRED",
                "这个检定定稿动作缺少合法选择。",
                "若玩家不改骰面，使用choice=accept_result；否则使用窗口列出的特质、羁绊或【幸运七】动作。",
            )
        expected_actions = {
            "zero_hp": {ActionType.RESOLVE_ZERO_HP},
            "npc_fate": {ActionType.RESOLVE_DECISION},
            "critical_opportunity": {ActionType.TRIGGER_OPPORTUNITY},
            "opportunity_parameter": {ActionType.TRIGGER_OPPORTUNITY},
            "spell_parameter": {ActionType.RESOLVE_DECISION},
            "skill_judgement": {
                ActionType.RESOLVE_DECISION,
                ActionType.SKILL,
                ActionType.NARRATE,
            },
            "trait_invocation": {ActionType.INVOKE_TRAIT, ActionType.RESOLVE_DECISION},
            "bond_invocation": {ActionType.INVOKE_BOND, ActionType.RESOLVE_DECISION},
            "fumble_opportunity": {ActionType.TRIGGER_OPPORTUNITY},
            "acceleration_benefit": {
                ActionType.ATTACK,
                ActionType.SPELL,
                ActionType.RESOLVE_DECISION,
            },
            "immediate_attack": {
                ActionType.ATTACK,
                ActionType.RESOLVE_DECISION,
            },
            "skill_parameter": {
                ActionType.ATTACK,
                ActionType.SPELL,
                ActionType.HINDER,
                ActionType.OBJECTIVE,
                ActionType.USE_INVENTORY,
                ActionType.SKILL,
                ActionType.RESOLVE_DECISION,
            },
            "held_action": {ActionType.RESOLVE_DECISION},
        }
        allowed_for_window = expected_actions.get(window.kind)
        if allowed_for_window and action_type not in allowed_for_window:
            return self._failure(
                "resolve_rule_window",
                "DECISION_ACTION_KIND_MISMATCH",
                f"窗口【{window.kind}】不能用动作【{action_type.value}】处理。",
                "根据get_gameplay_state中的kind选择对应规则动作。",
            )
        if window.kind == "zero_hp":
            legal_choices = {
                self._clean(option.get("choice"))
                for option in window.options
                if self._clean(option.get("choice"))
            }
            if choice not in legal_choices:
                return self._failure(
                    "resolve_rule_window",
                    "ILLEGAL_ZERO_HP_CHOICE",
                    f"【{choice or '未指定'}】不是这次生命值归零窗口的合法选择。",
                    "只使用窗口列出的sacrifice或give_up_resistance；必须由该角色的玩家明确选择。",
                )
            if choice == "give_up_resistance":
                consequence_type = self._clean(details.get("consequence_type"))
                consequence = self._clean(details.get("consequence"))
                if consequence_type not in self._ZERO_HP_CONSEQUENCE_TYPES:
                    return self._failure(
                        "resolve_rule_window",
                        "ZERO_HP_CONSEQUENCE_TYPE_REQUIRED",
                        "放弃抵抗后，GM还没有选择一类合法的严重后果。",
                        "从黑暗、绝望、损失、怨恨、分离中选择恰好一类，并提交具体consequence。",
                    )
                if not consequence:
                    return self._failure(
                        "resolve_rule_window",
                        "ZERO_HP_CONSEQUENCE_REQUIRED",
                        "放弃抵抗后还缺少一项与当前局势相符的具体后果。",
                        "给出一项具体后果；不要同时叠加被俘、失物等多个独立后果。",
                    )
                if not consequence.startswith(f"{consequence_type}："):
                    details["consequence"] = f"{consequence_type}：{consequence}"
                if consequence_type == "黑暗":
                    new_theme = self._clean(details.get("new_theme"))
                    if new_theme not in self._DARKNESS_THEMES:
                        return self._failure(
                            "resolve_rule_window",
                            "DARKNESS_THEME_REQUIRED",
                            "【黑暗】后果必须把角色主题改为愤怒、疑虑、愧疚或复仇之一。",
                            "在details.new_theme中提交一个合法主题。",
                        )
                if consequence_type == "怨恨":
                    remove_target = self._clean(details.get("remove_bond_target"))
                    new_target = self._clean(details.get("new_bond_target"))
                    new_emotion = self._clean(details.get("new_emotion"))
                    character = runtime.app.character_manager.get(actor)
                    existing_targets = {bond.target for bond in character.bonds}
                    if remove_target not in existing_targets:
                        return self._failure(
                            "resolve_rule_window",
                            "RESENTMENT_EXISTING_BOND_REQUIRED",
                            "【怨恨】后果必须先抹除角色现有的一段羁绊。",
                            "从角色当前羁绊中选择details.remove_bond_target。",
                            result={"bond_targets": sorted(existing_targets)},
                        )
                    if not new_target or new_emotion not in self._RESENTMENT_EMOTIONS:
                        return self._failure(
                            "resolve_rule_window",
                            "RESENTMENT_REPLACEMENT_REQUIRED",
                            "【怨恨】后果缺少替代羁绊或合法的负面情感。",
                            "提交details.new_bond_target，并从憎恨、自卑、猜忌中选择details.new_emotion。",
                        )
            else:
                heroic_outcome = self._clean(details.get("heroic_outcome"))
                benefits_bond = details.get("sacrifice_benefits_bond")
                betters_world = details.get("sacrifice_betters_world")
                if not heroic_outcome:
                    return self._failure(
                        "resolve_rule_window",
                        "SACRIFICE_OUTCOME_REQUIRED",
                        "牺牲还没有说明角色以生命实际成就了什么。",
                        "依据玩家声明和当前局势提交具体details.heroic_outcome，不要只写‘英勇牺牲’。",
                    )
                if not isinstance(benefits_bond, bool) or not isinstance(betters_world, bool):
                    return self._failure(
                        "resolve_rule_window",
                        "SACRIFICE_CONDITIONS_REQUIRED",
                        "牺牲条件尚未按当前场景明确裁定。",
                        "分别提交布尔值details.sacrifice_benefits_bond与details.sacrifice_betters_world；规则层还会检查反派是否在场。",
                    )
        if window.kind == "npc_fate":
            legal_choices = {
                self._clean(option.get("choice"))
                for option in window.options
                if self._clean(option.get("choice"))
            }
            if choice not in legal_choices:
                return self._failure(
                    "resolve_rule_window",
                    "ILLEGAL_NPC_FATE",
                    f"【{choice or '未指定'}】不是这次NPC命运选择的合法处置。",
                    "从窗口列出的spare、capture、drive_off、unconscious、kill或other中选择。",
                )
            if choice == "other" and not self._clean(details.get("fate_description")):
                return self._failure(
                    "resolve_rule_window",
                    "NPC_FATE_DESCRIPTION_REQUIRED",
                    "选择其他处置时，还没有说明这个NPC具体遭遇了什么。",
                    "询问造成最后一击的玩家，并将答案写入details.fate_description。",
                )
        if window.kind == "held_action":
            if choice == "confirm":
                return self._failure(
                    "resolve_rule_window",
                    "HELD_ACTION_MUST_EXECUTE",
                    "确认缓存行动不能只关闭窗口，否则这一回合不会真正执行任何动作。",
                    "读取窗口payload中的action_type与action_parameters，改用对应的实际行动工具提交。",
                    result={
                        "cached_action_type": str(
                            window.payload.get("action_type") or ""
                        ),
                        "cached_action_parameters": dict(
                            window.payload.get("action_parameters") or {}
                        ),
                    },
                )
            if choice not in {"discard", "revise"}:
                return self._failure(
                    "resolve_rule_window",
                    "ILLEGAL_HELD_ACTION_CHOICE",
                    f"【{choice or '未指定'}】不是缓存行动的合法处理方式。",
                    "玩家要放弃时使用discard；准备另选行动时使用revise；确认时提交原本的实际行动。",
                )
        if window.kind == "trait_invocation" and action_type == ActionType.INVOKE_TRAIT:
            legal_traits = {
                self._clean(option.get("trait"))
                for option in window.options
                if self._clean(option.get("trait"))
            }
            if choice not in legal_traits:
                return self._failure(
                    "resolve_rule_window",
                    "ILLEGAL_TRAIT_INVOCATION",
                    f"【{choice or '未指定'}】不是这次检定可援用的特质。",
                    "从resolution_options逐字选择特质；若不重掷，改用ResolveDecision与accept_result。",
                )
            forbidden_replay_fields = {
                "success_transition",
                "success_observation",
                "success_answer",
                "failure_consequence",
            }
            leaked_fields = sorted(forbidden_replay_fields.intersection(details))
            if leaked_fields:
                return self._failure(
                    "resolve_rule_window",
                    "CHECK_TRANSACTION_FIELDS_ALREADY_STORED",
                    "重掷窗口不应重复提交原检定字段：" + "、".join(leaked_fields),
                    "只提交特质选择；原检定的成功、失败和转场事务已由窗口保存。重掷两枚骰时无需额外参数。",
                )
            # The model-friendly phrase ``reroll_dice: 2`` denotes a count,
            # while the lower-level rule engine historically interpreted an
            # integer as a one-based die index.  Two means both dice, which is
            # already the engine default when indices are omitted.
            reroll_count = details.pop("reroll_dice", None)
            if reroll_count not in (None, "", 2, "2"):
                return self._failure(
                    "resolve_rule_window",
                    "REROLL_DIE_SELECTION_REQUIRED",
                    "只重掷一枚骰时必须明确选择哪一枚。",
                    "使用details.reroll_indices数组并同时给出reroll_index_base=0；若重掷两枚骰，删除reroll_dice和reroll_indices。",
                )
        if window.kind == "bond_invocation" and action_type == ActionType.INVOKE_BOND:
            legal_targets = {
                self._clean(option.get("target"))
                for option in window.options
                if self._clean(option.get("target"))
            }
            if choice not in legal_targets:
                return self._failure(
                    "resolve_rule_window",
                    "ILLEGAL_BOND_INVOCATION",
                    f"【{choice or '未指定'}】不是这次检定可援用的羁绊对象。",
                    "从resolution_options逐字选择羁绊对象；若保留结果，改用ResolveDecision与accept_result。",
                )
        if window.kind in {"critical_opportunity", "fumble_opportunity"}:
            legal_effects = {
                self._clean(option.get("effect"))
                for option in window.options
                if self._clean(option.get("effect"))
            }
            if choice not in legal_effects:
                return self._failure(
                    "resolve_rule_window",
                    "ILLEGAL_OPPORTUNITY_EFFECT",
                    f"【{choice or '未指定'}】不是这个机会窗口的合法效果。",
                    "从get_gameplay_state返回的options中逐字选择effect。",
                )
        if action_type == ActionType.TRIGGER_OPPORTUNITY and choice == "揭示":
            target = self._clean(details.get("target"))
            if not target:
                return GMToolReceipt(
                    tool_name="resolve_rule_window",
                    ok=False,
                    error_code="OPPORTUNITY_TARGET_REQUIRED",
                    message="机会【揭示】还没有选择生物目标。",
                    correction_hint="向玩家询问想得知哪一个生物的目标或动机，不要替玩家猜。",
                    retryable=True,
                    result={"required_parameter": "target", "legal_effect": "揭示"},
                    public_fallback_reply="你想对哪一个生物使用【揭示】？",
                    lock_public_reply=True,
                )
            if not runtime.app.character_manager.exists(target):
                return self._failure(
                    "resolve_rule_window",
                    "OPPORTUNITY_TARGET_NOT_CREATURE",
                    f"没有找到可被【揭示】选中的生物【{target}】。",
                    "先调用get_gameplay_state或读取当前场景，再使用实际存在的生物名称。",
                )
            details["target_explicit"] = True
        if window.kind == "skill_parameter":
            details, skill_window_error = self._validated_skill_window_action(
                context,
                window=window,
                actor=actor,
                action_type=action_type,
                choice=choice,
                details=details,
            )
            if skill_window_error is not None:
                return skill_window_error
        if window.kind == "acceleration_benefit":
            legal_choices = {
                self._clean(option.get("choice"))
                for option in window.options
                if self._clean(option.get("choice"))
            }
            if choice not in legal_choices:
                return self._failure(
                    "resolve_rule_window",
                    "ILLEGAL_ACCELERATION_CHOICE",
                    f"【{choice or '未指定'}】不是这次【加速术】的合法选择。",
                    "从get_gameplay_state返回的options中选择attack、cast_spell或decline。",
                )
            expected_choice = {
                ActionType.ATTACK: "attack",
                ActionType.SPELL: "cast_spell",
                ActionType.RESOLVE_DECISION: "decline",
            }.get(action_type)
            if expected_choice != choice:
                return self._failure(
                    "resolve_rule_window",
                    "ACCELERATION_ACTION_CHOICE_MISMATCH",
                    f"动作【{action_type.value}】与选择【{choice}】不一致。",
                    "attack使用Attack，cast_spell使用Spell，decline使用ResolveDecision。",
                )
            if action_type == ActionType.ATTACK and not self._clean(details.get("target")):
                return self._failure(
                    "resolve_rule_window",
                    "ACCELERATION_ATTACK_TARGET_REQUIRED",
                    "【加速术】的顺势攻击还没有目标。",
                    "使用当前场景中存在且可被攻击的生物名称作为details.target。",
                )
            if action_type == ActionType.SPELL:
                target = self._clean(details.get("target"))
                details, spell_error = self._validated_spell_details(
                    context,
                    actor=actor,
                    target=target,
                    details=details,
                )
                if spell_error is not None:
                    return spell_error
        if window.kind == "immediate_attack":
            legal_choices = {
                self._clean(option.get("choice"))
                for option in window.options
                if self._clean(option.get("choice"))
            }
            if choice not in legal_choices:
                return self._failure(
                    "resolve_rule_window",
                    "ILLEGAL_IMMEDIATE_ATTACK_CHOICE",
                    f"【{choice or '未指定'}】不是这次【抢攻】的合法选择。",
                    "选择attack并提交details.target，或选择decline放弃顺势攻击。",
                )
            expected_choice = {
                ActionType.ATTACK: "attack",
                ActionType.RESOLVE_DECISION: "decline",
            }.get(action_type)
            if expected_choice != choice:
                return self._failure(
                    "resolve_rule_window",
                    "IMMEDIATE_ATTACK_ACTION_CHOICE_MISMATCH",
                    f"动作【{action_type.value}】与选择【{choice}】不一致。",
                    "attack使用Attack，decline使用ResolveDecision。",
                )
            if action_type == ActionType.ATTACK:
                target = self._clean(details.get("target"))
                legal_targets = {
                    self._clean(item)
                    for item in window.payload.get("legal_targets", [])
                    if self._clean(item)
                }
                if target not in legal_targets:
                    return self._failure(
                        "resolve_rule_window",
                        "IMMEDIATE_ATTACK_TARGET_REQUIRED",
                        f"【{target or '未指定'}】不是这次【抢攻】的合法目标。",
                        "从窗口的attack选项中选择一个details.target。",
                        result={"legal_targets": sorted(legal_targets)},
                    )
        parameters = {
            **details,
            "actor": actor,
            "window_id": window_id,
            "choice": choice,
        }
        if window.kind == "spell_parameter" and action_type == ActionType.RESOLVE_DECISION:
            # Keep the external tool convenient for a model while presenting
            # ActionInterceptor with its single canonical decision envelope.
            supplied = details.get("selected_option")
            selected = dict(supplied) if isinstance(supplied, dict) else {}
            selected.setdefault("choice", "cast_spell")
            for key in (
                "targets",
                "chosen_damage_type",
                "chosen_status",
                "chosen_attribute",
            ):
                if key in details:
                    selected[key] = details[key]

            parameter = self._clean(details.get("parameter"))
            value = details.get("value")
            if parameter in {"target", "targets"} and value not in (None, "", []):
                selected["targets"] = value if isinstance(value, list) else [value]
            elif parameter in {
                "chosen_damage_type",
                "chosen_status",
                "chosen_attribute",
            } and value not in (None, ""):
                selected[parameter] = value

            candidates = {
                self._clean(item)
                for item in window.payload.get("target_candidates", [])
                if self._clean(item)
            }
            if "targets" not in selected and choice in candidates:
                selected["targets"] = [choice]
            parameters["choice"] = "cast_spell"
            parameters["selected_option"] = selected
        if window.kind == "acceleration_benefit" and action_type in {
            ActionType.ATTACK,
            ActionType.SPELL,
        }:
            parameters["_acceleration_window_id"] = window_id
            parameters["opportunity_action"] = True
            target = self._clean(parameters.get("target"))
            if target:
                parameters["target_explicit"] = True
        if window.kind == "immediate_attack" and action_type == ActionType.ATTACK:
            parameters["_immediate_attack_window_id"] = window_id
            parameters["_reaction_followup"] = True
            parameters["_enforce_turn_order"] = False
            parameters["opportunity_action"] = True
        if window.kind == "skill_parameter" and action_type in {
            ActionType.ATTACK,
            ActionType.SPELL,
            ActionType.HINDER,
            ActionType.OBJECTIVE,
            ActionType.USE_INVENTORY,
            ActionType.SKILL,
        }:
            parameters["_skill_followup_window_id"] = window_id
            parameters["_reaction_followup"] = True
            parameters["_enforce_turn_order"] = False
            parameters["opportunity_action"] = True
        if action_type == ActionType.TRIGGER_OPPORTUNITY:
            parameters.setdefault("effect", choice)
        elif action_type == ActionType.INVOKE_TRAIT:
            parameters.setdefault("trait_name", choice)
        elif action_type == ActionType.INVOKE_BOND:
            parameters.setdefault("bond_target", choice)
        elif action_type == ActionType.SKILL:
            parameters.setdefault("skill_name", str(window.payload.get("skill") or choice))
        elif action_type == ActionType.NARRATE:
            if choice not in {"accept_result", "接受结果", "接受这次结果"}:
                return self._failure(
                    "resolve_rule_window",
                    "INVALID_POST_CHECK_ACCEPTANCE",
                    "这个叙事型窗口动作只能用于明确接受当前检定结果。",
                    "玩家若接受结果，choice使用accept_result；否则选择特质或羁绊动作。",
                )
            parameters["post_check_acceptance"] = True
        elif action_type == ActionType.RESOLVE_DECISION and is_pre_final_check_window:
            parameters["choice"] = "accept_result"
            parameters["selected_option"] = {"choice": "accept_result"}
            parameters["post_check_acceptance"] = True
        if action_type == ActionType.RESOLVE_DECISION and "selected_option" not in parameters:
            parameters["selected_option"] = {"choice": choice}
        return self._execute(
            context,
            tool_name="resolve_rule_window",
            action=Action(action_type, parameters),
            evidence=arguments.get("evidence"),
        )

    def _validated_skill_window_action(
        self,
        context: GMToolExecutionContext,
        *,
        window: Any,
        actor: str,
        action_type: ActionType,
        choice: str,
        details: dict[str, object],
    ) -> tuple[dict[str, object], GMToolReceipt | None]:
        """Validate optional skill actions without consuming their window.

        Choices that grant an immediate action stay pending until that action
        commits. This lets failed validation, post-check rerolls, save/load and
        nested decisions resume the same rules transaction safely.
        """

        tool_name = "resolve_rule_window"
        skill = self._clean(
            window.payload.get("skill")
            or window.payload.get("label")
        )
        legal_options = {
            self._clean(option.get("choice")): dict(option)
            for option in window.options
            if self._clean(option.get("choice"))
        }
        if choice not in legal_options:
            return details, self._failure(
                tool_name,
                "ILLEGAL_SKILL_WINDOW_CHOICE",
                f"【{choice or '未指定'}】不是【{skill or '技能'}】当前窗口的合法选择。",
                "从get_gameplay_state返回的options中逐字选择choice。",
            )

        direct_actions: dict[tuple[str, str], set[ActionType]] = {
            ("疾速身法", "attack"): {ActionType.ATTACK},
            ("疾速身法", "hinder_or_objective"): {
                ActionType.HINDER,
                ActionType.OBJECTIVE,
            },
            ("奥灵回响", "cast_spell"): {ActionType.SPELL},
            ("鹰眼", "immediate_ranged_attack"): {ActionType.ATTACK},
            ("应急用品", "use_inventory_action"): {
                ActionType.USE_INVENTORY,
            },
            ("快速评估", "declare_assessment"): {
                ActionType.SKILL,
            },
        }
        expected = direct_actions.get((skill, choice))
        if expected is None:
            if action_type != ActionType.RESOLVE_DECISION:
                return details, self._failure(
                    tool_name,
                    "SKILL_SELECTION_ACTION_MISMATCH",
                    f"【{skill}】的选择【{choice}】不是一项独立规则行动。",
                    "使用ResolveDecision提交这个选择；只有窗口明确授予攻击、施法、妨碍、推进目标或消耗物资行动时才提交对应动作。",
                )
            return details, None
        if action_type not in expected:
            expected_names = " 或 ".join(
                item.value for item in sorted(expected, key=lambda item: item.value)
            )
            return details, self._failure(
                tool_name,
                "SKILL_FOLLOWUP_ACTION_MISMATCH",
                f"【{skill}】的选择【{choice}】必须实际提交 {expected_names}。",
                "不要先用ResolveDecision关闭窗口；把玩家声明的完整目标和规则参数随实际动作一起提交。",
            )

        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        if not actor or not app.character_manager.exists(actor):
            return details, None
        character = app.character_manager.get(actor)
        normalized = deepcopy(details)
        option = legal_options[choice]

        if skill == "疾速身法":
            if character.mp < 10:
                return details, self._failure(
                    tool_name,
                    "QUICK_STEP_MP_INSUFFICIENT",
                    f"【{actor}】当前不足以支付【疾速身法】的 10 点精神值。",
                    "窗口保持待决；玩家可以选择decline，或在状态合法变化后再发动。",
                )
            modifier = int(option.get("modifier", 0) or 0)
            normalized["modifier"] = int(
                normalized.get("modifier", 0) or 0
            ) + modifier
            if action_type == ActionType.ATTACK:
                if not self._clean(normalized.get("target")):
                    return details, self._failure(
                        tool_name,
                        "QUICK_STEP_ATTACK_TARGET_REQUIRED",
                        "【疾速身法】的顺势攻击还没有目标。",
                        "填写当前冲突中合法生物的details.target。",
                    )
                return normalized, None

            target = self._clean(
                normalized.get("target")
                or normalized.get("clock_name")
            )
            raw_attributes = normalized.get("attributes")
            if not isinstance(raw_attributes, list) or len(raw_attributes) != 2:
                return details, self._failure(
                    tool_name,
                    "QUICK_STEP_ATTRIBUTES_REQUIRED",
                    "【疾速身法】的妨碍或推进目标检定必须明确两项属性。",
                    "在details.attributes中使用两项中文属性：敏捷、洞察、力量、意志；可以相同。",
                )
            attributes: list[str] = []
            for value in raw_attributes:
                attribute = self._ATTRIBUTE_ALIASES.get(self._clean(value))
                if not attribute:
                    return details, self._failure(
                        tool_name,
                        "QUICK_STEP_INVALID_ATTRIBUTE",
                        f"未知属性【{self._clean(value)}】。",
                        "details.attributes只使用敏捷、洞察、力量、意志。",
                    )
                attributes.append(attribute)
            try:
                difficulty = int(
                    normalized.get("difficulty")
                    or normalized.get("target_number")
                )
            except (TypeError, ValueError):
                difficulty = 0
            if difficulty < 7:
                return details, self._failure(
                    tool_name,
                    "QUICK_STEP_DIFFICULTY_REQUIRED",
                    "【疾速身法】的妨碍或推进目标检定缺少有效难度等级。",
                    "由GM依据局势在details.difficulty中提交不低于7的难度等级。",
                )
            purpose = self._clean(
                normalized.get("purpose")
                or normalized.get("reasoning")
            )
            success = self._clean(normalized.get("success_observation"))
            failure = self._clean(normalized.get("failure_consequence"))
            if not target or not purpose or not success or not failure:
                return details, self._failure(
                    tool_name,
                    "QUICK_STEP_CHECK_CONTRACT_REQUIRED",
                    "【疾速身法】的检定尚未说明目标、目的、成功结果或失败后果。",
                    "填写details.target、purpose、success_observation与failure_consequence；这些后台裁定不会在掷骰前逐字公开。",
                )
            normalized.update(
                {
                    "target": target,
                    "attributes": attributes,
                    "target_number": difficulty,
                    "reasoning": purpose,
                    "declared_action_goal": purpose,
                    "success_answer": success,
                    "scene_check_planned": True,
                    "non_damage": True,
                }
            )
            normalized.pop("difficulty", None)
            if action_type == ActionType.OBJECTIVE:
                clock_name = self._clean(
                    normalized.get("clock_name") or target
                )
                if not app.clock_manager.exists(clock_name):
                    return details, self._failure(
                        tool_name,
                        "QUICK_STEP_OBJECTIVE_CLOCK_REQUIRED",
                        f"当前没有活动命刻【{clock_name or '未指定'}】。",
                        "推进目标只能使用已经建立的命刻；否则改用Hinder。",
                    )
                normalized["clock_name"] = clock_name
            return normalized, None

        if skill == "奥灵回响":
            target = self._clean(normalized.get("target"))
            normalized, spell_error = self._validated_spell_details(
                context,
                actor=actor,
                target=target,
                details=normalized,
            )
            return normalized, spell_error

        if skill == "鹰眼":
            target = self._clean(normalized.get("target"))
            if not target:
                return details, self._failure(
                    tool_name,
                    "HAWKEYE_ATTACK_TARGET_REQUIRED",
                    "【鹰眼】的顺势远程攻击还没有目标。",
                    "填写当前冲突中的合法details.target。",
                )
            template_name = character.equipment_templates.get(
                character.equipped_main_hand,
                character.equipped_main_hand,
            )
            weapon = get_equipment_example(template_name)
            if (
                weapon is None
                or weapon.category not in {"弓", "枪械"}
                or weapon.range_type != "ranged"
            ):
                return details, self._failure(
                    tool_name,
                    "HAWKEYE_RANGED_WEAPON_REQUIRED",
                    "【鹰眼】的立即攻击要求当前装备弓类或枪械类武器。",
                    "先确认角色的主手装备；若没有合法武器，只能选择下一次远程攻击增伤或decline。",
                )
            normalized.update(
                {
                    "target": target,
                    "is_melee": False,
                    "_damage_high_roll_override": 0,
                }
            )
            return normalized, None

        if skill == "应急用品":
            item_name = self._clean(
                normalized.get("item_name")
                or normalized.get("item")
            )
            if not item_name:
                return details, self._failure(
                    tool_name,
                    "EMERGENCY_SUPPLIES_ITEM_REQUIRED",
                    "【应急用品】需要实际执行一次消耗物资行动，但尚未选择物品。",
                    "在details.item_name中填写治疗剂、圣灵水、万能药或元素裂片，并按需要填写目标。",
                )
            normalized["item_name"] = item_name
            normalized.setdefault("target", actor)
            return normalized, None

        if skill == "快速评估":
            assessments = normalized.get("assessments")
            rank = skill_rank(character.skills, "快速评估")
            if (
                not isinstance(assessments, list)
                or not assessments
                or len(assessments) > rank
            ):
                return details, self._failure(
                    tool_name,
                    "QUICK_ASSESSMENT_CHOICES_REQUIRED",
                    f"【快速评估】需要选择 1 至 {rank} 项评估。",
                    "details.assessments逐项填写target与kind；kind为trait或affinity，后者还需damage_type。",
                )
            normalized_assessments: list[dict[str, object]] = []
            damage_aliases = {
                "物理": "physical",
                "风": "wind",
                "雷": "lightning",
                "暗": "dark",
                "土": "earth",
                "火": "fire",
                "冰": "ice",
                "光": "light",
                "毒": "poison",
                "physical": "physical",
                "wind": "wind",
                "lightning": "lightning",
                "dark": "dark",
                "earth": "earth",
                "fire": "fire",
                "ice": "ice",
                "light": "light",
                "poison": "poison",
            }
            for raw in assessments:
                if not isinstance(raw, dict):
                    return details, self._failure(
                        tool_name,
                        "QUICK_ASSESSMENT_ITEM_INVALID",
                        "【快速评估】的每一项选择都必须是对象。",
                        "每项使用{target, kind}；相性评估再加damage_type。",
                    )
                target = self._clean(raw.get("target"))
                if (
                    not target
                    or not app.character_manager.exists(target)
                    or target not in app.conflict_manager.state.turn_order
                ):
                    return details, self._failure(
                        tool_name,
                        "QUICK_ASSESSMENT_TARGET_INVALID",
                        f"【{target or '未指定'}】不是当前冲突中可见的生物。",
                        "从get_gameplay_state的当前冲突角色中选择target。",
                    )
                kind = self._clean(raw.get("kind")).lower()
                if kind in {"trait", "特质", "特征"}:
                    requested_trait = self._clean(raw.get("trait"))
                    target_traits = {
                        str(item).strip()
                        for item in app.character_manager.get(target).traits
                        if str(item).strip()
                        and str(item).strip().lower()
                        not in {
                            "pc",
                            "npc",
                            "enemy",
                            "ally",
                            "villain",
                            "beast",
                            "construct",
                            "demon",
                            "elemental",
                            "humanoid",
                            "monster",
                            "plant",
                            "undead",
                            "玩家角色",
                            "非玩家角色",
                            "敌人",
                            "盟友",
                            "反派",
                            "野兽",
                            "构装体",
                            "恶魔",
                            "元素",
                            "人型",
                            "怪物",
                            "植物",
                            "不死族",
                        }
                    }
                    if not target_traits:
                        return details, self._failure(
                            tool_name,
                            "QUICK_ASSESSMENT_NO_TRAIT",
                            f"【{target}】没有已建档、可由【快速评估】揭示的特质。",
                            "改选另一个可见生物，或选择揭示一种伤害相性。",
                        )
                    if requested_trait and requested_trait not in target_traits:
                        return details, self._failure(
                            tool_name,
                            "QUICK_ASSESSMENT_TRAIT_NOT_FOUND",
                            f"【{target}】没有可揭示的真实特质【{requested_trait}】。",
                            "删除trait让规则层选择真实特质，或从该生物的实际特质中选择。",
                        )
                    normalized_assessments.append(
                        {
                            "target": target,
                            "kind": "trait",
                            "trait": requested_trait,
                        }
                    )
                    continue
                if kind in {"affinity", "相性", "伤害相性"}:
                    raw_damage = self._clean(raw.get("damage_type"))
                    damage_type = (
                        damage_aliases.get(raw_damage)
                        or damage_aliases.get(raw_damage.lower())
                    )
                    if damage_type is None:
                        return details, self._failure(
                            tool_name,
                            "QUICK_ASSESSMENT_DAMAGE_TYPE_REQUIRED",
                            "【快速评估】揭示相性时必须声明一种合法伤害类型。",
                            "damage_type使用物理、风、雷、暗、土、火、冰、光或毒。",
                        )
                    normalized_assessments.append(
                        {
                            "target": target,
                            "kind": "affinity",
                            "damage_type": damage_type,
                        }
                    )
                    continue
                return details, self._failure(
                    tool_name,
                    "QUICK_ASSESSMENT_KIND_REQUIRED",
                    "【快速评估】每项只能揭示一项特质或一种伤害相性。",
                    "kind使用trait或affinity。",
                )
            normalized.update(
                {
                    "skill_name": "快速评估",
                    "assessments": normalized_assessments,
                    "mp_cost": len(normalized_assessments) * 5,
                }
            )
            return normalized, None

        return normalized, None

    def resolve_gm_opportunity(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        """Commit a GM-owned fumble opportunity without inventing player evidence."""

        tool_name = "resolve_gm_opportunity"
        window_id = self._clean(arguments.get("window_id"))
        choice = self._clean(arguments.get("choice"))
        details, detail_error = self._validated_details(
            arguments.get("details"),
            tool_name=tool_name,
        )
        if detail_error is not None:
            return detail_error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        window = app.interceptor.decision_window_manager.find_pending(window_id=window_id)
        if window is None:
            return self._failure(
                tool_name,
                "GM_OPPORTUNITY_NOT_FOUND",
                "没有找到这个仍待处理的GM机会。",
                "先调用get_gameplay_state并使用当前fumble_opportunity的window_id。",
            )
        if window.kind != "fumble_opportunity" or window.owner != "__gm__":
            return self._failure(
                tool_name,
                "NOT_GM_FUMBLE_OPPORTUNITY",
                "这个窗口不是由GM处理的大失败机会。",
                "玩家自己的待决选择应使用resolve_rule_window。",
            )
        legal_effects = {
            self._clean(option.get("effect"))
            for option in window.options
            if self._clean(option.get("effect"))
        }
        if choice not in legal_effects:
            return self._failure(
                tool_name,
                "ILLEGAL_GM_OPPORTUNITY_EFFECT",
                f"【{choice or '未指定'}】不是这个大失败机会的合法效果。",
                "从get_gameplay_state返回的options中逐字选择effect。",
            )

        source_actor = self._clean(window.payload.get("source_actor"))
        parameters: dict[str, object] = {
            **details,
            "actor": "__gm__",
            "window_id": window.window_id,
            "effect": choice,
            "opportunity_action": True,
            "gm_controlled_opportunity": True,
            "source_actor": source_actor,
        }
        if choice == "受苦":
            target = self._clean(details.get("target"))
            status = self._clean(details.get("status_effect"))
            legal_statuses = {"slow", "dazed", "weakened", "shaken"}
            if not target or not app.character_manager.exists(target):
                return self._failure(
                    tool_name,
                    "GM_OPPORTUNITY_TARGET_REQUIRED",
                    "机会【受苦】需要选择一个当前存在的生物。",
                    "从当前角色或敌人中选择details.target。",
                )
            if status not in legal_statuses:
                return self._failure(
                    tool_name,
                    "GM_OPPORTUNITY_STATUS_REQUIRED",
                    "机会【受苦】只能施加眩晕、动摇、迟缓或虚弱。",
                    "details.status_effect使用dazed、shaken、slow或weakened。",
                )
        elif choice == "进展":
            clock_name = self._clean(details.get("clock_name"))
            if not clock_name or not app.clock_manager.exists(clock_name):
                return self._failure(
                    tool_name,
                    "GM_OPPORTUNITY_CLOCK_REQUIRED",
                    "机会【进展】只能影响当前已经存在的命刻。",
                    "从当前命刻中选择details.clock_name，不得在此工具里新建命刻。",
                )
            clock = app.clock_manager.get(clock_name)
            parameters["delta"] = 2
            parameters["erase"] = str(clock.clock_type or "").lower() != "threat"
        elif choice == "失物":
            target = self._clean(details.get("target") or source_actor)
            item_name = self._clean(details.get("item_name"))
            if not target or not app.character_manager.exists(target) or not item_name:
                return self._failure(
                    tool_name,
                    "GM_OPPORTUNITY_ITEM_REQUIRED",
                    "机会【失物】需要明确角色和一件可失去的物品。",
                    "填写details.target与details.item_name；不要选择角色的标志性核心装备。",
                )
            character = app.character_manager.get(target)
            if item_name not in character.equipment:
                return self._failure(
                    tool_name,
                    "GM_OPPORTUNITY_ITEM_NOT_OWNED",
                    f"【{target}】没有可被失去的物品【{item_name}】。",
                    "从角色当前equipment中选择非标志性物品，或改用其他机会效果。",
                )
            if item_name in {
                character.equipped_main_hand,
                character.equipped_off_hand,
                character.equipped_armor,
                character.equipped_accessory,
            }:
                return self._failure(
                    tool_name,
                    "GM_OPPORTUNITY_SIGNATURE_ITEM_PROTECTED",
                    "不能用大失败机会直接夺走角色当前装备的标志性物品。",
                    "选择背包中的非关键物品，或改用受苦、进展或转折。",
                )
        elif choice == "揭示":
            target = self._clean(details.get("target"))
            known_npc = bool(app.world_state.resolve_npc_name(target)) if target else False
            if not target or (
                not app.character_manager.exists(target)
                and not known_npc
            ):
                return self._failure(
                    tool_name,
                    "GM_OPPORTUNITY_REVEAL_TARGET_REQUIRED",
                    "机会【揭示】需要选择一个当前存在的生物。",
                    "填写details.target；若其动机尚未登记，同时填写details.revealed_motivation。",
                )
            parameters["target_explicit"] = True
        elif choice == "纽带":
            bond_owner = self._clean(details.get("bond_owner"))
            target = self._clean(details.get("target"))
            emotion = self._clean(details.get("emotion"))
            if not bond_owner or not app.character_manager.exists(bond_owner):
                return self._failure(
                    tool_name,
                    "GM_OPPORTUNITY_BOND_OWNER_REQUIRED",
                    "GM控制的大失败机会使用【纽带】时，需要明确哪一个生物建立羁绊。",
                    "填写当前存在角色的details.bond_owner。",
                )
            if not target or not emotion:
                return self._failure(
                    tool_name,
                    "GM_OPPORTUNITY_BOND_DETAILS_REQUIRED",
                    "机会【纽带】缺少羁绊对象或新增情感。",
                    "填写details.target与details.emotion。",
                )
        elif choice == "情报":
            information = self._clean(
                details.get("information")
                or details.get("fact")
                or details.get("description")
            )
            if not information:
                return self._failure(
                    tool_name,
                    "GM_OPPORTUNITY_INFORMATION_REQUIRED",
                    "机会【情报】需要一条真实、有用的线索。",
                    "依据已准备暗线或已知事实填写details.information，不得临时改写已公开事实。",
                )
        elif choice == "青睐":
            if not self._clean(details.get("target")):
                return self._failure(
                    tool_name,
                    "GM_OPPORTUNITY_FAVOR_TARGET_REQUIRED",
                    "机会【青睐】需要明确谁给予支持或赞赏。",
                    "填写details.target，并可用details.description说明支持方式。",
                )
        elif choice == "审视":
            if not self._clean(details.get("target")):
                return self._failure(
                    tool_name,
                    "GM_OPPORTUNITY_SCAN_TARGET_REQUIRED",
                    "机会【审视】需要选择一个能看见的生物。",
                    "填写details.target；规则层只会揭示该目标真实存在的弱点或特质。",
                )
        elif choice == "失态":
            if not self._clean(details.get("target")) or not self._clean(
                details.get("statement")
                or details.get("compromising_statement")
            ):
                return self._failure(
                    tool_name,
                    "GM_OPPORTUNITY_MISSTEP_DETAILS_REQUIRED",
                    "机会【失态】需要选择生物，并由其操控者给出一句妥协性言论。",
                    "填写details.target与details.statement。",
                )
        elif choice == "优势":
            target = self._clean(details.get("target"))
            if not target or not app.character_manager.exists(target):
                return self._failure(
                    tool_name,
                    "GM_OPPORTUNITY_ADVANTAGE_TARGET_REQUIRED",
                    "机会【优势】需要选择自己或一名盟友。",
                    "填写当前存在角色的details.target。",
                )
        elif choice == "转折":
            subject = self._clean(details.get("subject") or details.get("target"))
            if not subject:
                return self._failure(
                    tool_name,
                    "GM_OPPORTUNITY_TWIST_SUBJECT_REQUIRED",
                    "机会【转折】需要明确突然出现在场景中的某人或某物。",
                    "填写details.subject，并可用details.description说明其出现方式。",
                )
            parameters["subject"] = subject
        elif choice == "自定义":
            description = self._clean(details.get("description"))
            if not description:
                return self._failure(
                    tool_name,
                    "GM_OPPORTUNITY_CUSTOM_REQUIRED",
                    "自定义机会需要一个具体、可继续游玩的意外变化。",
                    "填写details.description；不能宣告必死、无解或改写已公开事实。",
                )
            parameters["description"] = description

        return self._execute(
            context,
            tool_name=tool_name,
            action=Action(ActionType.TRIGGER_OPPORTUNITY, parameters),
            evidence=None,
            require_evidence=False,
            require_character_actor=False,
        )

    def _execute_generic(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
        allowed: set[ActionType],
        tool_name: str,
    ) -> GMToolReceipt:
        action_type, error = self._validated_action_type(arguments, allowed, tool_name)
        if error is not None:
            return error
        details, detail_error = self._validated_details(arguments.get("details"), tool_name=tool_name)
        if detail_error is not None:
            return detail_error
        actor = self._clean(arguments.get("actor"))
        target = self._clean(arguments.get("target"))
        if action_type == ActionType.SPELL:
            details, spell_error = self._validated_spell_details(
                context,
                actor=actor,
                target=target,
                details=details,
            )
            if spell_error is not None:
                return spell_error
        if action_type == ActionType.SKILL:
            details, skill_error = self._validated_skill_details(
                context,
                actor=actor,
                details=details,
            )
            if skill_error is not None:
                return skill_error
        if action_type == ActionType.TINKERER_GADGET:
            details, gadget_error = self._validated_tinkerer_gadget_details(
                context,
                actor=actor,
                target=target,
                details=details,
            )
            if gadget_error is not None:
                return gadget_error
        if action_type in {
            ActionType.PLAN_RITUAL,
            ActionType.CONTRIBUTE_RITUAL,
            ActionType.CAST_RITUAL,
        }:
            details, ritual_error = self._validated_ritual_details(
                context,
                action_type=action_type,
                actor=actor,
                details=details,
            )
            if ritual_error is not None:
                return ritual_error
        if action_type in self._SCENE_ACTIONS:
            details, scene_error = self._validated_scene_action_details(
                context,
                action_type=action_type,
                actor=actor,
                target=target,
                details=details,
            )
            if scene_error is not None:
                return scene_error
        parameters = {**details, "actor": actor}
        if target:
            parameters["target"] = target
        return self._execute(
            context,
            tool_name=tool_name,
            action=Action(action_type, parameters),
            evidence=arguments.get("evidence"),
        )

    def _validated_ritual_details(
        self,
        context: GMToolExecutionContext,
        *,
        action_type: ActionType,
        actor: str,
        details: dict[str, object],
    ) -> tuple[dict[str, object], GMToolReceipt | None]:
        """Keep conflict rituals on their create-clock, fill, then-cast path."""

        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        manager = app.ritual_manager
        normalized = deepcopy(details)
        conflict_active = bool(app.conflict_manager.state.active)

        if action_type == ActionType.PLAN_RITUAL:
            if conflict_active:
                normalized["start_conflict_clock"] = True
                normalized["track_clock"] = True
            else:
                normalized["track_clock"] = bool(
                    normalized.get("track_clock", False)
                )
            return normalized, None

        raw_name = self._clean(
            normalized.get("clock_name")
            or normalized.get("name")
        )
        candidates = [raw_name] if raw_name else []
        if raw_name and not raw_name.startswith("仪式："):
            candidates.append(f"仪式：{raw_name}")
        clock_name = next(
            (
                candidate
                for candidate in candidates
                if candidate in manager.active_rituals
            ),
            "",
        )
        if (
            action_type == ActionType.CAST_RITUAL
            and not conflict_active
            and not clock_name
        ):
            # Outside conflict, rituals are a single final casting check and do
            # not require a preparatory clock. The interceptor constructs the
            # plan from these typed details and remains the rules authority.
            return normalized, None
        if not clock_name:
            return details, self._failure(
                "perform_ritual_project_action",
                "ACTIVE_RITUAL_REQUIRED",
                "当前没有与这次行动匹配的已启动仪式。",
                "先读取游戏状态；冲突中必须先用PlanRitual成功建立仪式命刻。",
            )
        plan = manager.active_rituals[clock_name]
        normalized["clock_name"] = clock_name

        if action_type == ActionType.CONTRIBUTE_RITUAL:
            clock = app.clock_manager.get(clock_name)
            if clock.current >= clock.max_segments or clock.status == "ready":
                return details, self._failure(
                    "perform_ritual_project_action",
                    "RITUAL_ALREADY_READY",
                    f"仪式【{plan.name}】的准备已经完成。",
                    f"等待【{plan.caster}】在其下个回合提交CastRitual。",
                )
            return normalized, None

        if actor != plan.caster:
            return details, self._failure(
                "perform_ritual_project_action",
                "RITUAL_CASTER_MISMATCH",
                f"仪式【{plan.name}】必须由启动它的【{plan.caster}】完成最终施法。",
                "等待该施法者本人的回合与玩家声明。",
            )
        if not app.clock_manager.exists(clock_name):
            return details, self._failure(
                "perform_ritual_project_action",
                "RITUAL_CLOCK_MISSING",
                f"仪式命刻【{clock_name}】不存在。",
                "不要绕过已中断或已结案的仪式；需要时重新启动。",
            )
        clock = app.clock_manager.get(clock_name)
        if clock.current < clock.max_segments:
            return details, self._failure(
                "perform_ritual_project_action",
                "RITUAL_CLOCK_INCOMPLETE",
                f"仪式【{plan.name}】尚未准备完成。",
                f"继续推进【{clock_name}】；当前为{clock.current}/{clock.max_segments}。",
            )
        normalized["require_completed_clock"] = True
        return normalized, None

    def _validated_spell_details(
        self,
        context: GMToolExecutionContext,
        *,
        actor: str,
        target: str,
        details: dict[str, object],
    ) -> tuple[dict[str, object], GMToolReceipt | None]:
        """Canonicalize a typed learned-spell action.

        Rituals and improvised scene magic have separate typed tools. When the
        model says a PC casts a learned spell, a typo must be repaired by the
        model rather than silently changing the action into a generic
        difficulty-10 magic check.
        """

        # The typed boundary accepts the two natural aliases the model most
        # commonly emits, then stores only canonical rule fields downstream.
        raw_name = self._clean(details.get("spell_name") or details.get("spell"))
        if not raw_name:
            return details, self._failure(
                "perform_character_action",
                "SPELL_NAME_REQUIRED",
                "施法行动缺少标准法术名。",
                "从角色已学会法术中选择spell_name；不要用动作描述代替法术名。",
            )
        unwrapped = raw_name
        wrapper_pairs = (("【", "】"), ("[", "]"), ("「", "」"), ("『", "』"))
        changed = True
        while changed and len(unwrapped) >= 2:
            changed = False
            for opening, closing in wrapper_pairs:
                if unwrapped.startswith(opening) and unwrapped.endswith(closing):
                    unwrapped = unwrapped[len(opening) : -len(closing)].strip()
                    changed = True
                    break
        canonical = normalize_spell_name(unwrapped)
        try:
            get_spell_definition(canonical)
        except ValueError:
            return details, self._failure(
                "perform_character_action",
                "UNKNOWN_SPELL_NAME",
                f"没有找到标准法术【{raw_name}】。",
                "先调用get_gameplay_state或query_rules_reference，使用角色卡中的标准法术名重试。",
            )

        runtime = self.host._runtime(context.campaign_id)
        if not actor or not runtime.app.character_manager.exists(actor):
            # _execute will return the more general actor validation receipt.
            return {**details, "spell_name": canonical}, None
        character = runtime.app.character_manager.get(actor)
        learned = {
            normalize_spell_name(str(name).strip())
            for name in character.spells
            if str(name).strip()
        }
        if canonical not in learned:
            known = "、".join(sorted(learned)) or "无"
            return details, self._failure(
                "perform_character_action",
                "SPELL_NOT_LEARNED",
                f"【{actor}】尚未学会法术【{canonical}】。",
                f"该角色当前已学会：{known}。选择其中之一，或先按升级/建卡规则学习法术。",
            )

        normalized = {**details, "spell_name": canonical}
        normalized.pop("spell", None)
        chimerist_species = character.chimerist_spell_species.get(canonical)
        if chimerist_species:
            normalized["chimerist_origin_species"] = chimerist_species
        if details.get("chosen_damage_type") is not None:
            normalized_damage_type = normalize_spell_damage_type(
                details.get("chosen_damage_type")
            )
            if normalized_damage_type:
                normalized["chosen_damage_type"] = normalized_damage_type
        if "chosen_damage_type" not in normalized and details.get("element") is not None:
            normalized_element = normalize_spell_damage_type(details.get("element"))
            if normalized_element:
                normalized["chosen_damage_type"] = normalized_element
        normalized.pop("element", None)
        if "targets" not in normalized and target:
            target_names = [
                item.strip()
                for item in re.split(r"\s*[、,，/；;]\s*", target)
                if item.strip()
            ]
            if len(target_names) > 1:
                normalized["targets"] = target_names
        return normalized, None

    def _validated_skill_details(
        self,
        context: GMToolExecutionContext,
        *,
        actor: str,
        details: dict[str, object],
    ) -> tuple[dict[str, object], GMToolReceipt | None]:
        raw_name = self._clean(details.get("skill_name"))
        if not raw_name:
            return details, self._failure(
                "perform_character_action",
                "SKILL_NAME_REQUIRED",
                "职业技能行动缺少标准技能名。",
                "读取角色卡并填写其实际拥有、且可主动发动的技能名。",
            )
        canonical = normalize_skill_reference_name(raw_name)
        reference = get_skill_reference(canonical)
        if reference is None:
            return details, self._failure(
                "perform_character_action",
                "UNKNOWN_SKILL_NAME",
                f"没有找到标准职业技能【{raw_name}】。",
                "读取角色卡或规则参考后，使用标准技能名重试。",
            )

        runtime = self.host._runtime(context.campaign_id)
        character = (
            runtime.app.character_manager.get(actor)
            if actor and runtime.app.character_manager.exists(actor)
            else None
        )
        if character is not None and skill_rank(character.skills, canonical) <= 0:
            known = "、".join(sorted(character.skills)) or "无"
            return details, self._failure(
                "perform_character_action",
                "SKILL_NOT_LEARNED",
                f"【{actor}】尚未拥有职业技能【{canonical}】。",
                f"该角色当前技能：{known}。只结算角色实际拥有的技能。",
            )

        if canonical in SPELL_GRANTING_SKILLS:
            known_spells = sorted(
                str(name).strip() for name in getattr(character, "spells", []) if str(name).strip()
            ) if character is not None else []
            clarification = (
                f"【{canonical}】是让角色学习{SPELL_GRANTING_SKILLS[canonical]}的职业技能，"
                "不是可以直接施放的法术。"
            )
            if known_spells:
                clarification += "你想施放哪个已学会的法术：" + "、".join(known_spells) + "？"
            else:
                clarification += "请先确认角色已经学会的具体法术。"
            return details, GMToolReceipt(
                tool_name="perform_character_action",
                ok=False,
                error_code="SPELL_GRANTING_SKILL_IS_NOT_SPELL",
                message=clarification,
                correction_hint="不要把授法技能改写成一次技能行动；询问具体法术，得到玩家选择后再提交Spell。",
                retryable=True,
                result={
                    "skill_name": canonical,
                    "known_spells": known_spells,
                    "clarification": clarification,
                },
                public_fallback_reply=clarification,
            )

        if canonical == "契约与召唤" and character is not None:
            normalized, arcanum_error = self._validated_arcanum_skill_details(
                actor=actor,
                character=character,
                details=details,
            )
            if arcanum_error is not None:
                return details, arcanum_error
            details = normalized

        coverage = skill_implementation_coverage(canonical)
        if coverage is not None and coverage.category == SKILL_COVERAGE_PASSIVE_HARD:
            clarification = (
                f"【{canonical}】会在满足技能条件时生效，不是可以单独发动的一次行动。"
                "请说明角色此刻实际要做什么。"
            )
            return details, GMToolReceipt(
                tool_name="perform_character_action",
                ok=False,
                error_code="PASSIVE_SKILL_IS_NOT_ACTION",
                message=clarification,
                correction_hint="根据玩家真正声明的攻击、施法、旅行、仪式或其他行动重新选择工具；不要提交Skill占位。",
                retryable=True,
                result={"skill_name": canonical, "clarification": clarification},
                public_fallback_reply=clarification,
            )

        return {**details, "skill_name": canonical}, None

    def _validated_arcanum_skill_details(
        self,
        *,
        actor: str,
        character: Character,
        details: dict[str, object],
    ) -> tuple[dict[str, object], GMToolReceipt | None]:
        """Resolve only unambiguous Arcanum choices from authoritative state."""

        normalized = {**details, "skill_name": "契约与召唤"}
        mode = self._clean(details.get("mode") or "summon").lower()
        dismiss = mode in {
            "dismiss",
            "release",
            "解除",
            "解除阿卡纳",
            "遣散",
            "遣散奥灵",
            "释放",
            "解放",
        } or bool(details.get("dismiss"))
        selected = self._clean(
            details.get("arcanum")
            or details.get("arcanum_name")
        )
        if selected:
            normalized["arcanum"] = selected
            return normalized, None

        if character.active_arcanum:
            normalized["arcanum"] = character.active_arcanum
            return normalized, None

        contracts = list(
            dict.fromkeys(
                self._clean(name)
                for name in character.bound_arcana
                if self._clean(name)
            )
        )
        if len(contracts) == 1:
            normalized["arcanum"] = contracts[0]
            return normalized, None
        if not contracts:
            return details, self._failure(
                "perform_character_action",
                "ARCANUM_CONTRACT_REQUIRED",
                f"【{actor}】尚未记录任何已结契奥灵。",
                "先在角色创建或剧情中完成并记录奥灵契约；不要默认成熔炉奥灵。",
            )

        verb = "遣散" if dismiss else "召唤"
        return details, self._failure(
            "perform_character_action",
            "ARCANUM_SELECTION_REQUIRED",
            f"【{actor}】拥有多个奥灵契约，本次还没有说明要{verb}哪一个。",
            f"从角色已结契奥灵中选择arcanum：{'、'.join(contracts)}。",
        )

    def _validated_tinkerer_gadget_details(
        self,
        context: GMToolExecutionContext,
        *,
        actor: str,
        target: str,
        details: dict[str, object],
    ) -> tuple[dict[str, object], GMToolReceipt | None]:
        """Validate the exact Portable Benefits rule before opening a transaction.

        A portable device is not a generic scanner.  The language model may
        still describe ordinary tools as part of an Investigate check, but a
        ``TinkererGadget`` action must name one of the concrete unlocked rule
        functions.  This keeps an unsupported fictional use from consuming a
        turn, resources, memories, or an autosave slot.
        """

        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        if not actor or not app.character_manager.exists(actor):
            return details, None
        character = app.character_manager.get(actor)
        unlocked = portable_device_tiers(character.skill_options.get("便携装置", []))

        raw_type = self._clean(
            details.get("gadget_type")
            or details.get("type")
            or details.get("device_type")
            or details.get("device")
        )
        raw_mode = self._clean(
            details.get("mode")
            or details.get("function")
            or details.get("subtype")
            or details.get("infusion_name")
        )
        combined = f"{raw_type} {raw_mode}".strip().lower()

        alchemy = any(token in combined for token in ("alchemy", "炼金装置", "炼金术", "调合"))
        infusion = any(token in combined for token in ("infusion", "注魔装置", "注魔", "灌注"))
        override = any(token in combined for token in ("override", "魔导覆写", "魔科技篡夺", "篡夺", "覆写"))
        cannon = any(token in combined for token in ("magicannon", "cannon", "魔法加农炮", "魔加农"))
        orb = any(token in combined for token in ("magisphere", "魔科天球", "法球", "天球"))

        # Naming only the skill or device family does not choose a rules
        # function.  In particular, "便携装置（魔导装置）" plus a narrative
        # purpose must not silently fall through to Magicannon creation.
        if not any((alchemy, infusion, override, cannon, orb)):
            return details, self._failure(
                "perform_character_action",
                "GADGET_RULE_FUNCTION_REQUIRED",
                "【便携装置】不是通用扫描或校准能力；这次行动没有指定可发动的装置规则功能。",
                (
                    "若玩家只是借助工具听声、测距、检查或维修，请改用普通检定并把工具作为行动方法；"
                    "只有玩家明确使用已解锁的炼金装置、注魔装置、魔导覆写、魔法加农炮或法球时，"
                    "才重新提交TinkererGadget。"
                ),
            )

        selected = sum(bool(item) for item in (alchemy, infusion, override, cannon, orb))
        if selected != 1:
            return details, self._failure(
                "perform_character_action",
                "AMBIGUOUS_GADGET_FUNCTION",
                "一次便携装置行动只能发动一种具体规则功能。",
                "保留玩家明确选择的那一种装置功能，删除相互冲突的mode或gadget_type后重试。",
            )

        def require(device: str, tier: int, *, function_name: str) -> GMToolReceipt | None:
            try:
                app.interceptor.gadget_manager.require_portable_device(actor, device, tier)
                return None
            except ValueError as exc:
                rule_message = str(exc)
            return self._failure(
                "perform_character_action",
                "GADGET_FUNCTION_NOT_UNLOCKED",
                rule_message or f"【{actor}】尚未解锁便携装置功能【{function_name}】。",
                f"读取角色卡中的便携装置选择；【{function_name}】需要【{device}】第{tier}阶增益。",
            )

        normalized = deepcopy(details)
        if alchemy:
            tier_aliases = {
                "basic": ("基础", 1),
                "基础": ("基础", 1),
                "advanced": ("进阶", 2),
                "高级": ("进阶", 2),
                "进阶": ("进阶", 2),
                "supreme": ("顶级", 3),
                "最高": ("顶级", 3),
                "顶级": ("顶级", 3),
            }
            raw_tier = self._clean(details.get("tier"))
            current_tier = int(unlocked.get("炼金装置", 0) or 0)
            if not raw_tier:
                if current_tier == 1:
                    raw_tier = "基础"
                elif current_tier > 1:
                    return details, self._failure(
                        "perform_character_action",
                        "ALCHEMY_TIER_REQUIRED",
                        "角色解锁了多档炼金装置；本次行动还没有选择使用哪一档混合剂。",
                        "询问玩家选择基础、进阶或顶级，然后用相同角色继续提交这次行动。",
                    )
            tier_data = tier_aliases.get(raw_tier.lower()) or tier_aliases.get(raw_tier)
            if tier_data is None:
                return details, self._failure(
                    "perform_character_action",
                    "UNKNOWN_ALCHEMY_TIER",
                    f"未知炼金装置档位【{raw_tier or '未指定'}】。",
                    "炼金装置只能选择基础、进阶或顶级。",
                )
            tier_name, required_tier = tier_data
            error = require("炼金装置", required_tier, function_name=f"{tier_name}炼金装置")
            if error is not None:
                return details, error
            normalized.update({"gadget_type": "炼金装置", "mode": "炼金装置", "tier": tier_name})
            return normalized, None

        if infusion:
            infusion_name = self._clean(details.get("infusion_name") or details.get("mode"))
            if not infusion_name or infusion_name in {"注魔", "注魔装置", "灌注", "灌注术", "infusion"}:
                return details, self._failure(
                    "perform_character_action",
                    "INFUSION_NAME_REQUIRED",
                    "注魔装置行动还没有选择具体注魔效果。",
                    "询问玩家选择其已解锁的注魔名称，再继续提交同一次行动。",
                )
            try:
                required_tier = app.interceptor.gadget_manager.infusion_required_tier(infusion_name)
            except ValueError as exc:
                return details, self._failure(
                    "perform_character_action",
                    "UNKNOWN_INFUSION",
                    str(exc),
                    "从角色已解锁的注魔效果中选择标准名称。",
                )
            error = require("注魔装置", required_tier, function_name=infusion_name)
            if error is not None:
                return details, error
            return details, self._failure(
                "perform_character_action",
                "INFUSION_REQUIRES_ATTACK",
                "注魔装置不是独立行动；它在角色的攻击命中时附加到该次攻击。",
                (
                    "把当前动作改为Attack，并在details.infusion_name中保留玩家选择的"
                    f"【{infusion_name}】；不要先单独结算一次TinkererGadget。"
                ),
            )

        if override:
            error = require("魔导装置", 1, function_name="魔导覆写")
            if error is not None:
                return details, error
            if not app.conflict_manager.state.active:
                return details, self._failure(
                    "perform_character_action",
                    "MAGITECH_OVERRIDE_REQUIRES_CONFLICT",
                    "魔导覆写只能在冲突场景中发动。",
                    "若玩家正在操作普通场景中的机械或魔导设施，请改用合适的普通检定。",
                )
            if not target:
                return details, self._failure(
                    "perform_character_action",
                    "MAGITECH_OVERRIDE_TARGET_REQUIRED",
                    "魔导覆写还没有指定目标。",
                    "询问玩家要覆写哪一个当前可见的敌人，再继续这次行动。",
                )
            forced_action = self._clean(details.get("forced_action") or details.get("command"))
            if not forced_action:
                return details, self._failure(
                    "perform_character_action",
                    "MAGITECH_OVERRIDE_COMMAND_REQUIRED",
                    "魔导覆写需要玩家指定迫使目标立即执行的行动。",
                    "询问玩家要让该目标执行什么行动，再继续这次行动。",
                )
            if not app.character_manager.exists(target):
                return details, self._failure(
                    "perform_character_action",
                    "MAGITECH_OVERRIDE_INVALID_TARGET",
                    f"【{target}】不是已建档、可被魔导覆写的生物。",
                    "若玩家正在操作场景装置，请改用普通检定或场景行动；魔导覆写只作用于满足条件的敌人。",
                )
            target_character = app.character_manager.get(target)
            target_traits = {str(item).strip().lower() for item in target_character.traits}
            if "pc" in target_traits or target not in app.conflict_manager.state.turn_order:
                return details, self._failure(
                    "perform_character_action",
                    "MAGITECH_OVERRIDE_TARGET_NOT_ENEMY",
                    "魔导覆写必须指定当前冲突中的敌人。",
                    "从当前冲突里可见且满足条件的敌人中选择目标。",
                )
            if not ({"construct", "构装体", "构造体", "elemental", "元素"} & target_traits):
                return details, self._failure(
                    "perform_character_action",
                    "MAGITECH_OVERRIDE_INVALID_SPECIES",
                    "魔导覆写只能指定构装体或元素敌人。",
                    "选择满足物种条件的当前敌人，或改用其他行动。",
                )
            if not ({"mindless", "无心智", "无心智生物"} & target_traits):
                return details, self._failure(
                    "perform_character_action",
                    "MAGITECH_OVERRIDE_TARGET_HAS_MIND",
                    "魔导覆写要求目标无心智。",
                    "选择同时具有无心智特征的构装体或元素敌人。",
                )
            if not target_character.statuses:
                return details, self._failure(
                    "perform_character_action",
                    "MAGITECH_OVERRIDE_STATUS_REQUIRED",
                    "魔导覆写要求目标当前受到至少一种异常状态影响。",
                    "先让目标陷入异常状态，或改用其他行动。",
                )
            normalized.update(
                {
                    "gadget_type": "魔导装置",
                    "mode": "魔导覆写",
                    "forced_action": forced_action,
                }
            )
            return normalized, None

        if cannon:
            error = require("魔导装置", 2, function_name="魔法加农炮")
            if error is not None:
                return details, error
            damage_aliases = {
                "物理": "physical",
                "physical": "physical",
                "风": "wind",
                "风系": "wind",
                "wind": "wind",
                "雷": "lightning",
                "雷系": "lightning",
                "lightning": "lightning",
                "冰": "ice",
                "冰系": "ice",
                "ice": "ice",
                "火": "fire",
                "火系": "fire",
                "fire": "fire",
                "土": "earth",
                "土系": "earth",
                "earth": "earth",
            }
            raw_damage = self._clean(details.get("damage_type"))
            if not raw_damage:
                return details, self._failure(
                    "perform_character_action",
                    "MAGICANNON_DAMAGE_TYPE_REQUIRED",
                    "制造魔法加农炮时还没有选择伤害类型。",
                    "询问玩家从物理、风、雷、冰、火、土中选择一种，再继续这次行动。",
                )
            damage_type = damage_aliases.get(raw_damage.lower()) or damage_aliases.get(raw_damage)
            if damage_type is None:
                return details, self._failure(
                    "perform_character_action",
                    "INVALID_MAGICANNON_DAMAGE_TYPE",
                    f"魔法加农炮不能选择伤害类型【{raw_damage}】。",
                    "从物理、风、雷、冰、火、土中选择一种。",
                )
            normalized.update(
                {"gadget_type": "魔导装置", "mode": "魔法加农炮", "damage_type": damage_type}
            )
            return normalized, None

        error = require("魔导装置", 3, function_name="法球")
        if error is not None:
            return details, error
        spell_name = self._clean(details.get("spell_name") or details.get("spell"))
        if not spell_name:
            return details, self._failure(
                "perform_character_action",
                "MAGISPHERE_SPELL_REQUIRED",
                "使用法球时还没有指定其中封存的法术。",
                "询问玩家要释放法球中记录的哪一个法术，再继续这次行动。",
            )
        normalized.update({"gadget_type": "魔导装置", "mode": "法球", "spell_name": spell_name})
        return normalized, None

    def _execute(
        self,
        context: GMToolExecutionContext,
        *,
        tool_name: str,
        action: Action,
        evidence: object,
        require_evidence: bool = True,
        require_character_actor: bool = True,
    ) -> GMToolReceipt:
        if require_evidence:
            evidence_error = self._validate_evidence(context, evidence, tool_name)
            if evidence_error is not None:
                return evidence_error
        if context.gate_status != "adventure":
            return self._failure(
                tool_name,
                "ADVENTURE_NOT_ACTIVE",
                "当前还没有进入可结算跑团行动的阶段。",
                "第零章内容使用第零章工具；进入第一章后再提交规则行动。",
            )
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        actor = self._clean(action.parameters.get("actor"))
        if require_character_actor and (not actor or not app.character_manager.exists(actor)):
            return self._failure(
                tool_name,
                "UNKNOWN_ACTOR",
                f"没有找到可结算角色【{actor or '未指定'}】。",
                "先调用get_gameplay_state，从当前角色中选择actor。",
            )
        if require_character_actor:
            ownership_error = self._validate_actor_ownership(runtime, context, actor)
            if ownership_error is not None:
                return ownership_error
        action.parameters["_speaker"] = context.speaker
        action.parameters.setdefault(
            "_enforce_turn_order",
            bool(app.conflict_manager.state.active),
        )
        action.parameters["_strict_tool_transaction"] = True

        recent_context = str(context.metadata.get("recent_public_context") or "")
        current_message = str(context.metadata.get("current_message") or "")
        route_decision = {
            "actor": actor,
            "intent_kind": "rules_action",
            "performed_action": True,
            "world_response_required": True,
            "source": "gm_tool_agent",
        }
        target = self._clean(action.parameters.get("target"))
        if target and app.world_state.resolve_npc_name(target):
            route_decision["npc_target"] = app.world_state.resolve_npc_name(target) or target
            route_decision["adjudication"] = "check_then_npc_response"
        with runtime.transaction_lock:
            transaction_snapshot = CampaignStateTransaction.capture(app, context.campaign_id)
            frame_before = app.scene_frame_manager.current_frame
            condition_states_before = {
                str(item.get("condition_id") or "").strip(): str(
                    item.get("player_fulfillment") or "pending"
                ).strip()
                for item in list(getattr(frame_before, "open_conditions", []) or [])
                if str(item.get("condition_id") or "").strip()
            }
            try:
                rest_context = None
                if action.action_type == ActionType.REST:
                    rest_context = self._begin_rest_scene(app, action)
                reply = app.run_structured_turn(
                    action,
                    current_message,
                    recent_public_context=recent_context,
                    speaker=context.speaker,
                    route_decision=route_decision,
                )
                if not str(reply or "").strip():
                    raise RuntimeError("规则行动已结算，但没有生成可发送的公开回复。")
                if rest_context is not None:
                    self._finish_rest_scene(app, action, rest_context)
                saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
            except Exception as exc:
                CampaignStateTransaction.restore(app, transaction_snapshot)
                return self._failure(
                    tool_name,
                    "RULE_ACTION_REJECTED",
                    str(exc) or "规则层拒绝了这个行动。",
                    "根据错误信息修正action参数；不要把失败描述成已经发生。",
                )

        fulfilled_condition: dict[str, object] = {}
        condition_payoff_due_from = ""
        required_followup_tools: list[str] = []
        required_followup_calls: list[dict[str, object]] = []
        frame_after = app.scene_frame_manager.current_frame
        scene = app.scene_manager.current_scene
        for condition in list(getattr(frame_after, "open_conditions", []) or []):
            condition_id = str(condition.get("condition_id") or "").strip()
            if (
                not condition_id
                or condition_states_before.get(condition_id) == "fulfilled"
                or str(condition.get("player_fulfillment") or "pending").strip()
                != "fulfilled"
            ):
                continue
            fulfilled_condition = dict(condition)
            requested_owner = self._clean(condition.get("npc"))
            condition_payoff_due_from = (
                app.world_state.resolve_npc_name(requested_owner) or requested_owner
            )
            persona = app.world_state.npc_personas.get(condition_payoff_due_from)
            if (
                not condition_payoff_due_from
                or scene is None
                or condition_payoff_due_from not in scene.participants
                or persona is None
            ):
                break
            entity_kind = str(
                getattr(persona, "entity_kind", "individual") or "individual"
            ).strip()
            followup_tool = (
                "decide_collective_response"
                if entity_kind == "collective"
                else "decide_npc_response"
            )
            required_followup_tools.append(followup_tool)
            required_followup_calls.append(
                {
                    "tool_name": followup_tool,
                    "arguments": {
                        "name": condition_payoff_due_from,
                        "actor": actor,
                        "condition_id": condition_id,
                    },
                    "authority_reason": (
                        "最终规则结果已确认玩家完整履行公开条件；"
                        "现在只能由条件所有者兑现promised_result。"
                    ),
                }
            )
            break

        pending = [
            {
                "window_id": window.window_id,
                "kind": window.kind,
                "owner": window.owner,
                "prompt": window.prompt,
                "options": list(window.options),
                "resolution_options": self._agent_decision_options(window),
                "blocking": bool(window.blocking),
                "roll_success": window.payload.get("roll_success"),
            }
            for window in app.interceptor.decision_window_manager.pending()
        ]
        gm_fumble_required = add_gm_fumble_followups(
            pending_decisions=pending,
            required_tools=required_followup_tools,
            required_calls=required_followup_calls,
        )
        followup_mode = required_followup_mode(
            required_followup_calls,
            independent_obligation_added=gm_fumble_required,
        )
        required_followup_tools = list(dict.fromkeys(required_followup_tools))
        check_receipt_id = str(
            getattr(app, "last_resolved_check_event_id", "") or ""
        ).strip()
        check_receipt: dict[str, object] = {}
        if check_receipt_id:
            event = next(
                (
                    item
                    for item in reversed(app.world_state.memory_events)
                    if item.event_id == check_receipt_id
                    and item.kind == "resolved_check"
                ),
                None,
            )
            if event is not None:
                check_receipt = {
                    "receipt_id": event.event_id,
                    **dict(event.payload),
                }
        return GMToolReceipt(
            tool_name=tool_name,
            ok=True,
            result={
                "action_type": action.action_type.value,
                "actor": actor,
                "committed_action": self._action_audit_payload(action),
                "pending_decisions": pending,
                "fulfilled_condition": dict(fulfilled_condition),
                "condition_payoff_due_from": condition_payoff_due_from,
                "allowed_followup_tools": list(required_followup_tools),
                "required_followup_tools": list(required_followup_tools),
                "required_followup_calls": list(required_followup_calls),
                "required_followup_mode": followup_mode,
                "check_receipt": check_receipt,
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply=str(reply).strip(),
            lock_public_reply=True,
        )

    @staticmethod
    def _begin_rest_scene(app: Any, action: Action) -> dict[str, object]:
        participants = [
            str(item or "").strip()
            for item in list(action.parameters.get("participants") or [])
            if str(item or "").strip()
        ]
        safe_source = str(action.parameters.get("safe_source") or "").strip()
        current = app.scene_manager.current_scene
        location = str(getattr(current, "location", "") or safe_source).strip()
        previous_name = str(getattr(current, "name", "") or "").strip()
        previous_objective = str(getattr(current, "objective", "") or "").strip()
        previous_summary = str(getattr(current, "summary", "") or "").strip()
        previous_type = getattr(current, "scene_type", SceneType.STANDARD)
        if previous_type in {
            SceneType.CONFLICT,
            SceneType.REST,
            SceneType.SESSION_ZERO,
        }:
            previous_type = SceneType.STANDARD
        rest_scene = app.scene_manager.start_scene(
            f"{safe_source}休息",
            SceneType.REST,
            location=location,
            participants=participants,
            objective="安全完成休息。",
        )
        return {
            "participants": participants,
            "location": location,
            "previous_name": previous_name,
            "previous_objective": previous_objective,
            "previous_summary": previous_summary,
            "previous_type": previous_type,
            "rest_scene_id": rest_scene.scene_id,
        }

    @staticmethod
    def _finish_rest_scene(
        app: Any,
        action: Action,
        rest_context: dict[str, object],
    ) -> None:
        safe_source = str(action.parameters.get("safe_source") or "").strip()
        participants = [
            str(item or "").strip()
            for item in list(rest_context.get("participants") or [])
            if str(item or "").strip()
        ]
        location = str(rest_context.get("location") or safe_source).strip()
        previous_name = str(rest_context.get("previous_name") or "").strip()
        previous_objective = str(
            rest_context.get("previous_objective") or ""
        ).strip()
        previous_summary = str(rest_context.get("previous_summary") or "").strip()
        previous_type = rest_context.get("previous_type")
        if not isinstance(previous_type, SceneType):
            previous_type = SceneType.STANDARD
        app.scene_manager.end_scene(f"众人在{safe_source}完成休息。")
        app.scene_manager.start_scene(
            (
                f"{previous_name}·休息之后"
                if previous_name
                else f"{location or safe_source}·休息之后"
            ),
            previous_type,
            location=location,
            participants=participants,
            objective=previous_objective,
            summary=" ".join(
                item
                for item in (
                    previous_summary,
                    f"众人已在{safe_source}完成休息。",
                )
                if item
            ),
        )

    @staticmethod
    def _action_audit_payload(action: Action) -> dict[str, object]:
        allowed = (
            "target",
            "targets",
            "spell_name",
            "chimerist_origin_species",
            "chosen_damage_type",
            "chosen_status",
            "chosen_attribute",
            "skill_name",
            "mode",
            "arcanum",
            "clock_name",
            "attributes",
            "target_number",
            "reasoning",
            "success_transition",
            "scene_condition_id",
        )
        payload: dict[str, object] = {"action_type": action.action_type.value}
        for key in allowed:
            if key not in action.parameters:
                continue
            value = action.parameters[key]
            if value not in (None, "", [], {}):
                payload[key] = deepcopy(value)
        return payload

    def _validate_actor_ownership(
        self,
        runtime: Any,
        context: GMToolExecutionContext,
        actor: str,
    ) -> GMToolReceipt | None:
        controls = self.host._player_character_control_map(runtime)
        owned = list(controls.get(context.speaker, []))
        known_owners = [
            player
            for player, heroes in controls.items()
            if actor in heroes
        ]
        if (
            known_owners
            and context.speaker not in known_owners
        ) or (owned and actor not in owned):
            return self._failure(
                "gameplay_action",
                "ACTOR_NOT_CONTROLLED_BY_SPEAKER",
                f"【{context.speaker}】不能替【{actor}】提交这个玩家选择。",
                "使用该玩家控制的角色，或等待角色操作者本人发言。",
            )
        return None

    def _validated_scene_action_details(
        self,
        context: GMToolExecutionContext,
        *,
        action_type: ActionType,
        actor: str,
        target: str,
        details: dict[str, object],
    ) -> tuple[dict[str, object], GMToolReceipt | None]:
        tool_name = "perform_scene_action"
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        normalized = deepcopy(details)

        if action_type == ActionType.REST:
            if app.conflict_manager.state.active:
                return details, self._failure(
                    tool_name,
                    "REST_DURING_CONFLICT",
                    "冲突仍在进行，不能开始休息。",
                    "先结束冲突或让队伍成功脱离危险，再提交休息。",
                )
            pending_travel_event = (
                app.travel_manager.pending_travel_event()
                if app.travel_manager is not None
                else None
            )
            if pending_travel_event is not None:
                return details, self._failure(
                    tool_name,
                    "TRAVEL_EVENT_PENDING",
                    "途中事件仍未解决，不能把当前危险直接跳过并开始休息。",
                    "先按已经发生的结果处理途中事件；确认抵达安全落脚点后再休息。",
                )
            current_scene = app.scene_manager.current_scene
            if (
                current_scene is not None
                and actor
                and actor not in current_scene.participants
            ):
                return details, self._failure(
                    tool_name,
                    "REST_ACTOR_NOT_IN_FOCUSED_SCENE",
                    f"【{actor}】不在当前镜头场景中，不能从异地替当前分队休息。",
                    "先切回该角色所在分队，或由当前场景中的角色发起休息。",
                )
            safe_source = self._clean(details.get("safe_source"))
            if not safe_source:
                return details, self._failure(
                    tool_name,
                    "REST_SAFE_SOURCE_REQUIRED",
                    "休息需要一处已经确认安全、可供休息的落脚点。",
                    "先确认safe_source；不能把‘想找地方休息’当成已经休息。",
                )
            raw_rest_type = self._clean(
                details.get("rest_type") or "wilderness"
            ).lower()
            rest_type = {
                "wilderness": "wilderness",
                "野外": "wilderness",
                "settlement": "settlement",
                "town": "settlement",
                "inn": "settlement",
                "定居点": "settlement",
                "城镇": "settlement",
                "旅馆": "settlement",
            }.get(raw_rest_type, "")
            if not rest_type:
                return details, self._failure(
                    tool_name,
                    "INVALID_REST_TYPE",
                    "休息类型必须是wilderness或settlement。",
                    "根据已经确认的安全落脚点重新选择。",
                )
            source_kind = self._clean(details.get("rest_source_kind")).lower()
            if not source_kind and rest_type == "wilderness" and safe_source == "魔法帐篷":
                source_kind = "tent"
            if source_kind not in {"tent", "hospitality", "lodging"}:
                return details, self._failure(
                    tool_name,
                    "REST_SOURCE_KIND_REQUIRED",
                    "还没有说明这次休息依靠魔法帐篷、好客地点还是付费旅馆。",
                    "填写rest_source_kind为tent、hospitality或lodging；不能把普通危险地点直接当成安全休息点。",
                )
            if rest_type == "wilderness" and source_kind not in {
                "tent",
                "hospitality",
            }:
                return details, self._failure(
                    tool_name,
                    "INVALID_WILDERNESS_REST_SOURCE",
                    "野外休息只能依靠魔法帐篷或已经确认的好客地点。",
                    "改用tent或hospitality。",
                )
            if rest_type == "settlement" and source_kind not in {
                "lodging",
                "hospitality",
            }:
                return details, self._failure(
                    tool_name,
                    "INVALID_SETTLEMENT_REST_SOURCE",
                    "定居点休息需要付费旅馆或已经确认的好客地点。",
                    "改用lodging或hospitality。",
                )
            if source_kind == "tent" and safe_source != "魔法帐篷":
                return details, self._failure(
                    tool_name,
                    "TENT_REST_SOURCE_MISMATCH",
                    "魔法帐篷休息的safe_source必须明确为魔法帐篷。",
                    "若是其他安全落脚点，改用hospitality。",
                )
            normalized["rest_type"] = rest_type
            normalized["safe_source"] = safe_source
            normalized["rest_source_kind"] = source_kind

            requested = details.get("participants")
            if requested not in (None, []):
                if not isinstance(requested, list):
                    return details, self._failure(
                        tool_name,
                        "REST_PARTICIPANTS_MUST_BE_ARRAY",
                        "休息参与者必须是角色名数组。",
                        "通常省略participants让规则层按同一分队推导。",
                    )
                participants = list(dict.fromkeys(self._clean(item) for item in requested if self._clean(item)))
                for name in participants:
                    if not app.character_manager.exists(name) or "pc" not in app.character_manager.get(name).traits:
                        return details, self._failure(
                            tool_name,
                            "REST_PARTICIPANT_UNKNOWN",
                            f"【{name}】不是可参与休息的玩家角色。",
                            "只列出当前战役中的玩家角色。",
                        )
                    if actor and name != actor and not app.scene_manager.actors_share_movement_origin(actor, name):
                        return details, self._failure(
                            tool_name,
                            "REST_PARTICIPANT_NOT_PRESENT",
                            f"【{name}】与【{actor}】不在同一分队，不能被这次休息恢复。",
                            "删除异地角色；让各分队分别在各自安全地点休息。",
                        )
                normalized["participants"] = participants
            else:
                participants = [
                    name
                    for name in list(getattr(current_scene, "participants", []) or [])
                    if app.character_manager.exists(name)
                    and "pc" in app.character_manager.get(name).traits
                ]
                if not participants and actor:
                    participants = [actor]
                normalized["participants"] = participants

            payer = self._clean(details.get("payer") or actor)
            if source_kind in {"tent", "lodging"}:
                if not payer:
                    return details, self._failure(
                        tool_name,
                        "REST_PAYER_REQUIRED",
                        "这次休息需要指定付款或支付物资的角色。",
                        "询问由谁支付，并在payer中使用该角色的标准名。",
                    )
                payer_error = self._validate_actor_ownership(runtime, context, payer)
                if payer_error is not None:
                    return details, payer_error
                if payer not in participants:
                    return details, self._failure(
                        tool_name,
                        "REST_PAYER_NOT_PRESENT",
                        f"付款者【{payer}】不在本次休息队伍中。",
                        "选择实际参与休息且由当前玩家控制的角色付款。",
                    )
                normalized["payer"] = payer
            if source_kind == "lodging":
                settlement_size = {
                    "village": "village",
                    "村庄": "village",
                    "town": "town",
                    "settlement": "town",
                    "小镇": "town",
                    "城镇": "town",
                    "city": "city",
                    "城市": "city",
                }.get(self._clean(details.get("settlement_size")).lower(), "")
                if not settlement_size:
                    return details, self._failure(
                        tool_name,
                        "LODGING_SIZE_REQUIRED",
                        "旅馆休息需要确定所在聚落规模，才能按每人每晚结算费用。",
                        "填写settlement_size为village、town或city。",
                    )
                normalized["settlement_size"] = settlement_size

            eligible_clocks = {
                clock.name
                for clock in app.clock_manager.all()
                if bool(clock.advance_on_rest)
                and str(clock.clock_type or "").strip().lower()
                in {"threat", "villain", "dungeon", "boss"}
                and str(clock.scope or "").strip().lower()
                in {"session", "campaign"}
                and str(clock.status or "").strip().lower() == "active"
                and clock.current < clock.max_segments
            }
            raw_clocks = details.get("threat_clocks")
            if raw_clocks is None:
                normalized["threat_clocks"] = sorted(eligible_clocks)
            elif not isinstance(raw_clocks, list):
                return details, self._failure(
                    tool_name,
                    "REST_CLOCKS_MUST_BE_ARRAY",
                    "休息推进命刻必须是名称数组。",
                    "通常省略threat_clocks，让规则层自动选择已登记的长期压力。",
                )
            else:
                requested_clocks = list(
                    dict.fromkeys(
                        self._clean(item)
                        for item in raw_clocks
                        if self._clean(item)
                    )
                )
                invalid_clocks = [
                    name for name in requested_clocks if name not in eligible_clocks
                ]
                if invalid_clocks:
                    return details, self._failure(
                        tool_name,
                        "REST_CLOCK_NOT_ELIGIBLE",
                        "这些命刻不会因休息自动推进：" + "、".join(invalid_clocks),
                        "不要由模型临时决定时间压力；只使用命刻自身登记的advance_on_rest规则。",
                    )
                normalized["threat_clocks"] = requested_clocks
            return normalized, None

        if action_type == ActionType.SHOP:
            raw_mode = self._clean(
                details.get("mode") or details.get("shop_action") or "buy"
            ).lower()
            if raw_mode in {
                "lodging",
                "inn",
                "rest_service",
                "旅馆",
                "住宿",
                "休息服务",
            }:
                return details, self._failure(
                    tool_name,
                    "LODGING_MUST_USE_REST",
                    "旅馆投宿必须与休息恢复作为同一笔规则事务结算。",
                    "改用Rest，并填写rest_source_kind=lodging、settlement_size和payer。",
                )
            if raw_mode in {
                "travel_service",
                "hire_transport",
                "rent_transport",
                "雇佣旅行服务",
                "旅行服务",
                "租交通",
            }:
                return details, self._failure(
                    tool_name,
                    "TRAVEL_SERVICE_MUST_USE_TRAVEL_TOOL",
                    "旅行服务必须在实际启程时按真实天数和同行人数统一结算。",
                    "改用travel_party；不要提前单独扣费。",
                )
            mode = {
                "buy": "buy",
                "购买": "buy",
                "restock": "restock",
                "补充": "restock",
                "补充库存": "restock",
                "inventory": "restock",
                "buy_transport": "buy_transport",
                "transport": "buy_transport",
                "vehicle": "buy_transport",
                "mount": "buy_transport",
                "购买交通": "buy_transport",
                "购买载具": "buy_transport",
                "购买坐骑": "buy_transport",
            }.get(raw_mode, "")
            if not mode:
                return details, self._failure(
                    tool_name,
                    "UNKNOWN_SHOP_MODE",
                    "没有这个购物结算方式。",
                    "使用buy、restock或buy_transport。",
                )
            if app.conflict_manager.state.active:
                return details, self._failure(
                    tool_name,
                    "SHOP_DURING_CONFLICT",
                    "冲突进行中不能进行普通购物。",
                    "先结束冲突或脱离危险。",
                )
            scene = app.scene_manager.current_scene
            if scene is None or actor not in scene.participants:
                return details, self._failure(
                    tool_name,
                    "SHOPPER_NOT_PRESENT",
                    f"【{actor or '未指定角色'}】不在当前购物场景。",
                    "先切回该角色所在场景或完成真实转场。",
                )
            normalized["mode"] = mode
            if mode == "buy" and not self._clean(
                details.get("item_name") or details.get("item")
            ):
                return details, self._failure(
                    tool_name,
                    "SHOP_ITEM_REQUIRED",
                    "购买行动还没有指定物品。",
                    "询问玩家要买什么，再填写item_name。",
                )
            if mode == "buy_transport" and not self._clean(
                details.get("transport") or details.get("item_name")
            ):
                return details, self._failure(
                    tool_name,
                    "TRANSPORT_NAME_REQUIRED",
                    "购买交通工具还没有指定具体类型。",
                    "调用get_travel_state，从可购买交通工具中选择。",
                )
            return normalized, None

        if action_type == ActionType.OPEN_CHEST:
            chest_name = self._clean(details.get("chest_name") or details.get("name"))
            if not chest_name:
                return details, self._failure(
                    tool_name,
                    "CHEST_NAME_REQUIRED",
                    "开箱行动需要指明已经发现并正在开启的宝箱。",
                    "填写chest_name；尚未发现或尚未打开时不要预先发奖。",
                )
            if self._clean(details.get("fixed_item")) or "fixed_zenit" in details:
                return details, self._failure(
                    tool_name,
                    "UNPREPARED_FIXED_CHEST_REWARD",
                    "普通开箱不能由表达模型临时指定物品或金币。",
                    "删除fixed_item与fixed_zenit，让规则层生成普通奖励；预设地下城奖励使用对应区域配置。",
                )
            rarity = self._clean(details.get("rarity") or "standard").lower()
            if rarity not in {"minor", "standard"}:
                return details, self._failure(
                    tool_name,
                    "UNPREPARED_RARE_CHEST",
                    "尚未登记的普通宝箱不能临时提升为稀有、首领或重大宝箱。",
                    "使用minor或standard；重大阶段奖励改用award_stage_reward，地下城奖励先写入区域配置。",
                )
            scene = app.scene_manager.current_scene
            if scene is None or actor not in scene.participants:
                return details, self._failure(
                    tool_name,
                    "CHEST_OPENER_NOT_PRESENT",
                    f"【{actor or '未指定角色'}】不在当前宝箱所在场景。",
                    "先切回角色所在场景或完成真实转场，再提交开箱。",
                )
            chest_id = self._clean(details.get("chest_id"))
            chest_key = chest_id or "|".join(
                [
                    str(scene.location or scene.name or "").strip(),
                    chest_name,
                ]
            )
            already_opened = next(
                (
                    event
                    for event in reversed(app.world_state.memory_events)
                    if event.kind == "chest_open_commit"
                    and str(event.payload.get("chest_key") or "") == chest_key
                ),
                None,
            )
            if already_opened is not None:
                return details, self._failure(
                    tool_name,
                    "CHEST_ALREADY_OPENED",
                    f"【{chest_name}】已经被开启并结算过奖励。",
                    "不要重复开箱；查询角色状态或现场公开事实确认已有奖励。",
                )
            normalized["chest_name"] = chest_name
            normalized["rarity"] = rarity
            normalized["_chest_key"] = chest_key
            return normalized, None

        if action_type == ActionType.EXPLORE_DUNGEON:
            if not app.dungeon_manager.state.active:
                return details, self._failure(
                    tool_name,
                    "DUNGEON_NOT_ACTIVE",
                    "当前没有已建立并开始的地下城探索。",
                    "先调用地下城准备/开始工具，再提交具体区域探索。",
                )
            area_name = self._clean(details.get("area_name"))
            area = next(
                (
                    item
                    for item in app.dungeon_manager.state.areas
                    if item.name == area_name
                ),
                None,
            )
            if area is None:
                return details, self._failure(
                    tool_name,
                    "DUNGEON_AREA_REQUIRED",
                    f"当前地下城没有区域【{area_name or '未指定'}】。",
                    "调用get_dungeon_state并逐字填写area_name。",
                )
            scene = app.scene_manager.current_scene
            if scene is None or actor not in scene.participants:
                return details, self._failure(
                    tool_name,
                    "DUNGEON_EXPLORER_NOT_PRESENT",
                    f"【{actor or '未指定角色'}】不在当前地下城场景。",
                    "先切回该角色所在的地下城分队，再提交探索。",
                )
            mode = self._clean(
                details.get("mode")
                or details.get("exploration_action")
                or details.get("dungeon_action")
                or "enter"
            )
            normalized["mode"] = mode
            normalized["area_name"] = area.name
            receipt_id = self._clean(details.get("check_receipt_id"))
            if details.get("success") is not None and not receipt_id:
                return details, self._failure(
                    tool_name,
                    "DUNGEON_SUCCESS_RECEIPT_REQUIRED",
                    "地下城检定结果不能由模型直接提交success。",
                    "先调用perform_check_action并绑定details.dungeon_area，再提交其check_receipt.receipt_id。",
                )
            if receipt_id:
                receipt_event = next(
                    (
                        event
                        for event in reversed(app.world_state.memory_events)
                        if event.event_id == receipt_id
                        and event.kind == "resolved_check"
                    ),
                    None,
                )
                if receipt_event is None:
                    return details, self._failure(
                        tool_name,
                        "DUNGEON_CHECK_RECEIPT_NOT_FOUND",
                        "没有找到可验证的最终检定回执。",
                        "使用perform_check_action成功回执中的check_receipt.receipt_id；待重掷结果不能使用。",
                    )
                receipt = dict(receipt_event.payload)
                if list(receipt.get("consumed_by") or []):
                    return details, self._failure(
                        tool_name,
                        "DUNGEON_CHECK_RECEIPT_ALREADY_USED",
                        "这份检定回执已经结算过一个地下城效果。",
                        "不要重复消费同一骰子结果；新的行动需要新的检定。",
                    )
                if str(receipt.get("actor") or "") != actor:
                    return details, self._failure(
                        tool_name,
                        "DUNGEON_CHECK_RECEIPT_ACTOR_MISMATCH",
                        "检定回执不属于当前行动者。",
                        "使用该角色本人最近完成、并绑定此区域的检定回执。",
                    )
                if str(receipt.get("dungeon_area") or "") != area.name:
                    return details, self._failure(
                        tool_name,
                        "DUNGEON_CHECK_RECEIPT_AREA_MISMATCH",
                        "检定回执没有绑定当前地下城区域。",
                        "重新进行检定，并在perform_check_action.details.dungeon_area中填写该区域标准名。",
                    )
                if str(receipt.get("scene_id") or "") != str(scene.scene_id or ""):
                    return details, self._failure(
                        tool_name,
                        "DUNGEON_CHECK_RECEIPT_SCENE_MISMATCH",
                        "检定回执来自另一个场景，不能在当前地下城消费。",
                        "使用当前地下城场景中最终结算的检定回执。",
                    )
                normalized["success"] = bool(receipt.get("success"))
                normalized["_check_receipt_id"] = receipt_id
                max_danger = 2 if bool(receipt.get("fumble")) else 1
                danger_segments = int(details.get("danger_segments") or 1)
                if danger_segments > max_danger:
                    return details, self._failure(
                        tool_name,
                        "DUNGEON_DANGER_SEGMENTS_EXCESSIVE",
                        f"这次检定最多只能推进危险{max_danger}格。",
                        "普通失败使用1格；只有大失败回执才可使用2格。",
                    )
            elif details.get("success") is not None:
                return details, self._failure(
                    tool_name,
                    "DUNGEON_SUCCESS_RECEIPT_REQUIRED",
                    "地下城success缺少最终检定回执。",
                    "删除success，或先完成并引用对应检定。",
                )
            if self._clean(details.get("fixed_item")):
                if self._clean(details.get("fixed_item")) != self._clean(area.reward_item):
                    return details, self._failure(
                        tool_name,
                        "DUNGEON_REWARD_ITEM_MISMATCH",
                        "提交的固定物品与该区域预设奖励不一致。",
                        "省略fixed_item让规则层读取区域奖励。",
                    )
            if "fixed_zenit" in details and details.get("fixed_zenit") != area.reward_zenit:
                return details, self._failure(
                    tool_name,
                    "DUNGEON_REWARD_ZENIT_MISMATCH",
                    "提交的固定金币与该区域预设奖励不一致。",
                    "省略fixed_zenit让规则层读取区域奖励。",
                )
            return normalized, None

        if action_type == ActionType.SELL_ITEM:
            item_name = self._clean(details.get("item_name"))
            if not item_name:
                return details, self._failure(
                    tool_name,
                    "SELL_ITEM_REQUIRED",
                    "出售行动还没有指定物品。",
                    "询问玩家要出售哪件库存物品，再填写item_name。",
                )
            normalized["item_name"] = item_name
            if "price_ratio" in details:
                try:
                    submitted_ratio = float(details.get("price_ratio"))
                except (TypeError, ValueError):
                    submitted_ratio = -1
                if submitted_ratio != 0.5:
                    return details, self._failure(
                        tool_name,
                        "NONSTANDARD_SALE_PRICE_REQUIRES_DEDICATED_RULING",
                        "普通出售按原价一半结算，表达模型不能临时改写售价。",
                        "删除price_ratio使用标准半价；特殊议价应先完成对应裁定，再使用专门的经济调整能力。",
                    )
            normalized["price_ratio"] = 0.5
            if app.conflict_manager.state.active:
                return details, self._failure(
                    tool_name,
                    "SELL_DURING_CONFLICT",
                    "冲突进行中不能完成普通出售交易。",
                    "先结束冲突或脱离危险。",
                )
            scene = app.scene_manager.current_scene
            if scene is None or actor not in scene.participants:
                return details, self._failure(
                    tool_name,
                    "SELLER_NOT_PRESENT",
                    f"【{actor or '未指定角色'}】不在当前交易场景。",
                    "先切回该角色所在场景或完成真实转场。",
                )
            return normalized, None

        if action_type == ActionType.MANAGE_BOND:
            bond_target = self._clean(target or details.get("target") or details.get("bond_target"))
            if not bond_target:
                return details, self._failure(
                    tool_name,
                    "BOND_TARGET_REQUIRED",
                    "羁绊变更需要明确对象。",
                    "根据玩家明确表达填写target；不要替玩家决定情感。",
                )
            normalized["target"] = bond_target
            return normalized, None

        return normalized, None

    @classmethod
    def _validated_triggered_commitment(
        cls,
        app: Any,
        *,
        frame: Any | None,
        companions: list[str],
        arguments: dict[str, object],
        tool_name: str,
    ) -> tuple[dict[str, str] | None, str, GMToolReceipt | None]:
        commitment_id = cls._clean(arguments.get("commitment_id"))
        requested_responder = cls._clean(arguments.get("commitment_responder"))
        if not commitment_id and not requested_responder:
            return None, "", None
        if not commitment_id or not requested_responder:
            return None, "", cls._failure(
                tool_name,
                "NPC_COMMITMENT_TRIGGER_FIELDS_INCOMPLETE",
                "承诺触发必须同时提交commitment_id与commitment_responder。",
                "读取当前pending_npc_commitments，使用准确ID和实际同行的兑现者；否则同时删除这两个字段。",
            )
        commitment = (
            app.scene_frame_manager.npc_deferred_commitment_manager.find_pending(
                frame,
                commitment_id,
            )
        )
        if commitment is None:
            return None, "", cls._failure(
                tool_name,
                "NPC_COMMITMENT_NOT_FOUND",
                f"短期承诺【{commitment_id}】不存在或已经结束。",
                "重新读取scene.pending_npc_commitments；不要拼接、猜测或复用过期ID。",
            )
        trigger_status = cls._clean(commitment.get("trigger_status")).lower()
        if trigger_status not in {"", "waiting"}:
            responder = cls._clean(commitment.get("trigger_responder"))
            return None, "", cls._failure(
                tool_name,
                "NPC_COMMITMENT_TRIGGER_ALREADY_REACHED",
                f"短期承诺【{commitment_id}】已经到达触发点，不能重复提交移动。",
                (
                    f"直接调用decide_npc_response，让【{responder}】以准确commitment_id兑现。"
                    if responder
                    else "读取承诺记录并让其中的trigger_responder当场兑现，不要再次消耗行动轮。"
                ),
                result={
                    "commitment_id": commitment_id,
                    "commitment_responder": responder,
                },
            )
        responder = app.world_state.resolve_npc_name(requested_responder)
        if not responder:
            return None, "", cls._failure(
                tool_name,
                "NPC_COMMITMENT_RESPONDER_UNKNOWN",
                f"没有找到承诺兑现者【{requested_responder}】。",
                "使用当前NPC档案中的稳定名称；人物尚未登场时不能让其兑现现场承诺。",
            )
        if responder not in companions:
            return None, "", cls._failure(
                tool_name,
                "NPC_COMMITMENT_RESPONDER_NOT_MOVING",
                f"承诺兑现者【{responder}】不在本次同行NPC中。",
                "只有实际一同抵达触发点的NPC才能作为commitment_responder。",
            )
        persona = app.world_state.npc_personas.get(responder)
        if persona is None or str(
            getattr(persona, "entity_kind", "individual") or "individual"
        ) != "individual":
            return None, "", cls._failure(
                tool_name,
                "NPC_COMMITMENT_INDIVIDUAL_REQUIRED",
                f"【{responder}】不是可独立决定并表达的单个NPC。",
                "选择承诺中实际负责兑现的单个在场NPC；集体行动须使用对应集体能力。",
            )
        return commitment, responder, None

    @classmethod
    def _validated_action_type(
        cls,
        arguments: dict[str, object],
        allowed: set[ActionType],
        tool_name: str,
    ) -> tuple[ActionType, GMToolReceipt | None]:
        raw = cls._clean(arguments.get("action_type"))
        try:
            action_type = ActionType(raw)
        except ValueError:
            return ActionType.NARRATE, cls._failure(
                tool_name,
                "UNKNOWN_ACTION_TYPE",
                f"未知规则动作【{raw or '未指定'}】。",
                "使用当前工具schema列出的action_type。",
            )
        if action_type not in allowed:
            return action_type, cls._failure(
                tool_name,
                "ACTION_TYPE_WRONG_TOOL",
                f"动作【{action_type.value}】不属于这个工具。",
                "根据动作类别改用对应的perform_*工具。",
            )
        return action_type, None

    @classmethod
    def _validated_details(
        cls,
        value: object,
        *,
        tool_name: str,
        forbidden: set[str] | None = None,
    ) -> tuple[dict[str, object], GMToolReceipt | None]:
        if value in (None, ""):
            return {}, None
        if not isinstance(value, dict):
            return {}, cls._failure(
                tool_name,
                "ACTION_DETAILS_MUST_BE_OBJECT",
                "行动details必须是JSON对象。",
                "把规则参数按字段提交，不要塞入自然语言JSON字符串。",
            )
        blocked = sorted(set(value) & (cls._PROTECTED_PARAMETER_KEYS | set(forbidden or set())))
        if blocked:
            return {}, cls._failure(
                tool_name,
                "PROTECTED_ACTION_PARAMETER",
                "行动包含不能由语义层直接提交的字段：" + "、".join(blocked),
                "删除这些字段；公开回复、命刻副作用和回合约束由对应组件负责。",
            )
        return deepcopy(value), None

    @classmethod
    def _validate_evidence(
        cls,
        context: GMToolExecutionContext,
        value: object,
        tool_name: str,
    ) -> GMToolReceipt | None:
        if not str(value or "").strip():
            return cls._failure(
                tool_name,
                "EVIDENCE_REQUIRED",
                "行动缺少当前消息中的逐字依据。",
                "从current_message复制连续原文，不得使用路由摘要。",
            )
        if not is_current_message_evidence(context, value):
            return cls._failure(
                tool_name,
                "EVIDENCE_NOT_LITERAL",
                "行动依据不是当前玩家消息中的连续原文。",
                "重新复制current_message里的原句；不得概括、补全或改变时态。",
            )
        return None

    @staticmethod
    def _character_species(character: Character) -> str:
        aliases = {
            "野兽": "野兽",
            "beast": "野兽",
            "构装体": "构装体",
            "构造体": "构装体",
            "construct": "构装体",
            "恶魔": "恶魔",
            "demon": "恶魔",
            "元素": "元素",
            "elemental": "元素",
            "人型": "人型",
            "humanoid": "人型",
            "怪物": "怪物",
            "monster": "怪物",
            "植物": "植物",
            "plant": "植物",
            "不死族": "不死族",
            "undead": "不死族",
        }
        traits = {
            str(trait).strip().lower()
            for trait in character.traits
            if str(trait).strip()
        }
        for raw, label in aliases.items():
            if raw.lower() in traits:
                return label
        return ""

    @staticmethod
    def _clean(value: object) -> str:
        return " ".join(str(value or "").split()).strip()

    @staticmethod
    def _clean_multiline(value: object) -> str:
        return "\n".join(
            line.rstrip()
            for line in str(value or "").strip().splitlines()
            if line.strip()
        ).strip()

    @classmethod
    def _first_sentence(cls, value: object) -> str:
        text = cls._clean(value)
        if not text:
            return ""
        match = re.match(r"^(.+?[。！？!?])(?:\s|$)", text)
        return (match.group(1) if match else text).strip()

    @staticmethod
    def _failure(
        tool_name: str,
        code: str,
        message: str,
        hint: str,
        *,
        result: dict[str, object] | None = None,
    ) -> GMToolReceipt:
        return GMToolReceipt(
            tool_name=tool_name,
            ok=False,
            error_code=code,
            message=message,
            correction_hint=hint,
            retryable=True,
            result=dict(result or {}),
            public_fallback_reply="这个行动还没有结算，我需要先把规则参数确认清楚。",
        )
