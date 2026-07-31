from __future__ import annotations

from copy import deepcopy
from typing import Any, Protocol
from uuid import uuid4

from fu_gm.gm_evidence import is_current_message_evidence
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolReceipt,
    GMToolRegistry,
)
from fu_gm.models import HeroDraft
from fu_gm.skill_library import get_skill_reference


class SessionZeroToolHost(Protocol):
    def _runtime(self, campaign_id: str, *, auto_load: bool = True) -> Any: ...

    def _autosave_campaign(self, runtime: Any, campaign_id: str) -> str: ...

    def _adventure_readiness_snapshot(
        self,
        runtime: Any,
        *,
        materialize_confirmed_characters: bool = False,
    ) -> dict[str, Any]: ...


class GMSessionZeroToolService:
    """Validated Session 0 writes chosen by the GM agent.

    The service never infers meaning from prose.  It verifies provenance,
    field shape, player ownership and lifecycle preconditions, then commits one
    structured command under the campaign transaction lock.
    """

    _SCALAR_FIELDS = {
        "campaign_title",
        "continent_name",
        "world_style",
        "world_shape",
        "map_card",
        "travel_day_length",
        "magic_tech_role",
        "group_concept",
        "starting_region",
        "party_dynamic",
        "description_style",
        "violence_guideline",
        "romance_guideline",
        "selected_first_act_id",
        "selected_first_act_summary",
    }
    _LIST_FIELDS = {
        "tone_preferences",
        "playstyle_themes",
        "evil_guidelines",
        "consensus_notes",
        "core_themes",
        "historical_events",
        "villain_seeds",
        "villain_mirrors",
        "mysteries",
        "world_threats",
        "starting_bond_suggestions",
    }
    _DICT_FIELDS = {
        "pillars",
        "major_locations",
        "kingdoms",
        "factions",
        "optional_rules",
    }
    _MAP_LOCATION_FIELD = "map_locations"
    _MAP_FEATURE_TYPES = (
        "settlement",
        "country",
        "mountain_range",
        "forest",
        "archipelago",
        "inland_sea",
        "lake",
        "coast",
        "region",
        "landmark",
        "fortress",
    )
    _MAP_POSITIONS = (
        "north",
        "northeast",
        "east",
        "southeast",
        "south",
        "southwest",
        "west",
        "northwest",
        "center",
    )
    _CONTRIBUTION_FIELDS = {
        "kingdoms": ("kingdom_contributors", "kingdom_contributions"),
        "major_locations": ("kingdom_contributors", "kingdom_contributions"),
        "map_locations": ("kingdom_contributors", "kingdom_contributions"),
        "historical_events": (
            "historical_event_contributors",
            "historical_event_contributions",
        ),
        "mysteries": ("mystery_contributors", "mystery_contributions"),
        "world_threats": ("threat_contributors", "threat_contributions"),
    }
    _TOPIC_CODES = {
        "kingdom": "kingdom_contributions",
        "historical_event": "historical_event_contributions",
        "mystery": "mystery_contributions",
        "threat": "threat_contributions",
    }
    _HERO_SCALARS = {"hero_name", "identity", "theme", "origin"}
    _HERO_DICTS = {"classes", "attributes", "skills", "skill_options", "equipment_slots"}
    _HERO_LISTS = {
        "bonds",
        "spells",
        "bound_arcana",
        "equipment",
        "notes",
        "open_questions",
        "remove_bonds",
        "remove_spells",
        "remove_bound_arcana",
        "remove_equipment",
        "remove_notes",
        "remove_classes",
        "remove_attributes",
        "remove_skills",
        "remove_skill_options",
        "remove_fields",
    }
    _HERO_BOOLEANS = {"replace_skills", "increment_skills"}
    _PUBLIC_UPDATE_CATEGORIES = {
        "campaign_title": "战役名称",
        "continent_name": "世界名称",
        "world_style": "世界风格",
        "magic_tech_role": "魔法与科技的关系",
        "group_concept": "小队概念",
        "starting_region": "起始地区",
        "party_dynamic": "队伍关系",
        "description_style": "描述风格",
        "tone_preferences": "基调偏好",
        "playstyle_themes": "玩法主题",
        "consensus_notes": "共识",
        "core_themes": "世界主题",
        "historical_events": "重大历史事件",
        "mysteries": "世界奥秘",
        "world_threats": "世界威胁",
        "major_locations": "地点",
        "map_locations": "地图地点",
        "kingdoms": "国家与势力",
        "factions": "组织与势力",
        "selected_first_act_id": "第一幕",
        "selected_first_act_summary": "第一幕",
    }

    def __init__(self, host: SessionZeroToolHost) -> None:
        self.host = host

    @classmethod
    def _public_update_categories(cls, updates: dict[str, object]) -> list[str]:
        categories: list[str] = []
        for field_name in updates:
            label = cls._PUBLIC_UPDATE_CATEGORIES.get(field_name)
            if label and label not in categories:
                categories.append(label)
        return categories

    @staticmethod
    def _public_update_confirmation(categories: list[str]) -> str:
        if not categories:
            return "这条设定记下了。"
        if len(categories) == 1:
            return f"好，{categories[0]}记下了。"
        if len(categories) <= 4:
            joined = "、".join(categories[:-1]) + f"和{categories[-1]}"
            return f"好，{joined}都记下了。"
        return "好，这些世界设定都记下了。"

    @staticmethod
    def _string_array_schema() -> dict[str, object]:
        return {"type": "array", "items": {"type": "string", "minLength": 1}}

    @staticmethod
    def _string_map_schema(description: str) -> dict[str, object]:
        return {
            "type": "object",
            "description": description,
            "additionalProperties": {"type": "string", "minLength": 1},
        }

    @classmethod
    def _world_updates_schema(cls) -> dict[str, object]:
        string_properties = {
            name: {"type": "string", "minLength": 1}
            for name in sorted(cls._SCALAR_FIELDS)
        }
        string_properties["selected_first_act_id"] = {
            "type": "string",
            "minLength": 1,
            "description": "仅填写state_summary.first_act_candidates中现有候选的id；自定义第一幕标题不是候选id。",
        }
        string_properties["selected_first_act_summary"] = {
            "type": "string",
            "minLength": 1,
            "description": "全桌直接确认自定义第一幕时，把标题、前提与目标一并写在这里；无需另填候选id或逐人投票。",
        }
        string_properties["violence_guideline"] = {
            "type": "string",
            "minLength": 1,
            "description": (
                "全桌对暴力表现强度的总体约定；显式‘界限：’或‘帷幕：’内容不得放这里，"
                "应另调用record_safety_boundary。"
            ),
        }
        string_properties["romance_guideline"] = {
            "type": "string",
            "minLength": 1,
            "description": (
                "全桌对浪漫与亲密情节表现强度的总体约定；显式‘界限：’或‘帷幕：’内容不得放这里，"
                "应另调用record_safety_boundary。"
            ),
        }
        string_properties.update(
            {
                name: cls._string_array_schema()
                for name in sorted(cls._LIST_FIELDS)
            }
        )
        string_properties["historical_events"]["description"] = (
            "塑造世界当前形态的重大历史事件。玩家在国家、地区或势力说明中叙述的"
            "过去事件若同时属于其历史贡献，必须另列于此；不能因为相同事实已经写进"
            "kingdoms、major_locations或factions说明而省略。"
        )
        string_properties["mysteries"]["description"] = (
            "玩家希望冒险中探索、答案尚未确定的世界奥秘；与地点或国家说明中已经"
            "公开成立的事实分开记录。"
        )
        string_properties["world_threats"]["description"] = (
            "正在危及地区、国家或世界未来的客观威胁；不要把单纯的玩法偏好、"
            "假设性选择或角色个人担忧误写为世界威胁。"
        )
        string_properties.update(
            {
                "pillars": cls._string_map_schema("八大支柱或世界支柱：名称到说明。"),
                "major_locations": cls._string_map_schema(
                    "地点名称到说明；有方位或地形要求时优先使用map_locations。"
                ),
                "kingdoms": cls._string_map_schema(
                    "国家或政治共同体的简短正式名称到说明；键只写实体名，"
                    "例如写‘树誓村社’，不要写‘沉默森林周边的树誓村社’。"
                ),
                "factions": cls._string_map_schema("势力名称到势力说明。"),
                "optional_rules": {
                    "type": "object",
                    "description": "可选规则名称到启用状态或详细设置。",
                    "additionalProperties": {
                        "anyOf": [
                            {"type": "boolean"},
                            {
                                "type": "object",
                                "properties": {
                                    "enabled": {"type": "boolean"},
                                    "note": {"type": "string"},
                                    "source": {"type": "string"},
                                },
                                "required": ["enabled"],
                                "additionalProperties": False,
                            },
                        ]
                    },
                },
                "map_locations": {
                    "type": "array",
                    "description": (
                        "需要参与地图布局的地点。每项应保留玩家给出的方位、地形、"
                        "相对位置和是否绘制图标。并列的东南西北清单分别写position_hint，"
                        "不能把清单中相邻项目臆造为relative_to。"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "minLength": 1},
                            "description": {"type": "string"},
                            "feature_type": {
                                "type": "string",
                                "enum": list(cls._MAP_FEATURE_TYPES),
                                "description": (
                                    "按玩家语义选择地图引擎类型：驿站/村镇用settlement，"
                                    "海岸用coast；不要创造waystation、coastline等同义枚举。"
                                ),
                            },
                            "terrain": {"type": "string"},
                            "position_hint": {
                                "type": "string",
                                "enum": list(cls._MAP_POSITIONS),
                                "description": (
                                    "玩家明确给出的绝对方位使用英文规范值；未知时省略。"
                                ),
                            },
                            "relative_to": {
                                "type": "string",
                                "description": (
                                    "仅在原句明确说明此地点与另一具名地点的关系时填写；"
                                    "并列清单不构成相对关系。"
                                ),
                            },
                            "relative_position": {
                                "type": "string",
                                "enum": list(cls._MAP_POSITIONS),
                                "description": (
                                    "仅在原句明确给出相对方向时填写；‘沿某海岸’不等于center。"
                                ),
                            },
                            "faction": {"type": "string"},
                            "draw_icon": {
                                "type": "boolean",
                                "description": (
                                    "settlement、country、fortress通常为true；"
                                    "山脉、森林、海域、海岸等自然地理为false。"
                                ),
                            },
                        },
                        "required": ["name", "feature_type"],
                        "additionalProperties": False,
                    },
                },
            }
        )
        return {
            "properties": string_properties,
            "minProperties": 1,
            "additionalProperties": False,
        }

    @classmethod
    def _hero_patch_schema(cls) -> dict[str, object]:
        string_list = cls._string_array_schema()
        return {
            "properties": {
                "hero_name": {"type": "string", "minLength": 1},
                "identity": {"type": "string", "minLength": 1},
                "theme": {"type": "string", "minLength": 1},
                "origin": {"type": "string", "minLength": 1},
                "classes": {
                    "type": "object",
                    "description": "中文职业名到该职业等级。",
                    "additionalProperties": {"type": "integer", "minimum": 0},
                },
                "attributes": {
                    "type": "object",
                    "description": "仅使用中文属性名：敏捷、洞察、力量、意志。",
                    "properties": {
                        name: {"type": "integer", "enum": [6, 8, 10, 12]}
                        for name in ("敏捷", "洞察", "力量", "意志")
                    },
                    "additionalProperties": False,
                },
                "skills": {
                    "type": "object",
                    "description": (
                        "完整中文职业技能名到已获取次数；键是技能名，不是职业名。"
                        "例如‘游说家技能选谴责’写为{\"谴责\":1}。"
                        "玩家再次获取同一技能时仍提交本次次数1，并令increment_skills为true。"
                    ),
                    "additionalProperties": {"type": "integer", "minimum": 0},
                },
                "skill_options": {
                    "type": "object",
                    "description": (
                        "只有技能规则要求的附带选择才放这里；普通职业技能选择不属于附带选择。"
                        "键为完整中文技能名，值为按获取顺序排列的选择。"
                        "例如便携装置1级选择魔导装置：{\"便携装置\":[\"魔导装置\"]}；"
                        "再次选择同类表示把该装置升到下一阶。"
                        "‘游说家技能选谴责’必须写入skills，绝不能写成"
                        "skill_options:{\"游说家\":[\"谴责\"]}。"
                    ),
                    "additionalProperties": string_list,
                },
                "equipment_slots": {
                    "type": "object",
                    "description": (
                        "可选的开场穿戴栏位，不是已购买装备清单。键只能是"
                        "main_hand、off_hand、armor、shield；值必须来自equipment中的已购买装备，"
                        "空字符串表示该栏留空。没有明确指定时不要提交，规则层会自动给出合理默认穿戴。"
                    ),
                    "properties": {
                        name: {"type": "string"}
                        for name in ("main_hand", "off_hand", "armor", "shield")
                    },
                    "additionalProperties": False,
                },
                **{name: string_list for name in sorted(cls._HERO_LISTS)},
                **{
                    name: {"type": "boolean"}
                    for name in sorted(cls._HERO_BOOLEANS)
                },
            },
            "minProperties": 1,
            "additionalProperties": False,
        }

    def register_tools(self, registry: GMToolRegistry) -> None:
        registry.register(
            GMToolDefinition(
                name="get_session_zero_readiness",
                description=(
                    "只读查询第零章距离完成、或进入第一章还缺哪些内容。"
                    "返回世界共创缺项、逐玩家贡献缺项和角色建卡校验结果；"
                    "玩家问‘还缺什么才能开启第一章’时只使用本工具，"
                    "不要用get_hero_drafts或get_session_status拼凑答案。"
                ),
                handler=self.get_session_zero_readiness,
                parameters=(
                    GMToolParameter(
                        "campaign_id",
                        "string",
                        "可选。明确要查看的战役；通常省略以查询当前团。",
                    ),
                ),
            )
        )
        registry.register(
            GMToolDefinition(
                name="propose_session_zero_update",
                description=(
                    "仅在玩家直接要求GM暂存或追踪时，把尚未确认的第零章世界或小队设定保存为待定提案。"
                    "普通玩家向其他玩家说‘大家觉得呢’、‘我有个点子’时不要调用，也不要写入任何状态。"
                    "玩家直接表达自己希望体验的基调、主题、表现方式或玩法偏好不是待定提案。"
                ),
                handler=self.propose_update,
                parameters=(
                    GMToolParameter("summary", "string", "面向桌面的简短提案摘要。", required=True),
                    GMToolParameter(
                        "updates",
                        "object",
                        "提案若通过将应用的结构化世界更新。",
                        required=True,
                        schema_details=self._world_updates_schema(),
                    ),
                    GMToolParameter("evidence", "string", "当前玩家消息中的逐字证据。", required=True, source="current_message"),
                ),
                side_effect="write_pending",
            )
        )
        registry.register(
            GMToolDefinition(
                name="commit_session_zero_update",
                description=(
                    "写入玩家已经明确贡献或全桌已经确认的第零章世界、小队和第一幕设定。"
                    "玩家明确表达自己希望体验的基调、主题、表现方式或玩法偏好也直接写入。"
                    "仍在玩家之间询问意见的共享设定不得写入；只有明确请GM暂存时才改用propose_session_zero_update。"
                    "同一句包含多个已确认类别时必须全部提交到各自顶层字段；"
                    "历史即使也写进国家或势力说明，仍要另写historical_events。"
                ),
                handler=self.commit_update,
                parameters=(
                    GMToolParameter(
                        "updates",
                        "object",
                        "要正式提交的结构化世界更新。",
                        required=True,
                        schema_details=self._world_updates_schema(),
                    ),
                    GMToolParameter("evidence", "string", "当前玩家消息中的逐字证据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="confirm_session_zero_proposal",
                description="在玩家明确同意某项待定提案后，将该提案原子转为正式设定并移除待定项。",
                handler=self.confirm_proposal,
                parameters=(
                    GMToolParameter("proposal_id", "string", "状态摘要中现存的提案ID。", required=True),
                    GMToolParameter("evidence", "string", "当前玩家明确同意的逐字证据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="mark_session_zero_topic_complete",
                description="玩家明确跳过或表示对某项没有想法时，记录该玩家已完成此项贡献。",
                handler=self.mark_topic_complete,
                parameters=(
                    GMToolParameter(
                        "topic",
                        "string",
                        "被跳过或完成的贡献主题。",
                        required=True,
                        enum=tuple(self._TOPIC_CODES),
                    ),
                    GMToolParameter("evidence", "string", "当前玩家消息中的逐字证据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="set_session_zero_nudge_preference",
                description=(
                    "仅在当前玩家明确表示以后不要再被GM主动点名询问第零章贡献，"
                    "或明确恢复接受这类主动提问时使用。只修改当前发言者本人。"
                    "“这项先跳过/这个问题没想法”只应使用mark_session_zero_topic_complete，"
                    "不能据此关闭今后的主动提问。"
                ),
                handler=self.set_nudge_preference,
                parameters=(
                    GMToolParameter(
                        "enabled",
                        "boolean",
                        "true表示允许GM在讨论停滞时主动邀请本人贡献；false表示不要主动点名本人。",
                        required=True,
                    ),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前玩家明确表达长期主动提问偏好的逐字证据。",
                        required=True,
                        source="current_message",
                    ),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="pause_session_zero_nudges",
                description=(
                    "当前玩家明确表示正在考虑、需要一点时间或稍后再回答当前第零章问题时使用。"
                    "这是仅持续到下一条玩家消息的临时安静等待，不代表跳过当前主题，"
                    "也不修改玩家今后是否接受主动提问的长期偏好。"
                ),
                handler=self.pause_nudges,
                parameters=(
                    GMToolParameter(
                        "topic",
                        "string",
                        "正在考虑的当前议题；无法准确命名时可留空。",
                    ),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前玩家表示需要时间思考的逐字证据。",
                        required=True,
                        source="current_message",
                    ),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="update_hero_draft",
                description=(
                    "按玩家当前提供的一项或多项信息增量更新角色草稿。"
                    "不要求一次填完，也不要因为仍有缺项而替玩家补值。"
                    "arguments只能包含subject和patch；只把本句新增或纠正的字段放入patch，"
                    "不得把现有草稿字段重新抄入patch。"
                    "玩家选择某职业的一项技能时，patch.skills的键必须是该技能的完整中文名；"
                    "职业名不能作为skills或skill_options的键。skill_options仅记录技能自身要求的附带选择。"
                    "首次选择示例：arguments={\"subject\":\"伊莉雅\",\"patch\":{\"skills\":{\"保镖\":1}}}。"
                    "再次获取草稿中已有的同名技能时，increment_skills必须放在patch内部，绝不能作为arguments参数。"
                    "equipment表示买下并放入库存的初始装备，equipment_slots只在玩家明确指定开场穿戴时使用。"
                    "玩家为已有外观补充数值模板时，必须用remove_equipment删除旧占位，并添加"
                    "“外观名（标准装备模板）”，不能把模板当成第三件物品追加。"
                ),
                handler=self.update_hero_draft,
                parameters=(
                    GMToolParameter("subject", "string", "玩家名或现有角色名；本人可填发言者。", required=True),
                    GMToolParameter(
                        "patch",
                        "object",
                        "角色草稿增量字段。",
                        required=True,
                        schema_details=self._hero_patch_schema(),
                    ),
                    GMToolParameter("evidence", "string", "当前玩家消息中的逐字证据。", required=True, source="current_message"),
                ),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )
        registry.register(
            GMToolDefinition(
                name="confirm_hero_draft",
                description=(
                    "玩家明确表示角色草稿已经定稿、确认角色或要求正式建卡时使用。"
                    "只有草稿已经满足创建规则才会确认；不得替仍在讨论的玩家确认。"
                ),
                handler=self.confirm_hero_draft,
                parameters=(
                    GMToolParameter(
                        "subject",
                        "string",
                        "要确认的玩家名或现有角色名；本人可填发言者。",
                        required=True,
                    ),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前玩家明确确认定稿的逐字证据。",
                        required=True,
                        source="current_message",
                    ),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="record_safety_boundary",
                description=(
                    "记录玩家明确声明的界限或帷幕。玩家明确使用‘界限：’或‘帷幕：’时按其标签记录，"
                    "并保留‘不详细描写’等强度说明；没有显式标签时，界限通常是不出现，帷幕通常是可以存在但淡出处理。"
                    "只有安全准则语义明确时调用，不因剧情中普通提到这些词而调用。"
                ),
                handler=self.record_safety_boundary,
                parameters=(
                    GMToolParameter(
                        "kind",
                        "string",
                        "line为界限，veil为帷幕。",
                        required=True,
                        enum=("line", "veil"),
                    ),
                    GMToolParameter("content", "string", "需要禁止或淡出的具体元素。", required=True),
                    GMToolParameter("evidence", "string", "当前玩家消息中的逐字证据。", required=True, source="current_message"),
                    GMToolParameter("anonymous", "boolean", "是否匿名记录。"),
                ),
                side_effect="write",
            )
        )

    def state_summary(self, context: GMToolExecutionContext) -> dict[str, object]:
        runtime = self.host._runtime(context.campaign_id)
        manager = runtime.app.session_zero_manager
        state = manager.state
        world = manager.state.world
        map_locations = []
        for item in list(runtime.app.world_state.map_locations.values())[:24]:
            map_locations.append(
                {
                    "name": str(getattr(item, "name", "") or ""),
                    "feature_type": str(getattr(item, "feature_type", "") or ""),
                    "position_hint": str(getattr(item, "position_hint", "") or ""),
                    "relative_to": str(getattr(item, "relative_to", "") or ""),
                    "relative_position": str(
                        getattr(item, "relative_position", "") or ""
                    ),
                    "terrain": str(getattr(item, "terrain", "") or ""),
                    "description": str(getattr(item, "description", "") or "")[:240],
                    "faction": str(getattr(item, "faction", "") or ""),
                }
            )
        return {
            "active": bool(state.active),
            "stage": state.stage.value,
            "missing_topics": manager.missing_topics() if state.active else [],
            "participants": [participant.name for participant in state.participants],
            "participant_contribution_progress": manager.contribution_roster(),
            "proactive_pause": dict(state.proactive_pause or {}),
            "recent_contributions": {
                participant.name: list(participant.contributions[-3:])
                for participant in state.participants
                if participant.contributions
            },
            "group_concept": world.group_concept,
            "starting_region": world.starting_region,
            "playstyle_themes": list(world.playstyle_themes[-8:]),
            "consensus_notes": list(world.consensus_notes[-8:]),
            "safety_lines": list(world.safety_lines[-8:]),
            "safety_veils": list(world.safety_veils[-8:]),
            "first_act_candidates": [
                {
                    "id": candidate.candidate_id,
                    "title": candidate.title,
                    "summary": candidate.premise,
                }
                for candidate in world.first_act_candidates[-6:]
            ],
            "first_act_votes": dict(world.first_act_votes),
            "selected_first_act_id": world.selected_first_act_id,
            "selected_first_act_summary": world.selected_first_act_summary,
            "world_canon": {
                "campaign_title": world.campaign_title,
                "continent_name": world.continent_name,
                "tone_preferences": list(world.tone_preferences[-8:]),
                "party_dynamic": world.party_dynamic,
                "description_style": world.description_style,
                "magic_tech_role": world.magic_tech_role,
                "core_themes": list(world.core_themes[-8:]),
                "major_locations": dict(list(world.major_locations.items())[-16:]),
                "map_locations": map_locations,
                "kingdoms": dict(list(world.kingdoms.items())[-12:]),
                "factions": dict(list(world.factions.items())[-12:]),
                "historical_events": list(world.historical_events[-12:]),
                "mysteries": list(world.mysteries[-12:]),
                "world_threats": list(world.world_threats[-12:]),
                "villain_seeds": list(world.villain_seeds[-8:]),
            },
            "pending_proposals": [
                {
                    "id": str(item.get("id") or ""),
                    "speaker": str(item.get("speaker") or ""),
                    "summary": str(item.get("summary") or ""),
                }
                for item in world.pending_proposals
                if isinstance(item, dict)
            ],
        }

    def get_session_zero_readiness(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        campaign_id = str(
            arguments.get("campaign_id") or context.campaign_id
        ).strip()
        runtime = self.host._runtime(campaign_id)
        readiness = self.host._adventure_readiness_snapshot(
            runtime,
            materialize_confirmed_characters=False,
        )
        readiness = deepcopy(readiness)
        readiness["campaign_id"] = campaign_id
        return GMToolReceipt(
            tool_name="get_session_zero_readiness",
            ok=True,
            result=readiness,
            public_fallback_reply=self._readiness_public_reply(readiness),
            lock_public_reply=True,
        )

    @staticmethod
    def _readiness_public_reply(readiness: dict[str, object]) -> str:
        if bool(readiness.get("ready")):
            return "第零章需要的内容已经齐了。等大家明确同意，就可以开启第一章。"

        lines = ["第零章还差这些："]
        session_zero = readiness.get("session_zero")
        session_zero = session_zero if isinstance(session_zero, dict) else {}
        world_fields = [
            str(item).strip()
            for item in list(session_zero.get("missing_world_fields") or [])
            if str(item).strip()
        ]
        if world_fields:
            lines.append("- 世界共创：" + "、".join(world_fields) + "。")

        contribution_gaps = session_zero.get("contribution_gaps")
        if isinstance(contribution_gaps, dict):
            for player, topics in contribution_gaps.items():
                topic_list = [
                    str(item).strip()
                    for item in list(topics or [])
                    if str(item).strip()
                ]
                if topic_list:
                    lines.append(
                        f"- {player}：还没贡献或跳过"
                        + "、".join(topic_list)
                        + "。"
                    )

        hero_creation = readiness.get("hero_creation")
        hero_creation = hero_creation if isinstance(hero_creation, dict) else {}
        missing_by_player = hero_creation.get("missing_by_player")
        if isinstance(missing_by_player, dict):
            for player, fields in missing_by_player.items():
                field_list = [
                    str(item).strip().rstrip("。")
                    for item in list(fields or [])
                    if str(item).strip()
                ]
                if field_list:
                    lines.append(f"- {player}：" + "；".join(field_list) + "。")

        if len(lines) == 1:
            lines.append("- 当前还没有足够的第零章记录。")
        return "\n".join(lines)

    def propose_update(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._evidence_error(context, arguments)
        if evidence_error:
            return evidence_error
        summary = str(arguments.get("summary") or "").strip()
        updates, error = self._validated_world_updates(arguments.get("updates"))
        if error:
            return error
        runtime, manager, inactive = self._active_manager(context)
        if inactive:
            return inactive
        proposal_id = f"proposal-{uuid4().hex[:12]}"
        proposal = {
            "id": proposal_id,
            "speaker": context.speaker,
            "summary": summary,
            "proposed_updates": updates,
            "evidence": str(arguments.get("evidence") or ""),
        }
        with runtime.transaction_lock:
            manager.apply_world_updates({"pending_proposals": [proposal]})
            manager.world_state.apply_world_profile(manager.state.world)
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        return GMToolReceipt(
            tool_name="propose_session_zero_update",
            ok=True,
            result={"proposal": proposal, "saved_path": saved_path},
            state_changed=True,
            public_fallback_reply="我先把这条作为待定提案放在桌上，等大家确认。",
        )

    def commit_update(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._evidence_error(context, arguments)
        if evidence_error:
            return evidence_error
        updates, error = self._validated_world_updates(arguments.get("updates"))
        if error:
            return error
        runtime, manager, inactive = self._active_manager(context)
        if inactive:
            return inactive
        first_act_error = self._validate_first_act_selection(
            manager,
            updates,
            tool_name="commit_session_zero_update",
        )
        if first_act_error is not None:
            return first_act_error
        recorded_categories = self._public_update_categories(updates)
        updates = self._with_contributor_updates(updates, context.speaker)
        with runtime.transaction_lock:
            manager.apply_world_updates(updates)
            self._record_structured_contribution(
                manager,
                context.speaker,
                str(arguments.get("evidence") or ""),
                updates,
            )
            stage = manager.refresh_stage_from_state()
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        return GMToolReceipt(
            tool_name="commit_session_zero_update",
            ok=True,
            result={
                "applied_fields": sorted(updates),
                "stage": stage.value,
                "missing_topics": manager.missing_topics(),
                "first_act_selected": bool(
                    manager.state.world.selected_first_act_id
                    or manager.state.world.selected_first_act_summary
                ),
                "selected_first_act_id": manager.state.world.selected_first_act_id,
                "selected_first_act_summary": manager.state.world.selected_first_act_summary,
                "recorded_categories": recorded_categories,
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply=self._public_update_confirmation(recorded_categories),
        )

    def confirm_proposal(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._evidence_error(context, arguments)
        if evidence_error:
            return evidence_error
        proposal_id = str(arguments.get("proposal_id") or "").strip()
        runtime, manager, inactive = self._active_manager(context)
        if inactive:
            return inactive
        proposal = next(
            (
                item
                for item in manager.state.world.pending_proposals
                if isinstance(item, dict) and str(item.get("id") or "") == proposal_id
            ),
            None,
        )
        if proposal is None:
            return GMToolReceipt(
                tool_name="confirm_session_zero_proposal",
                ok=False,
                error_code="PROPOSAL_NOT_FOUND",
                message=f"没有待定提案 {proposal_id}。",
                correction_hint="从state_summary.pending_proposals选择现存ID；若无法确定就向玩家追问。",
                retryable=True,
                result={"pending_proposals": self.state_summary(context)["pending_proposals"]},
                public_fallback_reply="我没找到你要确认的那条提案，暂时没有改动设定。",
            )
        raw_updates = proposal.get("proposed_updates")
        updates, error = self._validated_world_updates(raw_updates)
        if error:
            error.tool_name = "confirm_session_zero_proposal"
            return error
        first_act_error = self._validate_first_act_selection(
            manager,
            updates,
            tool_name="confirm_session_zero_proposal",
        )
        if first_act_error is not None:
            return first_act_error
        updates = self._with_contributor_updates(updates, str(proposal.get("speaker") or context.speaker))
        updates["clear_pending_proposals"] = [proposal_id]
        with runtime.transaction_lock:
            manager.apply_world_updates(updates)
            self._record_structured_contribution(
                manager,
                str(proposal.get("speaker") or context.speaker),
                str(proposal.get("evidence") or arguments.get("evidence") or ""),
                updates,
            )
            stage = manager.refresh_stage_from_state()
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        return GMToolReceipt(
            tool_name="confirm_session_zero_proposal",
            ok=True,
            result={
                "proposal_id": proposal_id,
                "summary": str(proposal.get("summary") or ""),
                "stage": stage.value,
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply="这条提案正式定下来了。",
        )

    def mark_topic_complete(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._evidence_error(context, arguments)
        if evidence_error:
            return evidence_error
        runtime, manager, inactive = self._active_manager(context)
        if inactive:
            return inactive
        topic = str(arguments.get("topic") or "")
        topic_code = self._TOPIC_CODES[topic]
        with runtime.transaction_lock:
            previous_stage = manager.state.stage
            participant_existed = manager.find_participant(context.speaker) is not None
            manager.ensure_participants([context.speaker])
            participant = manager.find_participant(context.speaker)
            evidence = str(arguments.get("evidence") or "").strip()
            changed = not participant_existed
            if participant is not None:
                if evidence and evidence not in participant.contributions:
                    participant.contributions.append(evidence)
                    changed = True
                if topic_code not in participant.answered_topics:
                    participant.answered_topics.append(topic_code)
                    changed = True
                if participant.pending_question:
                    participant.pending_question = ""
                    changed = True
            stage = manager.refresh_stage_from_state()
            changed = changed or stage != previous_stage
            saved_path = (
                self.host._autosave_campaign(runtime, context.campaign_id)
                if changed
                else str(getattr(runtime, "last_saved_path", "") or "")
            )
        return GMToolReceipt(
            tool_name="mark_session_zero_topic_complete",
            ok=True,
            result={"topic": topic, "stage": stage.value, "saved_path": saved_path},
            state_changed=changed,
            public_fallback_reply=(
                "好，这一项先跳过。"
                if changed
                else "这一项已经按跳过记过了。"
            ),
        )

    def set_nudge_preference(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._evidence_error(context, arguments)
        if evidence_error:
            return evidence_error
        runtime, manager, inactive = self._active_manager(context)
        if inactive:
            return inactive
        enabled = bool(arguments.get("enabled"))
        with runtime.transaction_lock:
            changed = manager.set_proactive_questions_enabled(
                context.speaker,
                enabled,
            )
            saved_path = self.host._autosave_campaign(
                runtime,
                context.campaign_id,
            )
        return GMToolReceipt(
            tool_name="set_session_zero_nudge_preference",
            ok=True,
            result={
                "player": context.speaker,
                "enabled": enabled,
                "saved_path": saved_path,
            },
            state_changed=changed,
            public_fallback_reply=(
                "好，需要推进第零章时我也可以来问你。"
                if enabled
                else "好，第零章需要主动提问时我先不点你。"
            ),
        )

    def pause_nudges(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._evidence_error(context, arguments)
        if evidence_error:
            return evidence_error
        if context.gate_status not in {"pre_session", "session_zero"}:
            return GMToolReceipt(
                tool_name="pause_session_zero_nudges",
                ok=False,
                error_code="SETUP_PHASE_NOT_ACTIVE",
                message="当前不在开团前或第零章阶段。",
                correction_hint="不要修改第零章主动提问状态；按当前会话阶段处理。",
                retryable=False,
            )
        runtime = self.host._runtime(context.campaign_id)
        manager = runtime.app.session_zero_manager
        evidence = str(arguments.get("evidence") or "").strip()
        topic = str(arguments.get("topic") or "").strip()
        with runtime.transaction_lock:
            changed = manager.pause_proactive_nudges(
                context.speaker,
                topic=topic,
                evidence=evidence,
            )
            saved_path = self.host._autosave_campaign(
                runtime,
                context.campaign_id,
            )
        return GMToolReceipt(
            tool_name="pause_session_zero_nudges",
            ok=True,
            result={
                "player": context.speaker,
                "topic": topic,
                "resume_condition": "next_player_message",
                "saved_path": saved_path,
            },
            state_changed=changed,
            public_fallback_reply="好，你慢慢想。",
        )

    def update_hero_draft(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._evidence_error(context, arguments)
        if evidence_error:
            return evidence_error
        patch, error = self._validated_hero_patch(arguments.get("patch"))
        if error:
            return error
        runtime, manager, inactive = self._active_manager(context)
        if inactive:
            return inactive
        subject = str(arguments.get("subject") or "").strip()
        key, draft = self._resolve_draft(manager.state.world.hero_drafts, subject)
        if draft is None:
            if subject not in {context.speaker, "我", "本人"}:
                return GMToolReceipt(
                    tool_name="update_hero_draft",
                    ok=False,
                    error_code="HERO_DRAFT_TARGET_NOT_FOUND",
                    message=f"没有找到 {subject} 的角色草稿。",
                    correction_hint="新草稿只能以当前发言者为玩家建立；点名修改时先读取草稿确认玩家名与角色名。",
                    retryable=True,
                    public_fallback_reply="我没找到对应的角色草稿，所以没有改动。",
                )
            key = context.speaker
            draft = HeroDraft(player_name=context.speaker)
        skill_rank_error = self._validate_skill_rank_change(draft, patch)
        if skill_rank_error:
            return skill_rank_error

        candidate = deepcopy(draft)
        try:
            manager._apply_hero_draft_patch(candidate, patch)
        except (TypeError, ValueError) as exc:
            return GMToolReceipt(
                tool_name="update_hero_draft",
                ok=False,
                error_code="INVALID_HERO_PATCH_VALUE",
                message=str(exc),
                correction_hint="按角色草稿字段重新提交；属性骰填写6、8、10或12。",
                retryable=True,
            )
        if candidate == draft:
            return GMToolReceipt(
                tool_name="update_hero_draft",
                ok=False,
                error_code="HERO_PATCH_NO_EFFECT",
                message="本次角色草稿修改没有改变任何字段。",
                correction_hint=(
                    "不要声称修改已经完成。先读取现有草稿：若目标状态已经满足，直接如实说明；"
                    "否则根据玩家原话提交确实存在差异的字段。"
                ),
                retryable=False,
                state_changed=False,
                public_fallback_reply="角色草稿没有发生变化。",
            )
        candidate.player_name = candidate.player_name or context.speaker
        validation_error = self._validate_candidate_shape(candidate)
        if validation_error:
            return validation_error

        with runtime.transaction_lock:
            manager.state.world.hero_drafts[key] = candidate
            manager.ensure_participants([candidate.player_name or context.speaker])
            participant = manager.find_participant(candidate.player_name or context.speaker)
            evidence = str(arguments.get("evidence") or "").strip()
            if participant is not None and evidence and evidence not in participant.contributions:
                participant.contributions.append(evidence)
            manager.world_state.apply_world_profile(manager.state.world)
            validation = runtime.app.validate_hero_draft(key)
            stage = manager.refresh_stage_from_state()
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        return GMToolReceipt(
            tool_name="update_hero_draft",
            ok=True,
            result={
                "record_key": key,
                "player_name": candidate.player_name,
                "hero_name": candidate.hero_name,
                "ready": bool(validation.ready),
                "missing_fields": list(validation.missing_fields),
                "errors": list(validation.errors),
                "stage": stage.value,
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply="这项角色信息记下了。",
        )

    def record_safety_boundary(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._evidence_error(context, arguments)
        if evidence_error:
            return evidence_error
        content = str(arguments.get("content") or "").strip()
        if not content:
            return GMToolReceipt(
                tool_name="record_safety_boundary",
                ok=False,
                error_code="SAFETY_CONTENT_REQUIRED",
                message="界限或帷幕需要具体内容。",
                correction_hint="从玩家消息中提取具体元素；不能确定时向玩家追问。",
                retryable=True,
            )
        runtime = self.host._runtime(context.campaign_id)
        kind = str(arguments.get("kind") or "")
        anonymous = bool(arguments.get("anonymous", False))
        with runtime.transaction_lock:
            result = runtime.app.safety_manager.declare(
                kind,
                content,
                speaker=context.speaker,
                anonymous=anonymous,
            )
            if not result.accepted:
                return GMToolReceipt(
                    tool_name="record_safety_boundary",
                    ok=False,
                    error_code="SAFETY_DECLARATION_REJECTED",
                    message=result.message,
                    correction_hint="补充具体内容后重试。",
                    retryable=True,
                )
            manager = runtime.app.session_zero_manager
            if manager.state.active:
                manager.refresh_stage_from_state()
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        label = "界限" if kind == "line" else "帷幕"
        return GMToolReceipt(
            tool_name="record_safety_boundary",
            ok=True,
            result={"kind": kind, "content": result.item, "saved_path": saved_path},
            state_changed=True,
            public_fallback_reply=f"ok，已记录这条{label}。",
        )

    def confirm_hero_draft(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._evidence_error(context, arguments)
        if evidence_error:
            evidence_error.tool_name = "confirm_hero_draft"
            return evidence_error
        runtime, manager, inactive = self._active_manager(context)
        if inactive:
            inactive.tool_name = "confirm_hero_draft"
            return inactive
        subject = str(arguments.get("subject") or "").strip()
        key, draft = self._resolve_draft(manager.state.world.hero_drafts, subject)
        if draft is None:
            return GMToolReceipt(
                tool_name="confirm_hero_draft",
                ok=False,
                error_code="HERO_DRAFT_TARGET_NOT_FOUND",
                message=f"没有找到 {subject} 的角色草稿。",
                correction_hint="先读取或建立对应草稿；不要猜测玩家与角色的对应关系。",
                retryable=True,
                public_fallback_reply="我没找到对应的角色草稿，所以还没有确认。",
            )
        validation = runtime.app.validate_hero_draft(key)
        if not validation.ready:
            return GMToolReceipt(
                tool_name="confirm_hero_draft",
                ok=False,
                error_code="HERO_DRAFT_INCOMPLETE",
                message="角色草稿尚未满足创建规则。",
                correction_hint="只指出回执中的实际缺项；等待玩家补充后再确认。",
                retryable=False,
                result={
                    "record_key": key,
                    "hero_name": draft.hero_name,
                    "missing_fields": list(validation.missing_fields),
                    "errors": list(validation.errors),
                },
                public_fallback_reply="这张角色草稿还没有满足创建规则，所以暂时没有确认。",
            )
        with runtime.transaction_lock:
            confirmed = runtime.app.confirm_hero_draft(key)
            stage = manager.refresh_stage_from_state()
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        label = draft.hero_name or draft.player_name or key
        return GMToolReceipt(
            tool_name="confirm_hero_draft",
            ok=True,
            result={
                "record_key": key,
                "player_name": draft.player_name,
                "hero_name": draft.hero_name,
                "ready": bool(confirmed.ready),
                "confirmed": True,
                "stage": stage.value,
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply=f"好，{label}建好了。",
        )

    def _active_manager(self, context: GMToolExecutionContext):
        runtime = self.host._runtime(context.campaign_id)
        manager = runtime.app.session_zero_manager
        if not manager.state.active:
            return runtime, manager, GMToolReceipt(
                tool_name="session_zero",
                ok=False,
                error_code="SESSION_ZERO_NOT_ACTIVE",
                message="当前没有进行第零章。",
                correction_hint="不要写入第零章；若玩家要开始第零章，应交给会话阶段工具。",
                retryable=False,
                public_fallback_reply="当前还没有开启第零章，所以我没有写入这条设定。",
            )
        return runtime, manager, None

    def _evidence_error(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt | None:
        if is_current_message_evidence(context, arguments.get("evidence")):
            return None
        return GMToolReceipt(
            tool_name="",
            ok=False,
            error_code="EVIDENCE_NOT_IN_CURRENT_MESSAGE",
            message="提交证据不是当前玩家消息中的连续原文。",
            correction_hint="从current_message逐字复制支持此次工具调用的片段；不得用路由摘要或历史概括代替。",
            retryable=True,
        )

    def _validated_world_updates(
        self,
        raw: object,
    ) -> tuple[dict[str, Any], GMToolReceipt | None]:
        if not isinstance(raw, dict) or not raw:
            return {}, self._invalid_world_update("updates必须是非空JSON对象。")
        allowed = self._SCALAR_FIELDS | self._LIST_FIELDS | self._DICT_FIELDS | {self._MAP_LOCATION_FIELD}
        unknown = sorted(set(raw) - allowed)
        if unknown:
            return {}, self._invalid_world_update("不允许的第零章字段：" + "、".join(unknown))
        clean = deepcopy(raw)
        for field in self._SCALAR_FIELDS:
            if field in clean and not isinstance(clean[field], str):
                return {}, self._invalid_world_update(f"{field}必须是字符串。")
        for field in self._LIST_FIELDS:
            if field in clean and (
                not isinstance(clean[field], list)
                or any(not isinstance(item, str) for item in clean[field])
            ):
                return {}, self._invalid_world_update(f"{field}必须是字符串数组。")
        for field in self._DICT_FIELDS:
            if field in clean and not isinstance(clean[field], dict):
                return {}, self._invalid_world_update(f"{field}必须是JSON对象。")
        if self._MAP_LOCATION_FIELD in clean and not isinstance(
            clean[self._MAP_LOCATION_FIELD], (dict, list)
        ):
            return {}, self._invalid_world_update("map_locations必须是对象或数组。")
        return clean, None

    @staticmethod
    def _validate_first_act_selection(
        manager: Any,
        updates: dict[str, Any],
        *,
        tool_name: str,
    ) -> GMToolReceipt | None:
        candidate_id = str(updates.get("selected_first_act_id") or "").strip()
        if not candidate_id:
            return None
        resolved = manager.prologue_manager.resolve_candidate_id(
            manager.state.world,
            candidate_id,
        )
        if resolved:
            updates["selected_first_act_id"] = resolved
            return None
        candidates = [
            {
                "id": item.candidate_id,
                "title": item.title,
            }
            for item in manager.state.world.first_act_candidates
        ]
        return GMToolReceipt(
            tool_name=tool_name,
            ok=False,
            error_code="UNKNOWN_FIRST_ACT_CANDIDATE",
            message=f"【{candidate_id}】不是现有第一幕候选ID。",
            correction_hint=(
                "若玩家确认的是自定义第一幕，删除selected_first_act_id，"
                "把标题、前提和目标完整写入selected_first_act_summary；不需要逐人投票。"
            ),
            retryable=True,
            result={"first_act_candidates": candidates},
        )

    @staticmethod
    def _invalid_world_update(message: str) -> GMToolReceipt:
        return GMToolReceipt(
            tool_name="commit_session_zero_update",
            ok=False,
            error_code="INVALID_SESSION_ZERO_UPDATE",
            message=message,
            correction_hint="只提交工具说明允许的字段，并按字段类型修正JSON。",
            retryable=True,
        )

    def _validated_hero_patch(
        self,
        raw: object,
    ) -> tuple[dict[str, Any], GMToolReceipt | None]:
        if not isinstance(raw, dict) or not raw:
            return {}, self._invalid_hero_patch("patch必须是非空JSON对象。")
        allowed = self._HERO_SCALARS | self._HERO_DICTS | self._HERO_LISTS | self._HERO_BOOLEANS
        unknown = sorted(set(raw) - allowed)
        if unknown:
            return {}, self._invalid_hero_patch("不允许的角色字段：" + "、".join(unknown))
        clean = deepcopy(raw)
        for field in self._HERO_SCALARS:
            if field in clean and not isinstance(clean[field], str):
                return {}, self._invalid_hero_patch(f"{field}必须是字符串。")
        for field in self._HERO_DICTS:
            if field in clean and not isinstance(clean[field], dict):
                return {}, self._invalid_hero_patch(f"{field}必须是JSON对象。")
        equipment_slots = clean.get("equipment_slots")
        if isinstance(equipment_slots, dict):
            allowed_slots = {"main_hand", "off_hand", "armor", "shield"}
            unknown_slots = sorted(set(equipment_slots) - allowed_slots)
            if unknown_slots:
                return {}, self._invalid_hero_patch(
                    "不允许的装备栏位：" + "、".join(unknown_slots)
                )
            if any(not isinstance(value, str) for value in equipment_slots.values()):
                return {}, self._invalid_hero_patch("equipment_slots的值必须是字符串。")
        for field in self._HERO_LISTS:
            if field in clean and (
                not isinstance(clean[field], list)
                or any(not isinstance(item, str) for item in clean[field])
            ):
                return {}, self._invalid_hero_patch(f"{field}必须是字符串数组。")
        for field in self._HERO_BOOLEANS:
            if field in clean and not isinstance(clean[field], bool):
                return {}, self._invalid_hero_patch(f"{field}必须是布尔值。")
        skills = clean.get("skills")
        if isinstance(skills, dict):
            canonical_skills: dict[str, int] = {}
            for raw_name, rank in skills.items():
                name = str(raw_name or "").strip()
                reference = get_skill_reference(name)
                if reference is None or reference.kind != "class":
                    return {}, GMToolReceipt(
                        tool_name="update_hero_draft",
                        ok=False,
                        error_code="UNKNOWN_HERO_SKILL",
                        message=f"【{name}】不是权威职业技能名。",
                        correction_hint=(
                            "先使用search_rule_references查询对应职业技能，再用完整技能名重新提交；"
                            "不得拆分带有‘与’或其他连接词的技能名。"
                        ),
                        retryable=True,
                    )
                try:
                    parsed_rank = int(rank)
                except (TypeError, ValueError):
                    return {}, self._invalid_hero_patch(
                        f"技能【{reference.name}】的等级必须是整数。"
                    )
                if parsed_rank > reference.max_ranks:
                    return {}, GMToolReceipt(
                        tool_name="update_hero_draft",
                        ok=False,
                        error_code="HERO_SKILL_RANK_EXCEEDED",
                        message=(
                            f"技能【{reference.name}】本次提交等级 {parsed_rank}，"
                            f"超过上限 {reference.max_ranks}。"
                        ),
                        correction_hint="按玩家本次实际选择次数重新提交。",
                        retryable=True,
                    )
                canonical_skills[reference.name] = parsed_rank
            clean["skills"] = canonical_skills
        skill_options = clean.get("skill_options")
        if isinstance(skill_options, dict):
            canonical_options: dict[str, list[str]] = {}
            for raw_name, raw_choices in skill_options.items():
                reference = get_skill_reference(str(raw_name or "").strip())
                if reference is None or reference.kind != "class":
                    return {}, GMToolReceipt(
                        tool_name="update_hero_draft",
                        ok=False,
                        error_code="UNKNOWN_HERO_SKILL_OPTION",
                        message=f"【{raw_name}】不是可记录附带选择的职业技能。",
                        correction_hint="先查询完整技能名，再提交skill_options。",
                        retryable=True,
                    )
                if not isinstance(raw_choices, list) or any(
                    not isinstance(choice, str) or not choice.strip()
                    for choice in raw_choices
                ):
                    return {}, self._invalid_hero_patch(
                        f"技能【{reference.name}】的附带选择必须是非空字符串数组。"
                    )
                canonical_options[reference.name] = [choice.strip() for choice in raw_choices]
            clean["skill_options"] = canonical_options
        remove_skills = clean.get("remove_skills")
        if isinstance(remove_skills, list):
            canonical_removals: list[str] = []
            for raw_name in remove_skills:
                reference = get_skill_reference(str(raw_name or "").strip())
                if reference is None or reference.kind != "class":
                    return {}, GMToolReceipt(
                        tool_name="update_hero_draft",
                        ok=False,
                        error_code="UNKNOWN_HERO_SKILL",
                        message=f"【{raw_name}】不是可移除的权威职业技能名。",
                        correction_hint="先读取草稿或查询规则，使用完整技能名。",
                        retryable=True,
                    )
                canonical_removals.append(reference.name)
            clean["remove_skills"] = list(dict.fromkeys(canonical_removals))
        return clean, None

    @staticmethod
    def _validate_skill_rank_change(
        draft: HeroDraft,
        patch: dict[str, Any],
    ) -> GMToolReceipt | None:
        skills = patch.get("skills")
        if not isinstance(skills, dict):
            return None
        increment = bool(patch.get("increment_skills"))
        replace = bool(patch.get("replace_skills"))
        for name, submitted_rank in skills.items():
            reference = get_skill_reference(str(name))
            if reference is None or reference.kind != "class":
                continue
            current_rank = 0 if replace else int(draft.skills.get(reference.name, 0) or 0)
            resulting_rank = (
                current_rank + int(submitted_rank)
                if increment
                else int(submitted_rank)
            )
            if resulting_rank > reference.max_ranks:
                return GMToolReceipt(
                    tool_name="update_hero_draft",
                    ok=False,
                    error_code="HERO_SKILL_RANK_EXCEEDED",
                    message=(
                        f"技能【{reference.name}】当前为 {current_rank} 级，"
                        f"本次更新后会变成 {resulting_rank} 级，超过上限 {reference.max_ranks}。"
                    ),
                    correction_hint=(
                        "不要重复执行已经成功的技能更新；只按玩家本条消息新增一次或提交正确绝对等级。"
                    ),
                    retryable=True,
                )
        return None

    @staticmethod
    def _invalid_hero_patch(message: str) -> GMToolReceipt:
        return GMToolReceipt(
            tool_name="update_hero_draft",
            ok=False,
            error_code="INVALID_HERO_DRAFT_PATCH",
            message=message,
            correction_hint="按角色草稿工具schema修正字段，不得替玩家补写未声明内容。",
            retryable=True,
        )

    @staticmethod
    def _resolve_draft(drafts: dict[str, HeroDraft], subject: str) -> tuple[str, HeroDraft | None]:
        clean = str(subject or "").strip()
        if clean in drafts:
            return clean, drafts[clean]
        for key, draft in drafts.items():
            if clean in {draft.player_name, draft.hero_name}:
                return key, draft
        return clean, None

    @staticmethod
    def _validate_candidate_shape(candidate: HeroDraft) -> GMToolReceipt | None:
        if len(candidate.classes) > 3 or sum(candidate.classes.values()) > 5:
            return GMToolReceipt(
                tool_name="update_hero_draft",
                ok=False,
                error_code="INVALID_CLASS_ALLOCATION",
                message="起始角色只能选择2到3个职业，总等级不能超过5。",
                correction_hint="修正classes后重新提交；增量建卡可以暂时少于5级。",
                retryable=True,
            )
        if any(value not in {6, 8, 10, 12} for value in candidate.attributes.values()):
            return GMToolReceipt(
                tool_name="update_hero_draft",
                ok=False,
                error_code="INVALID_ATTRIBUTE_DIE",
                message="属性骰只能是d6、d8、d10或d12。",
                correction_hint="attributes中使用6、8、10或12。",
                retryable=True,
            )
        if any(value < 0 for value in [*candidate.classes.values(), *candidate.skills.values()]):
            return GMToolReceipt(
                tool_name="update_hero_draft",
                ok=False,
                error_code="NEGATIVE_HERO_VALUE",
                message="职业和技能等级不能为负数。",
                correction_hint="修正数值后重新提交。",
                retryable=True,
            )
        return None

    def _with_contributor_updates(
        self,
        updates: dict[str, Any],
        speaker: str,
    ) -> dict[str, Any]:
        result = deepcopy(updates)
        for field, (contributor_field, _topic) in self._CONTRIBUTION_FIELDS.items():
            if field not in updates:
                continue
            raw = updates[field]
            if isinstance(raw, dict):
                values = list(raw)
            elif isinstance(raw, list):
                values = [
                    str(item.get("name") or "") if isinstance(item, dict) else str(item)
                    for item in raw
                ]
            else:
                values = [str(raw)]
            bucket = result.setdefault(contributor_field, {})
            if isinstance(bucket, dict):
                bucket.setdefault(speaker, [])
                bucket[speaker].extend(value for value in values if value)
        return result

    def _record_structured_contribution(
        self,
        manager: Any,
        speaker: str,
        evidence: str,
        updates: dict[str, Any],
    ) -> None:
        manager.ensure_participants([speaker])
        participant = manager.find_participant(speaker)
        if participant is None:
            return
        if evidence and evidence not in participant.contributions:
            participant.contributions.append(evidence)
        for field, (_contributor_field, topic) in self._CONTRIBUTION_FIELDS.items():
            if field in updates and topic not in participant.answered_topics:
                participant.answered_topics.append(topic)
        participant.pending_question = ""
