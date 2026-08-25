from __future__ import annotations

from typing import Any, Protocol

from fu_gm.components.gm_background_delegation import (
    GMBackgroundDelegationManager,
)
from fu_gm.components.gm_supervisor import GMCapabilityBroker
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolReceipt,
    GMToolRegistry,
)


class BackgroundDelegationToolHost(Protocol):
    background_delegation_manager: GMBackgroundDelegationManager


class GMBackgroundDelegationToolService:
    """Table-facing controls for long work owned by the background runner."""

    def __init__(self, host: BackgroundDelegationToolHost) -> None:
        self.host = host

    def register_tools(self, registry: GMToolRegistry) -> None:
        registry.register(
            GMToolDefinition(
                name="delegate_background_task",
                description=(
                    "把一项确实需要多轮读取、规划和工具调用的工作交给后台执行，"
                    "并立即释放当前聊天。适合补全多项世界设定、准备一组NPC/敌人、"
                    "整理多份规则资料等长任务。普通问答、当前场景行动、战斗回合、"
                    "命刻即时变化、玩家待决选择、一次即可完成的存档或修改不得委托。"
                    "同步地图渲染尚不属于安全后台工具，应继续走地图专用流程。"
                    "调用后只表示任务已受理，不表示目标已经完成。"
                ),
                handler=self.delegate_background_task,
                parameters=(
                    GMToolParameter(
                        "title",
                        "string",
                        "玩家能看懂的短标题，不含内部工具名。",
                        required=True,
                    ),
                    GMToolParameter(
                        "objective",
                        "string",
                        "完整目标、授权边界与必须保留的既有事实。",
                        required=True,
                    ),
                    GMToolParameter(
                        "completion_criteria",
                        "array",
                        "可验证的完成标准；每项描述一个必须完成的结果。",
                        required=True,
                        schema_details={
                            "items": {"type": "string", "minLength": 1},
                            "minItems": 1,
                            "maxItems": 12,
                            "uniqueItems": True,
                        },
                    ),
                    GMToolParameter(
                        "domains",
                        "array",
                        "完成任务所需的最小语义领域。",
                        required=True,
                        schema_details={
                            "items": {
                                "type": "string",
                                "enum": list(GMCapabilityBroker.domain_names()),
                            },
                            "minItems": 1,
                            "maxItems": 6,
                            "uniqueItems": True,
                        },
                    ),
                    GMToolParameter(
                        "requires_state_change",
                        "boolean",
                        (
                            "任务是否必须通过成功写工具改变权威状态才能算完成。"
                            "补全、修改、生成或准备应填true；纯研究或汇总填false。"
                        ),
                        required=True,
                    ),
                    GMToolParameter(
                        "max_steps",
                        "integer",
                        "最多后台规划/执行轮数，通常8到20；默认16。",
                        schema_details={"minimum": 2, "maximum": 40},
                    ),
                ),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )
        registry.register(
            GMToolDefinition(
                name="list_background_tasks",
                description=(
                    "列出当前团当前频道的后台委托及状态。玩家询问时使用；"
                    "不要把内部工具回执或模型轨迹公开。"
                ),
                handler=self.list_background_tasks,
                parameters=(
                    GMToolParameter(
                        "include_completed",
                        "boolean",
                        "是否包含最近已完成、失败或取消的委托；默认true。",
                    ),
                ),
                is_concurrency_safe=True,
                max_model_result_chars=5000,
            )
        )
        registry.register(
            GMToolDefinition(
                name="get_background_task",
                description="查看一项后台委托的进度、等待问题和最终状态。",
                handler=self.get_background_task,
                parameters=(
                    GMToolParameter(
                        "task_id",
                        "string",
                        "list_background_tasks返回的准确任务ID。",
                        required=True,
                    ),
                ),
                is_concurrency_safe=True,
                max_model_result_chars=6000,
            )
        )
        registry.register(
            GMToolDefinition(
                name="cancel_background_task",
                description=(
                    "取消当前玩家发起且尚未完成的后台委托。只停止后续步骤；"
                    "此前已由工具成功提交的事实不会假装回滚。"
                ),
                handler=self.cancel_background_task,
                parameters=(
                    GMToolParameter(
                        "task_id",
                        "string",
                        "要取消的准确任务ID；省略时仅在本人恰有一项活动委托时自动选择。",
                    ),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="resume_background_task",
                description=(
                    "回答后台委托提出的必要问题并恢复执行。只用于waiting_user状态；"
                    "省略task_id时仅在本人恰有一项等待回答的委托时自动选择。"
                ),
                handler=self.resume_background_task,
                parameters=(
                    GMToolParameter("task_id", "string", "等待回答的准确任务ID。"),
                    GMToolParameter(
                        "response",
                        "string",
                        "玩家对后台问题的完整回答。",
                        required=True,
                    ),
                ),
                side_effect="write",
            )
        )

    def state_summary(self, context: GMToolExecutionContext) -> dict[str, object]:
        return self.host.background_delegation_manager.state_summary(context)

    def delegate_background_task(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        if not self.host.background_delegation_manager.available:
            return GMToolReceipt.failure(
                "delegate_background_task",
                "BACKGROUND_RUNNER_UNAVAILABLE",
                "后台委托执行器当前不可用。",
                "不要声称已经受理；本轮改为前台完成可安全完成的部分，或如实说明稍后再试。",
                retryable=False,
            )
        objective = str(arguments.get("objective") or "").strip()
        criteria = [
            " ".join(str(item or "").split()).strip()
            for item in list(arguments.get("completion_criteria") or [])
            if " ".join(str(item or "").split()).strip()
        ]
        domains = [
            str(item or "").strip()
            for item in list(arguments.get("domains") or [])
            if str(item or "").strip()
        ]
        if not objective or not criteria or not domains:
            return GMToolReceipt.failure(
                "delegate_background_task",
                "BACKGROUND_TASK_INCOMPLETE",
                "后台委托缺少完整目标、完成标准或能力领域。",
                "补齐objective、completion_criteria和最小domains后重试。",
            )
        task = self.host.background_delegation_manager.enqueue(
            context=context,
            title=str(arguments.get("title") or "后台委托"),
            objective=objective,
            completion_criteria=criteria,
            domains=domains,
            requires_state_change=bool(arguments.get("requires_state_change")),
            max_steps=int(arguments.get("max_steps") or 16),
        )
        return GMToolReceipt.success(
            "delegate_background_task",
            result={
                "task": task.public_summary(include_progress=False),
                "operation": "background_task_accepted",
                "background_task_pending": True,
            },
            public_reply=(
                "这件事我先放到后台慢慢处理，你们可以继续聊；做好了我会直接告诉你们。"
            ),
            lock_public_reply=True,
        )

    def list_background_tasks(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        include_completed = arguments.get("include_completed") is not False
        tasks = self.host.background_delegation_manager.list_tasks(
            campaign_id=context.campaign_id,
            session_id=context.session_id,
            channel_id=context.channel_id,
            include_completed=include_completed,
            limit=20,
        )
        return GMToolReceipt.success(
            "list_background_tasks",
            result={
                "tasks": [task.public_summary(include_progress=False) for task in tasks],
                "count": len(tasks),
            },
        )

    def get_background_task(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        task = self.host.background_delegation_manager.get_task(
            str(arguments.get("task_id") or "")
        )
        if task is None or not self._same_scope(context, task):
            return GMToolReceipt.failure(
                "get_background_task",
                "BACKGROUND_TASK_NOT_FOUND",
                "当前团中没有这项后台委托。",
                "先调用list_background_tasks取得准确任务ID。",
                retryable=False,
            )
        return GMToolReceipt.success(
            "get_background_task",
            result={"task": task.public_summary(include_progress=True)},
        )

    def cancel_background_task(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        task, error = self._owned_task(
            context,
            str(arguments.get("task_id") or ""),
            statuses={"queued", "running", "waiting_user"},
        )
        if error is not None:
            return error
        assert task is not None
        cancelled = self.host.background_delegation_manager.cancel(
            task.task_id,
            notify=False,
        )
        return GMToolReceipt.success(
            "cancel_background_task",
            result={"task": cancelled.public_summary() if cancelled else {}},
            public_reply=f"后台委托【{task.title}】已经停下了。",
            lock_public_reply=True,
        )

    def resume_background_task(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        task, error = self._owned_task(
            context,
            str(arguments.get("task_id") or ""),
            statuses={"waiting_user"},
        )
        if error is not None:
            return error
        assert task is not None
        resumed = self.host.background_delegation_manager.resume(
            task.task_id,
            str(arguments.get("response") or ""),
        )
        return GMToolReceipt.success(
            "resume_background_task",
            result={"task": resumed.public_summary() if resumed else {}},
            public_reply="好，我接着处理；有结果后直接告诉你。",
            lock_public_reply=True,
        )

    def _owned_task(
        self,
        context: GMToolExecutionContext,
        task_id: str,
        *,
        statuses: set[str],
    ) -> tuple[Any | None, GMToolReceipt | None]:
        candidates = self.host.background_delegation_manager.list_tasks(
            campaign_id=context.campaign_id,
            session_id=context.session_id,
            channel_id=context.channel_id,
            include_completed=True,
            limit=40,
        )
        if task_id:
            candidates = [item for item in candidates if item.task_id == task_id]
        else:
            candidates = [item for item in candidates if item.status in statuses]
            candidates = [item for item in candidates if self._same_owner(context, item)]
            if len(candidates) != 1:
                return None, GMToolReceipt.failure(
                    "resume_background_task" if "waiting_user" in statuses else "cancel_background_task",
                    "BACKGROUND_TASK_ID_REQUIRED",
                    "无法唯一确定要处理的后台委托。",
                    "先调用list_background_tasks，再提交准确task_id。",
                )
        task = candidates[0] if len(candidates) == 1 else None
        if task is None or not self._same_scope(context, task):
            return None, GMToolReceipt.failure(
                "resume_background_task" if "waiting_user" in statuses else "cancel_background_task",
                "BACKGROUND_TASK_NOT_FOUND",
                "当前团中没有这项后台委托。",
                "先调用list_background_tasks取得准确任务ID。",
                retryable=False,
            )
        if not self._same_owner(context, task):
            return None, GMToolReceipt.failure(
                "resume_background_task" if "waiting_user" in statuses else "cancel_background_task",
                "BACKGROUND_TASK_OWNER_REQUIRED",
                "这项后台委托需要由发起者本人处理。",
                "请等待发起者本人回答或取消。",
                retryable=False,
            )
        if task.status not in statuses:
            return None, GMToolReceipt.failure(
                "resume_background_task" if "waiting_user" in statuses else "cancel_background_task",
                "BACKGROUND_TASK_STATUS_INVALID",
                f"这项委托当前是{task.status}状态，不能执行本操作。",
                "重新查看任务状态后选择适用操作。",
                retryable=False,
            )
        return task, None

    @staticmethod
    def _same_scope(context: GMToolExecutionContext, task: Any) -> bool:
        return bool(
            task.campaign_id == context.campaign_id
            and task.session_id == context.session_id
            and task.channel_id == context.channel_id
        )

    @staticmethod
    def _same_owner(context: GMToolExecutionContext, task: Any) -> bool:
        speaker_id = str(context.metadata.get("source_speaker_id") or "").strip()
        if speaker_id and task.owner_speaker_id:
            return speaker_id == task.owner_speaker_id
        return str(context.speaker or "").strip() == str(task.owner_speaker or "").strip()
