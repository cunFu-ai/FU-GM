from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

from fu_gm.components.gm_background_delegation import (
    GMBackgroundDelegationManager,
)
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolReceipt,
    GMToolRegistry,
)
from fu_gm.http_server import FUGMHttpService


class _ScriptedClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def create_chat_completion(self, **kwargs) -> str:
        self.calls.append(dict(kwargs))
        if not self.responses:
            raise AssertionError("缺少后台委托脚本响应。")
        return self.responses.pop(0)


class _BlockingClient:
    def __init__(self, response: str) -> None:
        self.response = response
        self.started = threading.Event()
        self.release = threading.Event()

    def create_chat_completion(self, **kwargs) -> str:
        self.started.set()
        assert self.release.wait(timeout=3)
        return self.response


class _StateBuilder:
    def build(self, context: GMToolExecutionContext) -> dict[str, object]:
        return {
            "gate_status": context.gate_status,
            "world_settings": {"revision": 0},
        }


class _GateManager:
    @staticmethod
    def get(campaign_id: str, channel_id: str, session_id: str) -> object:
        return SimpleNamespace(status="session_zero")


class _Host:
    def __init__(self) -> None:
        self.session_gates = _GateManager()
        self.runtime = SimpleNamespace(
            transaction_lock=threading.RLock(),
            state_version=0,
        )

    def _runtime(self, campaign_id: str) -> object:
        return self.runtime


def _context() -> GMToolExecutionContext:
    event = {
        "event_id": "event-1",
        "message_id": "message-1",
        "speaker": "村夫",
        "speaker_id": "8711",
        "text": "帮我们在后台补全这几项设定。",
        "is_at_gm": True,
    }
    return GMToolExecutionContext(
        campaign_id="campaign",
        session_id="group-1",
        channel_id="group-1",
        speaker="村夫",
        gate_status="session_zero",
        directly_addressed=True,
        metadata={
            "current_message": event["text"],
            "current_turn_events": [event],
            "source_speaker_id": "8711",
        },
    )


def _private_context() -> GMToolExecutionContext:
    context = _context()
    context.is_private = True
    context.session_id = "8711"
    context.channel_id = "8711"
    return context


def _manager(tmp_path, client, registry: GMToolRegistry):
    manager = GMBackgroundDelegationManager(tmp_path)
    manager._submit = lambda _task_id: None
    manager.bind(
        host=_Host(),
        client=client,
        model="test-model",
        registry=registry,
        state_builder=_StateBuilder(),
    )
    return manager


def test_long_task_executes_one_tool_per_step_then_queues_notification(tmp_path) -> None:
    registry = GMToolRegistry()
    committed: list[str] = []

    def create_world_setting(context, arguments):
        committed.append(str(arguments.get("value") or ""))
        return GMToolReceipt.success(
            "create_world_setting",
            result={"category": "historical_events", "value": arguments["value"]},
            state_changed=True,
        )

    registry.register(
        GMToolDefinition(
            name="create_world_setting",
            description="测试世界写入。",
            handler=create_world_setting,
            parameters=(
                GMToolParameter("value", "string", "测试值。", required=True),
            ),
            side_effect="write",
        )
    )
    client = _ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "call_tool",
                    "tool_name": "create_world_setting",
                    "arguments": {"value": "百年前的双日坠落改变了季风。"},
                    "reason": "先补历史事件",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "decision": "final",
                    "reply": "世界历史的缺项已经补好了。",
                    "reason": "完成标准满足",
                },
                ensure_ascii=False,
            ),
        ]
    )
    manager = _manager(tmp_path, client, registry)
    try:
        task = manager.enqueue(
            context=_context(),
            title="补全世界历史",
            objective="为当前世界补一项重大历史事件并写入存档。",
            completion_criteria=["存在一项已写入的重大历史事件"],
            domains=["world"],
            requires_state_change=True,
        )
        assert manager.run_task_once(task.task_id) == "continue"
        after_first = manager.get_task(task.task_id)
        assert after_first is not None
        assert after_first.step_count == 1
        assert after_first.status == "queued"
        assert committed == ["百年前的双日坠落改变了季风。"]

        assert manager.run_task_once(task.task_id) == "completed"
        completed = manager.get_task(task.task_id)
        assert completed is not None
        assert completed.status == "completed"
        assert completed.notification_status == "pending"
        assert len(client.calls) == 2
    finally:
        manager.shutdown()


def test_completion_cannot_defer_the_requested_deliverable(tmp_path) -> None:
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="get_rule_reference",
            description="查询规则。",
            handler=lambda context, arguments: GMToolReceipt.success(
                "get_rule_reference",
                result={"summary": "旅行检定每天进行一次。"},
            ),
        )
    )
    client = _ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "call_tool",
                    "tool_name": "get_rule_reference",
                    "arguments": {},
                    "reason": "取得依据",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "decision": "final",
                    "reply": "已受理，整理好后再告诉你。",
                    "reason": "错误地只报告进度",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "decision": "final",
                    "reply": "旅行检定每天进行一次。",
                    "reason": "直接交付结论",
                },
                ensure_ascii=False,
            ),
        ]
    )
    manager = _manager(tmp_path, client, registry)
    try:
        task = manager.enqueue(
            context=_context(),
            title="整理旅行规则",
            objective="查清旅行检定频率并直接给出结论。",
            completion_criteria=["玩家收到旅行检定频率"],
            domains=["rules"],
            requires_state_change=False,
            max_steps=4,
        )
        assert manager.run_task_once(task.task_id) == "continue"
        assert manager.run_task_once(task.task_id) == "retry"
        assert manager.get_task(task.task_id).status == "queued"
        assert manager.run_task_once(task.task_id) == "completed"
        assert manager.get_task(task.task_id).final_reply == "旅行检定每天进行一次。"
    finally:
        manager.shutdown()


def test_model_planning_does_not_hold_campaign_lock(tmp_path) -> None:
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="get_rule_reference",
            description="测试读取。",
            handler=lambda context, arguments: GMToolReceipt.success(
                "get_rule_reference",
                result={"found": True},
            ),
        )
    )
    client = _BlockingClient(
        json.dumps(
            {
                "decision": "final",
                "reply": "资料整理完成。",
                "reason": "无需写入",
            },
            ensure_ascii=False,
        )
    )
    host = _Host()
    manager = GMBackgroundDelegationManager(tmp_path)
    manager._submit = lambda _task_id: None
    manager.bind(
        host=host,
        client=client,
        model="test-model",
        registry=registry,
        state_builder=_StateBuilder(),
    )
    task = manager.enqueue(
        context=_context(),
        title="查规则",
        objective="整理一份规则答案，不修改状态。",
        completion_criteria=["给出有依据的结论"],
        domains=["reward"],
        requires_state_change=False,
    )
    thread = threading.Thread(target=manager.run_task_once, args=(task.task_id,))
    try:
        thread.start()
        assert client.started.wait(timeout=2)
        assert host.runtime.transaction_lock.acquire(timeout=0.2)
        host.runtime.transaction_lock.release()
        client.release.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert manager.get_task(task.task_id).status == "completed"
    finally:
        client.release.set()
        thread.join(timeout=1)
        manager.shutdown()


def test_cancelling_during_model_planning_prevents_late_write(tmp_path) -> None:
    committed: list[str] = []
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="create_world_setting",
            description="写入世界设定。",
            handler=lambda context, arguments: (
                committed.append(str(arguments["value"]))
                or GMToolReceipt.success(
                    "create_world_setting",
                    state_changed=True,
                )
            ),
            parameters=(
                GMToolParameter("value", "string", "设定。", required=True),
            ),
            side_effect="write",
        )
    )
    client = _BlockingClient(
        json.dumps(
            {
                "decision": "call_tool",
                "tool_name": "create_world_setting",
                "arguments": {"value": "不应被提交的迟到设定"},
                "reason": "迟到的模型计划",
            },
            ensure_ascii=False,
        )
    )
    manager = GMBackgroundDelegationManager(tmp_path)
    manager._submit = lambda _task_id: None
    manager.bind(
        host=_Host(),
        client=client,
        model="test-model",
        registry=registry,
        state_builder=_StateBuilder(),
    )
    task = manager.enqueue(
        context=_context(),
        title="可取消的设定",
        objective="写入一项世界设定。",
        completion_criteria=["设定已提交"],
        domains=["world"],
        requires_state_change=True,
    )
    thread = threading.Thread(target=manager.run_task_once, args=(task.task_id,))
    try:
        thread.start()
        assert client.started.wait(timeout=2)
        manager.cancel(task.task_id, notify=False)
        client.release.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert committed == []
        assert manager.get_task(task.task_id).status == "cancelled"
    finally:
        client.release.set()
        thread.join(timeout=1)
        manager.shutdown()


def test_scheduler_runs_task_to_completion_and_queues_private_notice(tmp_path) -> None:
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="get_rule_reference",
            description="读取规则依据。",
            handler=lambda context, arguments: GMToolReceipt.success(
                "get_rule_reference",
                result={"answer": "旅行检定每天一次。"},
            ),
        )
    )
    client = _ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "call_tool",
                    "tool_name": "get_rule_reference",
                    "arguments": {},
                    "reason": "先核对规则",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "decision": "final",
                    "reply": "旅行规则已经核对好了。",
                    "reason": "完成标准满足",
                },
                ensure_ascii=False,
            ),
        ]
    )
    manager = GMBackgroundDelegationManager(tmp_path, max_workers=1)
    manager.bind(
        host=_Host(),
        client=client,
        model="test-model",
        registry=registry,
        state_builder=_StateBuilder(),
    )
    try:
        task = manager.enqueue(
            context=_private_context(),
            title="核对旅行规则",
            objective="核对旅行检定频率。",
            completion_criteria=["给出规则结论"],
            domains=["reward"],
            requires_state_change=False,
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = manager.get_task(task.task_id)
            if current is not None and current.status == "completed":
                break
            time.sleep(0.02)
        completed = manager.get_task(task.task_id)
        assert completed is not None
        assert completed.status == "completed"
        notification = manager.poll_notification(
            campaign_id="campaign",
            session_id="8711",
            channel_id="8711",
        )
        assert notification is not None
        assert notification["is_private"] is True
        assert notification["reply"] == "旅行规则已经核对好了。"
    finally:
        manager.shutdown()


def test_long_task_releases_campaign_between_each_committed_step(tmp_path) -> None:
    registry = GMToolRegistry()
    committed: list[str] = []

    def create_world_setting(context, arguments):
        committed.append(str(arguments["value"]))
        return GMToolReceipt.success(
            "create_world_setting",
            result={"operation": "create", "value": arguments["value"]},
            state_changed=True,
        )

    registry.register(
        GMToolDefinition(
            name="create_world_setting",
            description="逐项写入世界设定。",
            handler=create_world_setting,
            parameters=(
                GMToolParameter("value", "string", "设定。", required=True),
            ),
            side_effect="write",
        )
    )
    client = _ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "call_tool",
                    "tool_name": "create_world_setting",
                    "arguments": {"value": value},
                    "reason": "逐项提交",
                },
                ensure_ascii=False,
            )
            for value in ("历史事件", "世界奥秘", "世界威胁")
        ]
        + [
            json.dumps(
                {
                    "decision": "final",
                    "reply": "三项世界缺口都已经补进存档。",
                    "reason": "完成标准满足",
                },
                ensure_ascii=False,
            )
        ]
    )
    host = _Host()
    manager = GMBackgroundDelegationManager(tmp_path)
    manager._submit = lambda _task_id: None
    manager.bind(
        host=host,
        client=client,
        model="test-model",
        registry=registry,
        state_builder=_StateBuilder(),
    )
    task = manager.enqueue(
        context=_context(),
        title="补全世界缺项",
        objective="补入历史、奥秘与威胁。",
        completion_criteria=["三项均已提交"],
        domains=["world"],
        requires_state_change=True,
    )
    try:
        for expected_count in (1, 2, 3):
            assert manager.run_task_once(task.task_id) == "continue"
            # A foreground message can take the same campaign lock between any
            # two background steps instead of waiting for the whole job.
            assert host.runtime.transaction_lock.acquire(timeout=0.2)
            host.runtime.transaction_lock.release()
            assert len(committed) == expected_count
        assert manager.run_task_once(task.task_id) == "completed"
        assert committed == ["历史事件", "世界奥秘", "世界威胁"]
    finally:
        manager.shutdown()


def test_synchronous_map_renderer_is_not_exposed_to_generic_background_runner(
    tmp_path,
) -> None:
    registry = GMToolRegistry()
    for name, side_effect, defer_group in (
        ("get_world_map_status", "read", ""),
        ("generate_world_map_preview", "write", "map_render"),
        ("edit_world_map", "write", ""),
    ):
        registry.register(
            GMToolDefinition(
                name=name,
                description=name,
                handler=lambda context, arguments, tool=name: GMToolReceipt.success(
                    tool
                ),
                side_effect=side_effect,
                defer_group=defer_group,
            )
        )
    manager = _manager(tmp_path, _ScriptedClient([]), registry)
    try:
        task = manager.enqueue(
            context=_context(),
            title="地图检查",
            objective="检查地图状态。",
            completion_criteria=["完成检查"],
            domains=["map"],
            requires_state_change=False,
        )
        allowed = manager._allowed_tool_names(
            manager._execution_context(task),
            task,
        )
        assert "get_world_map_status" in allowed
        assert "generate_world_map_preview" not in allowed
        assert "edit_world_map" not in allowed
    finally:
        manager.shutdown()


def test_authorized_read_research_remains_available_before_session_start(
    tmp_path,
) -> None:
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="get_rule_reference",
            description="查询规则。",
            handler=lambda context, arguments: GMToolReceipt.success(
                "get_rule_reference"
            ),
        )
    )
    manager = _manager(tmp_path, _ScriptedClient([]), registry)
    try:
        task = manager.enqueue(
            context=_context(),
            title="开团前查规则",
            objective="在开团前整理旅行规则。",
            completion_criteria=["给出规则结论"],
            domains=["rules"],
            requires_state_change=False,
        )
        inactive_context = manager._execution_context(task)
        inactive_context.gate_status = "inactive"
        allowed = manager._allowed_tool_names(inactive_context, task)
        assert "get_rule_reference" in allowed
    finally:
        manager.shutdown()


def test_waiting_task_can_resume_and_owner_can_cancel(tmp_path) -> None:
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="get_rule_reference",
            description="测试读取。",
            handler=lambda context, arguments: GMToolReceipt.success(
                "get_rule_reference"
            ),
        )
    )
    client = _ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "ask_user",
                    "reply": "这张地图要沿用旧海岸线，还是重新生成？",
                    "reason": "缺少必要选择",
                },
                ensure_ascii=False,
            )
        ]
    )
    manager = _manager(tmp_path, client, registry)
    try:
        task = manager.enqueue(
            context=_context(),
            title="调整地图",
            objective="调整地图。",
            completion_criteria=["地图方案明确"],
            domains=["reward"],
            requires_state_change=False,
        )
        assert manager.run_task_once(task.task_id) == "waiting_user"
        waiting = manager.get_task(task.task_id)
        assert waiting.status == "waiting_user"
        assert waiting.notification_status == "pending"

        resumed = manager.resume(task.task_id, "沿用旧海岸线。")
        assert resumed is not None
        assert resumed.status == "queued"
        assert resumed.waiting_response == "沿用旧海岸线。"
        cancelled = manager.cancel(task.task_id, notify=False)
        assert cancelled is not None
        assert cancelled.status == "cancelled"
        assert cancelled.notification_status == "none"
    finally:
        manager.shutdown()


def test_only_task_owner_can_cancel_or_answer_waiting_task(tmp_path) -> None:
    service = FUGMHttpService(data_root=tmp_path / "campaigns", use_llm=False)
    owner = _context()
    other = _context()
    other.speaker = "loading"
    other.metadata["source_speaker_id"] = "1628"
    other.metadata["current_turn_events"] = [
        {
            "event_id": "event-other",
            "message_id": "message-other",
            "speaker": "loading",
            "speaker_id": "1628",
            "text": "我来回答这个后台问题。",
            "is_at_gm": True,
        }
    ]
    try:
        service.background_delegation_manager._submit = lambda _task_id: None
        task = service.background_delegation_manager.enqueue(
            context=owner,
            title="补设定",
            objective="补完世界威胁。",
            completion_criteria=["世界威胁已确定"],
            domains=["world"],
            requires_state_change=True,
        )
        service.background_delegation_manager._wait_for_user(
            task.task_id,
            "威胁更偏向天灾还是帝国？",
        )

        denied_resume = service.gm_tool_registry.execute(
            "resume_background_task",
            {"task_id": task.task_id, "response": "帝国。"},
            other,
        )
        denied_cancel = service.gm_tool_registry.execute(
            "cancel_background_task",
            {"task_id": task.task_id},
            other,
        )
        assert denied_resume.ok is False
        assert denied_resume.error_code == "BACKGROUND_TASK_OWNER_REQUIRED"
        assert denied_cancel.ok is False
        assert denied_cancel.error_code == "BACKGROUND_TASK_OWNER_REQUIRED"
        assert service.background_delegation_manager.get_task(task.task_id).status == "waiting_user"

        resumed = service.gm_tool_registry.execute(
            "resume_background_task",
            {"task_id": task.task_id, "response": "帝国。"},
            owner,
        )
        assert resumed.ok is True
        assert service.background_delegation_manager.get_task(task.task_id).status == "queued"
    finally:
        service.shutdown()


def test_service_tools_persist_task_and_delivery_is_idempotent(tmp_path) -> None:
    service = FUGMHttpService(data_root=tmp_path / "campaigns", use_llm=False)
    context = _context()
    try:
        service.background_delegation_manager._submit = lambda _task_id: None
        service.background_delegation_manager.bind(
            host=service,
            client=_ScriptedClient([]),
            model="test-model",
            registry=service.gm_tool_registry,
            state_builder=service.gm_agent_message_coordinator.state_builder,
        )
        receipt = service.gm_tool_registry.execute(
            "delegate_background_task",
            {
                "title": "补全世界",
                "objective": "补全两项世界设定。",
                "completion_criteria": ["设定写入当前档"],
                "domains": ["world"],
                "requires_state_change": True,
            },
            context,
        )
        assert receipt.ok is True
        assert receipt.lock_public_reply is True
        task_id = receipt.result["task"]["task_id"]
        assert (tmp_path / "campaigns" / "_service" / "background_delegations.json").is_file()

        service.background_delegation_manager._complete(
            task_id,
            "缺少的两项世界设定已经补进当前存档。",
        )
        status, polled = service.handle(
            "POST",
            "/v1/background-delegations/poll",
            {
                "campaign_id": "campaign",
                "session_id": "group-1",
                "channel_id": "group-1",
            },
        )
        assert status == 200
        assert polled["send_reply"] is True
        notification_id = polled["notification_id"]

        status, delivered = service.handle(
            "POST",
            "/v1/background-delegations/delivered",
            {"notification_id": notification_id},
        )
        assert status == 200
        assert delivered["ok"] is True
        status, duplicate = service.handle(
            "POST",
            "/v1/background-delegations/delivered",
            {"notification_id": notification_id},
        )
        assert duplicate["already_delivered"] is True

        transcript = service._runtime("campaign").log_manager.load_transcript(
            "campaign",
            "group-1",
        )
        notifications = [
            item
            for item in transcript
            if item.metadata.get("mode") == "background_delegation_notification"
        ]
        assert len(notifications) == 1
    finally:
        service.shutdown()


def test_running_task_recovers_as_queued_after_restart(tmp_path) -> None:
    first = GMBackgroundDelegationManager(tmp_path)
    first._submit = lambda _task_id: None
    task = first.enqueue(
        context=_context(),
        title="恢复测试",
        objective="测试重启恢复。",
        completion_criteria=["任务保留"],
        domains=["world"],
        requires_state_change=False,
    )
    with first._lock:
        first._tasks[task.task_id].status = "running"
        first._persist_locked()
    first.shutdown()

    second = GMBackgroundDelegationManager(tmp_path)
    second._submit = lambda _task_id: None
    try:
        second.bind(
            host=_Host(),
            client=_ScriptedClient([]),
            model="test-model",
            registry=GMToolRegistry(),
            state_builder=_StateBuilder(),
        )
        recovered = second.get_task(task.task_id)
        assert recovered is not None
        assert recovered.status == "queued"
        assert "重启" in recovered.last_error
    finally:
        second.shutdown()
