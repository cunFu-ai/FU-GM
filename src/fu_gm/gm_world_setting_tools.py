from __future__ import annotations

from typing import Any, Protocol

from fu_gm.components.gm_tool_continuation_manager import (
    GMToolContinuationManager,
    ResolvedToolContinuation,
)
from fu_gm.components.world_setting_catalog import (
    WorldSettingCatalog,
    WorldSettingCatalogError,
)
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolReceipt,
    GMToolRegistry,
)
from fu_gm.gm_tool_receipts import GMToolReceiptPolicy


class WorldSettingToolHost(Protocol):
    def _runtime(self, campaign_id: str, *, auto_load: bool = True) -> Any: ...

    def _autosave_campaign(self, runtime: Any, campaign_id: str) -> str: ...

    def _adventure_readiness_snapshot(
        self,
        runtime: Any,
        *,
        materialize_confirmed_characters: bool = False,
    ) -> dict[str, Any]: ...


class GMWorldSettingToolService:
    """Composable world-lore capabilities chosen by the GM agent."""

    _CONSENSUS_FOLLOWUP_ISSUER_KEY = "_world_consensus_issuer"
    AUTHORITIES = (
        "player_confirmed",
        "table_consensus",
        "gm_authored",
        "gameplay_consequence",
        "retcon",
    )
    PLAYER_AUTHORITIES = frozenset(
        {"player_confirmed", "table_consensus", "retcon"}
    )
    PROTECTED_AUTHORITIES = frozenset(
        {"player_confirmed", "table_consensus", "retcon", "legacy_confirmed"}
    )
    CONTRIBUTION_CATEGORIES = {
        "kingdoms": ("kingdom_contributors", "kingdom_contributions"),
        "historical_events": (
            "historical_event_contributors",
            "historical_event_contributions",
        ),
        "mysteries": ("mystery_contributors", "mystery_contributions"),
        "world_threats": ("threat_contributors", "threat_contributions"),
    }

    def __init__(self, host: WorldSettingToolHost) -> None:
        self.host = host

    @classmethod
    def _map_attributes_schema(cls) -> dict[str, object]:
        return {
            "properties": {
                "terrain": {"type": "string"},
                "feature_type": {
                    "type": "string",
                    "enum": sorted(WorldSettingCatalog.MAP_FEATURE_TYPES),
                },
                "position_hint": {
                    "type": "string",
                    "enum": ["", *sorted(WorldSettingCatalog.MAP_POSITIONS)],
                    "description": "未知时省略；空字符串会按未提供处理。",
                },
                "relative_to": {"type": "string"},
                "relative_position": {
                    "type": "string",
                    "enum": ["", *sorted(WorldSettingCatalog.MAP_POSITIONS)],
                    "description": "未知时省略；空字符串会按未提供处理。",
                },
                "draw_icon": {"type": "boolean"},
                "icon_id": {"type": "string"},
                "faction": {"type": "string"},
                "discovered": {"type": "boolean"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "notes": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        }

    @classmethod
    def _write_parameters(
        cls,
        *,
        include_value: bool,
        rename: bool = False,
    ) -> tuple[GMToolParameter, ...]:
        parameters: list[GMToolParameter] = [
            GMToolParameter(
                "category",
                "string",
                (
                    "世界设定类别。角色、安全边界、战斗数值不属于本资料库。"
                    "kingdoms用于国家、王国、城邦、村社、部落或联盟等任何"
                    "能作为第零章国家/政治共同体贡献的具名政体；factions只用于"
                    "教团、商会、军团、情报组织等不等同于国家或政治共同体的势力。"
                    "同一实体若既是地图地点又是政治共同体，应分别建立"
                    "map_locations与kingdoms记录；"
                    "同批创建公开国家和地图位置时可以一起提交，系统会先创建国家，"
                    "再为自动生成的同名地图地点补充方位、地形和图标属性。"
                    "historical_events、mysteries、world_threats是列表类别，完整事实"
                    "写入value且省略name。"
                    "major_locations会自动生成同名基础地图地点；map_locations也会自动"
                    "同步到major_locations，二者是同一地点的不同投影。新增一个具名地点"
                    "时只能选其中一个：需要方位、地形或图标属性时直接用map_locations，"
                    "不要在同一批次再创建同名major_locations。"
                ),
                required=True,
                enum=WorldSettingCatalog.CATEGORIES,
            )
        ]
        if rename:
            parameters.extend(
                [
                    GMToolParameter("old_name", "string", "现有实体的准确名称。", required=True),
                    GMToolParameter("new_name", "string", "新的准确名称。", required=True),
                ]
            )
        else:
            parameters.append(
                GMToolParameter(
                    "name",
                    "string",
                    (
                        "具名实体或现有列表项的准确名称。kingdoms、factions、"
                        "major_locations、map_locations等具名类别必须填写；标量类别省略。"
                        "新增列表类别时name不会被单独保存，应省略name并把主体专名在内的"
                        "完整事实全部写入value；若仍填写name，它必须与value逐字相同。"
                        "更新、删除列表项时name必须是查询所得的旧全文。"
                    ),
                )
            )
        if include_value:
            parameters.extend(
                [
                    GMToolParameter(
                        "value",
                        "string",
                        "设定正文。更新列表项时，这是替换后的新文本。",
                        required=True,
                    ),
                    GMToolParameter(
                        "attributes",
                        "object",
                        "仅 map_locations 使用的结构化地图属性。",
                        schema_details=cls._map_attributes_schema(),
                    ),
                ]
            )
        parameters.extend(
            [
                GMToolParameter(
                    "visibility",
                    "string",
                    "public 是已公开事实；gm_private 是时悠的幕后准备。",
                    required=True,
                    enum=("public", "gm_private"),
                ),
                GMToolParameter(
                    "authority",
                    "string",
                    (
                        "本次改动的权限来源。玩家明确决定用 player_confirmed；"
                        "table_consensus只能逐字执行confirm_session_zero_proposal"
                        "签发的Python后续包，普通模型调用不得自行选择；"
                        "GM新增幕后或冒险细节用 gm_authored；"
                        "已在故事中发生的改变用 gameplay_consequence；明确修订旧设定用 retcon。"
                    ),
                    required=True,
                    enum=cls.AUTHORITIES,
                ),
                GMToolParameter(
                    "reason",
                    "string",
                    "简述为何有权且有必要执行本次操作，供审计使用，不会原样发给玩家。",
                    required=True,
                ),
                GMToolParameter(
                    "expected_revision",
                    "integer",
                    "可选。最近一次查询得到的世界设定版本；不一致时拒绝陈旧写入。",
                ),
                GMToolParameter(
                    "evidence",
                    "string",
                    "系统自动绑定的当前玩家原消息。",
                    source="current_message",
                ),
            ]
        )
        return tuple(parameters)

    def register_tools(self, registry: GMToolRegistry) -> None:
        registry.register(
            GMToolDefinition(
                name="query_world_settings",
                description=(
                    "按类别或准确名称查询世界设定及当前版本。需要精确修改、删除、重命名，"
                    "或不确定时悠已经记下什么时先调用。默认只查公开事实；"
                    "gm_private/all 只供主持规划，绝不能在回复中泄露幕后内容。"
                ),
                handler=self.query_world_settings,
                parameters=(
                    GMToolParameter(
                        "category",
                        "string",
                        "可选的世界设定类别。",
                        enum=WorldSettingCatalog.CATEGORIES,
                    ),
                    GMToolParameter("name", "string", "可选的准确实体名或列表项全文。"),
                    GMToolParameter(
                        "visibility",
                        "string",
                        "查询公开、幕后或全部资料；默认 public。",
                        enum=("public", "gm_private", "all"),
                    ),
                ),
                side_effect="read",
                max_model_result_chars=7000,
            )
        )
        registry.register(
            GMToolDefinition(
                name="create_world_setting",
                description=(
                    "新增一个不存在的世界设定。可在第零章记录已确认共创，也可在冒险中新增"
                    "GM准备、刚公开的地点、势力、历史、威胁或任意自定义事实。不会覆盖同名旧事实。"
                ),
                handler=self.create_world_setting,
                parameters=self._write_parameters(include_value=True),
                side_effect="write",
                max_successful_calls_per_message=12,
                max_model_result_chars=2600,
            )
        )
        registry.register(
            GMToolDefinition(
                name="update_world_setting",
                description=(
                    "精确修改一个已经存在的世界设定。标量省略name；映射和地点使用准确名称；"
                    "列表把name作为旧全文、value作为新全文。不会在找不到目标时偷偷新增。"
                ),
                handler=self.update_world_setting,
                parameters=self._write_parameters(include_value=True),
                side_effect="write",
                max_successful_calls_per_message=12,
                max_model_result_chars=2600,
            )
        )
        registry.register(
            GMToolDefinition(
                name="delete_world_setting",
                description=(
                    "删除一个准确的世界设定。删除地点会同步清理地图坐标、路线与引用；"
                    "删除已由玩家或全桌确认的公开事实必须有明确修订或游戏内后果权限。"
                ),
                handler=self.delete_world_setting,
                parameters=self._write_parameters(include_value=False),
                side_effect="write",
                max_successful_calls_per_message=12,
                max_model_result_chars=2600,
                is_destructive=True,
            )
        )
        registry.register(
            GMToolDefinition(
                name="rename_world_setting",
                description=(
                    "原子重命名具名地点、国家、势力、支柱、自定义事实或幕后实体，"
                    "并级联更新地图与引用。标量和列表不使用本工具。"
                ),
                handler=self.rename_world_setting,
                parameters=self._write_parameters(include_value=False, rename=True),
                side_effect="write",
                max_successful_calls_per_message=12,
                max_model_result_chars=2600,
            )
        )

    def state_summary(self, context: GMToolExecutionContext) -> dict[str, object]:
        catalog = self._catalog(context)
        public = catalog.query(visibility="public")
        private = catalog.query(visibility="gm_private")
        return {
            "revision": catalog.revision,
            "public_record_count": len(public["records"]),
            "private_record_count": len(private["records"]),
            "available_categories": list(WorldSettingCatalog.CATEGORIES),
        }

    def query_world_settings(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        try:
            result = self._catalog(context).query(
                category=str(arguments.get("category") or ""),
                name=str(arguments.get("name") or ""),
                visibility=str(arguments.get("visibility") or "public"),
            )
        except WorldSettingCatalogError as exc:
            return self._error("query_world_settings", exc)
        return GMToolReceipt.success("query_world_settings", result=result)

    def create_world_setting(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        return self._mutate("create", context, arguments)

    def update_world_setting(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        return self._mutate("update", context, arguments)

    def delete_world_setting(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        return self._mutate("delete", context, arguments)

    def rename_world_setting(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        return self._mutate("rename", context, arguments)

    def _mutate(
        self,
        operation: str,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        tool_name = f"{operation}_world_setting"
        runtime = self.host._runtime(context.campaign_id)
        catalog = WorldSettingCatalog(runtime.app)
        revision_error = self._revision_error(tool_name, catalog, arguments)
        if revision_error is not None:
            return revision_error
        category = str(arguments.get("category") or "").strip()
        visibility = str(arguments.get("visibility") or "public").strip()
        authority = str(arguments.get("authority") or "").strip()
        reason = str(arguments.get("reason") or "").strip()
        name = str(arguments.get("name") or "").strip()
        old_name = str(arguments.get("old_name") or "").strip()
        value = str(arguments.get("value") or "").strip()
        resumed_continuation: ResolvedToolContinuation | None = None
        if (
            operation == "create"
            and category
            in (WorldSettingCatalog.PUBLIC_LISTS | WorldSettingCatalog.PRIVATE_LISTS)
            and name
            and name != value
        ):
            return GMToolReceipt.failure(
                tool_name,
                "WORLD_LIST_ITEM_NAME_MUST_EQUAL_VALUE",
                "列表型世界设定不会单独保存name，当前短标题会在落档时丢失。",
                (
                    "重新调用同一工具：省略name，并让value包含玩家原话中的主体专名与"
                    "完整事实；或者令name与value逐字相同。"
                ),
                result={
                    "category": category,
                    "persisted_field": "value",
                    "discarded_name": name,
                },
            )
        consensus_error = self._table_consensus_permission_error(
            tool_name,
            context,
            arguments,
            operation=operation,
            authority=authority,
        )
        if consensus_error is not None:
            return consensus_error
        permission_error = self._permission_error(
            tool_name,
            context,
            runtime,
            catalog,
            operation=operation,
            category=category,
            name=old_name or name,
            visibility=visibility,
            authority=authority,
            reason=reason,
        )
        if permission_error is not None:
            return permission_error
        try:
            with runtime.transaction_lock:
                if operation == "create":
                    result = catalog.create(
                        category=category,
                        name=name,
                        value=value,
                        attributes=self._attributes(arguments),
                        visibility=visibility,
                        authority=authority,
                        speaker=context.speaker,
                        reason=reason,
                    )
                elif operation == "update":
                    result = catalog.update(
                        category=category,
                        name=name,
                        value=value,
                        attributes=self._attributes(arguments),
                        visibility=visibility,
                        authority=authority,
                        speaker=context.speaker,
                        reason=reason,
                    )
                elif operation == "delete":
                    result = catalog.delete(
                        category=category,
                        name=name,
                        visibility=visibility,
                        authority=authority,
                        speaker=context.speaker,
                        reason=reason,
                    )
                else:
                    result = catalog.rename(
                        category=category,
                        old_name=old_name,
                        new_name=str(arguments.get("new_name") or ""),
                        visibility=visibility,
                        authority=authority,
                        speaker=context.speaker,
                        reason=reason,
                    )
                if operation in {"create", "update"} and category == "continent_name":
                    resumed_continuation = GMToolContinuationManager(
                        runtime.app.world_state
                    ).resolve_for_field(
                        context,
                        required_field="continent_name",
                        value=result.get("value") or value,
                    )
                self._record_session_zero_contribution(
                    runtime,
                    context,
                    category=category,
                    visibility=visibility,
                    authority=authority,
                    value=str(result.get("name") or result.get("value") or ""),
                )
                saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        except WorldSettingCatalogError as exc:
            if (
                operation == "create"
                and exc.code == "WORLD_SETTING_ALREADY_EXISTS"
                and authority != "table_consensus"
                and category
                in (
                    WorldSettingCatalog.PUBLIC_LISTS
                    | WorldSettingCatalog.PRIVATE_LISTS
                )
            ):
                # A list fact is identified by its complete value. Repeating
                # that exact fact is an idempotent contribution, not a request
                # to rewrite it. Record contributor progress without asking
                # the model to update an unchanged value.
                records = list(
                    catalog.query(
                        category=category,
                        name=value,
                        visibility=visibility,
                    ).get("records")
                    or []
                )
                exact = next(
                    (
                        item
                        for item in records
                        if isinstance(item, dict)
                        and str(item.get("value") or "").strip() == value
                    ),
                    None,
                )
                if isinstance(exact, dict):
                    with runtime.transaction_lock:
                        self._record_session_zero_contribution(
                            runtime,
                            context,
                            category=category,
                            visibility=visibility,
                            authority=authority,
                            value=value,
                        )
                        saved_path = self.host._autosave_campaign(
                            runtime,
                            context.campaign_id,
                        )
                    result = {
                        **exact,
                        "operation": "create",
                        "already_effective": True,
                        "idempotent_contribution": True,
                        "saved_path": saved_path,
                    }
                    self._attach_readiness(runtime, result)
                    silent = self._silent_commit_allowed(context)
                    result["silent_commit_allowed"] = silent
                    result["source_message_already_public"] = silent
                    return GMToolReceipt.success(
                        tool_name,
                        result=result,
                        state_changed=True,
                    )
            if (
                operation == "create"
                and exc.code == "WORLD_SETTING_ALREADY_EXISTS"
                and authority != "table_consensus"
            ):
                suggested_arguments: dict[str, object] = {
                    "category": category,
                    "name": str(exc.result.get("name") or name),
                    "value": value,
                    "visibility": visibility,
                    "authority": authority,
                    "reason": reason,
                    "expected_revision": catalog.revision,
                }
                attributes = self._attributes(arguments)
                if attributes:
                    suggested_arguments["attributes"] = attributes
                result = dict(exc.result)
                result.update(
                    {
                        "required_next_tool": "update_world_setting",
                        "suggested_arguments": suggested_arguments,
                    }
                )
                return GMToolReceipt.failure(
                    tool_name,
                    exc.code,
                    exc.message,
                    (
                        "同名设定已经存在；下一次写操作必须改用 "
                        "update_world_setting，并沿用回执签发的参数。"
                    ),
                    result=result,
                )
            return self._error(tool_name, exc)

        result["saved_path"] = saved_path
        self._attach_readiness(runtime, result)
        self._attach_resumed_continuation(result, resumed_continuation)
        silent = (
            self._silent_commit_allowed(context)
            if resumed_continuation is None
            else False
        )
        result["silent_commit_allowed"] = silent
        result["source_message_already_public"] = silent
        return GMToolReceipt.success(
            tool_name,
            result=result,
            state_changed=True,
        )

    @staticmethod
    def _attributes(arguments: dict[str, object]) -> dict[str, object]:
        raw = arguments.get("attributes")
        return dict(raw) if isinstance(raw, dict) else {}

    def _permission_error(
        self,
        tool_name: str,
        context: GMToolExecutionContext,
        runtime: Any,
        catalog: WorldSettingCatalog,
        *,
        operation: str,
        category: str,
        name: str,
        visibility: str,
        authority: str,
        reason: str,
    ) -> GMToolReceipt | None:
        if authority not in self.AUTHORITIES:
            return GMToolReceipt.failure(
                tool_name,
                "INVALID_WORLD_SETTING_AUTHORITY",
                "缺少有效的世界设定权限来源。",
                "按工具schema选择 authority，不要让规则层猜测玩家意图。",
                result={"allowed_authorities": list(self.AUTHORITIES)},
            )
        if not reason:
            return GMToolReceipt.failure(
                tool_name,
                "WORLD_SETTING_REASON_REQUIRED",
                "世界设定写入缺少审计理由。",
                "用一句后台说明概括权限来源和改动目的。",
            )
        current_message = str(context.metadata.get("current_message") or "").strip()
        if authority in self.PLAYER_AUTHORITIES and not current_message:
            return GMToolReceipt.failure(
                tool_name,
                "PLAYER_AUTHORITY_WITHOUT_MESSAGE",
                "这次调用声称来自玩家或全桌确认，但没有绑定玩家原消息。",
                "等待玩家明确表达后再调用，或改用真实的GM权限来源。",
                retryable=False,
            )
        if authority == "gameplay_consequence" and context.gate_status != "adventure":
            return GMToolReceipt.failure(
                tool_name,
                "GAMEPLAY_CONSEQUENCE_OUTSIDE_ADVENTURE",
                "尚未处于冒险阶段，不能把设定改动伪装成游戏内后果。",
                "第零章使用玩家确认、全桌共识或GM幕后准备权限。",
                retryable=False,
            )
        if visibility == "public" and authority == "gm_authored":
            manager = runtime.app.session_zero_manager
            if manager.state.active and len(manager.state.participants) > 1:
                return GMToolReceipt.failure(
                    tool_name,
                    "MULTIPLAYER_SESSION_ZERO_REQUIRES_CONSENSUS",
                    "多人第零章中的公开共同设定不能由GM单方面定案。",
                    "仍在讨论时保存为提案；玩家或全桌确认后再以对应authority提交。",
                    retryable=False,
                )
        if operation in {"update", "delete", "rename"} and visibility == "public":
            if category not in WorldSettingCatalog.CATEGORIES:
                return None
            existing = catalog.query(
                category=category,
                name=name,
                visibility="public",
            )["records"]
            if not existing:
                return None
            metadata = catalog.metadata_for(category, name, visibility="public")
            prior_authority = str(metadata.get("authority") or "legacy_confirmed")
            if prior_authority in self.PROTECTED_AUTHORITIES and authority == "gm_authored":
                return GMToolReceipt.failure(
                    tool_name,
                    "CONFIRMED_WORLD_FACT_IS_PROTECTED",
                    "这条公开事实来自玩家、全桌或旧存档确认，GM不能无声改写。",
                    "由玩家明确修订，或在冒险中由已发生的游戏后果改变后重试。",
                    retryable=False,
                    result={"prior_authority": prior_authority, "record_revision": metadata.get("revision", 0)},
                )
        return None

    @classmethod
    def _table_consensus_permission_error(
        cls,
        tool_name: str,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
        *,
        operation: str,
        authority: str,
    ) -> GMToolReceipt | None:
        """Require a Python-signed proposal packet for consensus CRUD.

        ``table_consensus`` is a stronger authority than one player's source
        message.  Natural-language evidence therefore cannot grant it.  The
        only accepted path for create/update/delete/rename is the exact
        follow-up packet issued by ``confirm_session_zero_proposal`` and
        remembered by :class:`GMToolReceiptPolicy` for this transaction.

        ``evidence`` is injected by the registry from the current message and
        is deliberately absent from the signed packet.  Every model-owned
        argument must otherwise match exactly; extra fields are not allowed to
        widen or alter the confirmed operation.
        """

        if authority != "table_consensus" or operation not in {
            "create",
            "update",
            "delete",
            "rename",
        }:
            return None

        raw_followup = context.metadata.get(
            GMToolReceiptPolicy.REQUIRED_FOLLOWUP_CONTEXT_KEY
        )
        followup = raw_followup if isinstance(raw_followup, dict) else {}
        source_tool = str(followup.get("source_tool") or "").strip()
        calls = [
            item
            for item in list(followup.get("required_calls") or [])
            if isinstance(item, dict)
        ]
        candidate_calls = [
            item
            for item in calls
            if str(item.get("tool_name") or "").strip() == tool_name
            and item.get("python_auto_execute") is True
            and isinstance(item.get("arguments"), dict)
        ]
        # ``source_tool`` is a single compatibility field and can be replaced
        # when a successful child write adds a different mandatory follow-up.
        # Preserve its trusted origin inside this transaction the first time
        # the confirmation packet is observed, so a multi-call proposal keeps
        # its provenance without trusting the model's ``reason`` text.
        if source_tool == "confirm_session_zero_proposal":
            followup[cls._CONSENSUS_FOLLOWUP_ISSUER_KEY] = source_tool
        trusted_issuer = str(
            followup.get(cls._CONSENSUS_FOLLOWUP_ISSUER_KEY) or ""
        ).strip()
        signed_calls = (
            candidate_calls
            if trusted_issuer == "confirm_session_zero_proposal"
            else []
        )
        if not signed_calls:
            return GMToolReceipt.failure(
                tool_name,
                "TABLE_CONSENSUS_AUTHORITY_NOT_SIGNED",
                (
                    "这次世界设定写入声称使用全桌共识，但当前事务没有"
                    "confirm_session_zero_proposal签发的对应后续调用。"
                ),
                (
                    "如果只是当前玩家明确贡献，请把authority改为player_confirmed；"
                    "如果是在接受待定提案，必须先确认该提案并执行Python签发的"
                    "required_followup_calls。"
                ),
                result={
                    "required_source_tool": "confirm_session_zero_proposal",
                    "active_source_tool": source_tool,
                    "expected_tool_name": tool_name,
                },
            )

        actual_arguments = {
            key: value
            for key, value in arguments.items()
            if key != "evidence"
        }
        if any(
            dict(call.get("arguments") or {}) == actual_arguments
            for call in signed_calls
        ):
            return None

        return GMToolReceipt.failure(
            tool_name,
            "TABLE_CONSENSUS_FOLLOWUP_MISMATCH",
            (
                "当前事务确有全桌确认回执，但本次世界设定调用的参数"
                "不等于Python签发的对应后续参数。"
            ),
            "逐字执行required_followup_calls中的工具名和arguments，不得增删或改写字段。",
            result={
                "source_tool": source_tool,
                "expected_tool_name": tool_name,
                "signed_call_count": len(signed_calls),
                "actual_argument_keys": sorted(actual_arguments),
            },
        )

    @staticmethod
    def _revision_error(
        tool_name: str,
        catalog: WorldSettingCatalog,
        arguments: dict[str, object],
    ) -> GMToolReceipt | None:
        raw = arguments.get("expected_revision")
        if raw is None:
            return None
        try:
            expected = int(raw)
        except (TypeError, ValueError):
            return GMToolReceipt.failure(
                tool_name,
                "INVALID_WORLD_SETTING_REVISION",
                "expected_revision 必须是整数。",
                "重新查询世界设定并使用返回的 revision。",
            )
        if expected == catalog.revision:
            return None
        return GMToolReceipt.failure(
            tool_name,
            "WORLD_SETTING_VERSION_CONFLICT",
            f"世界设定已从版本 {expected} 更新到 {catalog.revision}。",
            "调用 query_world_settings 获取最新内容后重新规划，不要覆盖新改动。",
            result={"expected_revision": expected, "current_revision": catalog.revision},
        )

    def _record_session_zero_contribution(
        self,
        runtime: Any,
        context: GMToolExecutionContext,
        *,
        category: str,
        visibility: str,
        authority: str,
        value: str,
    ) -> None:
        manager = runtime.app.session_zero_manager
        if (
            not manager.state.active
            or visibility != "public"
            or authority not in self.PLAYER_AUTHORITIES
            or authority == "table_consensus"
        ):
            return
        manager.ensure_participants([context.speaker])
        participant = manager.find_participant(context.speaker)
        if participant is None:
            return
        evidence = str(context.metadata.get("current_message") or "").strip()
        if evidence and evidence not in participant.contributions:
            participant.contributions.append(evidence)
        contribution = self.CONTRIBUTION_CATEGORIES.get(category)
        if contribution is not None:
            contributor_field, topic = contribution
            bucket = getattr(manager.state.world, contributor_field)
            bucket.setdefault(context.speaker, [])
            if value and value not in bucket[context.speaker]:
                bucket[context.speaker].append(value)
            if topic not in participant.answered_topics:
                participant.answered_topics.append(topic)
            pending_topic = manager.topic_for_pending_question(
                participant.pending_question
            )
            # A successful country write must not silently erase a still-open
            # history/mystery/threat question (and vice versa).  Only the
            # category that actually received an authoritative write may
            # resolve a specific pending prompt.  Generic legacy prompts keep
            # their prior one-write behaviour.
            if not pending_topic or pending_topic == topic:
                participant.pending_question = ""
        manager.refresh_stage_from_state()

    def _attach_readiness(self, runtime: Any, result: dict[str, object]) -> None:
        manager = runtime.app.session_zero_manager
        if not manager.state.active:
            return
        readiness = self.host._adventure_readiness_snapshot(
            runtime,
            materialize_confirmed_characters=False,
        )
        transition = manager.chapter_one_transition_status(
            ready=bool(readiness.get("ready")),
        )
        required: list[str] = []
        if bool(readiness.get("ready")) and str(transition.get("status") or "") not in {
            "invited",
            "accepted",
        }:
            required.append("set_chapter_one_transition")
        result["adventure_ready"] = bool(readiness.get("ready"))
        result["chapter_one_transition"] = transition
        result["required_followup_tools"] = required
        result["required_followup_mode"] = "all"

    @staticmethod
    def _attach_resumed_continuation(
        result: dict[str, object],
        continuation: ResolvedToolContinuation | None,
    ) -> None:
        if continuation is None or not continuation.tool_name:
            return
        required = [
            str(item or "").strip()
            for item in list(result.get("required_followup_tools") or [])
            if str(item or "").strip()
        ]
        if continuation.tool_name not in required:
            required.append(continuation.tool_name)
        calls = [
            dict(item)
            for item in list(result.get("required_followup_calls") or [])
            if isinstance(item, dict)
        ]
        calls.append(
            {
                "tool_name": continuation.tool_name,
                "arguments": dict(continuation.arguments),
                "python_auto_execute": True,
            }
        )
        result["required_followup_tools"] = required
        result["required_followup_calls"] = calls
        result["required_followup_mode"] = "all"
        result["resumed_continuation"] = {
            "continuation_id": continuation.window_id,
            "required_field": continuation.required_field,
            "resume_tool": continuation.tool_name,
            "requester": continuation.requester,
        }

    @staticmethod
    def _silent_commit_allowed(context: GMToolExecutionContext) -> bool:
        metadata = context.metadata
        explicitly_addressed = bool(
            context.is_private
            or metadata.get("is_at_bot")
            or metadata.get("is_reply_to_bot")
            or metadata.get("identity_addressed")
            or metadata.get("_semantic_gm_addressed")
        )
        return not explicitly_addressed

    def _catalog(self, context: GMToolExecutionContext) -> WorldSettingCatalog:
        return WorldSettingCatalog(self.host._runtime(context.campaign_id).app)

    @staticmethod
    def _error(tool_name: str, exc: WorldSettingCatalogError) -> GMToolReceipt:
        return GMToolReceipt.failure(
            tool_name,
            exc.code,
            exc.message,
            exc.hint,
            result=exc.result,
        )


__all__ = ["GMWorldSettingToolService"]
