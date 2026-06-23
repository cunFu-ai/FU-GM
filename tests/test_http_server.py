import json
from pathlib import Path
import tempfile
import unittest

from fu_gm.http_server import FUGMHttpService
from fu_gm.models import Character, Clock, HeroDraft


class FUGMHttpServiceTests(unittest.TestCase):
    def test_audit_dashboard_exposes_state_logs_and_html_panel(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            runtime = service._runtime("审计测试")
            runtime.app.clock_manager.add(
                Clock(
                    name="魔导炉过载",
                    max_segments=6,
                    current=2,
                    clock_type="threat",
                    stakes="填满后实验室会爆炸。",
                )
            )
            runtime.app.character_manager.add(
                Character(
                    name="阿凛",
                    attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                    max_hp=45,
                    hp=21,
                    max_mp=45,
                    mp=30,
                    traits=["pc"],
                    identity="宝箱猎人",
                    theme="好奇心",
                    origin="星尘镇",
                )
            )
            runtime.app.world_state.record_memory_event("阿凛发现了会唱歌的宝箱。", entities=["阿凛", "宝箱"])
            runtime.app.world_state.world_profile.magic_tech_role = "辉钢财团垄断灵魂能源，上层城市享受阳光，下层街区承受污染。"
            runtime.app.world_state.world_profile.major_locations["永雨工业城"] = "公司安保和魔导工厂统治的双层城市。"
            runtime.app.world_state.world_profile.villain_seeds.append("辉钢财团要启动灵魂中枢。")
            runtime.app.world_state.world_profile.mysteries.append("永雨为什么从未停歇？")
            runtime.app.world_state.world_profile.hero_drafts["露米娅"] = HeroDraft(
                player_name="白河",
                hero_name="露米娅",
                identity="迷路的见习元素使",
                theme="归属",
                origin="星尘镇",
                classes={"元素使": 2, "旅人": 1},
                attributes={"敏捷": 6, "洞察": 10, "力量": 6, "意志": 10},
                notes=["想找到自己的小队。", "想找到自己的小队"],
                open_questions=["确认初始装备。"],
            )
            runtime.log_manager.append_message(
                "审计测试",
                "s1",
                speaker="阿凛",
                content="我检查宝箱有没有机关。",
                role="user",
            )

            status, dashboard = service.handle("GET", "/v1/audit/dashboard?campaign_id=审计测试&session_id=s1")

            self.assertEqual(status, 200)
            self.assertTrue(dashboard["ok"])
            self.assertEqual(dashboard["clocks"][0]["name"], "魔导炉过载")
            self.assertEqual(dashboard["characters"][0]["name"], "阿凛")
            self.assertEqual(dashboard["setup"]["hero_drafts"]["露米娅"]["theme"], "归属")
            self.assertIn("techno_pressure", dashboard["gm_guidance"]["inspiration_tags"])
            self.assertIn("current_focus", dashboard["play_process"])
            self.assertTrue(dashboard["play_process"]["scene_flow"])
            self.assertEqual(dashboard["story_arc"]["phase"], "opening")
            self.assertTrue(dashboard["story_arc"]["villain_pressure"])
            self.assertTrue(dashboard["story_arc"]["agenda"]["questions"])
            self.assertIn("service", dashboard["runtime"])
            self.assertIn("http", dashboard["runtime"])
            self.assertIn("pipeline", dashboard["runtime"])
            self.assertIn("astrbot_bridge", dashboard["runtime"])
            self.assertIn("conflict_queue", dashboard["runtime"])
            self.assertIn("action_recent_recoveries", dashboard["llm"])
            self.assertIn("session_zero_recent_recoveries", dashboard["llm"])
            self.assertTrue(
                any(
                    location["name"] in {"企业星城", "灵魂中枢"}
                    and location["status"] == "backstage_candidate"
                    for location in dashboard["gm_guidance"]["prepared_locations"]
                )
            )
            self.assertEqual(dashboard["logs"]["recent_transcript"][0]["content"], "我检查宝箱有没有机关。")
            self.assertIn("会唱歌的宝箱", dashboard["world"]["recent_memory_events"][0]["summary"])

            status, page = service.handle("GET", "/gm")
            self.assertEqual(status, 200)
            self.assertIsInstance(page, str)
            self.assertIn("FU-GM 审计面板", page)

            status, dashboard_page = service.handle("GET", "/dashboard")
            self.assertEqual(status, 200)
            self.assertIsInstance(dashboard_page, str)
            self.assertIn("FU-GM 审计面板", dashboard_page)
            self.assertIn("自动刷新", dashboard_page)
            self.assertIn("/v1/campaigns", dashboard_page)
            self.assertIn("读取选中存档", dashboard_page)
            self.assertIn("新建命名存档", dashboard_page)
            self.assertIn("开团前 / 第零章记录", dashboard_page)
            self.assertIn("GM 创作指导", dashboard_page)
            self.assertIn("游玩流程", dashboard_page)
            self.assertIn("长期故事节奏", dashboard_page)
            self.assertIn("运行监控", dashboard_page)
            self.assertIn("renderHeroDrafts", dashboard_page)
            self.assertIn("角色草稿", dashboard_page)
            self.assertIn("dedupeItems", dashboard_page)

    def test_health_chat_and_session_end_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            status, health = service.handle("GET", "/health")
            self.assertEqual(status, 200)
            self.assertTrue(health["ok"])
            self.assertIn("runtime", health)
            self.assertIn("astrbot_bridge", health)

            status, chat = service.handle(
                "POST",
                "/v1/chat",
                {
                    "campaign_id": "星尘宝箱谭",
                    "session_id": "s1",
                    "speaker": "阿凛",
                    "message": "普通水群，先打个招呼",
                    "mode": "casual",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(chat["route"], "casual")
            self.assertEqual(chat["reply"], "")
            self.assertTrue(chat["suppressed"])

            runtime = service._runtime("星尘宝箱谭")
            runtime.app.world_state.record_memory_event("阿凛在星尘迷宫净化了宝箱王。", entities=["阿凛", "星尘迷宫", "宝箱王"])
            runtime.log_manager.append_message(
                "星尘宝箱谭",
                "s1",
                speaker="AI GM",
                content="宝箱王刚在星尘迷宫现身，提出要收走一枚愿望。",
                role="assistant",
            )

            status, recalled = service.handle(
                "POST",
                "/v1/chat",
                {
                    "campaign_id": "星尘宝箱谭",
                    "session_id": "s1",
                    "speaker": "白河",
                    "message": "还记得宝箱王吗？",
                    "mode": "casual",
                },
            )
            self.assertEqual(status, 200)
            self.assertTrue(any("宝箱王" in item for item in recalled["public_memory"]))
            self.assertTrue(any("宝箱王刚在星尘迷宫现身" in item for item in recalled["live_context"]))

            status, ended = service.handle(
                "POST",
                "/v1/session/end",
                {
                    "campaign_id": "星尘宝箱谭",
                    "session_id": "s1",
                    "title": "星尘迷宫茶会",
                },
            )
            self.assertEqual(status, 200)
            self.assertTrue(ended["ok"])
            self.assertIn("星尘迷宫茶会", ended["summary"]["title"])
            self.assertIn("transcript_txt_path", ended["summary"])
            self.assertTrue(runtime.log_manager.summary_path("星尘宝箱谭", "s1").exists())
            self.assertTrue(runtime.log_manager.transcript_txt_path("星尘宝箱谭", "s1").exists())

    def test_runtime_monitor_tracks_astrbot_bridge_and_live_game_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "监控测试",
                    "session_id": "group-1",
                    "speaker": "阿凛",
                    "message": "开始跑团",
                    "channel_id": "group-1",
                },
            )
            runtime = service._runtime("监控测试")
            runtime.log_manager.append_message(
                "监控测试",
                "group-1",
                speaker="AI GM",
                content="永雨工业城下层刚刚停电。",
                role="assistant",
            )
            status, turn = service.handle(
                "POST",
                "/v1/game/turn",
                {
                    "campaign_id": "监控测试",
                    "session_id": "group-1",
                    "speaker": "阿凛",
                    "message": "我调查灵魂管线。",
                    "channel_id": "group-1",
                },
            )
            self.assertEqual(status, 200)
            self.assertTrue(turn["ok"])
            self.assertTrue(turn["live_context_used"])

            status, dashboard = service.handle(
                "GET",
                "/v1/audit/dashboard?campaign_id=监控测试&session_id=group-1&channel_id=group-1",
            )
            self.assertEqual(status, 200)
            self.assertTrue(dashboard["runtime"]["astrbot_bridge"]["connected_recently"])
            self.assertTrue(dashboard["runtime"]["http"]["recent_requests"])
            self.assertTrue(dashboard["runtime"]["pipeline"]["recent_turns"])
            self.assertIn("conflict_queue", dashboard["runtime"])

    def test_game_turn_recovers_from_transient_autosave_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            runtime = service._runtime("自动保存恢复")
            runtime.app.run_turn = lambda _chat: "继续推进场景。"
            original_save = runtime.app.save_campaign_memory
            calls = []

            def flaky_save(campaign_id, slot=None):
                calls.append(campaign_id)
                if len(calls) == 1:
                    raise OSError(22, "Invalid argument", str(Path(tmpdir) / "snapshot.json"))
                return original_save(campaign_id, slot=slot)

            runtime.app.save_campaign_memory = flaky_save

            status, turn = service.handle(
                "POST",
                "/v1/game/turn",
                {
                    "campaign_id": "自动保存恢复",
                    "session_id": "s1",
                    "speaker": "阿凛",
                    "message": "我观察驿站旧钟。",
                    "channel_id": "group-1",
                },
            )

            self.assertEqual(status, 200)
            self.assertTrue(turn["ok"])
            self.assertGreaterEqual(len(calls), 2)
            self.assertTrue(turn["saved_path"])
            self.assertTrue(Path(turn["saved_path"]).exists())

    def test_adventure_gate_requires_valid_formal_pc_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            runtime = service._runtime("建卡门控测试")
            runtime.app.start_session_zero(participants=["阿凛"])
            runtime.app.world_state.world_profile.hero_drafts["阿凛"] = HeroDraft(
                player_name="阿凛",
                hero_name="伊莉雅",
                identity="赤羽遗民的盾誓骑士",
                theme="责任",
                origin="白花碑驿站",
                classes={"守护者": 3, "元素使": 2},
                attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 6},
                skills={"保镖": 1, "防御精通": 1, "挺身守护": 1, "元素魔法": 1, "元素系仪式": 1},
                spells=["元素幕障"],
                equipment=["青铜剑", "青铜盾", "旅行装束"],
                confirmed=True,
            )

            status, blocked = service.handle(
                "POST",
                "/v1/session/gate",
                {
                    "campaign_id": "建卡门控测试",
                    "session_id": "s1",
                    "channel_id": "group-1",
                    "status": "adventure",
                },
            )

            self.assertEqual(status, 200)
            self.assertTrue(blocked["blocked"])
            self.assertIn("青铜剑", blocked["reply"])
            self.assertFalse(runtime.app.character_manager.exists("伊莉雅"))

            runtime.app.world_state.world_profile.hero_drafts["阿凛"].equipment = ["钢匕首", "青铜盾", "旅行装束"]
            status, opened = service.handle(
                "POST",
                "/v1/session/gate",
                {
                    "campaign_id": "建卡门控测试",
                    "session_id": "s1",
                    "channel_id": "group-1",
                    "status": "adventure",
                },
            )

            self.assertEqual(status, 200)
            self.assertFalse(opened.get("blocked", False))
            self.assertTrue(runtime.app.character_manager.exists("伊莉雅"))
            self.assertIn("pc", runtime.app.character_manager.get("伊莉雅").traits)

    def test_session_end_clears_active_conflict_for_dashboard_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            runtime = service._runtime("收团冲突测试")
            runtime.app.character_manager.add(
                Character(
                    name="阿凛",
                    attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                    max_hp=45,
                    hp=45,
                    max_mp=45,
                    mp=45,
                    traits=["pc"],
                )
            )
            runtime.app.character_manager.add(
                Character(
                    name="帝国机兵",
                    attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 6},
                    max_hp=50,
                    hp=50,
                    max_mp=30,
                    mp=30,
                    traits=["enemy"],
                )
            )
            runtime.app.conflict_manager.start_scene("桥头战", ["阿凛", "帝国机兵"])

            status, ended = service.handle(
                "POST",
                "/v1/session/end",
                {
                    "campaign_id": "收团冲突测试",
                    "session_id": "s1",
                    "channel_id": "group-1",
                    "title": "桥头战收束",
                },
            )
            self.assertEqual(status, 200)
            self.assertTrue(ended["ok"])
            self.assertFalse(runtime.app.conflict_manager.state.active)

            status, dashboard = service.handle(
                "GET",
                "/v1/audit/dashboard?campaign_id=收团冲突测试&session_id=s1&channel_id=group-1",
            )
            self.assertEqual(status, 200)
            self.assertIn("已收团", dashboard["phase"]["display"])
            self.assertNotIn("桥头战", dashboard["phase"]["display"])

    def test_auto_route_and_session_zero_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            status, response = service.handle(
                "POST",
                "/v1/chat",
                {
                    "campaign_id": "自动路由测试",
                    "session_id": "s0",
                    "speaker": "阿凛",
                    "message": "我想在第零章创建一个有地下城宝箱的世界",
                    "mode": "auto",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(response["route"], "session_zero")
            self.assertTrue(response["reply"])

    def test_natural_message_route_can_reply_or_stay_silent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            status, inactive = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "自然入口测试",
                    "session_id": "s1",
                    "speaker": "阿凛",
                    "message": "时悠，还记得宝箱王吗？",
                    "channel_id": "group-1",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(inactive["target"], "astrbot")
            self.assertFalse(inactive["send_reply"])

            status, opened = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "自然入口测试",
                    "session_id": "s1",
                    "speaker": "阿凛",
                    "message": "继续跑团",
                    "channel_id": "group-1",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(opened["gate"]["status"], "adventure")
            self.assertTrue(opened["send_reply"])

            status, chat = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "自然入口测试",
                    "session_id": "s1",
                    "speaker": "阿凛",
                    "message": "时悠，还记得宝箱王吗？",
                    "channel_id": "group-1",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(chat["target"], "fu_gm")
            self.assertEqual(chat["route"], "casual")
            self.assertFalse(chat["send_reply"])
            self.assertTrue(chat["suppressed"])
            self.assertTrue(chat["stop_astrbot"])

            status, silent = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "自然入口测试",
                    "session_id": "s1",
                    "speaker": "白河",
                    "message": "我们要不要先调查宝箱？",
                    "channel_id": "group-1",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(silent["target"], "silent")
            self.assertFalse(silent["send_reply"])
            self.assertTrue(silent["stop_astrbot"])

            status, paused = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "自然入口测试",
                    "session_id": "s1",
                    "speaker": "阿凛",
                    "message": "先暂停一下",
                    "channel_id": "group-1",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(paused["gate"]["status"], "paused")

            status, after_pause = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "自然入口测试",
                    "session_id": "s1",
                    "speaker": "阿凛",
                    "message": "时悠，还记得宝箱王吗？",
                    "channel_id": "group-1",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(after_pause["target"], "astrbot")

    def test_batched_natural_messages_are_routed_by_original_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            campaign_id = "批次路由测试"
            service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": campaign_id,
                    "session_id": "s1",
                    "speaker": "阿凛",
                    "message": "继续跑团",
                    "channel_id": "group-1",
                },
            )
            runtime = service._runtime(campaign_id)
            runtime.app.run_turn = lambda recent_chat: "已按合并发言推进一轮。"

            status, game = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": campaign_id,
                    "session_id": "s1",
                    "speaker": "白河",
                    "message": (
                        "以下是同一跑团会话中连续出现的群聊发言，请合并理解为同一轮桌面输入：\n"
                        "1. 白河：我们要不要先调查宝箱？\n"
                        "2. 阿凛：那我调查宝箱上的符文。"
                    ),
                    "channel_id": "group-1",
                    "batch_count": 2,
                    "batch_messages": [
                        {"speaker": "白河", "message": "我们要不要先调查宝箱？", "timestamp": 1.0},
                        {"speaker": "阿凛", "message": "那我调查宝箱上的符文。", "timestamp": 2.0},
                    ],
                },
            )

            self.assertEqual(status, 200)
            self.assertEqual(game["target"], "fu_gm")
            self.assertEqual(game["route"], "game")
            self.assertIn("batch", game["decision"]["tags"])
            self.assertEqual(game["reply"], "已按合并发言推进一轮。")

            status, silent = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": campaign_id,
                    "session_id": "s1",
                    "speaker": "白河",
                    "message": (
                        "以下是同一跑团会话中连续出现的群聊发言，请合并理解为同一轮桌面输入：\n"
                        "1. 白河：我们要不要先调查宝箱？\n"
                        "2. 阿凛：我觉得可以先等等。"
                    ),
                    "channel_id": "group-1",
                    "batch_count": 2,
                    "batch_messages": [
                        {"speaker": "白河", "message": "我们要不要先调查宝箱？", "timestamp": 3.0},
                        {"speaker": "阿凛", "message": "我觉得可以先等等。", "timestamp": 4.0},
                    ],
                },
            )

            self.assertEqual(status, 200)
            self.assertEqual(silent["target"], "silent")
            self.assertFalse(silent["send_reply"])
            self.assertIn("batch", silent["decision"]["tags"])

    def test_rules_reference_uses_canonical_class_skills_during_session_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "规则问答测试",
                    "session_id": "s0",
                    "speaker": "阿凛",
                    "message": "开始第零章",
                    "channel_id": "group-1",
                },
            )

            status, response = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "规则问答测试",
                    "session_id": "s0",
                    "speaker": "阿凛",
                    "message": "元素使的技能有哪些",
                    "channel_id": "group-1",
                },
            )

            self.assertEqual(status, 200)
            self.assertEqual(response["target"], "fu_gm")
            self.assertEqual(response["route"], "rules_reference")
            self.assertTrue(response["send_reply"])
            self.assertIn("天灾骤降", response["reply"])
            self.assertIn("元素魔法", response["reply"])
            self.assertIn("魔法炮击", response["reply"])
            self.assertIn("元素系仪式", response["reply"])
            self.assertIn("以械引咒", response["reply"])
            self.assertNotIn("元素专攻", response["reply"])
            self.assertNotIn("元素护盾", response["reply"])

    def test_session_zero_gate_overrides_forced_casual_and_game_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "前缀路由测试",
                    "session_id": "s0",
                    "speaker": "村夫",
                    "message": "开始第零章",
                    "channel_id": "group-1",
                },
            )

            status, casual = service.handle(
                "POST",
                "/v1/chat",
                {
                    "campaign_id": "前缀路由测试",
                    "session_id": "s0",
                    "speaker": "村夫",
                    "message": "~",
                    "channel_id": "group-1",
                    "mode": "casual",
                },
            )

            self.assertEqual(status, 200)
            self.assertEqual(casual["route"], "session_zero")
            self.assertIn("第零章频道已接上", casual["reply"])
            self.assertNotIn("我先记下这个想法：~", casual["reply"])

            status, game = service.handle(
                "POST",
                "/v1/chat",
                {
                    "campaign_id": "前缀路由测试",
                    "session_id": "s0",
                    "speaker": "村夫",
                    "message": "呀，我们在第零章，悠老师快进入状态",
                    "channel_id": "group-1",
                    "mode": "game",
                },
            )

            self.assertEqual(status, 200)
            self.assertEqual(game["route"], "session_zero")
            self.assertIn("第零章频道已接上", game["reply"])
            self.assertNotIn("请说明你们的下一步行动", game["reply"])

    def test_direct_gm_address_during_session_zero_stays_in_session_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "自然第零章测试",
                    "session_id": "s0",
                    "speaker": "村夫",
                    "message": "开始第零章",
                    "channel_id": "group-1",
                },
            )

            status, response = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "自然第零章测试",
                    "session_id": "s0",
                    "speaker": "村夫",
                    "message": "悠老师~",
                    "channel_id": "group-1",
                },
            )

            self.assertEqual(status, 200)
            self.assertEqual(response["target"], "fu_gm")
            self.assertEqual(response["route"], "session_zero")
            self.assertTrue(response["stop_astrbot"])
            self.assertIn("第零章频道已接上", response["reply"])

    def test_skill_selection_is_recorded_instead_of_listing_skill_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            campaign_id = "技能选择测试"
            service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": campaign_id,
                    "session_id": "s0",
                    "speaker": "村夫",
                    "message": "开始第零章",
                    "channel_id": "group-1",
                },
            )
            runtime = service._runtime(campaign_id)
            runtime.app.session_zero_manager.state.world.hero_drafts["诺艾尔"] = HeroDraft(
                player_name="村夫",
                hero_name="诺艾尔",
                identity="秘宝猎人",
                theme="野心",
                origin="托伦",
                classes={"元素使": 1, "武器大师": 2, "旅人": 1, "游说家": 1},
                skills={"元素魔法": 1},
                spells=["元素幕障"],
            )

            status, response = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": campaign_id,
                    "session_id": "s0",
                    "speaker": "村夫",
                    "message": "旅人技能选择宝物猎人，武器大师选择碎骨和破防打击，游说家技能选择谴责",
                    "channel_id": "group-1",
                },
            )

            self.assertEqual(status, 200)
            self.assertEqual(response["route"], "session_zero")
            self.assertNotIn("游说家的标准职业技能", response["reply"])
            self.assertNotIn("鼓舞（+6）", response["reply"])
            draft = runtime.app.session_zero_manager.state.world.hero_drafts["诺艾尔"]
            self.assertEqual(draft.skills["宝物猎人"], 1)
            self.assertEqual(draft.skills["碎骨"], 1)
            self.assertEqual(draft.skills["破防打击"], 1)
            self.assertEqual(draft.skills["谴责"], 1)

    def test_pre_session_consensus_before_session_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            status, opened = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "共识测试",
                    "session_id": "s0",
                    "speaker": "阿凛",
                    "message": "开始最终物语",
                    "channel_id": "group-1",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(opened["gate"]["status"], "pre_session")
            self.assertIn("开团前", opened["reply"])

            status, consensus = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "共识测试",
                    "session_id": "s0",
                    "speaker": "阿凛",
                    "message": "我想要王道冒险和地下城宝箱，描述风格偏动漫夸张。不要血腥细节，恋爱淡出。",
                    "channel_id": "group-1",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(consensus["route"], "pre_session")
            self.assertTrue(consensus["send_reply"])
            profile = service._runtime("共识测试").app.world_state.world_profile
            self.assertTrue(profile.tone_preferences)
            self.assertIn("日式RPG式淡化", profile.violence_guideline)
            self.assertIn("浪漫", profile.romance_guideline)

            status, more = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "共识测试",
                    "session_id": "s0",
                    "speaker": "白河",
                    "message": "队伍可以是刚认识的同路人，但不要内斗。没雷。",
                    "channel_id": "group-1",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(more["route"], "pre_session")
            self.assertIn("开启第零章", more["reply"])

            status, started = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "共识测试",
                    "session_id": "s0",
                    "speaker": "阿凛",
                    "message": "可以开启第零章",
                    "channel_id": "group-1",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(started["gate"]["status"], "session_zero")
            self.assertIn("第零章已开启", started["reply"])
            runtime = service._runtime("共识测试")
            self.assertTrue(runtime.last_saved_path)

            status, dashboard = service.handle(
                "GET",
                "/v1/audit/dashboard?campaign_id=共识测试&session_id=default",
            )
            self.assertEqual(status, 200)
            self.assertEqual(dashboard["session_id"], "s0")
            self.assertEqual(dashboard["channel_id"], "group-1")
            self.assertEqual(dashboard["scope"]["resolved_from"], "latest_active_gate")
            self.assertTrue(dashboard["setup"]["recorded_consensus"]["tone_preferences"])
            self.assertIn("动漫", dashboard["setup"]["recorded_consensus"]["description_style"])
            self.assertTrue(
                any("地下城" in item["fact"] for item in dashboard["setup"]["recent_accepted_facts"])
            )
            self.assertTrue(
                any(event["kind"] == "pre_session_consensus" for event in dashboard["world"]["recent_memory_events"])
            )

    def test_session_zero_dashboard_shows_recorded_world_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "第零章审计测试",
                    "session_id": "group-42",
                    "speaker": "阿凛",
                    "message": "开始第零章",
                    "channel_id": "group-42",
                },
            )
            status, response = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "第零章审计测试",
                    "session_id": "group-42",
                    "speaker": "阿凛",
                    "message": "我希望世界围绕浮空群岛、地下城宝箱和会反射主角阴影的反派展开。",
                    "channel_id": "group-42",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(response["target"], "fu_gm")
            runtime = service._runtime("第零章审计测试")
            self.assertTrue(runtime.last_saved_path)

            status, dashboard = service.handle(
                "GET",
                "/v1/audit/dashboard?campaign_id=第零章审计测试&session_id=default",
            )
            self.assertEqual(status, 200)
            self.assertEqual(dashboard["session_id"], "group-42")
            self.assertEqual(dashboard["channel_id"], "group-42")
            self.assertTrue(dashboard["setup"]["recent_accepted_facts"])
            self.assertTrue(
                any(event["kind"] == "session_zero_fact" for event in dashboard["world"]["recent_memory_events"])
            )

    def test_session_zero_gate_records_table_talk_without_replying_to_everything(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "第零章静默测试",
                    "session_id": "s1",
                    "speaker": "阿凛",
                    "message": "开始第零章",
                    "channel_id": "group-1",
                },
            )

            status, laugh = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "第零章静默测试",
                    "session_id": "s1",
                    "speaker": "阿凛",
                    "message": "哈哈哈",
                    "channel_id": "group-1",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(laugh["target"], "silent")
            self.assertFalse(laugh["send_reply"])
            self.assertTrue(laugh["stop_astrbot"])

            status, contribution = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "第零章静默测试",
                    "session_id": "s1",
                    "speaker": "阿凛",
                    "message": "我希望是个有地下城宝箱和奇遇的奇幻故事",
                    "channel_id": "group-1",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(contribution["target"], "fu_gm")
            self.assertEqual(contribution["route"], "session_zero")
            self.assertTrue(contribution["send_reply"])

            status, natural_contribution = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "第零章静默测试",
                    "session_id": "s1",
                    "speaker": "白河",
                    "message": "地下城宝箱奇遇挺好，就这个方向吧",
                    "channel_id": "group-1",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(natural_contribution["target"], "fu_gm")
            self.assertEqual(natural_contribution["route"], "session_zero")
            self.assertTrue(natural_contribution["send_reply"])

    def test_session_gate_signal_end_finalizes_and_deactivates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "收团测试",
                    "session_id": "s1",
                    "speaker": "阿凛",
                    "message": "开始跑团",
                    "channel_id": "group-1",
                },
            )
            service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "收团测试",
                    "session_id": "s1",
                    "speaker": "阿凛",
                    "message": "我调查宝箱",
                    "channel_id": "group-1",
                },
            )
            status, ended = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "收团测试",
                    "session_id": "s1",
                    "speaker": "阿凛",
                    "message": "今天到这，收团",
                    "channel_id": "group-1",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(ended["gate"]["status"], "inactive")
            self.assertTrue(ended["summary"])
            runtime = service._runtime("收团测试")
            self.assertTrue(runtime.log_manager.summary_path("收团测试", "s1").exists())
            self.assertTrue(runtime.log_manager.transcript_txt_path("收团测试", "s1").exists())
            self.assertIsNone(runtime.app.scene_manager.current_scene)
            status, dashboard = service.handle(
                "GET",
                "/v1/audit/dashboard",
                {
                    "campaign_id": "收团测试",
                    "session_id": "s1",
                    "channel_id": "group-1",
                },
            )
            self.assertEqual(status, 200)
            self.assertIsNone(dashboard["phase"]["current_scene"])
            self.assertEqual(dashboard["phase"]["display"], "已收团，等待下一场准备")

    def test_adventure_gate_blocks_until_characters_are_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            service._runtime("角色门禁测试").app.session_zero_manager.start(participants=["阿凛", "南星"])

            status, response = service.handle(
                "POST",
                "/v1/session/gate",
                {
                    "campaign_id": "角色门禁测试",
                    "session_id": "s1",
                    "channel_id": "group-1",
                    "status": "adventure",
                },
            )

            self.assertEqual(status, 200)
            self.assertTrue(response["blocked"])
            self.assertNotEqual(response["gate"]["status"], "adventure")
            self.assertIn("角色未创建完不能开启跑团", response["reply"])

    def test_safety_declare_endpoint_anonymizes_private_sender_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            status, response = service.handle(
                "POST",
                "/v1/safety/declare",
                {
                    "campaign_id": "匿名安全测试",
                    "session_id": "private",
                    "speaker": "阿凛",
                    "message": "我不希望出现蜘蛛，儿童遇险请带过。",
                    "anonymous": True,
                },
            )

            self.assertEqual(status, 200)
            self.assertTrue(response["ok"])
            self.assertTrue(response["anonymous"])
            self.assertEqual([item["declaration_type"] for item in response["declared"]], ["line", "veil"])
            self.assertIn("匿名记录", response["reply"])

            runtime = service._runtime("匿名安全测试")
            self.assertIn("蜘蛛", runtime.app.world_state.world_profile.safety_lines)
            self.assertIn("儿童遇险", runtime.app.world_state.world_profile.safety_veils)
            self.assertFalse(any("阿凛" in memory for memory in runtime.app.world_state.memories))
            self.assertTrue(runtime.last_saved_path)

    def test_chat_auto_routes_natural_safety_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            status, response = service.handle(
                "POST",
                "/v1/chat",
                {
                    "campaign_id": "自然安全路由",
                    "session_id": "s1",
                    "speaker": "白河",
                    "message": "不健康关系不要详细描写。",
                    "mode": "auto",
                },
            )

            self.assertEqual(status, 200)
            self.assertEqual(response["route"], "safety")
            self.assertIn("不健康关系", service._runtime("自然安全路由").app.world_state.world_profile.safety_veils)

    def test_game_turn_rules_block_returns_friendly_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            runtime = service._runtime("规则拦截测试")

            def blocked_turn(_recent_chat: str) -> str:
                raise ValueError("米露 尚未掌握【仪式御魂使术】，不能执行该学科的仪式。")

            runtime.app.run_turn = blocked_turn
            status, response = service.handle(
                "POST",
                "/v1/game/turn",
                {
                    "campaign_id": "规则拦截测试",
                    "session_id": "s1",
                    "speaker": "小梦",
                    "message": "我用灵魂仪式安抚宝箱。",
                },
            )

            self.assertEqual(status, 200)
            self.assertTrue(response["ok"])
            self.assertTrue(response["rules_blocked"])
            self.assertIn("规则结算拦截", response["reply"])

    def test_game_turn_missing_actor_keyerror_returns_friendly_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            runtime = service._runtime("缺角色拦截测试")

            def blocked_turn(_recent_chat: str) -> str:
                raise KeyError("洛岚")

            runtime.app.run_turn = blocked_turn
            status, response = service.handle(
                "POST",
                "/v1/game/turn",
                {
                    "campaign_id": "缺角色拦截测试",
                    "session_id": "s1",
                    "speaker": "白河",
                    "message": "洛岚检查灵魂晶炉。",
                },
            )

            self.assertEqual(status, 200)
            self.assertTrue(response["ok"])
            self.assertTrue(response["rules_blocked"])
            self.assertIn("规则结算拦截", response["reply"])
            self.assertIn("洛岚", response["reply"])

    def test_game_turn_success_autosaves_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            runtime = service._runtime("自动保存测试")

            def successful_turn(_recent_chat: str) -> str:
                runtime.app.world_state.record_memory_event("自动保存事实已经写入。", entities=["自动保存事实"])
                return "已记录。"

            runtime.app.run_turn = successful_turn
            status, response = service.handle(
                "POST",
                "/v1/game/turn",
                {
                    "campaign_id": "自动保存测试",
                    "session_id": "s1",
                    "speaker": "阿凛",
                    "message": "我记录一条事实。",
                },
            )

            self.assertEqual(status, 200)
            self.assertTrue(response["ok"])
            self.assertEqual(response["saved_path"], runtime.last_saved_path)

            snapshot_path = Path(response["saved_path"])
            self.assertTrue(snapshot_path.exists())
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            self.assertTrue(
                any(
                    "自动保存事实" in event.get("summary", "")
                    for event in snapshot["world_state"]["memory_events"]
                )
            )

    def test_campaign_save_load_list_and_auto_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            runtime = service._runtime("星尘宝箱谭")
            runtime.app.world_state.record_memory_event("阿凛获得了会唱歌的铜钥匙。", entities=["阿凛", "铜钥匙"])

            status, saved = service.handle(
                "POST",
                "/v1/campaigns/save",
                {"campaign_id": "星尘宝箱谭", "session_id": "s1", "speaker": "阿凛", "slot": "boss战前"},
            )
            self.assertEqual(status, 200)
            self.assertTrue(saved["ok"])
            self.assertEqual(saved["slot"], "boss战前")

            status, campaigns = service.handle("GET", "/v1/campaigns")
            self.assertEqual(status, 200)
            self.assertTrue(any(item["campaign_id"] == "星尘宝箱谭" for item in campaigns["campaigns"]))

            service.runtimes.clear()
            restored = service._runtime("星尘宝箱谭")
            self.assertTrue(restored.loaded_from_disk)
            self.assertTrue(any("铜钥匙" in memory for memory in restored.app.world_state.memories))

            restored.app.world_state.memories.clear()
            status, loaded = service.handle(
                "POST",
                "/v1/campaigns/load",
                {"campaign_id": "星尘宝箱谭", "slot": "boss战前"},
            )
            self.assertEqual(status, 200)
            self.assertTrue(loaded["ok"])
            self.assertTrue(any("铜钥匙" in memory for memory in restored.app.world_state.memories))
            self.assertIn("world_profile", loaded["loaded_sections"]["world_state_keys"])

    def test_campaign_import_chat_log_writes_structured_migration_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            import_payload = {
                "summary": "旧群聊迁移出两个角色草稿和第零章共识。",
                "confidence": 0.91,
                "world_updates": {
                    "campaign_title": "月轨遗歌",
                    "world_style": "月轨列车与魔导城市交错的奇幻世界。",
                    "group_concept": "护送失落星核的小队。",
                    "starting_region": "银钟站",
                    "major_locations": {"银钟站": "旧铁路线尽头的边境车站。"},
                    "factions": {"钟塔工会": "维护月轨列车的魔导技师组织。"},
                    "safety_lines": ["蜘蛛"],
                    "safety_veils": ["儿童遇险"],
                    "hero_drafts": {
                        "艾丽妮": {
                            "player_name": "阿凛",
                            "hero_name": "艾丽妮",
                            "identity": "失忆的月轨术士",
                            "theme": "疑虑",
                            "origin": "银钟站",
                            "classes": {"元素使": 2, "御魂使": 3},
                            "attributes": {"敏捷": 8, "洞察": 10, "力量": 6, "意志": 8},
                            "skills": {"元素魔法": 1},
                            "spells": ["巨岩"],
                            "notes": ["正在创建中，尚未确认装备。"],
                        },
                        "诺艾尔": {
                            "player_name": "白河",
                            "hero_name": "诺艾尔",
                            "identity": "守夜骑士",
                            "theme": "使命",
                            "origin": "钟塔工会",
                            "classes": {"守护者": 2, "武器大师": 3},
                            "equipment": ["青铜盾"],
                            "bound_arcana": ["高塔奥灵"],
                        },
                    },
                    "open_questions": ["确认两名角色的起始资金。"],
                },
                "subject_facts": {
                    "艾丽妮": ["对月轨列车的旧事故有模糊记忆。"],
                    "诺艾尔": ["宣誓保护艾丽妮抵达终点站。"],
                },
                "memory_events": [
                    {
                        "summary": "第零章迁移：小队围绕失落星核与月轨列车展开。",
                        "kind": "session_zero_fact",
                        "visibility": "public",
                        "entities": ["艾丽妮", "诺艾尔", "失落星核"],
                        "tags": ["session_zero"],
                    }
                ],
            }

            status, imported = service.handle(
                "POST",
                "/v1/campaigns/import-chat-log",
                {
                    "campaign_id": "月轨遗歌",
                    "session_id": "100000001",
                    "channel_id": "100000001",
                    "chat_log": "旧群聊里正在创建角色艾丽妮和诺艾尔。",
                    "target_slot": "迁移基准",
                    "import_payload": import_payload,
                },
            )

            self.assertEqual(status, 200)
            self.assertTrue(imported["ok"])
            self.assertEqual(imported["counts"]["hero_drafts"], 2)
            runtime = service._runtime("月轨遗歌")
            world = runtime.app.world_state.world_profile
            self.assertEqual(world.campaign_title, "月轨遗歌")
            self.assertIn("蜘蛛", world.safety_lines)
            self.assertIn("儿童遇险", world.safety_veils)
            self.assertIn("艾丽妮", world.hero_drafts)
            self.assertEqual(world.hero_drafts["艾丽妮"].classes["元素使"], 2)
            self.assertIn("高塔奥灵", world.hero_drafts["诺艾尔"].bound_arcana)
            self.assertIn("银钟站", runtime.app.world_state.map_locations)
            self.assertTrue(any("失落星核" in event.summary for event in runtime.app.world_state.memory_events))
            self.assertTrue(service._memory_store().snapshot_exists("月轨遗歌", slot="迁移基准"))

            status, dashboard = service.handle(
                "GET",
                "/v1/audit/dashboard?campaign_id=月轨遗歌&session_id=100000001&channel_id=100000001&include_private=true",
            )
            self.assertEqual(status, 200)
            self.assertEqual(dashboard["setup"]["hero_drafts"]["艾丽妮"]["notes"], ["正在创建中，尚未确认装备。"])
            self.assertEqual(dashboard["setup"]["hero_drafts"]["艾丽妮"]["concept_notes"], ["正在创建中，尚未确认装备。"])
            self.assertIn("attributes", dashboard["setup"]["hero_drafts"]["艾丽妮"])

            service.runtimes.clear()
            status, loaded = service.handle(
                "POST",
                "/v1/campaigns/load",
                {"campaign_id": "月轨遗歌", "slot": "迁移基准"},
            )
            self.assertEqual(status, 200)
            self.assertTrue(loaded["ok"])
            restored = service._runtime("月轨遗歌")
            self.assertIn("诺艾尔", restored.app.world_state.world_profile.hero_drafts)
            self.assertIn("world_profile", loaded["loaded_sections"]["world_state_keys"])

    def test_campaign_import_chat_log_dry_run_does_not_write_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            status, preview = service.handle(
                "POST",
                "/v1/campaigns/import-chat-log",
                {
                    "campaign_id": "预览团",
                    "chat_log": (
                        "时悠: 05-22 04:35:13\n"
                        "那 @loading 你的角色呢？比如被藤蔓灵选中的少年？\n"
                        "测试玩家甲: 05-22 04:49:24\n"
                        "okok，叫诺艾尔，他认为自己是个秘宝猎人。不希望看到血腥暴力的描述，政治相关请放在幕后。\n"
                        "loading: 05-22 04:40:04\n"
                        "我的角色叫艾丽妮，是青年女，主题是归属。\n"
                    ),
                    "dry_run": True,
                },
            )

            self.assertEqual(status, 200)
            self.assertTrue(preview["ok"])
            self.assertTrue(preview["dry_run"])
            self.assertTrue(preview["fallback_used"])
            drafts = preview["import_payload"]["world_updates"]["hero_drafts"]
            self.assertEqual(set(drafts), {"艾丽妮", "诺艾尔"})
            self.assertEqual(preview["import_payload"]["world_updates"]["safety_lines"], ["血腥暴力的描述"])
            self.assertEqual(preview["import_payload"]["world_updates"]["safety_veils"], ["政治相关"])
            self.assertFalse(service._memory_store().snapshot_exists("预览团"))

    def test_natural_save_controls_list_save_and_load_slots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            runtime = service._runtime("自然存档测试")
            runtime.app.world_state.record_memory_event("阿凛拿到了星形钥匙。", entities=["阿凛", "星形钥匙"])
            service.handle(
                "POST",
                "/v1/session/gate",
                {
                    "campaign_id": "自然存档测试",
                    "session_id": "s1",
                    "channel_id": "group-1",
                    "status": "adventure",
                },
            )

            status, saved = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "自然存档测试",
                    "session_id": "s1",
                    "speaker": "阿凛",
                    "message": "新建存档 boss战前",
                    "channel_id": "group-1",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(saved["route"], "save_control")
            self.assertEqual(saved["slot"], "boss战前")
            self.assertTrue(saved["send_reply"])

            runtime.app.world_state.memories.clear()
            status, listed = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "自然存档测试",
                    "session_id": "s1",
                    "speaker": "阿凛",
                    "message": "时悠，调出存档列表",
                    "channel_id": "group-1",
                },
            )
            self.assertEqual(status, 200)
            self.assertIn("boss战前", listed["reply"])

            status, loaded = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "自然存档测试",
                    "session_id": "s1",
                    "speaker": "阿凛",
                    "message": "读档 boss战前",
                    "channel_id": "group-1",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(loaded["route"], "save_control")
            self.assertTrue(loaded["ok"])
            self.assertTrue(any("星形钥匙" in memory for memory in runtime.app.world_state.memories))
            self.assertEqual(service._current_campaign_id(), "自然存档测试")

    def test_campaign_new_and_current_payload_for_dashboard_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            status, created = service.handle("POST", "/v1/campaigns/new", {"campaign_id": "新团"})
            self.assertEqual(status, 200)
            self.assertTrue(created["ok"])

            status, current = service.handle("GET", "/v1/campaigns/current")
            self.assertEqual(status, 200)
            self.assertEqual(current["campaign_id"], "新团")

            status, campaigns = service.handle("GET", "/v1/campaigns")
            self.assertEqual(status, 200)
            self.assertEqual(campaigns["current_campaign_id"], "新团")
            self.assertEqual(campaigns["campaigns"][0]["campaign_id"], "新团")

    def test_campaign_save_slots_decodes_url_campaign_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            service.handle("POST", "/v1/campaigns/save", {"campaign_id": "中文战役", "slot": "Boss战前"})

            status, slots = service.handle("GET", "/v1/campaigns/%E4%B8%AD%E6%96%87%E6%88%98%E5%BD%B9/save-slots")
            self.assertEqual(status, 200)
            self.assertEqual(slots["campaign_id"], "中文战役")
            self.assertTrue(any(slot["slot"] == "Boss战前" for slot in slots["slots"]))

    def test_campaign_delete_save_slot_latest_and_entire_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            runtime = service._runtime("删档测试")
            runtime.app.world_state.record_memory_event("删档测试团保存了一把铜钥匙。", entities=["铜钥匙"])
            service.handle("POST", "/v1/campaigns/save", {"campaign_id": "删档测试", "slot": "boss战前"})

            status, deleted_slot = service.handle(
                "POST",
                "/v1/campaigns/delete",
                {"campaign_id": "删档测试", "slot": "boss战前"},
            )
            self.assertEqual(status, 200)
            self.assertTrue(deleted_slot["ok"])
            self.assertTrue(deleted_slot["deleted"])

            status, missing_load = service.handle(
                "POST",
                "/v1/campaigns/load",
                {"campaign_id": "删档测试", "slot": "boss战前"},
            )
            self.assertEqual(status, 404)
            self.assertFalse(missing_load["ok"])

            status, deleted_latest = service.handle("POST", "/v1/campaigns/delete", {"campaign_id": "删档测试"})
            self.assertEqual(status, 200)
            self.assertTrue(deleted_latest["deleted"])

            service.handle("POST", "/v1/campaigns/save", {"campaign_id": "删档测试", "slot": "最终战前"})
            status, blocked = service.handle(
                "POST",
                "/v1/campaigns/delete",
                {"campaign_id": "删档测试", "delete_all": True},
            )
            self.assertEqual(status, 400)
            self.assertFalse(blocked["ok"])

            status, deleted_all = service.handle(
                "POST",
                "/v1/campaigns/delete",
                {"campaign_id": "删档测试", "delete_all": True, "confirm": "确认删除"},
            )
            self.assertEqual(status, 200)
            self.assertTrue(deleted_all["deleted"])
            self.assertNotIn("删档测试", service.runtimes)
            self.assertNotEqual(service._current_campaign_id(), "删档测试")

            status, campaigns = service.handle("GET", "/v1/campaigns")
            self.assertEqual(status, 200)
            self.assertFalse(any(item["campaign_id"] == "删档测试" for item in campaigns["campaigns"]))

    def test_attendance_away_back_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            status, away = service.handle(
                "POST",
                "/v1/session/away",
                {
                    "campaign_id": "午后迷宫",
                    "session_id": "s1",
                    "speaker": "白河",
                    "reason": "临时去接电话",
                },
            )
            self.assertEqual(status, 200)
            self.assertIn("白河", away["attendance"]["absent_players"])

            service.runtimes.clear()
            status, status_payload = service.handle("POST", "/v1/session/status", {"campaign_id": "午后迷宫"})
            self.assertEqual(status, 200)
            self.assertIn("白河", status_payload["attendance"]["absent_players"])

            status, back = service.handle(
                "POST",
                "/v1/session/back",
                {"campaign_id": "午后迷宫", "session_id": "s1", "speaker": "白河"},
            )
            self.assertEqual(status, 200)
            self.assertNotIn("白河", back["attendance"]["absent_players"])


if __name__ == "__main__":
    unittest.main()
