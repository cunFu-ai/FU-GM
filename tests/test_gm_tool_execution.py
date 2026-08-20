from __future__ import annotations

import tempfile
from random import Random
from unittest.mock import patch

from fu_gm.components.campaign_state_transaction import CampaignStateTransaction
from fu_gm.components.gm_message_tool_transaction import GMMessageToolTransaction
from fu_gm.components.scene_frame_manager import SceneFrame
from fu_gm.gm_tool_agent import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolReceipt,
    GMToolRegistry,
)
from fu_gm.gm_tool_execution import GMToolCallLedger
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import PendingCheckBatch


def _context() -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="test",
        session_id="default",
        channel_id="group",
        speaker="玩家",
        gate_status="adventure",
    )


def test_registry_rejects_invalid_handler_receipt() -> None:
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="broken",
            description="broken",
            handler=lambda _context, _arguments: None,  # type: ignore[arg-type,return-value]
        )
    )

    receipt = registry.execute("broken", {}, _context())

    assert not receipt.ok
    assert receipt.error_code == "INVALID_TOOL_RECEIPT"
    assert not receipt.state_changed


def test_failure_receipt_cannot_claim_state_change() -> None:
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="reject",
            description="reject",
            handler=lambda _context, _arguments: GMToolReceipt(
                tool_name="wrong-name",
                ok=False,
                retryable=True,
                state_changed=True,
            ),
            side_effect="write",
        )
    )

    receipt = registry.execute("reject", {}, _context())

    assert not receipt.ok
    assert receipt.tool_name == "reject"
    assert receipt.error_code == "TOOL_REJECTED"
    assert receipt.correction_hint
    assert not receipt.state_changed


def test_multi_message_write_requires_an_exact_source_event() -> None:
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="remember_contribution",
            description="remember",
            handler=lambda _context, _arguments: GMToolReceipt.success(
                "remember_contribution",
                state_changed=True,
            ),
            side_effect="write",
        )
    )
    context = GMToolExecutionContext(
        campaign_id="test",
        session_id="default",
        channel_id="group",
        speaker="南星",
        gate_status="session_zero",
        metadata={
            "current_message": "我觉得可以。",
            "current_turn_events": [
                {
                    "event_id": "event-white",
                    "message_id": "m-white",
                    "speaker": "白河",
                    "speaker_id": "u-white",
                    "text": "我贡献钟鸣公国。",
                },
                {
                    "event_id": "event-south",
                    "message_id": "m-south",
                    "speaker": "南星",
                    "speaker_id": "u-south",
                    "text": "我觉得可以。",
                },
            ],
        },
    )

    receipt = registry.execute("remember_contribution", {}, context)

    assert not receipt.ok
    assert receipt.error_code == "SOURCE_EVENT_REQUIRED"
    assert [
        item["event_id"]
        for item in receipt.result["allowed_source_events"]
    ] == ["event-white", "event-south"]


def test_source_event_binds_write_to_its_real_speaker_and_exact_text() -> None:
    observed: dict[str, object] = {}

    def remember(context, arguments):
        observed.update(
            {
                "speaker": context.speaker,
                "message": context.metadata.get("current_message"),
                "source_event_id": context.metadata.get("source_event_id"),
                "evidence": arguments.get("evidence"),
            }
        )
        return GMToolReceipt.success(
            "remember_contribution",
            state_changed=True,
        )

    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="remember_contribution",
            description="remember",
            parameters=(
                GMToolParameter(
                    "evidence",
                    "string",
                    "trusted source text",
                    required=True,
                    source="current_message",
                ),
            ),
            handler=remember,
            side_effect="write",
        )
    )
    context = GMToolExecutionContext(
        campaign_id="test",
        session_id="default",
        channel_id="group",
        speaker="南星",
        gate_status="session_zero",
        metadata={
            "current_message": "我觉得可以。",
            "current_turn_events": [
                {
                    "event_id": "event-white",
                    "message_id": "m-white",
                    "speaker": "白河",
                    "speaker_id": "u-white",
                    "text": "我贡献钟鸣公国。",
                },
                {
                    "event_id": "event-south",
                    "message_id": "m-south",
                    "speaker": "南星",
                    "speaker_id": "u-south",
                    "text": "我觉得可以。",
                },
            ],
        },
    )

    receipt = registry.execute(
        "remember_contribution",
        {"source_event_id": "event-white"},
        context,
    )

    assert receipt.ok
    assert observed == {
        "speaker": "白河",
        "message": "我贡献钟鸣公国。",
        "source_event_id": "event-white",
        "evidence": "我贡献钟鸣公国。",
    }


def test_single_message_write_ignores_stale_model_source_event_id() -> None:
    observed: dict[str, object] = {}

    def remember(context, arguments):
        observed.update(
            {
                "speaker": context.speaker,
                "message": context.metadata.get("current_message"),
                "source_event_id": context.metadata.get("source_event_id"),
                "evidence": arguments.get("evidence"),
            }
        )
        return GMToolReceipt.success(
            "remember_contribution",
            state_changed=True,
        )

    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="remember_contribution",
            description="remember",
            parameters=(
                GMToolParameter(
                    "evidence",
                    "string",
                    "trusted source text",
                    required=True,
                    source="current_message",
                ),
            ),
            handler=remember,
            side_effect="write",
        )
    )
    context = GMToolExecutionContext(
        campaign_id="test",
        session_id="default",
        channel_id="group",
        speaker="南星",
        gate_status="session_zero",
        metadata={
            "current_message": "我贡献钟鸣公国。",
            "current_turn_events": [
                {
                    "event_id": "event-current",
                    "message_id": "m-current",
                    "speaker": "南星",
                    "speaker_id": "u-south",
                    "text": "我贡献钟鸣公国。",
                }
            ],
        },
    )

    receipt = registry.execute(
        "remember_contribution",
        {"source_event_id": "event-from-old-context"},
        context,
    )

    assert receipt.ok
    assert observed == {
        "speaker": "南星",
        "message": "我贡献钟鸣公国。",
        "source_event_id": "event-current",
        "evidence": "我贡献钟鸣公国。",
    }


def test_heartbeat_write_ignores_stale_player_source_event_id() -> None:
    observed: dict[str, object] = {}

    def advance(context, _arguments):
        observed.update(
            {
                "speaker": context.speaker,
                "source_event_id": context.metadata.get("source_event_id"),
                "events": context.metadata.get("current_turn_events"),
            }
        )
        return GMToolReceipt.success("advance_scene", state_changed=True)

    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="advance_scene",
            description="advance",
            handler=advance,
            side_effect="write",
        )
    )
    context = GMToolExecutionContext(
        campaign_id="test",
        session_id="default",
        channel_id="group",
        speaker="时悠",
        gate_status="adventure",
        metadata={
            "system_gm_beat_request": True,
            "source_event_id": "old-player-event",
            "source_speaker": "玩家",
            "current_turn_events": [],
        },
    )

    receipt = registry.execute(
        "advance_scene",
        {"source_event_id": "old-player-event"},
        context,
    )

    assert receipt.ok
    assert observed == {
        "speaker": "时悠",
        "source_event_id": None,
        "events": [],
    }


def test_call_ledger_applies_same_duplicate_guard_across_single_and_batch_calls() -> None:
    registry = GMToolRegistry()
    writes: list[str] = []

    def save(_context, arguments):
        writes.append(str(arguments["slot"]))
        return GMToolReceipt.success("save", state_changed=True)

    registry.register(
        GMToolDefinition(
            name="save",
            description="save",
            handler=save,
            parameters=(GMToolParameter("slot", "string", "slot", required=True),),
            side_effect="write",
        )
    )
    ledger = GMToolCallLedger(
        registry=registry,
        context=_context(),
        state_summary={},
    )

    first = ledger.execute("save", {"slot": "checkpoint"})
    duplicate = ledger.execute("save", {"slot": "checkpoint"}, batch_index=1)

    assert first.receipt is not None and first.receipt.ok
    assert duplicate.receipt is None
    assert duplicate.protocol_error_code == "DUPLICATE_SUCCESSFUL_TOOL_CALL"
    assert writes == ["checkpoint"]
    assert len(ledger.receipts) == 1
    assert ledger.history[-1]["protocol_error"]["error_code"] == "DUPLICATE_SUCCESSFUL_TOOL_CALL"


def test_call_ledger_rejects_tool_before_handler_when_context_guard_denies_it() -> None:
    registry = GMToolRegistry()
    calls: list[str] = []

    registry.register(
        GMToolDefinition(
            name="system_only",
            description="system only",
            handler=lambda _context, _arguments: (
                calls.append("executed")
                or GMToolReceipt.success("system_only", state_changed=True)
            ),
            side_effect="write",
        )
    )
    ledger = GMToolCallLedger(
        registry=registry,
        context=_context(),
        state_summary={},
        tool_permission_guard=lambda name: name != "system_only",
    )

    denied = ledger.execute("system_only", {})

    assert denied.receipt is None
    assert denied.protocol_error_code == "TOOL_NOT_AVAILABLE_IN_CONTEXT"
    assert calls == []
    assert ledger.receipts == []
    assert (
        ledger.history[-1]["protocol_error"]["error_code"]
        == "TOOL_NOT_AVAILABLE_IN_CONTEXT"
    )


def test_call_ledger_uses_compact_model_receipt_but_keeps_full_audit_receipt() -> None:
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="start_session",
            description="start",
            handler=lambda _context, _arguments: GMToolReceipt.success(
                "start_session",
                result={
                    "adventure_opening_required": True,
                    "saved_path": "/tmp/full-audit.json",
                    "required_followup_tools": ["start_scene"],
                    "session_situation_contract": {
                        "potential_scenes": [
                            {
                                "scene_key": "opening",
                                "scene_role": "strong_start",
                            },
                            {
                                "scene_key": "later",
                                "scene_role": "aftermath",
                            },
                        ]
                    },
                },
                state_changed=True,
            ),
            side_effect="write",
        )
    )
    ledger = GMToolCallLedger(
        registry=registry,
        context=_context(),
        state_summary={},
    )

    event = ledger.execute("start_session", {})

    assert event.receipt is not None and event.receipt.ok
    assert ledger.receipts[0].result["saved_path"] == "/tmp/full-audit.json"
    model_result = ledger.history[-1]["tool_receipt"]["result"]
    assert "saved_path" not in model_result
    assert model_result["session_situation_contract"]["opening_scene"][
        "scene_key"
    ] == "opening"


def test_real_message_transaction_restores_memory_disk_and_restart_state() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        runtime = service._runtime("real-message-transaction")
        runtime.app.initialize_session_zero(participants=["白河"])
        service._autosave_campaign(runtime, "real-message-transaction")
        snapshot_path = runtime.app.memory_store._snapshot_path(
            "real-message-transaction"
        )
        snapshot_before = snapshot_path.read_bytes()
        context = GMToolExecutionContext(
            campaign_id="real-message-transaction",
            session_id="s0",
            channel_id="group-1",
            speaker="白河",
            gate_status="session_zero",
            directly_addressed=True,
            metadata={
                "current_message": "我贡献一个国家：钟鸣公国。",
            },
        )
        state_summary: dict[str, object] = {}
        message_transaction = GMMessageToolTransaction.begin(
            registry=service.gm_tool_registry,
            context=context,
            state_summary=state_summary,
        )
        ledger = GMToolCallLedger(
            registry=service.gm_tool_registry,
            context=context,
            state_summary=state_summary,
            message_transaction=message_transaction,
        )

        event = ledger.execute(
            "create_world_setting",
            {
                "category": "kingdoms",
                "name": "钟鸣公国",
                "value": "以钟塔与风铃航路闻名",
                "visibility": "public",
                "authority": "player_confirmed",
                "reason": "玩家明确贡献国家。",
            },
        )

        assert event.receipt is not None and event.receipt.ok
        assert "钟鸣公国" in runtime.app.world_state.world_profile.kingdoms
        assert snapshot_path.read_bytes() != snapshot_before

        assert message_transaction.rollback() == ""
        GMMessageToolTransaction.mark_receipts_rolled_back(ledger.receipts)

        assert "钟鸣公国" not in runtime.app.world_state.world_profile.kingdoms
        assert snapshot_path.read_bytes() == snapshot_before
        assert not event.receipt.state_changed
        assert event.receipt.result["rolled_back"] is True
        assert event.receipt.narrative_events
        assert all(
            item.status == "rolled_back"
            and not item.outcome
            and not item.public_facts
            for item in event.receipt.narrative_events
        )

        restarted = FUGMHttpService(data_root=data_root, use_llm=False)
        restored = restarted._runtime("real-message-transaction")
        assert "钟鸣公国" not in restored.app.world_state.world_profile.kingdoms


def test_message_transaction_uses_one_outer_snapshot_and_versions_on_commit() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        runtime = service._runtime("versioned-message")
        runtime.app.initialize_session_zero(participants=["白河"])
        context = GMToolExecutionContext(
            campaign_id="versioned-message",
            session_id="s0",
            channel_id="group-1",
            speaker="白河",
            gate_status="session_zero",
            directly_addressed=True,
            metadata={
                "current_message": "补充一个国家和一段历史。",
                "_gm_campaign_observed_version": 0,
            },
        )
        state_summary: dict[str, object] = {}
        message_transaction = GMMessageToolTransaction.begin(
            registry=service.gm_tool_registry,
            context=context,
            state_summary=state_summary,
            side_effect_lock=runtime.transaction_lock,
        )
        ledger = GMToolCallLedger(
            registry=service.gm_tool_registry,
            context=context,
            state_summary=state_summary,
            side_effect_lock=runtime.transaction_lock,
            message_transaction=message_transaction,
        )

        original_capture = CampaignStateTransaction.capture
        with patch.object(
            CampaignStateTransaction,
            "capture",
            wraps=original_capture,
        ) as capture:
            first = ledger.execute(
                "create_world_setting",
                {
                    "category": "kingdoms",
                    "name": "钟鸣公国",
                    "value": "钟塔之国",
                    "visibility": "public",
                    "authority": "player_confirmed",
                    "reason": "玩家明确贡献国家。",
                },
            )
            second = ledger.execute(
                "create_world_setting",
                {
                    "category": "kingdoms",
                    "name": "潮汐联邦",
                    "value": "群岛之国",
                    "visibility": "public",
                    "authority": "player_confirmed",
                    "reason": "玩家明确贡献国家。",
                },
            )

        assert first.receipt is not None and first.receipt.ok
        assert second.receipt is not None and second.receipt.ok
        # 一次消息级总快照，加上每个工具各自的短事务快照。
        assert capture.call_count == 3
        # 消息内部的中间写入不对其他请求发布新版本；只有整条消息提交时
        # 才统一升级一次。这里随后回滚，因此版本应始终保持在起点。
        assert runtime.state_version == 0
        assert runtime.write_lease_owner

        assert message_transaction.rollback() == ""
        assert runtime.state_version == 0
        assert runtime.write_lease_owner == ""
        assert "钟鸣公国" not in runtime.app.world_state.world_profile.kingdoms


def test_stale_parallel_message_cannot_overwrite_committed_campaign_version() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        runtime = service._runtime("parallel-version")
        runtime.app.initialize_session_zero(participants=["白河", "南星"])

        def transaction_for(speaker: str, observed_version: int):
            context = GMToolExecutionContext(
                campaign_id="parallel-version",
                session_id="s0",
                channel_id="group-1",
                speaker=speaker,
                gate_status="session_zero",
                metadata={
                    "current_message": "我补一项世界设定。",
                    "_gm_campaign_observed_version": observed_version,
                },
            )
            state_summary: dict[str, object] = {}
            message_transaction = GMMessageToolTransaction.begin(
                registry=service.gm_tool_registry,
                context=context,
                state_summary=state_summary,
                side_effect_lock=runtime.transaction_lock,
            )
            ledger = GMToolCallLedger(
                registry=service.gm_tool_registry,
                context=context,
                state_summary=state_summary,
                side_effect_lock=runtime.transaction_lock,
                message_transaction=message_transaction,
            )
            return context, message_transaction, ledger

        _context_a, transaction_a, ledger_a = transaction_for("白河", 0)
        first = ledger_a.execute(
            "create_world_setting",
            {
                "category": "kingdoms",
                "name": "钟鸣公国",
                "value": "钟塔之国",
                "visibility": "public",
                "authority": "player_confirmed",
                "reason": "玩家明确贡献国家。",
            },
        )
        assert first.receipt is not None and first.receipt.ok
        assert runtime.state_version == 0

        context_b, transaction_b, ledger_b = transaction_for("南星", 0)
        stale = ledger_b.execute(
            "create_world_setting",
            {
                "category": "kingdoms",
                "name": "潮汐联邦",
                "value": "群岛之国",
                "visibility": "public",
                "authority": "player_confirmed",
                "reason": "玩家明确贡献国家。",
            },
        )
        assert stale.receipt is None
        assert stale.protocol_error_code == "MESSAGE_TRANSACTION_START_FAILED"
        assert context_b.metadata["_gm_campaign_version_conflict"]
        assert "潮汐联邦" not in runtime.app.world_state.world_profile.kingdoms

        assert transaction_a.commit() == ""
        assert runtime.state_version == 1
        assert runtime.write_lease_owner == ""
        assert transaction_b.rollback() == ""

        _context_c, transaction_c, ledger_c = transaction_for("南星", 1)
        fresh = ledger_c.execute(
            "create_world_setting",
            {
                "category": "kingdoms",
                "name": "潮汐联邦",
                "value": "群岛之国",
                "visibility": "public",
                "authority": "player_confirmed",
                "reason": "玩家明确贡献国家。",
            },
        )
        assert fresh.receipt is not None and fresh.receipt.ok
        assert transaction_c.commit() == ""
        assert runtime.state_version == 2
        assert "钟鸣公国" in runtime.app.world_state.world_profile.kingdoms
        assert "潮汐联邦" in runtime.app.world_state.world_profile.kingdoms


def test_replace_state_message_versions_and_releases_every_affected_runtime() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        source = service._runtime("旧团")
        source.app.initialize_session_zero(participants=["白河"])
        service._autosave_campaign(source, "旧团")
        context = GMToolExecutionContext(
            campaign_id="旧团",
            session_id="s0",
            channel_id="group-1",
            speaker="白河",
            gate_status="session_zero",
            directly_addressed=True,
            metadata={
                "current_message": "请新建战役新团。",
                "_gm_campaign_observed_version": 0,
            },
        )
        state_summary: dict[str, object] = {}
        transaction = GMMessageToolTransaction.begin(
            registry=service.gm_tool_registry,
            context=context,
            state_summary=state_summary,
            side_effect_lock=source.transaction_lock,
        )
        ledger = GMToolCallLedger(
            registry=service.gm_tool_registry,
            context=context,
            state_summary=state_summary,
            side_effect_lock=source.transaction_lock,
            message_transaction=transaction,
        )

        created = ledger.execute(
            "create_campaign",
            {"campaign_id": "新团"},
        )

        assert created.receipt is not None and created.receipt.ok
        target = service.runtimes["新团"]
        assert source.state_version == 0
        assert target.state_version == 0
        assert source.write_lease_owner
        assert target.write_lease_owner == source.write_lease_owner

        assert transaction.commit() == ""
        assert source.state_version == 1
        assert target.state_version == 1
        assert source.write_lease_owner == ""
        assert target.write_lease_owner == ""
        assert service.current_campaign_id == "新团"


def test_call_ledger_permission_guard_does_not_replace_unknown_tool_validation() -> None:
    registry = GMToolRegistry()
    ledger = GMToolCallLedger(
        registry=registry,
        context=_context(),
        state_summary={},
        tool_permission_guard=lambda _name: True,
    )

    unknown = ledger.execute("extension_that_is_not_registered", {})

    assert unknown.receipt is not None
    assert not unknown.receipt.ok
    assert unknown.receipt.error_code == "UNKNOWN_TOOL"


def test_call_ledger_aborts_when_rephrased_call_exceeds_tool_limit() -> None:
    registry = GMToolRegistry()
    writes: list[str] = []

    def answer(_context, arguments):
        writes.append(str(arguments["instruction"]))
        return GMToolReceipt.success(
            "decide_npc_response",
            state_changed=True,
            public_reply="会长已经回答。",
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
    ledger = GMToolCallLedger(
        registry=registry,
        context=_context(),
        state_summary={},
    )

    first = ledger.execute(
        "decide_npc_response",
        {"instruction": "回答放行条件"},
    )
    rephrased = ledger.execute(
        "decide_npc_response",
        {"instruction": "再次说明是否放行"},
    )

    assert first.receipt is not None and first.receipt.ok
    assert rephrased.receipt is None
    assert rephrased.protocol_error_code == "TOOL_CALL_LIMIT_REACHED"
    assert rephrased.abort_repeated_call_loop
    assert writes == ["回答放行条件"]


def test_call_ledger_applies_tool_limit_to_successful_read_calls() -> None:
    registry = GMToolRegistry()
    reads: list[str] = []

    def read_world(_context, arguments):
        reads.append(str(arguments["campaign_id"]))
        return GMToolReceipt.success(
            "get_world_state",
            result={"campaign_id": str(arguments["campaign_id"])},
        )

    registry.register(
        GMToolDefinition(
            name="get_world_state",
            description="world",
            handler=read_world,
            parameters=(
                GMToolParameter("campaign_id", "string", "campaign", required=True),
            ),
            max_successful_calls_per_message=1,
        )
    )
    ledger = GMToolCallLedger(
        registry=registry,
        context=_context(),
        state_summary={},
    )

    first = ledger.execute("get_world_state", {"campaign_id": "当前团"})
    switched = ledger.execute("get_world_state", {"campaign_id": "另一个存档"})

    assert first.receipt is not None and first.receipt.ok
    assert switched.receipt is None
    assert switched.protocol_error_code == "TOOL_CALL_LIMIT_REACHED"
    assert switched.abort_repeated_call_loop
    assert reads == ["当前团"]


def test_call_ledger_requires_same_npc_tool_after_invalid_transaction() -> None:
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="decide_npc_response",
            description="npc",
            parameters=(GMToolParameter("name", "string", "npc", required=True),),
            handler=lambda _context, _arguments: GMToolReceipt(
                tool_name="decide_npc_response",
                ok=False,
                error_code="NPC_RESPONSE_TRANSACTION_INVALID",
                message="the GM transaction did not answer the NPC's own gate condition",
                correction_hint="retry the same NPC transaction",
                retryable=True,
            ),
            side_effect="write",
        )
    )
    registry.register(
        GMToolDefinition(
            name="commit_scene_response",
            description="scene",
            handler=lambda _context, _arguments: GMToolReceipt.success(
                "commit_scene_response",
                state_changed=True,
            ),
            side_effect="write",
        )
    )
    ledger = GMToolCallLedger(
        registry=registry,
        context=_context(),
        state_summary={},
    )

    failed = ledger.execute("decide_npc_response", {"name": "白花守门人"})
    protocol_error = ledger.retry_protocol_error(
        {
            "decision": "call_tool",
            "tool_name": "commit_scene_response",
            "arguments": {},
        }
    )

    assert failed.receipt is not None and not failed.receipt.ok
    assert protocol_error is not None
    assert (
        protocol_error["protocol_error"]["error_code"]
        == "AGENT_OUTPUT_RETRY_TOOL_OMITTED"
    )
    assert protocol_error["protocol_error"]["required_retry"]["tool_name"] == "decide_npc_response"


def test_call_ledger_stops_after_three_invalid_npc_transactions() -> None:
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="decide_npc_action",
            description="npc beat",
            handler=lambda _context, _arguments: GMToolReceipt(
                tool_name="decide_npc_action",
                ok=False,
                error_code="NPC_RESPONSE_TRANSACTION_INVALID",
                message="the GM transaction contradicted the latest public action",
                correction_hint="retry the same transaction with the latest public state",
                retryable=True,
            ),
            side_effect="write",
        )
    )
    ledger = GMToolCallLedger(
        registry=registry,
        context=_context(),
        state_summary={},
    )

    first = ledger.execute("decide_npc_action", {})
    second = ledger.execute("decide_npc_action", {})
    third = ledger.execute("decide_npc_action", {})

    assert not first.abort_repeated_call_loop
    assert not second.abort_repeated_call_loop
    assert third.abort_repeated_call_loop
    assert ledger.pending_required_retry is not None
    assert ledger.pending_required_retry["attempt_count"] == 3
    assert ledger.pending_required_retry["max_attempts"] == 3


def test_required_write_retry_allows_read_only_rule_lookup_first() -> None:
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="update_hero_draft",
            description="write",
            handler=lambda _context, _arguments: GMToolReceipt.failure(
                "update_hero_draft",
                "UNKNOWN_ARGUMENT",
                "参数错误。",
                "修正后重试。",
            ),
            side_effect="write",
        )
    )
    registry.register(
        GMToolDefinition(
            name="search_rule_references",
            description="read",
            parameters=(
                GMToolParameter("kind", "string", "kind", required=True),
                GMToolParameter("text", "string", "text"),
            ),
            handler=lambda _context, _arguments: GMToolReceipt.success(
                "search_rule_references",
                result={"references": [{"name": "契约与召唤"}]},
            ),
        )
    )
    ledger = GMToolCallLedger(
        registry=registry,
        context=_context(),
        state_summary={},
    )

    ledger.execute("update_hero_draft", {"terminal_decision": "silent"})
    protocol_error = ledger.retry_protocol_error(
        {
            "decision": "call_tool",
            "tool_name": "search_rule_references",
            "arguments": {"kind": "skill", "text": "契约与召唤"},
        }
    )

    assert protocol_error is None
    lookup = ledger.execute(
        "search_rule_references",
        {"kind": "skill", "text": "契约与召唤"},
    )
    assert lookup.receipt is not None and lookup.receipt.ok
    assert ledger.required_retry_pending


def test_call_ledger_requires_same_tool_after_all_schema_omission_errors() -> None:
    for invalid_arguments, expected_code in (
        ({}, "MISSING_ARGUMENT"),
        (
            {"name": "伊莉雅", "evidence": "模型不得填写"},
            "SYSTEM_ARGUMENT_NOT_ALLOWED",
        ),
    ):
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="update_hero",
                description="write",
                parameters=(
                    GMToolParameter(
                        "name",
                        "string",
                        "hero",
                        required=True,
                    ),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "trusted message",
                        required=True,
                        source="current_message",
                    ),
                ),
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "update_hero",
                    state_changed=True,
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="other_write",
                description="other",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "other_write",
                    state_changed=True,
                ),
                side_effect="write",
            )
        )
        ledger = GMToolCallLedger(
            registry=registry,
            context=_context(),
            state_summary={},
        )

        failed = ledger.execute("update_hero", invalid_arguments)
        protocol_error = ledger.retry_protocol_error(
            {
                "decision": "call_tool",
                "tool_name": "other_write",
                "arguments": {},
            }
        )

        assert failed.receipt is not None
        assert failed.receipt.error_code == expected_code
        assert protocol_error is not None
        assert (
            protocol_error["protocol_error"]["error_code"]
            == "SCHEMA_RETRY_TOOL_OMITTED"
        )
        assert (
            protocol_error["protocol_error"]["required_retry"]["tool_name"]
            == "update_hero"
        )


def test_required_retry_is_replaced_by_the_new_receipt_error() -> None:
    attempts = 0

    def update(_context, _arguments):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return GMToolReceipt.failure(
                "update_hero_draft",
                "UNKNOWN_ARGUMENT",
                "参数错误。",
                "删除多余参数。",
            )
        return GMToolReceipt.failure(
            "update_hero_draft",
            "UNKNOWN_HERO_SKILL",
            "技能名不存在。",
            "先查询完整技能名。",
        )

    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="update_hero_draft",
            description="write",
            parameters=(
                GMToolParameter("value", "object", "value", required=True),
            ),
            handler=update,
            side_effect="write",
        )
    )
    ledger = GMToolCallLedger(
        registry=registry,
        context=_context(),
        state_summary={},
    )

    ledger.execute("update_hero_draft", {"value": {"bad": True}})
    assert ledger.required_retry_pending
    ledger.execute("update_hero_draft", {"value": {"skills": {"契约": 1}}})

    assert not ledger.required_retry_pending


def test_invalid_action_type_reselects_tool_instead_of_forcing_semantic_drift() -> None:
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="perform_character_action",
            description="character action",
            parameters=(
                GMToolParameter(
                    "action_type",
                    "string",
                    "action",
                    required=True,
                    enum=("Attack", "Guard"),
                ),
            ),
            handler=lambda _context, _arguments: GMToolReceipt.success(
                "perform_character_action",
                state_changed=True,
            ),
            side_effect="write",
        )
    )
    registry.register(
        GMToolDefinition(
            name="declare_movement_check",
            description="movement",
            handler=lambda _context, _arguments: GMToolReceipt.success(
                "declare_movement_check",
                state_changed=True,
            ),
            side_effect="write",
        )
    )
    ledger = GMToolCallLedger(
        registry=registry,
        context=_context(),
        state_summary={},
    )

    failed = ledger.execute(
        "perform_character_action",
        {"action_type": "Objective"},
    )

    assert failed.receipt is not None
    assert failed.receipt.error_code == "ARGUMENT_ENUM_MISMATCH"
    assert ledger.required_retry_pending is False
    assert ledger.retry_protocol_error(
        {
            "decision": "call_tool",
            "tool_name": "declare_movement_check",
            "arguments": {},
        }
    ) is None
    assert any(
        item.get("protocol_error", {}).get("error_code")
        == "ACTION_TYPE_TOOL_RESELECTION_REQUIRED"
        for item in ledger.history
    )


def test_registry_rolls_back_failed_mutating_handler_before_returning_receipt() -> None:
    state: list[str] = []
    lifecycle: list[str] = []

    class Transaction:
        def commit(self) -> None:
            lifecycle.append("commit")

        def rollback(self) -> None:
            state.clear()
            lifecycle.append("rollback")

    registry = GMToolRegistry(
        transaction_factory=lambda *_args: Transaction()
    )
    registry.register(
        GMToolDefinition(
            name="partial_write",
            description="test",
            handler=lambda _context, _arguments: (
                state.append("mutated")
                or GMToolReceipt.failure(
                    "partial_write",
                    "DOMAIN_REJECTED",
                    "领域层拒绝。",
                    "修正后重试。",
                )
            ),
            side_effect="write",
        )
    )

    receipt = registry.execute("partial_write", {}, _context())

    assert not receipt.ok
    assert receipt.error_code == "DOMAIN_REJECTED"
    assert state == []
    assert lifecycle == ["rollback"]


def test_real_tool_transaction_restores_topic_memory_directory() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        runtime = service._runtime("topic-memory-rollback")
        existing = runtime.app.topic_memory_store.write_topic_memory(
            "topic-memory-rollback",
            visibility="public",
            memory_type="existing",
            title="既有事实",
            body="这条事实必须保留。",
            filename="existing",
        )
        existing_payload = existing.read_bytes()

        def fail_after_topic_write(_context, _arguments):
            runtime.app.topic_memory_store.write_topic_memory(
                "topic-memory-rollback",
                visibility="public",
                memory_type="ghost",
                title="幽灵事实",
                body="事务失败后不得留下。",
                filename="ghost",
            )
            raise RuntimeError("fail after topic write")

        service.gm_tool_registry.register(
            GMToolDefinition(
                name="write_topic_then_fail",
                description="test only",
                handler=fail_after_topic_write,
                side_effect="write",
            )
        )

        receipt = service.gm_tool_registry.execute(
            "write_topic_then_fail",
            {},
            GMToolExecutionContext(
                campaign_id="topic-memory-rollback",
                session_id="s1",
                channel_id="group",
                speaker="阿凛",
                gate_status="adventure",
            ),
        )

        assert receipt.ok is False
        memory_root = (
            runtime.app.memory_store._campaign_dir("topic-memory-rollback")
            / "memory"
        )
        assert existing.read_bytes() == existing_payload
        assert not (memory_root / "public" / "ghost.md").exists()
        index = (memory_root / "MEMORY.md").read_text(encoding="utf-8")
        assert "既有事实" in index
        assert "幽灵事实" not in index


def test_real_tool_transaction_keeps_full_in_memory_histories_on_rollback() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        runtime = service._runtime("lossless-rollback")
        app = runtime.app
        app.scene_frame_manager.history = [
            SceneFrame(scene_key=f"scene-{index}", scene_name=f"场景{index}")
            for index in range(8)
        ]
        app.world_state.check_batch_history = [
            PendingCheckBatch(
                batch_id=f"batch-{index}",
                kind="group",
                source_action_type="GroupCheck",
                source_parameters={},
                actor_order=["伊莉雅"],
            )
            for index in range(120)
        ]

        def mutate_then_fail(_context, _arguments):
            app.scene_frame_manager.history.append(
                SceneFrame(scene_key="ghost", scene_name="幽灵场景")
            )
            app.world_state.check_batch_history.append(
                PendingCheckBatch(
                    batch_id="ghost",
                    kind="group",
                    source_action_type="GroupCheck",
                    source_parameters={},
                    actor_order=["伊莉雅"],
                )
            )
            return GMToolReceipt.failure(
                "mutate_history_then_fail",
                "INJECTED_FAILURE",
                "注入失败。",
                "测试回滚。",
            )

        service.gm_tool_registry.register(
            GMToolDefinition(
                name="mutate_history_then_fail",
                description="test only",
                handler=mutate_then_fail,
                side_effect="write",
            )
        )

        receipt = service.gm_tool_registry.execute(
            "mutate_history_then_fail",
            {},
            GMToolExecutionContext(
                campaign_id="lossless-rollback",
                session_id="s1",
                channel_id="group",
                speaker="阿凛",
                gate_status="adventure",
            ),
        )

        assert not receipt.ok
        assert [
            frame.scene_key for frame in app.scene_frame_manager.history
        ] == [f"scene-{index}" for index in range(8)]
        assert [
            batch.batch_id for batch in app.world_state.check_batch_history
        ] == [f"batch-{index}" for index in range(120)]


def test_real_tool_transaction_restores_character_creation_ephemeral_state() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        runtime = service._runtime("creation-rollback")
        manager = runtime.app.character_creation_manager
        manager.hero_profiles = {"伊莉雅": "existing"}  # type: ignore[dict-item]
        rng_state = manager.rules_engine._rng.getstate()
        expected_rng = Random()
        expected_rng.setstate(rng_state)
        expected_next_roll = expected_rng.randint(1, 6)

        def mutate_then_fail(_context, _arguments):
            manager.hero_profiles["幽灵角色"] = "ghost"  # type: ignore[assignment]
            manager.rules_engine.roll_die(6)
            return GMToolReceipt.failure(
                "mutate_creation_then_fail",
                "INJECTED_FAILURE",
                "注入失败。",
                "测试回滚。",
            )

        service.gm_tool_registry.register(
            GMToolDefinition(
                name="mutate_creation_then_fail",
                description="test only",
                handler=mutate_then_fail,
                side_effect="write",
            )
        )

        receipt = service.gm_tool_registry.execute(
            "mutate_creation_then_fail",
            {},
            GMToolExecutionContext(
                campaign_id="creation-rollback",
                session_id="s1",
                channel_id="group",
                speaker="阿凛",
                gate_status="session_zero",
            ),
        )

        assert not receipt.ok
        assert manager.hero_profiles == {"伊莉雅": "existing"}
        assert manager.rules_engine.roll_die(6) == expected_next_roll


def test_registry_commits_successful_mutating_handler_once() -> None:
    lifecycle: list[str] = []

    class Transaction:
        def commit(self) -> None:
            lifecycle.append("commit")

        def rollback(self) -> None:
            lifecycle.append("rollback")

    registry = GMToolRegistry(transaction_factory=lambda *_args: Transaction())
    registry.register(
        GMToolDefinition(
            name="write",
            description="test",
            handler=lambda _context, _arguments: GMToolReceipt.success(
                "write",
                state_changed=True,
            ),
            side_effect="write",
        )
    )

    receipt = registry.execute("write", {}, _context())

    assert receipt.ok
    assert lifecycle == ["commit"]


def test_argument_error_receipt_returns_exact_model_schema() -> None:
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="decide_npc_response",
            description="npc",
            handler=lambda _context, _arguments: GMToolReceipt.success(
                "decide_npc_response"
            ),
            parameters=(
                GMToolParameter("name", "string", "NPC name", required=True),
                GMToolParameter("actor", "string", "PC name"),
                GMToolParameter(
                    "evidence",
                    "string",
                    "trusted message",
                    required=True,
                    source="current_message",
                ),
            ),
            side_effect="write",
        )
    )

    receipt = registry.execute(
        "decide_npc_response",
        {"npc": "失忆旅人", "prompt": "稳住脚步"},
        _context(),
    )

    assert not receipt.ok
    assert receipt.error_code == "UNKNOWN_ARGUMENT"
    assert receipt.result["allowed_arguments"] == ["name", "actor"]
    assert receipt.result["required_arguments"] == ["name"]
    assert "evidence" not in receipt.result["argument_schema"]
    assert "不要把删除错误字段理解为提交空对象" in receipt.correction_hint
