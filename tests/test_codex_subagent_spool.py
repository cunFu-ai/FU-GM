from __future__ import annotations

import json
import threading
import time

import pytest

from fu_gm.llm_client import ChatMessage
from fu_gm.config import ImageGenerationConfig, LLMConfig
from fu_gm.http_server import FUGMHttpService
from fu_gm.testing.codex_subagent_spool import CodexSubagentSpoolClient
from fu_gm.llm_client_bundle import (
    TestLLMClientBundle,
    require_test_llm_bundle,
)


def test_codex_spool_requires_explicit_test_mode(tmp_path) -> None:
    with pytest.raises(ValueError, match="test_only"):
        CodexSubagentSpoolClient(tmp_path)


def test_codex_spool_round_trip_uses_matching_response(tmp_path) -> None:
    client = CodexSubagentSpoolClient(
        tmp_path,
        timeout_seconds=2,
        poll_interval_seconds=0.01,
        test_only=True,
    )
    result: dict[str, str] = {}

    def invoke() -> None:
        result["content"] = client.create_chat_completion(
            model="gpt-5.6-terra",
            messages=[ChatMessage(role="user", content="只输出JSON。")],
            response_format={"type": "json_object"},
            operation="codex_spool_test",
        )

    worker = threading.Thread(target=invoke)
    worker.start()
    request_path = None
    for _ in range(100):
        candidates = list((tmp_path / "requests").glob("*.json"))
        if candidates:
            request_path = candidates[0]
            break
        time.sleep(0.01)
    assert request_path is not None
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["response_format"] == {"type": "json_object"}
    assert request["output_contract"]["kind"] == "component_completion"
    assert request["output_contract"]["json_required"] is True
    response_path = tmp_path / "responses" / f"{request['request_id']}.json"
    response_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "request_id": request["request_id"],
                "request_payload_sha256": request["payload_sha256"],
                "status": "completed",
                "provider": "codex_subagent",
                "worker_id": "test-worker",
                "content": '{"decision":"final","reply":"在。"}',
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result["content"] == '{"decision":"final","reply":"在。"}'
    assert client.telemetry_payload()["completed_calls"] == 1


def test_codex_spool_marks_player_protocol_without_inventing_tool_requirement(
    tmp_path,
) -> None:
    client = CodexSubagentSpoolClient(tmp_path, test_only=True)

    contract = client._output_contract(
        "fu_pl.generate",
        response_format={"type": "json_object"},
    )

    assert contract["kind"] == "player_simulator_decision"
    assert contract["tool_calls_allowed"] is False
    assert "speak表示玩家发言" in contract["note"]


def test_codex_spool_empty_response_format_is_not_a_json_contract(tmp_path) -> None:
    client = CodexSubagentSpoolClient(tmp_path, test_only=True)

    contract = client._output_contract(
        "plain_component",
        response_format={},
    )

    assert contract["json_required"] is False


def test_codex_spool_telemetry_does_not_count_waiting_as_failure(tmp_path) -> None:
    client = CodexSubagentSpoolClient(tmp_path, test_only=True)
    client.calls.extend(
        [
            {"status": "completed"},
            {"status": "waiting"},
            {"status": "failed"},
        ]
    )

    telemetry = client.telemetry_payload()

    assert telemetry["total_calls"] == 3
    assert telemetry["completed_calls"] == 1
    assert telemetry["pending_calls"] == 1
    assert telemetry["failed_calls"] == 1


def test_test_llm_bundle_rejects_unmarked_object() -> None:
    with pytest.raises(ValueError, match="test_only"):
        require_test_llm_bundle(object())


def test_codex_bundle_reaches_every_service_llm_role(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CodexSubagentSpoolClient(
        tmp_path / "spool",
        timeout_seconds=2,
        poll_interval_seconds=0.01,
        test_only=True,
    )
    bundle = TestLLMClientBundle.shared(client)
    monkeypatch.setenv("FU_GM_WORLD_MAP_RENDERER", "image")
    monkeypatch.setattr(
        LLMConfig,
        "from_env",
        classmethod(lambda _cls: (_ for _ in ()).throw(AssertionError("dotenv read"))),
    )
    monkeypatch.setattr(
        ImageGenerationConfig,
        "from_env",
        classmethod(lambda _cls: (_ for _ in ()).throw(AssertionError("image dotenv read"))),
    )
    service = FUGMHttpService(
        data_root=tmp_path / "campaigns",
        use_llm=True,
        test_llm_bundle=bundle,
    )
    runtime = service._runtime("codex-probe")
    app = runtime.app

    assert service.gm_agent_runtime.llm_client is client
    assert service.gm_tool_agent is not None
    assert service.gm_tool_agent.client is client
    assert service.gm_tool_agent._decision_requester.client is client
    assert service.gm_tool_agent.reply_grounding_verifier.client is client
    assert app.llm_client is client
    assert app.expressor.client is client
    assert app.npc_blueprint_designer.client is client
    concretizer = app.campaign_pacing_manager.contract_planner.concretizer
    assert concretizer.client is client
    assert concretizer.reachability_reviewer.client is client
    assert runtime.log_manager.summarizer.client is client
    assert service._chat_log_importer().client is client


def test_codex_spool_matches_two_concurrent_responses_by_request_id(tmp_path) -> None:
    client = CodexSubagentSpoolClient(
        tmp_path,
        timeout_seconds=3,
        poll_interval_seconds=0.01,
        test_only=True,
    )
    results: dict[str, str] = {}

    def invoke(name: str) -> None:
        results[name] = client.create_chat_completion(
            model="gpt-5.6-terra",
            messages=[ChatMessage(role="user", content=name)],
            operation=f"concurrent.{name}",
        )

    workers = [
        threading.Thread(target=invoke, args=(name,))
        for name in ("甲", "乙")
    ]
    for worker in workers:
        worker.start()

    requests: dict[str, dict[str, object]] = {}
    for _ in range(200):
        for path in (tmp_path / "requests").glob("*.json"):
            request = json.loads(path.read_text(encoding="utf-8"))
            content = str(request["messages"][-1]["content"])
            requests[content] = request
        if len(requests) == 2:
            break
        time.sleep(0.01)
    assert set(requests) == {"甲", "乙"}

    for name in ("乙", "甲"):
        request_id = str(requests[name]["request_id"])
        response_path = tmp_path / "responses" / f"{request_id}.json"
        response_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "request_id": request_id,
                    "request_payload_sha256": requests[name]["payload_sha256"],
                    "status": "completed",
                    "provider": "codex_subagent",
                    "worker_id": f"worker-{name}",
                    "content": f"回应{name}",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    for worker in workers:
        worker.join(timeout=3)
        assert not worker.is_alive()
    assert results == {"甲": "回应甲", "乙": "回应乙"}


def test_codex_spool_quarantines_wrong_digest_then_accepts_correction(tmp_path) -> None:
    client = CodexSubagentSpoolClient(
        tmp_path,
        timeout_seconds=2,
        poll_interval_seconds=0.01,
        test_only=True,
    )
    result: dict[str, str] = {}

    def invoke() -> None:
        result["content"] = client.create_chat_completion(
            model="codex-subagent-test",
            messages=[ChatMessage(role="user", content="测试请求摘要")],
        )

    worker = threading.Thread(target=invoke)
    worker.start()
    request_path = None
    for _ in range(100):
        candidates = list((tmp_path / "requests").glob("*.json"))
        if candidates:
            request_path = candidates[0]
            break
        time.sleep(0.01)
    assert request_path is not None
    request = json.loads(request_path.read_text(encoding="utf-8"))
    (tmp_path / "responses" / f"{request['request_id']}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "request_id": request["request_id"],
                "request_payload_sha256": "not-the-request-hash",
                "status": "completed",
                "provider": "codex_subagent",
                "worker_id": "wrong-worker",
                "content": "不应被接受",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    for _ in range(100):
        if list((tmp_path / "invalid").glob("*.json")):
            break
        time.sleep(0.01)
    assert len(list((tmp_path / "invalid").glob("*.json"))) == 1

    (tmp_path / "responses" / f"{request['request_id']}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "request_id": request["request_id"],
                "request_payload_sha256": request["payload_sha256"],
                "status": "completed",
                "provider": "codex_subagent",
                "worker_id": "corrected-worker",
                "content": "更正后的响应",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result["content"] == "更正后的响应"
    call = client.telemetry_payload()["calls"][0]
    assert call["status"] == "completed"
    assert call["invalid_response_count"] == 1
    assert "请求摘要" in call["last_invalid_response_error"]
