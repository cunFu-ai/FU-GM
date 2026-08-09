from __future__ import annotations

from fu_gm.components.gm_batched_message_router import GMBatchedMessageRouter


class _Host:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    def _message_route(self, payload: dict[str, object]) -> dict[str, object]:
        self.payloads.append(dict(payload))
        if payload.get("message") == "切到旧团":
            return {
                "ok": True,
                "active_campaign_id": "旧团",
                "target": "fu_gm",
                "route": "casual",
                "reply": "切好了。",
            }
        return {
            "ok": True,
            "active_campaign_id": str(payload.get("campaign_id") or ""),
            "target": "silent",
            "route": "casual",
            "reply": "",
        }


def test_batch_is_one_semantic_turn_and_preserves_each_speaker() -> None:
    host = _Host()
    router = GMBatchedMessageRouter(host)

    result = router.route(
        {
            "campaign_id": "当前团",
            "session_id": "s1",
            "channel_id": "group-1",
            "batch_id": "batch-1",
        },
        [
            {
                "speaker": "阿凛",
                "message": "先等等",
                "payload": {"speaker_id": "user-a", "message_id": "m1"},
            },
            {
                "speaker": "白河",
                "message": "切到旧团",
                "payload": {"speaker_id": "user-b", "message_id": "m2"},
            },
            {
                "speaker": "南星",
                "message": "看看当前场景",
                "payload": {"speaker_id": "user-c", "message_id": "m3"},
            },
        ],
    )

    assert len(host.payloads) == 1
    routed = host.payloads[0]
    assert routed["campaign_id"] == "当前团"
    assert routed["speaker"] == "南星"
    assert routed["message"] == "看看当前场景"
    turn_messages = routed["current_turn_messages"]
    assert [item["speaker"] for item in turn_messages] == ["阿凛", "白河", "南星"]
    assert [item["speaker_id"] for item in turn_messages] == ["user-a", "user-b", "user-c"]
    assert turn_messages[0]["batch_index"] == 1
    assert turn_messages[2]["batch_has_later_messages"] is False
    assert result["batch_count"] == 3
    assert "single_semantic_turn" in result["decision"]["tags"]
