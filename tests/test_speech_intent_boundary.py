from fu_gm.components.speech_intent_boundary import SpeechIntentBoundary


def test_rejects_explicit_if_if_player_menu_when_options_are_forbidden() -> None:
    violation = SpeechIntentBoundary.violation(
        (
            "黄铜片贴地亮起冷白纹路。若打断黄铜片，风铃会把巡逻队引得更近；"
            "若任它继续，旅人会失去刚说出的方向感。"
        ),
        {"avoid": ["替玩家行动", "列出两三个选项"]},
    )

    assert "选项菜单" in violation


def test_allows_npc_action_and_visible_consequence_without_menu() -> None:
    violation = SpeechIntentBoundary.violation(
        (
            "财团使者把黄铜片推进门槛，冷白纹路立刻缠住旅人的影子。"
            "旅人刚说出的方向感正从记忆里被抽走。"
        ),
        {"avoid": ["替玩家行动", "列出两三个选项"]},
    )

    assert violation == ""
