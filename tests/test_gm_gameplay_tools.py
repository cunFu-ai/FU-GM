import tempfile
import unittest
from unittest.mock import patch

from fu_gm.gm_tool_agent import GMToolExecutionContext
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import (
    Action,
    ActionType,
    Affinity,
    Character,
    Clock,
    EffectTiming,
    HeroDraft,
    RollOutcome,
    SceneType,
    StatusEffect,
    TimedEffect,
    TravelThreatLevel,
)


def gameplay_context(message: str, *, speaker: str = "阿凛") -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="gameplay-tool-test",
        session_id="s1",
        channel_id="group-1",
        speaker=speaker,
        gate_status="adventure",
        directly_addressed=False,
        metadata={
            "current_message": message,
            "recent_public_context": "众人正在白花碑驿站的风铃廊里寻找旧路。",
        },
    )


class GMGameplayToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.service = FUGMHttpService(data_root=self.tmpdir.name, use_llm=False)
        self.runtime = self.service._runtime("gameplay-tool-test")
        self.app = self.runtime.app
        self.app.character_manager.add(
            Character(
                name="伊莉雅",
                attributes={"DEX": 8, "INS": 10, "MIG": 8, "WLP": 6},
                max_hp=45,
                hp=45,
                max_mp=35,
                mp=35,
                traits=["pc"],
                skills={},
            )
        )
        self.app.character_manager.add(
            Character(
                name="洛岚",
                attributes={"DEX": 8, "INS": 8, "MIG": 6, "WLP": 10},
                max_hp=40,
                hp=40,
                max_mp=50,
                mp=50,
                traits=["pc"],
            )
        )
        self.app.world_state.world_profile.hero_drafts["阿凛"] = HeroDraft(
            player_name="阿凛",
            hero_name="伊莉雅",
        )
        self.app.world_state.world_profile.hero_drafts["白河"] = HeroDraft(
            player_name="白河",
            hero_name="洛岚",
        )
        self.app.scene_manager.start_scene(
            "白花碑驿站",
            SceneType.STANDARD,
            location="风铃廊",
            participants=["伊莉雅", "洛岚"],
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_state_summary_exposes_authoritative_control_and_chinese_attributes(self) -> None:
        state = self.service.gm_gameplay_tools.state_summary(
            gameplay_context("伊莉雅观察四周。")
        )

        self.assertEqual(state["controlled_characters"], ["伊莉雅"])
        ilya = next(item for item in state["characters"] if item["name"] == "伊莉雅")
        self.assertEqual(ilya["attributes"]["洞察"], 10)
        self.assertNotIn("INS", ilya["attributes"])

    def test_state_summary_exposes_skill_runtime_state_needed_by_the_agent(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.skill_options["形意咒法"] = ["力量+意志"]
        ilya.skill_counters["酒馆攀谈"] = 2
        ilya.chimerist_spell_species["活力汲取"] = "怪物"

        state = self.service.gm_gameplay_tools.state_summary(
            gameplay_context("伊莉雅还能使用哪些技能资源？")
        )
        exposed = next(
            item for item in state["characters"] if item["name"] == "伊莉雅"
        )

        self.assertEqual(exposed["skill_options"]["形意咒法"], ["力量+意志"])
        self.assertEqual(exposed["skill_counters"]["酒馆攀谈"], 2)
        self.assertEqual(exposed["chimerist_spells"]["活力汲取"], "怪物")

    def test_create_loyal_companion_is_atomic_and_exposed_in_gameplay_state(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.skills["忠诚伙伴"] = 1
        message = "伊莉雅的伙伴就叫铜铃，按刚才定好的构装体方案创建吧。"
        arguments = {
            "owner": "伊莉雅",
            "name": "铜铃",
            "species": "构装体",
            "traits": ["忠心", "好奇", "坚固", "小型"],
            "attribute_spread": "标准",
            "attribute_order": ["力量", "敏捷", "洞察", "意志"],
            "selected_skills": ["强化生命", "特殊攻击"],
            "skill_options": {},
            "attacks": [
                {
                    "name": "铁尾横扫",
                    "attributes": ["力量", "敏捷"],
                    "damage_type": "物理",
                    "range": "melee",
                    "status_effect_on_hit": "迟缓",
                }
            ],
            "profile": {"core_drive": "保护伊莉雅"},
            "evidence": "按刚才定好的构装体方案创建吧",
        }

        receipt = self.service.gm_gameplay_tools.create_loyal_companion(
            gameplay_context(message),
            arguments,
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertTrue(self.app.character_manager.exists("铜铃"))
        state = self.service.gm_gameplay_tools.state_summary(
            gameplay_context("伊莉雅的忠诚伙伴现在是什么状态？")
        )
        exposed = next(
            item for item in state["characters"] if item["name"] == "伊莉雅"
        )
        self.assertEqual(exposed["loyal_companion"]["name"], "铜铃")

    def test_loyal_companion_survives_reload_and_can_still_be_commanded(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.skills["忠诚伙伴"] = 1
        self.app.character_manager.add(
            Character(
                name="巡路机兵",
                attributes={"DEX": 6, "INS": 6, "MIG": 8, "WLP": 6},
                max_hp=40,
                hp=40,
                max_mp=20,
                mp=20,
                defenses={"physical": 6, "magic": 6},
                traits=["enemy", "construct"],
            )
        )
        self.app.scene_manager.add_participant("巡路机兵")
        message = "伊莉雅确认构装体伙伴铜铃就按刚才商量的资料创建。"
        created = self.service.gm_gameplay_tools.create_loyal_companion(
            gameplay_context(message),
            {
                "owner": "伊莉雅",
                "name": "铜铃",
                "species": "构装体",
                "traits": ["忠心", "好奇", "坚固", "小型"],
                "attribute_spread": "标准",
                "attribute_order": ["力量", "敏捷", "洞察", "意志"],
                "selected_skills": ["强化生命", "特殊攻击"],
                "skill_options": {},
                "attacks": [
                    {
                        "name": "铁尾横扫",
                        "attributes": ["力量", "敏捷"],
                        "damage_type": "物理",
                        "range": "melee",
                        "status_effect_on_hit": "迟缓",
                    }
                ],
                "profile": {"core_drive": "保护伊莉雅"},
                "evidence": "确认构装体伙伴铜铃",
            },
        )
        self.assertTrue(created.ok, created.message)

        reloaded_service = FUGMHttpService(
            data_root=self.tmpdir.name,
            use_llm=False,
        )
        reloaded_app = reloaded_service._runtime("gameplay-tool-test").app
        companion = reloaded_app.loyal_companion_manager.companion_for(
            "伊莉雅"
        )

        self.assertIsNotNone(companion)
        self.assertEqual(companion.name, "铜铃")
        self.assertEqual(
            reloaded_app.loyal_companion_manager.public_state("伊莉雅")[
                "attacks"
            ][0]["name"],
            "铁尾横扫",
        )
        self.assertIn(
            "铜铃",
            reloaded_app.scene_manager.current_scene.participants,
        )

        reloaded_app.conflict_manager.start_scene(
            "风铃廊伏击",
            ["伊莉雅", "铜铃", "巡路机兵"],
            player_side=["伊莉雅", "铜铃"],
            enemy_side=["巡路机兵"],
        )
        command_message = "伊莉雅让铜铃护住自己。"
        commanded = (
            reloaded_service.gm_gameplay_tools.perform_character_action(
                gameplay_context(command_message),
                {
                    "action_type": "Skill",
                    "actor": "伊莉雅",
                    "details": {
                        "skill_name": "忠诚伙伴",
                        "companion_action_type": "Guard",
                    },
                    "evidence": "让铜铃护住自己",
                },
            )
        )

        self.assertTrue(commanded.ok, commanded.message)
        self.assertTrue(
            reloaded_app.character_manager.get("铜铃").guarding
        )
        self.assertNotIn(
            "铜铃",
            reloaded_app.conflict_manager.state.turn_order,
        )

    def test_failed_loyal_companion_autosave_rolls_back_every_domain(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.skills["忠诚伙伴"] = 1
        message = "伊莉雅确认创建构装体伙伴铜铃。"
        with patch.object(
            self.service,
            "_autosave_campaign",
            side_effect=RuntimeError("磁盘写入失败"),
        ):
            receipt = self.service.gm_gameplay_tools.create_loyal_companion(
                gameplay_context(message),
                {
                    "owner": "伊莉雅",
                    "name": "铜铃",
                    "species": "构装体",
                    "traits": ["忠心", "好奇", "坚固", "小型"],
                    "attribute_spread": "标准",
                    "attribute_order": ["力量", "敏捷", "洞察", "意志"],
                    "selected_skills": ["强化生命", "特殊攻击"],
                    "skill_options": {},
                    "attacks": [
                        {
                            "name": "铁尾横扫",
                            "attributes": ["力量", "敏捷"],
                            "damage_type": "物理",
                            "range": "melee",
                            "status_effect_on_hit": "迟缓",
                        }
                    ],
                    "evidence": "确认创建构装体伙伴铜铃",
                },
            )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "LOYAL_COMPANION_COMMIT_FAILED",
        )
        self.assertFalse(self.app.character_manager.exists("铜铃"))
        self.assertNotIn(
            "忠诚伙伴",
            self.app.character_manager.get("伊莉雅").npc_skill_effects,
        )
        self.assertNotIn("铜铃", self.app.world_state.npc_personas)

    def test_chimerist_spell_learning_persists_source_and_spell_cast_uses_fixed_attributes(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.skills.update({"形意咒法": 1, "同源之毒": 1})
        ilya.skill_options["形意咒法"] = ["力量+意志"]
        source = Character(
            name="沼泽巫兽",
            attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
            max_hp=50,
            hp=50,
            max_mp=50,
            mp=50,
            defenses={"physical": 8, "magic": 8},
            traits=["enemy", "monster"],
            spells=["活力汲取"],
        )
        self.app.character_manager.add(source)
        self.app.scene_manager.add_participant("沼泽巫兽")
        message = "伊莉雅要记住沼泽巫兽刚才施放的活力汲取。"

        learned = self.service.gm_gameplay_tools.learn_chimerist_spell(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "source": "沼泽巫兽",
                "spell_name": "活力汲取",
                "evidence": "记住沼泽巫兽刚才施放的活力汲取",
            },
        )

        self.assertTrue(learned.ok, learned.message)
        self.assertEqual(ilya.chimerist_spell_species["活力汲取"], "怪物")
        self.assertIn("活力汲取", ilya.spells)

        cast_message = "伊莉雅对沼泽巫兽施放活力汲取。"
        cast = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(cast_message),
            {
                "action_type": "Spell",
                "actor": "伊莉雅",
                "target": "沼泽巫兽",
                "details": {"spell_name": "活力汲取"},
                "evidence": "伊莉雅对沼泽巫兽施放活力汲取",
            },
        )

        self.assertTrue(cast.ok, cast.message)
        committed = cast.result["committed_action"]
        self.assertEqual(committed["chimerist_origin_species"], "怪物")
        self.assertIn("力量", cast.public_fallback_reply)
        self.assertIn("意志", cast.public_fallback_reply)

    def test_failed_chimerist_spell_autosave_rolls_back_learning(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.skills["形意咒法"] = 1
        ilya.skill_options["形意咒法"] = ["洞察+意志"]
        self.app.character_manager.add(
            Character(
                name="沼泽巫兽",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                max_hp=50,
                hp=50,
                max_mp=50,
                mp=50,
                traits=["enemy", "monster"],
                spells=["活力汲取"],
            )
        )
        self.app.scene_manager.add_participant("沼泽巫兽")
        message = "伊莉雅记住沼泽巫兽的活力汲取。"

        with patch.object(
            self.service,
            "_autosave_campaign",
            side_effect=RuntimeError("磁盘写入失败"),
        ):
            receipt = self.service.gm_gameplay_tools.learn_chimerist_spell(
                gameplay_context(message),
                {
                    "actor": "伊莉雅",
                    "source": "沼泽巫兽",
                    "spell_name": "活力汲取",
                    "evidence": "记住沼泽巫兽的活力汲取",
                },
            )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "CHIMERIST_SPELL_COMMIT_FAILED")
        restored = self.app.character_manager.get("伊莉雅")
        self.assertNotIn("活力汲取", restored.spells)
        self.assertNotIn("活力汲取", restored.chimerist_spell_species)

    def test_memory_training_returns_only_public_scene_record(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.skills["记忆训练"] = 1
        ended = self.app.scene_manager.end_scene("众人在钟楼找到公开的铜钥匙。")
        self.app.scene_manager.start_scene(
            "旧路入口",
            SceneType.STANDARD,
            location="东侧堤脊",
            participants=["伊莉雅", "洛岚"],
        )
        self.app.scene_frame_manager.ensure_frame(
            scene=self.app.scene_manager.current_scene,
            recent_chat="",
            world_state=self.app.world_state,
            character_manager=self.app.character_manager,
        ).secrets = ["铜钥匙其实属于监察官。"]

        receipt = self.service.gm_gameplay_tools.recall_scene_memory(
            gameplay_context("伊莉雅回想白花碑驿站。"),
            {"actor": "伊莉雅", "scene_name": ended.name},
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertIn("公开的铜钥匙", receipt.result["scene"]["summary"])
        self.assertNotIn("监察官", str(receipt.result))

    def test_tavern_talk_is_granted_by_lodging_rest_and_consumed_once_per_question(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.skills["酒馆攀谈"] = 2
        ilya.zenit = 500
        rest_message = "伊莉雅和洛岚在白花碑旅馆住一晚，由伊莉雅付钱。"
        rested = self.service.gm_gameplay_tools.perform_scene_action(
            gameplay_context(rest_message),
            {
                "action_type": "Rest",
                "actor": "伊莉雅",
                "details": {
                    "rest_type": "settlement",
                    "safe_source": "白花碑旅馆",
                    "rest_source_kind": "lodging",
                    "settlement_size": "city",
                    "payer": "伊莉雅",
                },
                "evidence": "伊莉雅和洛岚在白花碑旅馆住一晚",
            },
        )
        self.assertTrue(rested.ok, rested.message)
        self.assertEqual(ilya.skill_counters["酒馆攀谈"], 2)

        question_message = "伊莉雅问酒客：最近哪条路最常有财团巡逻？"
        answered = self.service.gm_gameplay_tools.resolve_tavern_talk(
            gameplay_context(question_message),
            {
                "actor": "伊莉雅",
                "question": "最近哪条路最常有财团巡逻？",
                "public_answer": "东侧盐沼堤脊每逢退潮后都会出现新的金属踏痕。",
                "evidence": "最近哪条路最常有财团巡逻",
            },
        )

        self.assertTrue(answered.ok, answered.message)
        self.assertEqual(
            answered.public_fallback_reply,
            "东侧盐沼堤脊每逢退潮后都会出现新的金属踏痕。",
        )
        self.assertEqual(ilya.skill_counters["酒馆攀谈"], 1)

    def test_failed_tavern_talk_autosave_does_not_consume_question(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.skills["酒馆攀谈"] = 1
        ilya.skill_counters["酒馆攀谈"] = 1
        message = "伊莉雅问酒客：旧路今晚有人走过吗？"

        with patch.object(
            self.service,
            "_autosave_campaign",
            side_effect=RuntimeError("磁盘写入失败"),
        ):
            receipt = self.service.gm_gameplay_tools.resolve_tavern_talk(
                gameplay_context(message),
                {
                    "actor": "伊莉雅",
                    "question": "旧路今晚有人走过吗？",
                    "public_answer": "有一辆没有挂灯的货车刚过去。",
                    "evidence": "旧路今晚有人走过吗",
                },
            )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "TAVERN_TALK_COMMIT_FAILED")
        restored = self.app.character_manager.get("伊莉雅")
        self.assertEqual(restored.skill_counters["酒馆攀谈"], 1)

    def test_state_summary_exposes_pending_and_persistent_zero_hp_state(self) -> None:
        self.app.conflict_manager.start_scene("断桥之战", ["伊莉雅", "洛岚"])
        self.app.character_manager.get("伊莉雅").hp = 0
        self.app.conflict_manager.resolve_zero_hp("伊莉雅")

        pending_state = self.service.gm_gameplay_tools.state_summary(
            gameplay_context("我选择放弃抵抗。")
        )
        pending_hero = next(
            item for item in pending_state["characters"] if item["name"] == "伊莉雅"
        )
        self.assertEqual(pending_hero["defeat_state"], "awaiting_zero_hp_choice")
        self.assertFalse(pending_hero["can_act"])

        self.app.conflict_manager.resolve_pending_zero_hp(
            "伊莉雅",
            choice="give_up_resistance",
            consequence="分离：被洪流冲到下游",
        )
        fallen_state = self.service.gm_gameplay_tools.state_summary(
            gameplay_context("伊莉雅现在怎么样？")
        )
        fallen_hero = next(
            item for item in fallen_state["characters"] if item["name"] == "伊莉雅"
        )
        self.assertEqual(fallen_hero["defeat_state"], "gave_up_resistance")
        self.assertEqual(
            fallen_hero["active_defeat_consequence"],
            "分离：被洪流冲到下游",
        )
        self.assertEqual(
            fallen_state["conflict"]["fallen_pcs"],
            {"伊莉雅": "分离：被洪流冲到下游"},
        )

    def test_zero_hp_tool_requires_one_concrete_gm_consequence(self) -> None:
        self.app.conflict_manager.start_scene("断桥之战", ["伊莉雅", "洛岚"])
        self.app.character_manager.get("伊莉雅").hp = 0
        self.app.conflict_manager.resolve_zero_hp("伊莉雅")
        window = self.app.interceptor.decision_window_manager.find_pending(
            kind="zero_hp",
            owner="伊莉雅",
        )

        receipt = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("伊莉雅选择放弃抵抗。"),
            {
                "action_type": "ResolveZeroHP",
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "give_up_resistance",
                "details": {},
                "evidence": "选择放弃抵抗",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "ZERO_HP_CONSEQUENCE_TYPE_REQUIRED")
        self.assertIsNotNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window.window_id
            )
        )

    def test_zero_hp_darkness_consequence_changes_theme_and_remains_queryable(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.theme = "希望"
        self.app.conflict_manager.start_scene("断桥之战", ["伊莉雅", "洛岚"])
        hero.hp = 0
        self.app.conflict_manager.resolve_zero_hp("伊莉雅")
        window = self.app.interceptor.decision_window_manager.find_pending(
            kind="zero_hp",
            owner="伊莉雅",
        )

        receipt = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("伊莉雅选择放弃抵抗。"),
            {
                "action_type": "ResolveZeroHP",
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "give_up_resistance",
                "details": {
                    "consequence_type": "黑暗",
                    "consequence": "眼看村庄陷落，原本的希望化作复仇",
                    "new_theme": "复仇",
                },
                "evidence": "选择放弃抵抗",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(hero.theme, "复仇")
        self.assertEqual(
            self.app.conflict_manager.state.fallen_pcs["伊莉雅"],
            "黑暗：眼看村庄陷落，原本的希望化作复仇",
        )
        self.assertIn("后果是", receipt.public_fallback_reply)

    def test_zero_hp_choice_resumes_the_attacker_turn_exactly_once(self) -> None:
        self.app.character_manager.add(
            Character(
                name="财团机兵",
                attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 6},
                max_hp=70,
                hp=70,
                max_mp=40,
                mp=40,
                defenses={"physical": 11, "magic": 8},
                traits=["enemy", "construct"],
            )
        )
        self.app.conflict_manager.start_scene(
            "断桥之战",
            ["财团机兵", "伊莉雅", "洛岚"],
        )
        self.app.character_manager.get("伊莉雅").hp = 0
        self.app.conflict_manager.resolve_zero_hp(
            "伊莉雅",
            source_actor="财团机兵",
        )
        window = self.app.interceptor.decision_window_manager.find_pending(
            kind="zero_hp",
            owner="伊莉雅",
        )

        receipt = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("伊莉雅选择放弃抵抗。"),
            {
                "action_type": "ResolveZeroHP",
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "give_up_resistance",
                "details": {
                    "consequence_type": "分离",
                    "consequence": "被冲击掀入断桥下方",
                },
                "evidence": "选择放弃抵抗",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(
            self.app.conflict_manager.state.current_actor(),
            "洛岚",
        )
        self.assertEqual(
            self.app.conflict_manager.state.turn_started_actor,
            "洛岚",
        )

    def test_zero_hp_window_and_consequence_survive_service_restart(self) -> None:
        self.app.conflict_manager.start_scene("断桥之战", ["伊莉雅", "洛岚"])
        self.app.character_manager.get("伊莉雅").hp = 0
        self.app.conflict_manager.resolve_zero_hp("伊莉雅")
        pending = self.app.interceptor.decision_window_manager.find_pending(
            kind="zero_hp",
            owner="伊莉雅",
        )
        self.service._save_campaign({"campaign_id": "gameplay-tool-test"})

        restarted = FUGMHttpService(data_root=self.tmpdir.name, use_llm=False)
        status, loaded = restarted._load_campaign(
            {"campaign_id": "gameplay-tool-test"}
        )
        self.assertEqual(status, 200, loaded)
        restarted_app = restarted._runtime("gameplay-tool-test").app
        restored_window = restarted_app.interceptor.decision_window_manager.find_pending(
            window_id=pending.window_id
        )
        self.assertIsNotNone(restored_window)
        self.assertEqual(
            restarted_app.conflict_manager.state.current_actor(),
            self.app.conflict_manager.state.current_actor(),
        )

        receipt = restarted.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("伊莉雅选择放弃抵抗。"),
            {
                "action_type": "ResolveZeroHP",
                "actor": "伊莉雅",
                "window_id": restored_window.window_id,
                "choice": "give_up_resistance",
                "details": {
                    "consequence_type": "分离",
                    "consequence": "被洪流冲到旧桥下游",
                },
                "evidence": "选择放弃抵抗",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(
            restarted_app.conflict_manager.state.fallen_pcs["伊莉雅"],
            "分离：被洪流冲到旧桥下游",
        )
        self.assertIsNone(
            restarted_app.interceptor.decision_window_manager.find_pending(
                window_id=pending.window_id
            )
        )

    def test_final_blow_player_decides_ordinary_npc_fate(self) -> None:
        self.app.character_manager.add(
            Character(
                name="财团斥候",
                attributes={"DEX": 8, "INS": 8, "MIG": 6, "WLP": 6},
                max_hp=30,
                hp=0,
                max_mp=30,
                mp=30,
                traits=["enemy"],
            )
        )
        self.app.conflict_manager.start_scene(
            "断桥之战",
            ["伊莉雅", "财团斥候", "洛岚"],
        )

        event = self.app.conflict_manager.resolve_zero_hp(
            "财团斥候",
            source_actor="伊莉雅",
        )
        window = self.app.interceptor.decision_window_manager.find_pending(
            kind="npc_fate",
            owner="伊莉雅",
        )
        self.assertEqual(event.event_type, "npc_fate_choice_required")
        self.assertIsNotNone(window)
        self.assertNotIn(
            {"action_type": "ResolveDecision", "choice": "accept_result", "label": "接受当前检定结果，不重掷"},
            self.service.gm_gameplay_tools._agent_decision_options(window),
        )

        receipt = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("我把斥候绑起来，留给守望会审问。"),
            {
                "action_type": "ResolveDecision",
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "capture",
                "details": {
                    "fate_description": "被绑起并交给守望会看管",
                },
                "evidence": "把斥候绑起来",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(
            self.app.conflict_manager.state.defeated_npc_fates["财团斥候"],
            "被绑起并交给守望会看管",
        )
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window.window_id
            )
        )
        self.assertIn("交给守望会看管", receipt.public_fallback_reply)
        self.assertEqual(
            self.app.conflict_manager.state.current_actor(),
            "洛岚",
        )
        self.assertEqual(
            self.app.conflict_manager.state.turn_started_actor,
            "洛岚",
        )

    def test_other_npc_fate_requires_players_description(self) -> None:
        self.app.character_manager.add(
            Character(
                name="失控魔像",
                attributes={"DEX": 6, "INS": 6, "MIG": 10, "WLP": 6},
                max_hp=40,
                hp=0,
                max_mp=30,
                mp=30,
                traits=["enemy"],
            )
        )
        self.app.conflict_manager.start_scene(
            "工坊冲突",
            ["伊莉雅", "失控魔像"],
        )
        self.app.conflict_manager.resolve_zero_hp(
            "失控魔像",
            source_actor="伊莉雅",
        )
        window = self.app.interceptor.decision_window_manager.find_pending(
            kind="npc_fate",
            owner="伊莉雅",
        )

        receipt = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("我想用别的方式处置它。"),
            {
                "action_type": "ResolveDecision",
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "other",
                "details": {},
                "evidence": "用别的方式处置",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "NPC_FATE_DESCRIPTION_REQUIRED")
        self.assertIsNotNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window.window_id
            )
        )

    def test_deterministic_in_scene_action_records_position_and_waits_for_full_party_round(self) -> None:
        self.app.clock_manager.add(
            Clock(
                name="财团巡逻队逼近",
                max_segments=6,
                clock_type="threat",
                auto_advance="每个行动轮结束时推进1格",
                scope="session",
            )
        )
        message = "伊莉雅沿着同一条风铃廊跟上巡守，在闸门边举盾守住旅人。"

        receipt = self.service.gm_gameplay_tools.perform_in_scene_action(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "action_summary": "伊莉雅跟上巡守并在闸门边守住旅人",
                "position_note": "风铃廊·闸门边",
                "evidence": "伊莉雅沿着同一条风铃廊跟上巡守",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(
            self.app.scene_manager.current_scene.participant_activities["伊莉雅"],
            "伊莉雅跟上巡守并在闸门边守住旅人",
        )
        self.assertEqual(
            self.app.scene_manager.location_of("伊莉雅"),
            "风铃廊",
        )
        self.assertEqual(
            self.app.scene_manager.position_of("伊莉雅"),
            "风铃廊·闸门边",
        )
        self.assertIn("洛岚", self.app.scene_manager.current_scene.participants)
        self.assertFalse(receipt.result["action_round"]["action_round_completed"])
        self.assertEqual(
            receipt.result["action_round"]["action_round_waiting_for"],
            ["洛岚"],
        )
        self.assertEqual(self.app.clock_manager.get("财团巡逻队逼近").current, 0)

    def test_in_scene_action_does_not_echo_a_player_only_position_change(self) -> None:
        self.app.clock_manager.add(
            Clock(
                name="财团巡逻队逼近",
                max_segments=6,
                clock_type="threat",
                auto_advance="每个行动轮结束时推进1格",
                scope="session",
            )
        )
        message = "伊莉雅走到失忆旅人身后，在风铃廊里站定。"

        receipt = self.service.gm_gameplay_tools.perform_in_scene_action(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "action_summary": "伊莉雅走到失忆旅人身后，在风铃廊里站定",
                "position_note": "风铃廊·失忆旅人身后",
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertTrue(receipt.state_changed)
        self.assertEqual(receipt.public_fallback_reply, "【财团巡逻队逼近】0/6")
        self.assertNotIn("伊莉雅走到", receipt.public_fallback_reply)
        self.assertTrue(receipt.lock_public_reply)

    def test_in_scene_action_without_external_result_or_clock_is_silent(self) -> None:
        message = "伊莉雅走到风铃廊入口站定。"

        receipt = self.service.gm_gameplay_tools.perform_in_scene_action(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "action_summary": "伊莉雅走到风铃廊入口站定",
                "position_note": "风铃廊入口",
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertTrue(receipt.state_changed)
        self.assertEqual(receipt.public_fallback_reply, "")
        self.assertFalse(receipt.lock_public_reply)

    def test_in_character_promise_records_only_speakers_action(self) -> None:
        message = (
            "伊莉雅压低声音对洛岚说：“我愿意承担失忆旅人的同行照看；"
            "请把这份承诺转告会长。”"
        )

        receipt = self.service.gm_gameplay_tools.perform_in_scene_action(
            gameplay_context(message, speaker="阿凛"),
            {
                "actor": "伊莉雅",
                "action_summary": "伊莉雅向洛岚承诺照看旅人，并请他转告会长",
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertTrue(receipt.state_changed)
        self.assertTrue(receipt.result["silent_commit_allowed"])
        self.assertEqual(
            self.app.scene_manager.current_scene.participant_activities["伊莉雅"],
            "伊莉雅向洛岚承诺照看旅人，并请他转告会长",
        )
        self.assertNotIn(
            "洛岚",
            self.app.scene_manager.current_scene.participant_activities,
        )

    def test_story_item_acquisition_persists_custody_across_reload(self) -> None:
        message = "洛岚立刻将油布旧册收好，准备把这份记录带离登记小室。"
        public_fact = "油布旧册现由洛岚持有。"

        receipt = self.service.gm_gameplay_tools.commit_story_item_action(
            gameplay_context(message, speaker="白河"),
            {
                "actor": "洛岚",
                "operation": "acquire",
                "item_name": "油布旧册",
                "description": "记有“借响者：伊瑟娅”的驿站登记册",
                "public_result": f"洛岚把油布旧册收入防水袋；{public_fact}",
                "public_fact": public_fact,
                "tags": ["线索", "登记册"],
                "evidence": "将油布旧册收好",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        item_id = receipt.result["story_item"]["item_id"]
        item = self.app.world_state.story_items[item_id]
        self.assertEqual(item.holder, "洛岚")
        self.assertEqual(item.status.value, "carried")
        self.assertIn(public_fact, self.app.scene_frame_manager.current_frame.public_facts)

        reloaded_service = FUGMHttpService(data_root=self.tmpdir.name, use_llm=False)
        reloaded = reloaded_service._runtime("gameplay-tool-test").app
        loaded_item = reloaded.world_state.story_items[item_id]
        self.assertEqual(loaded_item.holder, "洛岚")
        self.assertEqual(loaded_item.name, "油布旧册")
        self.assertEqual(loaded_item.history[-1].operation, "acquire")

    def test_story_item_acquisition_can_require_one_followup_check_without_consuming_round(self) -> None:
        self.app.clock_manager.add(
            Clock(
                name="巡逻逼近",
                max_segments=6,
                clock_type="threat",
                auto_advance="每个行动轮结束时推进1格",
                auto_advance_timing="action_round_end",
                scope="session",
            )
        )
        message = "洛岚拿起白蜡路封，沿着半环细缝检查里面的字。"
        receipt = self.service.gm_gameplay_tools.commit_story_item_action(
            gameplay_context(message, speaker="白河"),
            {
                "actor": "洛岚",
                "operation": "acquire",
                "item_name": "白蜡路封",
                "description": "刻有半环印记的旧路凭据",
                "public_result": "洛岚拿起白蜡路封；白蜡路封现由洛岚持有。",
                "public_fact": "白蜡路封现由洛岚持有。",
                "tags": ["线索", "凭证"],
                "continue_with_check": True,
                "evidence": "拿起白蜡路封，沿着半环细缝检查里面的字",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["required_followup_tools"], ["perform_check_action"])
        self.assertEqual(receipt.result["allowed_followup_tools"], ["perform_check_action"])
        self.assertEqual(receipt.result["action_round"], {})
        self.assertEqual(
            self.app.world_state.find_story_item(name="白蜡路封").holder,
            "洛岚",
        )
        self.assertEqual(self.app.clock_manager.get("巡逻逼近").current, 0)

    def test_story_item_cannot_be_acquired_twice_by_another_actor(self) -> None:
        first_message = "伊莉雅把黑色回执签收进盾后的夹层。"
        first = self.service.gm_gameplay_tools.commit_story_item_action(
            gameplay_context(first_message),
            {
                "actor": "伊莉雅",
                "operation": "acquire",
                "item_name": "黑色回执签",
                "public_result": "伊莉雅收起黑色回执签；黑色回执签现由伊莉雅持有。",
                "public_fact": "黑色回执签现由伊莉雅持有。",
                "evidence": "把黑色回执签收进盾后的夹层",
            },
        )
        self.assertTrue(first.ok, first.message)

        second_message = "洛岚也把黑色回执签收起来。"
        second = self.service.gm_gameplay_tools.commit_story_item_action(
            gameplay_context(second_message, speaker="白河"),
            {
                "actor": "洛岚",
                "operation": "acquire",
                "item_name": "黑色回执签",
                "item_id": first.result["story_item"]["item_id"],
                "public_result": "洛岚收起黑色回执签；黑色回执签现由洛岚持有。",
                "public_fact": "黑色回执签现由洛岚持有。",
                "evidence": "把黑色回执签收起来",
            },
        )

        self.assertFalse(second.ok)
        self.assertEqual(second.error_code, "STORY_ITEM_COMMIT_FAILED")
        item = self.app.world_state.find_story_item(name="黑色回执签")
        self.assertEqual(item.holder, "伊莉雅")

    def test_story_item_transfer_resolves_present_npc_alias_to_canonical_name(self) -> None:
        self.app.world_state.ensure_npc_persona(
            "白花守望会会长",
            aliases=["会长"],
            current_location="风铃廊",
        )
        self.app.scene_manager.add_participant("白花守望会会长")
        acquired = self.service.gm_gameplay_tools.commit_story_item_action(
            gameplay_context("伊莉雅拿起白花路牌。"),
            {
                "actor": "伊莉雅",
                "operation": "acquire",
                "item_name": "白花路牌",
                "public_result": "伊莉雅拿起白花路牌；白花路牌现由伊莉雅持有。",
                "public_fact": "白花路牌现由伊莉雅持有。",
                "evidence": "拿起白花路牌",
            },
        )
        self.assertTrue(acquired.ok, acquired.message)

        transferred = self.service.gm_gameplay_tools.commit_story_item_action(
            gameplay_context("伊莉雅把白花路牌交给会长。"),
            {
                "actor": "伊莉雅",
                "operation": "transfer",
                "item_name": "白花路牌",
                "item_id": acquired.result["story_item"]["item_id"],
                "to_holder": "会长",
                "public_result": "会长接过白花路牌；白花路牌现由白花守望会会长持有。",
                "public_fact": "白花路牌现由白花守望会会长持有。",
                "evidence": "把白花路牌交给会长",
            },
        )

        self.assertTrue(transferred.ok, transferred.message)
        item = self.app.world_state.find_story_item(name="白花路牌")
        self.assertEqual(item.holder, "白花守望会会长")
        self.assertEqual(item.history[-1].to_holder, "白花守望会会长")

    def test_explicit_scene_pass_completes_party_round_and_ticks_clock_once(self) -> None:
        self.app.clock_manager.add(
            Clock(
                name="财团巡逻队逼近",
                max_segments=6,
                clock_type="threat",
                auto_advance="每个行动轮结束时推进1格",
                scope="session",
            )
        )
        first = self.app.record_free_scene_player_action("伊莉雅")
        self.assertFalse(first["action_round_completed"])

        receipt = self.service.gm_gameplay_tools.pass_in_scene_action(
            gameplay_context("洛岚暂时不采取行动，先让伊莉雅处理。", speaker="白河"),
            {
                "actor": "洛岚",
                "evidence": "洛岚暂时不采取行动",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertTrue(receipt.state_changed)
        self.assertTrue(receipt.result["recorded"])
        self.assertTrue(receipt.result["action_round"]["action_round_completed"])
        self.assertEqual(self.app.clock_manager.get("财团巡逻队逼近").current, 1)
        self.assertEqual(receipt.public_fallback_reply, "【财团巡逻队逼近】1/6")
        self.assertTrue(receipt.lock_public_reply)

    def test_free_scene_round_ignores_pc_not_participating_in_this_session(self) -> None:
        self.app.character_manager.add(
            Character(
                name="赛璃",
                attributes={"DEX": 8, "INS": 8, "MIG": 6, "WLP": 10},
                max_hp=35,
                hp=35,
                max_mp=55,
                mp=55,
                traits=["pc"],
            )
        )
        self.app.session_ledger.start(
            "s1",
            participating_pcs=["伊莉雅", "洛岚"],
        )
        self.app.clock_manager.add(
            Clock(
                name="财团巡逻队逼近",
                max_segments=6,
                clock_type="threat",
                auto_advance="每个行动轮结束时推进1格",
                scope="session",
            )
        )

        first = self.app.record_free_scene_player_action("伊莉雅")
        second = self.app.record_free_scene_player_action("洛岚")

        self.assertFalse(first["action_round_completed"])
        self.assertEqual(first["action_round_waiting_for"], ["洛岚"])
        self.assertTrue(second["action_round_completed"])
        self.assertEqual(self.app.clock_manager.get("财团巡逻队逼近").current, 1)

    def test_scene_pass_is_silent_until_it_completes_the_full_party_round(self) -> None:
        self.app.clock_manager.add(
            Clock(
                name="财团巡逻队逼近",
                max_segments=6,
                clock_type="threat",
                auto_advance="每个行动轮结束时推进1格",
                scope="session",
            )
        )

        receipt = self.service.gm_gameplay_tools.pass_in_scene_action(
            gameplay_context("洛岚先等一等，这轮不行动。", speaker="白河"),
            {"actor": "洛岚", "evidence": "这轮不行动"},
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertTrue(receipt.state_changed)
        self.assertFalse(receipt.result["action_round"]["action_round_completed"])
        self.assertEqual(
            receipt.result["action_round"]["action_round_waiting_for"],
            ["伊莉雅"],
        )
        self.assertEqual(receipt.public_fallback_reply, "")
        self.assertFalse(receipt.lock_public_reply)
        self.assertTrue(receipt.result["silent_commit_allowed"])
        self.assertEqual(self.app.clock_manager.get("财团巡逻队逼近").current, 0)

    def test_scene_pass_without_action_round_pressure_is_a_noop(self) -> None:
        receipt = self.service.gm_gameplay_tools.pass_in_scene_action(
            gameplay_context("洛岚暂时不行动。", speaker="白河"),
            {"actor": "洛岚", "evidence": "暂时不行动"},
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertFalse(receipt.state_changed)
        self.assertFalse(receipt.result["recorded"])
        self.assertEqual(receipt.public_fallback_reply, "")

    def test_in_scene_action_rejects_a_character_outside_the_focused_scene(self) -> None:
        self.app.scene_manager.current_scene.participants.remove("洛岚")
        self.app.scene_manager.actor_locations["洛岚"] = "驿站外院"
        message = "洛岚检查旧钟。"

        receipt = self.service.gm_gameplay_tools.perform_in_scene_action(
            gameplay_context(message, speaker="白河"),
            {
                "actor": "洛岚",
                "action_summary": "洛岚检查旧钟",
                "public_result": "洛岚俯身查看旧钟。",
                "evidence": "洛岚检查旧钟",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "ACTOR_NOT_IN_FOCUSED_SCENE")
        self.assertIn("驿站外院", receipt.correction_hint)

    def test_structured_environment_investigation_uses_declared_subject_without_threat_side_effect(self) -> None:
        message = "伊莉雅观察风铃廊四周，确认守望会留下了什么暗号。"
        receipt = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context(message),
            {
                "action_type": "Investigate",
                "actor": "伊莉雅",
                "target": "风铃廊四周",
                "attributes": ["洞察", "洞察"],
                "difficulty": 7,
                "purpose": "寻找守望会留下的暗号",
                "check_label": "寻找守望会暗号",
                "success_observation": "一枚白花刻痕指向旧路内侧。",
                "failure_consequence": "廊下铃声盖住了细微动静，这次没能分辨出可靠暗号。",
                "details": {
                    "success_information": ["一枚白花刻痕指向旧路内侧。"],
                    "high_success_information": ["刻痕是今夜新留下的。"],
                },
                "evidence": "伊莉雅观察风铃廊四周",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertTrue(receipt.lock_public_reply)
        self.assertIn("寻找守望会暗号", receipt.public_fallback_reply)
        self.assertNotIn("对 风铃廊四周 的检定", receipt.public_fallback_reply)
        self.assertEqual(self.app.clock_manager.all(), [])

    def test_open_chest_rejects_unprepared_fixed_reward_and_cannot_repeat(self) -> None:
        message = "伊莉雅打开风铃廊角落里的旧木箱。"
        before_zenit = self.app.character_manager.get("伊莉雅").zenit

        injected = self.service.gm_gameplay_tools.perform_scene_action(
            gameplay_context(message),
            {
                "action_type": "OpenChest",
                "actor": "伊莉雅",
                "details": {
                    "chest_name": "风铃廊旧木箱",
                    "fixed_item": "不存在的神器",
                    "fixed_zenit": 9999,
                },
                "evidence": "打开风铃廊角落里的旧木箱",
            },
        )
        first = self.service.gm_gameplay_tools.perform_scene_action(
            gameplay_context(message),
            {
                "action_type": "OpenChest",
                "actor": "伊莉雅",
                "details": {
                    "chest_name": "风铃廊旧木箱",
                    "rarity": "standard",
                },
                "evidence": "打开风铃廊角落里的旧木箱",
            },
        )
        after_first = self.app.character_manager.get("伊莉雅").zenit
        repeated = self.service.gm_gameplay_tools.perform_scene_action(
            gameplay_context(message),
            {
                "action_type": "OpenChest",
                "actor": "伊莉雅",
                "details": {
                    "chest_name": "风铃廊旧木箱",
                    "rarity": "standard",
                },
                "evidence": "打开风铃廊角落里的旧木箱",
            },
        )

        self.assertFalse(injected.ok)
        self.assertEqual(injected.error_code, "UNPREPARED_FIXED_CHEST_REWARD")
        self.assertEqual(before_zenit, 0)
        self.assertTrue(first.ok, first.message)
        self.assertGreater(after_first, before_zenit)
        self.assertFalse(repeated.ok)
        self.assertEqual(repeated.error_code, "CHEST_ALREADY_OPENED")
        self.assertEqual(self.app.character_manager.get("伊莉雅").zenit, after_first)

    def test_dungeon_check_receipt_binds_final_roll_to_one_area_action(self) -> None:
        brief = self.app.dungeon_manager.design_dungeon(name="沉钟地窟")
        state = self.app.dungeon_manager.start_from_brief(
            brief,
            location="沉钟地窟入口",
        )
        area = state.areas[0]
        area.trap = "回声绊线"
        self.app.scene_manager.start_scene(
            "沉钟地窟",
            SceneType.DUNGEON,
            location="沉钟地窟入口",
            participants=["伊莉雅", "洛岚"],
        )
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["INS", "DEX"],
                dice=[(10, 7), (8, 5)],
                total=12,
                modifier=0,
                high_roll=7,
                target_number=9,
                success=True,
                critical_success=False,
                fumble=False,
                target=area.name,
                reason="解除回声绊线",
            )
        )
        message = f"伊莉雅仔细拆除{area.name}的回声绊线。"
        checked = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context(message),
            {
                "action_type": "RequestRoll",
                "actor": "伊莉雅",
                "target": "回声绊线",
                "attributes": ["洞察", "敏捷"],
                "difficulty": 9,
                "purpose": "拆除回声绊线",
                "check_label": "拆除回声绊线",
                "success_observation": "伊莉雅截断绊线，机关彻底失效。",
                "failure_consequence": "绊线牵动地窟深处的警铃。",
                "details": {"dungeon_area": area.name},
                "evidence": f"仔细拆除{area.name}的回声绊线",
            },
        )
        receipt_id = checked.result["check_receipt"]["receipt_id"]
        resolved = self.service.gm_gameplay_tools.perform_scene_action(
            gameplay_context(message),
            {
                "action_type": "ExploreDungeon",
                "actor": "伊莉雅",
                "details": {
                    "area_name": area.name,
                    "mode": "disarm_trap",
                    "check_receipt_id": receipt_id,
                },
                "evidence": f"仔细拆除{area.name}的回声绊线",
            },
        )
        repeated = self.service.gm_gameplay_tools.perform_scene_action(
            gameplay_context(message),
            {
                "action_type": "ExploreDungeon",
                "actor": "伊莉雅",
                "details": {
                    "area_name": area.name,
                    "mode": "disarm_trap",
                    "check_receipt_id": receipt_id,
                },
                "evidence": f"仔细拆除{area.name}的回声绊线",
            },
        )

        self.assertTrue(checked.ok, checked.message)
        self.assertTrue(checked.result["check_receipt"]["success"])
        self.assertTrue(resolved.ok, resolved.message)
        self.assertTrue(area.trap_disarmed)
        self.assertFalse(repeated.ok)
        self.assertEqual(
            repeated.error_code,
            "DUNGEON_CHECK_RECEIPT_ALREADY_USED",
        )

    def test_dungeon_success_flag_without_check_receipt_is_rejected(self) -> None:
        brief = self.app.dungeon_manager.design_dungeon(name="沉钟地窟")
        state = self.app.dungeon_manager.start_from_brief(
            brief,
            location="沉钟地窟入口",
        )
        area = state.areas[0]
        area.trap = "回声绊线"
        self.app.scene_manager.start_scene(
            "沉钟地窟",
            SceneType.DUNGEON,
            location="沉钟地窟入口",
            participants=["伊莉雅"],
        )
        message = "伊莉雅尝试拆除回声绊线。"

        receipt = self.service.gm_gameplay_tools.perform_scene_action(
            gameplay_context(message),
            {
                "action_type": "ExploreDungeon",
                "actor": "伊莉雅",
                "details": {
                    "area_name": area.name,
                    "mode": "disarm_trap",
                    "success": True,
                },
                "evidence": "尝试拆除回声绊线",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "DUNGEON_SUCCESS_RECEIPT_REQUIRED")
        self.assertFalse(area.trap_disarmed)

    def test_successful_check_commits_only_after_player_accepts_post_check_window(self) -> None:
        self.app.character_manager.get("伊莉雅").identity = "白花守望者"
        self.app.character_manager.get("伊莉雅").fabula_points = 3
        self.app.clock_manager.add(
            Clock(
                name="财团巡逻队逼近",
                max_segments=6,
                clock_type="threat",
                auto_advance="每个行动轮结束时推进1格",
                scope="session",
            )
        )
        self.app.record_free_scene_player_action("洛岚")
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["INS", "INS"],
                dice=[(10, 6), (10, 5)],
                total=11,
                modifier=0,
                high_roll=6,
                target_number=9,
                success=True,
                critical_success=False,
                fumble=False,
                target="门外车轮声",
                reason="辨认车轮方向",
            )
        )

        provisional = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context("伊莉雅辨听门外车轮声是在靠近还是离开。"),
            {
                "action_type": "Investigate",
                "actor": "伊莉雅",
                "target": "门外车轮声",
                "attributes": ["洞察", "洞察"],
                "difficulty": 9,
                "purpose": "辨认车轮方向",
                "check_label": "辨听车轮方向",
                "success_observation": "车轮声正沿驿站外路向登记小室靠近。",
                "failure_consequence": "回声在墙间折返，这次无法判断车轮方向。",
                "details": {},
                "evidence": "辨听门外车轮声",
            },
        )

        self.assertTrue(provisional.ok, provisional.message)
        self.assertEqual(self.app.clock_manager.get("财团巡逻队逼近").current, 0)
        self.assertNotIn("车轮声正沿", provisional.public_fallback_reply)
        self.assertNotIn("【财团巡逻队逼近】1/6", provisional.public_fallback_reply)
        pending = provisional.result["pending_decisions"]
        self.assertTrue(pending)
        self.assertTrue(pending[0]["blocking"])
        self.assertTrue(pending[0]["roll_success"])
        self.assertIn(
            {
                "action_type": "ResolveDecision",
                "choice": "accept_result",
                "label": "接受当前检定结果，不重掷",
            },
            pending[0]["resolution_options"],
        )

        invalid_decline = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("我接受这次结果，不重掷。"),
            {
                "action_type": "InvokeTrait",
                "actor": "伊莉雅",
                "window_id": pending[0]["window_id"],
                "choice": "decline",
                "details": {},
                "evidence": "接受这次结果",
            },
        )

        self.assertFalse(invalid_decline.ok)
        self.assertEqual(invalid_decline.error_code, "ILLEGAL_TRAIT_INVOCATION")
        self.assertEqual(self.app.clock_manager.get("财团巡逻队逼近").current, 0)
        self.assertTrue(
            self.app.interceptor.decision_window_manager.pending(
                kind="trait_invocation",
                owner="伊莉雅",
            )
        )

        accepted = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("我接受这次结果，不重掷。"),
            {
                "action_type": "ResolveDecision",
                "actor": "伊莉雅",
                "window_id": pending[0]["window_id"],
                "choice": "accept_result",
                "details": {},
                "evidence": "接受这次结果",
            },
        )

        self.assertTrue(accepted.ok, accepted.message)
        self.assertIn("车轮声正沿驿站外路向登记小室靠近。", accepted.public_fallback_reply)
        self.assertIn("【财团巡逻队逼近】1/6", accepted.public_fallback_reply)
        self.assertEqual(self.app.clock_manager.get("财团巡逻队逼近").current, 1)
        self.assertFalse(
            self.app.interceptor.decision_window_manager.pending(
                kind="trait_invocation",
                owner="伊莉雅",
            )
        )

    def test_successful_escort_check_moves_pc_and_npc_only_after_final_acceptance(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.identity = "白花护送者"
        hero.fabula_points = 3
        self.app.world_state.ensure_npc_persona(
            "失忆旅人",
            active_goal="跟随可信任的护送者离开风铃廊",
        )
        self.app.scene_manager.add_participant("失忆旅人", location="风铃廊")

        source = self.app.scene_manager.current_scene
        source.participants.remove("洛岚")
        source.participant_locations.pop("洛岚", None)
        self.app.scene_manager.actor_locations["洛岚"] = "登记小室"
        destination, mode = self.app.scene_manager.focus_actor_branch(
            "洛岚",
            name="登记小室",
            location="登记小室",
        )
        self.assertEqual(mode, "created")
        restored, mode = self.app.scene_manager.focus_actor_branch(
            "伊莉雅",
            name="白花碑驿站",
            location="风铃廊",
        )
        self.assertIs(restored, source)
        self.assertEqual(mode, "restored")

        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["WLP", "INS"],
                dice=[(6, 5), (10, 7)],
                total=12,
                modifier=0,
                high_roll=7,
                target_number=7,
                success=True,
                critical_success=False,
                fumble=False,
                target="失忆旅人",
                reason="护送失忆旅人进入登记小室",
            )
        )
        message = "伊莉雅牵着失忆旅人进入登记小室，把他带到洛岚身边。"
        provisional = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context(message),
            {
                "action_type": "RequestRoll",
                "actor": "伊莉雅",
                "target": "失忆旅人",
                "attributes": ["意志", "洞察"],
                "difficulty": 7,
                "purpose": "护送失忆旅人进入登记小室",
                "check_label": "护送失忆旅人",
                "success_observation": "伊莉雅与失忆旅人一同抵达登记小室，洛岚就在门内接应。",
                "failure_consequence": "失忆旅人在门槛前停住，双方仍留在风铃廊。",
                "success_transition": {
                    "destination": "登记小室",
                    "participants": ["伊莉雅", "失忆旅人"],
                    "scene_name": "登记小室",
                },
                "evidence": "伊莉雅牵着失忆旅人进入登记小室",
            },
        )

        self.assertTrue(provisional.ok, provisional.message)
        self.assertEqual(self.app.scene_manager.location_of("伊莉雅"), "风铃廊")
        self.assertEqual(self.app.scene_manager.location_of("失忆旅人"), "风铃廊")
        pending = provisional.result["pending_decisions"]
        self.assertTrue(pending)

        accepted = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("我接受这次结果，不重掷。"),
            {
                "action_type": "ResolveDecision",
                "actor": "伊莉雅",
                "window_id": pending[0]["window_id"],
                "choice": "accept_result",
                "details": {},
                "evidence": "接受这次结果",
            },
        )

        self.assertTrue(accepted.ok, accepted.message)
        self.assertEqual(self.app.scene_manager.current_scene.scene_id, destination.scene_id)
        self.assertEqual(self.app.scene_manager.location_of("伊莉雅"), "登记小室")
        self.assertEqual(self.app.scene_manager.location_of("失忆旅人"), "登记小室")
        self.assertIn("伊莉雅", self.app.scene_manager.current_scene.participants)
        self.assertIn("失忆旅人", self.app.scene_manager.current_scene.participants)
        self.assertNotIn("伊莉雅", source.participants)
        self.assertNotIn("失忆旅人", source.participants)
        self.assertEqual(
            self.app.world_state.npc_personas["失忆旅人"].current_location,
            "登记小室",
        )

    def test_failed_escort_check_never_commits_success_transition(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.identity = "白花护送者"
        hero.fabula_points = 3
        self.app.world_state.ensure_npc_persona("失忆旅人")
        self.app.scene_manager.add_participant("失忆旅人", location="风铃廊")
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["WLP", "INS"],
                dice=[(6, 1), (10, 2)],
                total=3,
                modifier=0,
                high_roll=2,
                target_number=7,
                success=False,
                critical_success=False,
                fumble=False,
                target="失忆旅人",
                reason="护送失忆旅人进入登记小室",
            )
        )
        provisional = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context("伊莉雅牵着失忆旅人进入登记小室。"),
            {
                "action_type": "RequestRoll",
                "actor": "伊莉雅",
                "target": "失忆旅人",
                "attributes": ["意志", "洞察"],
                "difficulty": 7,
                "purpose": "护送失忆旅人进入登记小室",
                "check_label": "护送失忆旅人",
                "success_observation": "伊莉雅与失忆旅人一同抵达登记小室。",
                "failure_consequence": "失忆旅人在门槛前停住，双方仍留在风铃廊。",
                "success_transition": {
                    "destination": "登记小室",
                    "participants": ["伊莉雅", "失忆旅人"],
                },
                "evidence": "伊莉雅牵着失忆旅人进入登记小室",
            },
        )
        pending = provisional.result["pending_decisions"]
        accepted = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("我接受失败结果，不重掷。"),
            {
                "action_type": "ResolveDecision",
                "actor": "伊莉雅",
                "window_id": pending[0]["window_id"],
                "choice": "accept_result",
                "details": {},
                "evidence": "接受失败结果",
            },
        )

        self.assertTrue(accepted.ok, accepted.message)
        self.assertEqual(self.app.scene_manager.location_of("伊莉雅"), "风铃廊")
        self.assertEqual(self.app.scene_manager.location_of("失忆旅人"), "风铃廊")

    def test_check_transition_requires_public_destination_before_roll(self) -> None:
        receipt = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context("伊莉雅贴墙退往风铃廊。"),
            {
                "action_type": "RequestRoll",
                "actor": "伊莉雅",
                "target": "门外巡逻灯影",
                "attributes": ["敏捷", "洞察"],
                "difficulty": 9,
                "purpose": "避开灯影退往风铃廊",
                "check_label": "贴墙撤离",
                "success_observation": "伊莉雅避开灯影，退到门外安全位置。",
                "failure_consequence": "灯影扫过门缝，伊莉雅只能停在原地。",
                "success_transition": {
                    "destination": "白花碑驿站·风铃廊",
                    "participants": ["伊莉雅"],
                },
                "evidence": "伊莉雅贴墙退往风铃廊",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "SUCCESS_TRANSITION_PUBLIC_DESTINATION_REQUIRED",
        )
        self.assertFalse(
            self.app.interceptor.decision_window_manager.pending(owner="伊莉雅")
        )

    def test_check_transition_accepts_natural_final_location_name(self) -> None:
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["DEX", "INS"],
                dice=[(8, 5), (8, 5)],
                total=10,
                modifier=0,
                high_roll=5,
                target_number=9,
                success=True,
                critical_success=False,
                fumble=False,
                target="门外巡逻灯影",
                reason="避开灯影退往风铃廊",
            )
        )
        receipt = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context("伊莉雅贴墙退往风铃廊。"),
            {
                "action_type": "RequestRoll",
                "actor": "伊莉雅",
                "target": "门外巡逻灯影",
                "attributes": ["敏捷", "洞察"],
                "difficulty": 9,
                "purpose": "避开灯影退往风铃廊",
                "check_label": "贴墙撤离",
                "success_observation": "伊莉雅避开灯影，抵达风铃廊。",
                "failure_consequence": "灯影扫过门缝，伊莉雅只能停在原地。",
                "success_transition": {
                    "destination": "白花碑驿站·风铃廊",
                    "participants": ["伊莉雅"],
                },
                "evidence": "伊莉雅贴墙退往风铃廊",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)

    def test_trait_window_treats_reroll_dice_two_as_both_dice_not_index_two(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.identity = "白花护送者"
        hero.fabula_points = 3
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["DEX", "INS"],
                dice=[(8, 1), (8, 2)],
                total=3,
                modifier=0,
                high_roll=2,
                target_number=9,
                success=False,
                critical_success=False,
                fumble=False,
                target="门外巡逻灯影",
                reason="避开灯影",
            )
        )
        provisional = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context("伊莉雅观察门外灯影。"),
            {
                "action_type": "Investigate",
                "actor": "伊莉雅",
                "target": "门外巡逻灯影",
                "attributes": ["敏捷", "洞察"],
                "difficulty": 9,
                "purpose": "观察门外灯影",
                "check_label": "观察灯影",
                "success_observation": "伊莉雅看清了灯影移动规律。",
                "failure_consequence": "灯影互相交叠，暂时看不清规律。",
                "evidence": "伊莉雅观察门外灯影",
            },
        )
        window_id = provisional.result["pending_decisions"][0]["window_id"]
        captured: dict[str, object] = {}
        original_reroll = self.app.interceptor.rules_engine.reroll_outcome

        def capture_reroll(outcome, reroll_indices=None, **kwargs):
            captured["indices"] = reroll_indices
            return original_reroll(outcome, reroll_indices, **kwargs)

        self.app.interceptor.rules_engine.reroll_outcome = capture_reroll
        receipt = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("我援用白花护送者，重掷两枚骰。"),
            {
                "action_type": "InvokeTrait",
                "actor": "伊莉雅",
                "window_id": window_id,
                "choice": "白花护送者",
                "details": {"reroll_dice": 2},
                "evidence": "援用白花护送者，重掷两枚骰",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertIsNone(captured["indices"])

    def test_escort_transition_accepts_parent_and_child_locations_in_same_origin_scene(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.identity = "白花护送者"
        self.app.world_state.ensure_npc_persona(
            "失忆旅人",
            active_goal="跟随伊莉雅进入登记小室",
        )
        self.app.scene_manager.set_participant_location("伊莉雅", "白花碑驿站")
        self.app.scene_manager.add_participant(
            "失忆旅人",
            location="白花碑驿站·风铃廊",
        )
        self.assertTrue(
            self.app.scene_manager.actors_share_movement_origin(
                "伊莉雅",
                "失忆旅人",
            )
        )

        receipt = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context("伊莉雅牵着失忆旅人进入登记小室。"),
            {
                "action_type": "RequestRoll",
                "actor": "伊莉雅",
                "target": "失忆旅人",
                "attributes": ["意志", "洞察"],
                "difficulty": 7,
                "purpose": "护送失忆旅人进入登记小室",
                "check_label": "护送失忆旅人",
                "success_observation": "伊莉雅与失忆旅人一同抵达白花碑驿站·登记小室。",
                "failure_consequence": "两人被门边震动阻住，仍留在原处。",
                "success_transition": {
                    "destination": "白花碑驿站·登记小室",
                    "participants": ["伊莉雅", "失忆旅人"],
                },
                "evidence": "伊莉雅牵着失忆旅人进入登记小室",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)

    def test_old_shared_scene_does_not_authorize_companion_after_actor_moved(self) -> None:
        self.app.world_state.ensure_npc_persona("失忆旅人")
        self.app.scene_manager.add_participant("失忆旅人", location="风铃廊")
        source = self.app.scene_manager.current_scene
        source.participants.remove("伊莉雅")
        source.participant_locations.pop("伊莉雅", None)
        self.app.scene_manager.actor_locations["伊莉雅"] = "登记小室"

        self.assertFalse(
            self.app.scene_manager.actors_share_movement_origin(
                "伊莉雅",
                "失忆旅人",
            )
        )

    def test_resolved_scene_group_movement_moves_pc_and_consenting_npc(self) -> None:
        persona = self.app.world_state.ensure_npc_persona(
            "失忆旅人",
            current_stance="跟随伊莉雅，不单独行动",
            active_goal="与伊莉雅一起进入登记小室",
        )
        persona.current_location = "白花碑驿站·风铃廊"
        self.app.scene_manager.set_participant_location("伊莉雅", "白花碑驿站")
        self.app.scene_manager.add_participant(
            "失忆旅人",
            location="白花碑驿站·风铃廊",
        )
        source = self.app.scene_manager.current_scene
        source.participants.remove("洛岚")
        source.participant_locations.pop("洛岚", None)
        self.app.scene_manager.actor_locations["洛岚"] = "白花碑驿站·登记小室"
        destination, mode = self.app.scene_manager.focus_actor_branch(
            "洛岚",
            name="登记小室",
            location="白花碑驿站·登记小室",
        )
        self.assertEqual(mode, "created")

        message = "伊莉雅牵着失忆旅人进入白花碑驿站·登记小室。"
        receipt = self.service.gm_gameplay_tools.move_scene_group(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "companions": ["失忆旅人"],
                "destination": "白花碑驿站·登记小室",
                "action_summary": "伊莉雅牵着失忆旅人进入登记小室",
                "public_result": "伊莉雅与失忆旅人一同抵达白花碑驿站·登记小室。",
                "position_note": "登记小室入口内侧",
                "companion_positions": {"失忆旅人": "伊莉雅身侧"},
                "evidence": "伊莉雅牵着失忆旅人进入白花碑驿站·登记小室",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(self.app.scene_manager.current_scene.scene_id, destination.scene_id)
        self.assertEqual(
            self.app.scene_manager.location_of("伊莉雅"),
            "白花碑驿站·登记小室",
        )
        self.assertEqual(
            self.app.scene_manager.location_of("失忆旅人"),
            "白花碑驿站·登记小室",
        )
        self.assertNotIn("伊莉雅", source.participants)
        self.assertNotIn("失忆旅人", source.participants)
        self.assertEqual(
            self.app.world_state.npc_personas["失忆旅人"].current_location,
            "白花碑驿站·登记小室",
        )
        self.assertEqual(
            self.app.scene_manager.position_of("失忆旅人"),
            "伊莉雅身侧",
        )

    def test_resolved_scene_group_movement_allows_pc_to_join_active_scene_alone(self) -> None:
        source = self.app.scene_manager.current_scene
        source.participants.remove("伊莉雅")
        source.participant_locations.pop("伊莉雅", None)
        self.app.scene_manager.actor_locations["伊莉雅"] = "白花碑驿站·风铃廊"
        source.participants.remove("洛岚")
        source.participant_locations.pop("洛岚", None)
        self.app.scene_manager.actor_locations["洛岚"] = (
            "白花碑驿站·登记小室"
        )
        destination, mode = self.app.scene_manager.focus_actor_branch(
            "洛岚",
            name="登记小室",
            location="白花碑驿站·登记小室",
        )
        self.assertEqual(mode, "created")
        message = "伊莉雅贴着内侧单列通过窄道，前往白花碑驿站·登记小室。"

        receipt = self.service.gm_gameplay_tools.move_scene_group(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "companions": [],
                "destination": "白花碑驿站·登记小室",
                "action_summary": "伊莉雅通过窄道进入登记小室",
                "public_result": "伊莉雅穿过窄道，抵达白花碑驿站·登记小室。",
                "evidence": "伊莉雅贴着内侧单列通过窄道，前往白花碑驿站·登记小室",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(self.app.scene_manager.current_scene.scene_id, destination.scene_id)
        self.assertEqual(receipt.result["companions"], [])
        self.assertEqual(
            self.app.scene_manager.location_of("伊莉雅"),
            "白花碑驿站·登记小室",
        )
        self.assertIn("伊莉雅", destination.participants)
        self.assertNotIn("伊莉雅", source.participants)

    def test_scene_group_movement_requires_destination_npc_followup(self) -> None:
        persona = self.app.world_state.ensure_npc_persona(
            "白花守望会会长",
            current_stance="要求所有人先说明来意",
            active_goal="守住白花碑驿站的中立立场",
        )
        persona.current_location = "白花碑驿站·会长室"
        self.app.scene_manager.actor_locations[persona.name] = persona.current_location
        destination, mode = self.app.scene_manager.focus_actor_branch(
            persona.name,
            name="会长室",
            location=persona.current_location,
        )
        self.assertEqual(mode, "created")
        self.app.world_state.update_npc_state(
            persona.name,
            location=persona.current_location,
            scene=destination.scene_id,
        )
        source, mode = self.app.scene_manager.focus_actor_branch(
            "伊莉雅",
            name="风铃廊",
            location="风铃廊",
        )
        self.assertEqual(mode, "restored")
        message = "伊莉雅去会长室，当面问会长是否愿意开放旧路。"

        receipt = self.service.gm_gameplay_tools.move_scene_group(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "companions": [],
                "destination": "白花碑驿站·会长室",
                "action_summary": "伊莉雅前往会长室并当面询问是否开放旧路",
                "public_result": "伊莉雅抵达白花碑驿站·会长室。",
                "followup_npc_name": "白花守望会会长",
                "followup_response_instruction": "回应伊莉雅是否愿意开放旧路。",
                "evidence": "伊莉雅去会长室，当面问会长是否愿意开放旧路",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(
            receipt.result["required_followup_tools"],
            ["decide_npc_response"],
        )
        followup = receipt.result["required_followup_calls"][0]
        self.assertEqual(followup["tool_name"], "decide_npc_response")
        self.assertEqual(
            followup["arguments"]["name"],
            "白花守望会会长",
        )
        self.assertEqual(followup["arguments"]["actor"], "伊莉雅")
        self.assertIn(
            "是否愿意开放旧路",
            followup["arguments"]["response_instruction"],
        )
        self.assertEqual(
            self.app.scene_manager.current_scene.scene_id,
            destination.scene_id,
        )
        self.assertNotIn("伊莉雅", source.participants)

    def test_scene_group_movement_rejects_followup_npc_at_other_location(self) -> None:
        persona = self.app.world_state.ensure_npc_persona(
            "白花守望会会长",
        )
        persona.current_location = "钟鸣公国"
        self.app.scene_manager.actor_locations[persona.name] = "钟鸣公国"
        message = "伊莉雅去会长室，当面问会长是否愿意开放旧路。"

        receipt = self.service.gm_gameplay_tools.move_scene_group(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "companions": [],
                "destination": "白花碑驿站·会长室",
                "action_summary": "伊莉雅前往会长室并当面询问是否开放旧路",
                "public_result": "伊莉雅抵达白花碑驿站·会长室。",
                "followup_npc_name": "白花守望会会长",
                "followup_response_instruction": "回应伊莉雅是否愿意开放旧路。",
                "evidence": "伊莉雅去会长室，当面问会长是否愿意开放旧路",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "NPC_NOT_AT_DESTINATION")
        self.assertEqual(self.app.scene_manager.current_scene.location, "风铃廊")

    def test_local_group_movement_updates_positions_without_splitting_scene_or_echoing(self) -> None:
        persona = self.app.world_state.ensure_npc_persona(
            "失忆旅人",
            current_stance="愿意在伊莉雅护送下走回白花碑旁",
            active_goal="跟随伊莉雅完成回撤演练",
        )
        scene = self.app.scene_manager.current_scene
        persona.current_location = str(scene.location or scene.name)
        self.app.scene_manager.add_participant(
            "失忆旅人",
            location=str(scene.location or scene.name),
        )
        original_scene_id = scene.scene_id
        original_scene_location = scene.location
        original_loran_position = self.app.scene_manager.position_of("洛岚")
        message = (
            "伊莉雅向洛岚打出回撤手势，护着失忆旅人沿风铃廊内侧退向白花碑旁。"
        )

        receipt = self.service.gm_gameplay_tools.move_group_within_scene(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "companions": ["失忆旅人"],
                "destination_position": "白花碑旁",
                "action_summary": "伊莉雅护着失忆旅人退向白花碑旁",
                "public_result": "",
                "evidence": "护着失忆旅人沿风铃廊内侧退向白花碑旁",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(self.app.scene_manager.current_scene.scene_id, original_scene_id)
        self.assertEqual(self.app.scene_manager.current_scene.location, original_scene_location)
        self.assertEqual(self.app.scene_manager.position_of("伊莉雅"), "白花碑旁")
        self.assertEqual(self.app.scene_manager.position_of("失忆旅人"), "白花碑旁")
        self.assertEqual(
            self.app.scene_manager.position_of("洛岚"),
            original_loran_position,
        )
        self.assertEqual(receipt.public_fallback_reply, "")
        self.assertFalse(receipt.lock_public_reply)

    def test_local_group_movement_marks_condition_fulfilled_and_requires_npc_payoff(self) -> None:
        scene = self.app.scene_manager.current_scene
        self.app.scene_frame_manager.ensure_frame(
            scene=scene,
            recent_chat="",
            world_state=self.app.world_state,
            character_manager=self.app.character_manager,
        )
        chair = self.app.world_state.ensure_npc_persona(
            "白花守望会会长",
            current_stance="监督护送路线演练",
            active_goal="确认路线后兑现放行承诺",
        )
        traveler = self.app.world_state.ensure_npc_persona(
            "失忆旅人",
            current_stance="愿意跟随伊莉雅走完演练路线",
            active_goal="跟随伊莉雅抵达旧路闸门",
        )
        for persona in (chair, traveler):
            persona.current_location = str(scene.location or scene.name)
            self.app.scene_manager.add_participant(
                persona.name,
                location=str(scene.location or scene.name),
            )
        condition = self.app.scene_frame_manager.record_condition(
            npc="白花守望会会长",
            condition="由伊莉雅护送失忆旅人实际走完风铃廊至旧路闸门的路线",
            promised_result="打开旧路闸门并交出白花通行牌",
            required_actor="伊莉雅",
            scene=scene,
        )
        message = "伊莉雅护着失忆旅人沿风铃廊内侧抵达旧路闸门，实际走完演练路线。"

        receipt = self.service.gm_gameplay_tools.move_group_within_scene(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "companions": ["失忆旅人"],
                "destination_position": "旧路闸门前",
                "action_summary": "伊莉雅护送失忆旅人走完演练路线",
                "public_result": "",
                "condition_id": condition["condition_id"],
                "evidence": "护着失忆旅人沿风铃廊内侧抵达旧路闸门",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(condition["status"], "open")
        self.assertEqual(condition["player_fulfillment"], "fulfilled")
        self.assertEqual(
            receipt.result["required_followup_tools"],
            ["decide_npc_response"],
        )
        self.assertEqual(
            receipt.result["condition_payoff_due_from"],
            "白花守望会会长",
        )
        self.assertEqual(
            receipt.result["required_followup_calls"],
            [
                {
                    "tool_name": "decide_npc_response",
                    "arguments": {
                        "name": "白花守望会会长",
                        "actor": "伊莉雅",
                        "condition_id": condition["condition_id"],
                    },
                    "authority_reason": (
                        "本次移动完成了公开条件中的玩家义务，"
                        "现在由条件所有者决定并兑现promised_result。"
                    ),
                }
            ],
        )
        self.assertEqual(self.app.scene_manager.position_of("伊莉雅"), "旧路闸门前")
        self.assertEqual(
            self.app.scene_manager.position_of("失忆旅人"),
            "旧路闸门前",
        )

    def test_successful_bound_check_marks_condition_fulfilled_and_requires_npc_payoff(self) -> None:
        scene = self.app.scene_manager.current_scene
        self.app.scene_frame_manager.ensure_frame(
            scene=scene,
            recent_chat="",
            world_state=self.app.world_state,
            character_manager=self.app.character_manager,
        )
        chair = self.app.world_state.ensure_npc_persona(
            "白花守望会会长",
            current_stance="要求伊莉雅证明风铃暗号",
            active_goal="确认暗号后开放旧路",
        )
        chair.current_location = str(scene.location or scene.name)
        self.app.scene_manager.add_participant(
            chair.name,
            location=str(scene.location or scene.name),
        )
        condition = self.app.scene_frame_manager.record_condition(
            npc=chair.name,
            condition="由伊莉雅准确复原风铃暗号",
            promised_result="打开旧路闸门",
            required_actor="伊莉雅",
            scene=scene,
        )
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["INS", "WLP"],
                dice=[(10, 7), (6, 4)],
                total=11,
                modifier=0,
                high_roll=7,
                target_number=9,
                success=True,
                critical_success=False,
                fumble=False,
                target="白花风铃",
                reason="复原风铃暗号",
            )
        )

        receipt = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context("伊莉雅按旧谱依次敲响三枚白花风铃，复原完整暗号。"),
            {
                "action_type": "RequestRoll",
                "actor": "伊莉雅",
                "target": "白花风铃",
                "attributes": ["洞察", "意志"],
                "difficulty": 9,
                "purpose": "复原完整风铃暗号",
                "check_label": "复原风铃暗号",
                "success_observation": "三段铃音与守望会旧谱完全吻合。",
                "failure_consequence": "最后一段铃音走调，暗号没有成立。",
                "condition_id": condition["condition_id"],
                "details": {},
                "evidence": "复原完整暗号",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(condition["status"], "open")
        self.assertEqual(condition["player_fulfillment"], "fulfilled")
        self.assertEqual(receipt.result["fulfilled_condition"]["condition_id"], condition["condition_id"])
        self.assertEqual(receipt.result["required_followup_tools"], ["decide_npc_response"])
        self.assertEqual(
            receipt.result["required_followup_calls"][0]["arguments"],
            {
                "name": chair.name,
                "actor": "伊莉雅",
                "condition_id": condition["condition_id"],
            },
        )

    def test_failed_bound_check_keeps_condition_pending_without_npc_payoff(self) -> None:
        scene = self.app.scene_manager.current_scene
        self.app.scene_frame_manager.ensure_frame(
            scene=scene,
            recent_chat="",
            world_state=self.app.world_state,
            character_manager=self.app.character_manager,
        )
        chair = self.app.world_state.ensure_npc_persona(
            "白花守望会会长",
            current_stance="要求伊莉雅证明风铃暗号",
            active_goal="确认暗号后开放旧路",
        )
        chair.current_location = str(scene.location or scene.name)
        self.app.scene_manager.add_participant(
            chair.name,
            location=str(scene.location or scene.name),
        )
        condition = self.app.scene_frame_manager.record_condition(
            npc=chair.name,
            condition="由伊莉雅准确复原风铃暗号",
            promised_result="打开旧路闸门",
            required_actor="伊莉雅",
            scene=scene,
        )
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["INS", "WLP"],
                dice=[(10, 2), (6, 3)],
                total=5,
                modifier=0,
                high_roll=3,
                target_number=9,
                success=False,
                critical_success=False,
                fumble=False,
                target="白花风铃",
                reason="复原风铃暗号",
            )
        )

        receipt = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context("伊莉雅按旧谱敲响白花风铃，试着复原完整暗号。"),
            {
                "action_type": "RequestRoll",
                "actor": "伊莉雅",
                "target": "白花风铃",
                "attributes": ["洞察", "意志"],
                "difficulty": 9,
                "purpose": "复原完整风铃暗号",
                "check_label": "复原风铃暗号",
                "success_observation": "三段铃音与守望会旧谱完全吻合。",
                "failure_consequence": "最后一段铃音走调，暗号没有成立。",
                "condition_id": condition["condition_id"],
                "details": {},
                "evidence": "试着复原完整暗号",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(condition["player_fulfillment"], "pending")
        self.assertEqual(receipt.result["fulfilled_condition"], {})
        self.assertEqual(receipt.result["required_followup_tools"], [])

    def test_local_group_movement_triggers_exact_deferred_npc_commitment(self) -> None:
        scene = self.app.scene_manager.current_scene
        frame = self.app.scene_frame_manager.ensure_frame(
            scene=scene,
            recent_chat="",
            world_state=self.app.world_state,
            character_manager=self.app.character_manager,
        )
        guard = self.app.world_state.ensure_npc_persona(
            "白花守望者",
            current_stance="已经答应在旧路闸门前为队伍带路",
            active_goal="带队通过旧路闸门",
        )
        guard.current_location = str(scene.location or scene.name)
        self.app.scene_manager.add_participant(
            guard.name,
            location=str(scene.location or scene.name),
        )
        commitment = (
            self.app.scene_frame_manager.npc_deferred_commitment_manager.record_from_public_answer(
                frame,
                npc="白花守望会会长",
                public_statement="守望者会在旧路闸门前为你们带路。",
                speech_plan={
                    "deferred_action": "白花守望者前往旧路闸门并在那里带路",
                    "deferred_result": "在旧路闸门前为队伍带路",
                    "deferred_trigger": "队伍抵达旧路闸门",
                },
            )
        )
        message = "伊莉雅跟着白花守望者抵达旧路闸门。"

        receipt = self.service.gm_gameplay_tools.move_group_within_scene(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "companions": ["白花守望者"],
                "destination_position": "旧路闸门前",
                "action_summary": "伊莉雅跟随白花守望者抵达旧路闸门",
                "public_result": "",
                "commitment_id": commitment["commitment_id"],
                "commitment_responder": "白花守望者",
                "evidence": "跟着白花守望者抵达旧路闸门",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(commitment["trigger_status"], "reached")
        self.assertEqual(commitment["trigger_responder"], "白花守望者")
        self.assertEqual(
            receipt.result["triggered_commitment"]["commitment_id"],
            commitment["commitment_id"],
        )
        self.assertEqual(
            receipt.result["required_followup_calls"],
            [
                {
                    "tool_name": "decide_npc_response",
                    "arguments": {
                        "name": "白花守望者",
                        "actor": "伊莉雅",
                        "commitment_id": commitment["commitment_id"],
                    },
                    "authority_reason": (
                        "本次移动抵达了NPC短期承诺的公开触发点，"
                        "现在由随行兑现者当场完成promised_result。"
                    ),
                }
            ],
        )

    def test_deferred_commitment_responder_must_be_among_moving_companions(self) -> None:
        scene = self.app.scene_manager.current_scene
        frame = self.app.scene_frame_manager.ensure_frame(
            scene=scene,
            recent_chat="",
            world_state=self.app.world_state,
            character_manager=self.app.character_manager,
        )
        for name in ("白花守望者", "失忆旅人"):
            persona = self.app.world_state.ensure_npc_persona(name)
            persona.current_location = str(scene.location or scene.name)
            self.app.scene_manager.add_participant(
                name,
                location=str(scene.location or scene.name),
            )
        commitment = (
            self.app.scene_frame_manager.npc_deferred_commitment_manager.record_from_public_answer(
                frame,
                npc="白花守望会会长",
                public_statement="守望者会在旧路闸门前为你们带路。",
                speech_plan={
                    "deferred_action": "白花守望者前往旧路闸门并在那里带路",
                    "deferred_result": "在旧路闸门前为队伍带路",
                    "deferred_trigger": "队伍抵达旧路闸门",
                },
            )
        )

        receipt = self.service.gm_gameplay_tools.move_group_within_scene(
            gameplay_context("伊莉雅带着失忆旅人抵达旧路闸门。"),
            {
                "actor": "伊莉雅",
                "companions": ["失忆旅人"],
                "destination_position": "旧路闸门前",
                "action_summary": "伊莉雅带着失忆旅人抵达旧路闸门",
                "commitment_id": commitment["commitment_id"],
                "commitment_responder": "白花守望者",
                "evidence": "带着失忆旅人抵达旧路闸门",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "NPC_COMMITMENT_RESPONDER_NOT_MOVING",
        )
        self.assertEqual(commitment["trigger_status"], "waiting")

    def test_scene_group_movement_carries_triggered_commitment_into_destination(self) -> None:
        scene = self.app.scene_manager.current_scene
        frame = self.app.scene_frame_manager.ensure_frame(
            scene=scene,
            recent_chat="",
            world_state=self.app.world_state,
            character_manager=self.app.character_manager,
        )
        guard = self.app.world_state.ensure_npc_persona(
            "白花守望者",
            current_stance="正带队前往旧路入口",
            active_goal="抵达后为队伍带路",
        )
        guard.current_location = str(scene.location or scene.name)
        self.app.scene_manager.add_participant(
            guard.name,
            location=str(scene.location or scene.name),
        )
        commitment = (
            self.app.scene_frame_manager.npc_deferred_commitment_manager.record_from_public_answer(
                frame,
                npc="白花守望会会长",
                public_statement="守望者会在旧路入口等你们，并在那里带路。",
                speech_plan={
                    "deferred_action": "白花守望者前往旧路入口并在那里带路",
                    "deferred_result": "在旧路入口为队伍带路",
                    "deferred_trigger": "队伍抵达旧路入口",
                },
            )
        )
        message = "伊莉雅跟上白花守望者，迅速前往旧路入口。"

        receipt = self.service.gm_gameplay_tools.move_scene_group(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "companions": ["白花守望者"],
                "destination": "旧路入口",
                "action_summary": "伊莉雅跟随白花守望者前往旧路入口",
                "public_result": "伊莉雅与白花守望者已经抵达旧路入口。",
                "commitment_id": commitment["commitment_id"],
                "commitment_responder": "白花守望者",
                "evidence": "跟上白花守望者，迅速前往旧路入口",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        current_frame = self.app.scene_frame_manager.current_frame
        inherited = (
            self.app.scene_frame_manager.npc_deferred_commitment_manager.find_pending(
                current_frame,
                commitment["commitment_id"],
            )
        )
        self.assertIsNotNone(inherited)
        self.assertEqual(inherited["trigger_status"], "reached")
        self.assertEqual(inherited["trigger_location"], "旧路入口")
        self.assertEqual(
            receipt.result["required_followup_tools"],
            ["decide_npc_response"],
        )

    def test_resolved_scene_group_movement_rejects_stale_shared_scene(self) -> None:
        persona = self.app.world_state.ensure_npc_persona("失忆旅人")
        persona.current_location = "风铃廊"
        self.app.scene_manager.add_participant("失忆旅人", location="风铃廊")
        source = self.app.scene_manager.current_scene
        source.participants.remove("伊莉雅")
        source.participant_locations.pop("伊莉雅", None)
        self.app.scene_manager.actor_locations["伊莉雅"] = "登记小室"

        receipt = self.service.gm_gameplay_tools.move_scene_group(
            gameplay_context("伊莉雅把失忆旅人带进登记小室。"),
            {
                "actor": "伊莉雅",
                "companions": ["失忆旅人"],
                "destination": "登记小室",
                "action_summary": "伊莉雅把失忆旅人带进登记小室",
                "public_result": "伊莉雅与失忆旅人抵达登记小室。",
                "evidence": "伊莉雅把失忆旅人带进登记小室",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "COMPANION_NOT_AT_ORIGIN")

    def test_planned_hinder_success_always_publishes_its_concrete_result(self) -> None:
        self.app.clock_manager.add(
            Clock(name="财团巡逻队逼近", max_segments=8, current=0, clock_type="threat")
        )
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["DEX", "INS"],
                dice=[(8, 6), (10, 5)],
                total=11,
                modifier=0,
                high_roll=6,
                target_number=9,
                success=True,
                critical_success=False,
                fumble=False,
                margin=2,
                reason="干扰车队灯带",
            )
        )
        message = "伊莉雅用盾面反光干扰车队灯带，让来车放慢。"

        receipt = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context(message),
            {
                "action_type": "Hinder",
                "actor": "伊莉雅",
                "target": "雾中财团车队",
                "attributes": ["敏捷", "洞察"],
                "difficulty": 9,
                "purpose": "用反光干扰驾驶视野，让车队放慢",
                "check_label": "反光干扰车队",
                "success_observation": "最前方的车灯偏向路肩，整列车队明显减速。",
                "failure_consequence": "反光没能照进驾驶位，车队仍按原速逼近。",
                "details": {"clock_name": "财团巡逻队逼近"},
                "evidence": "用盾面反光干扰车队灯带",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertIn("最前方的车灯偏向路肩", receipt.public_fallback_reply)
        self.assertIn("【妨碍】", receipt.public_fallback_reply)

    def test_invalid_difficulty_is_rejected_before_any_roll(self) -> None:
        message = "伊莉雅试着撬开旧路闸门。"
        before_memories = list(self.app.world_state.memories)
        receipt = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context(message),
            {
                "action_type": "Objective",
                "actor": "伊莉雅",
                "target": "旧路闸门",
                "attributes": ["力量", "力量"],
                "difficulty": 0,
                "purpose": "撬开闸门",
                "evidence": "伊莉雅试着撬开旧路闸门",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "INVALID_DIFFICULTY")
        self.assertEqual(self.app.world_state.memories, before_memories)

    def test_unowned_skill_failure_rolls_back_and_returns_repairable_receipt(self) -> None:
        message = "伊莉雅使用火山烧穿闸门。"
        before_mp = self.app.character_manager.get("伊莉雅").mp
        receipt = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(message),
            {
                "action_type": "Skill",
                "actor": "伊莉雅",
                "target": "旧路闸门",
                "details": {"skill_name": "火山"},
                "evidence": "伊莉雅使用火山烧穿闸门",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "SKILL_NOT_LEARNED")
        self.assertIn("尚未拥有", receipt.message)
        self.assertEqual(self.app.character_manager.get("伊莉雅").mp, before_mp)

    def test_rule_action_exception_rolls_back_every_mutable_campaign_domain(self) -> None:
        self.app.clock_manager.add(Clock(name="财团巡逻队逼近", max_segments=6, current=1))
        self.app.conflict_manager.start_scene("风铃廊冲突", ["伊莉雅", "洛岚"])
        before_actor = self.app.conflict_manager.state.current_actor()
        before_round = self.app.conflict_manager.state.round_number
        before_rng = self.app.interceptor.rules_engine._rng.getstate()
        before_travel_history = list(self.app.travel_manager.history)
        before_routes = list(self.app.world_map_manager.route_plans)

        def fail_after_partial_commit(*_args, **_kwargs):
            self.app.character_manager.get("伊莉雅").hp = 1
            self.app.character_manager.get("伊莉雅").mp = 0
            self.app.clock_manager.get("财团巡逻队逼近").current = 5
            self.app.conflict_manager.next_turn()
            self.app.travel_manager.history.append({"kind": "partial-write"})
            self.app.world_map_manager.route_plans.append({"kind": "partial-route"})
            self.app.interceptor.rules_engine._rng.random()
            raise RuntimeError("模拟表达或存档阶段失败")

        with patch.object(self.app, "run_structured_turn", side_effect=fail_after_partial_commit):
            receipt = self.service.gm_gameplay_tools.perform_character_action(
                gameplay_context("伊莉雅举盾防御。"),
                {
                    "action_type": "Guard",
                    "actor": "伊莉雅",
                    "details": {},
                    "evidence": "伊莉雅举盾防御",
                },
            )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "RULE_ACTION_REJECTED")
        self.assertEqual(self.app.character_manager.get("伊莉雅").hp, 45)
        self.assertEqual(self.app.character_manager.get("伊莉雅").mp, 35)
        self.assertEqual(self.app.clock_manager.get("财团巡逻队逼近").current, 1)
        self.assertEqual(self.app.conflict_manager.state.current_actor(), before_actor)
        self.assertEqual(self.app.conflict_manager.state.round_number, before_round)
        self.assertEqual(self.app.travel_manager.history, before_travel_history)
        self.assertEqual(self.app.world_map_manager.route_plans, before_routes)
        self.assertEqual(self.app.interceptor.rules_engine._rng.getstate(), before_rng)

    def test_failed_rule_action_restores_orchestrator_ephemeral_state(self) -> None:
        self.app._surfaced_topic_memory_paths = {"before/topic.md"}
        self.app._world_map_generation_status = {
            "status": "ready",
            "attempts": 1,
        }
        self.app.recent_pipeline_spans = [{"turn": "before"}]
        self.app.last_resolved_check_event_id = "before-receipt"
        self.app.last_gm_beat_diagnostics = [{"beat": "before"}]
        self.app.last_gm_beat_fidelity_diagnostics = [
            {"fidelity": "before"}
        ]
        self.app.interceptor._advancing_check_batches = False

        def fail_after_runtime_caches_changed(*_args, **_kwargs):
            self.app._surfaced_topic_memory_paths.add("failed/topic.md")
            self.app._world_map_generation_status = {
                "status": "failed",
                "attempts": 9,
            }
            self.app.recent_pipeline_spans.append({"turn": "failed"})
            self.app.last_resolved_check_event_id = "failed-receipt"
            self.app.last_gm_beat_diagnostics.append({"beat": "failed"})
            self.app.last_gm_beat_fidelity_diagnostics.append(
                {"fidelity": "failed"}
            )
            self.app.interceptor._advancing_check_batches = True
            raise RuntimeError("模拟存档失败")

        with patch.object(
            self.service,
            "_autosave_campaign",
            side_effect=fail_after_runtime_caches_changed,
        ):
            receipt = self.service.gm_gameplay_tools.perform_character_action(
                gameplay_context("伊莉雅举盾防御。"),
                {
                    "action_type": "Guard",
                    "actor": "伊莉雅",
                    "details": {},
                    "evidence": "伊莉雅举盾防御",
                },
            )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            self.app._surfaced_topic_memory_paths,
            {"before/topic.md"},
        )
        self.assertEqual(
            self.app._world_map_generation_status,
            {"status": "ready", "attempts": 1},
        )
        self.assertEqual(
            self.app.recent_pipeline_spans,
            [{"turn": "before"}],
        )
        self.assertEqual(
            self.app.last_resolved_check_event_id,
            "before-receipt",
        )
        self.assertEqual(
            self.app.last_gm_beat_diagnostics,
            [{"beat": "before"}],
        )
        self.assertEqual(
            self.app.last_gm_beat_fidelity_diagnostics,
            [{"fidelity": "before"}],
        )
        self.assertFalse(self.app.interceptor._advancing_check_batches)

    def test_spell_granting_skill_is_clarified_before_rules_state_changes(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.skills = {"熵系魔法": 1}
        hero.spells = ["影袭"]
        before_mp = hero.mp
        before_memories = list(self.app.world_state.memories)
        message = "伊莉雅施展熵系魔法。"

        receipt = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(message),
            {
                "action_type": "Skill",
                "actor": "伊莉雅",
                "details": {"skill_name": "熵系魔法"},
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "SPELL_GRANTING_SKILL_IS_NOT_SPELL")
        self.assertIn("不是可以直接施放的法术", receipt.public_fallback_reply)
        self.assertIn("影袭", receipt.public_fallback_reply)
        self.assertNotIn("passive_hard", receipt.public_fallback_reply)
        self.assertEqual(hero.mp, before_mp)
        self.assertEqual(self.app.world_state.memories, before_memories)

    def test_passive_skill_cannot_be_committed_as_placeholder_action(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.skills = {"闪避": 1}
        message = "伊莉雅使用闪避。"

        receipt = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(message),
            {
                "action_type": "Skill",
                "actor": "伊莉雅",
                "details": {"skill_name": "闪避"},
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "PASSIVE_SKILL_IS_NOT_ACTION")
        self.assertIn("不是可以单独发动", receipt.public_fallback_reply)

    def test_bind_and_summon_infers_the_only_recorded_arcanum_contract(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.skills = {"契约与召唤": 1}
        hero.bound_arcana = ["魔典奥灵"]
        hero.max_mp = 60
        hero.mp = 60
        message = "伊莉雅消耗40点精神值，召唤自己已结契的奥灵。"

        receipt = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(message),
            {
                "action_type": "Skill",
                "actor": "伊莉雅",
                "details": {"skill_name": "契约与召唤"},
                "evidence": "召唤自己已结契的奥灵",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(hero.active_arcanum, "魔典")
        self.assertEqual(hero.mp, 20)
        self.assertIn("魔典奥灵", receipt.public_fallback_reply)
        self.assertNotIn("熔炉奥灵", receipt.public_fallback_reply)
        self.assertEqual(receipt.result["committed_action"]["arcanum"], "魔典奥灵")

    def test_bind_and_summon_requires_a_choice_for_multiple_contracts(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.skills = {"契约与召唤": 1}
        hero.bound_arcana = ["魔典奥灵", "天空奥灵"]
        hero.max_mp = 60
        hero.mp = 60
        message = "伊莉雅召唤自己已结契的奥灵。"

        receipt = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(message),
            {
                "action_type": "Skill",
                "actor": "伊莉雅",
                "details": {"skill_name": "契约与召唤"},
                "evidence": "召唤自己已结契的奥灵",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "ARCANUM_SELECTION_REQUIRED")
        self.assertIn("魔典奥灵", receipt.correction_hint)
        self.assertIn("天空奥灵", receipt.correction_hint)
        self.assertEqual(hero.mp, 60)
        self.assertEqual(hero.active_arcanum, "")

    def test_generic_portable_device_scan_is_rejected_without_committing_state(self) -> None:
        hero = self.app.character_manager.get("洛岚")
        hero.skills = {"便携装置": 1}
        hero.skill_options = {"便携装置": ["魔导装置"]}
        hero.inventory_points = 6
        before_memories = list(self.app.world_state.memories)
        before_round = self.app.scene_manager.current_scene.action_round_acted_actors.copy()
        message = "洛岚启动便携装置，朝雾中校准声源。"

        receipt = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(message, speaker="白河"),
            {
                "action_type": "TinkererGadget",
                "actor": "洛岚",
                "details": {
                    "device": "便携装置（魔导装置）",
                    "purpose": "校准雾中的车轮声源",
                },
                "evidence": "洛岚启动便携装置",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "GADGET_RULE_FUNCTION_REQUIRED")
        self.assertIn("普通检定", receipt.correction_hint)
        self.assertEqual(hero.inventory_points, 6)
        self.assertEqual(self.app.world_state.memories, before_memories)
        self.assertEqual(
            self.app.scene_manager.current_scene.action_round_acted_actors,
            before_round,
        )

    def test_unlocked_device_family_does_not_grant_advanced_magicannon(self) -> None:
        hero = self.app.character_manager.get("洛岚")
        hero.skills = {"便携装置": 1}
        hero.skill_options = {"便携装置": ["魔导装置"]}
        hero.inventory_points = 6
        before_equipment = list(hero.equipment)
        message = "洛岚用魔导装置制造一门火系魔法加农炮。"

        receipt = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(message, speaker="白河"),
            {
                "action_type": "TinkererGadget",
                "actor": "洛岚",
                "details": {
                    "gadget_type": "魔导装置",
                    "mode": "魔法加农炮",
                    "damage_type": "火",
                },
                "evidence": "洛岚用魔导装置制造一门火系魔法加农炮",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "GADGET_FUNCTION_NOT_UNLOCKED")
        self.assertEqual(hero.inventory_points, 6)
        self.assertEqual(hero.equipment, before_equipment)

    def test_gadget_runtime_failure_rolls_back_strict_tool_transaction(self) -> None:
        hero = self.app.character_manager.get("洛岚")
        hero.skills = {"便携装置": 2}
        hero.skill_options = {"便携装置": ["魔导装置", "魔导装置"]}
        hero.inventory_points = 0
        before_equipment = list(hero.equipment)
        before_memories = list(self.app.world_state.memories)
        message = "洛岚用魔导装置制造一门火系魔法加农炮。"

        receipt = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(message, speaker="白河"),
            {
                "action_type": "TinkererGadget",
                "actor": "洛岚",
                "details": {
                    "gadget_type": "魔导装置",
                    "mode": "魔法加农炮",
                    "damage_type": "火",
                },
                "evidence": "洛岚用魔导装置制造一门火系魔法加农炮",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "RULE_ACTION_REJECTED")
        self.assertEqual(hero.inventory_points, 0)
        self.assertEqual(hero.equipment, before_equipment)
        self.assertEqual(self.app.world_state.memories, before_memories)

    def test_infusion_must_be_part_of_attack_in_typed_tool_boundary(self) -> None:
        hero = self.app.character_manager.get("洛岚")
        hero.skills = {"便携装置": 1}
        hero.skill_options = {"便携装置": ["注魔装置"]}
        hero.inventory_points = 6
        message = "洛岚单独发动电击注魔。"

        receipt = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(message, speaker="白河"),
            {
                "action_type": "TinkererGadget",
                "actor": "洛岚",
                "details": {
                    "gadget_type": "注魔装置",
                    "infusion_name": "电击",
                },
                "evidence": "洛岚单独发动电击注魔",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "INFUSION_REQUIRES_ATTACK")
        self.assertIn("Attack", receipt.correction_hint)
        self.assertEqual(hero.inventory_points, 6)

    def test_typed_non_offensive_spell_normalizes_name_and_never_becomes_generic_check(self) -> None:
        self.app.character_manager.get("伊莉雅").spells = ["屏障"]
        self.app.scene_manager.current_scene.participants.append("失名旅人")
        message = "伊莉雅施放【屏障】，保护失名旅人、洛岚和自己。"

        receipt = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(message),
            {
                "action_type": "Spell",
                "actor": "伊莉雅",
                "target": "失名旅人、洛岚、伊莉雅",
                "details": {"spell_name": "【屏障】"},
                "evidence": "伊莉雅施放【屏障】",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertNotIn("检定", receipt.public_fallback_reply)
        self.assertNotIn("重掷", receipt.public_fallback_reply)
        self.assertEqual(receipt.result["pending_decisions"], [])
        self.assertEqual(receipt.result["committed_action"]["spell_name"], "屏障")
        self.assertCountEqual(
            receipt.result["committed_action"]["targets"],
            ["失名旅人", "洛岚", "伊莉雅"],
        )
        self.assertEqual(self.app.character_manager.get("伊莉雅").mp, 20)

    def test_spell_aliases_and_in_scene_position_preserve_npc_target(self) -> None:
        caster = self.app.character_manager.get("伊莉雅")
        caster.spells = ["元素幕障"]
        self.app.scene_manager.add_participant("失忆旅人")
        self.app.scene_manager.set_participant_position("伊莉雅", "失忆旅人前方")
        original_scene = self.app.scene_manager.current_scene
        message = "伊莉雅施放元素幕障，选择土，保护自己和失忆旅人。"

        receipt = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(message),
            {
                "action_type": "Spell",
                "actor": "伊莉雅",
                "details": {
                    "spell": "元素幕障",
                    "element": "土",
                    "targets": ["伊莉雅", "失忆旅人"],
                },
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertIs(self.app.scene_manager.current_scene, original_scene)
        self.assertEqual(self.app.scene_manager.location_of("伊莉雅"), "风铃廊")
        self.assertEqual(self.app.scene_manager.position_of("伊莉雅"), "失忆旅人前方")
        self.assertIn("失忆旅人", original_scene.participants)
        self.assertEqual(receipt.result["pending_decisions"], [])
        self.assertIn("对土系伤害获得抵抗相性", receipt.public_fallback_reply)
        self.assertCountEqual(
            receipt.result["committed_action"]["targets"],
            ["伊莉雅", "失忆旅人"],
        )

    def test_spell_damage_type_accepts_explicit_element_wording_without_decision_window(self) -> None:
        caster = self.app.character_manager.get("伊莉雅")
        caster.spells = ["元素幕障"]
        self.app.scene_manager.add_participant("失忆旅人")
        message = "伊莉雅施放元素幕障，选择土元素，以自己和失忆旅人为目标。"

        receipt = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(message),
            {
                "action_type": "Spell",
                "actor": "伊莉雅",
                "target": "伊莉雅、失忆旅人",
                "details": {
                    "spell_name": "元素幕障",
                    "chosen_damage_type": "土元素",
                    "targets": ["伊莉雅", "失忆旅人"],
                },
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["pending_decisions"], [])
        self.assertEqual(
            receipt.result["committed_action"]["chosen_damage_type"],
            "earth",
        )
        self.assertIn("对土系伤害获得抵抗相性", receipt.public_fallback_reply)

    def test_typed_unknown_spell_is_rejected_instead_of_becoming_improv_magic(self) -> None:
        self.app.character_manager.get("伊莉雅").spells = ["屏障"]
        message = "伊莉雅施放【屏障术】，保护洛岚。"

        receipt = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(message),
            {
                "action_type": "Spell",
                "actor": "伊莉雅",
                "target": "洛岚",
                "details": {"spell_name": "屏障术"},
                "evidence": "伊莉雅施放【屏障术】",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "UNKNOWN_SPELL_NAME")
        self.assertEqual(self.app.character_manager.get("伊莉雅").mp, 35)

    def test_speaker_cannot_submit_another_players_character_action(self) -> None:
        message = "洛岚调查旧钟。"
        receipt = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context(message, speaker="阿凛"),
            {
                "action_type": "Investigate",
                "actor": "洛岚",
                "target": "旧钟",
                "attributes": ["洞察", "洞察"],
                "difficulty": 7,
                "purpose": "辨认旧钟结构",
                "check_label": "辨认旧钟结构",
                "success_observation": "钟轴上的磨损表明它最近被人反向转动过。",
                "failure_consequence": "积尘遮住了钟轴接缝，暂时无法确认它的结构。",
                "evidence": "洛岚调查旧钟",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "ACTOR_NOT_CONTROLLED_BY_SPEAKER")

    def test_unmapped_speaker_cannot_control_a_character_with_known_owner(self) -> None:
        message = "旁观者替伊莉雅调查旧钟。"

        receipt = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context(message, speaker="旁观者"),
            {
                "action_type": "Investigate",
                "actor": "伊莉雅",
                "target": "旧钟",
                "attributes": ["洞察", "洞察"],
                "difficulty": 7,
                "purpose": "辨认旧钟结构",
                "check_label": "辨认旧钟结构",
                "success_observation": "钟轴最近被反向转动过。",
                "failure_consequence": "积尘遮住了钟轴接缝。",
                "evidence": "替伊莉雅调查旧钟",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "ACTOR_NOT_CONTROLLED_BY_SPEAKER")

    def test_rest_scene_action_does_not_heal_a_remote_split_party(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        loran = self.app.character_manager.get("洛岚")
        ilya.hp = 10
        ilya.mp = 5
        ilya.statuses = [StatusEffect.SLOW]
        loran.hp = 8
        loran.mp = 4
        loran.statuses = [StatusEffect.SHAKEN]
        self.app.scene_manager.current_scene.participants.remove("洛岚")
        self.app.scene_manager.current_scene.participant_locations.pop("洛岚", None)
        self.app.scene_manager.actor_locations["洛岚"] = "驿站外院"
        message = "伊莉雅留在风铃廊的安全客房休息。"

        receipt = self.service.gm_gameplay_tools.perform_scene_action(
            gameplay_context(message),
            {
                "action_type": "Rest",
                "actor": "伊莉雅",
                "details": {
                    "rest_type": "settlement",
                    "safe_source": "风铃廊安全客房",
                    "rest_source_kind": "hospitality",
                },
                "evidence": "伊莉雅留在风铃廊的安全客房休息",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(ilya.hp, ilya.max_hp)
        self.assertEqual(ilya.mp, ilya.max_mp)
        self.assertEqual(ilya.statuses, [])
        self.assertEqual(loran.hp, 8)
        self.assertEqual(loran.mp, 4)
        self.assertEqual(loran.statuses, [StatusEffect.SHAKEN])

    def test_rest_rejects_explicit_remote_participant(self) -> None:
        self.app.scene_manager.current_scene.participants.remove("洛岚")
        self.app.scene_manager.current_scene.participant_locations.pop("洛岚", None)
        self.app.scene_manager.actor_locations["洛岚"] = "驿站外院"
        message = "伊莉雅留在风铃廊的安全客房休息。"

        receipt = self.service.gm_gameplay_tools.perform_scene_action(
            gameplay_context(message),
            {
                "action_type": "Rest",
                "actor": "伊莉雅",
                "details": {
                    "rest_type": "settlement",
                    "safe_source": "风铃廊安全客房",
                    "rest_source_kind": "hospitality",
                    "participants": ["伊莉雅", "洛岚"],
                },
                "evidence": "伊莉雅留在风铃廊的安全客房休息",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "REST_PARTICIPANT_NOT_PRESENT")

    def test_rest_ends_scene_lifecycle_and_advances_only_registered_pressure(self) -> None:
        old_scene = self.app.scene_manager.current_scene
        self.app.clock_manager.add(
            Clock(
                name="守住风铃廊",
                max_segments=6,
                scope="scene",
            )
        )
        self.app.clock_manager.add(
            Clock(
                name="财团封锁全境",
                max_segments=8,
                clock_type="villain",
                scope="campaign",
                advance_on_rest=True,
            )
        )
        window = self.app.interceptor.decision_window_manager.create(
            kind="post_check",
            owner="伊莉雅",
            scope_kind="scene",
            scope_id=old_scene.scene_id,
            blocking=False,
        )
        self.app.conflict_manager.apply_guard("伊莉雅")

        receipt = self.service.gm_gameplay_tools.perform_scene_action(
            gameplay_context("伊莉雅与洛岚在安全客房休息。"),
            {
                "action_type": "Rest",
                "actor": "伊莉雅",
                "details": {
                    "rest_type": "settlement",
                    "safe_source": "风铃廊安全客房",
                    "rest_source_kind": "hospitality",
                },
                "evidence": "伊莉雅与洛岚在安全客房休息",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertFalse(self.app.clock_manager.exists("守住风铃廊"))
        self.assertEqual(self.app.clock_manager.get("财团封锁全境").current, 1)
        self.assertEqual(window.status.value, "expired")
        self.assertFalse(self.app.character_manager.get("伊莉雅").guarding)
        self.assertIsNotNone(self.app.scene_manager.current_scene)
        self.assertEqual(self.app.scene_manager.current_scene.scene_type, old_scene.scene_type)
        self.assertEqual(
            self.app.scene_manager.current_scene.participants,
            ["伊莉雅", "洛岚"],
        )
        self.assertEqual(
            self.app.scene_manager.current_scene.objective,
            old_scene.objective,
        )
        self.assertIn(
            "众人已在风铃廊安全客房完成休息。",
            self.app.scene_manager.current_scene.summary,
        )
        self.assertNotEqual(self.app.scene_manager.current_scene.scene_id, old_scene.scene_id)
        self.assertTrue(
            any(scene.scene_id == old_scene.scene_id for scene in self.app.scene_manager.history)
        )

    def test_rest_preserves_dungeon_context_after_the_rest_scene(self) -> None:
        old_scene = self.app.scene_manager.current_scene
        old_scene.scene_type = SceneType.DUNGEON
        old_scene.name = "钢铁墓园"
        old_scene.location = "守墓人营火"
        old_scene.objective = "找到通往核心墓室的路"
        old_scene.summary = "队伍已开启北侧闸门。"

        receipt = self.service.gm_gameplay_tools.perform_scene_action(
            gameplay_context("伊莉雅与洛岚在守墓人营火旁休息。"),
            {
                "action_type": "Rest",
                "actor": "伊莉雅",
                "details": {
                    "rest_type": "wilderness",
                    "safe_source": "守墓人营火",
                    "rest_source_kind": "hospitality",
                },
                "evidence": "在守墓人营火旁休息",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        scene = self.app.scene_manager.current_scene
        self.assertEqual(scene.scene_type, SceneType.DUNGEON)
        self.assertEqual(scene.name, "钢铁墓园·休息之后")
        self.assertEqual(scene.location, "守墓人营火")
        self.assertEqual(scene.objective, "找到通往核心墓室的路")
        self.assertIn("队伍已开启北侧闸门。", scene.summary)

    def test_rest_cannot_skip_a_pending_travel_event(self) -> None:
        self.app.world_map_manager.add_location("钟鸣公国", terrain="城市")
        self.app.interceptor.rules_engine.roll_die = lambda _sides: 8
        self.app.travel_manager.begin_journey(
            journey_id="rest-pending-travel",
            origin="白花碑驿站",
            destination="钟鸣公国",
            party_names=["伊莉雅", "洛岚"],
            threat_levels=[TravelThreatLevel.LOW],
            distance=1,
        )
        self.app.travel_manager.advance_active_journey()
        before_scene = self.app.scene_manager.current_scene

        receipt = self.service.gm_gameplay_tools.perform_scene_action(
            gameplay_context("伊莉雅想在途中先搭帐篷休息。"),
            {
                "action_type": "Rest",
                "actor": "伊莉雅",
                "details": {
                    "rest_type": "wilderness",
                    "safe_source": "魔法帐篷",
                    "rest_source_kind": "tent",
                    "payer": "伊莉雅",
                },
                "evidence": "在途中先搭帐篷休息",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "TRAVEL_EVENT_PENDING")
        self.assertIs(self.app.scene_manager.current_scene, before_scene)

    def test_rest_cannot_make_an_unrelated_objective_clock_advance(self) -> None:
        self.app.clock_manager.add(
            Clock(
                name="说服守望会",
                max_segments=6,
                current=2,
                clock_type="objective",
                scope="campaign",
            )
        )

        receipt = self.service.gm_gameplay_tools.perform_scene_action(
            gameplay_context("伊莉雅在安全客房休息。"),
            {
                "action_type": "Rest",
                "actor": "伊莉雅",
                "details": {
                    "rest_type": "settlement",
                    "safe_source": "风铃廊安全客房",
                    "rest_source_kind": "hospitality",
                    "threat_clocks": ["说服守望会"],
                },
                "evidence": "伊莉雅在安全客房休息",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "REST_CLOCK_NOT_ELIGIBLE")
        self.assertEqual(self.app.clock_manager.get("说服守望会").current, 2)
        self.assertEqual(self.app.scene_manager.current_scene.name, "白花碑驿站")

    def test_lodging_charges_each_resting_character_and_recovers_them_atomically(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        loran = self.app.character_manager.get("洛岚")
        ilya.zenit = 100
        ilya.hp = 5
        loran.hp = 7

        receipt = self.service.gm_gameplay_tools.perform_scene_action(
            gameplay_context("伊莉雅请两人在城里的旅馆住一晚，由她付钱。"),
            {
                "action_type": "Rest",
                "actor": "伊莉雅",
                "details": {
                    "rest_type": "settlement",
                    "safe_source": "白花碑旅馆",
                    "rest_source_kind": "lodging",
                    "settlement_size": "city",
                    "payer": "伊莉雅",
                },
                "evidence": "伊莉雅请两人在城里的旅馆住一晚",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(ilya.zenit, 60)
        self.assertEqual(ilya.hp, ilya.max_hp)
        self.assertEqual(loran.hp, loran.max_hp)
        self.assertIn("40Z", receipt.public_fallback_reply)

    def test_shop_cannot_prepay_a_travel_service_or_lodging(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.zenit = 500

        travel = self.service.gm_gameplay_tools.perform_scene_action(
            gameplay_context("伊莉雅先付三天陆地旅行服务的钱。"),
            {
                "action_type": "Shop",
                "actor": "伊莉雅",
                "details": {
                    "mode": "travel_service",
                    "transport": "陆地旅行服务",
                    "days": 3,
                    "party_size": 1,
                },
                "evidence": "伊莉雅先付三天陆地旅行服务的钱",
            },
        )
        lodging = self.service.gm_gameplay_tools.perform_scene_action(
            gameplay_context("伊莉雅只付旅馆费。"),
            {
                "action_type": "Shop",
                "actor": "伊莉雅",
                "details": {
                    "mode": "lodging",
                    "settlement_size": "town",
                    "party_size": 1,
                },
                "evidence": "伊莉雅只付旅馆费",
            },
        )

        self.assertFalse(travel.ok)
        self.assertEqual(
            travel.error_code,
            "TRAVEL_SERVICE_MUST_USE_TRAVEL_TOOL",
        )
        self.assertFalse(lodging.ok)
        self.assertEqual(lodging.error_code, "LODGING_MUST_USE_REST")
        self.assertEqual(ilya.zenit, 500)

    def test_sale_tool_rejects_model_invented_price_ratio(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.equipment.append("钢匕首")

        receipt = self.service.gm_gameplay_tools.perform_scene_action(
            gameplay_context("伊莉雅把钢匕首卖掉。"),
            {
                "action_type": "SellItem",
                "actor": "伊莉雅",
                "details": {
                    "item_name": "钢匕首",
                    "quantity": 1,
                    "price_ratio": 0.9,
                },
                "evidence": "伊莉雅把钢匕首卖掉",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "NONSTANDARD_SALE_PRICE_REQUIRES_DEDICATED_RULING",
        )
        self.assertIn("钢匕首", ilya.equipment)

    def test_objective_requires_an_existing_clock_instead_of_creating_one_implicitly(self) -> None:
        message = "伊莉雅试着松开闸门横梁。"
        receipt = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context(message),
            {
                "action_type": "Objective",
                "actor": "伊莉雅",
                "target": "松开闸门横梁",
                "attributes": ["洞察", "敏捷"],
                "difficulty": 10,
                "purpose": "松开卡死的闸门横梁",
                "check_label": "松开闸门横梁",
                "success_observation": "横梁松开了一截。",
                "failure_consequence": "横梁反而咬得更紧，短时间内无法继续硬撬。",
                "evidence": "伊莉雅试着松开闸门横梁",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "OBJECTIVE_CLOCK_NOT_FOUND")
        self.assertEqual(self.app.clock_manager.all(), [])

    def test_objective_can_only_advance_the_named_existing_clock(self) -> None:
        self.app.clock_manager.add(Clock(name="开启旧路闸门", max_segments=6))
        message = "伊莉雅继续处理开启旧路闸门的横梁。"
        receipt = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context(message),
            {
                "action_type": "Objective",
                "actor": "伊莉雅",
                "target": "旧路闸门横梁",
                "attributes": ["洞察", "敏捷"],
                "difficulty": 7,
                "purpose": "推进开启旧路闸门",
                "check_label": "调整闸门横梁",
                "success_observation": "横梁的锁舌被拨回一段。",
                "failure_consequence": "锁舌卡在锈蚀槽里，这次没有移动。",
                "details": {"clock_name": "开启旧路闸门"},
                "evidence": "伊莉雅继续处理开启旧路闸门的横梁",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertIn("调整闸门横梁", receipt.public_fallback_reply)
        self.assertNotIn("对 旧路闸门横梁 的检定", receipt.public_fallback_reply)
        self.assertGreaterEqual(self.app.clock_manager.get("开启旧路闸门").current, 1)

    def test_out_of_turn_action_is_cached_with_exact_actor_and_parameters(self) -> None:
        self.app.conflict_manager.start_scene("风铃廊冲突", ["伊莉雅", "洛岚"])
        message = "洛岚先举起符文盾防御，轮到我时就这么做。"

        receipt = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(message, speaker="白河"),
            {
                "action_type": "Guard",
                "actor": "洛岚",
                "details": {},
                "evidence": "洛岚先举起符文盾防御",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(self.app.conflict_manager.state.current_actor(), "伊莉雅")
        self.assertFalse(self.app.character_manager.get("伊莉雅").guarding)
        self.assertFalse(self.app.character_manager.get("洛岚").guarding)
        held = self.app.conflict_manager.held_actions_for_actor("洛岚")
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["action_type"], "Guard")
        self.assertEqual(held[0]["action_parameters"]["actor"], "洛岚")
        self.assertIn("你的行动我先缓存", receipt.public_fallback_reply)

    def test_valid_out_of_turn_assist_does_not_end_current_actor_turn(self) -> None:
        self.app.conflict_manager.start_scene("风铃廊冲突", ["伊莉雅", "洛岚"])
        action = Action(
            ActionType.GUARD,
            {
                "actor": "洛岚",
                "assist_target": "伊莉雅",
                "reasoning": "洛岚协助伊莉雅稳住盾阵",
                "_enforce_turn_order": True,
            },
        )

        resolution = self.app.interceptor.resolve(action)
        self.app._auto_advance_conflict_turn(action, resolution)

        self.assertTrue(resolution.payload["team_assist_registered"])
        self.assertTrue(resolution.payload["out_of_turn"])
        self.assertEqual(self.app.conflict_manager.state.current_actor(), "伊莉雅")
        self.assertIn("洛岚", self.app.conflict_manager.state.acted_this_round)

    def test_on_turn_action_consumes_cached_draft_so_it_cannot_repeat_next_round(self) -> None:
        self.app.conflict_manager.start_scene("风铃廊冲突", ["伊莉雅", "洛岚"])
        cached_message = "洛岚先举起符文盾防御，轮到我时就这么做。"
        cached = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(cached_message, speaker="白河"),
            {
                "action_type": "Guard",
                "actor": "洛岚",
                "details": {},
                "evidence": "洛岚先举起符文盾防御",
            },
        )
        self.assertTrue(cached.ok, cached.message)

        current_message = "伊莉雅先收盾调整姿态。"
        current = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(current_message),
            {
                "action_type": "Guard",
                "actor": "伊莉雅",
                "details": {},
                "evidence": "伊莉雅先收盾调整姿态",
            },
        )
        self.assertTrue(current.ok, current.message)
        self.assertEqual(self.app.conflict_manager.state.current_actor(), "洛岚")

        held_message = "洛岚按刚才的打算举盾防御。"
        acted = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(held_message, speaker="白河"),
            {
                "action_type": "Guard",
                "actor": "洛岚",
                "details": {},
                "evidence": "洛岚按刚才的打算举盾防御",
            },
        )

        self.assertTrue(acted.ok, acted.message)
        self.assertEqual(self.app.conflict_manager.held_actions_for_actor("洛岚"), [])
        self.assertEqual(
            self.app.interceptor.decision_window_manager.pending(
                kind="held_action",
                owner="洛岚",
            ),
            [],
        )

    def test_player_can_discard_cached_action_without_losing_their_turn(self) -> None:
        self.app.conflict_manager.start_scene("风铃廊冲突", ["伊莉雅", "洛岚"])
        cached = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(
                "洛岚先举盾，轮到我时就这么做。",
                speaker="白河",
            ),
            {
                "action_type": "Guard",
                "actor": "洛岚",
                "details": {},
                "evidence": "洛岚先举盾",
            },
        )
        self.assertTrue(cached.ok, cached.message)
        window = self.app.interceptor.decision_window_manager.find_pending(
            kind="held_action",
            owner="洛岚",
        )
        self.assertIsNotNone(window)

        discarded = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("洛岚取消刚才缓存的动作。", speaker="白河"),
            {
                "action_type": "ResolveDecision",
                "actor": "洛岚",
                "window_id": window.window_id,
                "choice": "discard",
                "details": {},
                "evidence": "洛岚取消刚才缓存的动作",
            },
        )

        self.assertTrue(discarded.ok, discarded.message)
        self.assertEqual(self.app.conflict_manager.state.current_actor(), "伊莉雅")
        self.assertEqual(self.app.conflict_manager.held_actions_for_actor("洛岚"), [])
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window.window_id,
            )
        )

    def test_cached_action_confirm_cannot_close_window_without_execution(self) -> None:
        self.app.conflict_manager.start_scene("风铃廊冲突", ["伊莉雅", "洛岚"])
        cached = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(
                "洛岚先举盾，轮到我时就这么做。",
                speaker="白河",
            ),
            {
                "action_type": "Guard",
                "actor": "洛岚",
                "details": {},
                "evidence": "洛岚先举盾",
            },
        )
        self.assertTrue(cached.ok, cached.message)
        window = self.app.interceptor.decision_window_manager.find_pending(
            kind="held_action",
            owner="洛岚",
        )
        self.assertIsNotNone(window)

        rejected = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("洛岚照刚才的行动执行。", speaker="白河"),
            {
                "action_type": "ResolveDecision",
                "actor": "洛岚",
                "window_id": window.window_id,
                "choice": "confirm",
                "details": {},
                "evidence": "洛岚照刚才的行动执行",
            },
        )

        self.assertFalse(rejected.ok)
        self.assertEqual(rejected.error_code, "HELD_ACTION_MUST_EXECUTE")
        self.assertEqual(len(self.app.conflict_manager.held_actions_for_actor("洛岚")), 1)
        self.assertIsNotNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window.window_id,
            )
        )

    def test_ritual_start_check_creates_and_progresses_clock_before_turn_advances(self) -> None:
        self.app.character_manager.get("伊莉雅").skills["元素系仪式"] = 1
        self.app.conflict_manager.start_scene("风铃廊冲突", ["伊莉雅", "洛岚"])
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["INS", "WLP"],
                dice=[(10, 5), (6, 3)],
                total=8,
                modifier=0,
                high_roll=5,
                target_number=7,
                success=True,
                critical_success=False,
                fumble=False,
                margin=1,
                reason="启动仪式【风铃回声】",
            )
        )
        message = "伊莉雅启动元素仪式风铃回声，借廊下的风寻找旧路。"

        receipt = self.service.gm_gameplay_tools.perform_ritual_project_action(
            gameplay_context(message),
            {
                "action_type": "PlanRitual",
                "actor": "伊莉雅",
                "details": {
                    "name": "风铃回声",
                    "discipline": "elementalism",
                    "potency": "minor",
                    "scope": "individual",
                    "effect": "让风声指出旧路机关的位置",
                    "attributes": ["INS", "WLP"],
                    "track_clock": True,
                },
                "evidence": "启动元素仪式风铃回声",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertTrue(self.app.clock_manager.exists("仪式：风铃回声"))
        self.assertEqual(self.app.clock_manager.get("仪式：风铃回声").current, 1)
        self.assertEqual(self.app.conflict_manager.state.current_actor(), "洛岚")
        self.assertEqual(self.app.conflict_manager.held_actions_for_actor("伊莉雅"), [])
        self.assertNotIn("意图已暂存", receipt.public_fallback_reply)

    def test_check_tool_rejects_observation_driven_threat_progress_fields(self) -> None:
        message = "伊莉雅观察远处的巡逻火光。"
        receipt = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context(message),
            {
                "action_type": "Investigate",
                "actor": "伊莉雅",
                "target": "远处的巡逻火光",
                "attributes": ["洞察", "洞察"],
                "difficulty": 7,
                "purpose": "判断巡逻队位置",
                "details": {
                    "threat_clock_delta": 2,
                    "advance_threat_on_failure": True,
                },
                "evidence": "伊莉雅观察远处的巡逻火光",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "PROTECTED_ACTION_PARAMETER")

    def test_reveal_opportunity_asks_for_creature_before_committing_choice(self) -> None:
        window = self.app.interceptor.decision_window_manager.create(
            kind="critical_opportunity",
            owner="伊莉雅",
            prompt="选择大成功机会。",
            options=[
                {"effect": "揭示", "requires": ["target"]},
                {"effect": "优势"},
            ],
            blocking=True,
            action_type="TriggerOpportunity",
        )
        message = "伊莉雅把这次大成功的机会用于揭示。"

        receipt = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context(message),
            {
                "action_type": "TriggerOpportunity",
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "揭示",
                "details": {},
                "evidence": "机会用于揭示",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "OPPORTUNITY_TARGET_REQUIRED")
        self.assertEqual(receipt.public_fallback_reply, "你想对哪一个生物使用【揭示】？")
        self.assertIsNotNone(
            self.app.interceptor.decision_window_manager.find_pending(window_id=window.window_id)
        )

    def test_spell_parameter_tool_preserves_direct_target_details(self) -> None:
        caster = self.app.character_manager.get("伊莉雅")
        caster.spells = ["屏障"]
        pending = self.app.interceptor.resolve(
            Action(
                ActionType.SPELL,
                {
                    "actor": "伊莉雅",
                    "spell_name": "屏障",
                    "target": "不存在的桌外玩家名",
                },
            )
        )
        window_id = str(pending.payload["decision_window_id"])

        receipt = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("目标选伊莉雅。"),
            {
                "action_type": "ResolveDecision",
                "actor": "伊莉雅",
                "window_id": window_id,
                "choice": "cast_spell",
                "details": {"targets": ["伊莉雅"]},
                "evidence": "目标选伊莉雅",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(window_id=window_id)
        )
        self.assertEqual(caster.mp, 30)

    def test_reveal_opportunity_commits_only_after_explicit_existing_target(self) -> None:
        self.app.character_manager.add(
            Character(
                name="守望会会长",
                attributes={"DEX": 8, "INS": 8, "MIG": 6, "WLP": 10},
                max_hp=40,
                hp=40,
                max_mp=50,
                mp=50,
                traits=["npc"],
            )
        )
        self.app.world_state.ensure_npc_persona(
            "守望会会长",
            active_goal="保护旧路，不让财团找到失忆旅人",
        )
        window = self.app.interceptor.decision_window_manager.create(
            kind="critical_opportunity",
            owner="伊莉雅",
            prompt="选择大成功机会。",
            options=[{"effect": "揭示", "requires": ["target"]}],
            blocking=True,
            action_type="TriggerOpportunity",
        )
        message = "伊莉雅要揭示守望会会长真正想做什么。"

        receipt = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context(message),
            {
                "action_type": "TriggerOpportunity",
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "揭示",
                "details": {"target": "守望会会长"},
                "evidence": "揭示守望会会长",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertIn("保护旧路", receipt.public_fallback_reply)
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(window_id=window.window_id)
        )

    def test_acceleration_window_can_decline_through_typed_rule_tool(self) -> None:
        self.app.conflict_manager.start_scene("断桥激战", ["伊莉雅", "监察官"])
        self.app.conflict_manager.register_effect(
            TimedEffect(
                owner="伊莉雅",
                effect_type="acceleration",
                expires_on=EffectTiming.SCENE_END,
                target="伊莉雅",
                source="加速术",
                effect_key="spell:加速术:伊莉雅",
                data={"benefits_used": 0, "max_benefits": 2, "max_spell_mp": 10},
            )
        )
        self.assertEqual(self.app.conflict_manager.next_turn(), "伊莉雅")
        window = self.app.interceptor.decision_window_manager.find_pending(
            kind="acceleration_benefit",
            owner="伊莉雅",
        )
        self.assertIsNotNone(window)
        message = "伊莉雅这回不发动加速术。"

        receipt = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context(message),
            {
                "action_type": "ResolveDecision",
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "decline",
                "details": {},
                "evidence": "不发动加速术",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertIn("不发动【加速术】", receipt.public_fallback_reply)
        self.assertIsNone(self.app.conflict_manager.state.pending_turn_end_actor)

    def test_acceleration_window_attack_uses_typed_action_and_advances_once(self) -> None:
        self.app.character_manager.get("伊莉雅").weapon_accuracy_attributes = ("DEX", "MIG")
        self.app.character_manager.get("伊莉雅").weapon_damage = 5
        self.app.character_manager.add(
            Character(
                name="监察官",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                max_hp=40,
                hp=40,
                max_mp=40,
                mp=40,
                traits=["npc"],
            )
        )
        self.app.conflict_manager.start_scene("断桥激战", ["伊莉雅", "监察官"])
        self.app.conflict_manager.register_effect(
            TimedEffect(
                owner="伊莉雅",
                effect_type="acceleration",
                expires_on=EffectTiming.SCENE_END,
                target="伊莉雅",
                source="加速术",
                effect_key="spell:加速术:伊莉雅",
                data={"benefits_used": 0, "max_benefits": 2, "max_spell_mp": 10},
            )
        )
        self.assertEqual(self.app.conflict_manager.next_turn(), "伊莉雅")
        window = self.app.interceptor.decision_window_manager.find_pending(
            kind="acceleration_benefit",
            owner="伊莉雅",
        )
        self.assertIsNotNone(window)
        message = "伊莉雅借加速术攻击监察官。"
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["DEX", "MIG"],
                dice=[(8, 6), (8, 4)],
                total=10,
                modifier=0,
                high_roll=6,
                target_number=8,
                success=True,
                critical_success=False,
                fumble=False,
                margin=2,
                reason="加速术顺势攻击",
            )
        )

        receipt = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context(message),
            {
                "action_type": "Attack",
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "attack",
                "details": {"target": "监察官"},
                "evidence": "攻击监察官",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertIn("【加速术】触发", receipt.public_fallback_reply)
        self.assertEqual(self.app.conflict_manager.state.current_actor(), "监察官")

    def test_guard_skill_followup_waits_for_real_attack_then_resumes_once(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.skills = {"死战不退": 1, "鹰眼": 1}
        hero.equipment = ["短弓"]
        hero.equipped_main_hand = "短弓"
        hero.weapon_range = "ranged"
        hero.weapon_accuracy_attributes = ["DEX", "DEX"]
        hero.weapon_damage = 8
        hero.fabula_points = 0
        self.app.character_manager.add(
            Character(
                name="监察官",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                max_hp=50,
                hp=50,
                max_mp=40,
                mp=40,
                defenses={"physical": 8, "magic": 8},
                traits=["enemy"],
            )
        )
        self.app.conflict_manager.start_scene(
            "断桥激战",
            ["伊莉雅", "监察官"],
        )

        guarded = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context("伊莉雅先防御，随后寻找射击空隙。"),
            {
                "action_type": "Guard",
                "actor": "伊莉雅",
                "details": {},
                "evidence": "伊莉雅先防御",
            },
        )
        self.assertTrue(guarded.ok, guarded.message)
        self.assertEqual(
            self.app.conflict_manager.state.current_actor(),
            "伊莉雅",
        )
        stand = self.app.interceptor.decision_window_manager.find_pending(
            kind="skill_parameter",
            owner="伊莉雅",
        )
        self.assertEqual(stand.payload["skill"], "死战不退")
        resolved_stand = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("伊莉雅借意志撑住。"),
            {
                "action_type": "ResolveDecision",
                "actor": "伊莉雅",
                "window_id": stand.window_id,
                "choice": "attribute",
                "details": {
                    "selected_option": {
                        "choice": "attribute",
                        "attribute": "WLP",
                    }
                },
                "evidence": "借意志撑住",
            },
        )
        self.assertTrue(resolved_stand.ok, resolved_stand.message)
        self.assertEqual(
            self.app.conflict_manager.state.current_actor(),
            "伊莉雅",
        )
        hawkeye = self.app.interceptor.decision_window_manager.find_pending(
            kind="skill_parameter",
            owner="伊莉雅",
        )
        self.assertEqual(hawkeye.payload["skill"], "鹰眼")
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["DEX", "DEX"],
                dice=[(8, 6), (8, 4)],
                total=10,
                modifier=0,
                high_roll=6,
                target_number=8,
                success=True,
                critical_success=False,
                fumble=False,
                margin=2,
                target="监察官",
                reason="鹰眼顺势攻击",
            )
        )
        shot = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("伊莉雅发动鹰眼，立刻射击监察官。"),
            {
                "action_type": "Attack",
                "actor": "伊莉雅",
                "window_id": hawkeye.window_id,
                "choice": "immediate_ranged_attack",
                "details": {"target": "监察官"},
                "evidence": "发动鹰眼，立刻射击监察官",
            },
        )

        self.assertTrue(shot.ok, shot.message)
        self.assertEqual(
            self.app.character_manager.get("监察官").hp,
            42,
        )
        self.assertEqual(
            self.app.conflict_manager.state.current_actor(),
            "监察官",
        )
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=hawkeye.window_id
            )
        )

    def test_quick_step_rejects_incomplete_action_without_spending_or_closing(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.skills = {"疾速身法": 2}
        hero.mp = 35
        hero.fabula_points = 0
        self.app.character_manager.add(
            Character(
                name="监察官",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                max_hp=50,
                hp=50,
                max_mp=40,
                mp=40,
                defenses={"physical": 8, "magic": 8},
                traits=["enemy"],
            )
        )
        self.app.conflict_manager.start_scene(
            "断桥激战",
            ["监察官", "伊莉雅"],
        )
        outcome = self.app.interceptor.skill_lifecycle.trigger(
            "conflict_start",
            hero,
            visible_targets=["监察官"],
        )
        self.app.interceptor._capture_skill_lifecycle(outcome)
        window = self.app.interceptor.decision_window_manager.find_pending(
            kind="skill_parameter",
            owner="伊莉雅",
        )

        rejected = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("伊莉雅用疾速身法抢先攻击。"),
            {
                "action_type": "Attack",
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "attack",
                "details": {},
                "evidence": "用疾速身法抢先攻击",
            },
        )

        self.assertFalse(rejected.ok)
        self.assertEqual(
            rejected.error_code,
            "QUICK_STEP_ATTACK_TARGET_REQUIRED",
        )
        self.assertEqual(hero.mp, 35)
        self.assertIsNotNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window.window_id
            )
        )

        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["DEX", "MIG"],
                dice=[(8, 5), (8, 3)],
                total=10,
                modifier=2,
                high_roll=5,
                target_number=8,
                success=True,
                critical_success=False,
                fumble=False,
                margin=2,
                target="监察官",
                reason="疾速身法顺势攻击",
            )
        )
        committed = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("伊莉雅用疾速身法抢先攻击监察官。"),
            {
                "action_type": "Attack",
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "attack",
                "details": {"target": "监察官"},
                "evidence": "用疾速身法抢先攻击监察官",
            },
        )

        self.assertTrue(committed.ok, committed.message)
        self.assertEqual(hero.mp, 25)
        self.assertEqual(
            self.app.conflict_manager.state.current_actor(),
            "监察官",
        )
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window.window_id
            )
        )

    def test_arcanum_echo_enforces_total_spell_cost_and_keeps_window_on_failure(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.skills = {"奥灵回响": 1}
        hero.spells = ["炎弹", "精神汲取"]
        hero.equipment = ["法杖"]
        hero.equipped_main_hand = "法杖"
        hero.mp = 35
        hero.fabula_points = 0
        self.app.character_manager.add(
            Character(
                name="监察官",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                max_hp=50,
                hp=50,
                max_mp=40,
                mp=40,
                defenses={"physical": 8, "magic": 8},
                traits=["enemy"],
            )
        )
        self.app.conflict_manager.start_scene(
            "断桥激战",
            ["伊莉雅", "监察官"],
        )
        outcome = self.app.interceptor.skill_lifecycle.trigger(
            "arcanum_dismissed",
            hero,
            active_dismissal=True,
            summoned_this_turn=False,
            magic_weapon_equipped=True,
        )
        self.app.interceptor._capture_skill_lifecycle(outcome)
        window = self.app.interceptor.decision_window_manager.find_pending(
            kind="skill_parameter",
            owner="伊莉雅",
        )

        rejected = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("伊莉雅借奥灵回响施放炎弹。"),
            {
                "action_type": "Spell",
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "cast_spell",
                "details": {
                    "spell_name": "炎弹",
                    "target": "洛岚",
                },
                "evidence": "借奥灵回响施放炎弹",
            },
        )
        self.assertTrue(rejected.ok, rejected.message)
        self.assertIn("不高于 5 点", rejected.public_fallback_reply)
        self.assertEqual(hero.mp, 35)
        self.assertIsNotNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window.window_id
            )
        )

        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["INS", "WLP"],
                dice=[(10, 5), (6, 3)],
                total=8,
                modifier=0,
                high_roll=5,
                target_number=8,
                success=True,
                critical_success=False,
                fumble=False,
                margin=0,
                target="监察官",
                reason="奥灵回响施法",
            )
        )
        accepted = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("伊莉雅改用精神汲取。"),
            {
                "action_type": "Spell",
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "cast_spell",
                "details": {
                    "spell_name": "精神汲取",
                    "target": "洛岚",
                },
                "evidence": "改用精神汲取",
            },
        )

        self.assertTrue(accepted.ok, accepted.message)
        self.assertEqual(hero.mp, 30)
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window.window_id
            )
        )
        self.assertEqual(
            self.app.conflict_manager.state.current_actor(),
            "伊莉雅",
        )

    def test_emergency_supplies_commits_inventory_action_without_spending_turn(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.skills = {"应急用品": 1}
        hero.hp = 10
        hero.crisis_threshold = 22
        hero.inventory_points = 6
        self.app.character_manager.add(
            Character(
                name="监察官",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                max_hp=50,
                hp=50,
                max_mp=40,
                mp=40,
                defenses={"physical": 8, "magic": 8},
                traits=["enemy"],
            )
        )
        self.app.conflict_manager.start_scene(
            "断桥激战",
            ["伊莉雅", "监察官"],
        )
        window = self.app.interceptor.decision_window_manager.find_pending(
            kind="skill_parameter",
            owner="伊莉雅",
        )
        self.assertIsNotNone(window)
        self.assertEqual(window.payload["skill"], "应急用品")

        used = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("伊莉雅发动应急用品，使用治疗剂。"),
            {
                "action_type": "UseInventory",
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "use_inventory_action",
                "details": {
                    "item_name": "治疗剂",
                    "target": "伊莉雅",
                },
                "evidence": "发动应急用品，使用治疗剂",
            },
        )

        self.assertTrue(used.ok, used.message)
        self.assertEqual(hero.hp, 45)
        self.assertEqual(hero.inventory_points, 3)
        self.assertIn("scene:skill:应急用品", hero.trigger_cooldowns)
        self.assertEqual(
            self.app.conflict_manager.state.current_actor(),
            "伊莉雅",
        )
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window.window_id
            )
        )

    def test_quick_assessment_reveals_exact_choices_without_consuming_turn(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.skills = {"快速评估": 2}
        hero.mp = 35
        self.app.character_manager.add(
            Character(
                name="监察官",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                max_hp=50,
                hp=50,
                max_mp=40,
                mp=40,
                defenses={"physical": 8, "magic": 8},
                affinities={"fire": Affinity.RESIST},
                traits=["enemy", "humanoid", "冷酷", "警觉"],
            )
        )
        self.app.conflict_manager.start_scene(
            "断桥激战",
            ["监察官", "伊莉雅"],
        )
        outcome = self.app.interceptor.skill_lifecycle.trigger(
            "conflict_start",
            hero,
            visible_targets=["监察官"],
        )
        self.app.interceptor._capture_skill_lifecycle(outcome)
        window = self.app.interceptor.decision_window_manager.find_pending(
            kind="skill_parameter",
            owner="伊莉雅",
        )

        assessed = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("伊莉雅评估监察官的冷酷特质和火焰相性。"),
            {
                "action_type": "Skill",
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "declare_assessment",
                "details": {
                    "assessments": [
                        {
                            "target": "监察官",
                            "kind": "trait",
                            "trait": "冷酷",
                        },
                        {
                            "target": "监察官",
                            "kind": "affinity",
                            "damage_type": "火",
                        },
                    ]
                },
                "evidence": "评估监察官的冷酷特质和火焰相性",
            },
        )

        self.assertTrue(assessed.ok, assessed.message)
        self.assertIn("冷酷", assessed.public_fallback_reply)
        self.assertIn("火系相性：抵抗", assessed.public_fallback_reply)
        self.assertEqual(hero.mp, 25)
        self.assertEqual(
            self.app.conflict_manager.state.current_actor(),
            "监察官",
        )
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window.window_id
            )
        )

    def test_unused_emergency_supplies_window_expires_at_turn_end(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.skills = {"应急用品": 1}
        hero.hp = 10
        hero.crisis_threshold = 22
        self.app.character_manager.add(
            Character(
                name="监察官",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                max_hp=50,
                hp=50,
                max_mp=40,
                mp=40,
                defenses={"physical": 8, "magic": 8},
                traits=["enemy"],
            )
        )
        self.app.conflict_manager.start_scene(
            "断桥激战",
            ["伊莉雅", "监察官"],
        )
        window = self.app.interceptor.decision_window_manager.find_pending(
            kind="skill_parameter",
            owner="伊莉雅",
        )
        self.assertIsNotNone(window)

        guarded = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context("伊莉雅这回先防御。"),
            {
                "action_type": "Guard",
                "actor": "伊莉雅",
                "details": {},
                "evidence": "这回先防御",
            },
        )

        self.assertTrue(guarded.ok, guarded.message)
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window.window_id
            )
        )
        self.assertEqual(
            self.app.conflict_manager.state.current_actor(),
            "监察官",
        )

    def test_player_check_fumble_requires_same_transaction_gm_followup(self) -> None:
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["INS", "INS"],
                dice=[(10, 1), (10, 1)],
                total=2,
                modifier=0,
                high_roll=1,
                target_number=10,
                success=False,
                critical_success=False,
                fumble=True,
                opportunity_count=1,
                margin=-8,
                target="潮湿石阶",
                reason="辨认潮痕",
            )
        )

        receipt = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context("伊莉雅俯身辨认潮湿石阶上的旧痕。"),
            {
                "action_type": "RequestRoll",
                "actor": "伊莉雅",
                "target": "潮湿石阶",
                "attributes": ["洞察", "洞察"],
                "difficulty": 10,
                "purpose": "辨认潮痕",
                "check_label": "辨认潮痕",
                "success_observation": "她分辨出潮水退去的方向。",
                "failure_consequence": "新旧潮痕混在一起，暂时无法判断。",
                "details": {},
                "evidence": "辨认潮湿石阶上的旧痕",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(
            receipt.result["required_followup_tools"],
            ["resolve_gm_opportunity"],
        )
        self.assertEqual(receipt.result["required_followup_mode"], "all")
        fumble = next(
            item
            for item in receipt.result["pending_decisions"]
            if item["kind"] == "fumble_opportunity"
            and item["owner"] == "__gm__"
        )
        self.assertEqual(
            receipt.result["required_followup_calls"][0]["arguments"],
            {"window_id": fumble["window_id"]},
        )

    def test_gm_fumble_opportunity_is_resolved_by_dedicated_tool(self) -> None:
        window = self.app.interceptor.decision_window_manager.create(
            kind="fumble_opportunity",
            owner="__gm__",
            prompt="GM选择一个大失败机会。",
            options=[
                {"effect": "受苦"},
                {"effect": "进展"},
                {"effect": "失物"},
                {"effect": "转折"},
            ],
            blocking=True,
            allowed_responders=["__gm__"],
            action_type="TriggerOpportunity",
            payload={"source_actor": "伊莉雅", "source_action_type": "RequestRoll"},
        )

        receipt = self.service.gm_gameplay_tools.resolve_gm_opportunity(
            gameplay_context("伊莉雅在湿滑石阶上失足。"),
            {
                "window_id": window.window_id,
                "choice": "受苦",
                "details": {"target": "伊莉雅", "status_effect": "shaken"},
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertIn(StatusEffect.SHAKEN, self.app.character_manager.get("伊莉雅").statuses)
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(window_id=window.window_id)
        )


if __name__ == "__main__":
    unittest.main()
