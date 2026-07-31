from fu_gm.components.episode_momentum_tracker import EpisodeMomentumTracker
from fu_gm.models import SessionEpisodeProgress


def test_repeated_actions_raise_stagnation_faster_until_world_changes() -> None:
    progress = SessionEpisodeProgress()

    EpisodeMomentumTracker.observe_player_action(
        progress,
        action_summary="洛岚把木柜推到门口挡住巡逻队视线。",
        material_change=False,
    )
    EpisodeMomentumTracker.observe_player_action(
        progress,
        action_summary="洛岚继续用木柜挡住门口的巡逻视线。",
        material_change=False,
    )

    assert progress.stagnant_player_turns >= 3
    assert len(progress.recent_action_signatures) == 2

    EpisodeMomentumTracker.observe_player_action(
        progress,
        action_summary="赛璃打开后院通道。",
        material_change=True,
    )

    assert progress.stagnant_player_turns == 0
