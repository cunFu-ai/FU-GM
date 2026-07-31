from __future__ import annotations

import re
from dataclasses import asdict, is_dataclass
from typing import Any, Protocol

from fu_gm.components.character_creation_manager import (
    ARMOR_TABLE,
    SHIELD_TABLE,
    STARTING_EQUIPMENT_BUDGET,
    WEAPON_TABLE,
    resolve_equipment_request_text,
)
from fu_gm.gm_evidence import is_current_message_evidence
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolReceipt,
    GMToolRegistry,
)


class CampaignToolHost(Protocol):
    runtimes: dict[str, Any]

    def _list_campaigns(self) -> dict[str, Any]: ...

    def _format_save_list(self, *, current_campaign_id: str = "") -> str: ...

    def _runtime(self, campaign_id: str, *, auto_load: bool = True) -> Any: ...

    def _save_campaign(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def _load_campaign(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]: ...

    def _read_campaign_snapshot(
        self,
        campaign_id: str,
        *,
        slot: str | None = None,
    ) -> dict[str, Any]: ...

    def _new_campaign(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def _delete_campaign(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]: ...

    def _session_status(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def _current_campaign_id(self) -> str: ...

    def _autosave_campaign(self, runtime: Any, campaign_id: str) -> str: ...

    @staticmethod
    def _player_character_control_map(runtime: Any) -> dict[str, list[str]]: ...


class GMCampaignToolService:
    """Validated campaign capabilities available to the GM agent.

    This service owns domain preconditions and side effects. It does not infer
    intent from prose and it does not compose the normal public reply.
    """

    def __init__(self, host: CampaignToolHost) -> None:
        self.host = host

    def build_registry(self) -> GMToolRegistry:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="list_saves",
                description="读取FU-GM已知战役、最新快照和命名存档槽；没有副作用。",
                handler=self.list_saves,
            )
        )
        registry.register(
            GMToolDefinition(
                name="inspect_campaign",
                description=(
                    "只读查看一个存档里的公开世界设定、角色草稿、正式角色、场景与命刻摘要，"
                    "不会切换当前团。玩家说“看看某存档里面有什么”时使用本工具，不要使用load_campaign。"
                ),
                handler=self.inspect_campaign,
                parameters=(
                    GMToolParameter("campaign_id", "string", "要查看的战役名。", required=True),
                    GMToolParameter("slot", "string", "可选的命名存档槽；不填表示最新快照。"),
                ),
            )
        )
        registry.register(
            GMToolDefinition(
                name="create_campaign",
                description=(
                    "新建一个独立战役并切换到该战役。只有玩家明确要求新建且给出名称时使用；"
                    "不能把保存、改名、讨论中的世界名称误当成新建战役。核心GM负责理解"
                    "自然语言中的名称、引用和指代，规则层只校验名称与存储状态。"
                ),
                handler=self.create_campaign,
                parameters=(
                    GMToolParameter("campaign_id", "string", "玩家明确指定的新战役名称。", required=True),
                    GMToolParameter("evidence", "string", "当前消息中明确要求新建并包含名称的逐字片段。", required=True, source="current_message"),
                ),
                side_effect="replace_state",
            )
        )
        registry.register(
            GMToolDefinition(
                name="save_campaign",
                description="保存当前战役；未指定slot时更新最新快照，指定slot时创建或更新命名存档槽。",
                handler=self.save_campaign,
                parameters=(
                    GMToolParameter("campaign_id", "string", "要保存的战役；通常省略并使用当前团。"),
                    GMToolParameter("slot", "string", "可选的命名存档槽。"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="load_campaign",
                description=(
                    "读取一个已存在的战役快照。没有明确战役或槽位时不要猜；先调用list_saves。"
                    "执行前会保存当前团的最新快照。"
                ),
                handler=self.load_campaign,
                parameters=(
                    GMToolParameter("campaign_id", "string", "明确要读取的战役名。"),
                    GMToolParameter("slot", "string", "明确要读取的命名存档槽；不填表示该战役最新快照。"),
                ),
                side_effect="replace_state",
            )
        )
        registry.register(
            GMToolDefinition(
                name="delete_save",
                description=(
                    "删除最新快照、一个命名存档槽或整个战役。只有玩家当前消息明确要求删除"
                    "并指明目标时才能使用；核心GM负责确认自然语言授权，规则层只"
                    "校验目标是否存在、范围是否合法。"
                ),
                handler=self.delete_save,
                parameters=(
                    GMToolParameter(
                        "scope",
                        "string",
                        "latest仅删最新快照，slot删命名槽，campaign删整个战役及日志。",
                        required=True,
                        enum=("latest", "slot", "campaign"),
                    ),
                    GMToolParameter("campaign_id", "string", "目标战役；默认当前战役。"),
                    GMToolParameter("slot", "string", "scope=slot时的命名存档槽。"),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前消息中明确要求删除该目标的逐字片段。",
                        required=True,
                        source="current_message",
                    ),
                ),
                side_effect="replace_state",
            )
        )
        registry.register(
            GMToolDefinition(
                name="get_session_status",
                description="查看当前战役阶段、当前场景、行动者和在离席状态。",
                handler=self.get_session_status,
                parameters=(
                    GMToolParameter("campaign_id", "string", "要查看的战役；通常省略。"),
                ),
            )
        )
        registry.register(
            GMToolDefinition(
                name="set_player_attendance",
                description=(
                    "记录玩家临时离席或回到桌边。player填写QQ玩家名；若玩家只说角色名，"
                    "工具会按权威角色归属反查。离席不会让GM代操角色，也不会自动跳过冲突回合；"
                    "冲突中默认暂停等待该玩家。"
                ),
                handler=self.set_player_attendance,
                parameters=(
                    GMToolParameter(
                        "mode",
                        "string",
                        "away表示离席，back表示回归。",
                        required=True,
                        enum=("away", "back"),
                    ),
                    GMToolParameter(
                        "player",
                        "string",
                        "目标玩家名；本人离席/回归时可省略。",
                    ),
                    GMToolParameter("reason", "string", "可选离席原因。"),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前消息中明确表示离席或回归的逐字片段。",
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
                name="get_hero_drafts",
                description=(
                    "读取权威角色草稿，不修改草稿。可查看发言者本人、点名角色/玩家或全部草稿；"
                    "省略campaign_id时会承接最近只读查看的存档；没有查看焦点时才读取当前团。"
                ),
                handler=self.get_hero_drafts,
                parameters=(
                    GMToolParameter(
                        "scope",
                        "string",
                        "mine查看发言者本人，named查看点名对象，all查看全部。",
                        required=True,
                        enum=("mine", "named", "all"),
                    ),
                    GMToolParameter("subjects", "array", "scope=named时填写玩家名或角色名数组。"),
                    GMToolParameter(
                        "campaign_id",
                        "string",
                        "可选。明确要查看的战役；玩家明确说当前团时传入当前团ID。",
                    ),
                    GMToolParameter("slot", "string", "可选的命名存档槽。"),
                ),
                max_successful_calls_per_message=1,
            )
        )
        registry.register(
            GMToolDefinition(
                name="get_hero_state",
                description=(
                    "读取角色的权威状态，不修改数据。查询金钱、初始装备预算、库存、已装备栏位、"
                    "HP、MP、物资点、物语点、等级、经验、属性、防御、异常状态、职业、技能、"
                    "法术或位置时优先使用本工具，不得依据聊天记录心算。"
                    "草稿与正式角色同时存在时会分别返回，避免把500Z建卡预算误当成正式随身资金。"
                ),
                handler=self.get_hero_state,
                parameters=(
                    GMToolParameter(
                        "scope",
                        "string",
                        "mine查看发言者本人，named查看点名对象，all查看全部。",
                        required=True,
                        enum=("mine", "named", "all"),
                    ),
                    GMToolParameter("subjects", "array", "scope=named时填写玩家名或角色名数组。"),
                    GMToolParameter(
                        "campaign_id",
                        "string",
                        "可选。明确要查看的战役；玩家明确说当前团时传入当前团ID。",
                    ),
                    GMToolParameter("slot", "string", "可选的命名存档槽。"),
                ),
                max_successful_calls_per_message=1,
            )
        )
        registry.register(
            GMToolDefinition(
                name="get_world_state",
                description=(
                    "读取公开世界设定与当前世界状态，不修改数据。可明确指定其他战役或命名存档；"
                    "省略campaign_id时会承接最近只读查看的存档；不得返回GM秘密或未公开线索。"
                ),
                handler=self.get_world_state,
                parameters=(
                    GMToolParameter(
                        "campaign_id",
                        "string",
                        "可选。明确要查看的战役；玩家明确说当前团时传入当前团ID。",
                    ),
                    GMToolParameter("slot", "string", "可选的命名存档槽。"),
                ),
                max_successful_calls_per_message=1,
            )
        )
        return registry

    def create_campaign(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"), "create_campaign")
        if evidence_error is not None:
            return evidence_error
        campaign_id = str(arguments.get("campaign_id") or "").strip()
        if not self._valid_campaign_id(campaign_id):
            return GMToolReceipt(
                tool_name="create_campaign",
                ok=False,
                error_code="INVALID_CAMPAIGN_ID",
                message="战役名不能为空、不能是路径，也不能包含控制字符。",
                correction_hint="使用玩家明确给出的普通战役名称，长度不超过80个字符。",
                retryable=True,
                public_fallback_reply="这个战役名不能直接用，你想换一个名字吗？",
            )
        known_campaigns = {
            str(item.get("campaign_id") or "").strip()
            for item in self.host._list_campaigns().get("campaigns", [])
        }
        known_campaigns.update(self.host.runtimes)
        if campaign_id in known_campaigns:
            return GMToolReceipt(
                tool_name="create_campaign",
                ok=False,
                error_code="CAMPAIGN_ALREADY_EXISTS",
                message=f"战役《{campaign_id}》已经存在。",
                correction_hint="若玩家要继续该战役，改用load_campaign；若要新建，询问另一个名称。",
                retryable=True,
                result={"campaign_id": campaign_id},
                public_fallback_reply=f"《{campaign_id}》已经有存档了。你是想读取它，还是另起一个名字？",
            )

        source_campaign_id = str(context.campaign_id or "").strip()
        if source_campaign_id in self.host.runtimes:
            current_runtime = self.host.runtimes[source_campaign_id]
            with current_runtime.transaction_lock:
                self.host._save_campaign(
                    {
                        "campaign_id": source_campaign_id,
                        "session_id": context.session_id,
                        "channel_id": context.channel_id,
                        "speaker": context.speaker,
                    }
                )
        result = self.host._new_campaign({"campaign_id": campaign_id})
        return GMToolReceipt(
            tool_name="create_campaign",
            ok=True,
            result={
                "campaign_id": campaign_id,
                "path": str(result.get("path") or ""),
                "previous_campaign_id": source_campaign_id,
            },
            state_changed=True,
            public_fallback_reply=str(result.get("reply") or f"已新建战役《{campaign_id}》。"),
        )

    def state_summary(self, context: GMToolExecutionContext) -> dict[str, object]:
        campaigns = self.host._list_campaigns().get("campaigns", [])
        runtime = self.host._runtime(context.campaign_id)
        world = runtime.app.world_state.world_profile
        return {
            "current_campaign_id": self.host._current_campaign_id() or context.campaign_id,
            "message_campaign_id": context.campaign_id,
            "inspection_focus": self._inspection_focus(context),
            "gate_status": context.gate_status,
            "campaigns": [
                {
                    "campaign_id": str(item.get("campaign_id") or ""),
                    "has_latest_snapshot": bool(item.get("has_latest_snapshot")),
                    "slots": [
                        str(slot.get("slot") or "")
                        for slot in (item.get("slot_details") or [])
                        if str(slot.get("slot") or "")
                    ],
                    "active_status": str(item.get("active_status") or ""),
                }
                for item in campaigns
            ],
            "hero_drafts": [
                {
                    "record_key": key,
                    "player_name": draft.player_name,
                    "hero_name": draft.hero_name,
                }
                for key, draft in world.hero_drafts.items()
            ],
        }

    def list_saves(
        self,
        context: GMToolExecutionContext,
        _arguments: dict[str, object],
    ) -> GMToolReceipt:
        result = self.host._list_campaigns()
        current_campaign_id = str(result.get("current_campaign_id") or context.campaign_id)
        return GMToolReceipt(
            tool_name="list_saves",
            ok=True,
            result={
                "current_campaign_id": current_campaign_id,
                "campaigns": list(result.get("campaigns") or []),
            },
            public_fallback_reply=self.host._format_save_list(
                current_campaign_id=current_campaign_id,
            ),
        )

    def inspect_campaign(
        self,
        _context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        campaign_id = str(arguments.get("campaign_id") or "").strip()
        slot = str(arguments.get("slot") or "").strip()
        snapshot, error = self._read_persisted_snapshot(
            campaign_id,
            slot=slot,
            tool_name="inspect_campaign",
        )
        if error is not None:
            return error
        assert snapshot is not None

        overview = self._snapshot_public_overview(snapshot)
        hero_drafts = list(overview.get("hero_drafts") or [])
        characters = list(overview.get("characters") or [])
        world = dict(overview.get("world") or {})
        profile = dict(world.get("profile") or {})
        lines = [
            f"《{campaign_id}》"
            + (f"的存档槽「{slot}」" if slot else "的最新快照")
            + f"保存于 {str(snapshot.get('saved_at') or '未知时间')}。"
        ]
        world_bits = [
            str(profile.get("campaign_title") or "").strip(),
            str(profile.get("world_style") or "").strip(),
            str(profile.get("group_concept") or "").strip(),
        ]
        world_bits = [item for item in world_bits if item]
        if world_bits:
            lines.append("世界：" + "；".join(world_bits) + "。")
        if hero_drafts:
            lines.append(
                "角色草稿："
                + "、".join(
                    f"{item.get('player_name') or item.get('record_key')}（{item.get('hero_name') or '未命名'}）"
                    for item in hero_drafts
                )
                + "。"
            )
        if characters:
            lines.append(
                "正式角色："
                + "、".join(str(item.get("name") or "未命名") for item in characters)
                + "。"
            )
        if not hero_drafts and not characters:
            lines.append("这份快照没有角色草稿或正式角色卡。")
        locations = dict(profile.get("major_locations") or {})
        if locations:
            lines.append("地点：" + "、".join(locations) + "。")
        clocks = list(overview.get("clocks") or [])
        if clocks:
            lines.append(
                "命刻："
                + "、".join(
                    f"{item.get('name') or '未命名'} {item.get('filled', 0)}/{item.get('segments', 0)}"
                    for item in clocks
                )
                + "。"
            )
        return GMToolReceipt(
            tool_name="inspect_campaign",
            ok=True,
            result={
                "campaign_id": campaign_id,
                "slot": slot,
                "saved_at": str(snapshot.get("saved_at") or ""),
                **overview,
            },
            public_fallback_reply="\n".join(lines),
        )

    def save_campaign(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        campaign_id = str(arguments.get("campaign_id") or context.campaign_id).strip()
        slot = str(arguments.get("slot") or "").strip()
        known_campaigns = {
            str(item.get("campaign_id") or "").strip()
            for item in self.host._list_campaigns().get("campaigns", [])
        }
        known_campaigns.update(self.host.runtimes)
        if campaign_id not in known_campaigns and campaign_id != context.campaign_id:
            return GMToolReceipt(
                tool_name="save_campaign",
                ok=False,
                error_code="UNKNOWN_CAMPAIGN",
                message=f"没有找到战役《{campaign_id}》。",
                correction_hint="先调用 list_saves，并从已存在的战役中选择；新建战役需要单独工具。",
                retryable=True,
                result={"known_campaigns": sorted(item for item in known_campaigns if item)},
                public_fallback_reply=f"我没找到战役《{campaign_id}》，所以没有保存。",
            )
        if campaign_id != str(context.campaign_id or "").strip():
            return GMToolReceipt(
                tool_name="save_campaign",
                ok=False,
                error_code="CROSS_CAMPAIGN_SAVE_NOT_ALLOWED",
                message=(
                    f"当前消息属于战役《{context.campaign_id}》，不能直接改写"
                    f"另一个战役《{campaign_id}》的存档。"
                ),
                correction_hint=(
                    "若玩家要操作另一个战役，先调用load_campaign确认切换；"
                    "切换成功后再保存。"
                ),
                retryable=True,
                public_fallback_reply=(
                    f"当前还在《{context.campaign_id}》。要先切到"
                    f"《{campaign_id}》再保存吗？"
                ),
            )

        runtime = self.host._runtime(campaign_id)
        with runtime.transaction_lock:
            result = self.host._save_campaign(
                {
                    "campaign_id": campaign_id,
                    "session_id": context.session_id,
                    "channel_id": context.channel_id,
                    "speaker": context.speaker,
                    "slot": slot,
                }
            )
        return GMToolReceipt(
            tool_name="save_campaign",
            ok=True,
            result={
                "campaign_id": campaign_id,
                "slot": str(result.get("slot") or ""),
                "path": str(result.get("path") or ""),
            },
            state_changed=True,
            public_fallback_reply=str(result.get("reply") or "存好了。"),
        )

    def delete_save(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        tool_name = "delete_save"
        evidence_error = self._validate_evidence(
            context,
            arguments.get("evidence"),
            tool_name,
        )
        if evidence_error is not None:
            return evidence_error
        scope = str(arguments.get("scope") or "").strip()
        campaign_id = str(arguments.get("campaign_id") or context.campaign_id).strip()
        slot = str(arguments.get("slot") or "").strip()
        campaigns = {
            str(item.get("campaign_id") or "").strip(): item
            for item in self.host._list_campaigns().get("campaigns", [])
            if str(item.get("campaign_id") or "").strip()
        }
        if campaign_id not in campaigns:
            return GMToolReceipt(
                tool_name=tool_name,
                ok=False,
                error_code="UNKNOWN_CAMPAIGN",
                message=f"没有找到战役《{campaign_id}》。",
                correction_hint="先调用list_saves并从已存在的战役中选择。",
                retryable=True,
                result={"known_campaigns": sorted(campaigns)},
                public_fallback_reply=f"我没找到战役《{campaign_id}》，所以没有删除任何内容。",
            )
        if scope == "slot":
            if not slot:
                return GMToolReceipt(
                    tool_name=tool_name,
                    ok=False,
                    error_code="DELETE_SLOT_REQUIRED",
                    message="删除命名存档需要指定存档槽名。",
                    correction_hint="先调用list_saves；若玩家没有说明具体槽名，向玩家追问。",
                    retryable=True,
                    public_fallback_reply="你想删除哪一个命名存档？",
                )
            known_slots = {
                str(item.get("slot") or "")
                for item in campaigns[campaign_id].get("slot_details", [])
            }
            if slot not in known_slots:
                return GMToolReceipt(
                    tool_name=tool_name,
                    ok=False,
                    error_code="SAVE_SLOT_NOT_FOUND",
                    message=f"战役《{campaign_id}》没有存档槽「{slot}」。",
                    correction_hint="重新调用list_saves，不要声称已经删除。",
                    retryable=True,
                    result={"known_slots": sorted(known_slots)},
                    public_fallback_reply=f"《{campaign_id}》里没有「{slot}」这个存档，我没有删东西。",
                )
        elif scope not in {"campaign", "latest"}:
            return GMToolReceipt(
                tool_name=tool_name,
                ok=False,
                error_code="INVALID_DELETE_SCOPE",
                message="删除范围必须是latest、slot或campaign。",
                correction_hint="按玩家明确要求选择删除范围。",
                retryable=True,
            )

        status, result = self.host._delete_campaign(
            {
                "campaign_id": campaign_id,
                "slot": slot if scope == "slot" else "",
                "delete_all": scope == "campaign",
                "confirm": "确认删除" if scope == "campaign" else "",
            }
        )
        if status != 200 or not result.get("ok"):
            return GMToolReceipt(
                tool_name=tool_name,
                ok=False,
                error_code="DELETE_FAILED",
                message=str(result.get("error") or "删除失败。"),
                correction_hint="不要声称删除成功；重新列出存档并确认目标。",
                retryable=False,
                result=dict(result),
                public_fallback_reply="这次删除没有成功，现有存档没有变化。",
            )
        return GMToolReceipt(
            tool_name=tool_name,
            ok=True,
            result=dict(result),
            state_changed=True,
            public_fallback_reply=str(result.get("reply") or "已删除。"),
            lock_public_reply=True,
        )

    @staticmethod
    def _valid_campaign_id(value: str) -> bool:
        if not value or len(value) > 80 or value in {".", ".."}:
            return False
        if "/" in value or "\\" in value:
            return False
        return re.search(r"[\x00-\x1f\x7f]", value) is None

    @staticmethod
    def _validate_evidence(
        context: GMToolExecutionContext,
        value: object,
        tool_name: str,
    ) -> GMToolReceipt | None:
        if not is_current_message_evidence(context, value):
            deleting = tool_name == "delete_save"
            return GMToolReceipt(
                tool_name=tool_name,
                ok=False,
                error_code="EVIDENCE_NOT_LITERAL",
                message="evidence不是当前消息中的逐字连续片段。",
                correction_hint=(
                    "从current_message复制明确的删除请求，不使用摘要或推断。"
                    if deleting
                    else "从current_message复制明确的新建请求，不使用摘要或推断。"
                ),
                retryable=True,
                public_fallback_reply=(
                    "我还不能确认你要删除哪份存档，所以没有改动。"
                    if deleting
                    else "你是要新建一个战役吗？如果是，请告诉我战役名。"
                ),
            )
        return None

    def load_campaign(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        requested_campaign = str(arguments.get("campaign_id") or "").strip()
        slot = str(arguments.get("slot") or "").strip()
        campaigns = list(self.host._list_campaigns().get("campaigns") or [])
        campaign_by_id = {
            str(item.get("campaign_id") or "").strip(): item
            for item in campaigns
            if str(item.get("campaign_id") or "").strip()
        }
        if not requested_campaign and not slot:
            return GMToolReceipt(
                tool_name="load_campaign",
                ok=False,
                error_code="LOAD_TARGET_REQUIRED",
                message="读档需要明确战役或存档槽。",
                correction_hint="先调用 list_saves；若有多个合理目标，向玩家追问。",
                retryable=True,
                result={"campaigns": campaigns},
                public_fallback_reply="你想读取哪一个存档？",
            )

        campaign_id = requested_campaign
        if not campaign_id and slot:
            matches = self._campaigns_with_slot(campaigns, slot)
            if len(matches) == 1:
                campaign_id = str(matches[0].get("campaign_id") or "")
            elif not matches:
                return self._slot_not_found(slot, campaigns=campaigns)
            else:
                return GMToolReceipt(
                    tool_name="load_campaign",
                    ok=False,
                    error_code="AMBIGUOUS_SAVE_SLOT",
                    message=f"多个战役都有名为「{slot}」的存档槽。",
                    correction_hint="请同时提供 campaign_id，或向玩家确认要读取哪个战役。",
                    retryable=True,
                    result={
                        "slot": slot,
                        "matching_campaigns": [
                            str(item.get("campaign_id") or "") for item in matches
                        ],
                    },
                    public_fallback_reply=f"有不止一个「{slot}」存档，你想读哪个战役里的？",
                )

        campaign = campaign_by_id.get(campaign_id)
        if campaign is None:
            return GMToolReceipt(
                tool_name="load_campaign",
                ok=False,
                error_code="UNKNOWN_CAMPAIGN",
                message=f"没有找到战役《{campaign_id}》。",
                correction_hint="调用 list_saves 后重新选择，或向玩家确认战役名。",
                retryable=True,
                result={"known_campaigns": sorted(campaign_by_id)},
                public_fallback_reply=f"我没找到战役《{campaign_id}》，没有切换存档。",
            )

        known_slots = {
            str(detail.get("slot") or "")
            for detail in (campaign.get("slot_details") or [])
            if str(detail.get("slot") or "")
        }
        if slot and slot not in known_slots:
            return self._slot_not_found(slot, campaign_id=campaign_id, known_slots=known_slots)
        if not slot and not bool(campaign.get("has_latest_snapshot")):
            return GMToolReceipt(
                tool_name="load_campaign",
                ok=False,
                error_code="LATEST_SNAPSHOT_NOT_FOUND",
                message=f"战役《{campaign_id}》没有最新快照。",
                correction_hint="若存在命名槽，请指定 slot；否则向玩家说明没有可读快照。",
                retryable=bool(known_slots),
                result={"campaign_id": campaign_id, "known_slots": sorted(known_slots)},
                public_fallback_reply=(
                    f"《{campaign_id}》没有最新快照。你想读哪个命名存档？"
                    if known_slots
                    else f"《{campaign_id}》目前没有可读取的快照。"
                ),
            )

        # The service-wide dashboard focus can belong to another QQ channel.
        # A natural-language load must preserve the campaign bound to this
        # message, not whichever campaign was inspected most recently.
        source_campaign_id = str(context.campaign_id or "").strip()
        if source_campaign_id in self.host.runtimes:
            source_runtime = self.host.runtimes[source_campaign_id]
            with source_runtime.transaction_lock:
                self.host._save_campaign(
                    {
                        "campaign_id": source_campaign_id,
                        "session_id": context.session_id,
                        "channel_id": context.channel_id,
                        "speaker": "系统自动保存",
                    }
                )

        status, result = self.host._load_campaign(
            {"campaign_id": campaign_id, "slot": slot}
        )
        if status != 200 or not result.get("ok"):
            return GMToolReceipt(
                tool_name="load_campaign",
                ok=False,
                error_code="LOAD_FAILED",
                message=str(result.get("error") or "读取存档失败。"),
                correction_hint="不要声称已经读档；重新列出存档或向玩家说明失败。",
                retryable=False,
                result=dict(result),
                public_fallback_reply="这次读档没有成功，当前进度没有切换。",
            )
        return GMToolReceipt(
            tool_name="load_campaign",
            ok=True,
            result={
                "campaign_id": campaign_id,
                "slot": str(result.get("slot") or ""),
                "saved_at": str(result.get("saved_at") or ""),
                "loaded_sections": dict(result.get("loaded_sections") or {}),
            },
            state_changed=True,
            public_fallback_reply=str(result.get("reply") or "读档完成。"),
        )

    def get_session_status(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        campaign_id = str(arguments.get("campaign_id") or context.campaign_id).strip()
        result = self.host._session_status(
            {
                "campaign_id": campaign_id,
                "session_id": context.session_id,
                "channel_id": context.channel_id,
            }
        )
        gate = dict(result.get("gate") or {})
        actor = str(result.get("current_actor") or "")
        scene = str(result.get("current_scene") or "")
        status = str(gate.get("status") or result.get("game_phase") or "未知")
        details = [f"当前团是《{campaign_id}》，阶段为{status}"]
        if scene:
            details.append(f"场景是【{scene}】")
        if actor:
            details.append(f"当前行动者是【{actor}】")
        return GMToolReceipt(
            tool_name="get_session_status",
            ok=True,
            result=dict(result),
            public_fallback_reply="；".join(details) + "。",
        )

    def set_player_attendance(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        tool_name = "set_player_attendance"
        evidence_error = self._validate_attendance_evidence(
            context,
            arguments.get("evidence"),
        )
        if evidence_error is not None:
            return evidence_error
        mode = str(arguments.get("mode") or "").strip().lower()
        if mode not in {"away", "back"}:
            return GMToolReceipt.failure(
                tool_name,
                "INVALID_ATTENDANCE_MODE",
                "出勤状态只能是away或back。",
                "根据玩家明确表达重新选择。",
            )
        runtime = self.host._runtime(context.campaign_id)
        requested = str(arguments.get("player") or context.speaker or "").strip()
        player, error = self._resolve_attendance_player(
            runtime,
            context,
            requested,
        )
        if error is not None:
            return error
        reason = str(arguments.get("reason") or "").strip()
        world = runtime.app.world_state
        was_absent = player in world.absent_players
        previous_reason = str(world.absent_players.get(player) or "")
        if mode == "away" and was_absent and previous_reason == reason:
            return GMToolReceipt(
                tool_name=tool_name,
                ok=True,
                result={"player": player, "mode": mode, "attendance": world.attendance_snapshot()},
                state_changed=False,
                public_fallback_reply=f"{player}已经记为临时离席。",
            )
        if mode == "back" and not was_absent:
            return GMToolReceipt(
                tool_name=tool_name,
                ok=True,
                result={"player": player, "mode": mode, "attendance": world.attendance_snapshot()},
                state_changed=False,
                public_fallback_reply=f"{player}现在就在桌边。",
            )

        with runtime.transaction_lock:
            if mode == "away":
                world.mark_player_absent(player, reason)
                summary = f"桌面状态：{player} 临时离席"
                if reason:
                    summary += f"（{reason}）"
                summary += "。"
                tags = ["attendance", "away"]
            else:
                world.mark_player_present(player)
                summary = f"桌面状态：{player} 回到本场。"
                tags = ["attendance", "back"]
            world.record_memory_event(
                summary,
                kind="attendance",
                entities=[player],
                tags=tags,
                source="gm_tool_agent",
            )
            saved_path = self.host._autosave_campaign(
                runtime,
                context.campaign_id,
            )

        controls = self.host._player_character_control_map(runtime)
        controlled_characters = list(controls.get(player, []))
        current_actor = str(
            runtime.app.conflict_manager.state.current_actor() or ""
        )
        conflict_waiting = bool(
            mode == "away"
            and runtime.app.conflict_manager.state.active
            and current_actor in controlled_characters
        )
        if mode == "away":
            fallback = f"好，{player}先离席。"
            if conflict_waiting:
                fallback += f"现在正轮到【{current_actor}】，先停在这里等你回来。"
        else:
            fallback = f"{player}回来了，我们从刚才停下的地方继续。"
        return GMToolReceipt(
            tool_name=tool_name,
            ok=True,
            result={
                "player": player,
                "mode": mode,
                "reason": reason,
                "controlled_characters": controlled_characters,
                "conflict_waiting": conflict_waiting,
                "attendance": world.attendance_snapshot(),
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply=fallback,
        )

    def _resolve_attendance_player(
        self,
        runtime: Any,
        context: GMToolExecutionContext,
        requested: str,
    ) -> tuple[str, GMToolReceipt | None]:
        tool_name = "set_player_attendance"
        controls = self.host._player_character_control_map(runtime)
        if not requested:
            return "", GMToolReceipt.failure(
                tool_name,
                "ATTENDANCE_PLAYER_REQUIRED",
                "没有找到要更新出勤状态的玩家。",
                "本人操作时省略player；替他人记录时使用QQ玩家名。",
            )
        if requested in controls or requested in runtime.app.world_state.present_players:
            player = requested
        else:
            owners = [
                owner
                for owner, heroes in controls.items()
                if requested in heroes
            ]
            if len(owners) != 1:
                return "", GMToolReceipt.failure(
                    tool_name,
                    "ATTENDANCE_PLAYER_UNKNOWN",
                    f"无法把【{requested}】唯一对应到一名玩家。",
                    "调用get_hero_state或get_session_status，使用实际QQ玩家名。",
                )
            player = owners[0]
        if player != context.speaker:
            current = str(context.metadata.get("current_message") or "")
            mentioned = player in current or any(
                hero in current for hero in controls.get(player, [])
            )
            if not mentioned:
                return "", GMToolReceipt.failure(
                    tool_name,
                    "OTHER_PLAYER_ATTENDANCE_NOT_EXPLICIT",
                    "当前消息没有明确点名要更新状态的另一位玩家。",
                    "不要根据沉默推断他人离席；等待明确说明。",
                )
        return player, None

    @staticmethod
    def _validate_attendance_evidence(
        context: GMToolExecutionContext,
        value: object,
    ) -> GMToolReceipt | None:
        evidence = " ".join(str(value or "").split()).strip()
        current = str(context.metadata.get("current_message") or "")
        if evidence and evidence in current:
            return None
        return GMToolReceipt.failure(
            "set_player_attendance",
            "ATTENDANCE_EVIDENCE_REQUIRED",
            "当前消息没有明确表达离席或回归。",
            "不要因玩家暂时沉默就标记离席；只在明确说明时调用。",
        )

    def get_world_state(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        campaign_id, slot, use_persisted = self._read_target(context, arguments)
        if use_persisted:
            snapshot, error = self._read_persisted_snapshot(
                campaign_id,
                slot=slot,
                tool_name="get_world_state",
            )
            if error is not None:
                return error
            assert snapshot is not None
            world = self._public_world_from_snapshot(snapshot)
            source = "persisted_snapshot"
        else:
            runtime = self.host._runtime(campaign_id)
            world = self._public_world_from_runtime(runtime)
            source = "live_runtime"

        profile = dict(world.get("profile") or {})
        lines = [f"《{campaign_id}》的公开世界状态："]
        headline = [
            str(profile.get("campaign_title") or "").strip(),
            str(profile.get("world_style") or "").strip(),
            str(profile.get("group_concept") or "").strip(),
        ]
        headline = [item for item in headline if item]
        if headline:
            lines.append("；".join(headline) + "。")
        core_themes = list(profile.get("core_themes") or [])
        if core_themes:
            lines.append("核心主题：" + "、".join(str(item) for item in core_themes) + "。")
        locations = dict(profile.get("major_locations") or {})
        if locations:
            lines.append(
                "主要地点："
                + "；".join(f"{name}：{description}" for name, description in locations.items())
                + "。"
            )
        factions = dict(profile.get("factions") or {})
        if factions:
            lines.append("势力：" + "；".join(f"{name}：{text}" for name, text in factions.items()) + "。")
        mysteries = list(profile.get("mysteries") or [])
        if mysteries:
            lines.append("已公开奥秘：" + "、".join(str(item) for item in mysteries) + "。")
        threats = list(profile.get("world_threats") or [])
        if threats:
            lines.append("已公开威胁：" + "、".join(str(item) for item in threats) + "。")
        if len(lines) == 1:
            lines.append("目前还没有记录公开世界设定。")
        return GMToolReceipt(
            tool_name="get_world_state",
            ok=True,
            result={
                "campaign_id": campaign_id,
                "slot": slot,
                "source": source,
                "world": world,
            },
            public_fallback_reply="\n".join(lines),
        )

    def get_hero_drafts(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        scope = str(arguments.get("scope") or "").strip()
        raw_subjects = arguments.get("subjects") or []
        if not isinstance(raw_subjects, list) or any(
            not isinstance(subject, str) for subject in raw_subjects
        ):
            return GMToolReceipt(
                tool_name="get_hero_drafts",
                ok=False,
                error_code="INVALID_SUBJECTS",
                message="subjects 必须是玩家名或角色名组成的字符串数组。",
                correction_hint="修正 subjects 后重新调用。",
                retryable=True,
            )
        subjects = [str(subject).strip() for subject in raw_subjects if str(subject).strip()]
        if scope == "named" and not subjects:
            return GMToolReceipt(
                tool_name="get_hero_drafts",
                ok=False,
                error_code="DRAFT_SUBJECT_REQUIRED",
                message="查看点名草稿时需要提供玩家名或角色名。",
                correction_hint="从消息中提取点名对象；若消息没有说明，就向玩家追问。",
                retryable=True,
                public_fallback_reply="你想看谁的角色草稿？",
            )

        campaign_id, slot, use_persisted = self._read_target(context, arguments)
        runtime = None
        if use_persisted:
            snapshot, error = self._read_persisted_snapshot(
                campaign_id,
                slot=slot,
                tool_name="get_hero_drafts",
            )
            if error is not None:
                return error
            assert snapshot is not None
            world_state = snapshot.get("world_state")
            world_state = world_state if isinstance(world_state, dict) else {}
            world_profile = world_state.get("world_profile")
            world_profile = world_profile if isinstance(world_profile, dict) else {}
            raw_drafts = world_profile.get("hero_drafts")
            drafts = raw_drafts if isinstance(raw_drafts, dict) else {}
            source = "persisted_snapshot"
        else:
            runtime = self.host._runtime(campaign_id)
            drafts = runtime.app.world_state.world_profile.hero_drafts
            source = "live_runtime"
        selected = self._select_drafts(
            drafts,
            scope=scope,
            subjects=subjects,
            speaker=context.speaker,
        )
        if not selected:
            known = [
                {
                    "record_key": key,
                    "player_name": self._draft_value(draft, "player_name"),
                    "hero_name": self._draft_value(draft, "hero_name"),
                }
                for key, draft in drafts.items()
            ]
            target_text = "、".join(subjects) if subjects else context.speaker
            return GMToolReceipt(
                tool_name="get_hero_drafts",
                ok=False,
                error_code="HERO_DRAFT_NOT_FOUND",
                message=f"没有找到与「{target_text}」匹配的角色草稿。",
                correction_hint="核对 known_drafts；不要把玩家名和角色名互换。",
                retryable=True,
                result={"known_drafts": known},
                public_fallback_reply=f"我没找到「{target_text}」对应的角色草稿。",
            )

        records: list[dict[str, object]] = []
        fallback_lines = ["当前角色草稿："]
        for key, draft in selected:
            try:
                if runtime is None:
                    raise LookupError
                validation = runtime.app.validate_hero_draft(key)
                missing_fields = list(validation.missing_fields)
                errors = list(validation.errors)
                ready = bool(validation.ready)
            except Exception:
                missing_fields = []
                errors = []
                ready = bool(self._draft_value(draft, "confirmed", False))
            records.append(
                {
                    "record_key": key,
                    "player_name": self._draft_value(draft, "player_name"),
                    "hero_name": self._draft_value(draft, "hero_name"),
                    "identity": self._draft_value(draft, "identity"),
                    "theme": self._draft_value(draft, "theme"),
                    "origin": self._draft_value(draft, "origin"),
                    "classes": dict(self._draft_value(draft, "classes", {}) or {}),
                    "attributes": dict(self._draft_value(draft, "attributes", {}) or {}),
                    "skills": dict(self._draft_value(draft, "skills", {}) or {}),
                    "spells": list(self._draft_value(draft, "spells", []) or []),
                    "equipment": list(self._draft_value(draft, "equipment", []) or []),
                    "notes": list(self._draft_value(draft, "notes", []) or []),
                    "confirmed": bool(self._draft_value(draft, "confirmed", False)),
                    "ready": ready,
                    "missing_fields": missing_fields,
                    "errors": errors,
                }
            )
            owner = str(self._draft_value(draft, "player_name") or key)
            hero_name = str(self._draft_value(draft, "hero_name") or "未命名角色")
            summary = f"- {owner}：{hero_name}"
            identity = str(self._draft_value(draft, "identity") or "")
            if identity:
                summary += f"；{identity}"
            if missing_fields:
                summary += "；还缺：" + "、".join(missing_fields)
            fallback_lines.append(summary)
        return GMToolReceipt(
            tool_name="get_hero_drafts",
            ok=True,
            result={
                "campaign_id": campaign_id,
                "slot": slot,
                "source": source,
                "scope": scope,
                "drafts": records,
            },
            public_fallback_reply="\n".join(fallback_lines),
        )

    def get_hero_state(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        scope = str(arguments.get("scope") or "").strip()
        raw_subjects = arguments.get("subjects") or []
        if not isinstance(raw_subjects, list) or any(
            not isinstance(subject, str) for subject in raw_subjects
        ):
            return GMToolReceipt(
                tool_name="get_hero_state",
                ok=False,
                error_code="INVALID_SUBJECTS",
                message="subjects 必须是玩家名或角色名组成的字符串数组。",
                correction_hint="修正 subjects 后重新调用。",
                retryable=True,
            )
        subjects = [str(subject).strip() for subject in raw_subjects if str(subject).strip()]
        if scope == "named" and not subjects:
            return GMToolReceipt(
                tool_name="get_hero_state",
                ok=False,
                error_code="HERO_SUBJECT_REQUIRED",
                message="查看点名角色状态时需要提供玩家名或角色名。",
                correction_hint="从消息中提取点名对象；若消息没有说明，就向玩家追问。",
                retryable=True,
                public_fallback_reply="你想查谁的状态？",
            )

        campaign_id, slot, use_persisted = self._read_target(context, arguments)
        runtime = None
        if use_persisted:
            snapshot, error = self._read_persisted_snapshot(
                campaign_id,
                slot=slot,
                tool_name="get_hero_state",
            )
            if error is not None:
                return error
            assert snapshot is not None
            world_state = snapshot.get("world_state")
            world_state = world_state if isinstance(world_state, dict) else {}
            world_profile = world_state.get("world_profile")
            world_profile = world_profile if isinstance(world_profile, dict) else {}
            raw_drafts = world_profile.get("hero_drafts")
            drafts = raw_drafts if isinstance(raw_drafts, dict) else {}
            raw_characters = snapshot.get("characters")
            characters = raw_characters if isinstance(raw_characters, list) else []
            source = "persisted_snapshot"
            controls: dict[str, list[str]] = {}
            location_by_name: dict[str, str] = {}
        else:
            runtime = self.host._runtime(campaign_id)
            drafts = runtime.app.world_state.world_profile.hero_drafts
            characters = [
                character
                for character in runtime.app.character_manager.all()
                if "pc" in character.traits
            ]
            source = "live_runtime"
            controls = self.host._player_character_control_map(runtime)
            location_by_name = {
                character.name: runtime.app.scene_manager.location_of(character.name)
                for character in characters
            }

        selected_drafts = self._select_drafts(
            drafts,
            scope=scope,
            subjects=subjects,
            speaker=context.speaker,
        )
        selected_hero_names = {
            str(self._draft_value(draft, "hero_name") or "").strip().casefold()
            for _, draft in selected_drafts
            if str(self._draft_value(draft, "hero_name") or "").strip()
        }
        if scope == "mine":
            selected_hero_names.update(
                str(name).strip().casefold()
                for name in controls.get(context.speaker, [])
                if str(name).strip()
            )
        elif scope == "named":
            selected_hero_names.update(subject.casefold() for subject in subjects)

        selected_characters: list[Any] = []
        for character in characters:
            name = str(
                character.get("name") if isinstance(character, dict) else character.name
            ).strip()
            if (
                scope == "all"
                or name.casefold() in selected_hero_names
            ):
                selected_characters.append(character)

        draft_records: list[dict[str, object]] = []
        for key, draft in selected_drafts:
            record = self._draft_record(key, draft)
            record["equipment_slots"] = dict(
                self._draft_value(draft, "equipment_slots", {}) or {}
            )
            record["equipment_ledger"] = self._draft_equipment_ledger(
                list(self._draft_value(draft, "equipment", []) or [])
            )
            if runtime is not None:
                try:
                    validation = runtime.app.validate_hero_draft(key)
                    record["ready"] = bool(validation.ready)
                    record["missing_fields"] = list(validation.missing_fields)
                    record["errors"] = list(validation.errors)
                except (KeyError, TypeError, ValueError):
                    pass
            draft_records.append(record)

        character_records = [
            self._character_state_record(
                character,
                location=location_by_name.get(
                    str(
                        character.get("name")
                        if isinstance(character, dict)
                        else character.name
                    ),
                    "",
                ),
            )
            for character in selected_characters
        ]

        if not draft_records and not character_records:
            known = [
                {
                    "record_key": key,
                    "player_name": self._draft_value(draft, "player_name"),
                    "hero_name": self._draft_value(draft, "hero_name"),
                }
                for key, draft in drafts.items()
            ]
            known.extend(
                {
                    "hero_name": str(
                        character.get("name")
                        if isinstance(character, dict)
                        else character.name
                    )
                }
                for character in characters
            )
            target_text = "、".join(subjects) if subjects else context.speaker
            return GMToolReceipt(
                tool_name="get_hero_state",
                ok=False,
                error_code="HERO_NOT_FOUND",
                message=f"没有找到与「{target_text}」匹配的角色状态。",
                correction_hint="核对 known_heroes；不要把玩家名和角色名互换。",
                retryable=True,
                result={"known_heroes": known},
                public_fallback_reply=f"我没找到「{target_text}」对应的角色状态。",
            )

        lines: list[str] = []
        for record in character_records:
            lines.append(
                f"{record['name']}：{record['hp']}/{record['max_hp']} HP，"
                f"{record['mp']}/{record['max_mp']} MP，"
                f"{record['zenit']}Z，{record['inventory_points']}/{record['max_inventory_points']}物资点，"
                f"{record['fabula_points']}物语点。"
            )
        for record in draft_records:
            if any(
                str(character.get("name") or "") == str(record.get("hero_name") or "")
                for character in character_records
            ):
                continue
            ledger = dict(record.get("equipment_ledger") or {})
            hero_name = str(record.get("hero_name") or "未命名角色")
            remaining = ledger.get("budget_remaining")
            if remaining is None:
                lines.append(f"{hero_name}仍是角色草稿；初始装备账本含有无法识别的条目。")
            else:
                lines.append(
                    f"{hero_name}仍是角色草稿；500Z初始装备预算还剩{remaining}Z，"
                    "正式建卡时才会掷2d6×10并确定开局随身资金。"
                )
        return GMToolReceipt(
            tool_name="get_hero_state",
            ok=True,
            result={
                "campaign_id": campaign_id,
                "slot": slot,
                "source": source,
                "scope": scope,
                "drafts": draft_records,
                "characters": character_records,
            },
            public_fallback_reply="\n".join(lines),
        )

    @staticmethod
    def _inspection_focus(context: GMToolExecutionContext) -> dict[str, str]:
        raw_focus = context.metadata.get("inspection_focus")
        if not isinstance(raw_focus, dict):
            return {}
        campaign_id = str(raw_focus.get("campaign_id") or "").strip()
        if not campaign_id:
            return {}
        return {
            "campaign_id": campaign_id,
            "slot": str(raw_focus.get("slot") or "").strip(),
        }

    @classmethod
    def _read_target(
        cls,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> tuple[str, str, bool]:
        requested_campaign = str(arguments.get("campaign_id") or "").strip()
        requested_slot = str(arguments.get("slot") or "").strip()
        if requested_campaign:
            # Naming the message's active campaign explicitly overrides an old
            # inspection focus and reads its live state.
            use_persisted = bool(
                requested_slot or requested_campaign != context.campaign_id
            )
            return requested_campaign, requested_slot, use_persisted

        focus = cls._inspection_focus(context)
        focused_campaign = str(focus.get("campaign_id") or "")
        if focused_campaign:
            return (
                focused_campaign,
                requested_slot or str(focus.get("slot") or ""),
                True,
            )
        return context.campaign_id, requested_slot, bool(requested_slot)

    @staticmethod
    def _campaigns_with_slot(campaigns: list[dict[str, Any]], slot: str) -> list[dict[str, Any]]:
        return [
            item
            for item in campaigns
            if slot
            in {
                str(detail.get("slot") or "")
                for detail in (item.get("slot_details") or [])
            }
        ]

    @staticmethod
    def _slot_not_found(
        slot: str,
        *,
        campaigns: list[dict[str, Any]] | None = None,
        campaign_id: str = "",
        known_slots: set[str] | None = None,
        tool_name: str = "load_campaign",
    ) -> GMToolReceipt:
        message = (
            f"战役《{campaign_id}》没有存档槽「{slot}」。"
            if campaign_id
            else f"没有找到存档槽「{slot}」。"
        )
        result: dict[str, object] = (
            {"campaign_id": campaign_id, "known_slots": sorted(known_slots or set())}
            if campaign_id
            else {"campaigns": list(campaigns or [])}
        )
        return GMToolReceipt(
            tool_name=tool_name,
            ok=False,
            error_code="SAVE_SLOT_NOT_FOUND",
            message=message,
            correction_hint="调用 list_saves 后重新选择，或向玩家确认名称。",
            retryable=True,
            result=result,
            public_fallback_reply=message + "当前进度没有改动。",
        )

    @staticmethod
    def _select_drafts(
        drafts: dict[str, Any],
        *,
        scope: str,
        subjects: list[str],
        speaker: str,
    ) -> list[tuple[str, Any]]:
        if scope == "all":
            return list(drafts.items())
        targets = [speaker] if scope == "mine" else subjects
        normalized_targets = {target.casefold() for target in targets}
        selected: list[tuple[str, Any]] = []
        for key, draft in drafts.items():
            candidate_names = {
                str(key).strip().casefold(),
                str(GMCampaignToolService._draft_value(draft, "player_name") or "")
                .strip()
                .casefold(),
                str(GMCampaignToolService._draft_value(draft, "hero_name") or "")
                .strip()
                .casefold(),
            }
            if normalized_targets & candidate_names:
                selected.append((key, draft))
        return selected

    def _read_persisted_snapshot(
        self,
        campaign_id: str,
        *,
        slot: str,
        tool_name: str,
    ) -> tuple[dict[str, Any] | None, GMToolReceipt | None]:
        if not campaign_id:
            return None, GMToolReceipt(
                tool_name=tool_name,
                ok=False,
                error_code="CAMPAIGN_REQUIRED",
                message="查看存档需要明确战役名。",
                correction_hint="从当前消息或最近对话中取得战役名；仍不明确时向玩家追问。",
                retryable=True,
                public_fallback_reply="你想看哪一个战役存档？",
            )
        campaigns = list(self.host._list_campaigns().get("campaigns") or [])
        campaign = next(
            (
                item
                for item in campaigns
                if str(item.get("campaign_id") or "").strip() == campaign_id
            ),
            None,
        )
        if campaign is None:
            return None, GMToolReceipt(
                tool_name=tool_name,
                ok=False,
                error_code="UNKNOWN_CAMPAIGN",
                message=f"没有找到战役《{campaign_id}》。",
                correction_hint="调用list_saves后重新选择，或向玩家确认战役名。",
                retryable=True,
                result={
                    "known_campaigns": [
                        str(item.get("campaign_id") or "") for item in campaigns
                    ]
                },
                public_fallback_reply=f"我没找到战役《{campaign_id}》。",
            )
        known_slots = {
            str(detail.get("slot") or "")
            for detail in list(campaign.get("slot_details") or [])
            if str(detail.get("slot") or "")
        }
        if slot and slot not in known_slots:
            return None, self._slot_not_found(
                slot,
                campaign_id=campaign_id,
                known_slots=known_slots,
                tool_name=tool_name,
            )
        if not slot and not bool(campaign.get("has_latest_snapshot")):
            return None, GMToolReceipt(
                tool_name=tool_name,
                ok=False,
                error_code="LATEST_SNAPSHOT_NOT_FOUND",
                message=f"战役《{campaign_id}》没有最新快照。",
                correction_hint="若存在命名槽，请指定slot；否则说明没有可查看的快照。",
                retryable=bool(known_slots),
                result={"campaign_id": campaign_id, "known_slots": sorted(known_slots)},
                public_fallback_reply=f"《{campaign_id}》目前没有最新快照。",
            )
        try:
            return (
                self.host._read_campaign_snapshot(
                    campaign_id,
                    slot=slot or None,
                ),
                None,
            )
        except Exception as exc:
            return None, GMToolReceipt(
                tool_name=tool_name,
                ok=False,
                error_code="SNAPSHOT_READ_FAILED",
                message=f"读取存档失败：{exc}",
                correction_hint="不要声称存档为空；向玩家说明读取失败并保留当前状态。",
                retryable=False,
                public_fallback_reply="这份存档暂时读取失败了，但我没有改动当前进度。",
            )

    @classmethod
    def _snapshot_public_overview(cls, snapshot: dict[str, Any]) -> dict[str, object]:
        world = cls._public_world_from_snapshot(snapshot)
        world_state = snapshot.get("world_state")
        world_state = world_state if isinstance(world_state, dict) else {}
        world_profile = world_state.get("world_profile")
        world_profile = world_profile if isinstance(world_profile, dict) else {}
        raw_drafts = world_profile.get("hero_drafts")
        raw_drafts = raw_drafts if isinstance(raw_drafts, dict) else {}
        hero_drafts = [
            cls._draft_record(key, draft)
            for key, draft in raw_drafts.items()
            if isinstance(draft, dict)
        ]
        raw_characters = snapshot.get("characters")
        raw_characters = raw_characters if isinstance(raw_characters, list) else []
        characters = [
            cls._character_record(item)
            for item in raw_characters
            if isinstance(item, dict)
        ]
        raw_clocks = snapshot.get("clocks")
        raw_clocks = raw_clocks if isinstance(raw_clocks, list) else []
        clocks = [
            {
                "name": str(item.get("name") or ""),
                "filled": int(item.get("filled") or 0),
                "segments": int(item.get("segments") or 0),
                "completed": bool(item.get("completed")),
            }
            for item in raw_clocks
            if isinstance(item, dict)
        ]
        scene_manager = snapshot.get("scene_manager")
        scene_manager = scene_manager if isinstance(scene_manager, dict) else {}
        current_scene = scene_manager.get("current_scene")
        current_scene = current_scene if isinstance(current_scene, dict) else {}
        return {
            "world": world,
            "hero_drafts": hero_drafts,
            "characters": characters,
            "clocks": clocks,
            "current_scene": {
                "scene_id": str(current_scene.get("scene_id") or ""),
                "title": str(current_scene.get("title") or current_scene.get("name") or ""),
                "location": str(current_scene.get("location") or ""),
            },
        }

    @classmethod
    def _public_world_from_snapshot(cls, snapshot: dict[str, Any]) -> dict[str, object]:
        world_state = snapshot.get("world_state")
        world_state = world_state if isinstance(world_state, dict) else {}
        profile = world_state.get("world_profile")
        profile = profile if isinstance(profile, dict) else {}
        return cls._public_world_summary(profile, world_state)

    @classmethod
    def _public_world_from_runtime(cls, runtime: Any) -> dict[str, object]:
        world_state = runtime.app.world_state
        profile = cls._plain(world_state.world_profile)
        raw_world = {
            "map_locations": cls._plain(world_state.map_locations),
            "map_notes": cls._plain(world_state.map_notes),
            "party_sheet": cls._plain(world_state.party_sheet),
            "world_sheet": cls._plain(world_state.world_sheet),
            "present_players": cls._plain(world_state.present_players),
            "absent_players": cls._plain(world_state.absent_players),
        }
        return cls._public_world_summary(profile, raw_world)

    @classmethod
    def _public_world_summary(
        cls,
        profile: dict[str, Any],
        world_state: dict[str, Any],
    ) -> dict[str, object]:
        public_profile_fields = (
            "campaign_title",
            "continent_name",
            "tone_preferences",
            "playstyle_themes",
            "party_dynamic",
            "description_style",
            "violence_guideline",
            "evil_guidelines",
            "romance_guideline",
            "consensus_notes",
            "pre_session_ready",
            "optional_rules",
            "world_style",
            "world_shape",
            "map_card",
            "travel_day_length",
            "magic_tech_role",
            "pillars",
            "core_themes",
            "group_concept",
            "starting_region",
            "major_locations",
            "kingdoms",
            "historical_events",
            "factions",
            "villain_seeds",
            "villain_mirrors",
            "mysteries",
            "world_threats",
            "safety_lines",
            "safety_veils",
            "selected_first_act_id",
            "selected_first_act_summary",
            "starting_bond_suggestions",
            "open_questions",
            "pending_proposals",
            "completed",
        )
        public_profile = {
            field: cls._plain(profile.get(field))
            for field in public_profile_fields
            if field in profile
        }
        return {
            "profile": public_profile,
            "map_locations": cls._plain(world_state.get("map_locations") or {}),
            "map_notes": cls._plain(world_state.get("map_notes") or {}),
            "party_sheet": cls._plain(world_state.get("party_sheet")),
            "world_sheet": cls._plain(world_state.get("world_sheet")),
            "present_players": cls._plain(world_state.get("present_players") or []),
            "absent_players": cls._plain(world_state.get("absent_players") or {}),
        }

    @classmethod
    def _draft_record(cls, key: str, draft: Any) -> dict[str, object]:
        return {
            "record_key": key,
            "player_name": str(cls._draft_value(draft, "player_name") or ""),
            "hero_name": str(cls._draft_value(draft, "hero_name") or ""),
            "identity": str(cls._draft_value(draft, "identity") or ""),
            "theme": str(cls._draft_value(draft, "theme") or ""),
            "origin": str(cls._draft_value(draft, "origin") or ""),
            "classes": dict(cls._draft_value(draft, "classes", {}) or {}),
            "attributes": dict(cls._draft_value(draft, "attributes", {}) or {}),
            "skills": dict(cls._draft_value(draft, "skills", {}) or {}),
            "spells": list(cls._draft_value(draft, "spells", []) or []),
            "equipment": list(cls._draft_value(draft, "equipment", []) or []),
            "equipment_slots": dict(
                cls._draft_value(draft, "equipment_slots", {}) or {}
            ),
            "confirmed": bool(cls._draft_value(draft, "confirmed", False)),
        }

    @staticmethod
    def _draft_equipment_ledger(equipment: list[str]) -> dict[str, object]:
        items: list[dict[str, object]] = []
        invalid_items: list[str] = []
        total = 0
        for raw_name in equipment:
            raw = str(raw_name or "").strip()
            if not raw:
                continue
            try:
                request = resolve_equipment_request_text(raw)
                template = request.template_name
                definition = (
                    ARMOR_TABLE.get(template)
                    or SHIELD_TABLE.get(template)
                    or WEAPON_TABLE.get(template)
                )
                if definition is None:
                    raise ValueError(f"未知装备模板：{template}")
                price = int(getattr(definition, "price", 0) or 0)
                required_ability = str(
                    getattr(definition, "required_ability", "") or ""
                )
                total += price
                items.append(
                    {
                        "display_name": request.display_name,
                        "template_name": template,
                        "price": price,
                        "required_ability": required_ability,
                    }
                )
            except (KeyError, TypeError, ValueError):
                invalid_items.append(raw)
        return {
            "budget_total": STARTING_EQUIPMENT_BUDGET,
            "spent": total,
            "budget_remaining": (
                STARTING_EQUIPMENT_BUDGET - total if not invalid_items else None
            ),
            "items": items,
            "invalid_items": invalid_items,
            "starting_cash_roll_pending": True,
            "starting_cash_formula": "初始装备预算余款 + 2d6×10Z",
        }

    @classmethod
    def _character_state_record(
        cls,
        character: Any,
        *,
        location: str = "",
    ) -> dict[str, object]:
        def value(field: str, default: Any = None) -> Any:
            if isinstance(character, dict):
                return character.get(field, default)
            return getattr(character, field, default)

        statuses = [
            str(getattr(status, "value", status))
            for status in list(value("statuses", []) or [])
        ]
        attributes = dict(value("attributes", {}) or {})
        return {
            "name": str(value("name", "") or ""),
            "level": int(value("level", 0) or 0),
            "experience_points": int(value("experience_points", 0) or 0),
            "identity": str(value("identity", "") or ""),
            "theme": str(value("theme", "") or ""),
            "origin": str(value("origin", "") or ""),
            "classes": dict(value("classes", {}) or {}),
            "attributes": {
                "敏捷": attributes.get("DEX"),
                "洞察": attributes.get("INS"),
                "力量": attributes.get("MIG"),
                "意志": attributes.get("WLP"),
            },
            "hp": int(value("hp", 0) or 0),
            "max_hp": int(value("max_hp", 0) or 0),
            "crisis_threshold": int(value("crisis_threshold", 0) or 0),
            "mp": int(value("mp", 0) or 0),
            "max_mp": int(value("max_mp", 0) or 0),
            "inventory_points": int(value("inventory_points", 0) or 0),
            "max_inventory_points": int(value("max_inventory_points", 0) or 0),
            "fabula_points": int(value("fabula_points", 0) or 0),
            "zenit": int(value("zenit", 0) or 0),
            "defenses": dict(value("defenses", {}) or {}),
            "statuses": statuses,
            "bonds": cls._plain(value("bonds", []) or []),
            "skills": dict(value("skills", {}) or {}),
            "skill_options": cls._plain(value("skill_options", {}) or {}),
            "spells": list(value("spells", []) or []),
            "bound_arcana": list(value("bound_arcana", []) or []),
            "equipment_inventory": list(value("equipment", []) or []),
            "equipment_templates": dict(value("equipment_templates", {}) or {}),
            "equipped": {
                "main_hand": str(value("equipped_main_hand", "") or ""),
                "off_hand": str(value("equipped_off_hand", "") or ""),
                "armor": str(value("equipped_armor", "") or ""),
                "shield": str(value("equipped_shield", "") or ""),
                "accessory": str(value("equipped_accessory", "") or ""),
            },
            "location": str(location or ""),
        }

    @staticmethod
    def _character_record(character: dict[str, Any]) -> dict[str, object]:
        return {
            "name": str(character.get("name") or ""),
            "level": int(character.get("level") or 0),
            "identity": str(character.get("identity") or ""),
            "theme": str(character.get("theme") or ""),
            "origin": str(character.get("origin") or ""),
            "classes": dict(character.get("classes") or {}),
        }

    @staticmethod
    def _draft_value(draft: Any, field: str, default: Any = "") -> Any:
        if isinstance(draft, dict):
            return draft.get(field, default)
        return getattr(draft, field, default)

    @classmethod
    def _plain(cls, value: Any) -> Any:
        if is_dataclass(value):
            return cls._plain(asdict(value))
        if isinstance(value, dict):
            return {str(key): cls._plain(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [cls._plain(item) for item in value]
        enum_value = getattr(value, "value", None)
        if enum_value is not None and not isinstance(value, (str, int, float, bool)):
            return cls._plain(enum_value)
        return value
