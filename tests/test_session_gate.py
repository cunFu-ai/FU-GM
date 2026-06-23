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


if __name__ == "__main__":
    unittest.main()
