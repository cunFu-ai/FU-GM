from fu_gm.testing.longrun_diagnostics import (
    build_fatal_error_context,
    collect_error_contexts,
    format_error_contexts,
)


def test_error_context_keeps_recent_public_turns_and_failure_evidence() -> None:
    calls = [
        {
            "index": 1,
            "label": "玩家行动",
            "speaker": "阿凛",
            "message": "我检查门锁。",
            "reply": "门锁上有潮湿的符文。",
            "route": "/v1/message/route",
            "status": 200,
            "ok": True,
            "body": {"ok": True},
        },
        {
            "index": 2,
            "label": "玩家推进",
            "speaker": "南星",
            "message": "我试着解除符文。",
            "reply": "",
            "route": "/v1/message/route",
            "status": 200,
            "ok": False,
            "body": {
                "ok": False,
                "agent_error": "structured output invalid",
                "tool_receipts": [
                    {
                        "tool_name": "advance_clock",
                        "ok": False,
                        "state_changed": False,
                        "result": {
                            "error_code": "INVALID_ARGUMENT",
                            "message": "缺少命刻名称",
                            "private_prompt": "must not leak",
                        },
                    }
                ],
            },
            "llm_diagnostics": {
                "core_gm": {
                    "used_fallback": False,
                    "error": "structured output invalid",
                    "recovery_attempts": [],
                }
            },
        },
    ]

    contexts = collect_error_contexts(calls)

    assert len(contexts) == 1
    context = contexts[0]
    assert context["recent_public_context"][0]["message"] == "我检查门锁。"
    assert context["current_call"]["message"] == "我试着解除符文。"
    assert "failed_tool_receipt" in context["reasons"]
    assert context["failure"]["failed_tool_receipts"][0]["error_code"] == "INVALID_ARGUMENT"
    assert "private_prompt" not in str(context)


def test_error_context_records_recovered_provider_errors() -> None:
    contexts = collect_error_contexts(
        [
            {
                "index": 1,
                "label": "在线调用",
                "status": 200,
                "ok": True,
                "body": {"ok": True},
                "service_recovery_attempts": [
                    {"attempt": 1, "status": 503, "error": "busy"}
                ],
            }
        ]
    )

    assert contexts[0]["reasons"] == ["service_recovery"]


def test_error_context_ignores_explicitly_recovered_tool_rejection() -> None:
    contexts = collect_error_contexts(
        [
            {
                "index": 1,
                "label": "参数修正",
                "status": 200,
                "ok": True,
                "body": {
                    "ok": True,
                    "tool_receipts": [
                        {
                            "tool_name": "update_hero",
                            "ok": False,
                            "result": {"recovered_precondition": True},
                        },
                        {
                            "tool_name": "update_hero",
                            "ok": True,
                            "state_changed": True,
                        },
                    ],
                },
            }
        ]
    )

    assert contexts == []


def test_fatal_context_and_empty_text_are_readable() -> None:
    fatal = build_fatal_error_context(
        [],
        error=RuntimeError("boom"),
        traceback_text="traceback",
    )

    assert fatal["error_type"] == "RuntimeError"
    assert "boom" in format_error_contexts([fatal])
    assert "没有记录到" in format_error_contexts([])
