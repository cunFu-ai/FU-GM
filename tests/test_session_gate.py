import tempfile
import unittest

from fu_gm.session_gate import SessionGateManager


class SessionGateManagerTests(unittest.TestCase):
    def test_detects_start_pause_end_signals(self) -> None:
        manager = SessionGateManager()
        self.assertEqual(manager.detect_signal("开始第零章吧").status, "session_zero")
        self.assertEqual(manager.detect_signal("开启最终物语第零章").status, "session_zero")
        self.assertEqual(manager.detect_signal("今晚开团").status, "pre_session")
        self.assertEqual(manager.detect_signal("开启最终物语跑团").status, "pre_session")
        self.assertEqual(manager.detect_signal("最终物语跑团开始啦").status, "pre_session")
        self.assertEqual(manager.detect_signal("继续跑团").status, "adventure")
        self.assertEqual(manager.detect_signal("先暂停一下").kind, "pause")
        self.assertEqual(manager.detect_signal("今天到这里，收团").kind, "end")

    def test_in_fiction_opening_a_mouth_near_a_corporation_is_not_starting_a_campaign(self) -> None:
        manager = SessionGateManager()

        signal = manager.detect_signal(
            "艾薇娅先不顶撞，我想直接朝艾蕾娜开口：你们财团要记录什么异常？",
            current_status="adventure",
        )

        self.assertIsNone(signal)

    def test_natural_start_phrase_still_works_before_a_campaign(self) -> None:
        manager = SessionGateManager()

        signal = manager.detect_signal("我们想开一场最终物语", current_status="inactive")
        quoted = manager.detect_signal("我们准备开一场《最终物语》", current_status="inactive")

        self.assertIsNotNone(signal)
        self.assertEqual(signal.status, "pre_session")
        self.assertIsNotNone(quoted)
        self.assertEqual(quoted.status, "pre_session")

    def test_adventure_cannot_be_downgraded_by_natural_setup_words(self) -> None:
        manager = SessionGateManager()

        self.assertIsNone(
            manager.detect_signal("我们顺路聊聊开团前共识里的基调", current_status="adventure")
        )
        self.assertIsNone(
            manager.detect_signal("重新开始第零章吧", current_status="adventure")
        )
        self.assertEqual(
            manager.detect_signal("继续跑团", current_status="adventure").status,
            "adventure",
        )

    def test_persists_gate_state_by_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SessionGateManager(tmpdir)
            state = manager.activate("星尘团", "group-1", "s1", status="adventure", reason="测试")
            self.assertEqual(state.status, "adventure")

            restored = SessionGateManager(tmpdir).get("星尘团", "group-1", "s1")
            self.assertEqual(restored.status, "adventure")
            self.assertEqual(restored.reason, "测试")

            paused = manager.pause("星尘团", "group-1", "s1", reason="中场")
            self.assertEqual(paused.status, "paused")

            inactive = manager.deactivate("星尘团", "group-1", "s1", reason="收团")
            self.assertEqual(inactive.status, "inactive")

    def test_reactivation_on_same_channel_updates_active_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SessionGateManager(tmpdir)
            manager.activate("星尘团", "group-1", "session-zero", status="session_zero")

            state = manager.activate("星尘团", "group-1", "session-01", status="adventure")
            restored = SessionGateManager(tmpdir).get("星尘团", "group-1", "session-01")

            self.assertEqual(state.session_id, "session-01")
            self.assertEqual(restored.session_id, "session-01")
            self.assertEqual(restored.status, "adventure")


if __name__ == "__main__":
    unittest.main()
