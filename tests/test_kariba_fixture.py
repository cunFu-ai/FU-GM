import tempfile

from fu_gm.http_server import FUGMHttpService
from fu_gm.testing.kariba_fixture import (
    KARIBA_INVITATION,
    kariba_opening_probe_messages,
    seed_kariba_ready_campaign,
)


def test_kariba_fixture_is_ready_without_touching_a_real_campaign() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        runtime = seed_kariba_ready_campaign(
            service,
            campaign_id="kariba-test",
            session_id="session-1",
            channel_id="group-1",
        )

        readiness = service._adventure_readiness_snapshot(
            runtime,
            materialize_confirmed_characters=False,
        )
        transition = runtime.app.session_zero_manager.chapter_one_transition_status(
            ready=bool(readiness["ready"]),
        )

        assert readiness["ready"] is True
        assert transition["status"] == "invited"
        assert service._player_character_control_map(runtime) == {
            "测试玩家甲": ["诺艾尔"],
            "loading": ["艾丽妮"],
        }
        assert runtime.app.session_zero_manager.state.world.first_act_opening_equipment_restrictions == [
            {
                "actor": "诺艾尔",
                "items": ["钢匕首", "细剑"],
                "reason": "入狱时被守卫收缴",
                "location": "卡里巴村监狱值班室证物柜",
            },
            {
                "actor": "艾丽妮",
                "items": ["法杖", "魔典", "贤者之袍"],
                "reason": "入狱时被守卫收缴",
                "location": "卡里巴村监狱值班室证物柜",
            },
        ]
        transcript = runtime.log_manager.load_transcript("kariba-test", "session-1")
        assert transcript[-1].content == KARIBA_INVITATION


def test_kariba_probe_includes_table_talk_that_should_stay_silent() -> None:
    messages = kariba_opening_probe_messages()

    assert messages[0].reply_to_gm is True
    assert any(item.expectation == "silent" for item in messages)
    assert any("观察" in item.text for item in messages)
