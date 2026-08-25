from __future__ import annotations

import json
import tempfile
from unittest.mock import patch

from fu_gm.components.gm_supervisor import GMCapabilityBroker
from fu_gm.gm_tool_agent import GMToolExecutionContext, LLMGMToolAgent
from fu_gm.http_server import FUGMHttpService


class CapabilityAwareScriptedClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = [json.dumps(item, ensure_ascii=False) for item in responses]

    def create_chat_completion(self, **kwargs: object) -> str:
        if not self.responses:
            raise AssertionError("缺少脚本化模型响应。")
        messages = kwargs["messages"]
        request = json.loads(messages[1].content)
        available = {
            str(item.get("name") or "")
            for item in list(request.get("available_tools") or [])
            if isinstance(item, dict)
        }
        scripted = json.loads(self.responses[0])
        requested = {
            str(scripted.get("tool_name") or "")
        } if scripted.get("decision") == "call_tool" else set()
        missing = {name for name in requested if name and name not in available}
        if missing and GMCapabilityBroker.DISCOVERY_TOOL in available:
            domains = GMCapabilityBroker.domains_for_tools(missing)
            if domains:
                return json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "tool_name": "discover_capabilities",
                        "arguments": {
                            "domains": domains[:4],
                            "reason": "测试取得确认提案所需能力。",
                        },
                    },
                    ensure_ascii=False,
                )
        return self.responses.pop(0)


def _context(message: str, *, speaker: str) -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="真实签发包回滚团",
        session_id="s0",
        channel_id="group-1",
        speaker=speaker,
        gate_status="session_zero",
        directly_addressed=True,
        metadata={"current_message": message},
    )


def test_signed_session_zero_packet_restores_memory_snapshot_and_restart() -> None:
    history = "灰烬之潮曾吞没旧王都。"
    threat = "灰烬之潮正在复苏。"
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        runtime = service._runtime("真实签发包回滚团")
        runtime.app.initialize_session_zero(participants=["白河", "南星"])
        proposed = service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "灰烬之潮既是历史灾难，也正在复苏",
                "world_operations": [
                    {
                        "operation": "create",
                        "category": "historical_events",
                        "value": history,
                        "visibility": "public",
                    },
                    {
                        "operation": "create",
                        "category": "world_threats",
                        "value": threat,
                        "visibility": "public",
                    },
                ],
            },
            _context(
                "我提议灰烬之潮曾吞没旧王都，而且它正在复苏。",
                speaker="白河",
            ),
        )
        assert proposed.ok, proposed.to_dict()
        proposal_id = proposed.result["proposal"]["id"]
        service._autosave_campaign(runtime, "真实签发包回滚团")
        snapshot_path = runtime.app.memory_store._snapshot_path(
            "真实签发包回滚团"
        )
        snapshot_before = snapshot_path.read_bytes()
        first_child_snapshot: list[bytes] = []
        real_autosave = service._autosave_campaign

        def fail_second_child(current_runtime, campaign_id: str) -> str:
            world = current_runtime.app.world_state.world_profile
            if history in world.historical_events and threat in world.world_threats:
                raise RuntimeError("second signed CRUD autosave failed")
            saved = real_autosave(current_runtime, campaign_id)
            if history in world.historical_events and not first_child_snapshot:
                first_child_snapshot.append(snapshot_path.read_bytes())
            return saved

        confirm_context = _context("我同意这项提案。", speaker="南星")
        client = CapabilityAwareScriptedClient(
            [
                {
                    "decision": "call_tool",
                    "message_kind": "gm_request",
                    "audience": "gm",
                    "tool_name": "confirm_session_zero_proposal",
                    "arguments": {"proposal_id": proposal_id},
                    "reason": "玩家明确确认当前待定提案。",
                },
                {
                    "decision": "final",
                    "message_kind": "gm_request",
                    "audience": "gm",
                    "reply": "这次没有完整写入。",
                    "reason": "签发包失败，等待整条消息事务回滚。",
                },
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="scripted",
            registry=service.gm_tool_registry,
            max_iterations=4,
        )
        state_summary = (
            service.gm_agent_message_coordinator.state_builder.build_full(
                confirm_context
            )
        )

        with patch.object(
            service,
            "_autosave_campaign",
            side_effect=fail_second_child,
        ):
            outcome = agent.run(
                "我同意这项提案。",
                recent_context="白河提出灰烬之潮提案。",
                context=confirm_context,
                state_summary=state_summary,
            )

        assert first_child_snapshot
        assert first_child_snapshot[0] != snapshot_before
        world = runtime.app.world_state.world_profile
        assert history not in world.historical_events
        assert threat not in world.world_threats
        assert [item["id"] for item in world.pending_proposals] == [proposal_id]
        assert snapshot_path.read_bytes() == snapshot_before
        assert not outcome.state_changed

        restarted = FUGMHttpService(data_root=data_root, use_llm=False)
        restored = restarted._runtime("真实签发包回滚团")
        restored_world = restored.app.world_state.world_profile
        assert history not in restored_world.historical_events
        assert threat not in restored_world.world_threats
        assert [item["id"] for item in restored_world.pending_proposals] == [
            proposal_id
        ]
