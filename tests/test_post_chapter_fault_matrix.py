from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from fu_gm.gm_tool_contracts import GMToolExecutionContext
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import Character, Clock, EnemyRank, RollOutcome, SceneType


CAMPAIGN_ID = "post-chapter-fault-matrix"


def context(message: str) -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id=CAMPAIGN_ID,
        session_id="session-01",
        channel_id="group-1",
        speaker="阿凛",
        gate_status="adventure",
        directly_addressed=True,
        metadata={
            "current_message": message,
            "recent_public_context": message,
        },
    )


class PostChapterFaultMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.service = FUGMHttpService(
            data_root=self.tmpdir.name,
            use_llm=False,
        )
        self.runtime = self.service._runtime(CAMPAIGN_ID)
        self.app = self.runtime.app
        self.app.character_manager.add(
            Character(
                name="伊莉雅",
                attributes={"DEX": 8, "INS": 10, "MIG": 8, "WLP": 6},
                max_hp=45,
                hp=20,
                max_mp=35,
                mp=10,
                crisis_threshold=22,
                inventory_points=4,
                max_inventory_points=6,
                fabula_points=3,
                zenit=500,
                traits=["pc"],
            )
        )
        self.app.world_map_manager.add_location(
            "白花碑驿站",
            x=0,
            y=0,
            terrain="村庄",
        )
        self.app.world_map_manager.add_location(
            "镜之水道",
            x=2,
            y=0,
            terrain="遗迹",
        )
        self.scene = self.app.start_scene(
            "白花碑驿站",
            SceneType.STANDARD,
            location="白花碑驿站",
            participants=["伊莉雅"],
        )
        self.service._autosave_campaign(self.runtime, CAMPAIGN_ID)
        self.snapshot_path = self.app.memory_store._snapshot_path(CAMPAIGN_ID)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_travel_write_rolls_back_memory_rng_scene_and_disk(self) -> None:
        disk_before = self.snapshot_path.read_bytes()
        rng_before = self.app.interceptor.rules_engine._rng.getstate()
        scene_before = self.app.scene_manager.current_scene.scene_id

        with patch.object(
            self.service,
            "_autosave_campaign",
            side_effect=RuntimeError("disk unavailable"),
        ):
            receipt = self.service.gm_tool_registry.execute(
                "travel_party",
                {
                    "origin": "白花碑驿站",
                    "destination": "镜之水道",
                    "participants": ["伊莉雅"],
                    "transport": "徒步",
                    "explicit_distance": 2,
                    "route_type": "land",
                    "default_threat_level": "low",
                },
                context("伊莉雅沿旧路徒步前往镜之水道。"),
            )

        self.assertFalse(receipt.ok)
        self.assertIsNone(self.app.travel_manager.active_journey)
        self.assertEqual(
            self.app.scene_manager.current_scene.scene_id,
            scene_before,
        )
        self.assertEqual(
            self.app.interceptor.rules_engine._rng.getstate(),
            rng_before,
        )
        self.assertEqual(self.snapshot_path.read_bytes(), disk_before)

    def test_dungeon_start_rolls_back_scene_clocks_and_dungeon_state(self) -> None:
        disk_before = self.snapshot_path.read_bytes()
        scene_before = self.app.scene_manager.current_scene.scene_id
        clocks_before = list(self.app.clock_manager.formatted())

        with patch.object(
            self.service,
            "_autosave_campaign",
            side_effect=RuntimeError("disk unavailable"),
        ):
            receipt = self.service.gm_tool_registry.execute(
                "start_dungeon_exploration",
                {
                    "name": "镜之水道",
                    "location": "白花碑驿站",
                    "importance": "major",
                    "preparation": "prepared",
                    "mode": "detailed",
                    "purpose": "寻找守钟日志",
                    "concept": "倒映旧日景象的地下水道",
                    "focus": "封存的守钟日志",
                    "inhabitants": "古代构装体",
                    "peculiarity": "水面映出一天后的景象",
                    "participants": ["伊莉雅"],
                },
                context("伊莉雅推开驿站下方的水门，进入镜之水道。"),
            )

        self.assertFalse(receipt.ok)
        self.assertFalse(self.app.dungeon_manager.state.active)
        self.assertEqual(
            self.app.scene_manager.current_scene.scene_id,
            scene_before,
        )
        self.assertEqual(self.app.clock_manager.formatted(), clocks_before)
        self.assertEqual(self.snapshot_path.read_bytes(), disk_before)

    def test_conflict_start_rolls_back_parent_scene_and_initiative_rng(self) -> None:
        self.app.character_manager.add(
            Character(
                name="水道机兵",
                attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 6},
                max_hp=70,
                hp=70,
                max_mp=40,
                mp=40,
                initiative=5,
                traits=["enemy", "construct"],
            )
        )
        self.app.conflict_manager.register_enemy(
            "水道机兵",
            EnemyRank.SOLDIER,
        )
        self.app.scene_manager.add_participant("水道机兵")
        self.service._autosave_campaign(self.runtime, CAMPAIGN_ID)
        disk_before = self.snapshot_path.read_bytes()
        scene_before = self.app.scene_manager.current_scene.scene_id
        rng_before = self.app.interceptor.rules_engine._rng.getstate()
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["DEX", "INS"],
                dice=[(8, 5), (10, 4)],
                total=9,
                modifier=0,
                high_roll=5,
                target_number=5,
                success=True,
                critical_success=False,
                fumble=False,
                margin=4,
            )
        )

        with patch.object(
            self.service,
            "_autosave_campaign",
            side_effect=RuntimeError("disk unavailable"),
        ):
            receipt = self.service.gm_tool_registry.execute(
                "start_conflict",
                {
                    "scene_name": "水门伏击",
                    "pcs": ["伊莉雅"],
                    "enemies": ["水道机兵"],
                    "leader": "伊莉雅",
                    "supporters": [],
                    "objective": "突破伏击",
                    "public_opening": "水道机兵踏进浅水，封住了唯一的出口。",
                },
                context("水道机兵封住出口，伊莉雅举剑迎战。"),
            )

        self.assertFalse(receipt.ok)
        self.assertFalse(self.app.conflict_manager.state.active)
        self.assertEqual(
            self.app.scene_manager.current_scene.scene_id,
            scene_before,
        )
        self.assertEqual(
            self.app.scene_manager.current_scene.scene_type,
            SceneType.STANDARD,
        )
        self.assertEqual(
            self.app.interceptor.rules_engine._rng.getstate(),
            rng_before,
        )
        self.assertEqual(self.snapshot_path.read_bytes(), disk_before)

    def test_rest_write_rolls_back_resources_and_scene(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        resources_before = (
            hero.hp,
            hero.mp,
            hero.inventory_points,
            hero.zenit,
        )
        scene_before = self.app.scene_manager.current_scene.scene_id
        disk_before = self.snapshot_path.read_bytes()

        with patch.object(
            self.service,
            "_autosave_campaign",
            side_effect=RuntimeError("disk unavailable"),
        ):
            receipt = self.service.gm_tool_registry.execute(
                "perform_scene_action",
                {
                    "action_type": "Rest",
                    "actor": "伊莉雅",
                    "details": {
                        "rest_type": "wilderness",
                        "safe_source": "魔法帐篷",
                        "rest_source_kind": "tent",
                        "payer": "伊莉雅",
                        "participants": ["伊莉雅"],
                    },
                },
                context("伊莉雅在安全的驿站院内支起魔法帐篷休息。"),
            )

        self.assertFalse(receipt.ok)
        restored = self.app.character_manager.get("伊莉雅")
        self.assertEqual(
            (
                restored.hp,
                restored.mp,
                restored.inventory_points,
                restored.zenit,
            ),
            resources_before,
        )
        self.assertEqual(
            self.app.scene_manager.current_scene.scene_id,
            scene_before,
        )
        self.assertEqual(self.snapshot_path.read_bytes(), disk_before)

    def test_clock_completion_repairs_missing_scene_frame_and_records_fact(self) -> None:
        self.assertIsNone(self.app.scene_frame_manager.current_frame)
        self.app.clock_manager.add(
            Clock(
                name="打开旧路",
                max_segments=4,
                current=3,
                clock_type="objective",
                scope="scene",
                scene_id=self.scene.scene_id,
                completion_consequence="旧路闸门打开",
            )
        )

        receipt = self.service.gm_tool_registry.execute(
            "change_clock",
            {
                "name": "打开旧路",
                "delta": 1,
                "cause": "direct_action_success",
                "reason": "最后一道锁舌已经解除",
                "public_reply": "旧闸升起，旧路已经打开。\n【打开旧路】4/4",
                "completion_facts": ["旧路已经打开。"],
            },
            context("伊莉雅转动最后一道锁舌。"),
        )

        self.assertTrue(receipt.ok, receipt.message)
        frame = self.app.scene_frame_manager.current_frame
        self.assertIsNotNone(frame)
        self.assertEqual(frame.source_scene_id, self.scene.scene_id)
        self.assertIn("旧路已经打开。", frame.public_facts)
        self.assertTrue(
            any("【打开旧路】4/4" in beat for beat in frame.recent_beats)
        )
        self.assertFalse(self.app.clock_manager.exists("打开旧路"))
        self.assertTrue(self.app.clock_manager.is_retired("打开旧路"))

    def test_clock_completion_save_failure_restores_missing_frame_and_clock(self) -> None:
        self.assertIsNone(self.app.scene_frame_manager.current_frame)
        self.app.clock_manager.add(
            Clock(
                name="打开旧路",
                max_segments=4,
                current=3,
                clock_type="objective",
                scope="scene",
                scene_id=self.scene.scene_id,
                completion_consequence="旧路闸门打开",
            )
        )
        self.service._autosave_campaign(self.runtime, CAMPAIGN_ID)
        disk_before = self.snapshot_path.read_bytes()

        with patch.object(
            self.service,
            "_autosave_campaign",
            side_effect=RuntimeError("disk unavailable"),
        ):
            receipt = self.service.gm_tool_registry.execute(
                "change_clock",
                {
                    "name": "打开旧路",
                    "delta": 1,
                    "cause": "direct_action_success",
                    "reason": "最后一道锁舌已经解除",
                    "public_reply": "旧闸升起，旧路已经打开。\n【打开旧路】4/4",
                    "completion_facts": ["旧路已经打开。"],
                },
                context("伊莉雅转动最后一道锁舌。"),
            )

        self.assertFalse(receipt.ok)
        self.assertIsNone(self.app.scene_frame_manager.current_frame)
        restored = self.app.clock_manager.get("打开旧路")
        self.assertEqual(restored.current, 3)
        self.assertFalse(self.app.clock_manager.is_retired("打开旧路"))
        self.assertEqual(self.snapshot_path.read_bytes(), disk_before)

    def test_npc_introduction_save_failure_restores_profile_scene_and_frame(self) -> None:
        self.assertIsNone(self.app.scene_frame_manager.current_frame)
        disk_before = self.snapshot_path.read_bytes()
        participants_before = list(self.scene.participants)

        with patch.object(
            self.service,
            "_autosave_campaign",
            side_effect=RuntimeError("disk unavailable"),
        ):
            receipt = self.service.gm_tool_registry.execute(
                "introduce_npc",
                {
                    "name": "守钟人阿莱",
                    "profile": {
                        "public_identity": "守钟人阿莱",
                        "role_in_story": "旧闸看守",
                        "active_goal": "确认来客不会惊醒水道里的构装体",
                        "authority_scope": "可以开启驿站旧闸",
                        "knowledge_scope": "知道旧闸和水道入口的现状",
                        "speech_style": "简短直接",
                        "npc_rank": "supporting",
                    },
                    "public_reply": "守钟人阿莱提着一串铜钥匙从廊柱后走出来。",
                    "public_facts": [
                        "守钟人阿莱提着一串铜钥匙从廊柱后走出来。"
                    ],
                },
                context("伊莉雅循着钥匙声望向廊柱后方。"),
            )

        self.assertFalse(receipt.ok)
        self.assertNotIn("守钟人阿莱", self.app.world_state.npc_personas)
        self.assertEqual(
            self.app.scene_manager.current_scene.participants,
            participants_before,
        )
        self.assertIsNone(self.app.scene_frame_manager.current_frame)
        self.assertEqual(self.snapshot_path.read_bytes(), disk_before)

    def test_story_item_save_failure_restores_custody_round_clock_and_frame(self) -> None:
        self.app.clock_manager.add(
            Clock(
                name="巡逻逼近",
                max_segments=6,
                clock_type="threat",
                auto_advance="每个完整行动轮结束时推进1格",
                auto_advance_timing="action_round_end",
                scope="session",
            )
        )
        self.service._autosave_campaign(self.runtime, CAMPAIGN_ID)
        disk_before = self.snapshot_path.read_bytes()

        with patch.object(
            self.service,
            "_autosave_campaign",
            side_effect=RuntimeError("disk unavailable"),
        ):
            receipt = self.service.gm_tool_registry.execute(
                "commit_story_item_action",
                {
                    "actor": "伊莉雅",
                    "operation": "acquire",
                    "item_name": "油布旧册",
                    "description": "记有旧闸维修记录的登记册",
                    "public_result": "伊莉雅把油布旧册收入盾后的夹层；油布旧册现由伊莉雅持有。",
                    "public_fact": "油布旧册现由伊莉雅持有。",
                    "tags": ["线索", "登记册"],
                },
                context("伊莉雅立刻将油布旧册收好。"),
            )

        self.assertFalse(receipt.ok)
        self.assertIsNone(self.app.world_state.find_story_item(name="油布旧册"))
        self.assertEqual(self.app.clock_manager.get("巡逻逼近").current, 0)
        self.assertEqual(self.app.scene_manager.free_action_round_acted_actors, [])
        self.assertIsNone(self.app.scene_frame_manager.current_frame)
        self.assertEqual(self.snapshot_path.read_bytes(), disk_before)

    def test_zero_hp_choice_save_failure_restores_window_theme_and_turn(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.theme = "希望"
        self.app.conflict_manager.start_scene(
            "断桥之战",
            ["伊莉雅"],
            player_side=["伊莉雅"],
            enemy_side=[],
        )
        hero.hp = 0
        self.app.conflict_manager.resolve_zero_hp("伊莉雅")
        window = self.app.interceptor.decision_window_manager.find_pending(
            kind="zero_hp",
            owner="伊莉雅",
        )
        self.assertIsNotNone(window)
        actor_before = self.app.conflict_manager.state.current_actor()
        self.service._autosave_campaign(self.runtime, CAMPAIGN_ID)
        disk_before = self.snapshot_path.read_bytes()

        with patch.object(
            self.service,
            "_autosave_campaign",
            side_effect=RuntimeError("disk unavailable"),
        ):
            receipt = self.service.gm_tool_registry.execute(
                "resolve_rule_window",
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
                },
                context("伊莉雅选择放弃抵抗。"),
            )

        self.assertFalse(receipt.ok)
        self.assertEqual(self.app.character_manager.get("伊莉雅").theme, "希望")
        self.assertNotIn("伊莉雅", self.app.conflict_manager.state.fallen_pcs)
        self.assertEqual(
            self.app.conflict_manager.state.current_actor(),
            actor_before,
        )
        self.assertIsNotNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window.window_id
            )
        )
        self.assertEqual(self.snapshot_path.read_bytes(), disk_before)

    def test_end_scene_save_failure_restores_scene_frame_and_scene_clock(self) -> None:
        frame = self.app.scene_frame_manager.ensure_frame(
            scene=self.scene,
            recent_chat="旧闸后的水声越来越响。",
            world_state=self.app.world_state,
            character_manager=self.app.character_manager,
        )
        self.app.scene_frame_manager.record_public_fact("旧闸后的水位正在上涨。")
        self.app.clock_manager.add(
            Clock(
                name="旧闸水位上涨",
                max_segments=6,
                current=2,
                clock_type="threat",
                scope="scene",
                scene_id=self.scene.scene_id,
            )
        )
        self.service._autosave_campaign(self.runtime, CAMPAIGN_ID)
        disk_before = self.snapshot_path.read_bytes()

        with patch.object(
            self.service,
            "_autosave_campaign",
            side_effect=RuntimeError("disk unavailable"),
        ):
            receipt = self.service.gm_tool_registry.execute(
                "end_scene",
                {
                    "summary": "队伍离开旧闸。",
                    "public_reply": "众人离开旧闸，水声被甩在身后。",
                },
                context("众人已经离开白花碑驿站的旧闸。"),
            )

        self.assertFalse(receipt.ok)
        self.assertIsNotNone(self.app.scene_manager.current_scene)
        self.assertEqual(
            self.app.scene_manager.current_scene.scene_id,
            self.scene.scene_id,
        )
        self.assertTrue(self.app.clock_manager.exists("旧闸水位上涨"))
        restored_frame = self.app.scene_frame_manager.current_frame
        self.assertIsNotNone(restored_frame)
        self.assertEqual(restored_frame.scene_key, frame.scene_key)
        self.assertIn("旧闸后的水位正在上涨。", restored_frame.public_facts)
        self.assertEqual(self.snapshot_path.read_bytes(), disk_before)

    def test_end_conflict_save_failure_restores_conflict_and_scene_type(self) -> None:
        self.app.character_manager.add(
            Character(
                name="水道机兵",
                attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 6},
                max_hp=70,
                hp=0,
                max_mp=40,
                mp=40,
                traits=["enemy", "construct"],
            )
        )
        self.scene.scene_type = SceneType.CONFLICT
        self.app.scene_manager.add_participant("水道机兵")
        self.app.conflict_manager.start_scene(
            "水门伏击",
            ["伊莉雅", "水道机兵"],
            player_side=["伊莉雅"],
            enemy_side=["水道机兵"],
        )
        self.app.conflict_manager.state.surrendered_combatants.add("水道机兵")
        self.service._autosave_campaign(self.runtime, CAMPAIGN_ID)
        disk_before = self.snapshot_path.read_bytes()
        actor_before = self.app.conflict_manager.state.current_actor()

        with patch.object(
            self.service,
            "_autosave_campaign",
            side_effect=RuntimeError("disk unavailable"),
        ):
            receipt = self.service.gm_tool_registry.execute(
                "end_conflict",
                {
                    "outcome": "水道机兵已经投降。",
                    "continue_scene": True,
                    "public_reply": "水道机兵垂下铁臂，伏击就此停下。",
                },
                context("水道机兵已经投降，战斗结束。"),
            )

        self.assertFalse(receipt.ok)
        self.assertTrue(self.app.conflict_manager.state.active)
        self.assertEqual(
            self.app.conflict_manager.state.current_actor(),
            actor_before,
        )
        self.assertEqual(
            self.app.scene_manager.current_scene.scene_type,
            SceneType.CONFLICT,
        )
        self.assertEqual(self.snapshot_path.read_bytes(), disk_before)


if __name__ == "__main__":
    unittest.main()
