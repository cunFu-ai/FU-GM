from __future__ import annotations

import time

from fu_gm.campaign_importer import CampaignChatLogImporter


class RecordingImportClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create_chat_completion(self, **kwargs: object) -> str:
        self.calls.append(dict(kwargs))
        return '{"summary":"迁移完成","world_updates":{"campaign_title":"旧团"}}'


def test_chat_log_import_has_short_bounded_non_thinking_model_call() -> None:
    client = RecordingImportClient()
    importer = CampaignChatLogImporter(
        client=client,
        model="deepseek-chat",
        model_timeout_seconds=1.0,
        max_output_tokens=1234,
    )
    outer_deadline = time.monotonic() + 5.0

    result = importer.extract(
        chat_log="玩家：战役标题：旧团",
        campaign_id="旧团",
        deadline=outer_deadline,
    )

    assert result.source == "llm"
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["operation"] == "campaign_chat_log_import"
    assert call["thinking_enabled"] is False
    assert call["max_tokens"] == 1234
    assert call["max_recovery_retries"] == 1
    assert call["retry_without_response_format_on_empty"] is True
    assert time.monotonic() < call["deadline"] <= outer_deadline


def test_expired_chat_log_import_deadline_uses_heuristic_without_model_call() -> None:
    client = RecordingImportClient()
    importer = CampaignChatLogImporter(
        client=client,
        model="deepseek-chat",
    )

    result = importer.extract(
        chat_log="玩家：战役标题：旧团",
        campaign_id="旧团",
        deadline=time.monotonic() - 1,
    )

    assert result.source == "heuristic"
    assert result.fallback_used is True
    assert client.calls == []
    assert any("deadline_budget_exhausted" in item for item in result.warnings)
