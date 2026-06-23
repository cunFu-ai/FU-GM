import tempfile
import unittest
from pathlib import Path

from fu_gm.testing.legal_actions import LegalActionLayer
from fu_gm.testing.player_simulator import ConstrainedPlayerSimulator
from fu_gm.testing.replay_models import LegalActionContext, ReplayScenario, ReplayStep
from fu_gm.testing.replay_runner import HumanLikeReplayRunner
from fu_gm.testing.rule_glossary import FINAL_FABULA_GLOSSARY


class HumanLikeReplayFrameworkTests(unittest.TestCase):
    def test_rule_glossary_teaches_final_fabula_terms_to_player_layer(self) -> None:
        rendered = FINAL_FABULA_GLOSSARY.render_for_player_prompt(
            legal_actions=["推进目标命刻", "消耗物语点引入事实"]
        )

        self.assertIn("物语点", rendered)
        self.assertIn("目标命刻", rendered)
        self.assertIn("检定永远是两颗骰", rendered)
        self.assertIn("推进目标命刻", rendered)

    def test_player_simulator_rejects_unsupported_spell_and_uses_fallback(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="bad_spell",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            message="南星: 赛璃施放超级复活术，自动成功把大家回满。",
        )
        context = LegalActionContext(
            stage_goal="测试非法法术拦截",
            conflict_active=False,
            known_pcs=["赛璃"],
            legal_actions=["调查"],
            legal_spells=["治愈术"],
        )

        utterance = simulator.compose(step=step, legal_context=context)

        self.assertTrue(utterance.used_fallback)
        self.assertIn("unsupported_spell_claim", utterance.validation_errors)
        self.assertNotIn("超级复活术", utterance.text)

    def test_legal_action_layer_mentions_out_of_turn_limit(self) -> None:
        context = LegalActionContext(
            stage_goal="测试抢跑",
            current_actor="伊莉雅",
            conflict_active=True,
            known_pcs=["伊莉雅", "洛岚"],
            legal_actions=["回合外等待", "给当前行动者建议"],
            notes=["当前行动者是 伊莉雅，洛岚 不能结算消耗回合的行动。"],
        )

        rendered = LegalActionLayer().as_prompt_block(context)

        self.assertIn("当前行动者：伊莉雅", rendered)
        self.assertIn("回合外等待", rendered)
        self.assertIn("不能结算消耗回合", rendered)

    def test_minimal_replay_runner_writes_transcript_records_and_report(self) -> None:
        scenario = ReplayScenario.load(Path("tests/replay_scenarios/minimal_replay_smoke.json"))
        with tempfile.TemporaryDirectory() as tmp:
            runner = HumanLikeReplayRunner(scenario, output_root=tmp, use_llm_gm=False, use_llm_player=False)
            result = runner.run()

            self.assertTrue(result["ok"], result["errors"])
            self.assertTrue(Path(result["conversation_txt"]).exists())
            self.assertTrue(Path(result["records_jsonl"]).exists())
            self.assertTrue(Path(result["report_md"]).exists())
            transcript = Path(result["conversation_txt"]).read_text(encoding="utf-8")
            self.assertIn("玩家贡献世界细节", transcript)
            self.assertIn("白钟大陆", transcript)


if __name__ == "__main__":
    unittest.main()
