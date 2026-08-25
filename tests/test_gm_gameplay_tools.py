import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fu_gm.check_difficulty import OPEN_CHECK_DIFFICULTY_GUIDANCE
from fu_gm.conversation import MessageEvent
from fu_gm.gm_tool_agent import GMToolExecutionContext
from fu_gm.gm_tool_contracts import GMToolReceipt
from fu_gm.components.gm_agent_message_coordinator import GMToolStateSnapshotBuilder
from fu_gm.expressor import Expressor
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
    RestType,
    SessionDramaticContract,
    SessionSceneOpportunity,
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

    def test_legacy_deferred_metadata_does_not_authorize_new_action(self) -> None:
        context = gameplay_context(
            "伊莉雅刚处理完自己的待决窗口。",
            speaker="阿凛",
        )
        context.metadata["_gm_deferred_player_followup_authorization"] = {
            "source_speaker": "白河",
            "source_message": "洛岚敲击机兵腿部联轴，试图令它迟缓。",
            "source_event_id": "event-loran",
            "actors": ["洛岚"],
            "clause_ids": ["deferred_c1"],
        }

        receipt = self.service.gm_gameplay_tools.declare_check_action(
            context,
            {
                "action_type": "Hinder",
                "actor": "洛岚",
                "target": "机兵腿部联轴",
                "attributes": ["力量", "洞察"],
                "difficulty": 10,
                "purpose": "令机兵迟缓",
                "check_label": "敲击腿部联轴",
                "base_observation": "联轴正从装甲接缝间转过。",
                "success_observation": "铁锤卡住联轴，机兵的步伐慢了下来。",
                "risk_hint": "装甲接缝正在闭合。",
                "failure_consequence": "铁锤被弹开，洛岚暴露在机兵面前。",
                "evidence": "洛岚敲击机兵腿部联轴",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "EVIDENCE_NOT_LITERAL")

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
        self.assertEqual(state["current_scene"]["name"], "白花碑驿站")
        self.assertEqual(state["current_scene"]["location"], "风铃廊")
        self.assertEqual(
            state["current_scene"]["participants"],
            ["伊莉雅", "洛岚"],
        )

    def test_minor_action_tool_normalizes_item_target_and_requires_final_state(self) -> None:
        self.app.world_state.commit_story_item_action(
            operation="place",
            item_name="炉心安全栓",
            actor="GM",
            scene_location="风铃廊",
            public_fact="炉心安全栓位于风铃廊的控制台。",
            source="test_fixture",
            to_location="风铃廊",
            state_note="辅助燃料仍连接",
        )
        self.app.conflict_manager.start_scene("风铃廊冲突", ["伊莉雅", "洛岚"])
        message = "伊莉雅用次要行动把【炉心安全栓】扭到【断开辅助燃料】。"

        missing_state = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(message),
            {
                "action_type": "MinorAction",
                "actor": "伊莉雅",
                "details": {"mode": "interact", "target": "炉心安全栓"},
                "evidence": message,
            },
        )
        self.assertFalse(missing_state.ok)
        self.assertEqual(missing_state.error_code, "MINOR_ACTION_STATE_REQUIRED")

        receipt = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(message),
            {
                "action_type": "MinorAction",
                "actor": "伊莉雅",
                "details": {
                    "mode": "interact",
                    "target": "炉心安全栓",
                    "state_note": "断开辅助燃料",
                },
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        item = self.app.world_state.find_story_item(name="炉心安全栓")
        self.assertEqual(item.current_state, "断开辅助燃料")
        self.assertEqual(self.app.conflict_manager.state.current_actor(), "伊莉雅")

    def test_absent_character_fades_from_current_conflict_without_offscreen_result(self) -> None:
        self.app.conflict_manager.start_scene("风铃廊冲突", ["伊莉雅", "洛岚"])
        self.app.world_state.mark_player_absent("阿凛", "临时离席")
        message = "我先离席半小时，伊莉雅暂时淡出场景，回来后再处理她去找守望会的结果。"

        receipt = self.service.gm_gameplay_tools.set_absent_character_mode(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "mode": "return_later",
                "task_note": "去找守望会；结果尚未决定",
                "evidence": "伊莉雅暂时淡出场景",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertNotIn("伊莉雅", self.app.conflict_manager.state.turn_order)
        self.assertIn("伊莉雅", self.app.conflict_manager.state.escaped_combatants)
        self.assertEqual(self.app.conflict_manager.state.current_actor(), "洛岚")
        self.assertNotIn("伊莉雅", self.app.scene_manager.current_scene.participants)
        facts = self.app.world_state.subject_facts.get("伊莉雅", [])
        self.assertTrue(any("结果尚未结算" in fact for fact in facts))
        self.assertFalse(any("成功" in fact for fact in facts))

    def test_absent_character_announced_out_of_turn_does_not_consume_current_actor_turn(self) -> None:
        self.app.conflict_manager.start_scene("风铃廊冲突", ["伊莉雅", "洛岚"])
        self.app.world_state.mark_player_absent("白河", "临时离席")
        message = "我得先走，洛岚从冲突里淡出，之后再回来。"

        receipt = self.service.gm_gameplay_tools.set_absent_character_mode(
            gameplay_context(message, speaker="白河"),
            {
                "actor": "洛岚",
                "mode": "fade_out",
                "evidence": "洛岚从冲突里淡出",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertNotIn("洛岚", self.app.conflict_manager.state.turn_order)
        self.assertEqual(self.app.conflict_manager.state.current_actor(), "伊莉雅")
        self.assertEqual(self.app.conflict_manager.state.round_number, 1)

    def test_absent_character_requires_explicit_attendance_change_first(self) -> None:
        message = "伊莉雅暂时淡出场景。"

        receipt = self.service.gm_gameplay_tools.set_absent_character_mode(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "mode": "fade_out",
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "PLAYER_STILL_PRESENT")
        self.assertIn("伊莉雅", self.app.scene_manager.current_scene.participants)

    def test_rest_rejects_active_conflict_without_replacing_scene(self) -> None:
        scene = self.app.scene_manager.current_scene
        self.app.conflict_manager.start_scene("风铃廊冲突", ["伊莉雅", "洛岚"])

        with self.assertRaisesRegex(ValueError, "冲突仍在进行"):
            self.app.take_rest(
                RestType.SETTLEMENT,
                safe_source="白花碑旅店",
            )

        self.assertIs(self.app.scene_manager.current_scene, scene)
        self.assertTrue(self.app.conflict_manager.state.active)

    def test_invalid_rest_clock_is_validated_before_scene_transition(self) -> None:
        scene = self.app.scene_manager.current_scene
        clock = Clock(
            name="风铃廊警戒",
            max_segments=6,
            clock_type="threat",
            scope="scene",
            advance_on_rest=True,
        )
        self.app.clock_manager.add(clock)

        with self.assertRaisesRegex(ValueError, "跨场景压力"):
            self.app.take_rest(
                RestType.SETTLEMENT,
                safe_source="白花碑旅店",
                threat_clocks=[clock.name],
            )

        self.assertIs(self.app.scene_manager.current_scene, scene)
        self.assertTrue(self.app.clock_manager.exists(clock.name))

    def test_state_summary_keeps_remote_branch_location_and_fine_position(self) -> None:
        source = self.app.scene_manager.current_scene
        source.participants.remove("洛岚")
        source.participant_locations.pop("洛岚", None)
        self.app.scene_manager.actor_locations["洛岚"] = "旧路闸门"
        destination, mode = self.app.scene_manager.focus_actor_branch(
            "洛岚",
            name="旧路闸门",
            location="旧路闸门",
        )
        self.assertEqual(mode, "created")
        self.app.scene_manager.set_participant_position("洛岚", "闸门内侧的锁栓旁")
        restored, mode = self.app.scene_manager.focus_actor_branch(
            "伊莉雅",
            name="白花碑驿站",
            location="风铃廊",
        )
        self.assertEqual(mode, "restored")
        self.assertIs(restored, source)

        state = self.service.gm_gameplay_tools.state_summary(
            gameplay_context("洛岚把硬楔片顶进锁栓下方。", speaker="白河")
        )

        self.assertNotIn("洛岚", state["current_scene"]["participants"])
        self.assertEqual(state["character_locations"]["洛岚"], "旧路闸门")
        self.assertEqual(
            state["character_positions"]["洛岚"],
            "闸门内侧的锁栓旁",
        )
        branch = next(
            item
            for item in state["active_scene_branches"]
            if item["scene_id"] == destination.scene_id
        )
        self.assertFalse(branch["camera_focused"])
        self.assertEqual(
            branch["participant_positions"]["洛岚"],
            "闸门内侧的锁栓旁",
        )

    def test_gm_selected_difficulty_parameters_expose_the_shared_rubric(self) -> None:
        schemas = {
            item["name"]: item
            for item in self.service.gm_tool_registry.schemas()
        }

        for tool_name, parameter_name in (
            ("declare_check_action", "difficulty"),
            ("declare_movement_check", "difficulty"),
            ("perform_check_action", "difficulty"),
            ("run_current_npc_turn", "target_number"),
        ):
            description = schemas[tool_name]["parameters"]["properties"][
                parameter_name
            ]["description"]
            self.assertIn(OPEN_CHECK_DIFFICULTY_GUIDANCE, description)
            self.assertIn("难度等级7为简单", description)
            self.assertIn("难度等级10为正常", description)
            self.assertIn("难度等级13为困难", description)
            self.assertIn("难度等级16为非常困难", description)

    def test_opportunity_tools_expose_the_complete_parameter_contract(self) -> None:
        schemas = {
            item["name"]: item
            for item in self.service.gm_tool_registry.schemas()
        }

        for tool_name in ("resolve_rule_window", "resolve_gm_opportunity"):
            description = schemas[tool_name]["parameters"]["properties"]["details"][
                "description"
            ]
            for fragment in (
                "揭示=target",
                "进展=clock_name",
                "纽带=target及emotion/emotions",
                "情报=information",
                "青睐=target",
                "审视=target",
                "失态=target及statement",
                "scene_object及description",
                "受苦=target及status_effect",
                "优势=target",
                "转折=subject",
                "自定义=description",
            ):
                self.assertIn(fragment, description)

    def test_equipment_access_preserves_ownership_but_blocks_equipping_and_persists(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.equipment.extend(["钢匕首", "丝质衬衫"])
        self.app.interceptor.economy_manager.configure_loadout(
            "伊莉雅",
            {"main_hand": "钢匕首", "armor": "丝质衬衫"},
            allow_armor=True,
        )

        restricted = self.service.gm_gameplay_tools.set_equipment_access(
            gameplay_context("卫兵把伊莉雅的钢匕首锁进证物柜。"),
            {
                "actor": "伊莉雅",
                "mode": "restrict",
                "items": ["钢匕首"],
                "reason": "被卫兵收缴",
                "location": "卡里巴村监狱证物柜",
                "evidence": "把伊莉雅的钢匕首锁进证物柜",
            },
        )

        self.assertTrue(restricted.ok, restricted.message)
        self.assertIn("钢匕首", ilya.equipment)
        self.assertIn("钢匕首", ilya.unavailable_equipment)
        self.assertEqual(ilya.equipped_main_hand, "徒手攻击")
        state = self.service.gm_gameplay_tools.state_summary(
            gameplay_context("伊莉雅现在拿得到哪些装备？")
        )
        exposed = next(
            item for item in state["characters"] if item["name"] == "伊莉雅"
        )
        self.assertEqual(
            exposed["unavailable_equipment"]["钢匕首"]["location"],
            "卡里巴村监狱证物柜",
        )

        equip = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context("伊莉雅装备钢匕首。"),
            {
                "action_type": "Equip",
                "actor": "伊莉雅",
                "details": {"slots": {"main_hand": "钢匕首"}},
                "evidence": "伊莉雅装备钢匕首",
            },
        )
        self.assertFalse(equip.ok)
        self.assertEqual(equip.error_code, "RULE_ACTION_REJECTED")
        self.assertIn("当前无法取用", equip.message)
        self.assertEqual(ilya.equipped_main_hand, "徒手攻击")

        reloaded_service = FUGMHttpService(
            data_root=self.tmpdir.name,
            use_llm=False,
        )
        reloaded = reloaded_service._runtime("gameplay-tool-test").app
        loaded_ilya = reloaded.character_manager.get("伊莉雅")
        self.assertIn("钢匕首", loaded_ilya.equipment)
        self.assertIn("钢匕首", loaded_ilya.unavailable_equipment)
        self.assertEqual(loaded_ilya.equipped_main_hand, "徒手攻击")

    def test_restoring_access_does_not_silently_re_equip_unless_explicit(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.equipment.append("钢匕首")
        self.app.interceptor.economy_manager.configure_loadout(
            "伊莉雅",
            {"main_hand": "钢匕首"},
            allow_armor=True,
        )
        manager = self.app.interceptor.economy_manager
        manager.set_equipment_access(
            "伊莉雅",
            ["钢匕首"],
            available=False,
            reason="被收缴",
            location="证物柜",
        )

        restored = self.service.gm_gameplay_tools.set_equipment_access(
            gameplay_context("伊莉雅从证物柜里取回钢匕首。"),
            {
                "actor": "伊莉雅",
                "mode": "restore",
                "items": ["钢匕首"],
                "restore_loadout": False,
                "evidence": "从证物柜里取回钢匕首",
            },
        )

        self.assertTrue(restored.ok, restored.message)
        self.assertNotIn("钢匕首", ilya.unavailable_equipment)
        self.assertEqual(ilya.equipped_main_hand, "徒手攻击")

    def test_restoring_access_defaults_to_the_suspended_loadout_outside_conflict(
        self,
    ) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.equipment.append("钢匕首")
        manager = self.app.interceptor.economy_manager
        manager.configure_loadout(
            "伊莉雅",
            {"main_hand": "钢匕首"},
            allow_armor=True,
        )
        manager.set_equipment_access(
            "伊莉雅",
            ["钢匕首"],
            available=False,
            reason="被收缴",
            location="证物柜",
        )

        restored = self.service.gm_gameplay_tools.set_equipment_access(
            gameplay_context("伊莉雅从证物柜取回自己的钢匕首。"),
            {
                "actor": "伊莉雅",
                "mode": "restore",
                "items": ["钢匕首"],
                "evidence": "从证物柜取回自己的钢匕首",
            },
        )

        self.assertTrue(restored.ok, restored.message)
        self.assertTrue(restored.result["restored_loadout"])
        self.assertEqual(ilya.equipped_main_hand, "钢匕首")

    def test_declared_check_restores_equipment_immediately_after_success(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.identity = "白花守望者"
        ilya.fabula_points = 3
        ilya.equipment.append("钢匕首")
        manager = self.app.interceptor.economy_manager
        manager.configure_loadout(
            "伊莉雅",
            {"main_hand": "钢匕首"},
            allow_armor=True,
        )
        manager.set_equipment_access(
            "伊莉雅",
            ["钢匕首"],
            available=False,
            reason="被收缴",
            location="证物柜",
        )
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["INS", "DEX"],
                dice=[(10, 5), (8, 4)],
                total=9,
                modifier=0,
                high_roll=5,
                target_number=8,
                success=True,
                critical_success=False,
                fumble=False,
                target="证物柜",
                reason="打开证物柜取回装备",
            )
        )
        message = "伊莉雅尝试打开证物柜，取回自己的钢匕首。"

        declared = self.service.gm_gameplay_tools.declare_check_action(
            gameplay_context(message),
            {
                "action_type": "RequestRoll",
                "actor": "伊莉雅",
                "target": "证物柜",
                "attributes": ["洞察", "敏捷"],
                "difficulty": 8,
                "purpose": "打开证物柜取回自己的钢匕首",
                "check_label": "取回钢匕首",
                "success_observation": "证物柜打开了，伊莉雅取回自己的钢匕首。",
                "success_state_changes": [
                    {
                        "type": "equipment_access",
                        "actor": "伊莉雅",
                        "mode": "restore",
                        "items": ["钢匕首"],
                    }
                ],
                "failure_consequence": "锁舌卡死，钢匕首仍留在证物柜里。",
                "evidence": message,
            },
        )
        self.assertTrue(declared.ok, declared.message)
        self.assertIn("钢匕首", ilya.unavailable_equipment)

        rolled = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("投。"),
            {
                "action_type": "ResolveDecision",
                "actor": "伊莉雅",
                "window_id": declared.result["window_id"],
                "choice": "roll",
                "details": {},
                "evidence": "投。",
            },
        )
        self.assertTrue(rolled.ok, rolled.message)
        ilya = self.app.character_manager.get("伊莉雅")
        self.assertNotIn("钢匕首", ilya.unavailable_equipment)
        self.assertEqual(ilya.equipped_main_hand, "钢匕首")
        self.assertFalse(rolled.result["pending_decisions"])
        self.assertTrue(
            rolled.result["check_receipt"]["success_state_changes_applied"]
        )
        self.assertEqual(
            rolled.result["check_receipt"]["applied_success_state_changes"][0][
                "items"
            ],
            ["钢匕首"],
        )

    def test_declared_check_cannot_claim_retrieved_gear_without_state_change(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.equipment.append("钢匕首")
        self.app.interceptor.economy_manager.set_equipment_access(
            "伊莉雅",
            ["钢匕首"],
            available=False,
            reason="被收缴",
            location="证物柜",
        )
        message = "伊莉雅打开证物柜取回自己的钢匕首。"

        declared = self.service.gm_gameplay_tools.declare_check_action(
            gameplay_context(message),
            {
                "action_type": "RequestRoll",
                "actor": "伊莉雅",
                "target": "证物柜",
                "attributes": ["洞察", "敏捷"],
                "difficulty": 8,
                "purpose": "打开证物柜取回钢匕首",
                "check_label": "取回钢匕首",
                "success_observation": "柜门打开，伊莉雅实际取回钢匕首。",
                "failure_consequence": "锁舌卡死，钢匕首仍留在柜里。",
                "details": {},
                "evidence": message,
            },
        )

        self.assertFalse(declared.ok)
        self.assertEqual(
            declared.error_code,
            "CHECK_SUCCESS_EQUIPMENT_STATE_UNCOMMITTED",
        )
        self.assertFalse(
            self.app.interceptor.decision_window_manager.pending(
                kind="check_roll_confirmation",
                owner="伊莉雅",
            )
        )

    def test_failed_declared_check_never_restores_equipment(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.identity = "白花守望者"
        ilya.fabula_points = 3
        ilya.equipment.append("钢匕首")
        manager = self.app.interceptor.economy_manager
        manager.configure_loadout(
            "伊莉雅",
            {"main_hand": "钢匕首"},
            allow_armor=True,
        )
        manager.set_equipment_access(
            "伊莉雅",
            ["钢匕首"],
            available=False,
            reason="被收缴",
            location="证物柜",
        )
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["INS", "DEX"],
                dice=[(10, 2), (8, 3)],
                total=5,
                modifier=0,
                high_roll=3,
                target_number=8,
                success=False,
                critical_success=False,
                fumble=False,
                target="证物柜",
                reason="打开证物柜取回装备",
            )
        )
        message = "伊莉雅尝试打开证物柜，取回自己的钢匕首。"
        declared = self.service.gm_gameplay_tools.declare_check_action(
            gameplay_context(message),
            {
                "action_type": "RequestRoll",
                "actor": "伊莉雅",
                "target": "证物柜",
                "attributes": ["洞察", "敏捷"],
                "difficulty": 8,
                "purpose": "打开证物柜取回自己的钢匕首",
                "check_label": "取回钢匕首",
                "success_observation": "证物柜打开了，伊莉雅取回自己的钢匕首。",
                "success_state_changes": [
                    {
                        "type": "equipment_access",
                        "actor": "伊莉雅",
                        "mode": "restore",
                        "items": ["钢匕首"],
                    }
                ],
                "failure_consequence": "锁舌卡死，钢匕首仍留在证物柜里。",
                "evidence": message,
            },
        )
        rolled = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("投。"),
            {
                "action_type": "ResolveDecision",
                "actor": "伊莉雅",
                "window_id": declared.result["window_id"],
                "choice": "roll",
                "details": {},
                "evidence": "投。",
            },
        )
        accepted = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("我接受失败结果，不重掷。"),
            {
                "action_type": "ResolveDecision",
                "actor": "伊莉雅",
                "window_id": rolled.result["pending_decisions"][0]["window_id"],
                "choice": "accept_result",
                "details": {},
                "evidence": "我接受失败结果，不重掷。",
            },
        )

        self.assertTrue(accepted.ok, accepted.message)
        ilya = self.app.character_manager.get("伊莉雅")
        self.assertIn("钢匕首", ilya.unavailable_equipment)
        self.assertEqual(ilya.equipped_main_hand, "徒手攻击")
        self.assertFalse(
            accepted.result["check_receipt"]["success_state_changes_applied"]
        )

    def test_nested_roll_outcome_in_pending_window_is_json_safe_for_core_gm(self) -> None:
        outcome = RollOutcome(
            actor="洛岚",
            attributes=["INS", "WLP"],
            dice=[(8, 3), (10, 4)],
            total=7,
            modifier=0,
            high_roll=4,
            target_number=8,
            success=False,
            critical_success=False,
            fumble=False,
            applied_affinity=Affinity.NORMAL,
        )
        self.app.interceptor.decision_window_manager.create(
            kind="skill_parameter",
            owner="艾薇娅",
            prompt="是否使用予以信任？",
            blocking=True,
            payload={"trigger_context": {"outcome": outcome}},
        )

        context = gameplay_context("我援用特质重掷。", speaker="白河")
        state = self.service.gm_gameplay_tools.state_summary(context)
        core_state = GMToolStateSnapshotBuilder(self.service).build(context)

        json.dumps(state, ensure_ascii=False)
        json.dumps(core_state, ensure_ascii=False)
        encoded_outcome = state["pending_decisions"][0]["payload"][
            "trigger_context"
        ]["outcome"]
        self.assertIsInstance(encoded_outcome, dict)
        self.assertEqual(encoded_outcome["total"], 7)
        self.assertEqual(encoded_outcome["applied_affinity"], "normal")

    def test_internal_state_keeps_opportunities_but_model_state_only_keeps_current_scene(self) -> None:
        contract = SessionDramaticContract(
            title="卡里巴村越狱",
            potential_scenes=[
                SessionSceneOpportunity(
                    scene_key="cells",
                    scene_role="strong_start",
                    title="熄灭的牢门符文",
                    location="风铃廊",
                ),
                SessionSceneOpportunity(
                    scene_key="records",
                    scene_role="alternate_approach",
                    title="值夜记录的蓝墨",
                    location="卡里巴村监狱·值班室",
                    entry_points=["找回装备", "检查转运记录"],
                ),
            ],
        )
        self.app.story_arc_manager.state.current_pacing_plan.dramatic_contract = contract
        frame = self.app.scene_frame_manager.ensure_frame(
            scene=self.app.scene_manager.current_scene,
            recent_chat="牢门符文刚刚熄灭。",
            world_state=self.app.world_state,
            character_manager=self.app.character_manager,
            contract=contract,
        )
        frame.session_opportunity_key = "cells"
        frame.session_opportunity_role = "strong_start"
        frame.session_opportunity_title = "熄灭的牢门符文"

        context = gameplay_context("诺艾尔想去值班室。")
        builder = GMToolStateSnapshotBuilder(self.service)
        internal_state = builder.build_full(context)
        lifecycle = internal_state["processes"]["session"]["scene_lifecycle"]

        self.assertEqual(lifecycle["current_opportunity"]["key"], "cells")
        self.assertEqual(
            [item["key"] for item in lifecycle["unused_opportunities"]],
            ["records"],
        )
        self.assertIn("不能只在叙事中声称已经抵达", lifecycle["usage"])

        model_state = builder.build(context)
        model_lifecycle = model_state["processes"]["session"]["scene_lifecycle"]
        self.assertEqual(model_lifecycle["current_opportunity"]["key"], "cells")
        self.assertNotIn("unused_opportunities", model_lifecycle)
        self.assertNotIn("used_opportunity_keys", model_lifecycle)
        self.assertNotIn("usage", model_lifecycle)
        self.assertNotIn("scene", model_state["runtime"])
        self.assertEqual(
            set(model_state["processes"]["scene"]),
            {"action_round"},
        )

    def test_trust_window_preserves_the_complete_selected_option(self) -> None:
        loran = self.app.character_manager.get("洛岚")
        loran.identity = "辉钢财团出逃的魔导工匠"
        loran.theme = "赎罪"
        loran.origin = "第七采掘城"
        loran.fabula_points = 1
        self.app.character_manager.add(
            Character(
                name="艾薇娅",
                attributes={"DEX": 8, "INS": 10, "MIG": 6, "WLP": 10},
                max_hp=35,
                hp=35,
                max_mp=55,
                mp=55,
                fabula_points=1,
                traits=["pc"],
                skills={"予以信任": 1},
            )
        )
        self.app.world_state.world_profile.hero_drafts["时雨"] = HeroDraft(
            player_name="时雨",
            hero_name="艾薇娅",
        )
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="洛岚",
                attributes=["INS", "WLP"],
                dice=[(8, 2), (10, 3)],
                total=5,
                modifier=0,
                high_roll=3,
                target_number=8,
                success=False,
                critical_success=False,
                fumble=False,
                target="旧路闸门的声响",
                reason="辨认旧路闸门的声响",
            )
        )
        self.app.interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {
                    "actor": "洛岚",
                    "target": "旧路闸门的声响",
                    "attributes": ["INS", "WLP"],
                    "target_number": 8,
                    "non_damage": True,
                },
            )
        )
        window = self.app.interceptor.decision_window_manager.find_pending(
            kind="skill_parameter",
            owner="艾薇娅",
        )
        self.assertIsNotNone(window)

        receipt = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context(
                "我发动予以信任，援用洛岚的辉钢财团出逃的魔导工匠。",
                speaker="时雨",
            ),
            {
                "action_type": "ResolveDecision",
                "actor": "艾薇娅",
                "window_id": window.window_id,
                "choice": "assist_trait",
                "details": {
                    "trait": "辉钢财团出逃的魔导工匠",
                    "target": "洛岚",
                },
                "evidence": "发动予以信任，援用洛岚的辉钢财团出逃的魔导工匠",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window.window_id
            )
        )

    def test_guard_is_rejected_outside_conflict_without_granting_combat_effects(self) -> None:
        receipt = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context("伊莉雅站到旅人身前，守住登记小室的方向。"),
            {
                "action_type": "Guard",
                "actor": "伊莉雅",
                "details": {},
                "evidence": "伊莉雅站到旅人身前",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "GUARD_REQUIRES_CONFLICT")
        self.assertFalse(self.app.character_manager.get("伊莉雅").guarding)

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
            "attribute_boosts": [],
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
                "attribute_boosts": [],
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
                    "attribute_boosts": [],
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

    def test_zero_hp_equipment_loss_must_commit_and_atomically_restrict_items(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.equipment.extend(["钢匕首", "细剑"])
        hero.abilities.append("可装备职业近战武器")
        self.app.interceptor.economy_manager.configure_loadout(
            "伊莉雅",
            {"main_hand": "钢匕首", "off_hand": "细剑"},
        )
        self.app.conflict_manager.start_scene("断桥之战", ["伊莉雅", "洛岚"])
        hero.hp = 0
        self.app.conflict_manager.resolve_zero_hp("伊莉雅")
        window = self.app.interceptor.decision_window_manager.find_pending(
            kind="zero_hp",
            owner="伊莉雅",
        )
        message = "伊莉雅选择放弃抵抗。"
        base = {
            "action_type": "ResolveZeroHP",
            "actor": "伊莉雅",
            "window_id": window.window_id,
            "choice": "give_up_resistance",
            "evidence": "选择放弃抵抗",
        }

        missing = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context(message),
            {
                **base,
                "details": {
                    "consequence_type": "损失",
                    "consequence": "钢匕首与细剑被守卫收缴",
                },
            },
        )

        self.assertFalse(missing.ok)
        self.assertEqual(missing.error_code, "ZERO_HP_EQUIPMENT_STATE_UNCOMMITTED")
        self.assertIsNotNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window.window_id
            )
        )

        committed = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context(message),
            {
                **base,
                "details": {
                    "consequence_type": "损失",
                    "consequence": "钢匕首与细剑被守卫收缴",
                    "equipment_access_changes": [
                        {
                            "type": "equipment_access",
                            "actor": "伊莉雅",
                            "mode": "restrict",
                            "items": ["钢匕首", "细剑"],
                            "reason": "败北后被守卫收缴",
                            "location": "卡里巴村监狱证物柜",
                        }
                    ],
                },
            },
        )

        self.assertTrue(committed.ok, committed.message)
        self.assertEqual(
            set(hero.unavailable_equipment),
            {"钢匕首", "细剑"},
        )
        self.assertEqual(hero.equipped_main_hand, "徒手攻击")
        self.assertEqual(hero.equipped_off_hand, "")
        self.assertEqual(
            committed.result["zero_hp_equipment_access_changes"][0]["items"],
            ["钢匕首", "细剑"],
        )

    def test_last_zero_hp_choice_requires_natural_conflict_end(self) -> None:
        self.app.character_manager.add(
            Character(
                name="财团机兵",
                attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 6},
                max_hp=70,
                hp=70,
                max_mp=40,
                mp=40,
                traits=["enemy", "construct"],
            )
        )
        self.app.conflict_manager.start_scene(
            "断桥之战",
            ["财团机兵", "伊莉雅"],
            player_side=["伊莉雅"],
            enemy_side=["财团机兵"],
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
                    "consequence": "被财团机兵押回囚室",
                },
                "evidence": "选择放弃抵抗",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertIn("end_conflict", receipt.result["required_followup_tools"])
        self.assertEqual(receipt.result["required_followup_mode"], "all")
        self.assertEqual(
            receipt.result["conflict_resolution_status"]["natural_outcome"],
            "player_side_removed",
        )

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
        self.app.conflict_manager.begin_current_turn()
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
            None,
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
        self.app.conflict_manager.begin_current_turn()

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
            None,
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
        self.assertFalse(receipt.result["silent_commit_allowed"])
        self.assertFalse(receipt.result["source_message_already_public"])

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

    def test_story_item_pickup_then_throw_commits_final_placement_silently(self) -> None:
        for hero_name, player_name in (("诺艾尔", "测试玩家甲"), ("艾丽妮", "测试玩家乙")):
            self.app.character_manager.add(
                Character(
                    name=hero_name,
                    attributes={"DEX": 8, "INS": 8, "MIG": 6, "WLP": 10},
                    max_hp=35,
                    hp=35,
                    max_mp=55,
                    mp=55,
                    traits=["pc"],
                )
            )
            self.app.world_state.world_profile.hero_drafts[player_name] = HeroDraft(
                player_name=player_name,
                hero_name=hero_name,
            )
            self.app.scene_manager.add_participant(hero_name)

        message = (
            "我捡起在铁栏根部摸到一枚被雨水冲出来的细长铁片，轻声和艾丽妮说："
            "“oi，你刚才在尝试寻找漏洞越狱是吧，这个铁片似乎能拿来撬锁”，"
            "说着我把铁片从铁栏的缝隙抛了过去。"
        )
        receipt = self.service.gm_gameplay_tools.commit_story_item_action(
            gameplay_context(message, speaker="测试玩家甲"),
            {
                "actor": "诺艾尔",
                "operation": "place",
                "item_name": "细长铁片",
                "description": "一枚被雨水从铁栏根部冲出来的细长铁片",
                "to_location": "艾丽妮牢房一侧",
                "state_note": "可用作简易撬锁工具",
                "tags": ["工具", "越狱"],
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertTrue(receipt.state_changed)
        self.assertTrue(receipt.result["silent_commit_allowed"])
        self.assertEqual(receipt.public_fallback_reply, "")
        self.assertFalse(receipt.lock_public_reply)
        self.assertEqual(receipt.pacing_events[0].public_image, "")
        self.assertEqual(receipt.pacing_events[0].consequence, "")
        item_id = receipt.result["story_item"]["item_id"]
        item = self.app.world_state.story_items[item_id]
        self.assertEqual(item.holder, "")
        self.assertEqual(item.location, "风铃廊·艾丽妮牢房一侧")
        self.assertEqual(item.status.value, "placed")
        self.assertEqual(item.history[-1].operation, "place")
        self.assertNotIn(
            "持有剧情物件【细长铁片】",
            self.app.world_state.subject_facts.get("诺艾尔", []),
        )

        reloaded_service = FUGMHttpService(data_root=self.tmpdir.name, use_llm=False)
        reloaded = reloaded_service._runtime("gameplay-tool-test").app
        loaded_item = reloaded.world_state.story_items[item_id]
        self.assertEqual(loaded_item.holder, "")
        self.assertEqual(loaded_item.location, "风铃廊·艾丽妮牢房一侧")

        pickup_message = "艾丽妮把落在牢房这一侧的细长铁片捡起来。"
        picked_up = reloaded_service.gm_gameplay_tools.commit_story_item_action(
            gameplay_context(pickup_message, speaker="测试玩家乙"),
            {
                "actor": "艾丽妮",
                "operation": "acquire",
                "item_name": "细长铁片",
                "item_id": item_id,
                "evidence": pickup_message,
            },
        )

        self.assertTrue(picked_up.ok, picked_up.message)
        self.assertTrue(picked_up.result["silent_commit_allowed"])
        self.assertEqual(loaded_item.holder, "艾丽妮")
        self.assertIn(
            "持有剧情物件【细长铁片】",
            reloaded.world_state.subject_facts.get("艾丽妮", []),
        )

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
        self.assertEqual(receipt.result["required_followup_tools"], ["declare_check_action"])
        self.assertEqual(receipt.result["allowed_followup_tools"], ["declare_check_action"])
        self.assertEqual(receipt.result["action_round"], {})
        self.assertEqual(
            self.app.world_state.find_story_item(name="白蜡路封").holder,
            "洛岚",
        )
        self.assertEqual(self.app.clock_manager.get("巡逻逼近").current, 0)

    def test_story_item_operation_preserves_custody_and_persists_state(self) -> None:
        acquired = self.service.gm_gameplay_tools.commit_story_item_action(
            gameplay_context("伊莉雅拿起蓝芯守望灯。"),
            {
                "actor": "伊莉雅",
                "operation": "acquire",
                "item_name": "蓝芯守望灯",
                "state_note": "未点亮",
                "public_result": "伊莉雅拿起蓝芯守望灯；蓝芯守望灯现由伊莉雅持有。",
                "public_fact": "蓝芯守望灯现由伊莉雅持有。",
                "evidence": "拿起蓝芯守望灯",
            },
        )
        self.assertTrue(acquired.ok, acquired.message)

        operated = self.service.gm_gameplay_tools.commit_story_item_action(
            gameplay_context("伊莉雅点亮手中的蓝芯守望灯。"),
            {
                "actor": "伊莉雅",
                "operation": "operate",
                "item_name": "蓝芯守望灯",
                "item_id": acquired.result["story_item"]["item_id"],
                "state_note": "已点亮",
                "public_result": "伊莉雅点亮蓝芯守望灯；蓝芯守望灯发出示警蓝光，仍由伊莉雅持有。",
                "public_fact": "蓝芯守望灯发出示警蓝光，仍由伊莉雅持有。",
                "evidence": "点亮手中的蓝芯守望灯",
            },
        )

        self.assertTrue(operated.ok, operated.message)
        item = self.app.world_state.find_story_item(name="蓝芯守望灯")
        self.assertEqual(item.holder, "伊莉雅")
        self.assertEqual(item.status.value, "carried")
        self.assertEqual(item.current_state, "已点亮")
        self.assertEqual(item.history[-1].operation, "operate")
        self.assertEqual(item.history[-1].from_state, "未点亮")
        self.assertEqual(item.history[-1].to_state, "已点亮")

        reloaded_service = FUGMHttpService(data_root=self.tmpdir.name, use_llm=False)
        reloaded = reloaded_service._runtime("gameplay-tool-test").app
        self.assertEqual(
            reloaded.world_state.find_story_item(name="蓝芯守望灯").current_state,
            "已点亮",
        )

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
        self.assertEqual(first["clock_progress"], ["【财团巡逻队逼近】0/6"])
        self.assertTrue(first["clock_status_refresh"])

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

    def test_declared_check_shows_short_risk_but_hides_full_failure_consequence(self) -> None:
        message = "伊莉雅观察牢门符文，想判断它和地下脉动是否有关。"
        context = gameplay_context(message)
        context.metadata.update(
            {
                "source_event_id": "event-check-1",
                "source_message_id": "message-check-1",
            }
        )

        declared = self.service.gm_gameplay_tools.declare_check_action(
            context,
            {
                "action_type": "Investigate",
                "actor": "伊莉雅",
                "target": "牢门符文与地下脉动",
                "attributes": ["洞察", "洞察"],
                "difficulty": 10,
                "purpose": "判断两者是否属于同一种魔力",
                "check_label": "辨认魔力共鸣",
                "base_observation": "地下每次震动时，牢门符文都会同时变暗。",
                "success_observation": "两者并非同一种魔力；地下脉动正在短暂切断牢门供能。",
                "risk_hint": "牢门符文的辉光并不稳定",
                "failure_consequence": "交叠的辉光让伊莉雅暂时分不清两种回路。",
                "details": {},
                "evidence": message,
            },
        )

        self.assertTrue(declared.ok, declared.message)
        self.assertTrue(declared.lock_public_reply)
        self.assertIn("地下每次震动", declared.public_fallback_reply)
        self.assertIn("【洞察+洞察】", declared.public_fallback_reply)
        self.assertIn("难度等级10", declared.public_fallback_reply)
        self.assertIn("牢门符文的辉光并不稳定。", declared.public_fallback_reply)
        self.assertNotIn("交叠的辉光让伊莉雅", declared.public_fallback_reply)
        window = self.app.interceptor.decision_window_manager.find_pending(
            kind="check_roll_confirmation",
            owner="伊莉雅",
        )
        self.assertIsNotNone(window)
        self.assertEqual(self.app.scene_manager.action_round_snapshot()["acted"], [])

        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["INS", "INS"],
                dice=[(10, 7), (10, 6)],
                total=13,
                modifier=0,
                high_roll=7,
                target_number=10,
                success=True,
                critical_success=False,
                fumble=False,
                target="牢门符文与地下脉动",
                reason="判断两者是否属于同一种魔力",
            )
        )
        rolled = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("投。"),
            {
                "action_type": "ResolveDecision",
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "roll",
                "details": {},
                "evidence": "投。",
            },
        )

        self.assertTrue(rolled.ok, rolled.message)
        self.assertIn("两者并非同一种魔力", rolled.public_fallback_reply)
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(
                kind="check_roll_confirmation",
                owner="伊莉雅",
            )
        )
        self.assertEqual(
            rolled.result["source_event"]["text"],
            message,
        )
        self.assertEqual(rolled.narrative_events[0].declaration, message)
        frame = self.app.scene_frame_manager.current_frame
        self.assertIsNotNone(frame)
        self.assertIn(
            "地下每次震动时，牢门符文都会同时变暗。",
            frame.public_facts,
        )
        self.assertIn(
            "两者并非同一种魔力；地下脉动正在短暂切断牢门供能。",
            frame.public_facts,
        )

    def test_declared_open_investigate_applies_knowledge_is_power_through_final_acceptance(
        self,
    ) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.skills = {"知识就是力量": 1}
        ilya.identity = "记录古代灾变的星塔学者"
        ilya.fabula_points = 3
        captured: list[tuple[Action, object]] = []
        original_resolve = self.app.interceptor.resolve

        def capture_resolution(action: Action):
            resolution = original_resolve(action)
            captured.append((action, resolution))
            return resolution

        self.app.interceptor.resolve = capture_resolution
        message = (
            "伊莉雅利用知识就是力量，以INS+INS公开检定分析牢门符文，"
            "这是非伤害检定。"
        )
        declared = self.service.gm_gameplay_tools.declare_check_action(
            gameplay_context(message),
            {
                "action_type": "Investigate",
                "actor": "伊莉雅",
                "target": "牢门符文",
                "attributes": ["洞察", "洞察"],
                "difficulty": 10,
                "purpose": "分析牢门符文的运作方式",
                "check_label": "分析牢门符文",
                "success_observation": "伊莉雅辨认出牢门符文会随地下脉动短暂断开。",
                "failure_consequence": "伊莉雅这次未能辨认牢门符文的运作方式。",
                "evidence": message,
            },
        )

        self.assertTrue(declared.ok, declared.message)
        declaration_window = self.app.interceptor.decision_window_manager.find_pending(
            window_id=str(declared.result["window_id"])
        )
        self.assertIsNotNone(declaration_window)
        self.assertTrue(
            declaration_window.payload["check_arguments"]["open_check"]
        )

        # d10=1 + d10=2 + SL1 = 4, a non-fumble failure that opens the normal
        # post-check acceptance window and exercises the final replay path.
        self.app.interceptor.rules_engine._rng.seed(2)
        rolled = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("伊莉雅确认现在投骰。"),
            {
                "action_type": "ResolveDecision",
                "actor": "伊莉雅",
                "window_id": declared.result["window_id"],
                "choice": "roll",
                "evidence": "确认现在投骰",
            },
        )
        self.assertTrue(rolled.ok, rolled.message)
        self.assertTrue(rolled.result["pending_decisions"])

        accepted = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("伊莉雅接受当前结果，不援用特质。"),
            {
                "action_type": "ResolveDecision",
                "actor": "伊莉雅",
                "window_id": rolled.result["pending_decisions"][0]["window_id"],
                "choice": "accept_result",
                "evidence": "接受当前结果",
            },
        )

        self.assertTrue(accepted.ok, accepted.message)
        settled = [
            (action, resolution)
            for action, resolution in captured
            if action.action_type == ActionType.INVESTIGATE
            and not resolution.payload.get("check_result_provisional")
        ]
        self.assertTrue(settled)
        settled_action, final = settled[-1]
        # Acceptance deliberately rewrites ``resolution.action`` to the
        # ResolveDecision action.  The authoritative replay input and the
        # committed-source journal must both retain the declaration flag.
        self.assertTrue(settled_action.parameters["open_check"])
        self.assertTrue(
            final.payload["committed_source_action"].parameters["open_check"]
        )
        self.assertEqual(final.payload["roll"].modifier, 1)
        self.assertEqual(final.payload["roll"].total, 4)
        self.assertEqual(
            final.payload["skill_trigger_effects"],
            [
                {
                    "source": "知识就是力量",
                    "amount": 1,
                    "note": "【洞察+洞察】开放检定获得修正。",
                }
            ],
        )

    def test_explicit_knowledge_is_power_declaration_fails_closed_when_invalid(
        self,
    ) -> None:
        message = "伊莉雅利用知识就是力量进行公开检定。"
        base_arguments = {
            "action_type": "Investigate",
            "actor": "伊莉雅",
            "target": "牢门符文",
            "attributes": ["洞察", "洞察"],
            "difficulty": 10,
            "purpose": "分析牢门符文",
            "check_label": "分析牢门符文",
            "success_observation": "伊莉雅辨认出符文的运作方式。",
            "failure_consequence": "伊莉雅这次没有辨认出符文的运作方式。",
            "evidence": message,
        }

        not_learned = self.service.gm_gameplay_tools.declare_check_action(
            gameplay_context(message),
            dict(base_arguments),
        )
        self.assertFalse(not_learned.ok)
        self.assertEqual(
            not_learned.error_code,
            "KNOWLEDGE_IS_POWER_NOT_LEARNED",
        )

        self.app.character_manager.get("伊莉雅").skills = {"知识就是力量": 1}
        wrong_attributes = self.service.gm_gameplay_tools.declare_check_action(
            gameplay_context(message),
            {**base_arguments, "attributes": ["洞察", "意志"]},
        )
        self.assertFalse(wrong_attributes.ok)
        self.assertEqual(
            wrong_attributes.error_code,
            "KNOWLEDGE_IS_POWER_REQUIRES_INS_INS",
        )
        self.assertFalse(
            self.app.interceptor.decision_window_manager.pending(
                kind="check_roll_confirmation",
                owner="伊莉雅",
            )
        )

    def test_attempt_failure_authority_accepts_only_the_deterministic_no_change_result(
        self,
    ) -> None:
        message = "伊莉雅观察塔顶的风向。"
        consequence = "伊莉雅这次未能辨认塔顶风向；本次尝试没有造成其他现场变化。"

        declared = self.service.gm_gameplay_tools.declare_check_action(
            gameplay_context(message),
            {
                "action_type": "Investigate",
                "actor": "伊莉雅",
                "target": "塔顶风向",
                "attributes": ["洞察", "洞察"],
                "difficulty": 8,
                "purpose": "辨认塔顶风向",
                "check_label": "辨认风向",
                "success_observation": "东南风正把云层推向山口。",
                "failure_consequence": consequence,
                "failure_authority": {"kind": "attempt"},
                "evidence": message,
            },
        )

        self.assertTrue(declared.ok, declared.message)
        window = self.app.interceptor.decision_window_manager.find_pending(
            window_id=str(declared.result["window_id"])
        )
        self.assertEqual(
            window.payload["check_arguments"]["failure_authority"],
            {"kind": "attempt", "authority_ref": ""},
        )

    def test_attempt_failure_authority_normalizes_new_cross_scene_threat_effects(self) -> None:
        cases = (
            (
                "伊莉雅查看屋梁的火星。",
                "查看屋梁的火星",
                "火势吞没整座工坊，所有出口从此封死。",
            ),
            (
                "伊莉雅试着辨认地板机关。",
                "辨认地板机关",
                "机关启动永久锁定，另一层的同伴也被困住。",
            ),
        )
        for message, purpose, consequence in cases:
            with self.subTest(consequence=consequence):
                receipt = self.service.gm_gameplay_tools.declare_check_action(
                    gameplay_context(message),
                    {
                        "action_type": "Investigate",
                        "actor": "伊莉雅",
                        "target": purpose,
                        "attributes": ["洞察", "洞察"],
                        "difficulty": 9,
                        "purpose": purpose,
                        "check_label": purpose,
                        "success_observation": "目标上留有一处可辨认的旧痕。",
                        "failure_consequence": consequence,
                        "failure_authority": {"kind": "attempt"},
                        "evidence": message,
                    },
                )

                self.assertTrue(receipt.ok, receipt.message)
                window = self.app.interceptor.decision_window_manager.find_pending(
                    window_id=str(receipt.result["window_id"])
                )
                self.assertEqual(
                    window.payload["check_arguments"]["failure_consequence"],
                    f"伊莉雅这次未能{purpose}；本次尝试没有造成其他现场变化。",
                )
                self.app.interceptor.decision_window_manager.resolve(
                    window_id=receipt.result["window_id"],
                    owner="伊莉雅",
                    responder="伊莉雅",
                    resolution={"choice": "cancel"},
                )

    def test_structured_hazard_failure_requires_an_exact_due_effect_record(self) -> None:
        message = "伊莉雅冲过风暴中的吊桥，抵达对岸平台。"
        context = gameplay_context(message)
        scene = self.app.scene_manager.current_scene
        context.metadata["check_failure_authorities"] = [
            {
                "hazard_id": "bridge-gust-3",
                "source_kind": "structured_hazard",
                "status": "triggered",
                "scene_id": scene.scene_id,
                "failure_consequence": "阵风把吊桥推回岩壁，伊莉雅仍留在桥头。",
            }
        ]

        accepted = self.service.gm_gameplay_tools.declare_movement_check(
            context,
            {
                "actor": "伊莉雅",
                "destination": "对岸平台",
                "resolution_mode": "single_obstacle",
                "obstacle": "风暴中的吊桥",
                "attributes": ["敏捷", "意志"],
                "difficulty": 10,
                "purpose": "冲过吊桥",
                "check_label": "穿越吊桥",
                "success_observation": "伊莉雅穿过吊桥抵达对岸平台。",
                "failure_consequence": "阵风把吊桥推回岩壁，伊莉雅仍留在桥头。",
                "failure_authority": {
                    "kind": "structured_hazard",
                    "authority_ref": "bridge-gust-3",
                },
                "evidence": message,
            },
        )
        self.assertTrue(accepted.ok, accepted.message)

        self.app.interceptor.decision_window_manager.resolve(
            window_id=accepted.result["window_id"],
            owner="伊莉雅",
            responder="伊莉雅",
            resolution={"choice": "cancel"},
        )
        rejected = self.service.gm_gameplay_tools.declare_movement_check(
            context,
            {
                "actor": "伊莉雅",
                "destination": "对岸平台",
                "resolution_mode": "single_obstacle",
                "obstacle": "风暴中的吊桥",
                "attributes": ["敏捷", "意志"],
                "difficulty": 10,
                "purpose": "冲过吊桥",
                "check_label": "穿越吊桥",
                "success_observation": "伊莉雅穿过吊桥抵达对岸平台。",
                "failure_consequence": "伊莉雅未能通过吊桥，手中的地图被阵风卷走。",
                "failure_authority": {
                    "kind": "structured_hazard",
                    "authority_ref": "bridge-gust-3",
                },
                "evidence": message,
            },
        )
        self.assertFalse(rejected.ok)
        self.assertEqual(rejected.error_code, "SCENE_CHANGE_EFFECT_NOT_AUTHORIZED")

    def test_split_party_check_focuses_the_actors_authoritative_branch(self) -> None:
        source = self.app.scene_manager.current_scene
        source.participants.remove("洛岚")
        source.participant_locations.pop("洛岚", None)
        self.app.scene_manager.actor_locations["洛岚"] = "驿站外院"
        destination, mode = self.app.scene_manager.focus_actor_branch(
            "洛岚",
            name="驿站外院",
            location="驿站外院",
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
                actor="洛岚",
                attributes=["INS", "INS"],
                dice=[(8, 6), (8, 5)],
                total=11,
                modifier=0,
                high_roll=6,
                target_number=8,
                success=True,
                critical_success=False,
                fumble=False,
            )
        )
        message = "洛岚在驿站外院检查泥地里的重靴足印。"
        context = gameplay_context(message, speaker="白河")
        declared = self.service.gm_gameplay_tools.declare_check_action(
            context,
            {
                "action_type": "Investigate",
                "actor": "洛岚",
                "target": "驿站外院的重靴足印",
                "attributes": ["洞察", "洞察"],
                "difficulty": 8,
                "purpose": "辨认来者人数与去向",
                "check_label": "辨认外院足印",
                "success_observation": "足印属于三名巡逻兵，并朝东门延伸。",
                "failure_consequence": "积水冲散足印，暂时无法判断。",
                "details": {},
                "evidence": message,
            },
        )

        self.assertTrue(declared.ok, declared.message)
        self.assertEqual(declared.result["focused_scene_id"], destination.scene_id)
        self.assertIs(self.app.scene_manager.current_scene, destination)
        rolled = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("投。", speaker="白河"),
            {
                "action_type": "ResolveDecision",
                "actor": "洛岚",
                "window_id": declared.result["window_id"],
                "choice": "roll",
                "details": {},
                "evidence": "投。",
            },
        )

        self.assertTrue(rolled.ok, rolled.message)
        self.assertEqual(rolled.result["check_receipt"]["scene_id"], destination.scene_id)
        progress = self.app.story_arc_manager.state.current_session_progress
        self.assertEqual(progress.scene_progress[destination.scene_id].reveals, 1)

    def test_declared_check_can_be_revised_without_rolling(self) -> None:
        message = "伊莉雅尝试硬撬牢门。"
        declared = self.service.gm_gameplay_tools.declare_check_action(
            gameplay_context(message),
            {
                "action_type": "RequestRoll",
                "actor": "伊莉雅",
                "target": "牢门",
                "attributes": ["力量", "力量"],
                "difficulty": 10,
                "purpose": "硬撬牢门",
                "check_label": "硬撬牢门",
                "success_observation": "牢门被撬开。",
                "failure_consequence": "锁舌咬死，蛮力暂时无法撬动它。",
                "details": {},
                "evidence": message,
            },
        )
        window_id = declared.result["window_id"]

        revised = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("等等，我换个办法。"),
            {
                "action_type": "ResolveDecision",
                "actor": "伊莉雅",
                "window_id": window_id,
                "choice": "revise",
                "details": {},
                "evidence": "等等，我换个办法。",
            },
        )

        self.assertTrue(revised.ok, revised.message)
        self.assertIn("新的做法", revised.public_fallback_reply)
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window_id,
            )
        )

    def test_declared_check_rejects_unknown_scene_condition_before_window(self) -> None:
        message = "伊莉雅借管家的通行条件拆除侧门锁栓。"
        declared = self.service.gm_gameplay_tools.declare_check_action(
            gameplay_context(message),
            {
                "action_type": "RequestRoll",
                "actor": "伊莉雅",
                "target": "侧门锁栓",
                "attributes": ["敏捷", "洞察"],
                "difficulty": 9,
                "purpose": "拆除侧门锁栓",
                "check_label": "拆除侧门锁栓",
                "success_observation": "侧门锁栓被完整拆除。",
                "failure_consequence": "锁栓卡在门体内，伊莉雅未能拆除它。",
                "condition_id": "mansion_passage",
                "details": {},
                "evidence": message,
            },
        )

        self.assertFalse(declared.ok)
        self.assertEqual(declared.error_code, "SCENE_CONDITION_NOT_FOUND")
        self.assertFalse(
            self.app.interceptor.decision_window_manager.pending(
                kind="check_roll_confirmation",
                owner="伊莉雅",
            )
        )

    def test_movement_check_atomically_stores_destination_before_roll(self) -> None:
        message = "伊莉雅避开巡逻灯，穿过侧门进入登记小室。"
        declared = self.service.gm_gameplay_tools.declare_movement_check(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "destination": "白花碑驿站·登记小室",
                "resolution_mode": "single_obstacle",
                "obstacle": "侧门外来回扫动的巡逻灯",
                "attributes": ["敏捷", "洞察"],
                "difficulty": 9,
                "purpose": "避开巡逻灯穿过侧门",
                "check_label": "潜入登记小室",
                "base_observation": "侧门通向登记小室，但巡逻灯正来回扫过门口。",
                "success_observation": "伊莉雅避开灯影，实际抵达登记小室。",
                "failure_consequence": "伊莉雅未能穿过侧门，只能留在原地。",
                "evidence": "穿过侧门进入登记小室",
            },
        )

        self.assertTrue(declared.ok, declared.message)
        self.assertEqual(declared.tool_name, "declare_movement_check")
        self.assertEqual(declared.result["difficulty"], 9)
        window = self.app.interceptor.decision_window_manager.find_pending(
            kind="check_roll_confirmation",
            owner="伊莉雅",
        )
        self.assertIsNotNone(window)
        transition = window.payload["check_arguments"]["success_transition"]
        self.assertEqual(transition["destination"], "白花碑驿站·登记小室")
        self.assertEqual(transition["participants"], ["伊莉雅"])
        self.assertEqual(self.app.scene_manager.location_of("伊莉雅"), "风铃廊")

    def test_movement_check_rejects_success_text_without_destination(self) -> None:
        receipt = self.service.gm_gameplay_tools.declare_movement_check(
            gameplay_context("伊莉雅穿过侧门进入登记小室。"),
            {
                "actor": "伊莉雅",
                "destination": "白花碑驿站·登记小室",
                "resolution_mode": "single_obstacle",
                "obstacle": "巡逻灯",
                "attributes": ["敏捷", "洞察"],
                "difficulty": 9,
                "purpose": "避开巡逻灯穿过侧门",
                "check_label": "潜入登记小室",
                "success_observation": "伊莉雅避开灯影，顺利穿了过去。",
                "failure_consequence": "伊莉雅未能穿过侧门，只能留在原地。",
                "evidence": "穿过侧门进入登记小室",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "SUCCESS_TRANSITION_PUBLIC_DESTINATION_REQUIRED",
        )

    def test_movement_check_rejects_route_search_expanded_to_remote_room(self) -> None:
        message = "伊莉雅沿东侧回廊往前走，找找通往楼上的路。"
        receipt = self.service.gm_gameplay_tools.declare_movement_check(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "destination": "灰棘宅邸·三楼主卧",
                "resolution_mode": "single_obstacle",
                "obstacle": "尚未查明的楼梯与沿途巡查",
                "attributes": ["敏捷", "洞察"],
                "difficulty": 10,
                "purpose": "穿过回廊找到楼梯并抵达三楼主卧",
                "check_label": "穿过宅邸",
                "success_observation": "伊莉雅抵达灰棘宅邸·三楼主卧。",
                "failure_consequence": "伊莉雅未能抵达三楼主卧。",
                "failure_authority": {"kind": "attempt"},
                "evidence": "沿东侧回廊往前走，找找通往楼上的路",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "EXPLORATION_EXPANDED_TO_ARRIVAL")
        self.assertFalse(
            self.app.interceptor.decision_window_manager.pending(owner="伊莉雅")
        )

    def test_movement_check_rejects_failure_beyond_the_current_ruin_obstacle(self) -> None:
        message = "伊莉雅贴着墙穿过断桥，走到祭坛前的平台。"
        receipt = self.service.gm_gameplay_tools.declare_movement_check(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "destination": "沉星遗迹·祭坛前平台",
                "resolution_mode": "single_obstacle",
                "obstacle": "断桥上不断坠落的碎石",
                "attributes": ["敏捷", "洞察"],
                "difficulty": 9,
                "purpose": "穿过断桥抵达祭坛前平台",
                "check_label": "穿过断桥",
                "success_observation": "伊莉雅抵达沉星遗迹·祭坛前平台。",
                "failure_consequence": "整个地下区域的全部通路封死，所有人都被困住。",
                "failure_authority": {"kind": "attempt"},
                "evidence": "贴着墙穿过断桥，走到祭坛前的平台",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "MOVEMENT_FAILURE_EXCEEDS_OBSTACLE")
        self.assertFalse(
            self.app.interceptor.decision_window_manager.pending(owner="伊莉雅")
        )

    def test_movement_attempt_normalizes_unstructured_environment_change(self) -> None:
        message = "伊莉雅避开门轴，穿过侧门进入东回廊。"
        declared = self.service.gm_gameplay_tools.declare_movement_check(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "destination": "灰棘宅邸·东回廊",
                "resolution_mode": "single_obstacle",
                "obstacle": "侧门的锁闭机关",
                "attributes": ["敏捷", "洞察"],
                "difficulty": 9,
                "purpose": "避开门轴穿过侧门",
                "check_label": "穿过侧门",
                "success_observation": "伊莉雅穿过侧门，抵达灰棘宅邸·东回廊。",
                "failure_consequence": "侧门的锁闭机关自行合拢，伊莉雅被困在门前。",
                "failure_authority": {"kind": "attempt"},
                "evidence": "避开门轴，穿过侧门进入东回廊",
            },
        )

        self.assertTrue(declared.ok, declared.message)
        window = self.app.interceptor.decision_window_manager.find_pending(
            kind="check_roll_confirmation",
            owner="伊莉雅",
        )
        self.assertIsNotNone(window)
        self.assertEqual(
            window.payload["check_arguments"]["failure_consequence"],
            "伊莉雅这次未能抵达灰棘宅邸·东回廊，位置保持不变。",
        )

    def test_generic_check_cannot_bypass_movement_scope_with_success_transition(self) -> None:
        message = "伊莉雅沿东侧回廊往前走，找找通往楼上的路。"
        receipt = self.service.gm_gameplay_tools.declare_check_action(
            gameplay_context(message),
            {
                "action_type": "RequestRoll",
                "actor": "伊莉雅",
                "target": "尚未查明的楼梯与沿途巡查",
                "attributes": ["敏捷", "洞察"],
                "difficulty": 10,
                "purpose": "穿过回廊找到楼梯并抵达三楼主卧",
                "check_label": "穿过宅邸",
                "success_observation": "伊莉雅抵达灰棘宅邸·三楼主卧。",
                "failure_consequence": "伊莉雅未能抵达三楼主卧。",
                "failure_authority": {"kind": "attempt"},
                "success_transition": {
                    "destination": "灰棘宅邸·三楼主卧",
                    "participants": ["伊莉雅"],
                },
                "evidence": "沿东侧回廊往前走，找找通往楼上的路",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "MOVEMENT_CHECK_TOOL_REQUIRED")
        self.assertFalse(
            self.app.interceptor.decision_window_manager.pending(owner="伊莉雅")
        )

    def test_generic_check_rejects_mansion_arrival_written_only_as_observation(self) -> None:
        message = "伊莉雅检查书房壁毯后的暗道。"
        receipt = self.service.gm_gameplay_tools.declare_check_action(
            gameplay_context(message),
            {
                "action_type": "RequestRoll",
                "actor": "伊莉雅",
                "target": "书房壁毯后的暗道",
                "attributes": ["洞察", "洞察"],
                "difficulty": 9,
                "purpose": "确认暗道通往哪里",
                "check_label": "检查宅邸暗道",
                "success_observation": "伊莉雅穿过暗道，抵达灰棘宅邸的三楼主卧。",
                "failure_consequence": "伊莉雅这次未能确认暗道通往哪里。",
                "failure_authority": {"kind": "attempt"},
                "evidence": "检查书房壁毯后的暗道",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "CHECK_SUCCESS_TRANSITION_UNCOMMITTED",
        )
        self.assertFalse(
            self.app.interceptor.decision_window_manager.pending(owner="伊莉雅")
        )

    def test_generic_check_rejects_bridge_collapse_written_only_as_observation(self) -> None:
        message = "伊莉雅割断吊桥边缘那根松绳。"
        receipt = self.service.gm_gameplay_tools.declare_check_action(
            gameplay_context(message),
            {
                "action_type": "RequestRoll",
                "actor": "伊莉雅",
                "target": "吊桥边缘的松绳",
                "attributes": ["力量", "洞察"],
                "difficulty": 9,
                "purpose": "割断眼前的松绳",
                "check_label": "割断松绳",
                "success_observation": "绳索断裂，整座吊桥轰然坍塌，两岸通路就此断绝。",
                "failure_consequence": "伊莉雅这次未能割断眼前的松绳。",
                "failure_authority": {"kind": "attempt"},
                "evidence": "割断吊桥边缘那根松绳",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "CHECK_SUCCESS_WORLD_CHANGE_UNCOMMITTED",
        )

    def test_investigation_can_reveal_preexisting_fire_damage(self) -> None:
        message = "伊莉雅检查西翼的火场痕迹。"
        receipt = self.service.gm_gameplay_tools.declare_check_action(
            gameplay_context(message),
            {
                "action_type": "Investigate",
                "actor": "伊莉雅",
                "target": "西翼的火场痕迹",
                "attributes": ["洞察", "洞察"],
                "difficulty": 9,
                "purpose": "判断大火的蔓延范围",
                "check_label": "检查火场痕迹",
                "success_observation": "焦痕表明整层建筑的西翼早已烧毁，火势没有越过中庭。",
                "failure_consequence": "伊莉雅这次未能判断大火的蔓延范围。",
                "failure_authority": {"kind": "attempt"},
                "evidence": "检查西翼的火场痕迹",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)

    def test_core_agent_cannot_skip_the_check_declaration_window(self) -> None:
        message = "伊莉雅观察牢门符文。"
        receipt = self.service.gm_tool_registry.execute(
            "perform_check_action",
            {
                "action_type": "Investigate",
                "actor": "伊莉雅",
                "target": "牢门符文",
                "attributes": ["洞察", "洞察"],
                "difficulty": 10,
                "purpose": "辨认牢门符文",
                "check_label": "辨认牢门符文",
                "success_observation": "符文的供能正被地下脉动切断。",
                "failure_consequence": "交叠的辉光遮住了回路走向。",
                "details": {},
            },
            gameplay_context(message),
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "CHECK_DECLARATION_REQUIRED")
        self.assertEqual(
            self.app.interceptor.decision_window_manager.pending(),
            [],
        )

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

    def test_successful_check_commits_with_hidden_invocation_right(self) -> None:
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

        resolved = self.service.gm_gameplay_tools.perform_check_action(
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

        self.assertTrue(resolved.ok, resolved.message)
        self.assertIn("车轮声正沿驿站外路向登记小室靠近。", resolved.public_fallback_reply)
        self.assertIn("【财团巡逻队逼近】1/6", resolved.public_fallback_reply)
        self.assertEqual(self.app.clock_manager.get("财团巡逻队逼近").current, 1)
        self.assertFalse(resolved.result["pending_decisions"])
        windows = self.app.interceptor.decision_window_manager.pending(
            kind="trait_invocation",
            owner="伊莉雅",
        )
        self.assertEqual(len(windows), 1)
        self.assertFalse(windows[0].blocking)
        self.assertTrue(windows[0].payload["suppress_public_prompt"])

    def test_successful_escort_check_moves_pc_and_npc_immediately(self) -> None:
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
        resolved = self.service.gm_gameplay_tools.perform_check_action(
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

        self.assertTrue(resolved.ok, resolved.message)
        self.assertFalse(resolved.result["pending_decisions"])
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

    def test_successful_conflict_exit_stays_on_parent_until_end_conflict(self) -> None:
        guard = Character(
            name="监狱守卫",
            attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 6},
            max_hp=60,
            hp=60,
            max_mp=30,
            mp=30,
            traits=["enemy"],
        )
        self.app.character_manager.add(guard)
        parent = self.app.scene_manager.current_scene
        parent.scene_type = SceneType.CONFLICT
        parent.name = "铁闸前的强行突破"
        self.app.scene_manager.add_participant("监狱守卫", location="风铃廊")
        self.app.conflict_manager.start_scene(
            parent.name,
            ["伊莉雅", "监狱守卫"],
            player_side=["伊莉雅"],
            enemy_side=["监狱守卫"],
            parent_scene_id=parent.scene_id,
            parent_scene_name="白花碑驿站",
            parent_scene_type=SceneType.STANDARD.value,
        )
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["DEX", "INS"],
                dice=[(8, 7), (10, 8)],
                total=15,
                modifier=0,
                high_roll=8,
                target_number=9,
                success=True,
                critical_success=False,
                fumble=False,
                target="铁闸",
                reason="冲出铁闸",
            )
        )
        message = "伊莉雅趁守卫失衡冲出铁闸，撤到驿站外院。"
        declared = self.service.gm_gameplay_tools.declare_movement_check(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "destination": "白花碑驿站·外院",
                "resolution_mode": "single_obstacle",
                "obstacle": "守卫与铁闸",
                "attributes": ["敏捷", "洞察"],
                "difficulty": 9,
                "purpose": "冲出铁闸脱离守卫",
                "check_label": "突破铁闸",
                "success_observation": "伊莉雅冲出铁闸，实际抵达白花碑驿站·外院。",
                "failure_consequence": "伊莉雅未能穿过铁闸，留在原地。",
                "evidence": message,
            },
        )
        window = self.app.interceptor.decision_window_manager.find_pending(
            kind="check_roll_confirmation",
            owner="伊莉雅",
        )
        self.assertTrue(declared.ok, declared.message)
        self.assertIsNotNone(window)

        rolled = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("投。"),
            {
                "action_type": "ResolveDecision",
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "roll",
                "details": {},
                "evidence": "投。",
            },
        )

        self.assertTrue(rolled.ok, rolled.message)
        self.assertIs(self.app.scene_manager.current_scene, parent)
        self.assertEqual(parent.scene_type, SceneType.CONFLICT)
        self.assertIn("伊莉雅", self.app.conflict_manager.state.escaped_combatants)
        self.assertEqual(
            self.app.scene_manager.location_of("伊莉雅"),
            "白花碑驿站·外院",
        )
        self.assertIn("end_conflict", rolled.result["required_followup_tools"])
        self.assertEqual(
            self.app.conflict_manager.state.pending_exit_transitions[0]["destination"],
            "白花碑驿站·外院",
        )
        reloaded_service = FUGMHttpService(
            data_root=self.tmpdir.name,
            use_llm=False,
        )
        reloaded = reloaded_service._runtime("gameplay-tool-test")
        self.assertEqual(
            reloaded.app.conflict_manager.state.pending_exit_transitions[0][
                "destination"
            ],
            "白花碑驿站·外院",
        )

        closing_message = "伊莉雅已经脱离守卫，结束这场冲突。"
        ended = self.service.gm_runtime_tools.end_conflict(
            gameplay_context(closing_message),
            {
                "outcome": "伊莉雅成功撤离，守卫未能追上。",
                "continue_scene": False,
                "public_reply": "铁闸在身后合拢，守卫没有追出外院。",
                "evidence": closing_message,
            },
        )

        self.assertTrue(ended.ok, ended.message)
        self.assertFalse(self.app.conflict_manager.state.active)
        self.assertEqual(
            self.app.scene_manager.current_scene.location,
            "白花碑驿站·外院",
        )
        self.assertEqual(
            ended.result["post_conflict_transitions"][0]["destination"],
            "白花碑驿站·外院",
        )
        self.assertEqual(
            self.app.conflict_manager.state.pending_exit_transitions,
            [],
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

    def test_trait_window_rejects_invented_rationale_then_accepts_literal_reason(self) -> None:
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
        rejected = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("我援用白花护送者，重掷两枚骰。"),
            {
                "action_type": "InvokeTrait",
                "actor": "伊莉雅",
                "window_id": window_id,
                "choice": "白花护送者",
                "details": {
                    "reroll_dice": 2,
                    "invocation_rationale": "作为白花护送者，伊莉雅必须看清巡逻灯影以保护同行者。",
                },
                "evidence": "援用白花护送者，重掷两枚骰",
            },
        )

        self.assertFalse(rejected.ok)
        self.assertEqual(
            rejected.error_code,
            "TRAIT_INVOCATION_RATIONALE_NOT_LITERAL",
        )
        self.assertNotIn("indices", captured)

        receipt = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context(
                "我援用白花护送者；我必须看清巡逻灯影，才能保护同行者。重掷两枚骰。"
            ),
            {
                "action_type": "InvokeTrait",
                "actor": "伊莉雅",
                "window_id": window_id,
                "choice": "白花护送者",
                "details": {
                    "reroll_dice": 2,
                    "invocation_rationale": "我必须看清巡逻灯影，才能保护同行者",
                },
                "evidence": "援用白花护送者",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertIsNone(captured["indices"])

    def _stage_failed_check_for_grace_test(self):
        hero = self.app.character_manager.get("伊莉雅")
        hero.identity = "白花护送者"
        hero.theme = "希望"
        hero.origin = "白花碑驿站"
        hero.fabula_points = 3
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["INS", "INS"],
                dice=[(10, 2), (10, 3)],
                total=5,
                modifier=0,
                high_roll=3,
                target_number=9,
                success=False,
                critical_success=False,
                fumble=False,
                target="牢门符文",
                reason="辨认牢门符文",
            )
        )
        receipt = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context("伊莉雅检查牢门符文。"),
            {
                "action_type": "Investigate",
                "actor": "伊莉雅",
                "target": "牢门符文",
                "attributes": ["洞察", "洞察"],
                "difficulty": 9,
                "purpose": "辨认牢门符文",
                "check_label": "辨认牢门符文",
                "success_observation": "符文的供能节点显露出来。",
                "failure_consequence": "交错的辉光遮住了供能节点，伊莉雅暂时没能看清。",
                "evidence": "检查牢门符文",
            },
        )
        self.assertTrue(receipt.ok, receipt.message)
        self.assertNotIn("要援用", receipt.public_fallback_reply)
        window = self.app.interceptor.decision_window_manager.find_pending(
            kind="trait_invocation",
            owner="伊莉雅",
        )
        self.assertIsNotNone(window)
        self.assertTrue(window.payload["silent_failure_grace"])
        self.assertEqual(window.payload["failure_grace_seconds"], 15)
        window.payload["failure_grace_due_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        event = MessageEvent.from_payload(
            {
                "campaign_id": "gameplay-tool-test",
                "session_id": "s1",
                "channel_id": "group-1",
                "speaker": "阿凛",
                "message": "伊莉雅检查牢门符文。",
                "message_id": "failed-check-message",
            }
        )
        followups = self.service._scheduled_rule_followups(event)
        self.assertEqual(len(followups), 1)
        return window, followups[0]

    def test_late_trait_invocation_cancels_unsent_failure_narration(self) -> None:
        window, followup = self._stage_failed_check_for_grace_test()
        heartbeat = self.service._session_heartbeat(
            {
                "campaign_id": "gameplay-tool-test",
                "session_id": "s1",
                "channel_id": "group-1",
                "activity_version": 1,
                "auto_respond": True,
                "defer_delivery_log": True,
                "rule_followup_kind": "failed_check_grace",
                "rule_followup_window_id": window.window_id,
                "rule_followup_token": followup["token"],
            }
        )
        self.assertTrue(heartbeat["send_reply"])
        self.assertTrue(heartbeat["delivery_deferred"])
        delivery_id = heartbeat["delivery_id"]
        self.assertIn("伊莉雅", self.app.interceptor.pending_check_transactions)

        self.service._record_channel_activity_version(
            {"activity_version": 2},
            campaign_id="gameplay-tool-test",
            session_id="s1",
            channel_id="group-1",
        )
        self.assertNotIn(delivery_id, self.service.pending_heartbeat_deliveries)
        reroll_outcome = RollOutcome(
                actor="伊莉雅",
                attributes=["INS", "INS"],
                dice=[(10, 8), (10, 7)],
                total=15,
                modifier=0,
                high_roll=8,
                target_number=9,
                success=True,
                critical_success=False,
                fumble=False,
                target="牢门符文",
                reason="辨认牢门符文",
            )
        with patch.object(
            self.app.interceptor.rules_engine,
            "reroll_outcome",
            return_value=reroll_outcome,
        ):
            rerolled = self.service.gm_gameplay_tools.resolve_rule_window(
                gameplay_context("我援用白花护送者：我熟悉这里的符文维护方式，重掷两枚骰。"),
                {
                    "action_type": "InvokeTrait",
                    "actor": "伊莉雅",
                    "window_id": window.window_id,
                    "choice": "白花护送者",
                    "details": {
                        "reroll_dice": 2,
                        "invocation_rationale": "我熟悉这里的符文维护方式",
                    },
                    "evidence": "我援用白花护送者：我熟悉这里的符文维护方式，重掷两枚骰。",
                },
            )
        self.assertTrue(rerolled.ok, rerolled.message)
        self.assertTrue(rerolled.result["check_receipt"]["success"])
        self.assertIn("伊莉雅", self.app.interceptor.pending_check_transactions)
        silent_rights = self.app.interceptor.decision_window_manager.pending(
            kind="trait_invocation",
            owner="伊莉雅",
        )
        self.assertEqual(len(silent_rights), 1)
        self.assertFalse(silent_rights[0].blocking)

    def test_failed_check_commits_only_after_deferred_narration_is_delivered(self) -> None:
        window, followup = self._stage_failed_check_for_grace_test()
        heartbeat = self.service._session_heartbeat(
            {
                "campaign_id": "gameplay-tool-test",
                "session_id": "s1",
                "channel_id": "group-1",
                "activity_version": 1,
                "auto_respond": True,
                "defer_delivery_log": True,
                "rule_followup_kind": "failed_check_grace",
                "rule_followup_window_id": window.window_id,
                "rule_followup_token": followup["token"],
            }
        )
        self.assertIn("伊莉雅", self.app.interceptor.pending_check_transactions)
        self.assertIsNotNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window.window_id
            )
        )

        delivered = self.service._session_heartbeat_delivered(
            {
                "campaign_id": "gameplay-tool-test",
                "session_id": "s1",
                "channel_id": "group-1",
                "delivery_id": heartbeat["delivery_id"],
            }
        )
        self.assertTrue(delivered["ok"], delivered)
        self.assertNotIn("伊莉雅", self.app.interceptor.pending_check_transactions)
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window.window_id
            )
        )

    def test_failed_attack_timeout_has_rules_safe_fallback_narration(self) -> None:
        window = type(
            "Window",
            (),
            {
                "owner": "诺艾尔",
                "payload": {
                    "source_actor": "诺艾尔",
                    "source_action": {
                        "action_type": "Attack",
                        "parameters": {
                            "actor": "诺艾尔",
                            "target": "尤尔达·灰栓",
                        },
                    },
                },
            },
        )()

        reply = self.service._failure_consequence_from_window(window)

        self.assertEqual(reply, "诺艾尔的攻击没能命中尤尔达·灰栓。")

    def test_failed_attack_without_explicit_consequence_publishes_miss(self) -> None:
        window, followup = self._stage_failed_check_for_grace_test()
        window.payload["source_action"] = {
            "action_type": "Attack",
            "parameters": {
                "actor": "伊莉雅",
                "target": "牢门符文",
            },
        }

        heartbeat = self.service._session_heartbeat(
            {
                "campaign_id": "gameplay-tool-test",
                "session_id": "s1",
                "channel_id": "group-1",
                "auto_respond": True,
                "rule_followup_kind": "failed_check_grace",
                "rule_followup_window_id": window.window_id,
                "rule_followup_token": followup["token"],
            }
        )

        self.assertTrue(heartbeat["send_reply"], heartbeat)
        self.assertEqual(heartbeat["reply"], "伊莉雅的攻击没能命中牢门符文。")
        self.assertTrue(heartbeat["state_changed"])
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window.window_id
            )
        )
        self.assertNotIn("伊莉雅", self.app.interceptor.pending_check_transactions)

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
        carried = self.app.world_state.commit_story_item_action(
            operation="acquire",
            item_name="白花路牌",
            actor="伊莉雅",
            scene_location="白花碑驿站·风铃廊",
            public_fact="白花路牌现由伊莉雅持有。",
            source="test",
        )

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
        self.assertEqual(
            self.app.world_state.story_items[carried.item_id].location,
            "白花碑驿站·登记小室",
        )
        self.assertIn(carried.item_id, receipt.result["moved_story_items"])
        self.assertEqual(carried.history[-1].operation, "carry_move")

    def test_scene_group_movement_can_continue_into_one_declared_check(self) -> None:
        persona = self.app.world_state.ensure_npc_persona(
            "失忆旅人",
            current_stance="跟随伊莉雅，不单独行动",
            active_goal="与伊莉雅一起进入登记小室",
        )
        persona.current_location = "白花碑驿站·风铃廊"
        self.app.scene_manager.set_participant_location(
            "伊莉雅",
            "白花碑驿站·风铃廊",
        )
        self.app.scene_manager.add_participant(
            "失忆旅人",
            location="白花碑驿站·风铃廊",
        )
        message = (
            "伊莉雅跟着失忆旅人进入登记小室，"
            "先检查门闩和后窗能不能从里面封住。"
        )

        receipt = self.service.gm_gameplay_tools.move_scene_group(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "companions": ["失忆旅人"],
                "destination": "白花碑驿站·登记小室",
                "action_summary": "伊莉雅与失忆旅人进入登记小室",
                "companion_positions": {"失忆旅人": "登记小室内侧"},
                "continue_with_check": True,
                "evidence": "跟着失忆旅人进入登记小室",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(
            self.app.scene_manager.location_of("伊莉雅"),
            "白花碑驿站·登记小室",
        )
        self.assertEqual(
            self.app.scene_manager.location_of("失忆旅人"),
            "白花碑驿站·登记小室",
        )
        self.assertEqual(receipt.result["action_round"], {})
        self.assertEqual(
            receipt.result["required_followup_tools"],
            ["declare_check_action"],
        )
        self.assertTrue(receipt.result["silent_commit_allowed"])
        self.assertTrue(receipt.result["source_message_already_public"])
        self.assertEqual(receipt.public_fallback_reply, "")
        self.assertFalse(receipt.lock_public_reply)

    def test_movement_rule_action_continuation_is_exposed_only_on_movement_tools(
        self,
    ) -> None:
        story_item = self.service.gm_tool_registry._tools[
            "commit_story_item_action"
        ]
        local_movement = self.service.gm_tool_registry._tools[
            "move_group_within_scene"
        ]
        scene_movement = self.service.gm_tool_registry._tools["move_scene_group"]

        self.assertNotIn(
            "continue_with_rule_action",
            {parameter.name for parameter in story_item.parameters},
        )
        self.assertIn(
            "continue_with_rule_action",
            {parameter.name for parameter in local_movement.parameters},
        )
        self.assertIn(
            "continue_with_rule_action",
            {parameter.name for parameter in scene_movement.parameters},
        )

    def test_scene_group_movement_can_continue_into_dedicated_rule_action(
        self,
    ) -> None:
        message = "伊莉雅进入旧路闸门内侧，施放元素幕障挡住外面的冲击。"

        receipt = self.service.gm_gameplay_tools.move_scene_group(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "companions": [],
                "destination": "旧路闸门内侧",
                "action_summary": "伊莉雅进入旧路闸门内侧",
                "position_note": "闸门内侧",
                "continue_with_rule_action": True,
                "evidence": "进入旧路闸门内侧",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(
            self.app.scene_manager.location_of("伊莉雅"),
            "旧路闸门内侧",
        )
        self.assertEqual(receipt.result["action_round"], {})
        self.assertEqual(
            receipt.result["required_followup_tools"],
            ["perform_character_action", "perform_ritual_project_action"],
        )
        self.assertEqual(receipt.result["required_followup_mode"], "any")
        self.assertEqual(receipt.public_fallback_reply, "")
        self.assertFalse(receipt.lock_public_reply)

    def test_local_movement_can_continue_into_dedicated_rule_action(self) -> None:
        message = "伊莉雅退到闸门边，施放元素幕障保护同伴。"

        receipt = self.service.gm_gameplay_tools.move_group_within_scene(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "companions": [],
                "destination_position": "闸门边",
                "action_summary": "伊莉雅退到闸门边",
                "continue_with_rule_action": True,
                "evidence": "退到闸门边",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(self.app.scene_manager.position_of("伊莉雅"), "闸门边")
        self.assertEqual(receipt.result["action_round"], {})
        self.assertEqual(
            receipt.result["required_followup_tools"],
            ["perform_character_action", "perform_ritual_project_action"],
        )
        self.assertEqual(receipt.result["required_followup_mode"], "any")

    def test_movement_rejects_multiple_continuation_kinds(self) -> None:
        message = "伊莉雅进入旧路闸门内侧，观察后施放元素幕障。"

        receipt = self.service.gm_gameplay_tools.move_scene_group(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "companions": [],
                "destination": "旧路闸门内侧",
                "action_summary": "伊莉雅进入旧路闸门内侧",
                "continue_with_check": True,
                "continue_with_rule_action": True,
                "evidence": "进入旧路闸门内侧",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "MULTIPLE_MOVEMENT_CONTINUATIONS")
        self.assertEqual(
            self.app.scene_manager.location_of("伊莉雅"),
            "风铃廊",
        )

    def test_scene_group_movement_rejects_check_and_npc_followup_together(self) -> None:
        persona = self.app.world_state.ensure_npc_persona("失忆旅人")
        persona.current_location = "白花碑驿站·登记小室"
        self.app.scene_manager.actor_locations[persona.name] = persona.current_location
        message = "伊莉雅进入登记小室，检查门闩，并问旅人是否听见脚步声。"

        receipt = self.service.gm_gameplay_tools.move_scene_group(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "companions": [],
                "destination": "白花碑驿站·登记小室",
                "action_summary": "伊莉雅进入登记小室",
                "followup_npc_name": "失忆旅人",
                "followup_response_instruction": "回答是否听见脚步声",
                "continue_with_check": True,
                "evidence": "进入登记小室，检查门闩，并问旅人是否听见脚步声",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "AMBIGUOUS_MOVEMENT_CONTINUATION")

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

    def test_scene_group_movement_replaces_source_frame_with_destination_frame(self) -> None:
        source = self.app.scene_manager.current_scene
        source_frame = self.app.scene_frame_manager.ensure_frame(
            scene=source,
            recent_chat="众人仍在风铃廊里商量旧路。",
            world_state=self.app.world_state,
            character_manager=self.app.character_manager,
        )

        receipt = self.service.gm_gameplay_tools.move_scene_group(
            gameplay_context("伊莉雅独自前往旧路闸门。"),
            {
                "actor": "伊莉雅",
                "companions": [],
                "destination": "旧路闸门",
                "action_summary": "伊莉雅独自前往旧路闸门",
                "public_result": "伊莉雅抵达旧路闸门。",
                "evidence": "伊莉雅独自前往旧路闸门",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        destination = self.app.scene_manager.current_scene
        current_frame = self.app.scene_frame_manager.current_frame
        self.assertIsNotNone(current_frame)
        self.assertEqual(current_frame.source_scene_id, destination.scene_id)
        self.assertIn(
            source_frame.source_scene_id,
            self.app.scene_frame_manager.suspended_frames,
        )

    def test_scene_group_movement_rejects_public_result_that_moves_another_pc(self) -> None:
        original = self.app.scene_manager.current_scene
        receipt = self.service.gm_gameplay_tools.move_scene_group(
            gameplay_context("伊莉雅独自前往旧路闸门。"),
            {
                "actor": "伊莉雅",
                "companions": [],
                "destination": "旧路闸门",
                "action_summary": "伊莉雅独自前往旧路闸门",
                "public_result": "伊莉雅抵达旧路闸门，洛岚已经在门边等候。",
                "evidence": "伊莉雅独自前往旧路闸门",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "PUBLIC_MOVEMENT_ACTOR_NOT_PRESENT")
        self.assertIs(self.app.scene_manager.current_scene, original)
        self.assertEqual(self.app.scene_manager.location_of("伊莉雅"), "风铃廊")

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
        self.assertTrue(receipt.result["silent_commit_allowed"])

    def test_local_movement_allows_actor_only_and_keeps_npc_in_place(self) -> None:
        persona = self.app.world_state.ensure_npc_persona(
            "失忆旅人",
            current_stance="留在风铃廊等候",
            active_goal="听从伊莉雅从门内传来的提醒",
        )
        scene = self.app.scene_manager.current_scene
        persona.current_location = str(scene.location or scene.name)
        self.app.scene_manager.add_participant(
            "失忆旅人",
            location=str(scene.location or scene.name),
        )
        self.app.scene_manager.set_participant_position("伊莉雅", "风铃廊")
        self.app.scene_manager.set_participant_position("失忆旅人", "风铃廊")
        message = (
            "伊莉雅独自进入登记小室，隔着门提醒仍在风铃廊的失忆旅人不要回应呼喊。"
        )

        receipt = self.service.gm_gameplay_tools.move_group_within_scene(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "companions": [],
                "destination_position": "登记小室内",
                "action_summary": "伊莉雅独自进入登记小室",
                "public_result": "",
                "continue_with_check": True,
                "evidence": "独自进入登记小室",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(self.app.scene_manager.position_of("伊莉雅"), "登记小室内")
        self.assertEqual(self.app.scene_manager.position_of("失忆旅人"), "风铃廊")
        self.assertEqual(receipt.result["companions"], [])
        self.assertEqual(receipt.result["action_round"], {})
        self.assertEqual(
            receipt.result["required_followup_tools"],
            ["declare_check_action"],
        )
        self.assertEqual(receipt.public_fallback_reply, "")

    def test_local_group_movement_can_continue_into_check_without_consuming_round(self) -> None:
        persona = self.app.world_state.ensure_npc_persona(
            "失忆旅人",
            current_stance="愿意跟随伊莉雅进入登记小室",
            active_goal="避开外面的追索",
        )
        scene = self.app.scene_manager.current_scene
        persona.current_location = str(scene.location or scene.name)
        self.app.scene_manager.add_participant(
            "失忆旅人",
            location=str(scene.location or scene.name),
        )
        message = (
            "伊莉雅跟着失忆旅人进登记小室，先检查门闩和后窗能不能从里面封住。"
        )

        receipt = self.service.gm_gameplay_tools.move_group_within_scene(
            gameplay_context(message),
            {
                "actor": "伊莉雅",
                "companions": ["失忆旅人"],
                "destination_position": "登记小室内",
                "action_summary": "伊莉雅跟随失忆旅人进入登记小室",
                "public_result": "",
                "continue_with_check": True,
                "evidence": "跟着失忆旅人进登记小室",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(self.app.scene_manager.position_of("伊莉雅"), "登记小室内")
        self.assertEqual(
            self.app.scene_manager.position_of("失忆旅人"),
            "登记小室内",
        )
        self.assertEqual(receipt.result["action_round"], {})
        self.assertEqual(
            receipt.result["required_followup_tools"],
            ["declare_check_action"],
        )
        self.assertEqual(receipt.result["required_followup_calls"], [])
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
        self.assertFalse(receipt.result["silent_commit_allowed"])
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

    def test_non_objective_check_cannot_carry_clock_side_effect(self) -> None:
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

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "CLOCK_CHANGE_ONLY_FOR_OBJECTIVE")

    def test_hinder_status_matches_the_committed_success_observation(self) -> None:
        self.app.character_manager.add(
            Character(
                name="监察官艾蕾娜",
                attributes={"DEX": 8, "INS": 10, "MIG": 6, "WLP": 10},
                max_hp=80,
                hp=80,
                max_mp=80,
                mp=80,
                traits=["enemy", "villain"],
            )
        )
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["INS", "WLP"],
                dice=[(10, 6), (6, 5)],
                total=11,
                modifier=0,
                high_roll=6,
                target_number=9,
                success=True,
                critical_success=False,
                fumble=False,
                margin=2,
                reason="动摇监察官",
            )
        )

        receipt = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context("伊莉雅指出艾蕾娜命令里的矛盾，试图令她动摇。"),
            {
                "action_type": "Hinder",
                "actor": "伊莉雅",
                "target": "监察官艾蕾娜",
                "attributes": ["洞察", "意志"],
                "difficulty": 9,
                "purpose": "令监察官艾蕾娜动摇",
                "check_label": "揭穿命令矛盾",
                "success_observation": "监察官艾蕾娜被施加了动摇。",
                "failure_consequence": "艾蕾娜驳回质疑，守卫重新稳住阵线。",
                "evidence": "指出艾蕾娜命令里的矛盾",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        target = self.app.character_manager.get("监察官艾蕾娜")
        self.assertIn(StatusEffect.SHAKEN, target.statuses)
        self.assertNotIn(StatusEffect.DAZED, target.statuses)
        self.assertNotIn("眩晕", receipt.public_fallback_reply)

    def test_hinder_rejects_status_that_contradicts_success_observation(self) -> None:
        self.app.character_manager.add(
            Character(
                name="监察官艾蕾娜",
                attributes={"DEX": 8, "INS": 10, "MIG": 6, "WLP": 10},
                max_hp=80,
                hp=80,
                max_mp=80,
                mp=80,
                traits=["enemy", "villain"],
            )
        )

        receipt = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context("伊莉雅指出艾蕾娜命令里的矛盾，试图令她动摇。"),
            {
                "action_type": "Hinder",
                "actor": "伊莉雅",
                "target": "监察官艾蕾娜",
                "attributes": ["洞察", "意志"],
                "difficulty": 9,
                "purpose": "令监察官艾蕾娜动摇",
                "check_label": "揭穿命令矛盾",
                "status_effect": "眩晕",
                "success_observation": "监察官艾蕾娜被施加了动摇。",
                "failure_consequence": "艾蕾娜驳回质疑，守卫重新稳住阵线。",
                "evidence": "指出艾蕾娜命令里的矛盾",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "HINDER_STATUS_CONTRADICTION")

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

    def test_authoritative_rule_action_uses_canonical_text_when_llm_expression_fails(self) -> None:
        class FailingExpression:
            def __init__(self) -> None:
                self.fallback = Expressor()
                self.last_used_fallback = False

            def render(self, _resolution):
                raise RuntimeError("模拟表达供应商拒绝")

        self.app.expressor = FailingExpression()
        self.app.conflict_manager.start_scene("风铃廊冲突", ["伊莉雅", "洛岚"])

        receipt = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context("伊莉雅举盾防御。"),
            {
                "action_type": "Guard",
                "actor": "伊莉雅",
                "details": {},
                "evidence": "伊莉雅举盾防御",
            },
        )

        self.assertTrue(receipt.ok)
        self.assertTrue(receipt.state_changed)
        self.assertTrue(receipt.public_fallback_reply)
        self.assertTrue(self.app.expressor.last_used_fallback)
        self.assertTrue(self.app.recent_pipeline_spans[-1]["expression_degraded"])
        self.assertIn(
            "模拟表达供应商拒绝",
            self.app.recent_pipeline_spans[-1]["expressor_error"],
        )

    def test_failed_rule_action_restores_orchestrator_ephemeral_state(self) -> None:
        self.app.conflict_manager.start_scene("风铃廊冲突", ["伊莉雅", "洛岚"])
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

    def test_objective_named_in_message_cannot_degrade_after_wrong_target_fallback(self) -> None:
        self.app.clock_manager.add(Clock(name="旧路闸门开启", max_segments=6))
        message = "洛岚推进目标命刻【旧路闸门开启】，拆开机兵足架。"

        receipt = self.service.gm_gameplay_tools.declare_check_action(
            gameplay_context(message, speaker="白河"),
            {
                "action_type": "Objective",
                "actor": "洛岚",
                "target": "机兵足架",
                "attributes": ["洞察", "敏捷"],
                "difficulty": 10,
                "purpose": "拆开封锁",
                "check_label": "拆开机兵足架",
                "success_observation": "足架的锁栓被卸下一枚。",
                "failure_consequence": "足架自锁，封锁更加牢固。",
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "OBJECTIVE_CLOCK_NOT_FOUND")
        self.assertIn("不要降级成RequestRoll", receipt.correction_hint)
        self.assertEqual(
            receipt.result["suggested_clock_name"],
            "旧路闸门开启",
        )

    def test_objective_schema_exposes_clock_name_separately_from_target(self) -> None:
        schema = next(
            item
            for item in self.service.gm_tool_registry.schemas()
            if item["name"] == "declare_check_action"
        )
        properties = schema["parameters"]["properties"]

        self.assertIn("clock_name", properties)
        self.assertIn("clock_direction", properties)
        self.assertIn("现有命刻名称", properties["clock_name"]["description"])
        self.assertIn("实际检定对象", properties["target"]["description"])

    def test_check_schemas_expose_typed_open_check_flag(self) -> None:
        schemas = {
            item["name"]: item
            for item in self.service.gm_tool_registry.schemas()
        }

        for tool_name in ("declare_check_action", "perform_check_action"):
            with self.subTest(tool_name=tool_name):
                open_check = schemas[tool_name]["parameters"]["properties"][
                    "open_check"
                ]
                self.assertEqual(open_check["type"], "boolean")

    def test_objective_requires_explicit_fill_or_erase_direction(self) -> None:
        self.app.clock_manager.add(
            Clock(name="财团封锁协议", max_segments=6, current=3, clock_type="villain")
        )
        message = "伊莉雅切断信号回路，想阻止财团封锁协议。"

        receipt = self.service.gm_gameplay_tools.declare_check_action(
            gameplay_context(message),
            {
                "action_type": "Objective",
                "actor": "伊莉雅",
                "target": "信号回路",
                "clock_name": "财团封锁协议",
                "attributes": ["洞察", "敏捷"],
                "difficulty": 10,
                "purpose": "阻止财团封锁协议",
                "check_label": "切断信号回路",
                "success_observation": "信号回路的一枚指示灯熄灭。",
                "failure_consequence": "信号回路保持闭合。",
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "OBJECTIVE_CLOCK_DIRECTION_REQUIRED",
        )

    def test_objective_success_erases_villain_clock_when_gm_declares_erase(self) -> None:
        self.app.clock_manager.add(
            Clock(name="财团封锁协议", max_segments=6, current=3, clock_type="villain")
        )
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["INS", "DEX"],
                dice=[(10, 6), (8, 5)],
                total=11,
                modifier=0,
                high_roll=6,
                target_number=10,
                success=True,
                critical_success=False,
                fumble=False,
                target="信号回路",
                reason="阻止财团封锁协议",
            )
        )
        message = "伊莉雅切断信号回路，阻止财团封锁协议。"

        receipt = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context(message),
            {
                "action_type": "Objective",
                "actor": "伊莉雅",
                "target": "信号回路",
                "clock_name": "财团封锁协议",
                "clock_direction": "擦除",
                "attributes": ["洞察", "敏捷"],
                "difficulty": 10,
                "purpose": "阻止财团封锁协议",
                "check_label": "切断信号回路",
                "success_observation": "信号回路的一枚指示灯熄灭。",
                "failure_consequence": "信号回路保持闭合。",
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(self.app.clock_manager.get("财团封锁协议").current, 2)
        self.assertEqual(
            receipt.result["committed_action"]["clock_direction"],
            -1,
        )

    def test_objective_can_only_advance_the_named_existing_clock(self) -> None:
        self.app.clock_manager.add(Clock(name="开启旧路闸门", max_segments=6))
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["INS", "DEX"],
                dice=[(10, 5), (8, 4)],
                total=9,
                modifier=0,
                high_roll=5,
                target_number=7,
                success=True,
                critical_success=False,
                fumble=False,
                target="旧路闸门横梁",
                reason="推进开启旧路闸门",
            )
        )
        message = "伊莉雅继续处理开启旧路闸门的横梁。"
        receipt = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context(message),
            {
                "action_type": "Objective",
                "actor": "伊莉雅",
                "target": "旧路闸门横梁",
                "clock_name": "开启旧路闸门",
                "clock_direction": "填充",
                "attributes": ["洞察", "敏捷"],
                "difficulty": 7,
                "purpose": "推进开启旧路闸门",
                "check_label": "调整闸门横梁",
                "success_observation": "横梁的锁舌被拨回一段。",
                "failure_consequence": "锁舌卡在锈蚀槽里，这次没有移动。",
                "evidence": "伊莉雅继续处理开启旧路闸门的横梁",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertIn("调整闸门横梁", receipt.public_fallback_reply)
        self.assertNotIn("对 旧路闸门横梁 的检定", receipt.public_fallback_reply)
        self.assertGreaterEqual(self.app.clock_manager.get("开启旧路闸门").current, 1)

    def test_objective_declaration_rejects_model_authored_clock_delta(self) -> None:
        self.app.clock_manager.add(Clock(name="争取守望会信任", max_segments=4))
        message = "伊莉雅拿出证据争取守望会信任。"

        receipt = self.service.gm_gameplay_tools.declare_check_action(
            gameplay_context(message, speaker="阿凛"),
            {
                "action_type": "Objective",
                "actor": "伊莉雅",
                "target": "白花守望会",
                "attributes": ["洞察", "意志"],
                "difficulty": 10,
                "purpose": "以证据争取守望会信任",
                "check_label": "争取守望会信任",
                "success_observation": (
                    "会长认可证据；【争取守望会信任】推进一格。"
                ),
                "failure_consequence": "会长拒绝开放旧路。",
                "details": {"clock_name": "争取守望会信任"},
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "OBJECTIVE_SUCCESS_CLAIMS_CLOCK_DELTA",
        )
        self.assertEqual(self.app.clock_manager.get("争取守望会信任").current, 0)

    def test_objective_resolution_rejects_model_authored_clock_delta(self) -> None:
        self.app.clock_manager.add(Clock(name="争取守望会信任", max_segments=4))
        message = "伊莉雅拿出证据争取守望会信任。"

        receipt = self.service.gm_gameplay_tools.perform_check_action(
            gameplay_context(message, speaker="阿凛"),
            {
                "action_type": "Objective",
                "actor": "伊莉雅",
                "target": "白花守望会",
                "attributes": ["洞察", "意志"],
                "difficulty": 10,
                "purpose": "以证据争取守望会信任",
                "check_label": "争取守望会信任",
                "success_observation": "守望会的态度软化，命刻填充3格。",
                "failure_consequence": "会长拒绝开放旧路。",
                "details": {"clock_name": "争取守望会信任"},
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "OBJECTIVE_SUCCESS_CLAIMS_CLOCK_DELTA",
        )
        self.assertEqual(self.app.clock_manager.get("争取守望会信任").current, 0)

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

    def test_attack_cannot_replace_explicit_enemy_with_its_collective(self) -> None:
        for name, traits in (
            ("财团机兵", ["enemy", "construct"]),
            ("辉钢财团巡逻队", ["enemy"]),
        ):
            self.app.character_manager.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                    max_hp=40,
                    hp=40,
                    max_mp=30,
                    mp=30,
                    traits=traits,
                )
            )
        self.app.conflict_manager.start_scene(
            "风铃廊冲突",
            ["伊莉雅", "辉钢财团巡逻队"],
            player_side=["伊莉雅"],
            enemy_side=["辉钢财团巡逻队"],
        )
        message = "伊莉雅使用钢匕首近战攻击财团机兵。"

        receipt = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(message),
            {
                "action_type": "Attack",
                "actor": "伊莉雅",
                "target": "辉钢财团巡逻队",
                "timing": "immediate",
                "details": {"weapon": "钢匕首"},
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "ACTION_TARGET_CONTRADICTS_PLAYER_INTENT",
        )
        self.assertEqual(receipt.result["explicit_targets"], ["财团机兵"])

    def test_explicit_basic_attack_cannot_be_upgraded_to_skill(self) -> None:
        self.app.character_manager.add(
            Character(
                name="训练傀儡",
                attributes={"DEX": 6, "INS": 6, "MIG": 6, "WLP": 6},
                max_hp=40,
                hp=40,
                max_mp=20,
                mp=20,
                defenses={"physical": 10, "magic": 8},
                traits=["enemy", "construct"],
            )
        )
        actor = self.app.character_manager.get("伊莉雅")
        actor.skills["利刃风暴"] = 1
        self.app.conflict_manager.start_scene(
            "训练冲突",
            ["伊莉雅", "训练傀儡"],
            player_side=["伊莉雅"],
            enemy_side=["训练傀儡"],
        )
        hp_before = self.app.character_manager.get("训练傀儡").hp
        mp_before = actor.mp
        message = "伊莉雅用匕首普通攻击训练傀儡，按真实骰结算。"

        receipt = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(message),
            {
                "action_type": "Skill",
                "actor": "伊莉雅",
                "target": "训练傀儡",
                "timing": "immediate",
                "details": {
                    "skill_name": "利刃风暴",
                    "targets": ["训练傀儡"],
                },
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "ACTION_KIND_CONTRADICTS_PLAYER_INTENT",
        )
        self.assertEqual(receipt.result["expected_action_type"], "Attack")
        self.assertEqual(receipt.result["submitted_action_type"], "Skill")
        self.assertEqual(self.app.character_manager.get("训练傀儡").hp, hp_before)
        self.assertEqual(actor.mp, mp_before)
        self.assertEqual(self.app.conflict_manager.state.turn_serial, 0)

        disguised = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(message),
            {
                "action_type": "Attack",
                "actor": "伊莉雅",
                "target": "训练傀儡",
                "timing": "immediate",
                "details": {
                    "skill_name": "利刃风暴",
                    "targets": ["训练傀儡"],
                },
                "evidence": message,
            },
        )
        self.assertFalse(disguised.ok)
        self.assertEqual(
            disguised.error_code,
            "ACTION_KIND_CONTRADICTS_PLAYER_INTENT",
        )
        self.assertEqual(disguised.result["forbidden_mode_fields"], ["skill_name"])
        self.assertEqual(self.app.character_manager.get("训练傀儡").hp, hp_before)
        self.assertEqual(actor.mp, mp_before)

    def test_explicit_single_target_basic_attack_cannot_add_an_enemy(self) -> None:
        for name in ("甲傀儡", "乙傀儡"):
            self.app.character_manager.add(
                Character(
                    name=name,
                    attributes={"DEX": 6, "INS": 6, "MIG": 6, "WLP": 6},
                    max_hp=30,
                    hp=30,
                    max_mp=10,
                    mp=10,
                    defenses={"physical": 10, "magic": 8},
                    traits=["enemy", "construct"],
                )
            )
        self.app.conflict_manager.start_scene(
            "双目标训练",
            ["伊莉雅", "甲傀儡", "乙傀儡"],
            player_side=["伊莉雅"],
            enemy_side=["甲傀儡", "乙傀儡"],
        )
        message = "伊莉雅普通攻击甲傀儡。"

        receipt = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(message),
            {
                "action_type": "Attack",
                "actor": "伊莉雅",
                "target": "甲傀儡",
                "timing": "immediate",
                "details": {"targets": ["甲傀儡", "乙傀儡"]},
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "ACTION_TARGET_CONTRADICTS_PLAYER_INTENT",
        )
        self.assertEqual(receipt.result["explicit_targets"], ["甲傀儡"])
        self.assertEqual(receipt.result["unexpected_targets"], ["乙傀儡"])
        self.assertEqual(self.app.character_manager.get("甲傀儡").hp, 30)
        self.assertEqual(self.app.character_manager.get("乙傀儡").hp, 30)

    def test_negated_basic_attack_phrase_does_not_force_attack_kind(self) -> None:
        policy = self.service.gm_gameplay_tools._explicit_basic_attack_requested

        self.assertFalse(policy("这不是普通攻击，我明确发动利刃风暴。"))
        self.assertFalse(policy("不要再用基础攻击，改用法术。"))
        self.assertTrue(policy("诺艾尔用双盾普通攻击赤炉大将。"))

    def test_out_of_turn_action_requires_current_npc_turn_to_finish(self) -> None:
        self.app.character_manager.add(
            Character(
                name="监察官艾蕾娜",
                attributes={"DEX": 8, "INS": 10, "MIG": 8, "WLP": 10},
                max_hp=80,
                hp=80,
                max_mp=60,
                mp=60,
                traits=["enemy", "villain"],
            )
        )
        self.app.conflict_manager.start_scene(
            "风铃廊冲突",
            ["监察官艾蕾娜", "伊莉雅", "洛岚"],
        )

        receipt = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context("洛岚轮到自己时举起符文盾防御。", speaker="白河"),
            {
                "action_type": "Guard",
                "actor": "洛岚",
                "timing": "defer",
                "details": {},
                "evidence": "洛岚轮到自己时举起符文盾防御",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(
            self.app.conflict_manager.state.current_actor(),
            "监察官艾蕾娜",
        )
        self.assertEqual(receipt.result["required_followup_tools"], ["run_current_npc_turn"])
        self.assertEqual(
            receipt.result["required_followup_calls"][0]["arguments"],
            {"expected_actor": "监察官艾蕾娜"},
        )
        self.assertEqual(receipt.result["required_followup_mode"], "all")

    def test_out_of_turn_check_is_deferred_and_requires_current_npc_turn(self) -> None:
        self.app.character_manager.add(
            Character(
                name="监察官艾蕾娜",
                attributes={"DEX": 8, "INS": 10, "MIG": 8, "WLP": 10},
                max_hp=80,
                hp=80,
                max_mp=60,
                mp=60,
                traits=["enemy", "villain"],
            )
        )
        self.app.conflict_manager.start_scene(
            "风铃廊冲突",
            ["监察官艾蕾娜", "伊莉雅", "洛岚"],
        )

        receipt = self.service.gm_gameplay_tools.declare_check_action(
            gameplay_context("洛岚敲击机兵腿部联轴，想让它迟缓。", speaker="白河"),
            {
                "action_type": "Hinder",
                "actor": "洛岚",
                "target": "机兵腿部联轴",
                "attributes": ["力量", "洞察"],
                "difficulty": 10,
                "purpose": "令机兵迟缓",
                "check_label": "敲击腿部联轴",
                "base_observation": "联轴正从装甲接缝间转过。",
                "success_observation": "铁锤卡住联轴，机兵的步伐慢了下来。",
                "risk_hint": "装甲接缝正在闭合。",
                "failure_consequence": "铁锤被弹开，洛岚暴露在机兵面前。",
                "evidence": "洛岚敲击机兵腿部联轴",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        held = self.app.conflict_manager.held_actions_for_actor("洛岚")
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["action_type"], "Hinder")
        self.assertEqual(held[0]["action_parameters"]["_turn_timing"], "defer")
        self.assertEqual(receipt.result["required_followup_tools"], ["run_current_npc_turn"])

    def test_valid_out_of_turn_assist_does_not_end_current_actor_turn(self) -> None:
        self.app.conflict_manager.start_scene("风铃廊冲突", ["伊莉雅", "洛岚"])
        action = Action(
            ActionType.ASSIST,
            {
                "actor": "洛岚",
                "assist_target": "伊莉雅",
                "_enforce_turn_order": True,
            },
        )

        resolution = self.app.interceptor.resolve(action)
        self.app._auto_advance_conflict_turn(action, resolution)

        self.assertTrue(resolution.payload["team_assist_registered"])
        self.assertTrue(resolution.payload["out_of_turn"])
        self.assertEqual(self.app.conflict_manager.state.current_actor(), "伊莉雅")
        self.assertIn("洛岚", self.app.conflict_manager.state.acted_this_round)

    def test_perform_character_action_registers_explicit_out_of_turn_assist(self) -> None:
        self.app.conflict_manager.start_scene("风铃廊冲突", ["伊莉雅", "洛岚"])

        receipt = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(
                "洛岚用盾沿卡住齿轮，协助当前行动的伊莉雅完成检定。",
                speaker="白河",
            ),
            {
                "action_type": "Assist",
                "actor": "洛岚",
                "target": "伊莉雅",
                "details": {
                    "assist_target": "伊莉雅",
                    "reasoning": "用盾沿卡住齿轮，为伊莉雅创造稳定发力点。",
                },
                "evidence": "洛岚用盾沿卡住齿轮，协助当前行动的伊莉雅完成检定",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(self.app.conflict_manager.state.current_actor(), "伊莉雅")
        self.assertIn("洛岚", self.app.conflict_manager.state.acted_this_round)
        self.assertIn(
            "洛岚",
            self.app.conflict_manager.state.pending_assists.get("伊莉雅", []),
        )
        self.assertIn("团队合作", receipt.public_fallback_reply)

    def test_named_turn_start_clock_ticks_as_timeline_phase_not_combatant(self) -> None:
        self.app.clock_manager.add(
            Clock(
                name="灰飞烟灭",
                max_segments=6,
                clock_type="boss",
                auto_advance="每次【伊莉雅】回合开始时推进1格",
                auto_advance_timing="owner_turn_start",
                auto_advance_owner="伊莉雅",
                scope="scene",
            )
        )
        self.app.conflict_manager.start_scene("风铃廊冲突", ["伊莉雅", "洛岚"])
        action = Action(ActionType.GUARD, {"actor": "伊莉雅"})

        resolution = self.app.interceptor.resolve(action)
        self.app._auto_advance_conflict_turn(action, resolution)

        self.assertEqual(self.app.clock_manager.get("灰飞烟灭").current, 1)
        self.assertNotIn("灰飞烟灭", self.app.conflict_manager.state.turn_order)
        phases = list(resolution.payload.get("timeline_phases") or [])
        self.assertTrue(
            any(
                phase.get("timing") == "owner_turn_start"
                and phase.get("clock_names") == ["灰飞烟灭"]
                for phase in phases
            )
        )

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

    def test_ritual_plan_without_name_returns_typed_retry_error(self) -> None:
        self.app.character_manager.get("伊莉雅").skills["元素系仪式"] = 1

        receipt = self.service.gm_gameplay_tools.perform_ritual_project_action(
            gameplay_context("伊莉雅尝试用元素仪式稳定封印。"),
            {
                "action_type": "PlanRitual",
                "actor": "伊莉雅",
                "details": {
                    "discipline": "elementalism",
                    "potency": "minor",
                    "scope": "individual",
                    "effect": "稳定眼前的封印",
                },
                "evidence": "尝试用元素仪式稳定封印",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "RITUAL_NAME_REQUIRED")
        self.assertNotEqual(receipt.error_code, "RULE_ACTION_REJECTED")

    def test_ritual_plan_without_effect_returns_typed_retry_error(self) -> None:
        self.app.character_manager.get("伊莉雅").skills["元素系仪式"] = 1

        receipt = self.service.gm_gameplay_tools.perform_ritual_project_action(
            gameplay_context("伊莉雅尝试用元素仪式稳定封印。"),
            {
                "action_type": "PlanRitual",
                "actor": "伊莉雅",
                "details": {
                    "name": "稳定封印",
                    "discipline": "elementalism",
                    "potency": "minor",
                    "scope": "individual",
                    "effect": "",
                },
                "evidence": "尝试用元素仪式稳定封印",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "RITUAL_EFFECT_REQUIRED")
        self.assertNotEqual(receipt.error_code, "RULE_ACTION_REJECTED")

    def test_non_conflict_ritual_cast_without_effect_returns_typed_retry_error(self) -> None:
        self.app.character_manager.get("伊莉雅").skills["元素系仪式"] = 1

        receipt = self.service.gm_gameplay_tools.perform_ritual_project_action(
            gameplay_context("伊莉雅施展元素仪式稳定封印。"),
            {
                "action_type": "CastRitual",
                "actor": "伊莉雅",
                "details": {
                    "name": "稳定封印",
                    "discipline": "elementalism",
                    "potency": "minor",
                    "scope": "individual",
                },
                "evidence": "施展元素仪式稳定封印",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "RITUAL_EFFECT_REQUIRED")
        self.assertNotEqual(receipt.error_code, "RULE_ACTION_REJECTED")

    def test_agent_ritual_roll_requires_a_declared_failure_consequence(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.skills["元素系仪式"] = 1
        context = gameplay_context("伊莉雅用元素系仪式稳定失控封印。")
        context.metadata["gm_dynamic_capabilities_enabled"] = True

        receipt = self.service.gm_gameplay_tools.perform_ritual_project_action(
            context,
            {
                "action_type": "CastRitual",
                "actor": "伊莉雅",
                "details": {
                    "name": "稳定失控封印",
                    "discipline": "elementalism",
                    "potency": "minor",
                    "scope": "individual",
                    "effect": "让封印停止抽取囚犯的灵魂残留",
                },
                "evidence": "伊莉雅用元素系仪式稳定失控封印",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "RITUAL_FAILURE_CONSEQUENCE_REQUIRED",
        )
        self.assertEqual(hero.mp, hero.max_mp)
        self.assertFalse(
            self.app.interceptor.decision_window_manager.pending(
                kind="check_roll_confirmation"
            )
        )

    def test_agent_ritual_hides_failure_until_timeout_closes_it(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.skills["元素系仪式"] = 1
        hero.identity = "白花护送者"
        hero.theme = "希望"
        hero.origin = "白花碑驿站"
        hero.fabula_points = 3
        context = gameplay_context("伊莉雅用元素系仪式稳定失控封印。")
        context.metadata["gm_dynamic_capabilities_enabled"] = True
        failure = "封印脉动提前收紧，灵魂残留的稳定窗口被压缩。"
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["INS", "WLP"],
                dice=[(10, 3), (6, 2)],
                total=5,
                modifier=0,
                high_roll=3,
                target_number=7,
                success=False,
                critical_success=False,
                fumble=False,
                margin=-2,
                reason="仪式检定：稳定失控封印",
            )
        )

        declared = self.service.gm_gameplay_tools.perform_ritual_project_action(
            context,
            {
                "action_type": "CastRitual",
                "actor": "伊莉雅",
                "details": {
                    "name": "稳定失控封印",
                    "discipline": "elementalism",
                    "potency": "minor",
                    "scope": "individual",
                    "effect": "让封印停止抽取囚犯的灵魂残留",
                    "failure_consequence": failure,
                },
                "evidence": "伊莉雅用元素系仪式稳定失控封印",
            },
        )

        self.assertTrue(declared.ok, declared.message)
        self.assertIn("【洞察+意志】仪式检定", declared.public_fallback_reply)
        self.assertNotIn(failure, declared.public_fallback_reply)
        self.assertEqual(hero.mp, hero.max_mp)
        confirmation = self.app.interceptor.decision_window_manager.find_pending(
            kind="check_roll_confirmation",
            owner="伊莉雅",
        )
        self.assertIsNotNone(confirmation)

        roll_context = gameplay_context("投。")
        roll_context.metadata["gm_dynamic_capabilities_enabled"] = True
        rolled = self.service.gm_gameplay_tools.resolve_rule_window(
            roll_context,
            {
                "action_type": "ResolveDecision",
                "actor": "伊莉雅",
                "window_id": confirmation.window_id,
                "choice": "roll",
                "details": {},
                "evidence": "投。",
            },
        )

        self.assertTrue(rolled.ok, rolled.message)
        self.assertIn("失败", rolled.public_fallback_reply)
        pending = self.app.interceptor.decision_window_manager.find_pending(
            kind="trait_invocation",
            owner="伊莉雅",
        )
        self.assertIsNotNone(pending)
        self.assertEqual(
            self.service._failure_consequence_from_window(pending),
            failure,
        )
        pending.payload["failure_grace_due_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        heartbeat = self.service._session_heartbeat(
            {
                "campaign_id": "gameplay-tool-test",
                "session_id": "s1",
                "channel_id": "group-1",
                "auto_respond": True,
                "force": True,
                "rule_followup_kind": "failed_check_grace",
                "rule_followup_window_id": pending.window_id,
                "rule_followup_token": pending.payload["failure_grace_token"],
            }
        )

        self.assertTrue(heartbeat["send_reply"], heartbeat)
        self.assertEqual(heartbeat["reply"], failure)
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=pending.window_id
            )
        )

    def test_failed_ritual_timeout_has_rules_safe_legacy_fallback(self) -> None:
        window = type(
            "Window",
            (),
            {
                "owner": "艾丽妮",
                "payload": {
                    "source_actor": "艾丽妮",
                    "source_action": {
                        "action_type": "CastRitual",
                        "parameters": {
                            "actor": "艾丽妮",
                            "name": "稳定失控封印",
                            "effect": "让灵魂残留不再被抽取",
                        },
                    },
                },
            },
        )()

        reply = self.service._failure_consequence_from_window(window)

        self.assertIn("艾丽妮", reply)
        self.assertIn("仪式原本要产生的效果没有发生", reply)

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

    def test_character_action_rejects_forged_consumed_teamwork_turns(self) -> None:
        message = "伊莉雅挥剑攻击巡逻守卫。"
        receipt = self.service.gm_gameplay_tools.perform_character_action(
            gameplay_context(message),
            {
                "action_type": "Attack",
                "actor": "伊莉雅",
                "target": "巡逻守卫",
                "details": {
                    "teamwork_turns_already_consumed": ["不存在的支援者"],
                },
                "evidence": "伊莉雅挥剑攻击巡逻守卫",
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

    def test_opportunity_accept_result_normalizes_to_typed_decline(self) -> None:
        message = "伊莉雅接受当前结果，立即放弃本次机会，不保留到稍后。"

        for submitted_action_type in ("ResolveDecision", "TriggerOpportunity"):
            with self.subTest(action_type=submitted_action_type):
                window = self.app.interceptor.decision_window_manager.create(
                    kind="critical_opportunity",
                    owner="伊莉雅",
                    prompt="选择大成功机会，或立即放弃。",
                    # A saved pre-upgrade window has no explicit decline option.
                    # Both failure shapes observed in the live artifact must
                    # still close it without inventing a custom no-op effect.
                    options=[{"effect": "优势", "requires": ["target"]}],
                    blocking=True,
                    action_type="TriggerOpportunity",
                )
                receipt = self.service.gm_gameplay_tools.resolve_rule_window(
                    gameplay_context(message),
                    {
                        "action_type": submitted_action_type,
                        "actor": "伊莉雅",
                        "window_id": window.window_id,
                        "choice": "accept_result",
                        "details": {},
                        "evidence": "立即放弃本次机会",
                    },
                )

                self.assertTrue(receipt.ok, receipt.message)
                self.assertEqual(receipt.result["action_type"], "TriggerOpportunity")
                self.assertEqual(receipt.result["opportunity_effect"], "decline")
                self.assertEqual(receipt.public_fallback_reply, "这次机会未被使用。")
                self.assertIsNone(
                    self.app.interceptor.decision_window_manager.find_pending(
                        window_id=window.window_id
                    )
                )

    def test_gm_can_decline_opportunity_without_inventing_an_effect(self) -> None:
        window = self.app.interceptor.decision_window_manager.create(
            kind="fumble_opportunity",
            owner="__gm__",
            prompt="GM可以选择机会效果，或立即放弃。",
            options=[{"effect": "情报"}],
            blocking=True,
            allowed_responders=["__gm__"],
            action_type="TriggerOpportunity",
            payload={"source_actor": "伊莉雅", "controller": "gm"},
        )

        receipt = self.service.gm_gameplay_tools.resolve_gm_opportunity(
            gameplay_context("系统继续结算。"),
            {
                "window_id": window.window_id,
                "choice": "decline",
                "details": {},
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["opportunity_effect"], "decline")
        self.assertEqual(receipt.public_fallback_reply, "这次机会未被使用。")
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window.window_id
            )
        )

    def test_lost_item_opportunity_can_change_an_existing_scene_object(self) -> None:
        frame = self.app.scene_frame_manager.ensure_frame(
            scene=self.app.scene_manager.current_scene,
            recent_chat="伊莉雅面前有一扇老旧牢门。",
            world_state=self.app.world_state,
            character_manager=self.app.character_manager,
        )
        window = self.app.interceptor.decision_window_manager.create(
            kind="critical_opportunity",
            owner="伊莉雅",
            prompt="选择大成功机会。",
            options=[{"effect": "失物"}],
            blocking=True,
            action_type="TriggerOpportunity",
        )
        message = "我选择机会：失物，面前的牢门已经腐蚀，只要轻轻一推就能打开。"

        receipt = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context(message),
            {
                "action_type": "TriggerOpportunity",
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "失物",
                # The first model attempt in the real incident supplied only
                # the grounded outcome. This must remain sufficient.
                "details": {
                    "description": "伊莉雅面前的牢门已经腐蚀，只要轻轻一推就能打开。",
                },
                "evidence": "面前的牢门已经腐蚀",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["opportunity_effect"], "失物")
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window.window_id
            )
        )
        self.assertIn("牢门已经腐蚀", receipt.public_fallback_reply)
        self.assertTrue(
            any("牢门已经腐蚀" in fact for fact in frame.public_facts),
            frame.public_facts,
        )
        self.assertEqual(self.app.character_manager.get("伊莉雅").equipment, [])

    def test_all_core_opportunities_commit_through_the_same_tool_contract(self) -> None:
        self.app.character_manager.add(
            Character(
                name="守望会会长",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 10},
                max_hp=50,
                hp=50,
                max_mp=50,
                mp=50,
                traits=["npc", "警觉"],
                affinities={"fire": Affinity.WEAK},
            )
        )
        self.app.world_state.ensure_npc_persona(
            "守望会会长",
            active_goal="保护旧路，不让财团找到旅人",
            traits=["谨慎", "守序"],
        )
        self.app.clock_manager.add(
            Clock(
                name="打开牢门",
                max_segments=6,
                current=1,
                clock_type="objective",
                scope="scene",
            )
        )
        cases = [
            ("揭示", {"target": "守望会会长"}),
            ("进展", {"clock_name": "打开牢门", "delta": 2}),
            ("纽带", {"target": "守望会会长", "emotion": "敬意"}),
            (
                "情报",
                {
                    "information": "钥匙藏在值班室第三个抽屉里",
                    "subject": "牢区钥匙",
                },
            ),
            (
                "青睐",
                {
                    "target": "守望会会长",
                    "description": "守望会会长答应替队伍拖延一轮盘问",
                },
            ),
            ("审视", {"target": "守望会会长", "scan_type": "弱点"}),
            (
                "失态",
                {
                    "target": "守望会会长",
                    "statement": "我确实放走过一个无名旅人。",
                },
            ),
            (
                "失物",
                {"scene_object": "牢门锁舌", "description": "牢门锁舌锈断了。"},
            ),
            ("受苦", {"target": "守望会会长", "status_effect": "shaken"}),
            ("优势", {"target": "伊莉雅"}),
            (
                "转折",
                {"subject": "巡夜人", "description": "巡夜人突然从楼梯口冲了下来。"},
            ),
            ("自定义", {"description": "熄灭的符文短暂亮起，映出一条隐蔽线路。"}),
        ]

        for effect, details in cases:
            with self.subTest(effect=effect):
                window = self.app.interceptor.decision_window_manager.create(
                    kind="critical_opportunity",
                    owner="伊莉雅",
                    prompt="选择大成功机会。",
                    options=[{"effect": effect}],
                    blocking=True,
                    action_type="TriggerOpportunity",
                )
                receipt = self.service.gm_gameplay_tools.resolve_rule_window(
                    gameplay_context(f"我把机会用于{effect}。"),
                    {
                        "action_type": "TriggerOpportunity",
                        "actor": "伊莉雅",
                        "window_id": window.window_id,
                        "choice": effect,
                        "details": details,
                        "evidence": f"机会用于{effect}",
                    },
                )

                self.assertTrue(receipt.ok, receipt.message)
                self.assertEqual(receipt.result["opportunity_effect"], effect)
                if effect == "转折":
                    self.assertNotIn("。。", receipt.public_fallback_reply)
                self.assertIsNone(
                    self.app.interceptor.decision_window_manager.find_pending(
                        window_id=window.window_id
                    )
                )

        bond = self.app.character_manager.get("伊莉雅").bonds[0]
        self.assertEqual(bond.emotions, ["钦佩"])

    def test_incomplete_lost_item_and_suffer_choices_open_parameter_windows(self) -> None:
        cases = [
            (
                "失物",
                {},
                "item_or_scene_object",
                "哪件角色物品或现场物件",
            ),
            (
                "受苦",
                {"target": "洛岚"},
                "status_effect",
                "眩晕、动摇、迟缓还是虚弱",
            ),
        ]

        for effect, details, required, prompt_text in cases:
            with self.subTest(effect=effect):
                window = self.app.interceptor.decision_window_manager.create(
                    kind="critical_opportunity",
                    owner="伊莉雅",
                    prompt="选择大成功机会。",
                    options=[{"effect": effect}],
                    blocking=True,
                    action_type="TriggerOpportunity",
                )
                receipt = self.service.gm_gameplay_tools.resolve_rule_window(
                    gameplay_context(f"我把机会用于{effect}。"),
                    {
                        "action_type": "TriggerOpportunity",
                        "actor": "伊莉雅",
                        "window_id": window.window_id,
                        "choice": effect,
                        "details": details,
                        "evidence": f"机会用于{effect}",
                    },
                )

                self.assertTrue(receipt.ok, receipt.message)
                self.assertIn(prompt_text, receipt.public_fallback_reply)
                parameter = self.app.interceptor.decision_window_manager.find_pending(
                    kind="opportunity_parameter",
                    owner="伊莉雅",
                )
                self.assertIsNotNone(parameter)
                self.assertEqual(parameter.payload["required_parameter"], required)
                self.app.interceptor.decision_window_manager.cancel_matching(
                    kind="opportunity_parameter",
                    owner="伊莉雅",
                    reason="next_subtest",
                )

    def test_lost_item_parameter_followup_can_finish_with_a_scene_object(self) -> None:
        source = self.app.interceptor.decision_window_manager.create(
            kind="critical_opportunity",
            owner="伊莉雅",
            prompt="选择大成功机会。",
            options=[{"effect": "失物"}],
            blocking=True,
            action_type="TriggerOpportunity",
        )
        first = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("我把机会用在失物上。"),
            {
                "action_type": "TriggerOpportunity",
                "actor": "伊莉雅",
                "window_id": source.window_id,
                "choice": "失物",
                "details": {},
                "evidence": "机会用在失物上",
            },
        )

        self.assertTrue(first.ok, first.message)
        parameter = self.app.interceptor.decision_window_manager.find_pending(
            kind="opportunity_parameter",
            owner="伊莉雅",
        )
        self.assertIsNotNone(parameter)
        second = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("那就让牢门锁舌彻底锈断。"),
            {
                "action_type": "TriggerOpportunity",
                "actor": "伊莉雅",
                "window_id": parameter.window_id,
                "choice": "失物",
                "details": {
                    "scene_object": "牢门锁舌",
                    "description": "牢门锁舌彻底锈断了。",
                },
                "evidence": "牢门锁舌彻底锈断",
            },
        )

        self.assertTrue(second.ok, second.message)
        self.assertEqual(second.result["opportunity_effect"], "失物")
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=parameter.window_id
            )
        )
        self.assertTrue(
            any("牢门锁舌彻底锈断" in fact for fact in self.app.world_state.memories)
        )

    def test_opportunity_parameter_followup_keeps_details_from_the_first_step(self) -> None:
        source = self.app.interceptor.decision_window_manager.create(
            kind="critical_opportunity",
            owner="伊莉雅",
            prompt="选择大成功机会。",
            options=[{"effect": "受苦"}],
            blocking=True,
            action_type="TriggerOpportunity",
        )
        first = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("我让洛岚承受受苦。"),
            {
                "action_type": "TriggerOpportunity",
                "actor": "伊莉雅",
                "window_id": source.window_id,
                "choice": "受苦",
                "details": {"target": "洛岚"},
                "evidence": "让洛岚承受受苦",
            },
        )

        self.assertTrue(first.ok, first.message)
        parameter = self.app.interceptor.decision_window_manager.find_pending(
            kind="opportunity_parameter",
            owner="伊莉雅",
        )
        self.assertEqual(parameter.payload["provided_parameters"]["target"], "洛岚")
        second = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("施加动摇。"),
            {
                "action_type": "TriggerOpportunity",
                "actor": "伊莉雅",
                "window_id": parameter.window_id,
                "choice": "受苦",
                "details": {"status_effect": "shaken"},
                "evidence": "施加动摇",
            },
        )

        self.assertTrue(second.ok, second.message)
        self.assertIn(StatusEffect.SHAKEN, self.app.character_manager.get("洛岚").statuses)

    def test_lost_item_can_make_currently_equipped_gear_unavailable(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.equipment = ["钢匕首"]
        ilya.equipped_main_hand = "钢匕首"
        window = self.app.interceptor.decision_window_manager.create(
            kind="critical_opportunity",
            owner="伊莉雅",
            prompt="选择大成功机会。",
            options=[{"effect": "失物"}],
            blocking=True,
            action_type="TriggerOpportunity",
        )

        receipt = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("钢匕首被铁栅卡住，暂时取不回来。"),
            {
                "action_type": "TriggerOpportunity",
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "失物",
                "details": {
                    "target": "伊莉雅",
                    "item_name": "钢匕首",
                    "description": "钢匕首被铁栅卡住，暂时取不回来。",
                },
                "evidence": "钢匕首被铁栅卡住",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertIn("钢匕首", ilya.equipment)
        self.assertIn("钢匕首", ilya.unavailable_equipment)
        self.assertEqual(ilya.equipped_main_hand, "徒手攻击")

    def test_invalid_lost_item_explains_the_exact_reason_and_keeps_window(self) -> None:
        window = self.app.interceptor.decision_window_manager.create(
            kind="critical_opportunity",
            owner="伊莉雅",
            prompt="选择大成功机会。",
            options=[{"effect": "失物"}],
            blocking=True,
            action_type="TriggerOpportunity",
        )

        receipt = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("让伊莉雅失去并不存在的王冠。"),
            {
                "action_type": "TriggerOpportunity",
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "失物",
                "details": {"target": "伊莉雅", "item_name": "王冠"},
                "evidence": "失去并不存在的王冠",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "RULE_ACTION_REJECTED")
        self.assertIn("没有可失去的物品【王冠】", receipt.message)
        self.assertIsNotNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window.window_id
            )
        )

    def test_misstep_statement_is_deferred_to_another_pc_controller(self) -> None:
        window = self.app.interceptor.decision_window_manager.create(
            kind="critical_opportunity",
            owner="伊莉雅",
            prompt="选择大成功机会。",
            options=[{"effect": "失态"}],
            blocking=True,
            action_type="TriggerOpportunity",
        )
        first = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("我让洛岚失态，说他愿意投降。", speaker="阿凛"),
            {
                "action_type": "TriggerOpportunity",
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "失态",
                "details": {"target": "洛岚", "statement": "我愿意投降。"},
                "evidence": "让洛岚失态",
            },
        )

        self.assertTrue(first.ok, first.message)
        self.assertNotIn("我愿意投降", self.app.world_state.subject_facts.get("洛岚", []))
        parameter = self.app.interceptor.decision_window_manager.find_pending(
            kind="opportunity_parameter",
            owner="洛岚",
        )
        self.assertIsNotNone(parameter)
        self.assertEqual(parameter.allowed_responders, ["洛岚"])
        second = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("洛岚咬牙承认：我确实藏起了那把钥匙。", speaker="白河"),
            {
                "action_type": "TriggerOpportunity",
                "actor": "洛岚",
                "window_id": parameter.window_id,
                "choice": "失态",
                "details": {"statement": "我确实藏起了那把钥匙。"},
                "evidence": "我确实藏起了那把钥匙",
            },
        )

        self.assertTrue(second.ok, second.message)
        self.assertTrue(
            any(
                "我确实藏起了那把钥匙" in fact
                for fact in self.app.world_state.subject_facts.get("洛岚", [])
            )
        )

    def test_bond_opportunity_cannot_change_another_pcs_bond(self) -> None:
        window = self.app.interceptor.decision_window_manager.create(
            kind="critical_opportunity",
            owner="伊莉雅",
            prompt="选择大成功机会。",
            options=[{"effect": "纽带"}],
            blocking=True,
            action_type="TriggerOpportunity",
        )

        receipt = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("我替洛岚建立对守望会的羁绊。"),
            {
                "action_type": "TriggerOpportunity",
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "纽带",
                "details": {
                    "bond_owner": "洛岚",
                    "target": "守望会",
                    "emotion": "钦佩",
                },
                "evidence": "替洛岚建立",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "OPPORTUNITY_BOND_OWNER_MISMATCH")

    def test_reveal_opportunity_accepts_a_persistent_npc_without_combat_stats(self) -> None:
        self.app.world_state.ensure_npc_persona(
            "老狱卒",
            active_goal="拖到换岗铃响，再偷偷放走被冤枉的囚犯",
            traits=["疲惫", "心软"],
        )
        window = self.app.interceptor.decision_window_manager.create(
            kind="critical_opportunity",
            owner="伊莉雅",
            prompt="选择大成功机会。",
            options=[{"effect": "揭示"}],
            blocking=True,
            action_type="TriggerOpportunity",
        )

        receipt = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("我对老狱卒使用揭示。"),
            {
                "action_type": "TriggerOpportunity",
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "揭示",
                "details": {"target": "老狱卒"},
                "evidence": "对老狱卒使用揭示",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertIn("偷偷放走", receipt.public_fallback_reply)

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
        self.app.conflict_manager.begin_current_turn()
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
        self.app.conflict_manager.begin_current_turn()
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
        self.assertEqual(receipt.result["opportunity_effect"], "受苦")
        self.assertIn(StatusEffect.SHAKEN, self.app.character_manager.get("伊莉雅").statuses)
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(window_id=window.window_id)
        )

    def test_gm_critical_opportunity_is_resolved_by_same_dedicated_tool(self) -> None:
        window = self.app.interceptor.decision_window_manager.create(
            kind="critical_opportunity",
            owner="__gm__",
            prompt="GM选择一个NPC大成功机会。",
            options=[{"effect": "优势"}],
            blocking=True,
            allowed_responders=["__gm__"],
            action_type="TriggerOpportunity",
            payload={"source_actor": "财团机兵", "source_action_type": "Attack"},
        )

        receipt = self.service.gm_gameplay_tools.resolve_gm_opportunity(
            gameplay_context("财团机兵的攻击掷出大成功。"),
            {
                "window_id": window.window_id,
                "choice": "优势",
                "details": {"target": "伊莉雅"},
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["opportunity_effect"], "优势")
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window.window_id
            )
        )

    def test_all_core_gm_opportunities_commit_without_internal_actor_leaks(self) -> None:
        self.app.character_manager.add(
            Character(
                name="守望会会长",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 10},
                max_hp=50,
                hp=50,
                max_mp=50,
                mp=50,
                traits=["npc", "警觉"],
                affinities={"fire": Affinity.WEAK},
            )
        )
        self.app.world_state.ensure_npc_persona(
            "守望会会长",
            active_goal="守住旧路",
            traits=["谨慎"],
        )
        self.app.clock_manager.add(
            Clock(
                name="巡逻逼近",
                max_segments=6,
                current=1,
                clock_type="threat",
                scope="scene",
            )
        )
        cases = [
            ("揭示", {"target": "守望会会长"}),
            ("进展", {"clock_name": "巡逻逼近"}),
            (
                "纽带",
                {
                    "bond_owner": "守望会会长",
                    "target": "伊莉雅",
                    "emotion": "敬意",
                },
            ),
            ("情报", {"information": "钥匙不在值班室"}),
            (
                "青睐",
                {
                    "target": "守望会会长",
                    "description": "守望会会长答应替巡夜人守住后门",
                },
            ),
            ("审视", {"target": "守望会会长"}),
            (
                "失态",
                {"target": "守望会会长", "statement": "我知道后门还开着。"},
            ),
            (
                "失物",
                {"scene_object": "牢门锁舌", "description": "锁舌锈断了。"},
            ),
            ("受苦", {"target": "伊莉雅", "status_effect": "shaken"}),
            ("优势", {"target": "守望会会长"}),
            ("转折", {"subject": "巡夜人", "description": "巡夜人赶到。"}),
            ("自定义", {"description": "牢区灯火全部熄灭。"}),
        ]

        for effect, details in cases:
            with self.subTest(effect=effect):
                window = self.app.interceptor.decision_window_manager.create(
                    kind="fumble_opportunity",
                    owner="__gm__",
                    prompt="GM选择一个大失败机会。",
                    options=[{"effect": effect}],
                    blocking=True,
                    allowed_responders=["__gm__"],
                    action_type="TriggerOpportunity",
                    payload={"source_actor": "伊莉雅"},
                )
                receipt = self.service.gm_gameplay_tools.resolve_gm_opportunity(
                    gameplay_context("伊莉雅的大失败产生一个GM机会。"),
                    {
                        "window_id": window.window_id,
                        "choice": effect,
                        "details": details,
                    },
                )

                self.assertTrue(receipt.ok, receipt.message)
                self.assertEqual(receipt.result["opportunity_effect"], effect)
                self.assertNotIn("__gm__", receipt.public_fallback_reply)

        self.assertFalse(
            any("__gm__" in memory for memory in self.app.world_state.memories)
        )

    def test_gm_favor_requires_a_concrete_support_relationship(self) -> None:
        window = self.app.interceptor.decision_window_manager.create(
            kind="fumble_opportunity",
            owner="__gm__",
            prompt="GM选择一个大失败机会。",
            options=[{"effect": "青睐"}],
            blocking=True,
            allowed_responders=["__gm__"],
            action_type="TriggerOpportunity",
            payload={"source_actor": "伊莉雅"},
        )

        receipt = self.service.gm_gameplay_tools.resolve_gm_opportunity(
            gameplay_context("伊莉雅的大失败产生一个GM机会。"),
            {
                "window_id": window.window_id,
                "choice": "青睐",
                "details": {"target": "守望会会长"},
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "GM_OPPORTUNITY_FAVOR_DESCRIPTION_REQUIRED",
        )
        self.assertIn("支持给了谁", receipt.message)
        self.assertIsNotNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window.window_id
            )
        )

    def test_gm_progress_opportunity_respects_explicit_amount_and_direction(self) -> None:
        self.app.clock_manager.add(
            Clock(
                name="警报升高",
                max_segments=6,
                current=4,
                clock_type="threat",
                scope="scene",
            )
        )
        window = self.app.interceptor.decision_window_manager.create(
            kind="fumble_opportunity",
            owner="__gm__",
            prompt="GM选择一个大失败机会。",
            options=[{"effect": "进展"}],
            blocking=True,
            allowed_responders=["__gm__"],
            action_type="TriggerOpportunity",
            payload={"source_actor": "伊莉雅"},
        )

        receipt = self.service.gm_gameplay_tools.resolve_gm_opportunity(
            gameplay_context("大失败令守卫暂时被错误警报引开。"),
            {
                "window_id": window.window_id,
                "choice": "进展",
                "details": {
                    "clock_name": "警报升高",
                    "delta": 1,
                    "erase": True,
                },
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(self.app.clock_manager.get("警报升高").current, 3)

    def test_gm_progress_opportunity_defaults_against_the_players(self) -> None:
        for name, clock_type, expected in (
            ("打开旧路", "objective", 2),
            ("敌方仪式", "villain", 4),
        ):
            self.app.clock_manager.add(
                Clock(
                    name=name,
                    max_segments=6,
                    current=3,
                    clock_type=clock_type,
                    scope="scene",
                )
            )
            window = self.app.interceptor.decision_window_manager.create(
                kind="fumble_opportunity",
                owner="__gm__",
                prompt="GM选择一个大失败机会。",
                options=[{"effect": "进展"}],
                blocking=True,
                allowed_responders=["__gm__"],
                action_type="TriggerOpportunity",
                payload={"source_actor": "伊莉雅"},
            )

            receipt = self.service.gm_gameplay_tools.resolve_gm_opportunity(
                gameplay_context("伊莉雅的大失败让敌方取得机会。"),
                {
                    "window_id": window.window_id,
                    "choice": "进展",
                    "details": {"clock_name": name, "delta": 1},
                },
            )

            self.assertTrue(receipt.ok, receipt.message)
            self.assertEqual(self.app.clock_manager.get(name).current, expected)

    def test_gm_misstep_for_a_pc_waits_for_that_players_statement(self) -> None:
        source = self.app.interceptor.decision_window_manager.create(
            kind="fumble_opportunity",
            owner="__gm__",
            prompt="GM选择一个大失败机会。",
            options=[{"effect": "失态"}],
            blocking=True,
            allowed_responders=["__gm__"],
            action_type="TriggerOpportunity",
            payload={"source_actor": "伊莉雅"},
        )
        first = self.service.gm_gameplay_tools.resolve_gm_opportunity(
            gameplay_context("伊莉雅的大失败产生一个GM机会。"),
            {
                "window_id": source.window_id,
                "choice": "失态",
                "details": {
                    "target": "伊莉雅",
                    "statement": "这句不能由GM替玩家决定。",
                },
            },
        )

        self.assertTrue(first.ok, first.message)
        parameter = self.app.interceptor.decision_window_manager.find_pending(
            kind="opportunity_parameter",
            owner="伊莉雅",
        )
        self.assertIsNotNone(parameter)
        self.assertNotIn(
            "这句不能由GM替玩家决定",
            "".join(self.app.world_state.subject_facts.get("伊莉雅", [])),
        )
        second = self.service.gm_gameplay_tools.resolve_rule_window(
            gameplay_context("伊莉雅低声说：我把通行证藏在了钟后。"),
            {
                "action_type": "TriggerOpportunity",
                "actor": "伊莉雅",
                "window_id": parameter.window_id,
                "choice": "失态",
                "details": {"statement": "我把通行证藏在了钟后。"},
                "evidence": "通行证藏在了钟后",
            },
        )

        self.assertTrue(second.ok, second.message)
        self.assertIn(
            "通行证藏在了钟后",
            "".join(self.app.world_state.subject_facts.get("伊莉雅", [])),
        )

    def test_ritual_discount_requires_and_consumes_owned_story_material(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.classes["元素使"] = 1
        ilya.skills["元素系仪式"] = 1
        ilya.max_mp = ilya.mp = 100
        material = self.app.world_state.commit_story_item_action(
            operation="acquire",
            item_name="风之精灵羽",
            actor="伊莉雅",
            scene_location="风铃廊",
            public_fact="伊莉雅取得了风之精灵羽。",
            source="测试",
            tags=["material", "ritual_material"],
        )

        receipt = self.service.gm_gameplay_tools.perform_ritual_project_action(
            gameplay_context("伊莉雅用风之精灵羽施展仪式，唤醒沉睡的风铃。"),
            {
                "action_type": "CastRitual",
                "actor": "伊莉雅",
                "details": {
                    "name": "风铃苏醒",
                    "discipline": "elementalism",
                    "potency": "minor",
                    "scope": "individual",
                    "effect": "让沉睡的风铃重新发声",
                    "rare_material": "风之精灵羽",
                },
                "evidence": "用风之精灵羽施展仪式",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(ilya.mp, 90)
        self.assertEqual(
            self.app.world_state.story_items[material.item_id].status.value,
            "consumed",
        )

    def test_ritual_discount_rejects_an_unowned_material_name(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.classes["元素使"] = 1
        ilya.skills["元素系仪式"] = 1

        receipt = self.service.gm_gameplay_tools.perform_ritual_project_action(
            gameplay_context("伊莉雅想用并不存在的星砂启动仪式。"),
            {
                "action_type": "PlanRitual",
                "actor": "伊莉雅",
                "details": {
                    "name": "星砂回响",
                    "discipline": "elementalism",
                    "potency": "minor",
                    "scope": "individual",
                    "effect": "让星光指出道路",
                    "rare_material": "星砂",
                },
                "evidence": "用并不存在的星砂启动仪式",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "RITUAL_MATERIAL_NOT_OWNED")

    def test_project_separates_required_material_from_cost_material(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.classes["造物使"] = 1
        ilya.abilities.append("可发起项目")
        ilya.zenit = 500
        required = self.app.world_state.commit_story_item_action(
            operation="acquire",
            item_name="活化齿轮心",
            actor="伊莉雅",
            scene_location="风铃廊",
            public_fact="伊莉雅取得了活化齿轮心。",
            source="测试",
            tags=["material", "project_material"],
        )
        payment = self.app.world_state.commit_story_item_action(
            operation="acquire",
            item_name="辉钢锭",
            actor="伊莉雅",
            scene_location="风铃廊",
            public_fact="伊莉雅取得了辉钢锭。",
            source="测试",
            tags=["material"],
        )

        receipt = self.service.gm_gameplay_tools.perform_ritual_project_action(
            gameplay_context("伊莉雅用活化齿轮心和辉钢锭启动工程风行靴。"),
            {
                "action_type": "StartProject",
                "actor": "伊莉雅",
                "details": {
                    "name": "风行靴",
                    "potency": "moderate",
                    "scope": "individual",
                    "use": "consumable",
                    "effect": "短暂越过一处危险地形",
                    "special_materials": ["活化齿轮心"],
                    "cost_materials": ["辉钢锭"],
                    "material_credit": 100,
                },
                "evidence": "用活化齿轮心和辉钢锭启动工程风行靴",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(ilya.zenit, 400)
        project = self.app.project_manager.projects["风行靴"]
        self.assertEqual(project.special_materials, ["活化齿轮心"])
        self.assertEqual(project.cost_materials, ["辉钢锭"])
        self.assertEqual(
            self.app.world_state.story_items[required.item_id].status.value,
            "consumed",
        )
        self.assertEqual(
            self.app.world_state.story_items[payment.item_id].status.value,
            "consumed",
        )

    def test_project_cannot_enlist_another_players_character_without_confirmation(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.classes["造物使"] = 1
        ilya.abilities.append("可发起项目")
        ilya.zenit = 500
        self.app.project_manager.start_project(
            inventor="伊莉雅",
            name="水晶罗盘",
            potency=self.app.interceptor._ritual_potency("minor"),
            scope=self.app.interceptor._ritual_scope("individual"),
            use=self.app.interceptor._project_use("consumable"),
            effect="寻找遗迹入口",
        )

        receipt = self.service.gm_gameplay_tools.perform_ritual_project_action(
            gameplay_context("伊莉雅开始推进水晶罗盘。"),
            {
                "action_type": "WorkProject",
                "actor": "伊莉雅",
                "details": {
                    "project_name": "水晶罗盘",
                    "workers": ["伊莉雅", "洛岚"],
                },
                "evidence": "开始推进水晶罗盘",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "PROJECT_WORKER_CONFIRMATION_REQUIRED",
        )

    def test_project_accepts_recent_confirmation_from_each_workers_owner(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.classes["造物使"] = 1
        ilya.abilities.append("可发起项目")
        ilya.zenit = 500
        self.app.project_manager.start_project(
            inventor="伊莉雅",
            name="水晶罗盘",
            potency=self.app.interceptor._ritual_potency("minor"),
            scope=self.app.interceptor._ritual_scope("individual"),
            use=self.app.interceptor._project_use("consumable"),
            effect="寻找遗迹入口",
        )
        context = gameplay_context("伊莉雅开始推进水晶罗盘。")
        context.metadata["recent_public_context"] = (
            "白河: 洛岚确认也参加今天的水晶罗盘工程。"
        )

        receipt = self.service.gm_gameplay_tools.perform_ritual_project_action(
            context,
            {
                "action_type": "WorkProject",
                "actor": "伊莉雅",
                "details": {
                    "project_name": "水晶罗盘",
                    "workers": ["伊莉雅", "洛岚"],
                    "worker_confirmations": [
                        {
                            "worker": "洛岚",
                            "speaker": "白河",
                            "evidence": "洛岚确认也参加今天的水晶罗盘工程",
                        }
                    ],
                },
                "evidence": "开始推进水晶罗盘",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        project = self.app.project_manager.projects["水晶罗盘"]
        self.assertTrue(project.completed)
        self.assertEqual(project.current_progress, project.required_progress)


if __name__ == "__main__":
    unittest.main()
