import json

from fu_gm.context_compaction import StructuredContextCompactor


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
