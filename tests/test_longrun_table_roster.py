from __future__ import annotations

import pytest

from fu_gm.http_server import FUGMHttpService
from fu_gm.testing.longrun_table_roster import (
    LongRunTableRoster,
    LongRunTableSeat,
    THREE_PLAYER_LONGRUN_ROSTER,
)


def test_default_longrun_roster_is_three_players_and_one_gm() -> None:
    roster = THREE_PLAYER_LONGRUN_ROSTER

    assert roster.gm_name == "时悠"
    assert roster.player_names == ("阿凛", "南星", "白河")
    assert roster.hero_names == ("伊莉雅", "赛璃", "洛岚")
    assert roster.player_to_hero == {
        "阿凛": "伊莉雅",
        "南星": "赛璃",
        "白河": "洛岚",
    }


def test_longrun_roster_rejects_duplicate_player_identity() -> None:
    first = THREE_PLAYER_LONGRUN_ROSTER.seats[0]

    with pytest.raises(ValueError, match="player_name 不能重复"):
        LongRunTableRoster(
            gm_name="时悠",
            seats=(
                first,
                LongRunTableSeat("pl-duplicate", first.persona),
            ),
        )


def test_three_player_roster_rejects_old_five_player_checkpoint() -> None:
    old_payload = THREE_PLAYER_LONGRUN_ROSTER.checkpoint_payload()
    old_payload["seats"] = [
        *old_payload["seats"],
        {
            "seat_id": "pl-04",
            "player_name": "时雨",
            "hero_name": "艾薇娅",
        },
        {
            "seat_id": "pl-05",
            "player_name": "澄砚",
            "hero_name": "苍祈",
        },
    ]

    with pytest.raises(ValueError, match="桌面名册与当前配置不同"):
        THREE_PLAYER_LONGRUN_ROSTER.assert_checkpoint_payload(old_payload)


def test_exact_roster_rejects_duplicate_runtime_participant() -> None:
    with pytest.raises(RuntimeError, match="重复=阿凛"):
        THREE_PLAYER_LONGRUN_ROSTER.assert_exact_players(
            ("阿凛", "阿凛", "南星", "白河"),
            source="运行时桌面",
        )


def test_session_zero_endpoint_uses_exact_structured_roster(tmp_path) -> None:
    service = FUGMHttpService(data_root=tmp_path, use_llm=False)
    payload = {
        "campaign_id": "three-player-table",
        "session_id": "session-zero",
        "channel_id": "test-channel",
        "participants": list(THREE_PLAYER_LONGRUN_ROSTER.player_names),
    }

    status, body = service.handle("POST", "/v1/session-zero/start", payload)

    assert status == 200
    assert isinstance(body, dict) and body["ok"] is True
    manager = service._runtime("three-player-table").app.session_zero_manager
    THREE_PLAYER_LONGRUN_ROSTER.assert_exact_players(
        (participant.name for participant in manager.state.participants),
        source="测试第零章",
    )
