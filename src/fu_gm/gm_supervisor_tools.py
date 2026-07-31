from __future__ import annotations

from typing import Any, Protocol

from fu_gm.components.gm_agent_capability_policy import (
    GMToolAgentCapabilityPolicy,
)
from fu_gm.components.gm_supervisor import (
    GMCapabilityBroker,
    GMSupervisorMonitor,
)
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolReceipt,
    GMToolRegistry,
)


class SupervisorToolHost(Protocol):
    gm_supervisor: GMSupervisorMonitor

    def _runtime(self, campaign_id: str) -> Any: ...

    def _autosave_campaign(self, runtime: Any, campaign_id: str) -> str: ...


class GMSupervisorToolService:
    """Typed meta-capabilities for the bounded GM control plane."""

    def __init__(self, host: Any) -> None:
        self.host = host

    def register_tools(self, registry: GMToolRegistry) -> None:
        registry.register(
            GMToolDefinition(
                name=GMCapabilityBroker.DISCOVERY_TOOL,
                description=(
                    "按语义领域取得当前阶段可用的具体工具schema。"
                    "当需要的能力未出现在available_tools时先调用；"
                    "这只扩展本条消息的受控能力，不修改战役。"
                ),
                handler=self.discover_capabilities,
                parameters=(
                    GMToolParameter(
                        "domains",
                        "array",
                        "从current_state_summary.supervisor.capability_catalog选择的一个或多个领域名。",
                        schema_details={
                            "items": {
                                "type": "string",
                                "enum": list(GMCapabilityBroker.domain_names()),
                            },
                            "minItems": 1,
                            "maxItems": 4,
                            "uniqueItems": True,
                        },
                    ),
                    GMToolParameter(
                        "domain",
                        "string",
                        "兼容只需要一个领域时的单数写法；不要与domains重复填写。",
                        enum=GMCapabilityBroker.domain_names(),
                    ),
                    GMToolParameter(
                        "reason",
                        "string",
                        "本轮为何需要这些能力；只供审计，不公开。",
                        required=True,
                    ),
                ),
            )
        )
        registry.register(
            GMToolDefinition(
                name=GMCapabilityBroker.SUPERVISOR_READ_TOOL,
                description=(
                    "读取总控告警、熔断和当前组件健康摘要。"
                    "只在需要诊断异常、决定是否干预，或玩家明确询问运行状态时使用。"
                ),
                handler=self.inspect_supervisor_state,
            )
        )
        registry.register(
            GMToolDefinition(
                name=GMCapabilityBroker.SUPERVISOR_ACK_TOOL,
                description=(
                    "在相关问题已通过权威工具处理后，确认一个总控告警。"
                    "确认不会自行修改游戏状态，也不能代替真正的修复工具。"
                ),
                handler=self.acknowledge_supervisor_alert,
                parameters=(
                    GMToolParameter(
                        "alert_id",
                        "string",
                        "inspect_supervisor_state返回的准确告警ID。",
                        required=True,
                    ),
                    GMToolParameter(
                        "resolution_note",
                        "string",
                        "已使用何种权威能力处理；只供审计。",
                        required=True,
                    ),
                ),
            )
        )
        registry.register(
            GMToolDefinition(
                name="reconcile_supervisor_state",
                description=(
                    "协调总控告警中可确定性修复的组件状态。"
                    "目前只会归档已兑现却残留的命刻，或让GM场景框架重新对齐权威镜头；"
                    "不会修补冲突行动顺序、代替玩家处理待决选择、开启新场景或改写剧情事实。"
                ),
                handler=self.reconcile_supervisor_state,
                parameters=(
                    GMToolParameter(
                        "alert_ids",
                        "array",
                        "inspect_supervisor_state返回的准确告警ID。",
                        required=True,
                        schema_details={
                            "items": {"type": "string", "minLength": 1},
                            "minItems": 1,
                            "maxItems": 4,
                            "uniqueItems": True,
                        },
                    ),
                    GMToolParameter(
                        "reason",
                        "string",
                        "本轮协调原因；只供审计，不公开。",
                        required=True,
                    ),
                ),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )

    def state_summary(self, context: GMToolExecutionContext) -> dict[str, object]:
        state_builder = getattr(
            getattr(self.host, "gm_agent_message_coordinator", None),
            "state_builder",
            None,
        )
        processes: dict[str, object] = {}
        if state_builder is not None:
            full_state = state_builder.build_full(context)
            self.host.gm_supervisor.scan(context, full_state)
            processes = dict(full_state.get("processes") or {})
        return {
            **self.host.gm_supervisor.audit_payload(
                context.campaign_id
            ),
            "processes": processes,
        }

    def discover_capabilities(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        domains = [
            str(item or "").strip()
            for item in list(arguments.get("domains") or [])
            if str(item or "").strip()
        ]
        single_domain = str(arguments.get("domain") or "").strip()
        if single_domain and single_domain not in domains:
            domains.append(single_domain)
        registry = self.host.gm_tool_registry
        phase_tools = set(
            GMToolAgentCapabilityPolicy.phase_tool_names(registry, context) or set()
        )
        selected = GMCapabilityBroker.tools_for_domains(
            domains,
            registry=registry,
            phase_tools=phase_tools,
        )
        if not selected:
            return GMToolReceipt.failure(
                GMCapabilityBroker.DISCOVERY_TOOL,
                "NO_CAPABILITY_IN_CURRENT_PHASE",
                "所选领域在当前阶段没有可调用能力。",
                "从supervisor.capability_catalog重新选择当前列出的domain。",
                result={"available_domains": GMCapabilityBroker.domain_names()},
            )
        granted = GMCapabilityBroker.grant(context, selected)
        return GMToolReceipt.success(
            GMCapabilityBroker.DISCOVERY_TOOL,
            result={
                "domains": domains,
                "granted_tool_names": sorted(selected),
                "all_granted_tool_names": sorted(granted),
                "reason": str(arguments.get("reason") or "")[:240],
            },
        )

    def inspect_supervisor_state(
        self,
        context: GMToolExecutionContext,
        _arguments: dict[str, object],
    ) -> GMToolReceipt:
        summary = self.state_summary(context)
        active_alerts = [
            item
            for item in list(summary.get("active_alerts") or [])
            if isinstance(item, dict)
        ]
        open_circuits = [
            item
            for item in list(summary.get("open_circuits") or [])
            if isinstance(item, dict)
        ]
        if not active_alerts and not open_circuits:
            public_reply = "监督检查完成：当前没有活动告警，熔断器均未开启。"
        else:
            public_reply = (
                "监督检查完成："
                f"当前有{len(active_alerts)}项活动告警，"
                f"{len(open_circuits)}个熔断器处于开启状态。"
            )
        terminal_public_result = not bool(
            context.metadata.get("system_gm_beat_request")
        )
        if terminal_public_result:
            summary["terminal_public_result"] = True
        return GMToolReceipt.success(
            GMCapabilityBroker.SUPERVISOR_READ_TOOL,
            result=summary,
            public_reply=public_reply if terminal_public_result else "",
            lock_public_reply=terminal_public_result,
        )

    def acknowledge_supervisor_alert(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        alert = self.host.gm_supervisor.acknowledge(
            context.campaign_id,
            str(arguments.get("alert_id") or ""),
            note=str(arguments.get("resolution_note") or ""),
        )
        if alert is None:
            return GMToolReceipt.failure(
                GMCapabilityBroker.SUPERVISOR_ACK_TOOL,
                "UNKNOWN_SUPERVISOR_ALERT",
                "没有找到该战役中的活动告警。",
                "先调用inspect_supervisor_state并使用准确alert_id。",
            )
        return GMToolReceipt.success(
            GMCapabilityBroker.SUPERVISOR_ACK_TOOL,
            result={
                "alert_id": alert.alert_id,
                "status": alert.status,
            },
        )

    def reconcile_supervisor_state(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        requested_ids = [
            str(item or "").strip()
            for item in list(arguments.get("alert_ids") or [])
            if str(item or "").strip()
        ]
        repairable = {
            str(item.get("alert_id") or ""): str(item.get("code") or "")
            for item in self.host.gm_supervisor.autonomous_repair_alerts(
                context.campaign_id
            )
        }
        selected = {
            alert_id: repairable[alert_id]
            for alert_id in requested_ids
            if alert_id in repairable
        }
        if not selected:
            return GMToolReceipt.failure(
                "reconcile_supervisor_state",
                "NO_SAFE_SUPERVISOR_REPAIR",
                "所选告警不属于可自动协调的状态问题。",
                (
                    "先调用inspect_supervisor_state。冲突行动者、待决窗口和缺少场景等"
                    "告警只能诊断或交由相应玩家与维护者处理，不能猜测修补。"
                ),
                retryable=False,
                result={
                    "repairable_alert_ids": sorted(repairable),
                },
            )

        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        repaired: list[dict[str, object]] = []
        unresolved: list[dict[str, str]] = []
        state_changed = False
        with runtime.transaction_lock:
            if "FULFILLED_CLOCK_STILL_ACTIVE" in selected.values():
                settled = app.clock_lifecycle.reconcile_fulfilled()
                if settled:
                    state_changed = True
                    repaired.append(
                        {
                            "code": "FULFILLED_CLOCK_STILL_ACTIVE",
                            "settled_clocks": settled,
                        }
                    )
                else:
                    unresolved.append(
                        {
                            "code": "FULFILLED_CLOCK_STILL_ACTIVE",
                            "reason": "重新读取后没有发现可归档的完整命刻。",
                        }
                    )

            if "SCENE_FRAME_FOCUS_MISMATCH" in selected.values():
                scene = app.scene_manager.current_scene
                frame = app.scene_frame_manager.current_frame
                if scene is None:
                    unresolved.append(
                        {
                            "code": "SCENE_FRAME_FOCUS_MISMATCH",
                            "reason": "当前已经没有活动场景。",
                        }
                    )
                elif (
                    frame is not None
                    and str(frame.source_scene_id or "").strip()
                    == str(scene.scene_id or "").strip()
                ):
                    unresolved.append(
                        {
                            "code": "SCENE_FRAME_FOCUS_MISMATCH",
                            "reason": "重新读取后镜头与场景已经一致。",
                        }
                    )
                else:
                    app.scene_frame_manager.suspend_current_frame()
                    restored = app.scene_frame_manager.restore_suspended_frame(
                        scene
                    )
                    if restored is None:
                        plan = getattr(
                            app.story_arc_manager.state,
                            "current_pacing_plan",
                            None,
                        )
                        restored = app.scene_frame_manager.ensure_frame(
                            scene=scene,
                            recent_chat=str(
                                context.metadata.get(
                                    "recent_public_context"
                                )
                                or ""
                            ),
                            world_state=app.world_state,
                            character_manager=app.character_manager,
                            contract=getattr(
                                plan,
                                "dramatic_contract",
                                None,
                            ),
                        )
                    state_changed = True
                    repaired.append(
                        {
                            "code": "SCENE_FRAME_FOCUS_MISMATCH",
                            "scene_id": scene.scene_id,
                            "frame_source_scene_id": restored.source_scene_id,
                        }
                    )

            saved_path = (
                self.host._autosave_campaign(
                    runtime,
                    context.campaign_id,
                )
                if state_changed
                else ""
            )

        # A fresh authoritative scan closes alerts only if their invariant is
        # actually restored. No repair is accepted merely because the model
        # requested it.
        state_builder = getattr(
            getattr(self.host, "gm_agent_message_coordinator", None),
            "state_builder",
            None,
        )
        if state_builder is not None:
            fresh = state_builder.build_full(context)
            self.host.gm_supervisor.scan(context, fresh)
        remaining = {
            str(item.get("alert_id") or "")
            for item in self.host.gm_supervisor.active_alerts(
                context.campaign_id
            )
        }
        repaired_ids = [
            alert_id
            for alert_id in selected
            if alert_id not in remaining
        ]
        if not repaired_ids:
            return GMToolReceipt.failure(
                "reconcile_supervisor_state",
                "SUPERVISOR_REPAIR_NO_CHANGE",
                "重新读取后没有需要安全协调的状态变化。",
                "不要重复调用；保留尚未解决的告警给维护者处理。",
                retryable=False,
                result={
                    "unresolved": unresolved,
                    "active_alert_ids": sorted(remaining),
                },
            )
        return GMToolReceipt.success(
            "reconcile_supervisor_state",
            result={
                "repaired_alert_ids": repaired_ids,
                "repairs": repaired,
                "unresolved": unresolved,
                "saved_path": saved_path,
            },
            state_changed=state_changed,
        )
