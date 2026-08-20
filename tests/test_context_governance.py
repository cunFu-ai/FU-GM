import json

from fu_gm.context_governance import (
    GMContextBudget,
    GMContextGovernor,
    GMToolResultBudgeter,
)
from fu_gm.components.gm_turn_state_delta import (
    apply_state_delta,
    projection_hash,
)
from fu_gm.gm_tool_agent import LLMGMToolAgent
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolReceipt,
    GMToolRegistry,
)


class _ScriptedClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def create_chat_completion(self, **kwargs: object) -> str:
        self.calls.append(dict(kwargs))
        return self.responses.pop(0)


def _execution_context() -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="campaign",
        session_id="session",
        channel_id="group",
        speaker="阿凛",
        gate_status="adventure",
        directly_addressed=True,
        metadata={"_gm_campaign_observed_version": 23},
    )


def _large_schema_agent() -> LLMGMToolAgent:
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="get_world_state",
            description="稳定工具说明" * 8_000,
            handler=lambda _context, _arguments: GMToolReceipt.success(
                "get_world_state",
                result={},
            ),
        )
    )
    return LLMGMToolAgent(
        _ScriptedClient([]),
        model="test",
        registry=registry,
    )


def _runtime_feedback_near_hard_limit() -> dict:
    feedback = {
        "scope": "current_transaction",
        "phase": "building_context",
        "budget": {
            "status": "near_limit",
            "iteration": 7,
            "max_iterations": 8,
            "remaining_iterations": 2,
            "elapsed_ms": 87123,
            "timeout_ms": 90000,
            "remaining_ms": 2877,
        },
        "transaction": {"status": "uncommitted"},
        "issues": [
            {
                "code": "TOOL_RETRY_REQUIRED",
                "phase": "validating_receipt",
                "severity": "warning",
                "retryable": True,
                "tool_name": f"tool_{index}_" + "x" * 88,
                "correction_hint": (
                    "请依据权威工具回执修正参数后重试。" + "诊断" * 235
                ),
                "recovery_action": "retry_tool_with_correction",
            }
            for index in range(5)
        ],
    }
    serialized = json.dumps(
        {"runtime_feedback": feedback},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert 4000 <= len(serialized) <= 4096
    return feedback


def test_tool_result_budget_keeps_transaction_fields_lossless() -> None:
    required_calls = [
        {
            "tool_name": "resolve_rule_window",
            "arguments": {"window_id": "window-1", "actor": "诺艾尔"},
        }
    ]
    projection = GMToolResultBudgeter.project(
        {
            "window_id": "window-1",
            "required_followup_tools": ["resolve_rule_window"],
            "required_followup_calls": required_calls,
            "large_private_catalog": ["旧资料" * 400 for _ in range(20)],
        },
        max_chars=1000,
    )

    assert projection.result["window_id"] == "window-1"
    assert projection.result["required_followup_calls"] == required_calls
    assert "large_private_catalog" in projection.omitted_keys
    assert projection.result["_fu_gm_model_view"][
        "full_receipt_retained_by_host"
    ] is True
    assert projection.projected_chars <= 1000


def test_governor_keeps_quoted_message_outside_recent_tail() -> None:
    recent = [
        {"message_id": f"m-{index}", "speaker": "玩家", "text": str(index)}
        for index in range(12)
    ]
    request = {
        "current_message": "我说的是前面那条。",
        "current_turn": {"events": [{"message_id": "m-current", "text": "当前"}]},
        "session": {"campaign_id": "c", "gate_status": "adventure"},
        "request_context": {
            "quoted_message": {"message_id": "m-1", "text": "被引用的旧消息"}
        },
        "current_state_summary": {},
        "available_tools": [],
        "recent_messages": recent,
        "history": [],
    }
    governor = GMContextGovernor(
        GMContextBudget(recent_message_limit=4)
    )

    governed = governor.govern(request)
    ids = [row["message_id"] for row in governed.request["recent_messages"]]

    assert ids == ["m-1", "m-8", "m-9", "m-10", "m-11"]
    assert governed.manifest.omitted["recent_messages"] == 7


def test_history_microcompact_preserves_failure_and_pending_followup() -> None:
    history = [
        {
            "model_decision": {
                "decision": "call_tool",
                "tool_name": f"tool-{index}",
                "arguments": {"actor": "诺艾尔", "verbose": "参数" * 300},
            },
            "tool_receipt": {
                "tool_name": f"tool-{index}",
                "ok": True,
                "state_changed": True,
                "result": {"summary": "结果" * 300},
            },
        }
        for index in range(7)
    ]
    history.insert(
        1,
        {
            "model_decision": {"tool_name": "failed-tool"},
            "tool_receipt": {
                "tool_name": "failed-tool",
                "ok": False,
                "error_code": "INVALID_ARGUMENTS",
                "correction_hint": "必须保留这条精确修正。",
                "result": {},
            },
        },
    )
    history.insert(
        2,
        {
            "model_decision": {"tool_name": "window-tool"},
            "tool_receipt": {
                "tool_name": "window-tool",
                "ok": True,
                "state_changed": True,
                "result": {
                    "required_followup_tools": ["resolve_rule_window"],
                    "window_id": "window-1",
                },
            },
        },
    )
    governor = GMContextGovernor(
        GMContextBudget(history_limit=4, full_history_entries=1)
    )

    compacted, omitted, compacted_count = governor._microcompact_history(history)

    assert omitted > 0
    assert compacted_count > 0
    assert any(
        item.get("tool_receipt", {}).get("correction_hint")
        == "必须保留这条精确修正。"
        for item in compacted
    )
    assert any(
        item.get("tool_receipt", {}).get("result", {}).get("window_id")
        == "window-1"
        for item in compacted
    )


def test_proactive_compaction_restores_protected_kernel_exactly() -> None:
    current_turn = {
        "turn_id": "turn-9",
        "events": [
            {
                "event_id": f"event-{index}",
                "speaker": f"玩家{index}",
                "text": "这是不能被截断的当前行动。" * 5,
            }
            for index in range(4)
        ],
    }
    active_clock = {
        "name": "财团巡逻队逼近",
        "current": 5,
        "max_segments": 6,
        "auto_advance_timing": "round_end",
    }
    runtime_feedback = _runtime_feedback_near_hard_limit()
    request = {
        "available_tools": [
            {"name": "perform_action", "description": "工具说明" * 80}
        ],
        "current_state_summary": {
            "gate_status": "adventure",
            "runtime": {"active_actor": "赛璃", "turn_serial": 9},
            "processes": {
                "scene": {
                    "action_round": {
                        "expected_actor": "赛璃",
                        "out_of_turn_inbox": ["伊莉雅的防御行动"],
                    }
                }
            },
            "clocks": {"active": [active_clock]},
            "gameplay": {
                "pending_decisions": [
                    {"window_id": "window-1", "responder": "赛璃"}
                ]
            },
            "scene": {
                "scene_id": "scene-1",
                "public_facts": ["闸门仍然关闭。"],
                "private_situation": {"opposition_goal": "夺回旅人"},
                "old_planning_noise": "旧计划" * 3000,
            },
            "npcs": {
                "present_npcs": [
                    {"name": "监察官艾蕾娜", "active_goal": "拖住英雄"}
                ]
            },
            **{f"irrelevant_{index}": "旧状态" * 600 for index in range(20)},
        },
        "current_message": "赛璃现在要擦除命刻。",
        "current_turn": current_turn,
        "recent_messages": [],
        "session": {
            "campaign_id": "campaign",
            "session_id": "session",
            "speaker": "南星",
            "gate_status": "adventure",
        },
        "request_context": {
            "quoted_message": {"message_id": "m-1", "text": "上一条GM询问"}
        },
        "runtime_feedback": runtime_feedback,
        "history": [],
    }
    governor = GMContextGovernor(
        GMContextBudget(
            warning_chars=5000,
            proactive_compaction_chars=6000,
            hard_chars=16000,
            target_chars=8000,
        )
    )

    governed = governor.govern(request, state_version=17)
    restored = json.loads(governed.rendered)

    assert restored["current_message"] == request["current_message"]
    assert restored["current_turn"] == current_turn
    assert restored["request_context"] == request["request_context"]
    assert restored["runtime_feedback"] == runtime_feedback
    state = restored["current_state_summary"]
    assert state["runtime"] == request["current_state_summary"]["runtime"]
    assert state["processes"] == request["current_state_summary"]["processes"]
    assert state["clocks"]["active"] == [active_clock]
    assert state["gameplay"]["pending_decisions"][0]["window_id"] == "window-1"
    assert state["scene"]["public_facts"] == ["闸门仍然关闭。"]
    assert state["scene"]["private_situation"]["opposition_goal"] == "夺回旅人"
    assert state["npcs"]["present_npcs"][0]["name"] == "监察官艾蕾娜"
    assert governed.manifest.state_version == 17
    assert governed.manifest.strategy != ("unchanged",)
    assert governed.manifest.model_view_only is True
    assert "runtime_feedback" in governed.manifest.protected_paths


def test_large_compacted_agent_requests_keep_stable_cache_prefixes() -> None:
    agent = _large_schema_agent()
    stable_state = {
        "gate_status": "adventure",
        "runtime": {"turn_serial": 7},
        "scene": {"scene_id": "scene-1", "public_facts": ["闸门关闭。"]},
    }

    def build(message: str, state: dict[str, object]):
        context = _execution_context()
        user_message = agent._build_decision_messages(
            current_message=message,
            recent_context="",
            context=context,
            observed_state=state,
            receipts=[],
            history=[],
        )[1]
        return user_message, dict(context.metadata["_gm_context_manifest"])

    first, first_manifest = build("我检查闸门。", stable_state)
    second, second_manifest = build("我等待守卫。", stable_state)
    changed, changed_manifest = build(
        "我检查闸门。",
        {
            **stable_state,
            "runtime": {"turn_serial": 8},
        },
    )

    for manifest in (first_manifest, second_manifest, changed_manifest):
        assert manifest["original_chars"] > 40_000
        assert any(
            "structured" in str(strategy)
            for strategy in manifest["strategy"]
        )

    first_tool_end, first_state_end = first.cache_breakpoint_offsets
    second_tool_end, second_state_end = second.cache_breakpoint_offsets
    changed_tool_end, changed_state_end = changed.cache_breakpoint_offsets

    # Different player prose cannot poison the stable layout + tool prefix.
    assert first.content[:first_tool_end] == second.content[:second_tool_end]
    assert first.content[:first_tool_end] == changed.content[:changed_tool_end]
    # An unchanged state extends the reusable prefix through the state block.
    assert first.content[:first_state_end] == second.content[:second_state_end]
    # A changed state invalidates the state boundary, while the tool boundary
    # remains reusable.
    assert first.content[:first_state_end] != changed.content[:changed_state_end]

    root_keys = list(json.loads(first.content))
    assert root_keys[:4] == [
        "prompt_layout_version",
        "available_tools",
        "current_state_summary",
        "current_message",
    ]
    assert root_keys.index("_fu_gm_context_compaction") > root_keys.index(
        "history"
    )
    assert '"current_state_summary"' not in first.content[:first_tool_end]
    assert '"current_message"' not in first.content[:first_state_end]


def test_large_compacted_delta_request_keeps_exact_hash_roots_and_boundaries() -> None:
    agent = _large_schema_agent()
    state_summary = {
        "gate_status": "adventure",
        "runtime": {"turn_serial": 7},
        "scene": {"scene_id": "scene-1", "current_pressure": "脚步声逼近"},
    }
    delta_ops = [
        {
            "sequence": 1,
            "op": "replace",
            "path": "/scene/current_pressure",
            "source_tool": "advance_pressure",
            "value": "守卫抵达门外",
        }
    ]
    effective = apply_state_delta(state_summary, delta_ops)
    turn_state_delta = {
        "base_revision": 42,
        "projection_version": "authority-v1",
        "base_hash": projection_hash(state_summary),
        "effective_hash": projection_hash(effective),
        "mutation_sequence": 1,
        "ops": delta_ops,
        "reset_reason": None,
    }
    context = _execution_context()

    user_message = agent._build_decision_messages(
        current_message="我继续观察牢门。",
        recent_context="",
        context=context,
        observed_state=effective,
        prompt_state_summary=state_summary,
        turn_state_delta=turn_state_delta,
        receipts=[],
        history=[],
    )[1]
    request = json.loads(user_message.content)
    manifest = dict(context.metadata["_gm_context_manifest"])

    assert manifest["original_chars"] > 40_000
    assert any("structured" in str(item) for item in manifest["strategy"])
    assert request["current_state_summary"] == state_summary
    assert request["turn_state_delta"] == turn_state_delta
    assert projection_hash(request["current_state_summary"]) == turn_state_delta[
        "base_hash"
    ]
    assert projection_hash(
        apply_state_delta(
            request["current_state_summary"],
            request["turn_state_delta"]["ops"],
        )
    ) == turn_state_delta["effective_hash"]

    tool_end, state_end = user_message.cache_breakpoint_offsets
    assert '"available_tools"' in user_message.content[:tool_end]
    assert '"current_state_summary"' not in user_message.content[:tool_end]
    assert '"current_state_summary"' in user_message.content[tool_end:state_end]
    assert '"turn_state_delta"' not in user_message.content[:state_end]
    assert user_message.content[state_end:].startswith('"turn_state_delta"')


def test_summary_delta_roots_remain_byte_exact_through_compaction() -> None:
    state_summary = {
        "gate_status": "adventure",
        "scene": {
            "scene_id": "scene-1",
            "public_facts": ["牢门关闭", "守卫正在接近"],
            "current_pressure": "脚步声逼近",
        },
        # This field would normally be shortened or omitted by the structured
        # compactor.  In delta mode it is covered by base_hash and must remain
        # byte-for-byte identical.
        "projected_details": {
            f"fact-{index}": "不可改写的投影内容" * 80
            for index in range(20)
        },
    }
    delta_ops = [
        {
            "sequence": 3,
            "op": "replace",
            "path": "/scene/current_pressure",
            "source_tool": "advance_pressure",
            "value": "守卫抵达门外",
        }
    ]
    turn_state_delta = {
        "base_revision": 42,
        "projection_version": "authority-v1",
        "base_hash": projection_hash(state_summary),
        "effective_hash": projection_hash(
            apply_state_delta(state_summary, delta_ops)
        ),
        "mutation_sequence": 3,
        "ops": delta_ops,
        "reset_reason": None,
    }
    request = {
        "current_message": "我继续观察牢门。",
        "current_turn": {"events": []},
        "session": {"gate_status": "adventure"},
        "current_state_summary": state_summary,
        "turn_state_delta": turn_state_delta,
        "unprotected_notes": "可以压缩的旧资料" * 5000,
        "recent_messages": [],
        "history": [],
    }
    governor = GMContextGovernor(
        GMContextBudget(
            warning_chars=1000,
            proactive_compaction_chars=1500,
            hard_chars=3000,
            target_chars=2000,
        )
    )
    state_bytes = json.dumps(
        state_summary,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    delta_bytes = json.dumps(
        turn_state_delta,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    governed = governor.govern(
        request,
        protected_root_fields=("current_state_summary", "turn_state_delta"),
    )
    restored = json.loads(governed.rendered)

    assert governed.manifest.strategy != ("unchanged",)
    assert governed.manifest.original_chars > 40_000
    assert list(restored)[:3] == [
        "current_state_summary",
        "turn_state_delta",
        "current_message",
    ]
    assert json.dumps(
        restored["current_state_summary"],
        ensure_ascii=False,
        separators=(",", ":"),
    ) == state_bytes
    assert json.dumps(
        restored["turn_state_delta"],
        ensure_ascii=False,
        separators=(",", ":"),
    ) == delta_bytes
    assert projection_hash(restored["current_state_summary"]) == restored[
        "turn_state_delta"
    ]["base_hash"]
    assert projection_hash(
        apply_state_delta(
            restored["current_state_summary"],
            restored["turn_state_delta"]["ops"],
        )
    ) == restored["turn_state_delta"]["effective_hash"]
    assert restored.get("unprotected_notes") != request["unprotected_notes"]
    assert "current_state_summary" in governed.manifest.protected_paths
    assert "turn_state_delta" in governed.manifest.protected_paths
    assert governed.manifest.pressure == "protected_kernel_exceeds_hard_limit"


def test_protected_root_fields_are_optional_and_do_not_change_default_manifest() -> None:
    governor = GMContextGovernor()

    governed = governor.govern(
        {
            "current_message": "在吗？",
            "current_state_summary": {"runtime": {"active": True}},
            "recent_messages": [],
            "history": [],
        }
    )

    assert governed.manifest.protected_paths == governor._PROTECTED_ROOT_PATHS
    assert "turn_state_delta" not in governed.manifest.protected_paths


def test_agent_loop_audits_context_and_uses_budgeted_receipt_view() -> None:
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="inspect_large_reference",
            description="读取一份大型参考资料。",
            handler=lambda _context, _arguments: GMToolReceipt.success(
                "inspect_large_reference",
                result={
                    "window_id": "window-1",
                    "large_reference": ["资料" * 400 for _ in range(20)],
                },
            ),
            max_model_result_chars=900,
        )
    )
    client = _ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "call_tool",
                    "message_kind": "gm_request",
                    "audience": "gm",
                    "tool_name": "inspect_large_reference",
                    "arguments": {},
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "decision": "final",
                    "message_kind": "gm_request",
                    "audience": "gm",
                    "reply": "我查到了。",
                },
                ensure_ascii=False,
            ),
        ]
    )
    agent = LLMGMToolAgent(client, model="test", registry=registry)

    outcome = agent.run(
        "帮我查一下。",
        recent_context="",
        context=_execution_context(),
        state_summary={"runtime": {"active": True}},
    )

    assert outcome.reply == "我查到了。"
    assert outcome.receipts[0].result["large_reference"][0].startswith("资料")
    second_request = json.loads(client.calls[1]["messages"][-1].content)
    model_result = second_request["history"][-1]["tool_receipt"]["result"]
    assert model_result["window_id"] == "window-1"
    assert "large_reference" not in model_result
    assert model_result["_fu_gm_model_view"]["applied"] is True
    assert "context_manifest" not in second_request
    manifest = outcome.trace[-1]["context_manifest"]
    assert manifest["state_version"] == 23
    assert manifest["model_view_only"] is True
