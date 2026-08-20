import json

from fu_gm.context_compaction import StructuredContextCompactor


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


def _request_with_runtime_feedback() -> dict:
    return {
        "current_message": "当前消息",
        "current_turn": {
            "events": [{"event_id": "event-current", "text": "执行当前行动"}]
        },
        "session": {"campaign_id": "campaign"},
        "request_context": {"directly_addressed": True},
        "runtime_feedback": _runtime_feedback_near_hard_limit(),
        "current_state_summary": {},
        "available_tools": [],
        "history": [],
        "optional_noise": ["旧资料" * 1000 for _ in range(40)],
    }


def _large_request() -> dict:
    return {
        "current_message": "伊莉雅把风铃交给会长，但还没有松手。",
        "current_turn": {
            "events": [
                {
                    "speaker": "阿凛",
                    "text": "伊莉雅把风铃交给会长，但还没有松手。",
                    "event_id": "event-current",
                }
            ]
        },
        "session": {"campaign_id": "campaign", "speaker": "阿凛"},
        "request_context": {"directly_addressed": True},
        "current_state_summary": {
            f"state_{index}": "旧状态" * 200 for index in range(60)
        },
        "available_tools": [
            {
                "name": f"tool_{index}",
                "description": "工具说明" * 100,
                "side_effect": "write",
                "parameters": {
                    "target": {
                        "type": "string",
                        "required": True,
                        "description": "目标说明" * 40,
                    }
                },
            }
            for index in range(12)
        ],
        "history": [
            {"receipt": index, "message": "旧回执" * 100}
            for index in range(20)
        ],
    }


def test_structured_compaction_preserves_current_transaction_and_valid_json() -> None:
    source = json.dumps(_large_request(), ensure_ascii=False)

    result = StructuredContextCompactor().compact(source, max_chars=9000)
    compacted = json.loads(result.text)

    assert len(result.text) <= 9000
    assert result.strategy.startswith("structured-")
    assert compacted["current_message"] == "伊莉雅把风铃交给会长，但还没有松手。"
    assert compacted["current_turn"]["events"][-1]["event_id"] == "event-current"
    assert compacted["available_tools"][0]["name"] == "tool_0"
    assert compacted["history"][-1]["receipt"] == 19
    assert compacted["_fu_gm_context_compaction"]["applied"] is True


def test_non_json_context_is_left_for_plain_text_fallback() -> None:
    result = StructuredContextCompactor().compact("开头" + "很长" * 1000, max_chars=400)

    assert result.strategy == "not-json"
    assert len(result.text) > 400


def test_recovery_compaction_never_truncates_protected_transaction_fields() -> None:
    request = _large_request()
    request["current_message"] = "当前玩家原话" * 500
    request["current_turn"] = {
        "events": [
            {
                "event_id": "event-current",
                "speaker": "阿凛",
                "text": "本批次行动不能被截断" * 200,
            }
        ]
    }
    request["available_tools"] = [
        {
            "name": "resolve_rule_window",
            "description": "完整工具契约" * 200,
            "parameters": {"window_id": {"type": "string", "required": True}},
        }
    ]
    source = json.dumps(request, ensure_ascii=False)

    result = StructuredContextCompactor().compact(source, max_chars=1200)
    compacted = json.loads(result.text)

    assert compacted["current_message"] == request["current_message"]
    assert compacted["current_turn"] == request["current_turn"]
    assert compacted["available_tools"][0]["name"] == "resolve_rule_window"
    assert (
        compacted["available_tools"][0]["parameters"]
        == request["available_tools"][0]["parameters"]
    )
    assert len(compacted["available_tools"][0]["description"]) < len(
        request["available_tools"][0]["description"]
    )
    assert result.strategy.endswith("protected-over-budget")
    assert len(result.text) > 1200


def test_runtime_feedback_survives_balanced_and_emergency_compaction() -> None:
    request = _request_with_runtime_feedback()
    source = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    compactor = StructuredContextCompactor()

    for max_chars, expected_strategy in (
        (24000, "structured-balanced"),
        (5000, "structured-emergency"),
    ):
        result = compactor.compact(source, max_chars=max_chars)
        compacted = json.loads(result.text)

        assert result.strategy == expected_strategy
        assert len(result.text) <= max_chars
        assert compacted["runtime_feedback"] == request["runtime_feedback"]


def test_runtime_feedback_survives_absolute_minimum_compaction() -> None:
    class AbsolutePathCompactor(StructuredContextCompactor):
        _PROFILES = ()

        def _emergency_projection(
            self,
            value: object,
            *,
            max_chars: int,
        ) -> dict[str, object]:
            projected = super()._emergency_projection(value, max_chars=max_chars)
            projected["emergency_only_padding"] = "x" * 300
            return projected

    request = _request_with_runtime_feedback()
    source = json.dumps(request, ensure_ascii=False, separators=(",", ":"))

    result = AbsolutePathCompactor().compact(source, max_chars=4500)
    compacted = json.loads(result.text)

    assert result.strategy == "structured-absolute-minimum"
    assert len(result.text) <= 4500
    assert compacted["runtime_feedback"] == request["runtime_feedback"]
