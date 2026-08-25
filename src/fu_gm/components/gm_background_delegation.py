from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fu_gm.components.gm_agent_capability_policy import (
    GMToolAgentCapabilityPolicy,
)
from fu_gm.components.gm_agent_decision_requester import (
    GMToolAgentDecisionRequester,
)
from fu_gm.components.gm_supervisor import GMCapabilityBroker
from fu_gm.gm_tool_contracts import (
    GMToolExecutionContext,
    GMToolReceipt,
    GMToolRegistry,
    json_safe_value,
)
from fu_gm.prompt_cache import build_cache_friendly_messages


BACKGROUND_DELEGATION_SYSTEM_PROMPT = """
你是时悠的后台委托执行器。桌面主对话仍由核心GM处理；你只完成一项已经被明确委托、需要多轮工具调用的后台工作。

## 工作方式

1. 每轮重新读取任务、权威状态、既有回执和available_tools，然后只决定下一小步。
2. 一轮最多调用一个工具。工具成功后会释放战役锁，下一轮再根据最新状态规划；不要输出call_tools。
3. 世界、人物、地图或规则状态只有成功工具回执才能改变。玩家原消息授权的范围不可扩大，既有公开事实不可暗改。
4. 不处理当前场景行动、战斗回合、命刻即时变化、玩家待决选择、安全边界或角色自主决定；这些必须留给桌面主对话。
5. 读取结果不足时继续调用合适的读取工具。参数缺失且无法从权威状态唯一确定时选择ask_user，只问一个真正必要的问题。
6. completion_criteria全部满足后选择final。reply是完成通知，只概括实际完成的结果，不暴露工具名、JSON、内部提示或推理过程；不得声称失败工具完成了工作。
7. 如果任务要求修改状态，至少应有一个与目标直接相关的成功写回执后才能final。若能力确实不支持，可final如实说明未完成及原因。

只输出一个JSON对象：
{"decision":"call_tool|ask_user|final",
 "tool_name":"仅call_tool填写",
 "arguments":{},
 "reply":"仅ask_user或final填写",
 "reason":"简短后台依据"}
""".strip()


@dataclass
class GMBackgroundDelegationTask:
    task_id: str
    campaign_id: str
    session_id: str
    channel_id: str
    owner_speaker: str
    owner_speaker_id: str
    is_private: bool
    title: str
    objective: str
    completion_criteria: list[str]
    domains: list[str]
    source_message: str
    source_event: dict[str, object]
    requires_state_change: bool = False
    status: str = "queued"
    created_at: str = ""
    updated_at: str = ""
    step_count: int = 0
    max_steps: int = 16
    failure_count: int = 0
    progress: list[dict[str, object]] = field(default_factory=list)
    receipt_history: list[dict[str, object]] = field(default_factory=list)
    final_reply: str = ""
    waiting_question: str = ""
    waiting_response: str = ""
    last_error: str = ""
    notification_id: str = ""
    notification_status: str = "none"
    notification_media: list[dict[str, object]] = field(default_factory=list)
    notification_author: str = ""
    notification_model: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "GMBackgroundDelegationTask":
        fields = cls.__dataclass_fields__
        values = {name: payload[name] for name in fields if name in payload}
        task = cls(**values)
        task.completion_criteria = [
            str(item or "").strip()
            for item in list(task.completion_criteria or [])
            if str(item or "").strip()
        ]
        task.domains = [
            str(item or "").strip()
            for item in list(task.domains or [])
            if str(item or "").strip()
        ]
        task.progress = [
            dict(item) for item in list(task.progress or []) if isinstance(item, dict)
        ]
        task.receipt_history = [
            dict(item)
            for item in list(task.receipt_history or [])
            if isinstance(item, dict)
        ]
        task.source_event = (
            dict(task.source_event) if isinstance(task.source_event, dict) else {}
        )
        task.notification_media = [
            dict(item)
            for item in list(task.notification_media or [])
            if isinstance(item, dict)
        ]
        return task

    def public_summary(self, *, include_progress: bool = False) -> dict[str, object]:
        result: dict[str, object] = {
            "task_id": self.task_id,
            "title": self.title,
            "status": self.status,
            "step_count": self.step_count,
            "max_steps": self.max_steps,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "waiting_question": self.waiting_question,
            "last_error": self.last_error if self.status == "failed" else "",
            "notification_status": self.notification_status,
            "notification_author": self.notification_author,
            "notification_model": self.notification_model,
        }
        if include_progress:
            result["objective"] = self.objective
            result["completion_criteria"] = list(self.completion_criteria)
            result["domains"] = list(self.domains)
            result["progress"] = list(self.progress[-12:])
            result["final_reply"] = self.final_reply
        return result


class GMBackgroundDelegationManager:
    """Persist and execute long GM work without owning the foreground turn.

    Model planning happens without a campaign lock. Every selected tool is then
    executed as its own ordinary registry transaction, so foreground chat can
    commit between background steps and stale plans are rejected by the existing
    campaign version guard.
    """

    TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
    ACTIVE_STATUSES = frozenset({"queued", "running", "waiting_user"})
    _BACKGROUND_WRITE_ALLOWLIST = frozenset(
        {
            "propose_session_zero_update",
            "create_world_setting",
            "update_world_setting",
            "delete_world_setting",
            "rename_world_setting",
            "select_first_act",
            "create_npc_profile",
            "prepare_npc_combatant",
            "finalize_npc_combatant_preparation",
        }
    )
    # A defer marker is only an orchestration hint, not proof that the handler
    # is safe outside the foreground turn. NPC preparation already snapshots
    # its request and publishes under the campaign lock. The current map render
    # still mutates live WorldState while a renderer process is running, so it
    # must remain outside this generic runner until it has a detached commit.
    _BACKGROUND_SAFE_DEFERRED_TOOLS = frozenset({"prepare_npc_combatant"})
    _BACKGROUND_ALWAYS_DENY = frozenset(
        {
            "delegate_background_task",
            "list_background_tasks",
            "get_background_task",
            "cancel_background_task",
            "resume_background_task",
            "discover_capabilities",
            "acknowledge_supervisor_alert",
            "reconcile_supervisor_state",
        }
    )

    def __init__(
        self,
        data_root: str | Path,
        *,
        max_workers: int = 2,
        max_tasks: int = 200,
    ) -> None:
        self.data_root = Path(data_root)
        self.path = self.data_root / "_service" / "background_delegations.json"
        self.max_tasks = max(20, int(max_tasks))
        self._lock = threading.RLock()
        self._tasks: dict[str, GMBackgroundDelegationTask] = {}
        self._futures: dict[str, Future[None]] = {}
        self._active_campaigns: set[str] = set()
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="fu-gm-background",
        )
        self._host: Any | None = None
        self._client: Any | None = None
        self._model = ""
        self._registry: GMToolRegistry | None = None
        self._state_builder: Any | None = None
        self._grounding_verifier: Any | None = None
        self._requester: GMToolAgentDecisionRequester | None = None
        self._closed = False
        self._load()

    def bind(
        self,
        *,
        host: Any,
        client: Any | None,
        model: str,
        registry: GMToolRegistry,
        state_builder: Any,
        grounding_verifier: Any | None = None,
    ) -> None:
        self._host = host
        self._client = client
        self._model = str(model or "").strip()
        self._registry = registry
        self._state_builder = state_builder
        self._grounding_verifier = grounding_verifier
        self._requester = (
            GMToolAgentDecisionRequester(
                client,
                model=self._model,
                repair_model=self._model,
                parse_retries=2,
                empty_response_retries=1,
                max_output_tokens=max(
                    1024,
                    int(os.environ.get("FU_GM_BACKGROUND_MAX_TOKENS", "3072")),
                ),
            )
            if client is not None and self._model
            else None
        )
        with self._lock:
            for task in self._tasks.values():
                if task.status == "running":
                    task.status = "queued"
                    task.last_error = "服务重启后已恢复等待执行。"
                    task.updated_at = self._now()
            self._persist_locked()
            queued = [task.task_id for task in self._tasks.values() if task.status == "queued"]
        for task_id in queued:
            self._submit(task_id)

    @property
    def available(self) -> bool:
        with self._lock:
            return bool(
                not self._closed
                and self._requester is not None
                and self._registry is not None
                and self._state_builder is not None
            )

    def enqueue(
        self,
        *,
        context: GMToolExecutionContext,
        title: str,
        objective: str,
        completion_criteria: list[str],
        domains: list[str],
        requires_state_change: bool,
        max_steps: int = 16,
    ) -> GMBackgroundDelegationTask:
        now = self._now()
        source_events = [
            dict(item)
            for item in list(context.metadata.get("current_turn_events") or [])
            if isinstance(item, dict)
        ]
        source_event = source_events[0] if len(source_events) == 1 else {
            "event_id": str(context.metadata.get("source_event_id") or ""),
            "message_id": str(context.metadata.get("source_message_id") or ""),
            "speaker": str(context.metadata.get("source_speaker") or context.speaker),
            "speaker_id": str(context.metadata.get("source_speaker_id") or ""),
            "text": str(context.metadata.get("current_message") or ""),
        }
        task = GMBackgroundDelegationTask(
            task_id=f"bg-{uuid.uuid4().hex[:12]}",
            campaign_id=context.campaign_id,
            session_id=context.session_id,
            channel_id=context.channel_id,
            owner_speaker=context.speaker,
            owner_speaker_id=str(source_event.get("speaker_id") or ""),
            is_private=bool(context.is_private),
            title=" ".join(str(title or "后台委托").split()).strip()[:120],
            objective=str(objective or "").strip()[:3000],
            completion_criteria=list(dict.fromkeys(completion_criteria))[:12],
            domains=list(dict.fromkeys(domains))[:6],
            source_message=str(context.metadata.get("current_message") or "").strip()[:4000],
            source_event=json_safe_value(source_event),
            requires_state_change=bool(requires_state_change),
            created_at=now,
            updated_at=now,
            max_steps=min(40, max(2, int(max_steps))),
        )
        with self._lock:
            self._tasks[task.task_id] = task
            self._prune_locked()
            self._persist_locked()
        self._submit(task.task_id)
        return task

    def list_tasks(
        self,
        *,
        campaign_id: str,
        session_id: str = "",
        channel_id: str = "",
        include_completed: bool = True,
        limit: int = 20,
    ) -> list[GMBackgroundDelegationTask]:
        with self._lock:
            items = [
                task
                for task in self._tasks.values()
                if task.campaign_id == campaign_id
                and (not session_id or task.session_id == session_id)
                and (not channel_id or task.channel_id == channel_id)
                and (include_completed or task.status not in self.TERMINAL_STATUSES)
            ]
            items.sort(key=lambda item: item.updated_at, reverse=True)
            return [GMBackgroundDelegationTask.from_dict(asdict(item)) for item in items[: max(1, limit)]]

    def get_task(self, task_id: str) -> GMBackgroundDelegationTask | None:
        with self._lock:
            task = self._tasks.get(str(task_id or "").strip())
            return GMBackgroundDelegationTask.from_dict(asdict(task)) if task else None

    def cancel(
        self,
        task_id: str,
        *,
        notify: bool = True,
    ) -> GMBackgroundDelegationTask | None:
        with self._lock:
            task = self._tasks.get(str(task_id or "").strip())
            if task is None:
                return None
            if task.status in self.TERMINAL_STATUSES:
                return GMBackgroundDelegationTask.from_dict(asdict(task))
            task.status = "cancelled"
            task.updated_at = self._now()
            task.final_reply = f"后台委托【{task.title}】已取消。"
            if notify:
                self._queue_notification_locked(task)
            else:
                task.notification_id = ""
                task.notification_status = "none"
            self._persist_locked()
            return GMBackgroundDelegationTask.from_dict(asdict(task))

    def resume(self, task_id: str, response: str) -> GMBackgroundDelegationTask | None:
        with self._lock:
            task = self._tasks.get(str(task_id or "").strip())
            if task is None or task.status != "waiting_user":
                return None
            task.waiting_response = str(response or "").strip()[:3000]
            task.waiting_question = ""
            task.status = "queued"
            task.updated_at = self._now()
            task.notification_status = "none"
            task.notification_id = ""
            self._persist_locked()
        self._submit(task.task_id)
        return self.get_task(task.task_id)

    def poll_notification(
        self,
        *,
        campaign_id: str,
        session_id: str,
        channel_id: str,
    ) -> dict[str, object] | None:
        with self._lock:
            candidates = [
                task
                for task in self._tasks.values()
                if task.campaign_id == campaign_id
                and task.session_id == session_id
                and task.channel_id == channel_id
                and task.notification_status == "pending"
                and task.notification_id
            ]
            if not candidates:
                return None
            task = sorted(candidates, key=lambda item: item.updated_at)[0]
            return {
                "notification_id": task.notification_id,
                "task_id": task.task_id,
                "status": task.status,
                "reply": task.final_reply or task.waiting_question,
                "reply_media": list(task.notification_media),
                "campaign_id": task.campaign_id,
                "session_id": task.session_id,
                "channel_id": task.channel_id,
                "is_private": task.is_private,
            }

    def confirm_delivery(self, notification_id: str) -> GMBackgroundDelegationTask | None:
        clean_id = str(notification_id or "").strip()
        with self._lock:
            task = next(
                (item for item in self._tasks.values() if item.notification_id == clean_id),
                None,
            )
            if task is None:
                return None
            task.notification_status = "delivered"
            task.updated_at = self._now()
            self._persist_locked()
            return GMBackgroundDelegationTask.from_dict(asdict(task))

    def task_for_notification(
        self,
        notification_id: str,
    ) -> GMBackgroundDelegationTask | None:
        clean_id = str(notification_id or "").strip()
        with self._lock:
            task = next(
                (item for item in self._tasks.values() if item.notification_id == clean_id),
                None,
            )
            return GMBackgroundDelegationTask.from_dict(asdict(task)) if task else None

    def state_summary(self, context: GMToolExecutionContext) -> dict[str, object]:
        tasks = self.list_tasks(
            campaign_id=context.campaign_id,
            session_id=context.session_id,
            channel_id=context.channel_id,
            include_completed=True,
            limit=8,
        )
        return {
            "active": [
                item.public_summary(include_progress=False)
                for item in tasks
                if item.status in self.ACTIVE_STATUSES
            ],
            "recent": [
                item.public_summary(include_progress=False)
                for item in tasks
                if item.status in self.TERMINAL_STATUSES
            ][:4],
        }

    def purge_campaign(self, campaign_id: str) -> int:
        clean_id = str(campaign_id or "").strip()
        if not clean_id:
            return 0
        with self._lock:
            task_ids = [
                task_id
                for task_id, task in self._tasks.items()
                if task.campaign_id == clean_id
            ]
            for task_id in task_ids:
                self._tasks.pop(task_id, None)
            if task_ids:
                self._persist_locked()
            return len(task_ids)

    def audit_payload(
        self,
        *,
        campaign_id: str,
        session_id: str = "",
        channel_id: str = "",
    ) -> dict[str, object]:
        tasks = self.list_tasks(
            campaign_id=campaign_id,
            session_id=session_id,
            channel_id=channel_id,
            include_completed=True,
            limit=40,
        )
        return {
            "counts": {
                status: sum(1 for item in tasks if item.status == status)
                for status in (
                    "queued",
                    "running",
                    "waiting_user",
                    "completed",
                    "failed",
                    "cancelled",
                )
            },
            "tasks": [item.public_summary(include_progress=True) for item in tasks],
        }

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    def run_task_once(self, task_id: str) -> str:
        """Execute one planning/tool step; exposed for deterministic tests."""

        task = self.get_task(task_id)
        if task is None:
            return "missing"
        if task.status in self.TERMINAL_STATUSES or task.status == "waiting_user":
            return task.status
        if self._requester is None or self._registry is None or self._state_builder is None:
            self._fail(task_id, "后台模型执行器尚未启用。")
            return "failed"
        if task.step_count >= task.max_steps:
            self._fail(task_id, "后台委托超过最大步骤数，已停止以防止无限循环。")
            return "failed"

        context = self._execution_context(task)
        runtime = self._host._runtime(task.campaign_id)
        with runtime.transaction_lock:
            context.metadata["_gm_campaign_observed_version"] = int(
                getattr(runtime, "state_version", 0) or 0
            )
            state = self._state_builder.build(context)
        allowed_names = self._allowed_tool_names(context, task)
        schemas = self._registry.schemas(allowed_names)
        if not schemas:
            self._fail(task_id, "当前阶段没有可用于这项后台委托的安全工具。")
            return "failed"

        with self._lock:
            live = self._tasks.get(task_id)
            if live is None or live.status in self.TERMINAL_STATUSES:
                return "cancelled"
            live.status = "running"
            live.step_count += 1
            live.updated_at = self._now()
            self._persist_locked()
            step_number = live.step_count
            task = GMBackgroundDelegationTask.from_dict(asdict(live))

        request_payload = {
            "task": {
                "task_id": task.task_id,
                "title": task.title,
                "objective": task.objective,
                "completion_criteria": task.completion_criteria,
                "authorized_domains": task.domains,
                "requires_state_change": task.requires_state_change,
                "source_message": task.source_message,
                "waiting_response": task.waiting_response,
            },
            "current_state_summary": state,
            "progress": task.progress[-12:],
            "tool_receipts": task.receipt_history[-12:],
            "available_tools": schemas,
        }
        messages = build_cache_friendly_messages(
            static_system_prompt=BACKGROUND_DELEGATION_SYSTEM_PROMPT,
            user_content=json.dumps(json_safe_value(request_payload), ensure_ascii=False),
            cache_family="gm-background-delegation-v1",
        )
        trace: list[dict[str, object]] = []
        try:
            decision = self._requester.request(
                messages,
                iteration=step_number,
                deadline=time.monotonic()
                + max(
                    15.0,
                    float(os.environ.get("FU_GM_BACKGROUND_STEP_TIMEOUT_SECONDS", "90")),
                ),
                trace=trace,
            )
        except Exception as exc:
            self._retry_or_fail(task_id, f"模型规划失败：{type(exc).__name__}: {exc}")
            return "retry"

        action = str(decision.get("decision") or "").strip().lower()
        if action == "call_tools":
            self._record_protocol_issue(
                task_id,
                "后台委托每轮只能调用一个工具；请从批次中选择当前第一步。",
            )
            return "retry"
        if action == "call_tool":
            tool_name = str(decision.get("tool_name") or "").strip()
            arguments = decision.get("arguments")
            if tool_name not in allowed_names or not isinstance(arguments, dict):
                self._record_protocol_issue(
                    task_id,
                    "模型选择了未授权工具或无效参数，请按available_tools重选。",
                )
                return "retry"
            arguments = self._background_safe_arguments(tool_name, arguments)
            live_task = self.get_task(task_id)
            if live_task is None or live_task.status in self.TERMINAL_STATUSES:
                return "cancelled"
            receipt = self._registry.execute(
                tool_name,
                arguments,
                context,
                side_effect_lock=runtime.transaction_lock,
            )
            self._record_receipt(task_id, receipt)
            if receipt.ok:
                if str(receipt.result.get("status") or "").strip().lower() in {
                    "generating",
                    "pending",
                    "queued",
                    "running",
                }:
                    return "deferred"
                return "continue"
            if receipt.error_code in {
                "TOOL_TRANSACTION_START_FAILED",
                "STALE_AGENT_REQUEST",
                "BLOCKING_DECISION_PENDING",
            }:
                self._record_protocol_issue(
                    task_id,
                    "桌面状态在后台规划期间变化；下一步将重新读取后继续。",
                    count_failure=False,
                )
                return "retry"
            if receipt.retryable:
                return "retry"
            self._retry_or_fail(
                task_id,
                f"工具 {tool_name} 未完成：{receipt.message or receipt.error_code}",
            )
            return "retry"
        if action == "ask_user":
            question = str(decision.get("reply") or "").strip()
            if not question:
                self._record_protocol_issue(task_id, "ask_user缺少要询问的具体问题。")
                return "retry"
            self._wait_for_user(task_id, question)
            return "waiting_user"
        if action == "final":
            reply = str(decision.get("reply") or "").strip()
            if not reply:
                self._record_protocol_issue(task_id, "final缺少完成通知。")
                return "retry"
            if self._task_requires_write(task) and not self._has_successful_write(task_id):
                self._record_protocol_issue(
                    task_id,
                    "这项委托要求改变状态，但还没有直接相关的成功写回执。",
                )
                return "retry"
            reply, expression = self._render_completion_notification(task, reply)
            if not self._grounded_final(task, state, reply):
                self._record_protocol_issue(
                    task_id,
                    "完成通知包含权威状态或工具回执无法支持的说法，请改写。",
                )
                return "retry"
            self._complete(
                task_id,
                reply,
                notification_author=str(expression.get("author") or ""),
                notification_model=str(expression.get("model") or ""),
            )
            return "completed"
        self._record_protocol_issue(task_id, "后台模型没有选择合法决策。")
        return "retry"

    def _submit(self, task_id: str) -> None:
        with self._lock:
            if self._closed or self._requester is None:
                return
            existing = self._futures.get(task_id)
            if existing is not None and not existing.done():
                return
            self._futures[task_id] = self._executor.submit(self._run_task_loop, task_id)

    def _run_task_loop(self, task_id: str) -> None:
        task = self.get_task(task_id)
        if task is None:
            return
        campaign_id = task.campaign_id
        with self._lock:
            if campaign_id in self._active_campaigns:
                # Another task in this campaign will release shortly. Requeue
                # without occupying a worker with a long-lived condition wait.
                timer = threading.Timer(0.5, lambda: self._submit(task_id))
                timer.daemon = True
                timer.start()
                return
            self._active_campaigns.add(campaign_id)
        try:
            while True:
                status = self.run_task_once(task_id)
                if status in self.TERMINAL_STATUSES or status in {"missing", "waiting_user"}:
                    return
                current = self.get_task(task_id)
                if current is None or current.status in self.TERMINAL_STATUSES:
                    return
                if status == "retry":
                    time.sleep(min(3.0, 0.25 * (2 ** min(4, current.failure_count))))
                elif status == "deferred":
                    # Side-chain jobs such as NPC blueprint selection need wall
                    # time, but must not occupy the campaign transaction lock.
                    time.sleep(1.0)
        finally:
            with self._lock:
                self._active_campaigns.discard(campaign_id)

    def _execution_context(
        self,
        task: GMBackgroundDelegationTask,
    ) -> GMToolExecutionContext:
        gate = self._host.session_gates.get(
            task.campaign_id,
            task.channel_id,
            task.session_id,
        )
        event = dict(task.source_event)
        if not event.get("event_id"):
            event["event_id"] = f"background-{task.task_id}"
        event.setdefault("message_id", str(event.get("event_id") or ""))
        event.setdefault("speaker", task.owner_speaker)
        event.setdefault("speaker_id", task.owner_speaker_id)
        event.setdefault("text", task.source_message)
        return GMToolExecutionContext(
            campaign_id=task.campaign_id,
            session_id=task.session_id,
            channel_id=task.channel_id,
            speaker=task.owner_speaker,
            gate_status=gate.status,
            is_private=task.is_private,
            directly_addressed=True,
            metadata={
                "current_message": task.source_message,
                "current_turn_events": [event],
                "system_background_delegation": True,
                "background_task_id": task.task_id,
                "gm_dynamic_capabilities_enabled": True,
                "gm_capability_routing_mode": "baseline",
                "gm_explicitly_discovered_domains": list(task.domains),
            },
        )

    def _allowed_tool_names(
        self,
        context: GMToolExecutionContext,
        task: GMBackgroundDelegationTask,
    ) -> set[str]:
        registry = self._registry
        if registry is None:
            return set()
        phase_tools = set(
            GMToolAgentCapabilityPolicy.phase_tool_names(registry, context) or set()
        )
        selected = GMCapabilityBroker.tools_for_domains(
            task.domains,
            registry=registry,
            phase_tools=set(registry._tools),
        )
        # Every read in an authorized domain is safe. Writes are deliberately
        # limited to preparation/catalog operations; live tabletop mutations
        # remain in the foreground transaction.
        result = {
            name
            for name in selected
            if name not in self._BACKGROUND_ALWAYS_DENY
            and (
                registry.is_read_only(name)
                or (
                    name in phase_tools
                    and
                    name in self._BACKGROUND_WRITE_ALLOWLIST
                    and not bool(registry.execution_metadata(name).get("is_destructive"))
                    and (
                        not str(
                            registry.execution_metadata(name).get("defer_group")
                            or ""
                        ).strip()
                        or name in self._BACKGROUND_SAFE_DEFERRED_TOOLS
                    )
                )
            )
        }
        GMCapabilityBroker.grant(context, result)
        return result

    @staticmethod
    def _background_safe_arguments(
        tool_name: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        """Remove waits that would hold the campaign lock in a worker step."""

        safe = dict(arguments)
        if tool_name == "get_npc_combatant_design":
            safe["wait_seconds"] = 0
        return safe

    def _record_receipt(self, task_id: str, receipt: GMToolReceipt) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            compact_result = {
                key: json_safe_value(value)
                for key, value in receipt.result.items()
                if key not in {"private_prompt", "raw_model_output", "private_details"}
            }
            task.receipt_history.append(
                {
                    "tool_name": receipt.tool_name,
                    "ok": receipt.ok,
                    "state_changed": receipt.state_changed,
                    "error_code": receipt.error_code,
                    "message": str(receipt.message or "")[:500],
                    "correction_hint": str(receipt.correction_hint or "")[:500],
                    "result": compact_result,
                }
            )
            task.receipt_history = task.receipt_history[-24:]
            task.progress.append(
                {
                    "at": self._now(),
                    "step": task.step_count,
                    "tool": receipt.tool_name,
                    "outcome": "completed" if receipt.ok else "rejected",
                    "summary": (
                        str(receipt.message or "完成一步。")
                        if receipt.ok
                        else str(receipt.message or receipt.error_code)
                    )[:300],
                }
            )
            task.progress = task.progress[-40:]
            task.failure_count = 0 if receipt.ok else task.failure_count + 1
            task.status = "queued"
            task.updated_at = self._now()
            for item in list(receipt.result.get("reply_media") or []):
                if isinstance(item, dict) and item not in task.notification_media:
                    task.notification_media.append(dict(item))
            self._persist_locked()

    def _record_protocol_issue(
        self,
        task_id: str,
        message: str,
        *,
        count_failure: bool = True,
    ) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.progress.append(
                {
                    "at": self._now(),
                    "step": task.step_count,
                    "outcome": "replan",
                    "summary": str(message or "")[:300],
                }
            )
            if count_failure:
                task.failure_count += 1
            task.status = "queued"
            task.updated_at = self._now()
            self._persist_locked()
        if count_failure and self.get_task(task_id) and self.get_task(task_id).failure_count >= 3:
            self._fail(task_id, message)

    def _retry_or_fail(self, task_id: str, message: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.failure_count += 1
            task.last_error = str(message or "")[:500]
            task.status = "queued"
            task.updated_at = self._now()
            should_fail = task.failure_count >= 3
            self._persist_locked()
        if should_fail:
            self._fail(task_id, message)

    def _wait_for_user(self, task_id: str, question: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.status = "waiting_user"
            task.waiting_question = question
            task.final_reply = question
            task.updated_at = self._now()
            self._queue_notification_locked(task)
            self._persist_locked()

    def _complete(
        self,
        task_id: str,
        reply: str,
        *,
        notification_author: str = "",
        notification_model: str = "",
    ) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.status = "completed"
            task.final_reply = reply
            task.notification_author = str(notification_author or "")
            task.notification_model = str(notification_model or "")
            task.last_error = ""
            task.updated_at = self._now()
            self._queue_notification_locked(task)
            self._persist_locked()

    def _render_completion_notification(
        self,
        task: GMBackgroundDelegationTask,
        draft: str,
    ) -> tuple[str, dict[str, object]]:
        """Let the configured public author phrase a grounded completion.

        The core worker owns semantics and tool use. When the deployment has an
        explicit Expressor public-author mode, it receives only the short
        semantic draft and recent public chat, never the private task trace.
        """

        host = self._host
        if host is None or str(
            getattr(host, "public_expression_mode", "core") or "core"
        ).strip().lower() != "expressor":
            return draft, {"author": "core_background", "model": self._model}
        try:
            runtime = host._runtime(task.campaign_id)
            renderer = getattr(runtime.app, "expressor", None)
            render = getattr(renderer, "render_agent_message", None)
            if not callable(render):
                return draft, {"author": "core_background", "model": self._model}
            recent_context = ""
            log_manager = getattr(runtime, "log_manager", None)
            load_transcript = getattr(log_manager, "load_transcript", None)
            if callable(load_transcript):
                entries = list(
                    load_transcript(task.campaign_id, task.session_id) or []
                )[-8:]
                recent_context = "\n".join(
                    f"{str(getattr(item, 'speaker', '') or getattr(item, 'role', '') or '').strip()}: "
                    f"{str(getattr(item, 'content', '') or '').strip()}"
                    for item in entries
                    if str(getattr(item, "content", "") or "").strip()
                )
            rendered = list(
                render(
                    [draft],
                    current_message=task.source_message,
                    recent_context=recent_context[-6000:],
                    gate_status=self._execution_context(task).gate_status,
                    route_mode="background_delegation_completion",
                    expression_style="plain",
                )
                or []
            )
            clean = [str(item or "").strip() for item in rendered if str(item or "").strip()]
            if len(clean) != 1:
                return draft, {"author": "core_background", "model": self._model}
            metadata = dict(
                getattr(renderer, "last_agent_message_metadata", {}) or {}
            )
            return clean[0], {
                "author": str(metadata.get("author") or "expressor"),
                "model": str(metadata.get("model") or getattr(renderer, "model", "")),
            }
        except Exception:
            return draft, {"author": "core_background", "model": self._model}

    def _fail(self, task_id: str, message: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status in {"completed", "cancelled"}:
                return
            task.status = "failed"
            task.last_error = str(message or "")[:500]
            task.final_reply = (
                f"后台委托【{task.title}】没有完成：{task.last_error}"
            )
            task.updated_at = self._now()
            self._queue_notification_locked(task)
            self._persist_locked()

    def _queue_notification_locked(self, task: GMBackgroundDelegationTask) -> None:
        if task.notification_status == "delivered":
            return
        task.notification_id = task.notification_id or f"bgn-{uuid.uuid4().hex}"
        task.notification_status = "pending"

    def _task_requires_write(self, task: GMBackgroundDelegationTask) -> bool:
        return bool(task.requires_state_change)

    def _has_successful_write(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or self._registry is None:
                return False
            return any(
                bool(item.get("ok"))
                and self._registry.side_effect(str(item.get("tool_name") or "")) != "read"
                for item in task.receipt_history
            )

    def _grounded_final(
        self,
        task: GMBackgroundDelegationTask,
        state: dict[str, object],
        reply: str,
    ) -> bool:
        if self._looks_like_deferred_completion(reply):
            return False
        verifier = self._grounding_verifier
        if verifier is None:
            return True
        receipts = [
            GMToolReceipt(
                tool_name=str(item.get("tool_name") or ""),
                ok=bool(item.get("ok")),
                state_changed=bool(item.get("state_changed")),
                error_code=str(item.get("error_code") or ""),
                message=str(item.get("message") or ""),
                correction_hint=str(item.get("correction_hint") or ""),
                result=dict(item.get("result") or {}),
            )
            for item in task.receipt_history[-12:]
        ]
        deadline = time.monotonic() + 30.0
        try:
            completion_review = getattr(
                verifier,
                "verify_silence_responsibility",
                None,
            )
            if callable(completion_review):
                responsibility = completion_review(
                    current_message=(
                        f"{task.source_message}\n\n"
                        f"后台委托目标：{task.objective}\n"
                        "完成标准："
                        + "；".join(task.completion_criteria)
                    ),
                    recent_context="",
                    gate_status=self._execution_context(task).gate_status,
                    proposed_message_kind="background_delegation_completion",
                    proposed_audience="gm",
                    decision_reason="后台委托准备交付最终成果",
                    deadline=deadline,
                    proposed_delivery={"mode": "normal"},
                    has_independent_followup=False,
                    completed_receipts=receipts,
                    proposed_public_reply=reply,
                )
                if bool(responsibility.requires_gm_reply):
                    return False
            review = verifier.verify(
                current_message=task.source_message,
                recent_context="",
                observed_state=state,
                receipts=receipts,
                proposed_reply=reply,
                message_kind="background_delegation_completion",
                decision_reason="后台委托完成通知",
                deadline=deadline,
            )
            return bool(review.valid)
        except Exception:
            # The verifier is a secondary audit. Provider failure must not
            # strand an otherwise receipt-grounded task forever.
            return True

    @staticmethod
    def _looks_like_deferred_completion(reply: str) -> bool:
        text = "".join(str(reply or "").split())
        if not text:
            return True
        return any(
            marker in text
            for marker in (
                "已受理",
                "整理好后再告诉",
                "整理好后告诉",
                "完成后再告诉",
                "稍后随整理结果",
                "稍后把结果",
                "稍后将结果",
                "之后再把结果",
                "后续再把结果",
                "稍后另行通知",
                "有结果后再告诉",
            )
        )

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        rows = payload.get("tasks") if isinstance(payload, dict) else []
        for row in list(rows or []):
            if not isinstance(row, dict):
                continue
            try:
                task = GMBackgroundDelegationTask.from_dict(row)
            except (TypeError, ValueError):
                continue
            self._tasks[task.task_id] = task

    def _persist_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": self._now(),
            "tasks": [asdict(item) for item in self._tasks.values()],
        }
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink(missing_ok=True)

    def _prune_locked(self) -> None:
        if len(self._tasks) <= self.max_tasks:
            return
        terminal = sorted(
            (
                task
                for task in self._tasks.values()
                if task.status in self.TERMINAL_STATUSES
                and task.notification_status in {"none", "delivered"}
            ),
            key=lambda item: item.updated_at,
        )
        for task in terminal:
            if len(self._tasks) <= self.max_tasks:
                break
            self._tasks.pop(task.task_id, None)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
