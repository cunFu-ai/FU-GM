import json
import os
import unittest

from fu_gm.action_brain import LLMActionBrain
from fu_gm.config import LLMConfig
from fu_gm.expressor import LLMExpressor
from fu_gm.llm_client import ChatMessage, LLMHTTPError, OpenAICompatibleClient
from fu_gm.models import Action, ActionResolution, ActionType, GamePanel, RollOutcome, SessionZeroState
from fu_gm.session_zero_facilitator import LLMSessionZeroFacilitator


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post_json(self, url, headers, payload, timeout):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout": timeout,
            }
        )
        content = self.responses.pop(0)
        if isinstance(content, BaseException):
            raise content
        return {"choices": [{"message": {"content": content}}]}


class LLMIntegrationTests(unittest.TestCase):
    def test_llm_config_enables_heuristic_fallback_by_default(self) -> None:
        old_dotenv = os.environ.get("FU_GM_DOTENV_PATH")
        old_fallback = os.environ.get("FU_GM_ALLOW_HEURISTIC_FALLBACK")
        try:
            os.environ["FU_GM_DOTENV_PATH"] = "__missing_fu_gm_test_env__"
            os.environ.pop("FU_GM_ALLOW_HEURISTIC_FALLBACK", None)
            self.assertTrue(LLMConfig.from_env().allow_heuristic_fallback)

            os.environ["FU_GM_ALLOW_HEURISTIC_FALLBACK"] = "0"
            self.assertFalse(LLMConfig.from_env().allow_heuristic_fallback)
        finally:
            if old_dotenv is None:
                os.environ.pop("FU_GM_DOTENV_PATH", None)
            else:
                os.environ["FU_GM_DOTENV_PATH"] = old_dotenv
            if old_fallback is None:
                os.environ.pop("FU_GM_ALLOW_HEURISTIC_FALLBACK", None)
            else:
                os.environ["FU_GM_ALLOW_HEURISTIC_FALLBACK"] = old_fallback

    def test_action_brain_uses_gpt_5_4_nano_model(self) -> None:
        transport = FakeTransport(
            [
                json.dumps(
                    {
                        "action_type": "RequestRoll",
                        "parameters": {
                            "actor": "瓦莉亚",
                            "attributes": ["DEX", "MIG"],
                            "target": "帝国机甲",
                            "target_number": 12,
                            "damage_type": "lightning",
                            "reasoning": "玩家发起攻击，需要检定。",
                            "in_mind_reply": "雷光在剑刃上跃动。",
                        },
                    },
                    ensure_ascii=False,
                )
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.apiyi.com",
            api_key="test-key",
            action_model="gpt-5.4-nano",
            expressor_model="gpt-5.4-nano",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        brain = LLMActionBrain(client=client, model=config.action_model)

        action = brain.decide(
            GamePanel(
                game_phase="冲突场景",
                active_clocks=["[炸毁桥梁] 2/6"],
                pc_status=["瓦莉亚: HP 15/45, MP 20/30, 物语点 2"],
                enemy_status=["帝国机甲: HP 60/100"],
                recent_chat="玩家[瓦莉亚]: 我要用雷电魔法攻击机甲！",
            )
        )

        self.assertEqual(action.action_type, ActionType.REQUEST_ROLL)
        self.assertEqual(transport.calls[0]["payload"]["model"], "gpt-5.4-nano")
        self.assertTrue(transport.calls[0]["url"].endswith("/v1/chat/completions"))

    def test_action_brain_postprocesses_ritual_contribution_misroute(self) -> None:
        transport = FakeTransport(
            [
                json.dumps(
                    {
                        "action_type": "AdvanceClock",
                        "parameters": {
                            "clock_name": "财团巡逻队逼近",
                            "delta": 1,
                        },
                    },
                    ensure_ascii=False,
                )
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.apiyi.com",
            api_key="test-key",
            action_model="gpt-5.4-nano",
            expressor_model="gpt-5.4-nano",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        brain = LLMActionBrain(client=client, model=config.action_model)

        action = brain.decide(
            GamePanel(
                game_phase="标准场景",
                active_clocks=["仪式：风铃回声 1/4"],
                pc_status=["洛岚: HP 40/40, MP 40/40"],
                enemy_status=[],
                recent_chat=(
                    "公开上下文：命刻【财团巡逻队逼近】3/6。\n\n"
                    "当前玩家输入（只把这一段当作本轮新行动；上方内容是已公开上下文）：\n"
                    "白河: 洛岚协助推进仪式命刻【仪式：风铃回声】，用洞察+敏捷调整旧钟的共鸣。"
                ),
                current_actor="洛岚",
            )
        )

        self.assertEqual(action.action_type, ActionType.CONTRIBUTE_RITUAL)
        self.assertEqual(action.parameters["actor"], "洛岚")
        self.assertEqual(action.parameters["clock_name"], "仪式：风铃回声")
        self.assertEqual(action.parameters["attributes"], ["INS", "DEX"])

    def test_action_brain_postprocess_prefers_explicit_chat_actor_over_llm_actor(self) -> None:
        transport = FakeTransport(
            [
                json.dumps(
                    {
                        "action_type": "ContributeRitual",
                        "parameters": {
                            "actor": "伊莉雅",
                            "clock_name": "仪式：风铃回声",
                            "attributes": ["INS", "DEX"],
                            "reasoning": "协助推进仪式。",
                            "in_mind_reply": "仪式的回声被新的手势接住。",
                        },
                    },
                    ensure_ascii=False,
                )
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.apiyi.com",
            api_key="test-key",
            action_model="gpt-5.4-nano",
            expressor_model="gpt-5.4-nano",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        brain = LLMActionBrain(client=client, model=config.action_model)

        action = brain.decide(
            GamePanel(
                game_phase="标准场景",
                active_clocks=["仪式：风铃回声 2/4"],
                pc_status=["伊莉雅: HP 60/60", "洛岚: HP 50/50"],
                enemy_status=[],
                recent_chat="当前玩家输入\n白河: 洛岚协助推进仪式命刻【仪式：风铃回声】，用洞察+敏捷调整旧钟。",
                current_actor="伊莉雅",
            )
        )

        self.assertEqual(action.action_type, ActionType.CONTRIBUTE_RITUAL)
        self.assertEqual(action.parameters["actor"], "洛岚")

    def test_action_brain_postprocesses_ritual_plan_from_current_input_only(self) -> None:
        transport = FakeTransport(
            [
                json.dumps(
                    {
                        "action_type": "AdvanceClock",
                        "parameters": {"clock_name": "财团巡逻队逼近", "delta": 1},
                    },
                    ensure_ascii=False,
                )
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.apiyi.com",
            api_key="test-key",
            action_model="gpt-5.4-nano",
            expressor_model="gpt-5.4-nano",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        brain = LLMActionBrain(client=client, model=config.action_model)

        action = brain.decide(
            GamePanel(
                game_phase="标准场景",
                active_clocks=["财团巡逻队逼近 1/6"],
                pc_status=["赛璃: HP 40/40, MP 80/80"],
                enemy_status=[],
                recent_chat=(
                    "公开上下文：命刻【财团巡逻队逼近】1/6。\n\n"
                    "当前玩家输入（只把这一段当作本轮新行动；上方内容是已公开上下文）：\n"
                    "南星: 赛璃计划一个御魂仪式【风铃回声】：学科御魂，效力轻微，范围小范围。"
                ),
                current_actor="赛璃",
            )
        )

        self.assertEqual(action.action_type, ActionType.PLAN_RITUAL)
        self.assertEqual(action.parameters["caster"], "赛璃")
        self.assertEqual(action.parameters["name"], "风铃回声")

    def test_action_brain_postprocesses_objective_clock_misroute(self) -> None:
        transport = FakeTransport(
            [
                json.dumps(
                    {
                        "action_type": "ContributeRitual",
                        "parameters": {
                            "actor": "洛岚",
                            "clock_name": "仪式：旧路闸门开启",
                            "attributes": ["INS", "WLP"],
                        },
                    },
                    ensure_ascii=False,
                )
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.apiyi.com",
            api_key="test-key",
            action_model="gpt-5.4-nano",
            expressor_model="gpt-5.4-nano",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        brain = LLMActionBrain(client=client, model=config.action_model)

        action = brain.decide(
            GamePanel(
                game_phase="冲突场景",
                active_clocks=["旧路闸门开启 0/6"],
                pc_status=["洛岚: HP 40/40, MP 40/40"],
                enemy_status=["财团机兵: HP 48/60"],
                recent_chat="洛岚推进目标命刻【旧路闸门开启】，用洞察+敏捷拆开驿站旧闸门的财团封锁。",
                current_actor="洛岚",
            )
        )

        self.assertEqual(action.action_type, ActionType.OBJECTIVE)
        self.assertEqual(action.parameters["actor"], "洛岚")
        self.assertEqual(action.parameters["clock_name"], "旧路闸门开启")
        self.assertEqual(action.parameters["attributes"], ["INS", "DEX"])

    def test_action_brain_postprocesses_threat_clock_to_advance_clock(self) -> None:
        transport = FakeTransport(
            [
                json.dumps(
                    {
                        "action_type": "ContributeRitual",
                        "parameters": {"actor": "财团机兵", "clock_name": "仪式：风铃回声"},
                    },
                    ensure_ascii=False,
                )
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.apiyi.com",
            api_key="test-key",
            action_model="gpt-5.4-nano",
            expressor_model="gpt-5.4-nano",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        brain = LLMActionBrain(client=client, model=config.action_model)

        action = brain.decide(
            GamePanel(
                game_phase="冲突场景",
                active_clocks=["财团巡逻队逼近 0/6", "仪式：风铃回声 1/4"],
                pc_status=["伊莉雅: HP 43/60"],
                enemy_status=["财团机兵: HP 48/60"],
                recent_chat="财团机兵推进威胁命刻【财团巡逻队逼近】，它向远处发出红色信号。",
                current_actor="财团机兵",
            )
        )

        self.assertEqual(action.action_type, ActionType.ADVANCE_CLOCK)
        self.assertEqual(action.parameters["clock_name"], "财团巡逻队逼近")
        self.assertEqual(action.parameters["delta"], 1)
        self.assertEqual(action.parameters["clock_type"], "threat")

    def test_action_brain_postprocesses_ritual_cast_misroute(self) -> None:
        transport = FakeTransport(
            [
                json.dumps(
                    {
                        "action_type": "Narrate",
                        "parameters": {"summary": "赛璃继续收束风铃的回声。"},
                    },
                    ensure_ascii=False,
                )
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.apiyi.com",
            api_key="test-key",
            action_model="gpt-5.4-nano",
            expressor_model="gpt-5.4-nano",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        brain = LLMActionBrain(client=client, model=config.action_model)

        action = brain.decide(
            GamePanel(
                game_phase="标准场景",
                active_clocks=["仪式：风铃回声 1/4"],
                pc_status=["赛璃: HP 40/40, MP 80/80"],
                enemy_status=[],
                recent_chat="赛璃尝试完成仪式【风铃回声】。如果仪式命刻还没完成，请明确告诉我还差多少格。",
                current_actor="赛璃",
            )
        )

        self.assertEqual(action.action_type, ActionType.CAST_RITUAL)
        self.assertEqual(action.parameters["actor"], "赛璃")
        self.assertEqual(action.parameters["clock_name"], "仪式：风铃回声")
        self.assertTrue(action.parameters["require_completed_clock"])

    def test_action_brain_does_not_use_heuristic_fallback_by_default(self) -> None:
        transport = FakeTransport([RuntimeError("model unavailable")])
        config = LLMConfig(
            api_base_url="https://api.apiyi.com",
            api_key="test-key",
            action_model="gpt-5.4-nano",
            expressor_model="gpt-5.4-nano",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        brain = LLMActionBrain(client=client, model=config.action_model)

        with self.assertRaisesRegex(RuntimeError, "heuristic fallback is disabled"):
            brain.decide(
                GamePanel(
                    game_phase="冲突场景",
                    active_clocks=[],
                    pc_status=["瓦莉亚: HP 15/45"],
                    enemy_status=["帝国机甲: HP 60/100"],
                    recent_chat="我要攻击。",
                )
            )

        self.assertFalse(brain.last_used_fallback)
        self.assertIn("model unavailable", brain.last_error)

    def test_action_brain_retries_malformed_structured_output(self) -> None:
        transport = FakeTransport(
            [
                "{}",
                json.dumps(
                    {"action_type": "Narrate", "parameters": {"summary": "等待玩家补齐角色卡。"}},
                    ensure_ascii=False,
                ),
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.apiyi.com",
            api_key="test-key",
            action_model="gpt-5.4-nano",
            expressor_model="gpt-5.4-nano",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        brain = LLMActionBrain(client=client, model=config.action_model)

        action = brain.decide(
            GamePanel(
                game_phase="普通场景",
                active_clocks=[],
                pc_status=[],
                enemy_status=[],
                recent_chat="开始第一章。",
            )
        )

        self.assertEqual(action.action_type, ActionType.NARRATE)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(len(brain.last_recovery_attempts), 1)
        self.assertTrue(brain.last_recovery_attempts[0]["recovered"])
        self.assertEqual(len(brain.recent_recoveries), 1)
        self.assertIn("结构化输出错误恢复", json.dumps(transport.calls[1]["payload"], ensure_ascii=False))

    def test_session_zero_retries_malformed_structured_output(self) -> None:
        transport = FakeTransport(
            [
                '{"message":""}',
                json.dumps(
                    {
                        "message": "先从大陆上最醒目的地点开始共创。",
                        "stage": "tone",
                        "accepted_facts": [],
                        "suggestions": [],
                        "questions": ["你最先看见哪里？"],
                        "world_updates": {},
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.apiyi.com",
            api_key="test-key",
            action_model="gpt-5.4-nano",
            expressor_model="gpt-5.4-nano",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        facilitator = LLMSessionZeroFacilitator(client=client, model=config.action_model)

        response = facilitator.opening(SessionZeroState())

        self.assertIn("最醒目的地点", response.message)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(len(facilitator.last_recovery_attempts), 1)
        self.assertTrue(facilitator.last_recovery_attempts[0]["recovered"])
        self.assertEqual(len(facilitator.recent_recoveries), 1)
        self.assertEqual(facilitator.last_error, "")
        self.assertIn("结构化输出错误恢复", json.dumps(transport.calls[1]["payload"], ensure_ascii=False))

    def test_expressor_uses_gpt_5_4_nano_model(self) -> None:
        transport = FakeTransport(["【战斗结算】雷光炸裂，帝国机甲被命中弱点。"])
        config = LLMConfig(
            api_base_url="https://api.apiyi.com",
            api_key="test-key",
            action_model="gpt-5.4-nano",
            expressor_model="gpt-5.4-nano",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        expressor = LLMExpressor(client=client, model=config.expressor_model)

        text = expressor.render(
            ActionResolution(
                action=Action(
                    action_type=ActionType.REQUEST_ROLL,
                    parameters={"in_mind_reply": "雷鸣撕裂空气。"},
                ),
                rules_text="检定成功并造成伤害。",
                payload={
                    "roll": RollOutcome(
                        actor="瓦莉亚",
                        attributes=["DEX", "MIG"],
                        dice=[(8, 7), (10, 7)],
                        total=14,
                        modifier=0,
                        high_roll=7,
                        target_number=12,
                        success=True,
                        critical_success=False,
                        fumble=False,
                        target="帝国机甲",
                        damage=24,
                        damage_type="lightning",
                        hp_after=36,
                    )
                },
            )
        )

        self.assertIn("【战斗结算】", text)
        self.assertEqual(transport.calls[0]["payload"]["model"], "gpt-5.4-nano")

    def test_client_passes_deepseek_reasoning_and_thinking_options(self) -> None:
        transport = FakeTransport(["你好，英雄。"])
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-pro",
            expressor_model="deepseek-v4-pro",
            reasoning_effort="high",
            thinking_enabled=True,
        )
        client = OpenAICompatibleClient(config, transport=transport)

        content = client.create_chat_completion(
            model=config.action_model,
            messages=[],
            temperature=0.1,
        )

        payload = transport.calls[0]["payload"]
        self.assertEqual(content, "你好，英雄。")
        self.assertEqual(payload["model"], "deepseek-v4-pro")
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(transport.calls[0]["url"], "https://api.deepseek.com/chat/completions")

    def test_deepseek_base_url_uses_documented_chat_completions_path(self) -> None:
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-pro",
            expressor_model="deepseek-v4-pro",
        )

        self.assertEqual(config.chat_completions_url(), "https://api.deepseek.com/chat/completions")

    def test_client_reactive_compacts_and_retries_context_errors(self) -> None:
        transport = FakeTransport(
            [
                LLMHTTPError(status_code=413, body='{"error":{"code":"request_too_large"}}'),
                "压缩后成功。",
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-pro",
            expressor_model="deepseek-v4-pro",
            reactive_recovery_target_chars=1200,
        )
        client = OpenAICompatibleClient(config, transport=transport)

        content = client.create_chat_completion(
            model=config.action_model,
            messages=[
                ChatMessage(role="system", content="稳定系统提示"),
                ChatMessage(role="user", content="开头" + ("很长的动态上下文" * 600) + "最新玩家输入"),
            ],
        )

        self.assertEqual(content, "压缩后成功。")
        self.assertEqual(len(transport.calls), 2)
        retry_messages = transport.calls[1]["payload"]["messages"]
        self.assertEqual(retry_messages[0]["content"], "稳定系统提示")
        self.assertIn("错误恢复重试", retry_messages[1]["content"])
        self.assertIn("上下文折叠", retry_messages[1]["content"])
        self.assertIn("最新玩家输入", retry_messages[1]["content"])
        self.assertEqual(len(client.last_recovery_attempts), 1)

    def test_client_does_not_retry_non_recoverable_errors(self) -> None:
        transport = FakeTransport([RuntimeError("auth failed")])
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-pro",
            expressor_model="deepseek-v4-pro",
        )
        client = OpenAICompatibleClient(config, transport=transport)

        with self.assertRaises(RuntimeError):
            client.create_chat_completion(
                model=config.action_model,
                messages=[ChatMessage(role="user", content="你好")],
            )

        self.assertEqual(len(transport.calls), 1)

    def test_client_retries_transient_upstream_error_without_compacting_prompt(self) -> None:
        transport = FakeTransport(
            [
                LLMHTTPError(status_code=503, body='{"error":{"message":"temporarily unavailable"}}'),
                "恢复成功。",
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-pro",
            expressor_model="deepseek-v4-pro",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        messages = [ChatMessage(role="user", content="保持原样的玩家输入")]

        content = client.create_chat_completion(model=config.action_model, messages=messages)

        self.assertEqual(content, "恢复成功。")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[1]["payload"]["messages"][0]["content"], "保持原样的玩家输入")
        self.assertEqual(len(client.last_recovery_attempts), 1)


if __name__ == "__main__":
    unittest.main()
