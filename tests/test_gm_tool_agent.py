import json
import tempfile
import threading
import time
import unittest

from fu_gm.gm_tool_agent import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolReceipt,
    GMToolRegistry,
    LLMGMToolAgent,
)
from fu_gm.http_server import FUGMHttpService
from fu_gm.gm_tool_receipts import GMToolReceiptPolicy
from fu_gm.models import HeroDraft
from fu_gm.models import Character, SceneType


class ScriptedClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def create_chat_completion(self, **kwargs) -> str:
        self.calls.append(dict(kwargs))
        if not self.responses:
            raise AssertionError("缺少脚本化模型响应。")
        return self.responses.pop(0)


def execution_context(*, campaign_id: str = "agent-test", speaker: str = "阿凛") -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id=campaign_id,
        session_id="s1",
        channel_id="group-1",
        speaker=speaker,
        gate_status="adventure",
        directly_addressed=True,
    )


class GMToolRegistryTests(unittest.TestCase):
    def test_provider_failure_rolls_back_incomplete_cross_iteration_transaction(
        self,
    ) -> None:
        state: list[str] = []

        class Transaction:
            def __init__(self) -> None:
                self.before = list(state)
                self.active = True

            def commit(self) -> None:
                self.active = False

            def rollback(self) -> None:
                if self.active:
                    state[:] = self.before
                    self.active = False

        registry = GMToolRegistry(transaction_factory=lambda *_args: Transaction())
        registry.register(
            GMToolDefinition(
                name="prepare_transition",
                description="prepare",
                handler=lambda _context, _arguments: (
                    state.append("prepared")
                    or GMToolReceipt(
                        tool_name="prepare_transition",
                        ok=True,
                        result={
                            "required_followup_tools": ["finish_transition"],
                        },
                        state_changed=True,
                        public_fallback_reply="前置步骤完成。",
                        lock_public_reply=True,
                    )
                ),
                side_effect="write_pending",
            )
        )
        registry.register(
            GMToolDefinition(
                name="finish_transition",
                description="finish",
                handler=lambda _context, _arguments: (
                    state.append("finished")
                    or GMToolReceipt.success(
                        "finish_transition",
                        state_changed=True,
                        public_reply="转场完成。",
                        lock_public_reply=True,
                    )
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "prepare_transition",
                        "arguments": {},
                    }
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "@时悠 带我们转场",
            recent_context="",
            context=execution_context(),
            state_summary={"marker": "before"},
        )

        self.assertEqual(state, [])
        self.assertFalse(outcome.state_changed)
        self.assertEqual(
            outcome.mode,
            "gm_agent_message_transaction_rolled_back",
        )
        self.assertIn("没有留下改动", outcome.reply)
        self.assertTrue(outcome.receipts[0].result["rolled_back"])
        self.assertTrue(
            any("message_transaction_rollback" in item for item in outcome.trace)
        )

    def test_terminal_read_receipt_finishes_without_second_model_call(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="inspect_status",
                description="inspect",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "inspect_status",
                    result={
                        "active_alerts": [],
                        "terminal_public_result": True,
                    },
                    public_reply="监督检查完成：当前没有活动告警。",
                    lock_public_reply=True,
                ),
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "inspect_status",
                        "arguments": {},
                    }
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "@时悠 检查监督状态",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(outcome.mode, "gm_agent_tool")
        self.assertEqual(outcome.target, "fu_gm")
        self.assertEqual(outcome.reply, "监督检查完成：当前没有活动告警。")
        self.assertFalse(outcome.state_changed)
        self.assertFalse(outcome.error)

    def test_provider_failure_keeps_complete_tool_result_with_authoritative_reply(
        self,
    ) -> None:
        state: list[str] = []

        class Transaction:
            def __init__(self) -> None:
                self.before = list(state)
                self.active = True

            def commit(self) -> None:
                self.active = False

            def rollback(self) -> None:
                if self.active:
                    state[:] = self.before
                    self.active = False

        registry = GMToolRegistry(transaction_factory=lambda *_args: Transaction())
        registry.register(
            GMToolDefinition(
                name="record_fact",
                description="record",
                handler=lambda _context, _arguments: (
                    state.append("recorded")
                    or GMToolReceipt(
                        tool_name="record_fact",
                        ok=True,
                        state_changed=True,
                        public_fallback_reply="这项设定记下了。",
                    )
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "record_fact",
                        "arguments": {},
                    }
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "@时悠 记下这项设定",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(state, ["recorded"])
        self.assertTrue(outcome.state_changed)
        self.assertEqual(outcome.reply, "这项设定记下了。")

    def test_real_focus_branch_rolls_back_when_required_action_never_arrives(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as data_root:
            service = FUGMHttpService(data_root=data_root, use_llm=False)
            runtime = service._runtime("focus-rollback")
            for name in ("伊莉雅", "赛璃"):
                runtime.app.character_manager.add(
                    Character(
                        name=name,
                        attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                        max_hp=45,
                        hp=45,
                        max_mp=45,
                        mp=45,
                        traits=["pc"],
                    )
                )
            original = runtime.app.start_scene(
                "风铃廊",
                SceneType.STANDARD,
                location="白花碑驿站",
                participants=["伊莉雅"],
            )
            service._autosave_campaign(runtime, "focus-rollback")
            snapshot_path = runtime.app.memory_store._snapshot_path(
                "focus-rollback"
            )
            snapshot_before = snapshot_path.read_bytes()
            client = ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "focus_scene_branch",
                            "arguments": {
                                "actor": "赛璃",
                                "name": "驿站外缘",
                                "scene_type": "standard",
                                "location": "白花碑驿站外缘",
                                "objective": "查看追兵火光",
                                "private_situation": {
                                    "current_pressure": "远处有巡逻灯火",
                                },
                            },
                        },
                        ensure_ascii=False,
                    )
                ]
            )
            agent = LLMGMToolAgent(
                client,
                model="fake",
                registry=service.gm_tool_registry,
            )
            context = execution_context(
                campaign_id="focus-rollback",
                speaker="赛璃",
            )
            context.metadata.update(
                {
                    "current_message": "赛璃去驿站外缘查看追兵火光。",
                    "recent_public_context": "伊莉雅留在风铃廊。",
                }
            )

            outcome = agent.run(
                "赛璃去驿站外缘查看追兵火光。",
                recent_context="伊莉雅留在风铃廊。",
                context=context,
                state_summary={},
            )

            self.assertFalse(outcome.state_changed)
            self.assertEqual(
                outcome.mode,
                "gm_agent_message_transaction_rolled_back",
            )
            self.assertEqual(
                runtime.app.scene_manager.current_scene.scene_id,
                original.scene_id,
            )
            self.assertEqual(runtime.app.scene_manager.suspended_scenes, [])
            self.assertEqual(snapshot_path.read_bytes(), snapshot_before)

    def test_execution_scope_rejects_system_only_tool_from_player_message(self) -> None:
        calls: list[str] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="decide_npc_action",
                description="system NPC beat",
                handler=lambda _context, _arguments: (
                    calls.append("executed")
                    or GMToolReceipt.success(
                        "decide_npc_action",
                        state_changed=True,
                    )
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "decide_npc_action",
                        "arguments": {},
                    }
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "reply": "这不是当前玩家消息可以触发的行动。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "继续。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(calls, [])
        self.assertEqual(outcome.reply, "这不是当前玩家消息可以触发的行动。")
        self.assertTrue(
            any(
                step.get("protocol_error") == "TOOL_NOT_AVAILABLE_IN_CONTEXT"
                for step in outcome.trace
            )
        )

    def test_execution_scope_allows_system_only_tool_during_free_scene_beat(self) -> None:
        calls: list[str] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="decide_npc_action",
                description="system NPC beat",
                handler=lambda _context, _arguments: (
                    calls.append("executed")
                    or GMToolReceipt.success(
                        "decide_npc_action",
                        state_changed=True,
                        public_reply="守门人终于从门后走了出来。",
                        lock_public_reply=True,
                    )
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "decide_npc_action",
                        "arguments": {},
                    }
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context(speaker="系统主动节拍")
        context.directly_addressed = False
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "free_scene_beat",
            }
        )

        outcome = agent.run(
            "请判断现场NPC是否需要行动。",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertEqual(calls, ["executed"])
        self.assertEqual(outcome.reply, "守门人终于从门后走了出来。")

    def test_execution_scope_rejects_adventure_tool_during_session_zero(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="start_scene",
                description="start an adventure scene",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "start_scene",
                    state_changed=True,
                ),
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient([]),
            model="fake",
            registry=registry,
        )
        context = execution_context()
        context.gate_status = "session_zero"

        self.assertFalse(agent._tool_is_permitted("start_scene", context))

    def test_execution_scope_preserves_unmanaged_extension_tools(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="custom_extension",
                description="custom",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "custom_extension"
                ),
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient([]),
            model="fake",
            registry=registry,
        )

        self.assertTrue(
            agent._tool_is_permitted("custom_extension", execution_context())
        )

    def test_agent_prompt_composes_only_current_phase_guidance(self) -> None:
        agent = LLMGMToolAgent(
            ScriptedClient([]),
            model="fake",
            registry=GMToolRegistry(),
        )
        session_context = execution_context()
        session_context.gate_status = "session_zero"
        session_prompt = agent._system_prompt(
            session_context,
            observed_state={},
        )
        session_post_tool_prompt = agent._system_prompt(
            session_context,
            observed_state={},
            has_receipts=True,
        )
        adventure_prompt = agent._system_prompt(
            execution_context(),
            observed_state={"runtime": {"conflict": {"active": False}}},
        )

        self.assertIn("只提交current_message新增或明确纠正的最小差量", session_prompt)
        self.assertIn("tool_name、arguments、calls、terminal_decision", session_prompt)
        self.assertIn("我们确认/大家决定", session_prompt)
        self.assertIn("行动处于什么阶段", session_prompt)
        self.assertIn("我们要不要问会长", session_prompt)
        self.assertIn("不得把建议改成行动", session_prompt)
        self.assertIn("没有征求评价", session_prompt)
        self.assertIn("recorded_categories", session_prompt)
        self.assertIn("仍须另写historical_events", session_prompt)
        self.assertIn("不得把更新与确认放进同一个call_tools批次", session_prompt)
        self.assertIn("逐句重读current_message", session_post_tool_prompt)
        self.assertIn("不算完成重大历史分类", session_post_tool_prompt)
        self.assertNotIn("### NPC与集体", session_prompt)
        self.assertIn("required_followup_calls", adventure_prompt)
        self.assertIn("移动后必须兑现的NPC承诺", adventure_prompt)
        self.assertIn("followup调用与ID", adventure_prompt)
        self.assertNotIn("## 当前阶段：开团前与第零章", adventure_prompt)

    def test_hero_update_and_confirmation_batch_requires_observation(self) -> None:
        agent = LLMGMToolAgent(
            ScriptedClient([]),
            model="fake",
            registry=GMToolRegistry(),
        )

        error = agent._dependent_batch_error(
            [
                {
                    "tool_name": "update_hero_draft",
                    "arguments": {"subject": "苍祈", "patch": {"equipment": ["魔典"]}},
                },
                {
                    "tool_name": "confirm_hero_draft",
                    "arguments": {"subject": "苍祈"},
                },
            ]
        )

        self.assertIsNotNone(error)
        protocol_error = error["protocol_error"]
        self.assertEqual(
            protocol_error["error_code"],
            "DEPENDENT_TOOL_BATCH_REQUIRES_OBSERVATION",
        )
        self.assertIn("先单独调用update_hero_draft", protocol_error["correction_hint"])

    def test_required_retry_schema_bypasses_normal_phase_scope(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="perform_check_action",
                description="check",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "perform_check_action"
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="perform_in_scene_action",
                description="position",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "perform_in_scene_action"
                ),
                parameters=(
                    GMToolParameter("actor", "string", "actor", required=True),
                ),
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient([]),
            model="fake",
            registry=registry,
        )
        context = execution_context()
        schemas = agent._available_tool_schemas(
            context,
            required_retry_tool="perform_in_scene_action",
        )

        self.assertEqual(
            [str(schema.get("name") or "") for schema in schemas],
            ["perform_in_scene_action"],
        )

    def test_post_tool_iteration_uses_focused_receipt_prompt(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="update_hero",
                description="update hero",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="update_hero",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="技能记下了。",
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "update_hero",
                        "arguments": {},
                    }
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "reply": "这项技能记下了。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "伊莉雅选择保镖。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(outcome.reply, "这项技能记下了。")
        self.assertIn("工具事务收尾层", client.calls[1]["messages"][0].content)
        self.assertIn("不催填", client.calls[1]["messages"][0].content)

    def test_cross_scene_move_required_npc_followup_finishes_same_message(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="move_scene_group",
                description="move then ask",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="move_scene_group",
                    ok=True,
                    state_changed=True,
                    result={
                        "required_followup_tools": ["decide_npc_response"],
                        "required_followup_calls": [
                            {
                                "tool_name": "decide_npc_response",
                                "arguments": {
                                    "name": "白花守望会会长",
                                    "actor": "苍祈",
                                    "response_instruction": "明确表态。",
                                },
                            }
                        ],
                    },
                    public_fallback_reply="苍祈抵达风铃廊。",
                    lock_public_reply=True,
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="decide_npc_response",
                description="npc answers",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="decide_npc_response",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="会长说：“旧路可以开，但巡守必须同行。”",
                    lock_public_reply=True,
                ),
                parameters=(
                    GMToolParameter("name", "string", "NPC", required=True),
                    GMToolParameter("actor", "string", "交谈者", required=True),
                    GMToolParameter(
                        "response_instruction",
                        "string",
                        "回应要求",
                        required=True,
                    ),
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "move_scene_group",
                        "arguments": {},
                    }
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "decide_npc_response",
                        "arguments": {
                            "name": "白花守望会会长",
                            "actor": "苍祈",
                            "response_instruction": "明确表态。",
                        },
                    }
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "苍祈去风铃廊请会长明确表态。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(
            [receipt.tool_name for receipt in outcome.receipts],
            ["move_scene_group", "decide_npc_response"],
        )
        self.assertIn("苍祈抵达风铃廊", outcome.reply)
        self.assertIn("旧路可以开", outcome.reply)
        second_request = json.loads(client.calls[1]["messages"][-1].content)
        self.assertEqual(
            [tool["name"] for tool in second_request["available_tools"]],
            ["decide_npc_response"],
        )

    def test_independent_followups_cannot_be_skipped_after_first_completion(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="perform_check_action",
                description="check with two independent consequences",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="perform_check_action",
                    ok=True,
                    state_changed=True,
                    result={
                        "required_followup_tools": [
                            "resolve_gm_opportunity",
                            "decide_npc_response",
                        ],
                        "required_followup_calls": [
                            {
                                "tool_name": "resolve_gm_opportunity",
                                "arguments": {},
                            },
                            {
                                "tool_name": "decide_npc_response",
                                "arguments": {},
                            },
                        ],
                        "required_followup_mode": "all",
                    },
                    public_fallback_reply="伊莉雅失手惊动了守望会。",
                    lock_public_reply=True,
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="resolve_gm_opportunity",
                description="resolve fumble",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="resolve_gm_opportunity",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="门外的巡逻铃骤然响起。",
                    lock_public_reply=True,
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="decide_npc_response",
                description="npc pays off condition",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="decide_npc_response",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply=(
                        "会长沉声道：“我答应的路，仍会替你们打开。”"
                    ),
                    lock_public_reply=True,
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "perform_check_action",
                        "arguments": {},
                    }
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "resolve_gm_opportunity",
                        "arguments": {},
                    }
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "reply": "巡逻铃响起，事情结束了。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "decide_npc_response",
                        "arguments": {},
                    }
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "伊莉雅尝试履行会长提出的条件。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(
            [receipt.tool_name for receipt in outcome.receipts],
            [
                "perform_check_action",
                "resolve_gm_opportunity",
                "decide_npc_response",
            ],
        )
        self.assertIn("伊莉雅失手", outcome.reply)
        self.assertIn("巡逻铃骤然响起", outcome.reply)
        self.assertIn("我答应的路", outcome.reply)
        self.assertEqual(len(client.calls), 4)

    def test_required_followup_rejects_wrong_npc_before_tool_execution(self) -> None:
        registry = GMToolRegistry()
        executed: list[dict[str, object]] = []
        registry.register(
            GMToolDefinition(
                name="perform_check_action",
                description="check that obligates one NPC",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="perform_check_action",
                    ok=True,
                    state_changed=True,
                    result={
                        "required_followup_tools": ["decide_npc_response"],
                        "required_followup_calls": [
                            {
                                "tool_name": "decide_npc_response",
                                "arguments": {
                                    "name": "白花守望会会长",
                                    "condition_id": "condition-1",
                                },
                            }
                        ],
                        "required_followup_mode": "all",
                    },
                    public_fallback_reply="伊莉雅完成了约定的暗号。",
                    lock_public_reply=True,
                ),
                side_effect="write",
            )
        )

        def answer(_context, arguments):
            executed.append(dict(arguments))
            return GMToolReceipt(
                tool_name="decide_npc_response",
                ok=True,
                state_changed=True,
                result={"npc": arguments["name"]},
                public_fallback_reply="会长打开了旧路闸门。",
                lock_public_reply=True,
            )

        registry.register(
            GMToolDefinition(
                name="decide_npc_response",
                description="npc answers",
                handler=answer,
                parameters=(
                    GMToolParameter(
                        "name",
                        "string",
                        "NPC name",
                        required=True,
                    ),
                    GMToolParameter(
                        "condition_id",
                        "string",
                        "condition",
                        required=True,
                    ),
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "perform_check_action",
                        "arguments": {},
                    }
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "decide_npc_response",
                        "arguments": {
                            "name": "白花巡守",
                            "condition_id": "condition-1",
                        },
                    }
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "decide_npc_response",
                        "arguments": {
                            "name": "白花守望会会长",
                            "condition_id": "condition-1",
                        },
                    }
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "伊莉雅完成了会长要求的风铃暗号。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(
            executed,
            [
                {
                    "name": "白花守望会会长",
                    "condition_id": "condition-1",
                }
            ],
        )
        self.assertEqual(
            [receipt.tool_name for receipt in outcome.receipts],
            ["perform_check_action", "decide_npc_response"],
        )
        self.assertIn("打开了旧路闸门", outcome.reply)
        self.assertTrue(
            any(
                item.get("protocol_error")
                == "REQUIRED_FOLLOWUP_ARGUMENT_MISMATCH"
                for item in outcome.trace
            )
        )

    def test_single_successful_player_write_requires_brief_acknowledgement(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="update_hero_draft",
                description="update hero",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="update_hero_draft",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="这项角色信息记下了。",
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "audience": "table",
                        "tool_name": "update_hero_draft",
                        "arguments": {},
                        "terminal_decision": "silent",
                        "reason": "未被直接叫到的逐项技能选择只需写入。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "audience": "table",
                        "reply": "保镖记下了。",
                        "reason": "玩家要求的角色修改已经成功，需要简短确认。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context()
        context.directly_addressed = False
        context.gate_status = "session_zero"

        outcome = agent.run(
            "伊莉雅选择保镖。",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.target, "fu_gm")
        self.assertEqual(outcome.reply, "保镖记下了。")
        self.assertTrue(outcome.state_changed)
        self.assertEqual(len(client.calls), 2)
        retry_request = json.loads(client.calls[1]["messages"][-1].content)
        self.assertEqual(
            retry_request["history"][-1]["protocol_error"]["error_code"],
            "PLAYER_STATE_CHANGE_REQUIRES_ACKNOWLEDGEMENT",
        )

    def test_receipt_fallback_preserves_all_successful_batch_domains(self) -> None:
        receipts = [
            GMToolReceipt(
                tool_name="commit_world",
                ok=True,
                state_changed=True,
                public_fallback_reply="世界设定记下了。",
            ),
            GMToolReceipt(
                tool_name="record_line",
                ok=True,
                state_changed=True,
                public_fallback_reply="ok，已记录这条界限。",
            ),
            GMToolReceipt(
                tool_name="record_veil",
                ok=True,
                state_changed=True,
                public_fallback_reply="ok，已记录这条帷幕。",
            ),
        ]

        reply = GMToolReceiptPolicy.authoritative_reply(receipts)

        self.assertIn("世界设定", reply)
        self.assertIn("界限", reply)
        self.assertIn("帷幕", reply)

    def test_confirmation_fallback_supersedes_intermediate_draft_update(self) -> None:
        receipts = [
            GMToolReceipt(
                tool_name="update_hero_draft",
                ok=True,
                state_changed=True,
                public_fallback_reply="这项角色信息记下了。",
            ),
            GMToolReceipt(
                tool_name="confirm_hero_draft",
                ok=True,
                state_changed=True,
                public_fallback_reply="好，洛岚建好了。",
            ),
        ]

        reply = GMToolReceiptPolicy.authoritative_reply(receipts)

        self.assertEqual(reply, "好，洛岚建好了。")

    def test_registry_rejects_invalid_arguments_before_side_effect(self) -> None:
        calls: list[dict[str, object]] = []

        def handler(_context, arguments):
            calls.append(arguments)
            return GMToolReceipt(tool_name="save", ok=True, state_changed=True)

        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="save",
                description="test",
                handler=handler,
                parameters=(GMToolParameter("slot", "string", "slot", required=True),),
                side_effect="write",
            )
        )

        receipt = registry.execute("save", {"slot": 7}, execution_context())

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "ARGUMENT_TYPE_MISMATCH")
        self.assertTrue(receipt.retryable)
        self.assertEqual(calls, [])

    def test_registry_rejects_invalid_nested_arguments_before_side_effect(self) -> None:
        calls: list[dict[str, object]] = []

        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit_world",
                description="test",
                handler=lambda _context, arguments: (
                    calls.append(arguments)
                    or GMToolReceipt(tool_name="commit_world", ok=True, state_changed=True)
                ),
                parameters=(
                    GMToolParameter(
                        "updates",
                        "object",
                        "updates",
                        required=True,
                        schema_details={
                            "properties": {
                                "kingdoms": {
                                    "type": "object",
                                    "additionalProperties": {"type": "string"},
                                }
                            },
                            "additionalProperties": False,
                        },
                    ),
                ),
                side_effect="write",
            )
        )

        receipt = registry.execute(
            "commit_world",
            {"updates": {"kingdoms": ["钟鸣公国"]}},
            execution_context(),
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "ARGUMENT_SCHEMA_MISMATCH")
        self.assertIn("updates.kingdoms", receipt.message)
        self.assertEqual(calls, [])

    def test_nested_enum_error_lists_legal_values(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit_segments",
                description="test",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "commit_segments"
                ),
                parameters=(
                    GMToolParameter(
                        "segments",
                        "array",
                        "segments",
                        required=True,
                        schema_details={
                            "items": {
                                "type": "object",
                                "properties": {
                                    "tag": {
                                        "type": "string",
                                        "enum": ["fact", "direct_answer"],
                                    }
                                },
                                "required": ["tag"],
                                "additionalProperties": False,
                            }
                        },
                    ),
                ),
            )
        )

        receipt = registry.execute(
            "commit_segments",
            {"segments": [{"tag": "new_gate"}]},
            execution_context(),
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "ARGUMENT_SCHEMA_MISMATCH")
        self.assertIn("允许值：fact、direct_answer", receipt.message)

    def test_current_message_provenance_is_hidden_and_cannot_be_spoofed(self) -> None:
        calls: list[dict[str, object]] = []
        registry = GMToolRegistry()
        definition = GMToolDefinition(
            name="commit",
            description="commit",
            handler=lambda _context, arguments: (
                calls.append(arguments)
                or GMToolReceipt(tool_name="commit", ok=True, state_changed=True)
            ),
            parameters=(
                GMToolParameter("value", "string", "value", required=True),
                GMToolParameter(
                    "evidence",
                    "string",
                    "server provenance",
                    required=True,
                    source="current_message",
                ),
            ),
            side_effect="write",
        )
        registry.register(definition)
        context = execution_context()
        context.metadata["current_message"] = "玩家真正说的话"

        rejected = registry.execute(
            "commit",
            {"value": "事实", "evidence": "模型伪造的话"},
            context,
        )

        self.assertFalse(rejected.ok)
        self.assertEqual(rejected.error_code, "SYSTEM_ARGUMENT_NOT_ALLOWED")
        self.assertNotIn("evidence", definition.schema()["parameters"]["properties"])
        self.assertEqual(calls, [])

        receipt = registry.execute("commit", {"value": "事实"}, context)

        self.assertTrue(receipt.ok)
        self.assertEqual(
            calls,
            [{"value": "事实", "evidence": "玩家真正说的话"}],
        )

    def test_freshness_guard_and_handler_receive_effective_arguments(self) -> None:
        guard_calls: list[dict[str, object]] = []
        handler_calls: list[dict[str, object]] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit",
                description="commit",
                handler=lambda _context, arguments: (
                    handler_calls.append(dict(arguments))
                    or GMToolReceipt.success("commit", state_changed=True)
                ),
                parameters=(
                    GMToolParameter("value", "string", "value", required=True),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "server provenance",
                        required=True,
                        source="current_message",
                    ),
                ),
                side_effect="write",
            )
        )
        context = execution_context()
        context.metadata["current_message"] = "玩家真正说的话"

        receipt = registry.execute(
            "commit",
            {"value": "事实"},
            context,
            freshness_guard=lambda _definition, arguments, _context: (
                guard_calls.append(dict(arguments))
                or True
            ),
        )

        self.assertTrue(receipt.ok)
        self.assertEqual(
            guard_calls,
            [{"value": "事实", "evidence": "玩家真正说的话"}],
        )
        self.assertEqual(
            handler_calls,
            [{"value": "事实", "evidence": "玩家真正说的话"}],
        )

    def test_stale_guard_blocks_write_under_transaction_lock(self) -> None:
        calls: list[dict[str, object]] = []
        guard_calls: list[str] = []

        def handler(_context, arguments):
            calls.append(arguments)
            return GMToolReceipt(tool_name="save", ok=True, state_changed=True)

        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="save",
                description="test",
                handler=handler,
                parameters=(GMToolParameter("slot", "string", "slot", required=True),),
                side_effect="write",
            )
        )

        receipt = registry.execute(
            "save",
            {"slot": "第一幕"},
            execution_context(),
            freshness_guard=lambda definition, _arguments, _context: (
                guard_calls.append(definition.name) or False
            ),
            side_effect_lock=threading.RLock(),
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "STALE_AGENT_REQUEST")
        self.assertFalse(receipt.retryable)
        self.assertEqual(guard_calls, ["save"])
        self.assertEqual(calls, [])

    def test_read_tool_uses_campaign_lock_for_a_coherent_snapshot(self) -> None:
        entered: list[str] = []

        class TrackingLock:
            def __enter__(self):
                entered.append("enter")
                return self

            def __exit__(self, *_args):
                entered.append("exit")
                return False

        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="get_state",
                description="read",
                handler=lambda _context, _arguments: (
                    entered.append("handler")
                    or GMToolReceipt.success("get_state")
                ),
                side_effect="read",
            )
        )

        receipt = registry.execute(
            "get_state",
            {},
            execution_context(),
            side_effect_lock=TrackingLock(),
        )

        self.assertTrue(receipt.ok)
        self.assertEqual(entered, ["enter", "handler", "exit"])

    def test_agent_stops_silently_when_scheduled_write_becomes_stale(self) -> None:
        calls: list[dict[str, object]] = []

        def handler(_context, arguments):
            calls.append(arguments)
            return GMToolReceipt(tool_name="commit", ok=True, state_changed=True)

        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit",
                description="test",
                handler=handler,
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "commit",
                            "arguments": {},
                            "reply": "",
                            "reason": "提交主动节拍。",
                        },
                        ensure_ascii=False,
                    )
                ]
            ),
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "系统GM主动节拍请求",
            recent_context="",
            context=execution_context(),
            state_summary={},
            freshness_guard=lambda *_args: False,
            side_effect_lock=threading.RLock(),
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.mode, "gm_agent_stale")
        self.assertEqual(outcome.reply, "")
        self.assertEqual(calls, [])

    def test_agent_can_repair_rejected_tool_call_from_receipt(self) -> None:
        calls: list[dict[str, object]] = []

        def handler(_context, arguments):
            calls.append(arguments)
            return GMToolReceipt(
                tool_name="save_campaign",
                ok=True,
                result={"slot": arguments["slot"]},
                state_changed=True,
                public_fallback_reply="存好了。",
            )

        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="save_campaign",
                description="save",
                handler=handler,
                parameters=(GMToolParameter("slot", "string", "slot"),),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "save_campaign",
                        "arguments": {"slot": 7},
                        "reply": "",
                        "reason": "首次参数类型错误。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "save_campaign",
                        "arguments": {"slot": "第一幕结束"},
                        "reply": "",
                        "reason": "按回执修正参数。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "tool_name": "",
                        "arguments": {},
                        "reply": "存好了，叫「第一幕结束」。",
                        "reason": "工具已经成功。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry, max_iterations=4)

        outcome = agent.run(
            "@时悠 帮我存成第一幕结束",
            recent_context="阿凛刚结束了第一幕。",
            context=execution_context(),
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertTrue(outcome.state_changed)
        self.assertEqual([receipt.ok for receipt in outcome.receipts], [False, True])
        self.assertEqual(calls, [{"slot": "第一幕结束"}])
        self.assertIn("第一幕结束", outcome.reply)

    def test_agent_reasks_model_for_one_json_before_executing_any_tool(self) -> None:
        calls: list[dict[str, object]] = []

        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="save_campaign",
                description="save",
                handler=lambda _context, arguments: (
                    calls.append(arguments)
                    or GMToolReceipt(
                        tool_name="save_campaign",
                        ok=True,
                        state_changed=True,
                        public_fallback_reply="存好了。",
                    )
                ),
                parameters=(GMToolParameter("slot", "string", "slot", required=True),),
                side_effect="write",
            )
        )
        malformed = (
            '{"decision":"call_tool","tool_name":"save_campaign",'
            '"arguments":{"slot":"错误对象"}}'
            '{"decision":"not_applicable"}'
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    malformed,
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "save_campaign",
                            "arguments": {"slot": "正确对象"},
                            "reply": "",
                            "reason": "纠正为单个JSON。",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "decision": "final",
                            "tool_name": "",
                            "arguments": {},
                            "reply": "存好了。",
                            "reason": "保存完成。",
                        },
                        ensure_ascii=False,
                    ),
                ]
            ),
            model="fake",
            registry=registry,
            parse_retries=1,
        )

        outcome = agent.run(
            "@时悠 存档",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(calls, [{"slot": "正确对象"}])
        self.assertEqual(len(outcome.receipts), 1)
        self.assertTrue(outcome.receipts[0].ok)
        self.assertTrue(
            any(
                item.get("phase") == "decision_protocol_returned_to_agent"
                for item in outcome.trace
            )
        )

    def test_adjacent_tool_objects_are_validated_then_executed_as_one_batch(self) -> None:
        calls: list[str] = []
        registry = GMToolRegistry()
        for name in ("commit_world", "record_line"):
            registry.register(
                GMToolDefinition(
                    name=name,
                    description=name,
                    handler=lambda _context, _arguments, tool_name=name: (
                        calls.append(tool_name)
                        or GMToolReceipt(
                            tool_name=tool_name,
                            ok=True,
                            state_changed=True,
                            public_fallback_reply=f"{tool_name}完成。",
                        )
                    ),
                    side_effect="write",
                )
            )
        raw = "".join(
            [
                '{"decision":"call_tool","tool_name":"commit_world","arguments":{}}',
                '{"decision":"call_tool","tool_name":"record_line","arguments":{}}',
                '{"decision":"final","reply":"世界和界限都记好了。"}',
            ]
        )
        agent = LLMGMToolAgent(
            ScriptedClient([raw]),
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "@时悠 记录世界和界限",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(calls, ["commit_world", "record_line"])
        self.assertEqual(outcome.reply, "世界和界限都记好了。")
        self.assertEqual([item.ok for item in outcome.receipts], [True, True])
        self.assertEqual(outcome.trace[0]["decision"], "call_tools")

    def test_batch_stops_after_first_failed_receipt_before_later_side_effects(self) -> None:
        calls: list[str] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="first",
                description="first",
                handler=lambda _context, _arguments: (
                    calls.append("first")
                    or GMToolReceipt(
                        tool_name="first",
                        ok=False,
                        error_code="REPAIR_ME",
                        retryable=True,
                    )
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="second",
                description="second",
                handler=lambda _context, _arguments: (
                    calls.append("second")
                    or GMToolReceipt(tool_name="second", ok=True, state_changed=True)
                ),
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tools",
                            "calls": [
                                {"tool_name": "first", "arguments": {}},
                                {"tool_name": "second", "arguments": {}},
                            ],
                        }
                    ),
                    json.dumps(
                        {
                            "decision": "final",
                            "reply": "第一步没通过，后面没有执行。",
                        },
                        ensure_ascii=False,
                    ),
                ]
            ),
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "@时悠 执行两步",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(calls, ["first"])
        self.assertFalse(outcome.state_changed)
        self.assertEqual(len(outcome.receipts), 1)

    def test_replace_state_tool_must_run_before_other_batch_writes(self) -> None:
        calls: list[str] = []
        registry = GMToolRegistry()

        def load_handler(_context, _arguments):
            calls.append("load")
            return GMToolReceipt(
                tool_name="load_campaign",
                ok=True,
                result={"active_campaign_id": "旧团"},
                state_changed=True,
                public_fallback_reply="已经读回旧团。",
            )

        def write_handler(_context, _arguments):
            calls.append("write")
            return GMToolReceipt(
                tool_name="commit_note",
                ok=True,
                state_changed=True,
                public_fallback_reply="设定已写入。",
            )

        registry.register(
            GMToolDefinition(
                name="load_campaign",
                description="load",
                handler=load_handler,
                side_effect="replace_state",
            )
        )
        registry.register(
            GMToolDefinition(
                name="commit_note",
                description="write",
                handler=write_handler,
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tools",
                            "calls": [
                                {
                                    "tool_name": "load_campaign",
                                    "arguments": {},
                                },
                                {
                                    "tool_name": "commit_note",
                                    "arguments": {},
                                },
                            ],
                            "terminal_decision": "final",
                            "reply": "读档并修改完成。",
                            "reason": "错误地按旧状态规划同批写入。",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "load_campaign",
                            "arguments": {},
                            "reply": "",
                            "reason": "先单独读取战役。",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "decision": "final",
                            "reply": "已经读回旧团。",
                            "reason": "等拿到新状态后再修改。",
                        },
                        ensure_ascii=False,
                    ),
                ]
            ),
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "读取旧团，之后再改设定。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(calls, ["load"])
        self.assertEqual(
            outcome.trace[0]["protocol_error"],
            "REPLACE_STATE_BATCH_MUST_BE_ISOLATED",
        )
        self.assertTrue(outcome.state_changed)
        self.assertEqual(outcome.reply, "已经读回旧团。")

    def test_batch_rolls_back_preparatory_write_when_main_action_fails(self) -> None:
        state: list[str] = []
        action_attempts = 0

        class Transaction:
            def __init__(self) -> None:
                self.before = list(state)
                self.active = True

            def commit(self) -> None:
                self.active = False

            def rollback(self) -> None:
                if self.active:
                    state[:] = self.before
                    self.active = False

        registry = GMToolRegistry(transaction_factory=lambda *_args: Transaction())
        registry.register(
            GMToolDefinition(
                name="focus_scene_branch",
                description="focus",
                handler=lambda _context, _arguments: (
                    state.append("focused")
                    or GMToolReceipt.success(
                        "focus_scene_branch",
                        state_changed=True,
                    )
                ),
                side_effect="write",
            )
        )

        def perform(_context, arguments):
            nonlocal action_attempts
            action_attempts += 1
            if not arguments.get("valid"):
                return GMToolReceipt.failure(
                    "perform_character_action",
                    "UNKNOWN_ARGUMENT",
                    "法术参数需要修正。",
                    "补齐标准参数后重试。",
                )
            state.append("acted")
            return GMToolReceipt.success(
                "perform_character_action",
                state_changed=True,
                public_reply="行动已结算。",
                lock_public_reply=True,
            )

        registry.register(
            GMToolDefinition(
                name="perform_character_action",
                description="act",
                handler=perform,
                parameters=(GMToolParameter("valid", "boolean", "valid"),),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tools",
                        "calls": [
                            {"tool_name": "focus_scene_branch", "arguments": {}},
                            {"tool_name": "perform_character_action", "arguments": {}},
                        ],
                    }
                ),
                json.dumps(
                    {
                        "decision": "call_tools",
                        "calls": [
                            {"tool_name": "focus_scene_branch", "arguments": {}},
                            {
                                "tool_name": "perform_character_action",
                                "arguments": {"valid": True},
                            },
                        ],
                    }
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "@时悠 施法",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(state, ["focused", "acted"])
        self.assertEqual(action_attempts, 2)
        self.assertTrue(outcome.state_changed)
        self.assertEqual(outcome.reply, "行动已结算。")
        rollback = outcome.trace[0]["batch_receipts"][0]
        self.assertTrue(rollback["result"]["rolled_back"])

    def test_schema_failure_must_retry_same_tool_before_other_writes(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit_world",
                description="commit world",
                parameters=(
                    GMToolParameter("updates", "object", "world updates", required=True),
                ),
                handler=lambda _context, arguments: (
                    calls.append(("commit_world", dict(arguments)))
                    or GMToolReceipt(
                        tool_name="commit_world",
                        ok=True,
                        state_changed=True,
                        public_fallback_reply="世界记好了。",
                    )
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="record_line",
                description="record line",
                handler=lambda _context, arguments: (
                    calls.append(("record_line", dict(arguments)))
                    or GMToolReceipt(
                        tool_name="record_line",
                        ok=True,
                        state_changed=True,
                    )
                ),
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "commit_world",
                            "arguments": {
                                "updates": {"continent_name": "白钟大陆"},
                                "reason": "说明文字不应成为参数",
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "record_line",
                            "arguments": {},
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "decision": "call_tools",
                            "calls": [
                                {
                                    "tool_name": "commit_world",
                                    "arguments": {
                                        "updates": {"continent_name": "白钟大陆"}
                                    },
                                },
                                {"tool_name": "record_line", "arguments": {}},
                            ],
                            "terminal_decision": "final",
                            "reply": "都记好了。",
                        },
                        ensure_ascii=False,
                    ),
                ]
            ),
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "@时悠 记录世界和界限",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(
            calls,
            [
                ("commit_world", {"updates": {"continent_name": "白钟大陆"}}),
                ("record_line", {}),
            ],
        )
        self.assertEqual(outcome.reply, "都记好了。")
        self.assertEqual(outcome.trace[1]["protocol_error"], "SCHEMA_RETRY_TOOL_OMITTED")

    def test_agent_stops_after_three_invalid_npc_transactions(self) -> None:
        attempts: list[int] = []
        registry = GMToolRegistry()

        def fail_npc(_context, _arguments):
            attempts.append(1)
            return GMToolReceipt(
                tool_name="decide_npc_action",
                ok=False,
                error_code="NPC_RESPONSE_TRANSACTION_INVALID",
                message="GM提交的NPC行动没有承接最新公开事实。",
                correction_hint="保留同一NPC，依据最新公开事实重提事务。",
                retryable=True,
            )

        registry.register(
            GMToolDefinition(
                name="decide_npc_action",
                description="npc beat",
                handler=fail_npc,
                side_effect="write",
            )
        )
        response = json.dumps(
            {
                "decision": "call_tool",
                "tool_name": "decide_npc_action",
                "arguments": {},
            }
        )
        client = ScriptedClient([response, response, response, response])
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context()
        context.directly_addressed = False
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "free_scene_beat",
            }
        )

        outcome = agent.run(
            "推进当前局面",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.mode, "gm_agent_unresolved_silent")
        self.assertIn("连续三次", outcome.error)

    def test_agent_never_executes_the_same_successful_write_twice(self) -> None:
        calls: list[dict[str, object]] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit_world",
                description="commit",
                handler=lambda _context, arguments: (
                    calls.append(arguments)
                    or GMToolReceipt(
                        tool_name="commit_world",
                        ok=True,
                        state_changed=True,
                        public_fallback_reply="这条设定记下了。",
                    )
                ),
                parameters=(GMToolParameter("value", "string", "value", required=True),),
                side_effect="write",
            )
        )
        repeated_call = json.dumps(
            {
                "decision": "call_tool",
                "tool_name": "commit_world",
                "arguments": {"value": "沉默森林"},
            },
            ensure_ascii=False,
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    repeated_call,
                    repeated_call,
                    json.dumps(
                        {
                            "decision": "final",
                            "reply": "沉默森林记下了。",
                            "reason": "写入已经成功。",
                        },
                        ensure_ascii=False,
                    ),
                ]
            ),
            model="fake",
            registry=registry,
            max_iterations=4,
        )

        outcome = agent.run(
            "我贡献沉默森林。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(calls, [{"value": "沉默森林"}])
        self.assertEqual(len(outcome.receipts), 1)
        self.assertEqual(outcome.reply, "沉默森林记下了。")
        self.assertTrue(
            any(
                item.get("protocol_error") == "DUPLICATE_SUCCESSFUL_TOOL_CALL"
                for item in outcome.trace
            )
        )

    def test_repeated_duplicate_write_falls_back_without_a_second_side_effect(self) -> None:
        calls: list[dict[str, object]] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit_world",
                description="commit",
                handler=lambda _context, arguments: (
                    calls.append(arguments)
                    or GMToolReceipt(
                        tool_name="commit_world",
                        ok=True,
                        state_changed=True,
                        public_fallback_reply="这条设定记下了。",
                    )
                ),
                parameters=(GMToolParameter("value", "string", "value", required=True),),
                side_effect="write",
            )
        )
        repeated_call = json.dumps(
            {
                "decision": "call_tool",
                "tool_name": "commit_world",
                "arguments": {"value": "沉默森林"},
            },
            ensure_ascii=False,
        )
        agent = LLMGMToolAgent(
            ScriptedClient([repeated_call, repeated_call, repeated_call]),
            model="fake",
            registry=registry,
            max_iterations=4,
        )

        outcome = agent.run(
            "我贡献沉默森林。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(calls, [{"value": "沉默森林"}])
        self.assertEqual(len(outcome.receipts), 1)
        self.assertEqual(outcome.reply, "这条设定记下了。")

    def test_npc_response_without_authorized_followup_finishes_immediately(self) -> None:
        calls: list[dict[str, object]] = []
        registry = GMToolRegistry()

        def answer(_context, arguments):
            calls.append(dict(arguments))
            return GMToolReceipt(
                tool_name="decide_npc_response",
                ok=True,
                state_changed=True,
                public_fallback_reply="会长摇头：条件还没有补齐。",
                lock_public_reply=True,
            )

        registry.register(
            GMToolDefinition(
                name="decide_npc_response",
                description="npc",
                handler=answer,
                parameters=(
                    GMToolParameter("instruction", "string", "instruction", required=True),
                ),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "decide_npc_response",
                        "arguments": {"instruction": "回应尚未满足的条件"},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "decide_npc_response",
                        "arguments": {"instruction": "换个说法再次回应条件"},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "reply": "不应执行到这里。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry, max_iterations=4)

        outcome = agent.run(
            "伊莉雅回应会长的放行条件。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(outcome.reply, "会长摇头：条件还没有补齐。")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(outcome.receipts), 1)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(outcome.trace[-1]["tool_name"], "decide_npc_response")

    def test_repeated_invalid_json_fails_closed_without_state_change(self) -> None:
        calls: list[dict[str, object]] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="save_campaign",
                description="save",
                handler=lambda _context, arguments: (
                    calls.append(arguments)
                    or GMToolReceipt(tool_name="save_campaign", ok=True, state_changed=True)
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(["not-json", "still-not-json"])
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=registry,
            parse_retries=1,
        )

        outcome = agent.run(
            "@时悠 存档",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.mode, "gm_agent_unavailable")
        self.assertFalse(outcome.state_changed)
        self.assertEqual(calls, [])
        self.assertEqual(
            sum(item.get("phase") == "parse_recovery" for item in outcome.trace),
            2,
        )
        retry_messages = client.calls[1]["messages"]
        self.assertEqual([item.role for item in retry_messages], ["system", "user"])
        repair_instruction = retry_messages[0]
        self.assertIn("不是玩家消息", repair_instruction.content)
        self.assertIn("绝不向玩家提及", repair_instruction.content)
        self.assertIn("不得增加、删除或改换工具调用", repair_instruction.content)

    def test_readable_invalid_batch_is_returned_to_agent_before_any_call_executes(self) -> None:
        writes: list[str] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="save_campaign",
                description="save",
                handler=lambda _context, _arguments: (
                    writes.append("saved")
                    or GMToolReceipt(
                        tool_name="save_campaign",
                        ok=True,
                        state_changed=True,
                        public_fallback_reply="存好了。",
                    )
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tools",
                        "calls": [
                            {"tool_name": "save_campaign", "arguments": {}},
                            {"arguments": {"slot": "不应猜测"}},
                        ],
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "save_campaign",
                        "arguments": {},
                        "terminal_decision": "final",
                        "reply": "存好了。",
                        "reason": "根据协议错误重新提交完整决策。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="semantic-model",
            protocol_repair_model="syntax-model",
            registry=registry,
            max_iterations=3,
        )

        outcome = agent.run(
            "@时悠 存档",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(writes, ["saved"])
        self.assertEqual(outcome.reply, "存好了。")
        self.assertTrue(
            any(
                item.get("phase") == "decision_protocol_returned_to_agent"
                for item in outcome.trace
            )
        )
        second_request = json.loads(client.calls[1]["messages"][1].content)
        protocol_error = second_request["history"][-1]["protocol_error"]
        self.assertEqual(
            protocol_error["error_code"],
            "INVALID_AGENT_TOOL_PROTOCOL",
        )
        self.assertIn("calls[2]缺少tool_name", protocol_error["message"])
        self.assertIn("invalid_protocol_draft", protocol_error)

    def test_active_group_model_failure_is_silent_but_still_owned(self) -> None:
        context = execution_context()
        context.directly_addressed = False
        agent = LLMGMToolAgent(
            ScriptedClient(["not-json", "still-not-json"]),
            model="fake",
            registry=GMToolRegistry(),
            parse_retries=1,
        )

        outcome = agent.run(
            "谁方便盯外面，谁继续和会长谈？",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.mode, "gm_agent_unavailable_silent")
        self.assertEqual(outcome.reply, "")
        self.assertTrue(outcome.stop_astrbot)

    def test_system_beat_model_failure_never_emits_player_facing_error(self) -> None:
        context = execution_context()
        context.metadata["system_gm_beat_request"] = True
        agent = LLMGMToolAgent(
            ScriptedClient(["not-json", "still-not-json"]),
            model="fake",
            registry=GMToolRegistry(),
            parse_retries=1,
        )

        outcome = agent.run(
            "系统要求判断是否推进当前局面。",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.mode, "gm_agent_unavailable_silent")
        self.assertEqual(outcome.reply, "")

    def test_inactive_unaddressed_model_failure_can_route_external(self) -> None:
        context = execution_context()
        context.directly_addressed = False
        context.gate_status = "inactive"
        agent = LLMGMToolAgent(
            ScriptedClient(["not-json", "still-not-json"]),
            model="fake",
            registry=GMToolRegistry(),
            parse_retries=1,
        )

        outcome = agent.run(
            "今晚天气怎么样？",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertFalse(outcome.handled)

    def test_parse_repair_isolated_from_world_state_and_original_request(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit_scene_response",
                description="commit",
                handler=lambda _context, arguments: GMToolReceipt(
                    tool_name="commit_scene_response",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply=str(arguments["public_reply"]),
                    lock_public_reply=True,
                ),
                parameters=(
                    GMToolParameter("public_reply", "string", "reply", required=True),
                ),
                side_effect="write",
            )
        )
        malformed = (
            '{"decision":"call_tool","tool_name":"commit_scene_response",'
            '"arguments":{"public_reply":"守路人接过纸。"},reason:"交付成立"}'
        )
        repaired = json.dumps(
            {
                "decision": "call_tool",
                "tool_name": "commit_scene_response",
                "arguments": {"public_reply": "守路人接过纸。"},
                "reason": "交付成立",
            },
            ensure_ascii=False,
        )
        client = ScriptedClient(
            [
                malformed,
                repaired,
                json.dumps(
                    {"decision": "final", "reply": "", "reason": "已提交。"},
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=registry,
            parse_retries=1,
        )

        outcome = agent.run(
            "苍祈把纸递给守路人。",
            recent_context="这里有一段不应交给修复器的长聊天。",
            context=execution_context(),
            state_summary={"private_secret": "不能进入语法修复请求"},
        )

        self.assertTrue(outcome.state_changed)
        self.assertEqual(outcome.reply, "守路人接过纸。")
        repair_messages = client.calls[1]["messages"]
        joined = "\n".join(item.content for item in repair_messages)
        repair_payload = json.loads(repair_messages[1].content)
        self.assertEqual(repair_payload["malformed_protocol_draft"], malformed)
        self.assertNotIn("苍祈把纸递给守路人", joined)
        self.assertNotIn("private_secret", joined)
        self.assertNotIn("长聊天", joined)

    def test_agent_requests_sufficient_tokens_for_structured_tool_decision(self) -> None:
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "final",
                        "reply": "我在。",
                        "reason": "直接回应。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=GMToolRegistry(),
            max_output_tokens=4096,
        )

        outcome = agent.run(
            "@时悠 在吗",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(client.calls[0]["max_tokens"], 4096)

    def test_agent_allows_only_one_successful_limited_write_per_message(self) -> None:
        calls: list[dict[str, object]] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="update_draft",
                description="update once",
                handler=lambda _context, arguments: (
                    calls.append(arguments)
                    or GMToolReceipt(
                        tool_name="update_draft",
                        ok=True,
                        state_changed=True,
                        public_fallback_reply="记下了。",
                    )
                ),
                parameters=(GMToolParameter("value", "string", "value", required=True),),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "update_draft",
                        "arguments": {"value": "第一次"},
                        "reply": "",
                        "reason": "写入。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "update_draft",
                        "arguments": {"value": "第二次"},
                        "reply": "",
                        "reason": "又改一次。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "reply": "记下了。",
                        "reason": "遵循调用上限。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=registry,
            max_iterations=4,
        )

        outcome = agent.run(
            "只改一次",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(calls, [{"value": "第一次"}])
        self.assertTrue(
            any(
                item.get("protocol_error") == "TOOL_CALL_LIMIT_REACHED"
                for item in outcome.trace
            )
        )

    def test_parse_failure_after_successful_write_uses_receipt_without_agent_error(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit_part",
                description="commit",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="commit_part",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="这条设定记下了。",
                ),
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "commit_part",
                            "arguments": {},
                            "reply": "",
                            "reason": "先写入一部分。",
                        },
                        ensure_ascii=False,
                    ),
                    "not-json",
                    "still-not-json",
                ]
            ),
            model="fake",
            registry=registry,
            parse_retries=1,
        )

        outcome = agent.run(
            "@时悠 记下这一整段复合设定",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertTrue(outcome.state_changed)
        self.assertEqual(outcome.reply, "这条设定记下了。")
        self.assertEqual(outcome.error, "")
        self.assertEqual(outcome.mode, "gm_agent_tool")

    def test_iteration_limit_after_write_never_exposes_protocol_recovery_text(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit_part",
                description="commit",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="commit_part",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="白蜡封片裂开，露出一截旧登记条。",
                ),
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "commit_part",
                            "arguments": {},
                            "reply": "",
                            "reason": "提交公开变化。",
                        },
                        ensure_ascii=False,
                    )
                ]
            ),
            model="fake",
            registry=registry,
            max_iterations=1,
        )

        outcome = agent.run(
            "系统主动节拍要求局面发生变化。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertTrue(outcome.state_changed)
        self.assertEqual(outcome.reply, "白蜡封片裂开，露出一截旧登记条。")
        self.assertNotIn("还没记下", outcome.reply)
        self.assertNotIn("没有完成", outcome.reply)

    def test_parse_failure_after_corrected_write_is_recovered(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit_part",
                description="commit",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="commit_part",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="最后一项技能记下了。",
                ),
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "不存在的包装工具",
                            "arguments": {},
                            "reply": "",
                            "reason": "协议包装错误。",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "commit_part",
                            "arguments": {},
                            "reply": "",
                            "reason": "根据失败回执改用正确工具。",
                        },
                        ensure_ascii=False,
                    ),
                    "not-json",
                    "still-not-json",
                ]
            ),
            model="fake",
            registry=registry,
            parse_retries=1,
        )

        outcome = agent.run(
            "洛岚最后一项技能选破防打击。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual([receipt.ok for receipt in outcome.receipts], [False, True])
        self.assertTrue(outcome.state_changed)
        self.assertEqual(outcome.reply, "最后一项技能记下了。")
        self.assertEqual(outcome.error, "")

    def test_successful_gate_receipt_refreshes_context_for_next_batch_tool(self) -> None:
        observed_gate_statuses: list[str] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="open_gate",
                description="open",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="open_gate",
                    ok=True,
                    result={"gate": {"campaign_id": "agent-test", "status": "adventure"}},
                    state_changed=True,
                    public_fallback_reply="第一章开始了。",
                ),
                side_effect="write",
            )
        )

        def start_scene(context, _arguments):
            observed_gate_statuses.append(context.gate_status)
            return GMToolReceipt(
                tool_name="start_scene",
                ok=context.gate_status == "adventure",
                state_changed=context.gate_status == "adventure",
                public_fallback_reply="风铃廊出现在眼前。",
            )

        registry.register(
            GMToolDefinition(
                name="start_scene",
                description="scene",
                handler=start_scene,
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tools",
                            "calls": [
                                {"tool_name": "open_gate", "arguments": {}},
                                {"tool_name": "start_scene", "arguments": {}},
                            ],
                            "terminal_decision": "final",
                            "reply": "第一章开始了，风铃廊出现在眼前。",
                            "reason": "依次进入冒险并建立场景。",
                        },
                        ensure_ascii=False,
                    )
                ]
            ),
            model="fake",
            registry=registry,
        )
        context = execution_context()
        context.gate_status = "session_zero"
        state_summary = {"runtime": {"gate": {"status": "session_zero"}}}

        outcome = agent.run(
            "大家确认进入第一章。",
            recent_context="",
            context=context,
            state_summary=state_summary,
        )

        self.assertEqual(observed_gate_statuses, ["adventure"])
        self.assertTrue(all(receipt.ok for receipt in outcome.receipts))
        self.assertEqual(context.gate_status, "adventure")
        self.assertEqual(state_summary["runtime"]["gate"]["status"], "adventure")

    def test_not_applicable_in_active_session_fails_closed_without_legacy_fallback(self) -> None:
        registry = GMToolRegistry()
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "not_applicable",
                            "tool_name": "",
                            "arguments": {},
                            "reply": "",
                            "reason": "这是角色行动，应交给跑团流程。",
                        },
                        ensure_ascii=False,
                    )
                ]
            ),
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "伊莉雅推开旧路闸门。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.mode, "gm_agent_unavailable")
        self.assertIn("没有记入或结算", outcome.reply)

    def test_not_applicable_in_active_group_exhaustion_stays_silent(self) -> None:
        context = execution_context()
        context.directly_addressed = False
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "not_applicable",
                            "reason": "错误地尝试交给旧流程。",
                        },
                        ensure_ascii=False,
                    )
                    for _ in range(2)
                ]
            ),
            model="fake",
            registry=GMToolRegistry(),
            max_iterations=2,
        )

        outcome = agent.run(
            "伊莉雅推开旧路闸门。",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.mode, "gm_agent_unresolved_silent")
        self.assertEqual(outcome.reply, "")
        self.assertTrue(outcome.stop_astrbot)

    def test_agent_can_authoritatively_keep_player_discussion_silent(self) -> None:
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "silent",
                        "audience": "players",
                        "tool_name": "",
                        "arguments": {},
                        "reply": "",
                        "reason": "玩家正在彼此商量分工，尚未声明行动。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=GMToolRegistry(),
        )

        context = execution_context()
        context.directly_addressed = False
        outcome = agent.run(
            "谁方便盯外面，谁继续和会长谈？",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.target, "silent")
        self.assertTrue(outcome.stop_astrbot)
        self.assertEqual(outcome.reply, "")
        system_prompt = client.calls[0]["messages"][0].content
        self.assertIn("玩家间闲聊和商量保持silent", system_prompt)
        self.assertIn("不能用来催流程", system_prompt)
        self.assertIn("登记由谁负责比较合适", system_prompt)
        self.assertIn("已经表演出来的角色行动", system_prompt)
        self.assertIn("已发生的角色行动降格为闲聊", system_prompt)

    def test_unaddressed_table_reply_is_returned_to_agent_for_silence(self) -> None:
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "final",
                        "audience": "players",
                        "reply": "由伊莉雅负责最合适。",
                        "reason": "玩家正在商量登记分工。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "silent",
                        "audience": "players",
                        "reply": "",
                        "reason": "这是玩家之间的分工讨论，GM不替他们决定。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=GMToolRegistry(),
        )
        context = execution_context()
        context.directly_addressed = False

        outcome = agent.run(
            "我赞成先登记；登记由谁来负责比较合适？",
            recent_context="伊莉雅已经走进登记小室。",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.reply, "")
        self.assertEqual(len(client.calls), 2)
        retry_history = client.calls[1]["messages"][1].content
        self.assertIn("UNADDRESSED_TABLE_TALK_SHOULD_STAY_SILENT", retry_history)

    def test_in_character_speech_to_another_pc_uses_scene_action_without_acting_for_them(
        self,
    ) -> None:
        calls: list[dict[str, object]] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="perform_in_scene_action",
                description=(
                    "记录当前PC已经说出口的角色内发言与个人承诺；"
                    "不得替听者回应、转告或行动。"
                ),
                handler=lambda _context, arguments: (
                    calls.append(dict(arguments))
                    or GMToolReceipt.success(
                        "perform_in_scene_action",
                        result={"silent_commit_allowed": True},
                        state_changed=True,
                    )
                ),
                parameters=(
                    GMToolParameter("actor", "string", "行动角色。", required=True),
                    GMToolParameter(
                        "action_summary",
                        "string",
                        "仅概括当前角色已经执行的言行。",
                        required=True,
                    ),
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "audience": "players",
                        "tool_name": "perform_in_scene_action",
                        "arguments": {
                            "actor": "苍祈",
                            "action_summary": "苍祈向艾薇娅承诺照看旅人，并请她转告会长",
                        },
                        "terminal_decision": "silent",
                        "reason": "这是已经发生的角色内发言，只提交苍祈自己的言行。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context()
        context.directly_addressed = False

        outcome = agent.run(
            (
                "苍祈压低声音对艾薇娅说：“我愿意承担失忆旅人的同行照看；"
                "若遇袭或旅人要求停下，我会立即撤回。请把这份承诺转告会长。”"
            ),
            recent_context="会长要求队伍报备护送人选。",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "silent")
        self.assertTrue(outcome.state_changed)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(calls[0]["actor"], "苍祈")
        self.assertNotIn("艾薇娅已转告", calls[0]["action_summary"])
        system_prompt = client.calls[0]["messages"][0].content
        self.assertIn("已经说出口的角色内发言与个人承诺", system_prompt)

    def test_silent_scene_pass_suppresses_model_paraphrase_after_receipt(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="pass_in_scene_action",
                description="记录普通场景行动轮中的明确略过。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "pass_in_scene_action",
                    result={"silent_commit_allowed": True},
                    state_changed=True,
                ),
                parameters=(
                    GMToolParameter("actor", "string", "行动角色。", required=True),
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "audience": "table",
                        "tool_name": "pass_in_scene_action",
                        "arguments": {"actor": "伊莉雅"},
                        "reason": "记录本轮略过。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "audience": "table",
                        "reply": "伊莉雅暂时让出本轮行动，安静守在原地。",
                        "reason": "复述玩家的略过。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context()
        context.directly_addressed = False

        outcome = agent.run(
            "伊莉雅暂时不采取行动。",
            recent_context="巡逻队刚刚退向闸门。",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.reply, "")
        self.assertEqual(outcome.mode, "gm_agent_silent_commit")
        self.assertTrue(outcome.state_changed)

    def test_directly_addressed_silent_capable_write_still_requires_reply(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="record_local_note",
                description="记录本地状态。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "record_local_note",
                    result={"silent_commit_allowed": True},
                    state_changed=True,
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "audience": "gm",
                        "tool_name": "record_local_note",
                        "arguments": {},
                        "terminal_decision": "silent",
                        "reason": "尝试静默。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "audience": "gm",
                        "reply": "记下了。",
                        "reason": "直接询问需要回应。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context()
        context.directly_addressed = True

        outcome = agent.run(
            "@时悠，记一下。",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "fu_gm")
        self.assertEqual(outcome.reply, "记下了。")

    def test_unaddressed_session_zero_table_proposal_is_silent_and_never_persisted(self) -> None:
        writes: list[dict[str, object]] = []

        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="propose_session_zero_update",
                description="仅在玩家直接要求GM暂存待定提案时使用。",
                handler=lambda _context, arguments: (
                    writes.append(dict(arguments))
                    or GMToolReceipt.success(
                        "propose_session_zero_update",
                        state_changed=True,
                    )
                ),
                parameters=(
                    GMToolParameter("summary", "string", "待定提案摘要。", required=True),
                ),
                side_effect="write_pending",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "silent",
                        "audience": "players",
                        "reason": "这是玩家向全桌征求意见，等其他玩家回应。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=registry,
        )
        context = execution_context()
        context.gate_status = "session_zero"
        context.directly_addressed = False

        outcome = agent.run(
            "第一幕我提议从白花碑驿站开始，大家觉得呢？",
            recent_context="",
            context=context,
            state_summary={"session_zero": {"pending_proposals": []}},
        )

        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.reply, "")
        self.assertEqual(writes, [])
        self.assertEqual(outcome.receipts, [])
        system_prompt = client.calls[0]["messages"][0].content
        self.assertIn("仍在问“大家觉得呢”时不写入", system_prompt)
        self.assertIn("玩家明确请GM暂存才建立待定提案", system_prompt)
        self.assertIn("不要求逐人投票", system_prompt)

    def test_recent_context_pronoun_address_to_gm_cannot_finish_silent(self) -> None:
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "silent",
                        "audience": "gm",
                        "reason": "错误地把这句当成玩家闲聊。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "audience": "gm",
                        "reply": "是有点坏，我都问他三回啦。",
                        "reason": "结合上一句可知“你”指时悠。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=GMToolRegistry())
        context = execution_context()
        context.directly_addressed = False

        outcome = agent.run(
            "他很坏啊都不理你",
            recent_context=(
                "时悠: loading，如果暂时没灵感，也可以只留下一件怪事；"
                "想不到的话，先跳过世界奥秘也可以。"
            ),
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "fu_gm")
        self.assertEqual(outcome.reply, "是有点坏，我都问他三回啦。")
        retry_request = json.loads(client.calls[1]["messages"][-1].content)
        self.assertEqual(
            retry_request["history"][-1]["protocol_error"]["error_code"],
            "SEMANTICALLY_ADDRESSED_MESSAGE_REQUIRES_REPLY",
        )
        self.assertIn("他很坏啊都不理你", client.calls[0]["messages"][0].content)

    def test_agent_can_answer_without_tool_when_no_state_changes(self) -> None:
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "final",
                            "tool_name": "",
                            "arguments": {},
                            "reply": "命刻用来追踪复杂目标或逐步逼近的威胁。",
                            "reason": "这是纯规则解释，不修改团状态。",
                        },
                        ensure_ascii=False,
                    )
                ]
            ),
            model="fake",
            registry=GMToolRegistry(),
        )

        outcome = agent.run(
            "@时悠，命刻是什么？",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.target, "fu_gm")
        self.assertEqual(outcome.mode, "gm_agent_reply")
        self.assertEqual(outcome.receipts, [])

    def test_agent_silences_an_exact_echo_of_a_non_addressed_player_pass(self) -> None:
        message = "伊莉雅暂时不采取行动。"
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "final",
                            "tool_name": "",
                            "arguments": {},
                            "reply": message,
                            "reason": "错误地复述玩家。",
                        },
                        ensure_ascii=False,
                    )
                ]
            ),
            model="fake",
            registry=GMToolRegistry(),
        )
        context = execution_context()
        context.directly_addressed = False

        outcome = agent.run(
            message,
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.reply, "")
        self.assertTrue(outcome.stop_astrbot)

    def test_platform_address_prevents_agent_silence(self) -> None:
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "silent",
                            "audience": "gm",
                            "tool_name": "",
                            "arguments": {},
                            "reply": "",
                            "reason": "误判为玩家闲聊。",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "decision": "final",
                            "tool_name": "",
                            "arguments": {},
                            "reply": "我在，刚才你是在问我。",
                            "reason": "平台确认玩家直接点名了时悠。",
                        },
                        ensure_ascii=False,
                    ),
                ]
            ),
            model="fake",
            registry=GMToolRegistry(),
        )

        outcome = agent.run(
            "@时悠，你在吗？",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.target, "fu_gm")
        self.assertIn("我在", outcome.reply)
        self.assertEqual(len(outcome.trace), 2)

    def test_failed_receipt_cannot_be_rewritten_as_success_by_model(self) -> None:
        def handler(_context, _arguments):
            return GMToolReceipt(
                tool_name="load_campaign",
                ok=False,
                error_code="SAVE_SLOT_NOT_FOUND",
                message="没有这个存档。",
                retryable=False,
                public_fallback_reply="没有找到这个存档，当前进度没有改动。",
            )

        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="load_campaign",
                description="load",
                handler=handler,
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "load_campaign",
                            "arguments": {},
                            "reply": "",
                            "reason": "尝试读取。",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "decision": "final",
                            "tool_name": "",
                            "arguments": {},
                            "reply": "读档成功。",
                            "reason": "错误地把失败当成成功。",
                        },
                        ensure_ascii=False,
                    ),
                ]
            ),
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "@时悠 读档",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertFalse(outcome.state_changed)
        self.assertNotIn("成功", outcome.reply)
        self.assertIn("没有改动", outcome.reply)

    def test_locked_public_reply_cannot_be_paraphrased_after_fact_commit(self) -> None:
        def handler(_context, _arguments):
            return GMToolReceipt(
                tool_name="commit_scene_response",
                ok=True,
                result={"public_facts": ["巡守把钥匙收回腰间。"]},
                state_changed=True,
                public_fallback_reply="巡守把钥匙收回腰间。",
                lock_public_reply=True,
            )

        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit_scene_response",
                description="commit scene facts and reply",
                handler=handler,
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "commit_scene_response",
                            "arguments": {},
                            "reply": "",
                            "reason": "提交公开场景回应。",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "decision": "final",
                            "tool_name": "",
                            "arguments": {},
                            "reply": "巡守已经把钥匙交给了英雄。",
                            "reason": "错误地改写了事实。",
                        },
                        ensure_ascii=False,
                    ),
                ]
            ),
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "我示意巡守收好钥匙。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.reply, "巡守把钥匙收回腰间。")
        self.assertNotIn("交给", outcome.reply)

    def test_multiple_successful_locked_replies_are_preserved_in_tool_order(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="decide_npc_response",
                description="npc",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="decide_npc_response",
                    ok=True,
                    result={"allowed_followup_tools": ["start_scene"]},
                    state_changed=True,
                    public_fallback_reply="会长点头：‘我带你们过去。’",
                    lock_public_reply=True,
                ),
            )
        )
        registry.register(
            GMToolDefinition(
                name="start_scene",
                description="scene",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="start_scene",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="一行人穿过门洞，抵达旧路第一处界碑。",
                    lock_public_reply=True,
                ),
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps({"decision": "call_tool", "tool_name": "decide_npc_response", "arguments": {}}),
                    json.dumps({"decision": "call_tool", "tool_name": "start_scene", "arguments": {}}),
                    json.dumps({"decision": "final", "tool_name": "", "arguments": {}, "reply": "已经出发。"}),
                ]
            ),
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "请会长带我们去旧路界碑。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(
            outcome.reply,
            "会长点头：‘我带你们过去。’\n一行人穿过门洞，抵达旧路第一处界碑。",
        )
        self.assertEqual([item.tool_name for item in outcome.receipts], ["decide_npc_response", "start_scene"])

    def test_adventure_gate_must_continue_to_typed_scene_opening(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="start_session",
                description="gate",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="start_session",
                    ok=True,
                    result={
                        "adventure_opening_required": True,
                        "allowed_followup_tools": ["start_scene"],
                        "required_followup_tools": ["start_scene"],
                    },
                    state_changed=True,
                    public_fallback_reply="",
                    lock_public_reply=True,
                ),
            )
        )
        registry.register(
            GMToolDefinition(
                name="start_scene",
                description="scene",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="start_scene",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="潮雾压低风铃声，失忆旅人站在会长面前等候去路。",
                    lock_public_reply=True,
                ),
            )
        )
        client = ScriptedClient(
            [
                json.dumps({"decision": "call_tool", "tool_name": "start_session", "arguments": {}}),
                json.dumps({"decision": "call_tool", "tool_name": "start_scene", "arguments": {}}),
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "大家同意进入第一章，请先描述现场。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(
            outcome.reply,
            "潮雾压低风铃声，失忆旅人站在会长面前等候去路。",
        )
        self.assertEqual(
            [item.tool_name for item in outcome.receipts],
            ["start_session", "start_scene"],
        )
        second_request = json.loads(client.calls[1]["messages"][1].content)
        self.assertEqual(
            [tool["name"] for tool in second_request["available_tools"]],
            ["start_scene"],
        )

    def test_nonpublic_scene_focus_cannot_end_before_required_action(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="focus_scene_branch",
                description="focus",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="focus_scene_branch",
                    ok=True,
                    result={
                        "required_followup_tools": ["move_scene_group"],
                        "allowed_followup_tools": ["move_scene_group"],
                    },
                    state_changed=True,
                ),
            )
        )
        registry.register(
            GMToolDefinition(
                name="move_scene_group",
                description="move",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="move_scene_group",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="赛璃与失忆旅人抵达登记小室。",
                    lock_public_reply=True,
                ),
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "focus_scene_branch",
                        "arguments": {},
                    }
                ),
                json.dumps(
                    {
                        "decision": "silent",
                        "tool_name": "",
                        "arguments": {},
                        "reason": "镜头准备本身不公开。",
                    }
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "move_scene_group",
                        "arguments": {},
                    }
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "赛璃牵着失忆旅人进入登记小室。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(outcome.reply, "赛璃与失忆旅人抵达登记小室。")
        self.assertEqual(
            [item.tool_name for item in outcome.receipts],
            ["focus_scene_branch", "move_scene_group"],
        )

    def test_session_zero_opening_is_owned_by_core_gm_after_gate_receipt(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="start_session",
                description="gate",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="start_session",
                    ok=True,
                    result={
                        "session_zero_opening_required": True,
                        "opening_instruction": "请开始第零章，先聊基调和安全边界。",
                    },
                    state_changed=True,
                ),
            )
        )
        client = ScriptedClient(
            [
                json.dumps({"decision": "call_tool", "tool_name": "start_session", "arguments": {}}),
                json.dumps(
                    {
                        "decision": "final",
                        "reply": "好，我们先聊基调和安全边界。大家希望故事整体是什么感觉，又有哪些内容不希望出现或只想淡出处理？",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=registry,
            max_iterations=3,
        )

        outcome = agent.run(
            "大家准备好了，请开始第零章，先聊基调和安全边界。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(
            outcome.reply,
            "好，我们先聊基调和安全边界。大家希望故事整体是什么感觉，又有哪些内容不希望出现或只想淡出处理？",
        )
        self.assertEqual([item.tool_name for item in outcome.receipts], ["start_session"])

    def test_malformed_required_followup_returns_to_full_agent_loop(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="start_session",
                description="gate",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="start_session",
                    ok=True,
                    result={
                        "allowed_followup_tools": ["start_scene"],
                        "required_followup_tools": ["start_scene"],
                    },
                    state_changed=True,
                    lock_public_reply=True,
                ),
            )
        )
        registry.register(
            GMToolDefinition(
                name="start_scene",
                description="scene",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="start_scene",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="潮雾压着风铃廊，失忆旅人正在门边等候。",
                    lock_public_reply=True,
                ),
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "start_session",
                        "arguments": {},
                    }
                ),
                '{"decision":"call_tool","tool_name":"start_scene","arguments":{',
                '{"decision":"call_tool","tool_name":"start_scene","arguments":',
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "start_scene",
                        "arguments": {},
                    }
                ),
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="semantic-model",
            protocol_repair_model="syntax-model",
            registry=registry,
            parse_retries=1,
            max_iterations=4,
        )

        outcome = agent.run(
            "大家同意进入第一章，请描述现场。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(
            outcome.reply,
            "潮雾压着风铃廊，失忆旅人正在门边等候。",
        )
        self.assertEqual(
            [item.tool_name for item in outcome.receipts],
            ["start_session", "start_scene"],
        )
        self.assertTrue(
            any(
                item.get("phase") == "decision_protocol_returned_to_agent"
                for item in outcome.trace
            )
        )
        resumed_request = json.loads(client.calls[3]["messages"][1].content)
        self.assertEqual(
            [tool["name"] for tool in resumed_request["available_tools"]],
            ["start_scene"],
        )
        self.assertEqual(
            resumed_request["history"][-1]["protocol_error"]["error_code"],
            "INVALID_AGENT_TOOL_PROTOCOL",
        )

    def test_pending_followup_is_not_a_recovered_complete_state_change(self) -> None:
        receipts = [
            GMToolReceipt(
                tool_name="start_session",
                ok=True,
                result={
                    "allowed_followup_tools": ["start_scene"],
                    "required_followup_tools": ["start_scene"],
                },
                state_changed=True,
                lock_public_reply=True,
            )
        ]

        self.assertFalse(GMToolReceiptPolicy.state_change_recovered(receipts))

    def test_required_scene_followup_rejects_premature_final(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="start_session",
                description="gate",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="start_session",
                    ok=True,
                    result={
                        "allowed_followup_tools": ["start_scene"],
                        "required_followup_tools": ["start_scene"],
                    },
                    state_changed=True,
                    lock_public_reply=True,
                ),
            )
        )
        registry.register(
            GMToolDefinition(
                name="start_scene",
                description="scene",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="start_scene",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="风铃廊在潮雾中显出轮廓。",
                    lock_public_reply=True,
                ),
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps({"decision": "call_tool", "tool_name": "start_session", "arguments": {}}),
                    json.dumps({"decision": "final", "reply": "第一章开始。"}),
                    json.dumps({"decision": "call_tool", "tool_name": "start_scene", "arguments": {}}),
                ]
            ),
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "大家同意进入第一章，请描述现场。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(outcome.reply, "风铃廊在潮雾中显出轮廓。")
        self.assertEqual(
            [item.tool_name for item in outcome.receipts],
            ["start_session", "start_scene"],
        )
        self.assertEqual(len(agent.client.calls), 3)

    def test_npc_followup_grant_rejects_unrelated_second_tool(self) -> None:
        registry = GMToolRegistry()
        executed: list[str] = []

        registry.register(
            GMToolDefinition(
                name="decide_npc_response",
                description="npc",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="decide_npc_response",
                    ok=True,
                    result={"allowed_followup_tools": ["start_scene"]},
                    state_changed=True,
                    public_fallback_reply="会长答应带路。",
                    lock_public_reply=True,
                ),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )
        registry.register(
            GMToolDefinition(
                name="change_clock",
                description="clock",
                handler=lambda _context, _arguments: (
                    executed.append("change_clock")
                    or GMToolReceipt.success("change_clock", state_changed=True)
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "decide_npc_response",
                        "arguments": {},
                    }
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "change_clock",
                        "arguments": {},
                    }
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "请会长带路。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(outcome.reply, "会长答应带路。")
        self.assertEqual(executed, [])
        self.assertEqual(outcome.trace[-1]["protocol_error"], "PUBLIC_RECEIPT_FOLLOWUP_NOT_ALLOWED")

    def test_primary_rules_action_stops_later_actions_from_the_same_message(self) -> None:
        registry = GMToolRegistry()
        executed: list[str] = []

        def locked_receipt(name: str, reply: str):
            return lambda _context, _arguments: (
                executed.append(name)
                or GMToolReceipt(
                    tool_name=name,
                    ok=True,
                    state_changed=True,
                    public_fallback_reply=reply,
                    lock_public_reply=True,
                )
            )

        registry.register(
            GMToolDefinition(
                name="perform_check_action",
                description="check",
                handler=locked_receipt("perform_check_action", "伊莉雅完成了调查。"),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="perform_character_action",
                description="guard",
                handler=locked_receipt("perform_character_action", "伊莉雅随后进入防御。"),
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tools",
                            "calls": [
                                {"tool_name": "perform_check_action", "arguments": {}},
                                {"tool_name": "perform_character_action", "arguments": {}},
                            ],
                            "reason": "错误地把同一句站位和观察拆成两个规则动作。",
                        },
                        ensure_ascii=False,
                    )
                ]
            ),
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "伊莉雅挡在旅人身前，观察门外是否有追兵。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(executed, ["perform_check_action"])
        self.assertEqual(outcome.reply, "伊莉雅完成了调查。")
        self.assertEqual(len(outcome.receipts), 1)

    def test_gm_owned_fumble_window_can_finish_the_same_rules_transaction(self) -> None:
        registry = GMToolRegistry()
        executed: list[str] = []

        registry.register(
            GMToolDefinition(
                name="perform_check_action",
                description="check",
                handler=lambda _context, _arguments: (
                    executed.append("check")
                    or GMToolReceipt(
                        tool_name="perform_check_action",
                        ok=True,
                        state_changed=True,
                        result={
                            "pending_decisions": [
                                {
                                    "window_id": "fumble-1",
                                    "kind": "fumble_opportunity",
                                    "owner": "__gm__",
                                }
                            ]
                        },
                        public_fallback_reply="检定掷出了大失败。",
                        lock_public_reply=True,
                    )
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="resolve_gm_opportunity",
                description="resolve",
                handler=lambda _context, _arguments: (
                    executed.append("resolve")
                    or GMToolReceipt(
                        tool_name="resolve_gm_opportunity",
                        ok=True,
                        state_changed=True,
                        public_fallback_reply="GM把机会用于制造新的危险。",
                        lock_public_reply=True,
                    )
                ),
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "perform_check_action",
                            "arguments": {},
                        }
                    ),
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "resolve_gm_opportunity",
                            "arguments": {},
                        }
                    ),
                ]
            ),
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "伊莉雅检查闸门机关。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(executed, ["check", "resolve"])
        self.assertEqual(
            outcome.reply,
            "检定掷出了大失败。\nGM把机会用于制造新的危险。",
        )
        self.assertEqual(len(outcome.receipts), 2)

    def test_required_material_heartbeat_rejects_silence_until_write_receipt(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit_scene_response",
                description="commit",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="commit_scene_response",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="闸门外的骑手落地，封住了北侧出口。",
                    lock_public_reply=True,
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="save_campaign",
                description="unrelated",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="save_campaign", ok=True, state_changed=True
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps({"decision": "silent", "reason": "错误地保持静默。"}),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "commit_scene_response",
                        "arguments": {},
                        "reason": "提交可见变化。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {"decision": "final", "reply": "不应覆盖锁定回复。"},
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry, max_iterations=4)
        context = execution_context()
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "free_scene_beat",
                "heartbeat_require_material_change": True,
            }
        )

        outcome = agent.run(
            "系统要求推进当前局面。",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertTrue(outcome.state_changed)
        self.assertEqual(outcome.reply, "闸门外的骑手落地，封住了北侧出口。")
        first_request = json.loads(client.calls[0]["messages"][-1].content)
        tool_names = {item["name"] for item in first_request["available_tools"]}
        self.assertEqual(tool_names, {"commit_scene_response"})
        system_prompt = client.calls[0]["messages"][0].content
        self.assertIn("主动节拍决策层", system_prompt)
        self.assertNotIn("查看“我的角色草稿”", system_prompt)
        self.assertEqual(outcome.trace[1]["arguments"], {})
        self.assertEqual(len(client.calls), 2)

    def test_heartbeat_batch_stops_after_first_public_material_change(self) -> None:
        registry = GMToolRegistry()
        executed: list[str] = []

        def commit(_context, arguments):
            marker = str(arguments.get("marker") or "")
            executed.append(marker)
            return GMToolReceipt(
                tool_name="commit_scene_response",
                ok=True,
                state_changed=True,
                public_fallback_reply=marker,
                lock_public_reply=True,
            )

        registry.register(
            GMToolDefinition(
                name="commit_scene_response",
                description="commit",
                handler=commit,
                parameters=(
                    GMToolParameter("marker", "string", "公开变化", required=True),
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tools",
                        "calls": [
                            {"tool_name": "commit_scene_response", "arguments": {"marker": "第一拍"}},
                            {"tool_name": "commit_scene_response", "arguments": {"marker": "不应执行"}},
                        ],
                        "reason": "错误地试图连续推进。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context()
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "free_scene_beat",
                "heartbeat_require_material_change": True,
            }
        )

        outcome = agent.run(
            "系统要求推进当前局面。",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertEqual(executed, ["第一拍"])
        self.assertEqual(outcome.reply, "第一拍")
        self.assertEqual(len(outcome.receipts), 1)

    def test_batch_executes_identical_read_call_only_once(self) -> None:
        registry = GMToolRegistry()
        calls: list[str] = []

        def read(_context, _arguments):
            calls.append("read")
            return GMToolReceipt.success(
                "search_rule_references",
                result={"name": "谴责"},
            )

        registry.register(
            GMToolDefinition(
                name="search_rule_references",
                description="search",
                handler=read,
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tools",
                        "calls": [
                            {
                                "tool_name": "search_rule_references",
                                "arguments": {},
                            },
                            {
                                "tool_name": "search_rule_references",
                                "arguments": {},
                            },
                        ],
                        "terminal_decision": "final",
                        "reply": "【谴责】是游说家技能。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "谴责是什么技能？",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(calls, ["read"])
        self.assertEqual(len(outcome.receipts), 1)
        self.assertEqual(outcome.reply, "【谴责】是游说家技能。")
        self.assertEqual(
            outcome.trace[0]["skipped_duplicate_calls"][0]["batch_index"],
            2,
        )

    def test_npc_profile_tool_schema_exposes_allowed_nested_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            schema = next(
                item
                for item in service.gm_tool_registry.schemas()
                if item["name"] == "create_npc_profile"
            )

        parameters = schema["parameters"]
        self.assertIn("present_in_scene", parameters["required"])
        profile = parameters["properties"]["profile"]
        self.assertFalse(profile["additionalProperties"])
        self.assertIn("active_goal", profile["properties"])
        self.assertNotIn("current_location", profile["properties"])

    def test_free_scene_heartbeat_exposes_atomic_npc_introduction_not_profile_only_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            agent = LLMGMToolAgent(
                ScriptedClient([]),
                model="fake",
                registry=service.gm_tool_registry,
            )
            context = execution_context()
            context.metadata.update(
                {
                    "system_gm_beat_request": True,
                    "heartbeat_action": "free_scene_beat",
                }
            )

            names = {
                item["name"] for item in agent._available_tool_schemas(context)
            }

        self.assertIn("introduce_npc", names)
        self.assertNotIn("create_npc_profile", names)

    def test_scene_opening_heartbeat_exposes_only_atomic_scene_publication_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            agent = LLMGMToolAgent(
                ScriptedClient([]),
                model="fake",
                registry=service.gm_tool_registry,
            )
            context = execution_context()
            context.metadata.update(
                {
                    "system_gm_beat_request": True,
                    "heartbeat_action": "scene_opening",
                    "heartbeat_require_material_change": True,
                }
            )

            names = {item["name"] for item in agent._available_tool_schemas(context)}

        self.assertIn("start_scene", names)
        self.assertIn("commit_scene_response", names)
        self.assertIn("introduce_npc", names)
        self.assertIn("create_clock", names)
        self.assertIn("get_gameplay_state", names)
        self.assertIn("preview_npc_combatant", names)
        self.assertIn("create_npc_combatant", names)
        self.assertIn("configure_boss_phases", names)
        self.assertNotIn("save_campaign", names)


    def test_agent_refreshes_authoritative_state_after_each_tool_call(self) -> None:
        state = {"value": 0}
        registry = GMToolRegistry()

        def increment(_context, _arguments):
            state["value"] += 1
            return GMToolReceipt(
                tool_name="increment",
                ok=True,
                state_changed=True,
                result={"value": state["value"]},
            )

        registry.register(
            GMToolDefinition(
                name="increment",
                description="increment state",
                handler=increment,
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "increment",
                        "arguments": {},
                        "reply": "",
                        "reason": "change state",
                    }
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "tool_name": "",
                        "arguments": {},
                        "reply": "updated",
                        "reason": "observed committed state",
                    }
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "update it",
            recent_context="",
            context=execution_context(),
            state_summary={"value": 0},
            state_summary_provider=lambda: {"value": state["value"]},
        )

        self.assertEqual(state["value"], 1)
        self.assertEqual(outcome.reply, "updated")
        second_request = json.loads(client.calls[1]["messages"][-1].content)
        self.assertEqual(second_request["current_state_summary"]["value"], 1)


class FUGMToolHandlerTests(unittest.TestCase):
    def test_successful_agent_load_exposes_backend_confirmed_campaign_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            service._save_campaign({"campaign_id": "旧团"})
            service._save_campaign({"campaign_id": "当前团"})
            service.session_gates.activate(
                "当前团",
                "group-1",
                "s1",
                status="adventure",
            )
            service.gm_tool_agent = LLMGMToolAgent(
                ScriptedClient(
                    [
                        json.dumps(
                            {
                                "decision": "call_tool",
                                "tool_name": "discover_capabilities",
                                "arguments": {
                                    "domains": ["campaign"],
                                    "reason": "玩家要求读取另一个战役。",
                                },
                                "reply": "",
                                "reason": "先取得存读档能力。",
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "decision": "call_tool",
                                "tool_name": "load_campaign",
                                "arguments": {"campaign_id": "旧团"},
                                "reply": "",
                                "reason": "玩家明确选择旧团。",
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "decision": "final",
                                "tool_name": "",
                                "arguments": {},
                                "reply": "已经读回《旧团》。",
                                "reason": "读取成功。",
                            },
                            ensure_ascii=False,
                        ),
                    ]
                ),
                model="fake",
                registry=service.gm_tool_registry,
            )

            status, response = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "当前团",
                    "session_id": "s1",
                    "channel_id": "group-1",
                    "speaker": "阿凛",
                    "message": "@时悠 读取旧团",
                    "is_at_bot": True,
                },
            )

            self.assertEqual(status, 200)
            self.assertEqual(response["campaign_id"], "当前团")
            self.assertEqual(response["active_campaign_id"], "旧团")
            self.assertEqual(service._current_campaign_id(), "旧团")

            dashboard_status, dashboard = service.handle(
                "GET",
                "/v1/audit/dashboard?campaign_id=%E5%BD%93%E5%89%8D%E5%9B%A2&session_id=s1",
            )
            self.assertEqual(dashboard_status, 200)
            event = dashboard["gm_tools"]["recent_events"][-1]
            self.assertTrue(event["state_changed"])
            load_receipt = next(
                item
                for item in event["receipts"]
                if item["tool_name"] == "load_campaign"
            )
            self.assertTrue(load_receipt["ok"])

    def test_ambiguous_slot_returns_structured_error_without_loading(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            service._save_campaign({"campaign_id": "A", "slot": "共同槽"})
            service._save_campaign({"campaign_id": "B", "slot": "共同槽"})
            service._mark_current_campaign("A")

            receipt = service.gm_campaign_tools.load_campaign(
                execution_context(campaign_id="A"),
                {"slot": "共同槽"},
            )

            self.assertFalse(receipt.ok)
            self.assertEqual(receipt.error_code, "AMBIGUOUS_SAVE_SLOT")
            self.assertEqual(receipt.result["matching_campaigns"], ["A", "B"])
            self.assertEqual(service._current_campaign_id(), "A")

    def test_unknown_save_target_does_not_create_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            service._runtime("当前团")

            receipt = service.gm_campaign_tools.save_campaign(
                execution_context(campaign_id="当前团"),
                {"campaign_id": "模型猜出来的团", "slot": "误存"},
            )

            self.assertFalse(receipt.ok)
            self.assertEqual(receipt.error_code, "UNKNOWN_CAMPAIGN")
            self.assertFalse(service._memory_store().snapshot_exists("模型猜出来的团"))

    def test_hero_draft_tool_keeps_player_and_hero_names_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            runtime = service._runtime("角色团")
            runtime.app.world_state.world_profile.hero_drafts["loading"] = HeroDraft(
                player_name="loading",
                hero_name="艾丽妮",
                identity="失忆的钟匠学徒",
            )

            receipt = service.gm_campaign_tools.get_hero_drafts(
                execution_context(campaign_id="角色团", speaker="loading"),
                {"scope": "mine"},
            )

            self.assertTrue(receipt.ok)
            record = receipt.result["drafts"][0]
            self.assertEqual(record["player_name"], "loading")
            self.assertEqual(record["hero_name"], "艾丽妮")


if __name__ == "__main__":
    unittest.main()
