from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Protocol
from uuid import uuid4

from fu_gm.components.world_setting_catalog import WorldSettingCatalog
from fu_gm.components.gm_message_integrity import GMMessageIntegrityValidator
from fu_gm.gm_evidence import is_current_message_evidence
from fu_gm.components.solo_session_zero_completer import SoloSessionZeroCompleter
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
    _WORLD_OPERATION_TO_TOOL = {
        "create": "create_world_setting",
        "update": "update_world_setting",
        "delete": "delete_world_setting",
        "rename": "rename_world_setting",
    }
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
    _CONTRIBUTION_QUERY_TOPICS = {
        "kingdom": (
            "kingdom_contributions",
            "kingdom_contributors",
            "王国、国家或政治共同体",
        ),
        "historical_event": (
            "historical_event_contributions",
            "historical_event_contributors",
            "重大历史事件",
        ),
        "mystery": (
            "mystery_contributions",
            "mystery_contributors",
            "世界奥秘",
        ),
        "threat": (
            "threat_contributions",
            "threat_contributors",
            "世界性威胁",
        ),
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
        "villain_seeds": "反派种子",
        "villain_mirrors": "反派映照",
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

    @classmethod
    def _public_update_confirmation(cls, categories: list[str]) -> str:
        del categories
        return "好，记下了。"

    @staticmethod
    def _source_statement_can_commit_silently(
        context: GMToolExecutionContext,
    ) -> bool:
        """Distinguish a public contribution from an actual address to the GM.

        Legacy direct endpoints set ``force_gm_reply`` only to select FU-GM;
        that transport flag is not evidence that the player called the GM.
        """

        metadata = context.metadata
        explicitly_addressed = bool(
            context.is_private
            or metadata.get("is_at_bot")
            or metadata.get("is_reply_to_bot")
            or metadata.get("identity_addressed")
            or metadata.get("_semantic_gm_addressed")
        )
        return not explicitly_addressed

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
        string_properties["group_concept"] = {
            "type": "string",
            "minLength": 1,
            "description": (
                "小队原型与共同使命，回答‘这群英雄作为一支队伍是谁、为何同行、共同要做什么’。"
                "例如‘临时守护者：护送失忆旅人前往钟鸣公国’。玩家明确提出小队原型、"
                "队伍主题或共同任务时必须写这里，不能改写到party_dynamic。"
            ),
        }
        string_properties["party_dynamic"] = {
            "type": "string",
            "minLength": 1,
            "description": (
                "队员之间的内部关系与相处方式，回答‘他们起初是否熟识、如何合作、允许何种分歧’。"
                "例如‘多数人初次见面，但约定先保护平民再争论路线’；这不是小队原型或共同任务。"
            ),
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
        string_properties["villain_seeds"]["description"] = (
            "可供GM发展为反派的具名人物或组织及其信念、目标或手段。玩家明确提出"
            "‘某人相信、想要、正在推动某事’等对立人物概念时，应独立写入此字段；"
            "即使同一人物也出现在factions、world_threats或consensus_notes中也不能省略。"
        )
        string_properties["consensus_notes"]["description"] = (
            "没有专用结构字段的桌面共识。具名反派及其信念或目标应写入villain_seeds，"
            "重大历史、奥秘和威胁也必须使用各自字段，不得只放进共识。"
        )
        string_properties["mysteries"]["description"] = (
            "玩家希望冒险中探索、答案尚未确定的世界奥秘；与地点或国家说明中已经"
            "公开成立的事实分开记录。"
        )
        string_properties["world_threats"]["description"] = (
            "正在危及地区、国家或世界未来的客观威胁。带有明确危险主体、触发条件和"
            "地区性危害结果的条件危机仍属于世界威胁，例如‘若王室与行会决裂，财团将"
            "夺走群岛调查权’；只有单纯希望玩家选择产生后果的玩法偏好，才写入"
            "playstyle_themes或consensus_notes。"
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
    def _world_operations_schema(cls) -> dict[str, object]:
        return {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": list(cls._WORLD_OPERATION_TO_TOOL),
                    },
                    "category": {
                        "type": "string",
                        "enum": list(WorldSettingCatalog.CATEGORIES),
                    },
                    "name": {
                        "type": "string",
                        "description": (
                            "更新、删除或改名时使用查询所得的准确名称；"
                            "列表项必须填写完整原文。"
                        ),
                    },
                    "new_name": {"type": "string"},
                    "value": {"type": "string"},
                    "attributes": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                    "visibility": {
                        "type": "string",
                        "enum": ["public", "gm_private"],
                    },
                },
                "required": ["operation", "category", "visibility"],
                "additionalProperties": False,
            },
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
                name="get_session_zero_contributions",
                description=(
                    "只读查询某位玩家在第零章真正贡献或明确跳过了哪些国家、历史事件、"
                    "奥秘和威胁。玩家问‘我的王国/国家贡献是什么’时使用本工具；"
                    "地图地点和地貌会作为其他世界贡献返回，但绝不会冒充国家贡献。"
                ),
                handler=self.get_session_zero_contributions,
                parameters=(
                    GMToolParameter(
                        "player",
                        "string",
                        "可选。要查看的桌外玩家名；省略时查询当前发言者。",
                    ),
                    GMToolParameter(
                        "topic",
                        "string",
                        "可选。只查看一类贡献；默认 all。",
                        enum=("all", *self._CONTRIBUTION_QUERY_TOPICS),
                    ),
                ),
                side_effect="read",
                max_model_result_chars=5000,
            )
        )
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
                    GMToolParameter(
                        "purpose",
                        "string",
                        "answer_player用于直接回答缺项；gm_planning用于主持人在获授权后内部规划，不锁定公开回复。",
                        enum=("answer_player", "gm_planning"),
                    ),
                ),
            )
        )
        self._register_standard_session_zero_tools(registry)

    def get_session_zero_contributions(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        """Return contribution status without inferring it from geography."""

        runtime = self.host._runtime(context.campaign_id)
        manager = runtime.app.session_zero_manager
        requested_player = str(arguments.get("player") or context.speaker).strip()
        participant = manager.find_participant(requested_player)
        if participant is None:
            folded = requested_player.casefold()
            participant = next(
                (
                    item
                    for item in manager.state.participants
                    if str(item.name or "").strip().casefold() == folded
                ),
                None,
            )
        if participant is None:
            return GMToolReceipt.failure(
                "get_session_zero_contributions",
                "SESSION_ZERO_PARTICIPANT_NOT_FOUND",
                f"当前第零章没有找到玩家【{requested_player}】。",
                "使用当前参与者名单中的准确桌外玩家名；查询本人时省略player。",
                retryable=False,
            )

        topic = str(arguments.get("topic") or "all").strip() or "all"
        selected_topics = (
            tuple(self._CONTRIBUTION_QUERY_TOPICS)
            if topic == "all"
            else (topic,)
        )
        world = manager.state.world
        statuses: list[dict[str, object]] = []
        for topic_name in selected_topics:
            topic_code, contributor_field, label = self._CONTRIBUTION_QUERY_TOPICS[
                topic_name
            ]
            contributors = getattr(world, contributor_field)
            values = [
                str(item or "").strip()
                for item in list(contributors.get(participant.name, []) or [])
                if str(item or "").strip()
            ]
            explicitly_completed = topic_code in participant.answered_topics
            status = (
                "contributed"
                if values
                else "skipped"
                if explicitly_completed
                else "pending"
            )
            statuses.append(
                {
                    "topic": topic_name,
                    "topic_code": topic_code,
                    "label": label,
                    "status": status,
                    "values": values,
                }
            )

        authored_records = [
            {
                "category": str(record.get("category") or ""),
                "name": str(record.get("name") or ""),
                "value": str(record.get("value") or ""),
            }
            for record in WorldSettingCatalog(runtime.app).query(
                visibility="public"
            )["records"]
            if str(record.get("source_speaker") or "").strip()
            == participant.name
            and str(record.get("authority") or "").strip()
            in {"player_confirmed", "table_consensus", "retcon"}
        ]
        return GMToolReceipt.success(
            "get_session_zero_contributions",
            result={
                "campaign_id": context.campaign_id,
                "player": participant.name,
                "topics": statuses,
                "other_world_contributions": authored_records,
            },
        )

    def prepare_solo_adventure(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        """补全获授权的单人第零章；仅在明确开章时沿正常流程生成地图。"""

        evidence_error = self._evidence_error(context, arguments)
        if evidence_error:
            evidence_error.tool_name = "prepare_solo_adventure"
            return evidence_error
        if not context.is_private:
            return GMToolReceipt.failure(
                "prepare_solo_adventure",
                "SOLO_DELEGATION_REQUIRES_PRIVATE_CHANNEL",
                "整包单人委托只适用于私聊单人团。",
                "多人团仍需让各位玩家确认共同设定；按普通第零章工具逐项处理。",
                retryable=False,
            )
        runtime, manager, inactive = self._active_manager(context)
        if inactive:
            inactive.tool_name = "prepare_solo_adventure"
            return inactive

        participant_names = [
            str(item.name or "").strip()
            for item in manager.state.participants
            if str(item.name or "").strip()
        ]
        draft_entries = list(manager.state.world.hero_drafts.items())
        foreign_participants = [
            name
            for name in participant_names
            if not self._is_private_solo_identity(name, context.speaker)
        ]
        foreign_drafts = [
            key
            for key, draft in draft_entries
            if not self._is_private_solo_identity(
                str(draft.player_name or key or "").strip(),
                context.speaker,
            )
        ]
        if foreign_participants or foreign_drafts or len(draft_entries) > 1:
            return GMToolReceipt.failure(
                "prepare_solo_adventure",
                "CAMPAIGN_IS_NOT_SOLO",
                "当前第零章包含其他玩家，不能由一名玩家把全桌共创一次性交给GM。",
                "继续使用普通第零章流程，分别记录每位玩家的贡献或明确跳过。",
                retryable=False,
                public_reply=(
                    "这个存档里还记录着其他玩家，我不能替大家一次决定完"
                    "尚未确认的共创内容。"
                ),
            )

        draft_key, existing_draft = self._resolve_draft(
            manager.state.world.hero_drafts,
            context.speaker,
        )
        if existing_draft is None and len(draft_entries) == 1:
            alias_key, alias_draft = draft_entries[0]
            if self._is_private_solo_identity(
                str(alias_draft.player_name or alias_key or "").strip(),
                context.speaker,
            ):
                draft_key, existing_draft = alias_key, alias_draft
        if existing_draft is not None:
            existing_validation = runtime.app.validate_hero_draft(draft_key)
            has_partial_mechanics = any(
                (
                    existing_draft.classes,
                    existing_draft.attributes,
                    existing_draft.skills,
                    existing_draft.spells,
                    existing_draft.equipment,
                )
            )
            if has_partial_mechanics and not existing_validation.ready:
                return GMToolReceipt.failure(
                    "prepare_solo_adventure",
                    "PARTIAL_HERO_MECHANICS_REQUIRE_COMPLETION",
                    "现有角色草稿已经包含部分机械选择，整包模板不能覆盖这些已定内容。",
                    "保留现有选择，按回执中的实际缺项继续补完角色；完成后再开章。",
                    retryable=False,
                    result={
                        "hero_name": existing_draft.hero_name,
                        "missing_fields": list(existing_validation.missing_fields),
                        "errors": list(existing_validation.errors),
                    },
                )

        current_world = self._solo_completion_world_context(runtime, manager)
        completer = SoloSessionZeroCompleter(
            client=getattr(runtime.app, "creative_client", None),
            model=str(getattr(runtime.app, "creative_model", "") or ""),
        )
        deadline_raw = context.metadata.get("_gm_agent_deadline_monotonic")
        try:
            deadline = float(deadline_raw) if deadline_raw is not None else None
        except (TypeError, ValueError):
            deadline = None
        completion = completer.complete(
            current_world=current_world,
            player_name=context.speaker,
            creative_direction=str(arguments.get("creative_direction") or ""),
            deadline=deadline,
        )
        updates = self._missing_only_solo_world_updates(
            manager,
            runtime,
            completion.world_updates,
        )
        if not (
            manager.state.world.safety_lines
            or manager.state.world.safety_veils
        ):
            updates["safety_veils"] = [
                "单人团默认将露骨性内容与极端血腥细节淡出；玩家可随时追加或修改界限与帷幕。"
            ]

        candidate = deepcopy(existing_draft) if existing_draft is not None else HeroDraft(
            player_name=context.speaker
        )
        candidate.player_name = context.speaker
        candidate.hero_name = candidate.hero_name or completion.hero_story["hero_name"]
        candidate.identity = candidate.identity or completion.hero_story["identity"]
        candidate.theme = candidate.theme or completion.hero_story["theme"]
        candidate.origin = candidate.origin or completion.hero_story["origin"]
        if not candidate.classes:
            candidate.classes = {"武器大师": 2, "旅人": 1, "元素使": 2}
        if not candidate.attributes:
            candidate.attributes = {"敏捷": 8, "洞察": 8, "力量": 8, "意志": 8}
        if not candidate.skills:
            candidate.skills = {
                "碎骨": 1,
                "破防打击": 1,
                "宝物猎人": 1,
                "元素魔法": 1,
                "元素系仪式": 1,
            }
        if not candidate.spells:
            candidate.spells = ["元素武器"]
        if not candidate.equipment:
            candidate.equipment = ["钢匕首", "细剑", "丝质衬衫"]
        candidate.confirmed = True

        with runtime.transaction_lock:
            self._adopt_private_solo_identity(manager, context.speaker)
            if draft_key and self._is_anonymous_player_placeholder(draft_key):
                draft_key = context.speaker
            manager.ensure_participants([context.speaker])
            if updates:
                manager.apply_world_updates(updates)
            manager.state.world.hero_drafts[draft_key or context.speaker] = candidate
            participant = manager.find_participant(context.speaker)
            evidence = str(arguments.get("evidence") or "").strip()
            if participant is not None and evidence and evidence not in participant.contributions:
                participant.contributions.append(evidence)
            manager.world_state.apply_world_profile(manager.state.world)
            validation = runtime.app.validate_hero_draft(draft_key or context.speaker)
            stage = manager.refresh_stage_from_state()
            readiness = self.host._adventure_readiness_snapshot(
                runtime,
                materialize_confirmed_characters=False,
            )
            if not validation.ready or not bool(readiness.get("ready")):
                return GMToolReceipt.failure(
                    "prepare_solo_adventure",
                    "SOLO_COMPLETION_DID_NOT_REACH_READINESS",
                    "单人第零章补全包没有通过现有规则校验。",
                    "读取回执中的真实缺项；不要声称已经可以开章。",
                    retryable=False,
                    result={
                        "hero_missing_fields": list(validation.missing_fields),
                        "hero_errors": list(validation.errors),
                        "readiness": readiness,
                    },
                )
            if bool(arguments.get("start_adventure")):
                manager.set_chapter_one_transition(
                    "invited",
                    speaker=context.speaker,
                    evidence=evidence,
                )
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)

        start_adventure = bool(arguments.get("start_adventure"))
        opening_receipt: GMToolReceipt | None = None
        if start_adventure:
            opening_receipt = self._start_completed_solo_adventure(
                context,
                runtime,
                candidate,
                completion,
                evidence=str(arguments.get("evidence") or "").strip(),
            )
            if not opening_receipt.ok:
                return opening_receipt
        result: dict[str, object] = {
            "stage": stage.value,
            "adventure_ready": True,
            "hero_name": candidate.hero_name,
            "filled_world_fields": sorted(updates),
            "creative_model": completion.model,
            "creative_model_used": completion.used_model,
            "creative_fallback_reason": completion.error,
            "map_generation_deferred": not start_adventure,
            "saved_path": saved_path,
        }
        if opening_receipt is not None:
            result["adventure"] = opening_receipt.result
            result["required_followup_resolved"] = True
        return GMToolReceipt.success(
            "prepare_solo_adventure",
            result=result,
            state_changed=True,
            public_reply=(
                opening_receipt.public_fallback_reply
                if opening_receipt is not None
                else "剩下的第零章内容已经补齐，角色也准备好了。"
            ),
            lock_public_reply=opening_receipt is not None,
            pacing_events=(
                list(opening_receipt.pacing_events)
                if opening_receipt is not None
                else []
            ),
            narrative_events=(
                list(opening_receipt.narrative_events)
                if opening_receipt is not None
                else []
            ),
        )

    def _start_completed_solo_adventure(
        self,
        context: GMToolExecutionContext,
        runtime: Any,
        candidate: HeroDraft,
        completion: Any,
        *,
        evidence: str,
    ) -> GMToolReceipt:
        """完成确定性的开章链，避免让慢模型重复选择必然的后续工具。"""

        runtime_tools = self.host.gm_runtime_tools
        previous_prep_flag = context.metadata.get(
            "_gm_prepared_opening_disables_model_prep"
        )
        context.metadata["_gm_prepared_opening_disables_model_prep"] = True
        try:
            started = runtime_tools.start_session(
                context,
                {
                    "phase": "adventure",
                    "reason": "玩家授权GM补齐第零章后直接开始第一章",
                    "evidence": evidence,
                },
            )
        finally:
            if previous_prep_flag is None:
                context.metadata.pop(
                    "_gm_prepared_opening_disables_model_prep",
                    None,
                )
            else:
                context.metadata[
                    "_gm_prepared_opening_disables_model_prep"
                ] = previous_prep_flag
        if not started.ok:
            return GMToolReceipt.failure(
                "prepare_solo_adventure",
                started.error_code or "SOLO_ADVENTURE_START_FAILED",
                started.message or "补全完成，但第一章没有成功开启。",
                started.correction_hint or "保持第零章状态并检查真实阻塞项。",
                retryable=started.retryable,
                result={"start_session": started.result},
            )

        gate = started.result.get("gate")
        if isinstance(gate, dict):
            context.gate_status = str(gate.get("status") or "adventure")
        else:
            context.gate_status = "adventure"
        opening_contract = started.result.get("opening_contract")
        opening_contract = (
            dict(opening_contract) if isinstance(opening_contract, dict) else {}
        )
        situation_contract = started.result.get("session_situation_contract")
        situation_contract = (
            dict(situation_contract)
            if isinstance(situation_contract, dict)
            else {}
        )
        prepared_opening = (
            dict(completion.opening_scene)
            if isinstance(completion.opening_scene, dict)
            else {}
        )
        location = str(
            prepared_opening.get("location")
            or situation_contract.get("location")
            or opening_contract.get("starting_region")
            or completion.world_updates.get("starting_region")
            or "旅途起点"
        ).strip()
        scene_name = str(
            prepared_opening.get("scene_name")
            or situation_contract.get("title")
            or opening_contract.get("selected_first_act_summary")
            or f"{location}的异变"
        ).strip()
        participants = [
            str(item or "").strip()
            for item in list(opening_contract.get("confirmed_heroes") or [])
            if str(item or "").strip()
        ] or [str(candidate.hero_name or "").strip()]
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
                "location": str(item.get("location") or "").strip(),
            }
            for item in list(
                started.result.get("opening_equipment_restrictions") or []
            )
            if isinstance(item, dict)
            and str(item.get("actor") or "").strip()
            and list(item.get("items") or [])
        ]
        prepared_situation = (
            dict(prepared_opening.get("private_situation") or {})
            if isinstance(prepared_opening.get("private_situation"), dict)
            else {}
        )
        current_pressure = str(
            prepared_situation.get("current_pressure")
            or situation_contract.get("opening_disruption")
            or "眼前的异常正在扩大，已经没有继续准备的余裕。"
        ).strip()
        objective = str(
            prepared_opening.get("objective")
            or situation_contract.get("dramatic_question")
            or opening_contract.get("selected_first_act_summary")
            or "查清眼前异变，并决定第一步如何应对。"
        ).strip()
        fallback_situation = self._fallback_solo_opening_situation(
            situation_contract,
            location=location,
            objective=objective,
            current_pressure=current_pressure,
        )
        for key, value in fallback_situation.items():
            if key not in prepared_situation or not prepared_situation[key]:
                prepared_situation[key] = value
        public_opening = str(
            prepared_opening.get("public_opening")
            or f"{location}的空气被突如其来的异动绷紧了。{current_pressure}"
        ).strip()
        original_hero_name = str(
            completion.hero_story.get("hero_name") or ""
        ).strip()
        actual_hero_name = participants[0]
        if original_hero_name and original_hero_name != actual_hero_name:
            public_opening = public_opening.replace(
                original_hero_name,
                actual_hero_name,
            )
        player_handoff = str(
            prepared_opening.get("player_handoff")
            or f"{actual_hero_name}，你此刻怎么做？"
        ).strip()
        if original_hero_name and original_hero_name != actual_hero_name:
            player_handoff = player_handoff.replace(
                original_hero_name,
                actual_hero_name,
            )
        prepared_composition = {
            "source_tool": "prepare_solo_adventure",
            "private_situation": prepared_situation,
            "public_opening": public_opening,
            "player_handoff": player_handoff,
            "model": completion.model,
            "used_model": completion.used_model,
        }
        previous_composition = context.metadata.get(
            "_gm_prepared_scene_composition"
        )
        context.metadata["_gm_prepared_scene_composition"] = (
            prepared_composition
        )
        try:
            scene = runtime_tools.start_scene(
                context,
                {
                    "name": scene_name[:120],
                    "scene_type": "standard",
                    "location": location,
                    "participants": participants,
                    "equipment_access_changes": restrictions,
                    "objective": objective,
                    "creative_direction": (
                        "直接实现已确认的第一幕，让玩家从一个正在变化的具体局面开始行动；"
                        "保留玩家已定世界地理，不解释后台设计。"
                    ),
                    "private_situation": prepared_situation,
                    "public_opening": public_opening,
                    "player_handoff": player_handoff,
                    "evidence": evidence,
                },
            )
        finally:
            if previous_composition is None:
                context.metadata.pop("_gm_prepared_scene_composition", None)
            else:
                context.metadata["_gm_prepared_scene_composition"] = (
                    previous_composition
                )
        if not scene.ok:
            return GMToolReceipt.failure(
                "prepare_solo_adventure",
                scene.error_code or "SOLO_OPENING_SCENE_FAILED",
                scene.message or "第一章已经准备，但首场没有完整建立。",
                scene.correction_hint or "保持整条事务未提交，修正首场后重试。",
                retryable=scene.retryable,
                result={
                    "start_session": started.result,
                    "start_scene": scene.result,
                },
            )
        return GMToolReceipt.success(
            "prepare_solo_adventure",
            result={
                "gate": started.result.get("gate"),
                "world_map": started.result.get("world_map"),
                "scene": scene.result.get("scene"),
                "creative_author": scene.result.get("creative_author"),
            },
            state_changed=True,
            public_reply=scene.public_fallback_reply,
            lock_public_reply=True,
            pacing_events=list(scene.pacing_events),
            narrative_events=list(scene.narrative_events),
        )

    @staticmethod
    def _fallback_solo_opening_situation(
        contract: dict[str, object],
        *,
        location: str,
        objective: str,
        current_pressure: str,
    ) -> dict[str, object]:
        """把现有场次契约投影成离线首场后备包，不另写一条固定剧情。"""

        clue_routes = [
            dict(item)
            for item in list(contract.get("clue_routes") or [])
            if isinstance(item, dict)
        ]
        clue_pool = [
            str(item.get("visible_lead") or item.get("source") or "").strip()
            for item in clue_routes
            if str(item.get("visible_lead") or item.get("source") or "").strip()
        ]
        possible_reveals = [
            str(item.get("success_reveal") or item.get("conclusion") or "").strip()
            for item in clue_routes
            if str(item.get("success_reveal") or item.get("conclusion") or "").strip()
        ]
        if len(set(clue_pool)) < 2:
            clue_pool.extend(
                [
                    f"{location}现场一处与表面解释不符的痕迹",
                    "一名现场见证者迟疑或隐瞒的反应",
                ]
            )
        if len(set(possible_reveals)) < 2:
            possible_reveals.extend(
                [
                    "异变并非自然发生，而是受到某个可追查因素影响",
                    "现场压力与更大的世界威胁存在一条尚未公开的联系",
                ]
            )
        signature = str(
            contract.get("signature_image")
            or f"{location}边缘一处随异动发生变化的鲜明景物"
        ).strip()
        opposition_goal = str(
            contract.get("opposition_goal")
            or "现场的对立力量想维持现状，阻止英雄查清异变。"
        ).strip()
        dilemma = str(
            contract.get("dilemma")
            or "立即干预能保护眼前的人，但可能失去追查源头的时机。"
        ).strip()
        closure = str(
            contract.get("closure_requirement")
            or "英雄让眼前的异变得到解决或发生不可逆的改变。"
        ).strip()
        irreversible = str(
            contract.get("irreversible_change")
            or "英雄本场造成的局部结果会被写入战役，不会无故复原。"
        ).strip()
        ending_echo = str(
            contract.get("ending_echo")
            or f"收束时再次呈现“{signature}”，并让它因玩家选择发生可见变化。"
        ).strip()
        secrets = [
            str(item or "").strip()
            for item in list(contract.get("flexible_secrets") or [])
            if str(item or "").strip()
        ] or ["异变背后的真实原因尚未公开，可随玩家实际调查路径调整落点。"]
        escalations = [
            str(item or "").strip()
            for item in list(contract.get("escalation_ladder") or [])
            if str(item or "").strip()
        ]
        if len(escalations) < 2:
            escalations.extend(
                [
                    "现场异动影响到一个玩家可以立即接触的人或物。",
                    "对立力量采取一个可观察行动，让拖延产生新的具体代价。",
                ]
            )
        payoffs = [
            str(item or "").strip()
            for item in list(contract.get("possible_payoffs") or [])
            if str(item or "").strip()
        ]
        if len(payoffs) < 2:
            payoffs.extend(["保护眼前的人或资源", "获得通往异变源头的可靠路径"])
        return {
            "premise": str(contract.get("opening_disruption") or current_pressure).strip(),
            "stakes": f"英雄的选择将决定谁能安全离开，以及{objective}",
            "current_pressure": current_pressure,
            "dramatic_question": objective,
            "signature_image": signature,
            "opposition_goal": opposition_goal,
            "dilemma": dilemma,
            "closure_requirement": closure,
            "irreversible_change": irreversible,
            "ending_echo": ending_echo,
            "visible_elements": [
                signature,
                f"{location}里正在扩大、足以迫使人立即反应的异动",
            ],
            "clue_pool": list(dict.fromkeys(clue_pool))[:4],
            "secrets": secrets[:4],
            "possible_reveals": list(dict.fromkeys(possible_reveals))[:4],
            "escalation_ladder": list(dict.fromkeys(escalations))[:4],
            "possible_payoffs": list(dict.fromkeys(payoffs))[:4],
        }

    @staticmethod
    def _solo_completion_world_context(runtime: Any, manager: Any) -> dict[str, object]:
        world = manager.state.world
        return {
            "campaign_title": world.campaign_title,
            "continent_name": world.continent_name,
            "world_shape": world.world_shape,
            "magic_tech_role": world.magic_tech_role,
            "kingdoms": dict(world.kingdoms),
            "historical_events": list(world.historical_events),
            "mysteries": list(world.mysteries),
            "world_threats": list(world.world_threats),
            "group_concept": world.group_concept,
            "starting_region": world.starting_region,
            "major_locations": dict(world.major_locations),
            "map_locations": [
                {
                    "name": str(getattr(item, "name", "") or ""),
                    "feature_type": str(getattr(item, "feature_type", "") or ""),
                    "terrain": str(getattr(item, "terrain", "") or ""),
                    "position_hint": str(getattr(item, "position_hint", "") or ""),
                    "description": str(getattr(item, "description", "") or ""),
                }
                for item in runtime.app.world_state.map_locations.values()
            ],
            "hero_drafts": [
                {
                    "player_name": str(draft.player_name or key or ""),
                    "hero_name": str(draft.hero_name or ""),
                    "identity": str(draft.identity or ""),
                    "theme": str(draft.theme or ""),
                    "origin": str(draft.origin or ""),
                    "confirmed": bool(draft.confirmed),
                }
                for key, draft in world.hero_drafts.items()
            ],
        }

    @staticmethod
    def _missing_only_solo_world_updates(
        manager: Any,
        runtime: Any,
        proposed: dict[str, object],
    ) -> dict[str, object]:
        world = manager.state.world
        result: dict[str, object] = {}
        scalar_fields = (
            "continent_name",
            "world_shape",
            "map_card",
            "magic_tech_role",
            "group_concept",
            "starting_region",
            "selected_first_act_summary",
            "description_style",
        )
        for field_name in scalar_fields:
            if not str(getattr(world, field_name, "") or "").strip() and proposed.get(field_name):
                result[field_name] = proposed[field_name]
        for field_name in (
            "tone_preferences",
            "historical_events",
            "mysteries",
            "world_threats",
        ):
            if not list(getattr(world, field_name, []) or []) and proposed.get(field_name):
                result[field_name] = proposed[field_name]
        if not world.kingdoms and proposed.get("kingdoms"):
            result["kingdoms"] = proposed["kingdoms"]
        if len(runtime.app.world_state.map_locations) < 3 and proposed.get("map_locations"):
            result["map_locations"] = proposed["map_locations"]
        return result

    def _register_standard_session_zero_tools(
        self,
        registry: GMToolRegistry,
    ) -> None:
        registry.register(
            GMToolDefinition(
                name="set_chapter_one_transition",
                description=(
                    "第零章已经满足开章条件时，由GM结合当前消息和最近公开聊天，"
                    "记录桌面是在继续补充，还是适合询问是否现在进入第一章。"
                    "这不是关键词识别，也不会自动开始第一章。玩家正在补设定、"
                    "明确还想讨论或需要时间时使用supplementing；当前内容已经自然收束、"
                    "没有继续补充的意图时使用invited并询问是否开章。"
                    "若玩家已经明确要求开始第一章，直接使用start_session，不调用本工具。"
                ),
                handler=self.set_chapter_one_transition,
                parameters=(
                    GMToolParameter(
                        "posture",
                        "string",
                        "supplementing表示继续补充；invited表示现在询问是否进入第一章。",
                        required=True,
                        enum=("supplementing", "invited"),
                    ),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "支撑本次语义判断的当前玩家原话。",
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
                name="select_first_act",
                description=(
                    "选择现有第一幕候选，或在玩家明确授权GM决定时提交一项自定义第一幕。"
                    "只负责第一幕选择，不批量写入其他世界设定；世界事实使用世界设定CRUD。"
                ),
                handler=self.select_first_act,
                parameters=(
                    GMToolParameter(
                        "candidate_id",
                        "string",
                        "可选。现有第一幕候选ID。",
                    ),
                    GMToolParameter(
                        "custom_summary",
                        "string",
                        "可选。自定义第一幕的标题、前提、眼前危机与英雄首个目标。",
                    ),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前玩家选择候选或授权GM决定第一幕的原话。",
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
                name="propose_session_zero_update",
                description=(
                    "把玩家明确提出、正在征求同伴意见的第零章世界、小队或第一幕方案保存为待定提案。"
                    "玩家不必说出‘暂存’；只要方案内容具体且仍在等待其他玩家确认，就用此工具防止讨论丢失。"
                    "玩家说‘我贡献’‘就这样定’‘记下’等明确陈述时不使用本工具，改用世界设定CRUD逐项正式写入。"
                    "涉及世界设定新增、修改、删除或改名时使用world_operations；先查询并保存准确目标名。"
                    "提案获确认后仍由GM逐项调用世界设定CRUD，本工具和确认工具都不会代替这些操作。"
                    "零散灵感、犹豫中的个人想法和普通闲聊不建立提案；玩家个人的基调、主题、"
                    "表现方式或玩法偏好使用对应偏好与共识字段。待定提案不是已确认世界事实。"
                ),
                handler=self.propose_update,
                parameters=(
                    GMToolParameter("summary", "string", "面向桌面的简短提案摘要。", required=True),
                    GMToolParameter(
                        "updates",
                        "object",
                        "兼容旧存档的非CRUD第零章结构化更新；新世界设定提案改用world_operations。",
                        schema_details=self._world_updates_schema(),
                    ),
                    GMToolParameter(
                        "world_operations",
                        "array",
                        (
                            "提案若获确认后需要逐项执行的世界设定操作。修改、删除和改名必须先查询，"
                            "使用准确类别与名称；这里仅保存计划，不会直接改变世界。"
                        ),
                        schema_details=self._world_operations_schema(),
                    ),
                    GMToolParameter("evidence", "string", "当前玩家消息中的逐字证据。", required=True, source="current_message"),
                ),
                side_effect="write_pending",
            )
        )
        registry.register(
            GMToolDefinition(
                name="record_prologue_setup_answer",
                description=(
                    "在标准第一幕候选已经选定后，记录玩家对规则书序章问题的回答、"
                    "明确跳过，或玩家授权GM决定的答案。任何玩家都可以回答；"
                    "一次只记录当前消息实际处理的一问；未回答的问题继续保持待答状态。"
                ),
                handler=self.record_prologue_setup_answer,
                parameters=(
                    GMToolParameter(
                        "question",
                        "string",
                        "从state_summary.first_act_setup.open_questions中选择的原问题；也可填写其序号。",
                        required=True,
                    ),
                    GMToolParameter(
                        "resolution",
                        "string",
                        "answered表示玩家作答；skipped表示明确跳过；gm_decides表示玩家明确授权GM补全。",
                        required=True,
                        enum=("answered", "skipped", "gm_decides"),
                    ),
                    GMToolParameter(
                        "answer",
                        "string",
                        "answered时写玩家答案；gm_decides时写GM依照已确认设定补出的答案；skipped时省略。",
                    ),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前玩家回答、跳过或授权GM决定的逐字证据。",
                        required=True,
                        source="current_message",
                    ),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="confirm_session_zero_proposal",
                description=(
                    "在玩家明确同意某项待定提案后确认全桌授权。若提案包含world_operations，"
                    "本工具只签发table_consensus授权并强制后续逐项调用世界设定CRUD；"
                    "所有后续回执成功前不得宣称改动已经生效。玩家赞成但同时改名、细化或"
                    "替换内容时，必须提供完整replacement_world_operations；Python只签发"
                    "替换包，绝不会再执行旧提案。若同句另有不在旧提案范围内、但玩家"
                    "本人明确声明的公开新增事实，也放入该完整包；Python会把旧范围签为"
                    "table_consensus，把额外且可证明的非破坏性create签为player_confirmed。"
                    "旧存档中的非CRUD提案仍兼容处理。"
                ),
                handler=self.confirm_proposal,
                parameters=(
                    GMToolParameter("proposal_id", "string", "状态摘要中现存的提案ID。", required=True),
                    GMToolParameter(
                        "replacement_world_operations",
                        "array",
                        (
                            "仅在玩家明确赞成但同时修订提案时填写：确认后要执行的完整替换"
                            "操作包，不是增量补丁。必须完整覆盖原提案范围；同句额外明确"
                            "声明的公开新增事实可一并提供，但不会取得全桌共识权限。数组"
                            "元素只填写operation/category/name/value/attributes/visibility等"
                            "Schema字段；不要填写authority、reason、evidence或source_event_id，"
                            "这些权限与来源由Python按范围签发。"
                        ),
                        schema_details={
                            **self._world_operations_schema(),
                            "maxItems": 24,
                        },
                    ),
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

    @staticmethod
    def _first_act_setup_state(manager: Any) -> dict[str, object]:
        world = manager.state.world
        statuses = manager.prologue_manager.question_status(world)
        open_questions = [
            str(item.get("question") or "")
            for item in statuses
            if item.get("status") == "open"
        ]
        selected = next(
            (
                candidate
                for candidate in world.first_act_candidates
                if candidate.candidate_id == world.selected_first_act_id
            ),
            None,
        )
        applicable = bool(selected is not None and statuses)
        return {
            "applicable": applicable,
            "candidate_id": world.selected_first_act_id,
            "title": selected.title if selected is not None else "",
            "questions": statuses,
            "open_questions": open_questions,
            "next_question": open_questions[0] if open_questions else "",
            "all_resolved": bool(applicable and not open_questions),
            "guidance_only": True,
        }

    def state_summary(self, context: GMToolExecutionContext) -> dict[str, object]:
        runtime = self.host._runtime(context.campaign_id)
        manager = runtime.app.session_zero_manager
        state = manager.state
        world = manager.state.world
        adventure_readiness = self.host._adventure_readiness_snapshot(
            runtime,
            materialize_confirmed_characters=False,
        )
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
        first_act_setup = self._first_act_setup_state(manager)
        return {
            "active": bool(state.active),
            "stage": state.stage.value,
            "missing_topics": manager.missing_topics() if state.active else [],
            "participants": [participant.name for participant in state.participants],
            "participant_contribution_progress": manager.contribution_roster(),
            "proactive_pause": dict(state.proactive_pause or {}),
            "adventure_readiness": adventure_readiness,
            "chapter_one_transition": manager.chapter_one_transition_status(
                ready=bool(adventure_readiness.get("ready"))
            ),
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
                    "group_key": candidate.group_key,
                    "option": candidate.option,
                    "summary": candidate.premise,
                    "questions": list(candidate.questions),
                }
                for candidate in world.first_act_candidates[-6:]
            ],
            "first_act_votes": dict(world.first_act_votes),
            "selected_first_act_id": world.selected_first_act_id,
            "selected_first_act_summary": world.selected_first_act_summary,
            "first_act_setup": first_act_setup,
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
                    "scope_categories": sorted(
                        {
                            category
                            for category, _visibility in self._proposal_scope(item)
                        }
                    ),
                    "scope_subjects": self._proposal_scope_subjects(
                        [
                            category
                            for category, _visibility in self._proposal_scope(item)
                        ]
                    ),
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
        planning = str(arguments.get("purpose") or "answer_player").strip() == "gm_planning"
        return GMToolReceipt(
            tool_name="get_session_zero_readiness",
            ok=True,
            result=readiness,
            public_fallback_reply=("" if planning else self._readiness_public_reply(readiness)),
            lock_public_reply=not planning,
        )

    def select_first_act(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        candidate_id = str(arguments.get("candidate_id") or "").strip()
        custom_summary = str(arguments.get("custom_summary") or "").strip()
        if not candidate_id and not custom_summary:
            return GMToolReceipt.failure(
                "select_first_act",
                "FIRST_ACT_SELECTION_REQUIRED",
                "需要选择一个现有第一幕候选，或提供自定义第一幕摘要。",
                "填写 candidate_id 或 custom_summary 后重试。",
            )
        updates: dict[str, object] = {}
        if candidate_id:
            updates["selected_first_act_id"] = candidate_id
        if custom_summary:
            updates["selected_first_act_summary"] = custom_summary
        receipt = self.commit_update(
            context,
            {
                "updates": updates,
                "evidence": arguments.get("evidence"),
            },
        )
        receipt.tool_name = "select_first_act"
        if receipt.ok:
            receipt.public_fallback_reply = "第一幕定下来了。"
        return receipt

    def set_chapter_one_transition(
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
        readiness = self.host._adventure_readiness_snapshot(
            runtime,
            materialize_confirmed_characters=False,
        )
        if not bool(readiness.get("ready")):
            return GMToolReceipt.failure(
                "set_chapter_one_transition",
                "CHAPTER_ONE_NOT_READY",
                "当前仍有第零章或角色创建缺项，不能发出开章邀请。",
                "读取adventure_readiness并继续处理实际缺项。",
                result={"readiness": readiness},
            )
        posture = str(arguments.get("posture") or "").strip()
        if posture == "invited" and bool(manager.state.proactive_pause):
            return GMToolReceipt.failure(
                "set_chapter_one_transition",
                "PLAYER_REQUESTED_TIME",
                "玩家已经明确表示需要时间考虑，现在不应询问是否开章。",
                "保持静默，等玩家用新的实际内容继续或明确表示已经想好。",
                result={
                    "proactive_pause": dict(manager.state.proactive_pause),
                },
            )
        with runtime.transaction_lock:
            changed, previous = manager.set_chapter_one_transition(
                posture,
                speaker=context.speaker,
                evidence=str(arguments.get("evidence") or ""),
            )
            saved_path = (
                self.host._autosave_campaign(
                    runtime,
                    context.campaign_id,
                )
                if changed
                else str(getattr(runtime, "last_saved_path", "") or "")
            )
        first_announcement = previous not in {"supplementing", "invited"}
        if not changed:
            public_reply = ""
        elif posture == "invited":
            public_reply = "第零章已经准备好了。现在进入第一章吗？"
        elif first_announcement:
            public_reply = (
                "现在已经具备进入第一章的条件；你们想补的内容可以继续说。"
            )
        else:
            public_reply = "好，先继续补充；准备好后再开场。"
        return GMToolReceipt(
            tool_name="set_chapter_one_transition",
            ok=True,
            result={
                "posture": posture,
                "previous_posture": previous,
                "first_announcement": first_announcement,
                "already_in_posture": not changed,
                "should_ask_to_start": posture == "invited",
                "readiness": readiness,
                "saved_path": saved_path,
            },
            state_changed=changed,
            public_fallback_reply=public_reply,
        )

    @staticmethod
    def _readiness_public_reply(readiness: dict[str, object]) -> str:
        if readiness.get("has_session_zero_context") is False:
            return "当前还没有开启第零章，也没有已记录的世界共创或角色草稿。"
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
        runtime, manager, inactive = self._active_manager(context)
        if inactive:
            return inactive
        raw_updates = arguments.get("updates")
        updates: dict[str, Any] = {}
        if raw_updates not in (None, {}):
            updates, error = self._validated_world_updates(raw_updates)
            if error:
                error.tool_name = "propose_session_zero_update"
                return error
        world_operations, operation_error = self._validated_world_operations(
            arguments.get("world_operations"),
            runtime=runtime,
        )
        if operation_error:
            return operation_error
        if not updates and not world_operations:
            return GMToolReceipt(
                tool_name="propose_session_zero_update",
                ok=False,
                error_code="EMPTY_SESSION_ZERO_PROPOSAL",
                message="待定提案没有包含可确认的更新或世界设定操作。",
                correction_hint=(
                    "世界设定变更填写world_operations；仅兼容旧结构时才填写updates。"
                ),
                retryable=True,
            )
        proposal_id = f"proposal-{uuid4().hex[:12]}"
        proposal = {
            "id": proposal_id,
            "speaker": context.speaker,
            "summary": summary,
            "proposed_updates": updates,
            "world_operations": world_operations,
            "evidence": str(arguments.get("evidence") or ""),
            "source_event_id": str(
                context.metadata.get("source_event_id") or ""
            ).strip(),
            "source_message_id": str(
                context.metadata.get("source_message_id") or ""
            ).strip(),
        }
        with runtime.transaction_lock:
            manager.apply_world_updates({"pending_proposals": [proposal]})
            manager.world_state.apply_world_profile(manager.state.world)
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        silent_commit = self._source_statement_can_commit_silently(context)
        return GMToolReceipt(
            tool_name="propose_session_zero_update",
            ok=True,
            result={
                "proposal": proposal,
                "saved_path": saved_path,
                "silent_commit_allowed": silent_commit,
                "source_message_already_public": silent_commit,
            },
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
            first_act_setup = self._first_act_setup_state(manager)
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        readiness = self.host._adventure_readiness_snapshot(
            runtime,
            materialize_confirmed_characters=False,
        )
        transition = manager.chapter_one_transition_status(
            ready=bool(readiness.get("ready")),
        )
        required_followup_tools: list[str] = []
        if bool(readiness.get("ready")) and str(transition.get("status") or "") not in {
            "invited",
            "accepted",
        }:
            required_followup_tools.append("set_chapter_one_transition")
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
                "first_act_setup": first_act_setup,
                "recorded_categories": recorded_categories,
                "adventure_ready": bool(readiness.get("ready")),
                "chapter_one_transition": transition,
                "required_followup_tools": required_followup_tools,
                "required_followup_mode": "all",
                "saved_path": saved_path,
                "silent_commit_allowed": (
                    self._source_statement_can_commit_silently(context)
                    and not required_followup_tools
                ),
                "source_message_already_public": (
                    self._source_statement_can_commit_silently(context)
                    and not required_followup_tools
                ),
            },
            state_changed=True,
            public_fallback_reply=self._public_update_confirmation(recorded_categories),
        )

    def record_prologue_setup_answer(
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
        question = manager.prologue_manager.resolve_question(
            manager.state.world,
            str(arguments.get("question") or ""),
        )
        if not question:
            setup = self._first_act_setup_state(manager)
            return GMToolReceipt(
                tool_name="record_prologue_setup_answer",
                ok=False,
                error_code="UNKNOWN_PROLOGUE_QUESTION",
                message="这不是当前标准开场中的待处理问题。",
                correction_hint=(
                    "从state_summary.first_act_setup.open_questions选择原问题；"
                    "若尚未选定标准候选，先确认第一幕。"
                ),
                retryable=True,
                result={"first_act_setup": setup},
            )
        resolution = str(arguments.get("resolution") or "").strip()
        answer = str(arguments.get("answer") or "").strip()
        if resolution in {"answered", "gm_decides"} and not answer:
            return GMToolReceipt(
                tool_name="record_prologue_setup_answer",
                ok=False,
                error_code="PROLOGUE_ANSWER_REQUIRED",
                message="记录回答或由GM补全时必须提供answer。",
                correction_hint="依据当前消息填写答案；玩家没有授权GM决定时不得代填。",
                retryable=True,
            )
        with runtime.transaction_lock:
            if resolution == "skipped":
                changed = manager.prologue_manager.skip_question(
                    manager.state.world,
                    question,
                )
            else:
                answer_speaker = (
                    "时悠（受玩家委托）"
                    if resolution == "gm_decides"
                    else context.speaker
                )
                changed = manager.prologue_manager.record_question_answer(
                    manager.state.world,
                    question=question,
                    speaker=answer_speaker,
                    answer=answer,
                )
            participant = manager.find_participant(context.speaker)
            evidence = str(arguments.get("evidence") or "").strip()
            if participant is not None and evidence and evidence not in participant.contributions:
                participant.contributions.append(evidence)
            manager.world_state.apply_world_profile(manager.state.world)
            setup = self._first_act_setup_state(manager)
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        fallback = "好，这一问先跳过。" if resolution == "skipped" else "好，这一点记下了。"
        return GMToolReceipt(
            tool_name="record_prologue_setup_answer",
            ok=True,
            result={
                "question": question,
                "resolution": resolution,
                "answer": answer,
                "first_act_setup": setup,
                "saved_path": saved_path,
            },
            state_changed=changed,
            public_fallback_reply=fallback,
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
        replacement_raw = arguments.get("replacement_world_operations")
        replacement_supplied = replacement_raw is not None
        replacement_operations: list[dict[str, Any]] = []
        if replacement_supplied:
            if not isinstance(replacement_raw, list) or not replacement_raw:
                return GMToolReceipt(
                    tool_name="confirm_session_zero_proposal",
                    ok=False,
                    error_code="EMPTY_PROPOSAL_REPLACEMENT",
                    message="修订确认必须提供完整且非空的替换操作包。",
                    correction_hint=(
                        "把玩家确认后的完整版本写入replacement_world_operations；"
                        "若玩家原样接受旧提案，删除该参数。"
                    ),
                    retryable=True,
                )
            replacement_operations, replacement_error = (
                self._validated_world_operations(
                    replacement_raw,
                    runtime=runtime,
                )
            )
            if replacement_error:
                replacement_error.tool_name = "confirm_session_zero_proposal"
                return replacement_error
            raw_legacy_updates = proposal.get("proposed_updates")
            unsupported_legacy_fields = sorted(
                str(key)
                for key in (
                    raw_legacy_updates
                    if isinstance(raw_legacy_updates, dict)
                    else {}
                )
                if str(key) not in WorldSettingCatalog.CATEGORIES
            )
            if unsupported_legacy_fields:
                return GMToolReceipt(
                    tool_name="confirm_session_zero_proposal",
                    ok=False,
                    error_code="PROPOSAL_REPLACEMENT_UNSUPPORTED_LEGACY_FIELDS",
                    message=(
                        "旧提案同时包含不能由世界CRUD安全替换的第零章字段。"
                    ),
                    correction_hint=(
                        "若原样接受就删除replacement_world_operations；"
                        "若要修订，先拒绝或重建为对应专用工具可验证的新提案。"
                    ),
                    retryable=False,
                    result={
                        "proposal_id": proposal_id,
                        "unsupported_fields": unsupported_legacy_fields,
                    },
                )
            original_scope = self._proposal_scope(proposal)
            replacement_scope = {
                (
                    str(item.get("category") or "").strip(),
                    str(item.get("visibility") or "public").strip(),
                )
                for item in replacement_operations
            }
            if not original_scope or not original_scope.issubset(
                replacement_scope
            ):
                return GMToolReceipt(
                    tool_name="confirm_session_zero_proposal",
                    ok=False,
                    error_code="PROPOSAL_REPLACEMENT_SCOPE_MISMATCH",
                    message="修订操作包没有完整覆盖原待定提案的类别或可见域。",
                    correction_hint=(
                        "replacement_world_operations必须至少完整覆盖原提案已有的"
                        "(category, visibility)范围；不要漏掉旧提案中的类别。"
                    ),
                    retryable=True,
                    result={
                        "proposal_id": proposal_id,
                        "original_scope": [
                            {"category": category, "visibility": visibility}
                            for category, visibility in sorted(original_scope)
                        ],
                        "replacement_scope": [
                            {"category": category, "visibility": visibility}
                            for category, visibility in sorted(replacement_scope)
                        ],
                    },
                )
            consensus_operations = [
                item
                for item in replacement_operations
                if (
                    str(item.get("category") or "").strip(),
                    str(item.get("visibility") or "public").strip(),
                )
                in original_scope
            ]
            additional_operations = [
                item
                for item in replacement_operations
                if (
                    str(item.get("category") or "").strip(),
                    str(item.get("visibility") or "public").strip(),
                )
                not in original_scope
            ]
            explicit_player_categories = set(
                GMMessageIntegrityValidator.explicit_player_world_categories(
                    str(context.metadata.get("current_message") or "")
                )
            )
            unsafe_additional = [
                item
                for item in additional_operations
                if (
                    str(item.get("operation") or "").strip() != "create"
                    or str(item.get("visibility") or "public").strip()
                    != "public"
                    or str(item.get("category") or "").strip()
                    not in explicit_player_categories
                )
            ]
            if unsafe_additional:
                return GMToolReceipt(
                    tool_name="confirm_session_zero_proposal",
                    ok=False,
                    error_code="PROPOSAL_REPLACEMENT_ADDITIONAL_OPERATION_UNSAFE",
                    message=(
                        "修订包包含旧提案范围外、且不能由当前玩家原话安全授权的操作。"
                    ),
                    correction_hint=(
                        "原提案范围外只允许当前消息明确声明的public create，并只按"
                        "player_confirmed执行；删除、改名、私密内容或未明说类别必须另行处理。"
                    ),
                    retryable=True,
                    result={
                        "proposal_id": proposal_id,
                        "explicit_player_categories": sorted(
                            explicit_player_categories
                        ),
                        "rejected_operations": [
                            {
                                "operation": str(item.get("operation") or ""),
                                "category": str(item.get("category") or ""),
                                "visibility": str(
                                    item.get("visibility") or "public"
                                ),
                                "name": str(item.get("name") or ""),
                            }
                            for item in unsafe_additional
                        ],
                    },
                )
            operation_mismatch = self._proposal_replacement_operation_mismatch(
                proposal,
                consensus_operations,
            )
            if operation_mismatch:
                return GMToolReceipt(
                    tool_name="confirm_session_zero_proposal",
                    ok=False,
                    error_code="PROPOSAL_REPLACEMENT_OPERATION_MISMATCH",
                    message=(
                        "修订操作包改变了原提案授权的操作类型或破坏性目标。"
                    ),
                    correction_hint=(
                        "新增提案只能替换为同类别新增；修改、删除或改名必须保持"
                        "原操作和准确目标。需要另一项破坏性改动时另行提案。"
                    ),
                    retryable=True,
                    result={
                        "proposal_id": proposal_id,
                        **operation_mismatch,
                    },
                )
            summary = str(proposal.get("summary") or "").strip()
            followup_calls = self._world_operation_followup_calls(
                consensus_operations,
                proposal_id=proposal_id,
                summary=summary,
            )
            followup_calls.extend(
                self._world_operation_followup_calls(
                    additional_operations,
                    proposal_id=proposal_id,
                    summary=summary,
                    authority="player_confirmed",
                )
            )
            scope_categories = sorted(
                {category for category, _visibility in original_scope}
            )
            with runtime.transaction_lock:
                manager.apply_world_updates(
                    {"clear_pending_proposals": [proposal_id]}
                )
                stage = manager.refresh_stage_from_state()
                saved_path = self.host._autosave_campaign(
                    runtime,
                    context.campaign_id,
                )
            return GMToolReceipt(
                tool_name="confirm_session_zero_proposal",
                ok=True,
                result={
                    "proposal_id": proposal_id,
                    "summary": summary,
                    "stage": stage.value,
                    "authority": "table_consensus",
                    "authorized_world_operations": consensus_operations,
                    "additional_player_world_operations": additional_operations,
                    "proposal_resolution": "accepted_with_replacement",
                    "proposal_cleared": True,
                    "proposal_replacement_used": True,
                    "proposal_scope_categories": scope_categories,
                    "proposal_scope_subjects": self._proposal_scope_subjects(
                        scope_categories
                    ),
                    "additional_player_scope_categories": sorted(
                        {
                            str(item.get("category") or "").strip()
                            for item in additional_operations
                            if str(item.get("category") or "").strip()
                        }
                    ),
                    "required_followup_tools": list(
                        dict.fromkeys(
                            str(item.get("tool_name") or "")
                            for item in followup_calls
                            if str(item.get("tool_name") or "")
                        )
                    ),
                    "required_followup_calls": followup_calls,
                    "required_followup_mode": "all",
                    "python_auto_followup_terminal": True,
                    "saved_path": saved_path,
                    "silent_commit_allowed": False,
                    "source_message_already_public": False,
                },
                state_changed=True,
                public_fallback_reply=(
                    "大家已经同意修订后的提案；具体设定改动正在逐项落实。"
                ),
            )
        world_operations, operation_error = self._validated_world_operations(
            proposal.get("world_operations"),
            runtime=runtime,
        )
        if operation_error:
            operation_error.tool_name = "confirm_session_zero_proposal"
            return operation_error
        if world_operations:
            summary = str(proposal.get("summary") or "").strip()
            followup_calls = self._world_operation_followup_calls(
                world_operations,
                proposal_id=proposal_id,
                summary=summary,
            )
            with runtime.transaction_lock:
                manager.apply_world_updates({"clear_pending_proposals": [proposal_id]})
                stage = manager.refresh_stage_from_state()
                saved_path = self.host._autosave_campaign(
                    runtime,
                    context.campaign_id,
                )
            return GMToolReceipt(
                tool_name="confirm_session_zero_proposal",
                ok=True,
                result={
                    "proposal_id": proposal_id,
                    "summary": summary,
                    "stage": stage.value,
                    "authority": "table_consensus",
                    "authorized_world_operations": world_operations,
                    "proposal_resolution": "accepted_as_proposed",
                    "proposal_cleared": True,
                    "proposal_replacement_used": False,
                    "proposal_scope_categories": sorted(
                        {
                            str(item.get("category") or "").strip()
                            for item in world_operations
                            if str(item.get("category") or "").strip()
                        }
                    ),
                    "proposal_scope_subjects": self._proposal_scope_subjects(
                        [
                            str(item.get("category") or "").strip()
                            for item in world_operations
                        ]
                    ),
                    "required_followup_tools": list(
                        dict.fromkeys(
                            str(item.get("tool_name") or "")
                            for item in followup_calls
                            if str(item.get("tool_name") or "")
                        )
                    ),
                    "required_followup_calls": followup_calls,
                    "required_followup_mode": "all",
                    "python_auto_followup_terminal": True,
                    "saved_path": saved_path,
                    "silent_commit_allowed": False,
                    "source_message_already_public": False,
                },
                state_changed=True,
                public_fallback_reply=(
                    "大家已经同意这项提案；具体设定改动仍在逐项落实。"
                ),
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
                "proposal_resolution": "accepted_as_proposed",
                "proposal_cleared": True,
                "proposal_replacement_used": False,
                "proposal_scope_categories": sorted(
                    str(key)
                    for key in raw_updates
                    if str(key) in WorldSettingCatalog.CATEGORIES
                ),
                "proposal_scope_subjects": self._proposal_scope_subjects(
                    [str(key) for key in raw_updates]
                ),
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply="这条提案正式定下来了。",
        )

    def _world_operation_followup_calls(
        self,
        operations: list[dict[str, Any]],
        *,
        proposal_id: str,
        summary: str,
        authority: str = "table_consensus",
    ) -> list[dict[str, object]]:
        calls: list[dict[str, object]] = []
        clean_authority = str(authority or "table_consensus").strip()
        reason = (
            (
                f"全桌确认待定提案 {proposal_id}"
                if clean_authority == "table_consensus"
                else f"玩家在确认提案 {proposal_id} 时另行明确贡献"
            )
            + (f"：{summary}" if summary else "。")
        )
        for operation in operations:
            action = str(operation.get("operation") or "").strip()
            arguments = {
                key: deepcopy(value)
                for key, value in operation.items()
                if key != "operation"
            }
            if action == "rename":
                arguments["old_name"] = arguments.pop("name", "")
            arguments["authority"] = clean_authority
            arguments["reason"] = reason
            calls.append(
                {
                    "tool_name": self._WORLD_OPERATION_TO_TOOL[action],
                    "arguments": arguments,
                    "python_auto_execute": True,
                }
            )
        return calls

    @staticmethod
    def _proposal_scope(
        proposal: dict[str, Any],
    ) -> set[tuple[str, str]]:
        """Return the authority surface a replacement is allowed to touch."""

        scope: set[tuple[str, str]] = set()
        operations = proposal.get("world_operations")
        if isinstance(operations, list):
            for operation in operations:
                if not isinstance(operation, dict):
                    continue
                category = str(operation.get("category") or "").strip()
                visibility = str(
                    operation.get("visibility") or "public"
                ).strip()
                if category:
                    scope.add((category, visibility))
        updates = proposal.get("proposed_updates")
        if isinstance(updates, dict):
            for category in updates:
                clean_category = str(category or "").strip()
                if clean_category in WorldSettingCatalog.CATEGORIES:
                    scope.add((clean_category, "public"))
        return scope

    @staticmethod
    def _proposal_replacement_operation_mismatch(
        proposal: dict[str, Any],
        replacement_operations: list[dict[str, Any]],
    ) -> dict[str, object] | None:
        """Prevent a refinement from gaining new destructive authority."""

        raw_originals = proposal.get("world_operations")
        originals = [
            item
            for item in (
                raw_originals if isinstance(raw_originals, list) else []
            )
            if isinstance(item, dict)
        ]
        if not originals:
            destructive = [
                item
                for item in replacement_operations
                if str(item.get("operation") or "").strip() != "create"
            ]
            if destructive:
                return {
                    "original_kind": "legacy_updates",
                    "rejected_operations": [
                        {
                            "operation": str(item.get("operation") or ""),
                            "category": str(item.get("category") or ""),
                            "name": str(item.get("name") or ""),
                        }
                        for item in destructive
                    ],
                }
            return None

        def identity(item: dict[str, Any]) -> tuple[str, str, str, str]:
            return (
                str(item.get("operation") or "").strip(),
                str(item.get("category") or "").strip(),
                str(item.get("visibility") or "public").strip(),
                str(item.get("name") or item.get("old_name") or "").strip(),
            )

        original_identities = [identity(item) for item in originals]
        rejected: list[dict[str, str]] = []
        for replacement in replacement_operations:
            operation, category, visibility, target = identity(replacement)
            if operation == "create":
                allowed = any(
                    original_operation == "create"
                    and original_category == category
                    and original_visibility == visibility
                    for (
                        original_operation,
                        original_category,
                        original_visibility,
                        _original_target,
                    ) in original_identities
                )
            else:
                allowed = (
                    operation,
                    category,
                    visibility,
                    target,
                ) in original_identities
            if not allowed:
                rejected.append(
                    {
                        "operation": operation,
                        "category": category,
                        "visibility": visibility,
                        "target": target,
                    }
                )
        if not rejected:
            return None
        return {
            "original_operations": [
                {
                    "operation": operation,
                    "category": category,
                    "visibility": visibility,
                    "target": target,
                }
                for operation, category, visibility, target in original_identities
            ],
            "rejected_operations": rejected,
        }

    @staticmethod
    def _proposal_scope_subjects(categories: list[str]) -> list[str]:
        clean = {str(item or "").strip() for item in categories}
        subjects: list[str] = []
        if clean & {
            "continent_name",
            "world_shape",
            "map_locations",
            "major_locations",
        }:
            subjects.append("world_map")
        if "group_concept" in clean:
            subjects.append("group_concept")
        return subjects

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
                    pending_topic = manager.topic_for_pending_question(
                        participant.pending_question
                    )
                    if not pending_topic or pending_topic == topic_code:
                        participant.pending_question = ""
                        changed = True
            stage = manager.refresh_stage_from_state()
            changed = changed or stage != previous_stage
            saved_path = (
                self.host._autosave_campaign(runtime, context.campaign_id)
                if changed
                else str(getattr(runtime, "last_saved_path", "") or "")
            )
        silent_commit = self._source_statement_can_commit_silently(context)
        return GMToolReceipt(
            tool_name="mark_session_zero_topic_complete",
            ok=True,
            result={
                "topic": topic,
                "stage": stage.value,
                "saved_path": saved_path,
                "silent_commit_allowed": silent_commit,
                "source_message_already_public": silent_commit,
            },
            state_changed=changed,
            public_fallback_reply=(
                "好，这一项先跳过。"
                if changed
                else ""
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
            if enabled:
                changed = (
                    manager.resume_proactive_nudges_after_setup_progress()
                    or changed
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
                "resume_condition": "setup_progress_or_explicit_resume",
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
        option_mapping_error = self._hero_skill_option_mapping_error(
            context,
            arguments.get("patch"),
        )
        if option_mapping_error is not None:
            return option_mapping_error
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
        owner = str(draft.player_name or "").strip()
        speaker = str(context.speaker or "").strip()
        if owner and speaker and owner != speaker:
            return GMToolReceipt(
                tool_name="update_hero_draft",
                ok=False,
                error_code="HERO_DRAFT_UPDATE_NOT_OWNER",
                message=f"{speaker}不能修改{owner}的角色草稿。",
                correction_hint=(
                    "只修改当前发言者自己的草稿；代改需要另行建立可审计授权，"
                    "不能只靠模型改subject。"
                ),
                retryable=False,
                result={
                    "record_key": key,
                    "player_name": owner,
                    "hero_name": draft.hero_name,
                },
                public_fallback_reply="这张角色草稿只能由所属玩家本人修改。",
            )
        if not owner and speaker and str(key or "").strip() != speaker:
            return GMToolReceipt(
                tool_name="update_hero_draft",
                ok=False,
                error_code="HERO_DRAFT_OWNER_UNKNOWN",
                message="旧角色草稿缺少可证明的所属玩家，不能直接修改。",
                correction_hint="请先由管理员迁移草稿归属，或让玩家重新建立自己的草稿。",
                retryable=False,
                result={"record_key": key, "hero_name": draft.hero_name},
                public_fallback_reply="这张旧草稿的归属还不明确，所以没有改动。",
            )
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
                    "先读取现有草稿：若目标状态已经满足，直接如实说明；"
                    "否则根据玩家原话提交确实存在差异的字段。"
                ),
                retryable=False,
                state_changed=False,
                result={
                    "record_key": key,
                    "player_name": draft.player_name or context.speaker,
                    "hero_name": draft.hero_name,
                    "already_satisfied_fields": sorted(
                        str(name) for name in patch
                    ),
                },
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
        public_reply = self._hero_patch_public_reply(candidate, patch)
        silent_commit = self._source_statement_can_commit_silently(context)
        return GMToolReceipt(
            tool_name="update_hero_draft",
            ok=True,
            result={
                "record_key": key,
                "player_name": candidate.player_name,
                "hero_name": candidate.hero_name,
                # The message-level completeness gate must verify what the
                # authoritative handler actually accepted, rather than trust
                # the model's proposed patch.  Keep this compact: field names
                # plus the one nested mapping whose semantic destination is
                # easy to confuse with base attributes.
                "changed_fields": sorted(str(name) for name in patch),
                "applied_skill_options": deepcopy(
                    patch.get("skill_options")
                    if isinstance(patch.get("skill_options"), dict)
                    else {}
                ),
                "ready": bool(validation.ready),
                "stage": stage.value,
                "saved_path": saved_path,
                "silent_commit_allowed": silent_commit,
                "source_message_already_public": silent_commit,
            },
            state_changed=True,
            public_fallback_reply=public_reply,
            lock_public_reply=not silent_commit,
        )

    @staticmethod
    def _hero_skill_option_mapping_error(
        context: GMToolExecutionContext,
        raw_patch: object,
    ) -> GMToolReceipt | None:
        """Reject a high-confidence skill option written as base attributes.

        Attribute dice and a skill's attached casting-attribute choice share
        the same Chinese labels, so a structurally valid JSON object can still
        mutate the wrong authority field.  The player's bound source sentence
        is precise enough to reject this one ambiguity deterministically.
        """

        if not isinstance(raw_patch, dict):
            return None
        message = str(context.metadata.get("current_message") or "").strip()
        if "施法属性" not in message or not re.search(r"(?:我)?(?:选|选择|固定|采用)", message):
            return None
        if isinstance(raw_patch.get("skill_options"), dict):
            return None
        attributes = raw_patch.get("attributes")
        if not isinstance(attributes, dict) or not attributes:
            return None
        option_match = re.search(
            r"(?P<skill>[\u3400-\u9fffA-Za-z0-9·（）()]{1,24}?"
            r"(?:系仪式|仪式|咒法))(?:的)?施法属性(?:组合)?(?:我)?"
            r"(?:选|选择|固定|采用|定为|用|是)\s*"
            r"(?P<first>洞察|力量)\s*[+＋与和]\s*(?P<second>意志)",
            message,
        )
        if option_match is None:
            return None
        selected = [option_match.group("first"), option_match.group("second")]
        if not set(selected).intersection(
            str(name or "").strip() for name in attributes
        ):
            return None
        skill_name = str(option_match.group("skill") or "该技能").strip()
        choice = "+".join(selected[:2])
        return GMToolReceipt.failure(
            "update_hero_draft",
            "HERO_SKILL_OPTION_MAPPED_TO_BASE_ATTRIBUTES",
            (
                f"玩家是在选择【{skill_name}】的施法属性，"
                "不是重新分配角色的基础属性骰。"
            ),
            (
                "删除patch.attributes，改用"
                f"patch.skill_options={{\"{skill_name}\":[\"{choice}\"]}}；"
                "保留同一subject与原文evidence后重试。"
            ),
            retryable=True,
            result={
                "skill_name": skill_name,
                "selected_attributes": selected[:2],
                "expected_field": "skill_options",
            },
        )

    @staticmethod
    def _hero_patch_public_reply(candidate: HeroDraft, patch: dict[str, object]) -> str:
        """只确认本次增量，不把角色校验清单变成公开催填。"""

        skills = patch.get("skills")
        if isinstance(skills, dict) and len(skills) == 1:
            return f"【{next(iter(skills))}】记下了。"
        increments = patch.get("increment_skills")
        if isinstance(increments, dict) and len(increments) == 1:
            return f"【{next(iter(increments))}】记下了。"
        options = patch.get("skill_options")
        if isinstance(options, dict) and len(options) == 1:
            return f"【{next(iter(options))}】的选择记下了。"
        spells = patch.get("spells")
        if isinstance(spells, list) and len(spells) == 1:
            return f"【{spells[0]}】记下了。"
        equipment = patch.get("equipment")
        if isinstance(equipment, list) and equipment:
            return "起始装备记好了。"
        hero_name = str(candidate.hero_name or candidate.player_name or "角色").strip()
        return f"{hero_name}的这部分草稿记好了。"

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
        # 私聊匿名性是传输层事实，不能依赖模型是否记得填写可选参数。
        anonymous = bool(context.is_private or arguments.get("anonymous", False))
        with runtime.transaction_lock:
            result = runtime.app.safety_manager.declare(
                kind,
                content,
                speaker="" if anonymous else context.speaker,
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
            result={
                "kind": kind,
                "content": result.item,
                "anonymous": anonymous,
                "saved_path": saved_path,
            },
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
        owner = str(draft.player_name or "").strip()
        speaker = str(context.speaker or "").strip()
        effective_owner = owner
        if not effective_owner and speaker and str(key or "").strip() == speaker:
            effective_owner = speaker
        if not effective_owner and speaker:
            return GMToolReceipt(
                tool_name="confirm_hero_draft",
                ok=False,
                error_code="HERO_DRAFT_OWNER_UNKNOWN",
                message="旧角色草稿缺少可证明的所属玩家，不能确认建卡。",
                correction_hint="请先由管理员迁移草稿归属，或让玩家重新建立自己的草稿。",
                retryable=False,
                result={"record_key": key, "hero_name": draft.hero_name},
                public_fallback_reply="这张旧草稿的归属还不明确，所以暂时不能确认。",
            )
        if effective_owner and speaker and effective_owner != speaker:
            return GMToolReceipt(
                tool_name="confirm_hero_draft",
                ok=False,
                error_code="HERO_DRAFT_CONFIRMATION_NOT_OWNER",
                message=f"{speaker}不能替{effective_owner}确认角色草稿。",
                correction_hint=(
                    "等待角色所属玩家本人明确确认；代确认需要另行建立可审计授权，"
                    "不能只靠模型改subject。"
                ),
                retryable=False,
                result={
                    "record_key": key,
                    "player_name": effective_owner,
                    "hero_name": draft.hero_name,
                },
                public_fallback_reply="这张角色草稿需要由所属玩家本人确认。",
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
                    "player_name": effective_owner,
                    "hero_name": draft.hero_name,
                    "missing_fields": list(validation.missing_fields),
                    "errors": list(validation.errors),
                },
                public_fallback_reply="这张角色草稿还没有满足创建规则，所以暂时没有确认。",
            )
        with runtime.transaction_lock:
            if not draft.player_name and effective_owner:
                draft.player_name = effective_owner
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

    def _validated_world_operations(
        self,
        raw: object,
        *,
        runtime: Any,
    ) -> tuple[list[dict[str, Any]], GMToolReceipt | None]:
        if raw in (None, []):
            return [], None
        if not isinstance(raw, list) or not raw:
            return [], self._invalid_world_operation(
                "world_operations必须是非空数组。"
            )
        catalog = WorldSettingCatalog(runtime.app)
        clean_operations: list[dict[str, Any]] = []
        allowed_fields = {
            "operation",
            "category",
            "name",
            "new_name",
            "value",
            "attributes",
            "visibility",
        }
        for index, item in enumerate(raw, start=1):
            if not isinstance(item, dict):
                return [], self._invalid_world_operation(
                    f"world_operations第{index}项必须是JSON对象。"
                )
            unknown = sorted(set(item) - allowed_fields)
            if unknown:
                return [], self._invalid_world_operation(
                    f"world_operations第{index}项包含未知字段：{'、'.join(unknown)}。"
                )
            operation = str(item.get("operation") or "").strip()
            category = str(item.get("category") or "").strip()
            visibility = str(item.get("visibility") or "public").strip()
            name = str(item.get("name") or "").strip()
            new_name = str(item.get("new_name") or "").strip()
            value = str(item.get("value") or "").strip()
            attributes = item.get("attributes")
            if operation not in self._WORLD_OPERATION_TO_TOOL:
                return [], self._invalid_world_operation(
                    f"world_operations第{index}项的operation无效。"
                )
            if category not in WorldSettingCatalog.CATEGORIES:
                return [], self._invalid_world_operation(
                    f"world_operations第{index}项的category无效。"
                )
            if visibility not in {"public", "gm_private"}:
                return [], self._invalid_world_operation(
                    f"world_operations第{index}项的visibility无效。"
                )
            if attributes is not None and not isinstance(attributes, dict):
                return [], self._invalid_world_operation(
                    f"world_operations第{index}项的attributes必须是JSON对象。"
                )
            if operation in {"create", "update"} and not value:
                return [], self._invalid_world_operation(
                    f"world_operations第{index}项执行{operation}时必须提供value。"
                )
            if operation in {"update", "delete", "rename"}:
                if category not in WorldSettingCatalog.PUBLIC_SCALARS and not name:
                    return [], self._invalid_world_operation(
                        f"world_operations第{index}项执行{operation}时必须提供准确name。"
                    )
                records = catalog.query(
                    category=category,
                    name=name,
                    visibility=visibility,
                )["records"]
                if not records:
                    return [], GMToolReceipt(
                        tool_name="propose_session_zero_update",
                        ok=False,
                        error_code="WORLD_PROPOSAL_TARGET_NOT_FOUND",
                        message=(
                            f"待定操作没有找到准确目标：{category}.{name or category}。"
                        ),
                        correction_hint=(
                            "先调用query_world_settings取得准确名称和当前内容，再保存提案；"
                            "不得用‘刚才那条’等路由摘要代替目标。"
                        ),
                        retryable=True,
                        result={
                            "category": category,
                            "name": name,
                            "visibility": visibility,
                            "revision": catalog.revision,
                        },
                    )
            if operation == "rename" and not new_name:
                return [], self._invalid_world_operation(
                    f"world_operations第{index}项执行rename时必须提供new_name。"
                )
            clean: dict[str, Any] = {
                "operation": operation,
                "category": category,
                "visibility": visibility,
            }
            if name:
                clean["name"] = name
            if new_name:
                clean["new_name"] = new_name
            if value:
                clean["value"] = value
            if isinstance(attributes, dict):
                clean["attributes"] = deepcopy(attributes)
            clean_operations.append(clean)
        return clean_operations, None

    @staticmethod
    def _invalid_world_operation(message: str) -> GMToolReceipt:
        return GMToolReceipt(
            tool_name="propose_session_zero_update",
            ok=False,
            error_code="INVALID_WORLD_PROPOSAL_OPERATION",
            message=message,
            correction_hint=(
                "按world_operations schema修正；修改、删除和改名前先查询准确目标。"
            ),
            retryable=True,
        )

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
    def _is_anonymous_player_placeholder(name: object) -> bool:
        """只识别历史私聊流程产生的明确匿名占位符。"""

        return str(name or "").strip().casefold() == "匿名玩家".casefold()

    @classmethod
    def _is_private_solo_identity(cls, name: object, speaker: str) -> bool:
        """判断旧匿名记录是否可视为当前私聊中的唯一玩家。"""

        clean = str(name or "").strip()
        current = str(speaker or "").strip()
        if not clean or not current:
            return False
        return clean.casefold() == current.casefold() or cls._is_anonymous_player_placeholder(
            clean
        )

    @classmethod
    def _adopt_private_solo_identity(cls, manager: Any, speaker: str) -> bool:
        """把旧单人档的匿名占位记录原样归到当前私聊玩家名下。"""

        current = str(speaker or "").strip()
        if not current:
            return False
        participants = list(manager.state.participants)
        aliases = [
            participant
            for participant in participants
            if cls._is_private_solo_identity(participant.name, current)
        ]
        changed = False
        if aliases:
            target = next(
                (
                    participant
                    for participant in aliases
                    if str(participant.name or "").strip().casefold()
                    == current.casefold()
                ),
                aliases[0],
            )
            if target.name != current:
                target.name = current
                changed = True
            for participant in aliases:
                if participant is target:
                    continue
                for contribution in participant.contributions:
                    if contribution not in target.contributions:
                        target.contributions.append(contribution)
                for topic in participant.answered_topics:
                    if topic not in target.answered_topics:
                        target.answered_topics.append(topic)
                if not target.pending_question and participant.pending_question:
                    target.pending_question = participant.pending_question
                target.proactive_questions_enabled = (
                    target.proactive_questions_enabled
                    and participant.proactive_questions_enabled
                )
                changed = True
            rebuilt: list[Any] = []
            inserted = False
            for participant in participants:
                if participant not in aliases:
                    rebuilt.append(participant)
                    continue
                if not inserted:
                    rebuilt.append(target)
                    inserted = True
            manager.state.participants = rebuilt
            manager.state.current_participant_index = 0

        drafts = manager.state.world.hero_drafts
        if len(drafts) == 1:
            key, draft = next(iter(drafts.items()))
            owner = str(draft.player_name or key or "").strip()
            if cls._is_private_solo_identity(owner, current):
                if key != current:
                    drafts.pop(key)
                    changed = True
                if draft.player_name != current:
                    draft.player_name = current
                    changed = True
                drafts[current] = draft
        return changed

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
        pending_topic = manager.topic_for_pending_question(
            participant.pending_question
        )
        covered_topics = {
            topic
            for field, (_contributor_field, topic) in self._CONTRIBUTION_FIELDS.items()
            if field in updates
        }
        if not pending_topic or pending_topic in covered_topics:
            participant.pending_question = ""
