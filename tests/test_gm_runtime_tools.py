import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fu_gm.expressor import Expressor
from fu_gm.components.gm_message_tool_transaction import GMMessageToolTransaction
from fu_gm.components.session_log_manager import LLMStorySummarizer
from fu_gm.components.scene_creative_writer import SceneOpeningComposition
from fu_gm.gm_tool_agent import GMToolExecutionContext
from fu_gm.gm_tool_contracts import GMToolReceipt
from fu_gm.gm_tool_receipts import GMToolReceiptPolicy
from fu_gm.http_server import FUGMHttpService
from fu_gm.components.scene_frame_manager import SceneFrame
from fu_gm.models import (
    Character,
    Clock,
    EnemyRank,
    HeroDraft,
    RollOutcome,
    SceneType,
    SessionDramaticContract,
    SessionNPCRole,
    SessionSceneOpportunity,
)


class _BlockingSessionSummaryClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.started = threading.Event()
        self.release = threading.Event()

    def create_chat_completion(self, **kwargs: object) -> str:
        self.calls.append(dict(kwargs))
        self.started.set()
        if not self.release.wait(timeout=3):
            raise TimeoutError("summary test client was not released")
        return json.dumps(
            {
                "public_evidence_entry_ids": [0],
                "private_evidence_entry_ids": [],
                "location_entry_ids": [],
                "reward_entry_ids": [],
                "unresolved_entry_ids": [],
            },
            ensure_ascii=False,
        )


class _BlockingNPCSelectionClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.started = threading.Event()
        self.release = threading.Event()

    def create_chat_completion(self, **kwargs: object) -> str:
        self.calls.append(dict(kwargs))
        prompt = json.loads(kwargs["messages"][-1].content)
        self.started.set()
        if not self.release.wait(timeout=3):
            raise TimeoutError("npc selection test client was not released")
        return json.dumps(
            {
                "template_name": prompt["candidates"][0]["name"],
                "selection_reason": "测试选择。",
                "tactics": {},
            },
            ensure_ascii=False,
        )


def runtime_context(message: str, *, speaker: str = "阿凛") -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="runtime-tool-test",
        session_id="s1",
        channel_id="group-1",
        speaker=speaker,
        gate_status="adventure",
        directly_addressed=True,
        metadata={
            "current_message": message,
            "recent_public_context": "众人刚离开白花碑驿站。",
        },
    )


class GMRuntimeToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.service = FUGMHttpService(data_root=self.tmpdir.name, use_llm=False)
        self.runtime = self.service._runtime("runtime-tool-test")
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
            )
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_runtime_binds_npc_designer_to_authority_lock(self) -> None:
        designer = self.app.npc_blueprint_designer

        self.assertIs(designer._publication_lock, self.runtime.transaction_lock)
        self.assertIs(designer._publication_runtime, self.runtime)

    def test_transition_schema_forbids_absent_actor_even_in_negative_sentence(self) -> None:
        schema = next(
            item
            for item in self.service.gm_tool_registry.schemas()
            if item["name"] == "transition_scene"
        )
        public_arrival = schema["parameters"]["properties"]["public_arrival"]

        self.assertIn("否定", schema["description"])
        self.assertIn("未抵达者", public_arrival["description"])

    def _add_test_enemy(self, name: str = "财团机兵") -> None:
        self.app.character_manager.add(
            Character(
                name=name,
                attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 6},
                max_hp=70,
                hp=70,
                max_mp=40,
                mp=40,
                defenses={"physical": 11, "magic": 8},
                initiative=5,
                weapon_damage=14,
                traits=["enemy", "construct"],
            )
        )
        self.app.conflict_manager.register_enemy(name, EnemyRank.SOLDIER)

    def _add_test_ally(self, name: str = "白花巡守") -> None:
        self.app.character_manager.add(
            Character(
                name=name,
                attributes={"DEX": 10, "INS": 8, "MIG": 8, "WLP": 6},
                max_hp=50,
                hp=50,
                max_mp=40,
                mp=40,
                defenses={"physical": 10, "magic": 8},
                initiative=9,
                weapon_accuracy_attributes=["DEX", "MIG"],
                weapon_damage=10,
                weapon_type="physical",
                traits=["ally", "humanoid"],
            )
        )
        self.app.conflict_manager.register_enemy(name, EnemyRank.SOLDIER)

    def _prepare_composite_adventure_opening(
        self,
    ) -> tuple[GMToolExecutionContext, object]:
        self.service.adventure_opening_flow_mode = "optimized"
        world = self.app.session_zero_manager.state.world
        world.selected_first_act_summary = "第一幕从卡里巴村监狱越狱开始。"
        world.starting_region = "卡里巴村"
        world.hero_drafts["阿凛"] = HeroDraft(
            player_name="阿凛",
            hero_name="伊莉雅",
            confirmed=True,
        )
        contract = SessionDramaticContract(
            title="卡里巴村的迟响",
            location="卡里巴村",
            dramatic_question="英雄会如何离开封印松动的牢区",
            opening_disruption="牢门上的封印突然暗了一瞬。",
            signature_image="积水里的暗金符文正向两扇牢门之间游动。",
            opposition_goal="典狱方要在记录外泄前恢复封印",
            dilemma="立刻离开，或冒险留下证据帮助其他囚犯",
            closure_requirement="英雄离开牢区且监狱异状产生公开后果",
            irreversible_change="至少一处封印、人物去向或证据状态被改变",
            ending_echo="离开时再次看见暗金符文造成的变化",
            situation_facts=["牢门封印在地下震动后短暂错位"],
            flexible_secrets=["监狱地下有人抽取囚犯的灵魂残留"],
            escalation_ladder=["值夜守卫抵达", "地下装置开始销毁记录"],
            possible_payoffs=["带着转运牌离开", "释放一名知情囚犯"],
            potential_scenes=[
                SessionSceneOpportunity(
                    scene_key="opening",
                    scene_role="strong_start",
                    title="封印错位的牢门",
                    location="卡里巴村",
                    purpose="在守卫抵达前判断封印异常并决定如何离开",
                    required_elements=["错位的牢门符文", "走近的守卫灯光"],
                )
            ],
        )
        plan = self.app.story_arc_manager.state.current_pacing_plan
        plan.dramatic_contract = contract
        self.service.session_gates.activate(
            "runtime-tool-test",
            "group-1",
            "s1",
            status="session_zero",
            reason="第零章进行中",
        )
        self.app.session_zero_manager.set_chapter_one_transition(
            "invited",
            speaker="时悠",
            evidence="第零章已经准备好了。现在进入第一章吗？",
        )
        context = runtime_context("好，现在开始第一章。")
        context.gate_status = "session_zero"
        return context, plan

    def test_conflict_prewarm_replaces_social_placeholder_with_executable_sheet(self) -> None:
        self.app.character_manager.add(
            Character(
                name="雾中守卫",
                attributes={},
                max_hp=1,
                hp=1,
                max_mp=0,
                mp=0,
                traits=["npc"],
            )
        )
        self.app.world_state.ensure_npc_persona(
            "雾中守卫",
            public_identity="守卫",
            role_in_story="拦住旧路的武装守卫",
            traits=["警觉", "守序", "强硬", "忠诚"],
        )

        committed = self.app.ensure_npc_combat_profiles(
            ["雾中守卫"],
            combat_side="enemy",
        )

        self.assertEqual(committed, ["雾中守卫"])
        combatant = self.app.character_manager.get("雾中守卫")
        self.assertTrue(combatant.npc_attacks)
        self.assertGreater(combatant.max_hp, 1)
        self.assertTrue(combatant.npc_source_template)

    def test_hidden_prepared_npc_gets_private_blueprint_without_entering_scene(self) -> None:
        scene = self.app.scene_manager.start_scene(
            "白花碑驿站",
            location="风铃廊",
            participants=["伊莉雅"],
        )
        self.app.scene_frame_manager.current_frame = SceneFrame(
            scene_key=scene.scene_id,
            scene_name=scene.name,
            source_scene_id=scene.scene_id,
            location=scene.location,
            required_opening_npc_names=["守望会会长"],
            session_npc_records=[
                {
                    "name": "守望会会长",
                    "public_role": "白花守望会会长",
                    "goal_now": "确认英雄不会伤害失忆旅人",
                },
                {
                    "name": "灰衣追猎者",
                    "public_role": "财团追猎者",
                    "goal_now": "在旧路出口伏击携带遗物的人",
                    "private_secret": "他尚未抵达驿站",
                },
            ],
        )

        self.app._ensure_required_opening_npc_personas()
        jobs = list(self.app.npc_blueprint_designer._jobs)
        for job_id in jobs:
            self.app.npc_blueprint_designer.wait(job_id, timeout=3)

        self.assertIn("守望会会长", scene.participants)
        self.assertNotIn("灰衣追猎者", scene.participants)
        self.assertEqual(
            self.app.world_state.npc_personas["灰衣追猎者"].current_location,
            "",
        )
        self.assertIn("守望会会长", self.app.world_state.npc_combat_blueprints)
        self.assertIn("灰衣追猎者", self.app.world_state.npc_combat_blueprints)
        prewarmed = self.app.world_state.npc_combat_blueprints["灰衣追猎者"]

        committed = self.app.ensure_npc_combat_profiles(
            ["灰衣追猎者"],
            combat_side="enemy",
        )

        self.assertEqual(committed, ["灰衣追猎者"])
        self.assertEqual(
            self.app.world_state.npc_combat_blueprints[
                "灰衣追猎者"
            ].blueprint_id,
            prewarmed.blueprint_id,
        )
        self.assertEqual(
            self.app.character_manager.get("灰衣追猎者").npc_source_template,
            prewarmed.source_template,
        )

    def test_scene_start_materializes_required_opening_cast_before_first_reply(self) -> None:
        contract = SessionDramaticContract(
            title="白花碑驿站的迟响",
            location="白花碑驿站",
            potential_scenes=[
                SessionSceneOpportunity(
                    scene_key="opening",
                    scene_role="strong_start",
                    title="风铃廊问路",
                    location="白花碑驿站",
                    required_npc_names=["白花守望会会长"],
                    npc_names=["白花守望会会长", "失忆旅人"],
                )
            ],
            important_npcs=[
                SessionNPCRole(
                    name="白花守望会会长",
                    public_role="白花守望会会长",
                    goal_now="确认来客不会伤害旅人",
                )
            ],
        )
        self.app.story_arc_manager.state.current_pacing_plan.dramatic_contract = contract

        scene = self.app.start_scene(
            "第一章：白花碑驿站的迟响",
            location="白花碑驿站",
            participants=["伊莉雅"],
        )

        self.assertIn("白花守望会会长", scene.participants)
        persona = self.app.world_state.npc_personas["白花守望会会长"]
        self.assertEqual(persona.current_location, "白花碑驿站")
        self.assertEqual(persona.last_seen_scene, scene.scene_id)
        self.assertIn(
            "白花守望会会长",
            self.app.scene_frame_manager.current_frame.required_opening_npc_names,
        )

    def test_parent_location_materializes_required_child_scene_cast(self) -> None:
        contract = SessionDramaticContract(
            title="白花碑驿站的迟响",
            location="白花碑驿站",
            potential_scenes=[
                SessionSceneOpportunity(
                    scene_key="opening",
                    scene_role="strong_start",
                    title="风铃廊问路",
                    location="白花碑驿站·风铃廊",
                    required_npc_names=["白花守望会会长·梅芙", "失忆旅人"],
                    npc_names=["白花守望会会长·梅芙", "失忆旅人"],
                )
            ],
            important_npcs=[
                SessionNPCRole(
                    name="白花守望会会长·梅芙",
                    public_role="白花守望会会长",
                    goal_now="确认来客不会伤害旅人",
                ),
                SessionNPCRole(
                    name="失忆旅人",
                    public_role="失忆旅人",
                    goal_now="辨认尚未消失的记忆",
                ),
            ],
        )
        self.app.story_arc_manager.state.current_pacing_plan.dramatic_contract = contract

        scene = self.app.start_scene(
            "第一章：白花碑驿站的迟响",
            location="白花碑驿站",
            participants=["伊莉雅"],
        )

        self.assertEqual(scene.session_opportunity_key, "opening")
        self.assertEqual(
            self.app.scene_frame_manager.current_frame.session_opportunity_title,
            "风铃廊问路",
        )
        self.assertIn("白花守望会会长·梅芙", scene.participants)
        self.assertIn("失忆旅人", scene.participants)
        self.assertEqual(
            self.app.world_state.npc_personas["白花守望会会长·梅芙"].last_seen_scene,
            scene.scene_id,
        )
        self.assertEqual(
            self.app.world_state.npc_personas["失忆旅人"].current_location,
            "白花碑驿站",
        )

    def test_start_conflict_can_explicitly_type_a_collective_combatant(self) -> None:
        self.app.scene_manager.start_scene(
            "卡里巴监狱冲突",
            location="牢房走廊",
            participants=["伊莉雅", "两名看守"],
        )
        context = runtime_context("两名看守一起拦住伊莉雅。")
        self._force_successful_initiative()

        receipt = self.service.gm_runtime_tools.start_conflict(
            context,
            {
                "scene_name": "卡里巴监狱冲突",
                "pcs": ["伊莉雅"],
                "allied_npcs": [],
                "enemies": ["两名看守"],
                "collective_npcs": ["两名看守"],
                "leader": "伊莉雅",
                "objective": "突破看守封锁",
                "public_opening": "两名看守横过短棍，封住牢房走廊。",
                "evidence": "两名看守一起拦住伊莉雅。",
            },
        )

        self.assertTrue(receipt.ok, receipt)
        persona = self.app.world_state.npc_personas["两名看守"]
        self.assertEqual(persona.entity_kind, "collective")

    def test_start_conflict_waits_for_each_unanswered_pc_support_choice(self) -> None:
        # 本例只验证“支援窗口落定后启动冲突”。固定骰子避免团队
        # 先攻偶然大成功另外打开机会窗口，把另一条正确规则分支混进本测试。
        self.app.interceptor.rules_engine._rng.seed(8)
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
        self._add_test_enemy("财团机兵")
        self.app.scene_manager.start_scene(
            "白花碑驿站伏击",
            location="旧路闸门",
            participants=["伊莉雅", "赛璃", "财团机兵"],
        )
        message = "财团机兵拔出武器封住旧路，冲突爆发。"

        pending = self.service.gm_runtime_tools.start_conflict(
            runtime_context(message),
            {
                "scene_name": "白花碑驿站伏击",
                "pcs": ["伊莉雅", "赛璃"],
                "allied_npcs": [],
                "enemies": ["财团机兵"],
                "leader": "伊莉雅",
                "objective": "突破财团封锁",
                "public_opening": "财团机兵拔出武器，封住旧路。",
                "evidence": message,
            },
        )

        self.assertTrue(pending.ok, pending.message)
        self.assertTrue(pending.result["initiative_support_pending"])
        self.assertFalse(self.app.conflict_manager.state.active)
        window = self.app.interceptor.decision_window_manager.pending(
            kind="initiative_support",
            owner="赛璃",
        )[0]

        resolved = self.service.gm_gameplay_tools.resolve_rule_window(
            runtime_context("赛璃支援这次团队先攻。", speaker="赛璃"),
            {
                "action_type": "ResolveDecision",
                "actor": "赛璃",
                "window_id": window.window_id,
                "choice": "support",
                "details": {},
                "evidence": "赛璃支援这次团队先攻。",
            },
        )

        self.assertTrue(resolved.ok, resolved.message)
        self.assertTrue(self.app.conflict_manager.state.active)
        self.assertEqual(resolved.result["initiative_supporters"], ["赛璃"])
        self.assertNotIn("财团机兵拔出武器，封住旧路。", resolved.public_fallback_reply)

    def test_start_conflict_collects_each_players_own_support_choice(self) -> None:
        for name in ("洛岚", "赛璃"):
            self.app.character_manager.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                    max_hp=40,
                    hp=40,
                    max_mp=40,
                    mp=40,
                    traits=["pc"],
                )
            )
        self.app.world_state.world_profile.hero_drafts.update(
            {
                "阿凛": HeroDraft(player_name="阿凛", hero_name="伊莉雅"),
                "白河": HeroDraft(player_name="白河", hero_name="洛岚"),
                "南星": HeroDraft(player_name="南星", hero_name="赛璃"),
            }
        )
        self._add_test_enemy("财团机兵")
        message = "财团机兵封住旧路，冲突爆发。"
        pending = self.service.gm_runtime_tools.start_conflict(
            runtime_context(message),
            {
                "scene_name": "旧路伏击",
                "pcs": ["伊莉雅", "洛岚", "赛璃"],
                "enemies": ["财团机兵"],
                "leader": "伊莉雅",
                "objective": "突破财团封锁",
                "public_opening": "财团机兵封住旧路。",
                "evidence": message,
            },
        )

        self.assertTrue(pending.ok, pending.message)
        windows = {
            item.owner: item
            for item in self.app.interceptor.decision_window_manager.pending(
                kind="initiative_support"
            )
        }
        self.assertEqual(set(windows), {"洛岚", "赛璃"})

        impersonated = self.service.gm_gameplay_tools.resolve_rule_window(
            runtime_context("洛岚替赛璃选择支援。", speaker="白河"),
            {
                "action_type": "ResolveDecision",
                "actor": "赛璃",
                "window_id": windows["赛璃"].window_id,
                "choice": "support",
                "details": {},
                "evidence": "洛岚替赛璃选择支援。",
            },
        )
        self.assertFalse(impersonated.ok)
        self.assertEqual(
            impersonated.error_code,
            "ACTOR_NOT_CONTROLLED_BY_SPEAKER",
        )
        self.assertEqual(windows["赛璃"].status.value, "pending")

        skipped = self.service.gm_gameplay_tools.resolve_rule_window(
            runtime_context("赛璃不支援团队先攻。", speaker="南星"),
            {
                "action_type": "ResolveDecision",
                "actor": "赛璃",
                "window_id": windows["赛璃"].window_id,
                "choice": "skip",
                "details": {},
                "evidence": "赛璃不支援团队先攻。",
            },
        )
        self.assertTrue(skipped.ok, skipped.message)
        self.assertFalse(self.app.conflict_manager.state.active)
        self.assertEqual(skipped.result["waiting_for"], ["洛岚"])

        self._force_successful_initiative("洛岚")
        started = self.service.gm_gameplay_tools.resolve_rule_window(
            runtime_context("洛岚支援团队先攻。", speaker="白河"),
            {
                "action_type": "ResolveDecision",
                "actor": "洛岚",
                "window_id": windows["洛岚"].window_id,
                "choice": "support",
                "details": {},
                "evidence": "洛岚支援团队先攻。",
            },
        )
        self.assertTrue(started.ok, started.message)
        self.assertTrue(self.app.conflict_manager.state.active)
        self.assertEqual(started.result["initiative_supporters"], ["洛岚"])

    def test_start_conflict_schema_does_not_let_gm_choose_supporters(self) -> None:
        schema = next(
            item
            for item in self.service.gm_tool_registry.schemas()
            if item["name"] == "start_conflict"
        )
        self.assertNotIn("supporters", schema["parameters"]["properties"])

    def test_last_support_choice_rolls_back_when_conflict_start_fails(self) -> None:
        self.app.character_manager.add(
            Character(
                name="赛璃",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                max_hp=40,
                hp=40,
                max_mp=40,
                mp=40,
                traits=["pc"],
            )
        )
        self.app.world_state.world_profile.hero_drafts.update(
            {
                "阿凛": HeroDraft(player_name="阿凛", hero_name="伊莉雅"),
                "南星": HeroDraft(player_name="南星", hero_name="赛璃"),
            }
        )
        self._add_test_enemy("财团机兵")
        pending = self.service.gm_runtime_tools.start_conflict(
            runtime_context("财团机兵封住旧路。"),
            {
                "scene_name": "旧路伏击",
                "pcs": ["伊莉雅", "赛璃"],
                "enemies": ["财团机兵"],
                "leader": "伊莉雅",
                "objective": "突破财团封锁",
                "public_opening": "财团机兵封住旧路。",
                "evidence": "财团机兵封住旧路。",
            },
        )
        window = self.app.interceptor.decision_window_manager.pending(
            kind="initiative_support",
            owner="赛璃",
        )[0]
        failed_start = GMToolReceipt.failure(
            "start_conflict",
            "FORCED_START_FAILURE",
            "测试用启动失败。",
            "保留选择后重试。",
        )

        with patch.object(
            self.service.gm_runtime_tools,
            "start_conflict",
            return_value=failed_start,
        ):
            resolved = self.service.gm_gameplay_tools.resolve_rule_window(
                runtime_context("赛璃支援团队先攻。", speaker="南星"),
                {
                    "action_type": "ResolveDecision",
                    "actor": "赛璃",
                    "window_id": window.window_id,
                    "choice": "support",
                    "details": {},
                    "evidence": "赛璃支援团队先攻。",
                },
            )

        self.assertFalse(resolved.ok)
        restored = self.app.interceptor.decision_window_manager.find_pending(
            window_id=window.window_id
        )
        self.assertIsNotNone(restored)
        self.assertFalse(self.app.conflict_manager.state.active)

    def test_start_conflict_rejects_collective_not_listed_as_combatant(self) -> None:
        self.app.scene_manager.start_scene(
            "卡里巴监狱冲突",
            location="牢房走廊",
            participants=["伊莉雅", "守卫甲"],
        )
        context = runtime_context("守卫甲拦住伊莉雅。")

        receipt = self.service.gm_runtime_tools.start_conflict(
            context,
            {
                "scene_name": "卡里巴监狱冲突",
                "pcs": ["伊莉雅"],
                "allied_npcs": [],
                "enemies": ["守卫甲"],
                "collective_npcs": ["两名看守"],
                "leader": "伊莉雅",
                "objective": "突破看守封锁",
                "public_opening": "守卫甲封住牢房走廊。",
                "evidence": "守卫甲拦住伊莉雅。",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "COLLECTIVE_COMBATANT_UNKNOWN")

    def test_start_conflict_cannot_fold_named_enemies_into_collective(self) -> None:
        for name in ("财团机兵", "财团狙击手", "辉钢财团巡逻队"):
            self._add_test_enemy(name)
        self.app.scene_manager.start_scene(
            "白花碑驿站伏击",
            location="旧路闸门",
            participants=["伊莉雅", "财团机兵", "财团狙击手"],
        )
        message = "伊莉雅看见财团机兵和财团狙击手封路，请进入冲突。"

        receipt = self.service.gm_runtime_tools.start_conflict(
            runtime_context(message),
            {
                "scene_name": "白花碑驿站伏击",
                "pcs": ["伊莉雅"],
                "enemies": ["辉钢财团巡逻队"],
                "collective_npcs": ["辉钢财团巡逻队"],
                "leader": "伊莉雅",
                "objective": "突破封锁",
                "public_opening": "财团封锁线压住旧路。",
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "EXPLICIT_ENEMY_ROSTER_CHANGED")
        self.assertEqual(
            receipt.result["omitted_named_enemies"],
            ["财团机兵", "财团狙击手"],
        )

    def _force_successful_initiative(self, *supporters: str) -> None:
        for actor in ("伊莉雅", *supporters):
            self.app.interceptor.rules_engine.force_next_check_outcome(
                RollOutcome(
                    actor=actor,
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

    def test_start_scene_commits_private_situation_and_locked_public_opening(self) -> None:
        message = "大家沿旧路进入潮声钟塔。"
        receipt = self.service.gm_runtime_tools.start_scene(
            runtime_context(message),
            {
                "name": "潮声钟塔",
                "scene_type": "standard",
                "location": "潮声钟塔一层",
                "participants": ["伊莉雅"],
                "objective": "找到失忆旅人名字被抹去的原因",
                "private_situation": {
                    "premise": "旧钟会回应真实姓名",
                    "current_pressure": "潮水正从地下井道上涨",
                    "visible_elements": ["浸水台阶", "停摆的七面铜钟"],
                    "clue_pool": ["第七面钟内刻着被刮掉的姓氏"],
                    "secrets": ["会长亲手刮掉了旅人的姓氏"],
                    "story_outline": ["先确认钟塔异常，再决定救人还是追查会长"],
                },
                "public_opening": "潮水沿石阶一层层漫上来，七面铜钟却都停在同一刻。你们刚踏进塔门，最里面那面钟轻轻响了一声。",
                "player_handoff": "潮水还在上涨，伊莉雅，你先做什么？",
                "evidence": "沿旧路进入潮声钟塔",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertTrue(receipt.lock_public_reply)
        self.assertEqual(self.app.scene_manager.current_scene.scene_type, SceneType.STANDARD)
        frame = self.app.scene_frame_manager.current_frame
        self.assertEqual(frame.secrets, ["会长亲手刮掉了旅人的姓氏"])
        self.assertNotIn("会长亲手", receipt.public_fallback_reply)
        self.assertTrue(receipt.public_fallback_reply.endswith("伊莉雅，你先做什么？"))

    def test_start_scene_atomically_restricts_equipment_access(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.equipment.append("钢匕首")
        self.app.interceptor.economy_manager.configure_loadout(
            "伊莉雅",
            {"main_hand": "钢匕首"},
            allow_armor=True,
        )
        message = "第一章从伊莉雅被关在卡里巴村监狱开始。"

        receipt = self.service.gm_runtime_tools.start_scene(
            runtime_context(message),
            {
                "name": "卡里巴村监狱",
                "scene_type": "standard",
                "location": "相邻石牢",
                "participants": ["伊莉雅"],
                "private_situation": {
                    "premise": "牢房封印短暂失效。",
                    "current_pressure": "看守正赶往牢区。",
                    "visible_elements": ["熄灭一瞬的符文", "走廊尽头的灯"],
                },
                "equipment_access_changes": [
                    {
                        "actor": "伊莉雅",
                        "mode": "restrict",
                        "items": ["钢匕首"],
                        "reason": "入狱时被收缴",
                        "location": "监狱证物柜",
                    }
                ],
                "public_opening": "夜雨敲在牢窗上，牢门符文忽然熄灭了一瞬。",
                "player_handoff": "走廊里正有人赶来，伊莉雅先做什么？",
                "evidence": "从伊莉雅被关在卡里巴村监狱开始",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(self.app.scene_manager.current_scene.name, "卡里巴村监狱")
        self.assertIn("钢匕首", ilya.equipment)
        self.assertIn("钢匕首", ilya.unavailable_equipment)
        self.assertEqual(ilya.equipped_main_hand, "徒手攻击")

    def test_invalid_opening_equipment_change_rolls_back_scene_and_loadout(self) -> None:
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.equipment.append("钢匕首")
        self.app.interceptor.economy_manager.configure_loadout(
            "伊莉雅",
            {"main_hand": "钢匕首"},
            allow_armor=True,
        )
        message = "第一章从伊莉雅被关在卡里巴村监狱开始。"

        receipt = self.service.gm_runtime_tools.start_scene(
            runtime_context(message),
            {
                "name": "卡里巴村监狱",
                "scene_type": "standard",
                "location": "相邻石牢",
                "participants": ["伊莉雅"],
                "private_situation": {"premise": "牢房封印短暂失效。"},
                "equipment_access_changes": [
                    {
                        "actor": "伊莉雅",
                        "mode": "restrict",
                        "items": ["并不存在的王家宝剑"],
                    }
                ],
                "public_opening": "夜雨敲在牢窗上。",
                "player_handoff": "伊莉雅先做什么？",
                "evidence": "从伊莉雅被关在卡里巴村监狱开始",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "EQUIPMENT_ACCESS_ITEM_NOT_OWNED")
        self.assertIsNone(self.app.scene_manager.current_scene)
        self.assertEqual(ilya.equipped_main_hand, "钢匕首")
        self.assertEqual(ilya.unavailable_equipment, {})

    def test_start_scene_requires_player_handoff_before_committing(self) -> None:
        message = "大家沿旧路进入潮声钟塔。"
        receipt = self.service.gm_runtime_tools.start_scene(
            runtime_context(message),
            {
                "name": "潮声钟塔",
                "scene_type": "standard",
                "location": "潮声钟塔一层",
                "participants": ["伊莉雅"],
                "private_situation": {
                    "current_pressure": "潮水正从地下井道上涨",
                    "visible_elements": ["浸水台阶", "停摆的七面铜钟"],
                },
                "public_opening": "潮水已经漫上第一层石阶，最里面那面铜钟忽然响了一声。",
                "evidence": "沿旧路进入潮声钟塔",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "PLAYER_HANDOFF_REQUIRED")
        self.assertIsNone(self.app.scene_manager.current_scene)

    def test_start_conflict_rejects_fallen_pc_in_same_scene(self) -> None:
        self.app.character_manager.add(
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
        self.app.scene_manager.start_scene(
            "断桥",
            SceneType.STANDARD,
            participants=["伊莉雅", "帝国机兵"],
        )
        self.app.conflict_manager.state.fallen_pcs["伊莉雅"] = "分离：被冲到桥下"
        self.app.character_manager.get("伊莉雅").hp = 0

        receipt = self.service.gm_runtime_tools.start_conflict(
            runtime_context("帝国机兵扑了上来。"),
            {
                "scene_name": "断桥伏击",
                "pcs": ["伊莉雅"],
                "enemies": ["帝国机兵"],
                "leader": "伊莉雅",
                "objective": "撑过伏击",
                "public_opening": "帝国机兵从桥墩后扑了出来。",
                "evidence": "帝国机兵扑了上来",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "FALLEN_PC_STILL_UNCONSCIOUS")
        self.assertFalse(self.app.conflict_manager.state.active)

    def test_start_scene_rejects_empty_participants(self) -> None:
        message = "大家进入白花碑驿站。"
        receipt = self.service.gm_runtime_tools.start_scene(
            runtime_context(message),
            {
                "name": "白花碑驿站",
                "scene_type": "standard",
                "location": "白花碑驿站",
                "participants": [],
                "private_situation": {"premise": "驿站正等待来客。"},
                "public_opening": "驿站大门在暮色里半掩着。",
                "player_handoff": "你们先从哪里看起？",
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "NONEMPTY_ARRAY_REQUIRED")
        self.assertIsNone(self.app.scene_manager.current_scene)

    def test_start_scene_cannot_skip_an_active_conflict(self) -> None:
        self.app.start_conflict_scene("断桥之战", ["伊莉雅"])
        message = "伊莉雅想直接离开断桥进入营地。"

        receipt = self.service.gm_runtime_tools.start_scene(
            runtime_context(message),
            {
                "name": "西岸营地",
                "scene_type": "standard",
                "location": "西岸营地",
                "participants": ["伊莉雅"],
                "private_situation": {"premise": "营地暂时安全。"},
                "public_opening": "篝火在西岸营地里亮着。",
                "player_handoff": "伊莉雅，你先做什么？",
                "evidence": "进入营地",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "CONFLICT_ACTIVE")
        self.assertTrue(self.app.conflict_manager.state.active)
        self.assertEqual(self.app.scene_manager.current_scene.name, "断桥之战")

    def test_generic_scene_tools_cannot_skip_an_active_journey(self) -> None:
        self.app.scene_manager.start_scene(
            "白花碑驿站到钟鸣公国",
            SceneType.TRAVEL,
            location="白花碑驿站外的旧路",
            participants=["伊莉雅"],
        )
        self.app.travel_manager.begin_journey(
            journey_id="journey-1",
            origin="白花碑驿站",
            destination="钟鸣公国",
            threat_levels=["low"],
            party_names=["伊莉雅"],
        )

        started = self.service.gm_runtime_tools.start_scene(
            runtime_context("伊莉雅不走旅行流程，直接抵达钟鸣公国。"),
            {
                "name": "钟鸣公国城门",
                "scene_type": "standard",
                "location": "钟鸣公国",
                "participants": ["伊莉雅"],
                "private_situation": {},
                "public_opening": "城门已经在她面前打开。",
                "player_handoff": "伊莉雅，你进城后先做什么？",
                "evidence": "直接抵达钟鸣公国",
            },
        )
        ended = self.service.gm_runtime_tools.end_scene(
            runtime_context("直接结束这段旅途。"),
            {
                "summary": "旅途结束。",
                "public_reply": "已经抵达。",
                "evidence": "直接结束这段旅途",
            },
        )

        self.assertFalse(started.ok)
        self.assertEqual(
            started.error_code,
            "ACTIVE_JOURNEY_REQUIRES_TRAVEL_TOOL",
        )
        self.assertFalse(ended.ok)
        self.assertEqual(
            ended.error_code,
            "ACTIVE_JOURNEY_REQUIRES_TRAVEL_TOOL",
        )
        self.assertIsNotNone(self.app.travel_manager.active_journey)
        self.assertEqual(
            self.app.scene_manager.current_scene.scene_type,
            SceneType.TRAVEL,
        )

    def test_generic_scene_end_cannot_discard_an_active_dungeon(self) -> None:
        scene = self.app.scene_manager.start_scene(
            "镜之水道",
            SceneType.DUNGEON,
            location="旧王国地下",
            participants=["伊莉雅"],
        )
        self.app.dungeon_manager.state.active = True
        self.app.dungeon_manager.state.name = "镜之水道"
        self.app.clock_manager.add(
            Clock(
                name="水道完全淹没",
                max_segments=6,
                clock_type="threat",
            )
        )

        receipt = self.service.gm_runtime_tools.end_scene(
            runtime_context("不处理出口，直接结束地下城场景。"),
            {
                "summary": "地下城结束。",
                "public_reply": "你们已经离开。",
                "evidence": "直接结束地下城场景",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "ACTIVE_DUNGEON_REQUIRES_DUNGEON_TOOL",
        )
        self.assertEqual(self.app.scene_manager.current_scene.scene_id, scene.scene_id)
        self.assertTrue(self.app.clock_manager.exists("水道完全淹没"))

    def test_system_resolution_beat_ending_scene_emits_authoritative_closure(self) -> None:
        self.app.scene_manager.start_scene(
            "旧路复核",
            SceneType.STANDARD,
            location="白花碑驿站·旧路闸门",
            participants=["伊莉雅"],
            objective="决定队伍能否带着证据离开",
        )
        context = runtime_context("【最终收束窗口】")
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "free_scene_beat",
                "heartbeat_require_local_change": True,
                "heartbeat_require_local_resolution": True,
            }
        )

        receipt = self.service.gm_runtime_tools.end_scene(
            context,
            {
                "summary": "队伍保住旅人并取得受干扰的记录，但旧路仍未开放。",
                "public_reply": "风铃最后响了一次，旅人仍站在你们身边。",
                "evidence": "【最终收束窗口】",
            },
        )

        self.assertTrue(receipt.ok)
        self.assertEqual(len(receipt.pacing_events), 1)
        event = receipt.pacing_events[0]
        self.assertFalse(event.player_action)
        self.assertTrue(event.local_question_changed)
        self.assertTrue(event.local_question_resolved)
        self.assertTrue(event.signature_image_evolved)
        self.assertEqual(event.gm_beat_purpose, "free_scene_beat")
        self.assertIn("旧路仍未开放", event.consequence)

    def test_start_adventure_grants_typed_scene_followup_without_legacy_opening(self) -> None:
        world = self.app.session_zero_manager.state.world
        world.selected_first_act_summary = "第一幕从卡里巴村监狱越狱开始。"
        world.starting_region = "卡里巴村"
        world.first_act_question_answers = {
            "你们为什么会被关起来？": ["诺艾尔因盗窃男爵藏品被捕。"],
        }
        opening = "潮雾漫过白花碑驿站的门槛，失名旅人抬头望向迟响的风铃。"
        with patch.object(
            self.service,
            "_handle_gate_signal",
            return_value={"blocked": False, "reply": opening},
        ) as handle_gate:
            receipt = self.service.gm_runtime_tools.start_session(
                runtime_context("大家都同意进入第一章，请先描述现场。"),
                {
                    "phase": "adventure",
                    "reason": "全桌明确同意进入第一章",
                    "evidence": "大家都同意进入第一章",
                },
            )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertTrue(receipt.lock_public_reply)
        self.assertEqual(receipt.public_fallback_reply, "")
        self.assertTrue(receipt.result["adventure_opening_required"])
        self.assertEqual(receipt.result["allowed_followup_tools"], ["start_scene"])
        self.assertEqual(
            receipt.result["opening_contract"]["selected_first_act_summary"],
            "第一幕从卡里巴村监狱越狱开始。",
        )
        self.assertEqual(
            receipt.result["opening_contract"]["starting_region"],
            "卡里巴村",
        )
        self.assertEqual(
            receipt.result["opening_contract"]["setup_answers"],
            {"你们为什么会被关起来？": ["诺艾尔因盗窃男爵藏品被捕。"]},
        )
        self.assertTrue(receipt.result["session_situation_contract"]["title"])
        self.assertTrue(
            receipt.result["session_situation_contract"]["clue_routes"]
        )
        self.assertTrue(
            receipt.result["session_situation_contract"]["potential_scenes"]
        )
        self.assertTrue(
            receipt.result["session_situation_contract"]["flexible_secrets"]
        )
        self.assertIn("opening_character_state", receipt.result)
        self.assertIn("opening_equipment_instruction", receipt.result)
        payload = handle_gate.call_args.args[0]
        self.assertTrue(payload["defer_adventure_opening"])

    def test_composite_start_adventure_commits_one_locked_opening_scene(self) -> None:
        context, plan = self._prepare_composite_adventure_opening()
        runtime_tools = self.service.gm_runtime_tools

        with (
            patch.object(
                self.service,
                "_adventure_readiness_snapshot",
                return_value={"ready": True},
            ),
            patch.object(
                self.service,
                "_adventure_start_blockers",
                return_value={},
            ),
            patch.object(
                self.app,
                "ensure_world_map_for_adventure",
                return_value={"status": "not_ready"},
            ),
            patch.object(
                self.app.campaign_pacing_manager,
                "refresh_plan",
                return_value=plan,
            ),
            patch.object(
                runtime_tools,
                "start_scene",
                wraps=runtime_tools.start_scene,
            ) as start_scene,
        ):
            receipt = self.service.gm_tool_registry.execute(
                "start_adventure",
                {"reason": "玩家接受了第一章邀请"},
                context,
            )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.tool_name, "start_adventure")
        self.assertTrue(receipt.state_changed)
        self.assertTrue(receipt.lock_public_reply)
        self.assertTrue(receipt.public_fallback_reply)
        self.assertTrue(receipt.result["required_followup_resolved"])
        self.assertEqual(receipt.result["required_followup_tools"], [])
        self.assertEqual(start_scene.call_count, 1)
        self.assertEqual(len(self.app.scene_manager.active_scenes()), 1)
        self.assertEqual(self.app.scene_manager.history, [])
        self.assertEqual(
            self.app.scene_manager.current_scene.participants,
            ["伊莉雅"],
        )

    def test_composite_opening_keeps_deterministic_reveals_when_author_is_sparse(
        self,
    ) -> None:
        context, plan = self._prepare_composite_adventure_opening()
        runtime_tools = self.service.gm_runtime_tools

        class SparseOpeningWriter:
            available = True

            @staticmethod
            def compose_scene_opening(**_kwargs: object) -> SceneOpeningComposition:
                return SceneOpeningComposition(
                    private_situation={
                        "premise": "作者把镜头具体化到封印错位的牢门。",
                        "possible_reveals": ["符文错位与地下震动同时发生"],
                    },
                    public_opening=(
                        "卡里巴村。牢门上的封印突然暗了一瞬。"
                        "积水里的暗金符文正向两扇牢门之间游动。"
                    ),
                    player_handoff="守卫还没赶到——你们现在怎么做？",
                    model="deepseek-v4-flash",
                    used_model=True,
                )

        self.app.scene_creative_writer = SparseOpeningWriter()
        with (
            patch.object(
                self.service,
                "_adventure_readiness_snapshot",
                return_value={"ready": True},
            ),
            patch.object(
                self.service,
                "_adventure_start_blockers",
                return_value={},
            ),
            patch.object(
                self.app,
                "ensure_world_map_for_adventure",
                return_value={"status": "not_ready"},
            ),
            patch.object(
                self.app.campaign_pacing_manager,
                "refresh_plan",
                return_value=plan,
            ),
        ):
            receipt = self.service.gm_tool_registry.execute(
                "start_adventure",
                {"reason": "玩家接受了第一章邀请"},
                context,
            )

        self.assertTrue(receipt.ok, receipt.message)
        frame = self.app.scene_frame_manager.current_frame
        self.assertEqual(
            frame.premise,
            "作者把镜头具体化到封印错位的牢门。",
        )
        self.assertIn(
            "符文错位与地下震动同时发生",
            frame.possible_reveals,
        )
        self.assertGreaterEqual(len(frame.possible_reveals), 2)
        self.assertGreaterEqual(len(frame.clue_pool), 2)
        self.assertGreaterEqual(len(frame.visible_elements), 2)

    def test_composite_start_adventure_rejects_explicitly_withheld_consent(
        self,
    ) -> None:
        self.service.adventure_opening_flow_mode = "optimized"
        self.service.session_gates.activate(
            "runtime-tool-test",
            "group-1",
            "s1",
            status="session_zero",
            reason="第零章进行中",
        )
        context = runtime_context("等等，我还没准备好，先别开始第一章。")
        context.gate_status = "session_zero"
        runtime_tools = self.service.gm_runtime_tools

        with patch.object(runtime_tools, "start_session") as start_session:
            receipt = self.service.gm_tool_registry.execute(
                "start_adventure",
                {"reason": "玩家明确同意开章"},
                context,
            )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "CHAPTER_ONE_CONSENT_WITHHELD",
        )
        self.assertFalse(receipt.state_changed)
        start_session.assert_not_called()
        self.assertIsNone(self.app.scene_manager.current_scene)
        gate = self.service.session_gates.get(
            "runtime-tool-test",
            "group-1",
            "s1",
        )
        self.assertEqual(gate.status, "session_zero")

    def test_composite_start_adventure_rejects_unrelated_message_without_retry_or_state_change(
        self,
    ) -> None:
        context, _plan = self._prepare_composite_adventure_opening()
        context.metadata["current_message"] = "今天天气如何？"
        before_gate = self.service.session_gates.get(
            "runtime-tool-test",
            "group-1",
            "s1",
        )
        before_fabula = self.app.character_manager.get("伊莉雅").fabula_points

        receipt = self.service.gm_tool_registry.execute(
            "start_adventure",
            {"reason": "模型误判为开章同意"},
            context,
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "CHAPTER_ONE_CONSENT_REQUIRED")
        self.assertFalse(receipt.retryable)
        self.assertFalse(receipt.state_changed)
        self.assertEqual(context.gate_status, "session_zero")
        self.assertFalse(self.app.session_ledger.active)
        self.assertIsNone(self.app.scene_manager.current_scene)
        self.assertEqual(
            self.app.character_manager.get("伊莉雅").fabula_points,
            before_fabula,
        )
        after_gate = self.service.session_gates.get(
            "runtime-tool-test",
            "group-1",
            "s1",
        )
        self.assertEqual(after_gate, before_gate)

    def test_composite_scene_failure_restores_execution_context_gate(self) -> None:
        context, plan = self._prepare_composite_adventure_opening()
        runtime_tools = self.service.gm_runtime_tools

        with (
            patch.object(
                self.service,
                "_adventure_readiness_snapshot",
                return_value={"ready": True},
            ),
            patch.object(
                self.service,
                "_adventure_start_blockers",
                return_value={},
            ),
            patch.object(
                self.app,
                "ensure_world_map_for_adventure",
                return_value={"status": "not_ready"},
            ),
            patch.object(
                self.app.campaign_pacing_manager,
                "refresh_plan",
                return_value=plan,
            ),
            patch.object(
                runtime_tools,
                "start_scene",
                return_value=GMToolReceipt.failure(
                    "start_scene",
                    "OPENING_SCENE_PREP_INCOMPLETE",
                    "首场私有局面不完整。",
                    "补齐局面后重试。",
                    retryable=False,
                ),
            ),
        ):
            receipt = self.service.gm_tool_registry.execute(
                "start_adventure",
                {"reason": "玩家接受了第一章邀请"},
                context,
            )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "OPENING_SCENE_PREP_INCOMPLETE",
        )
        self.assertEqual(context.gate_status, "session_zero")
        self.assertNotIn("_gm_composite_adventure_start", context.metadata)
        self.assertFalse(self.app.session_ledger.active)
        self.assertIsNone(self.app.scene_manager.current_scene)
        gate = self.service.session_gates.get(
            "runtime-tool-test",
            "group-1",
            "s1",
        )
        self.assertEqual(gate.status, "session_zero")

    def test_outer_message_rollback_restores_jsonl_and_text_transcripts(self) -> None:
        context, plan = self._prepare_composite_adventure_opening()
        self.service.adventure_opening_flow_mode = "legacy"
        log_manager = self.runtime.log_manager
        log_manager.append_message(
            "runtime-tool-test",
            "s1",
            speaker="时悠",
            content="这是事务开始前的真实记录。",
            role="assistant",
            channel_id="group-1",
        )
        jsonl_path = log_manager.transcript_path("runtime-tool-test", "s1")
        text_path = log_manager.transcript_txt_path("runtime-tool-test", "s1")
        jsonl_before = jsonl_path.read_bytes()
        text_before = text_path.read_bytes()
        state_summary: dict[str, object] = {"gate": "session_zero"}
        transaction = GMMessageToolTransaction.begin(
            registry=self.service.gm_tool_registry,
            context=context,
            state_summary=state_summary,
        )
        arguments = {
            "phase": "adventure",
            "reason": "玩家明确同意进入第一章",
        }

        self.assertEqual(transaction.prepare("start_session", arguments), "")
        with (
            patch.object(
                self.service,
                "_adventure_start_blockers",
                return_value={},
            ),
            patch.object(
                self.app,
                "ensure_world_map_for_adventure",
                return_value={"status": "not_ready"},
            ),
            patch.object(
                self.app.campaign_pacing_manager,
                "refresh_plan",
                return_value=plan,
            ),
        ):
            receipt = self.service.gm_tool_registry.execute(
                "start_session",
                arguments,
                context,
            )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertIn(b"adventure", jsonl_path.read_bytes())
        self.assertIn("adventure", text_path.read_text(encoding="utf-8"))

        self.assertEqual(transaction.rollback(), "")

        self.assertEqual(jsonl_path.read_bytes(), jsonl_before)
        self.assertEqual(text_path.read_bytes(), text_before)
        self.assertNotIn("adventure", text_path.read_text(encoding="utf-8"))
        self.assertEqual(context.gate_status, "session_zero")
        self.assertFalse(self.app.session_ledger.active)
        self.assertEqual(
            self.service.session_gates.get(
                "runtime-tool-test",
                "group-1",
                "s1",
            ).status,
            "session_zero",
        )

    def test_end_session_summary_starts_only_after_outer_message_commit(self) -> None:
        summary_client = _BlockingSessionSummaryClient()
        self.runtime.log_manager.summarizer = LLMStorySummarizer(
            client=summary_client,  # type: ignore[arg-type]
            model="test-model",
        )
        context = runtime_context("今天先到这里。")
        context.gate_status = "pre_session"
        self.service.session_gates.activate(
            context.campaign_id,
            context.channel_id,
            context.session_id,
            status="pre_session",
        )
        self.runtime.log_manager.append_message(
            context.campaign_id,
            context.session_id,
            speaker=context.speaker,
            content="今天先到这里。",
        )
        state_summary: dict[str, object] = {"gate": "pre_session"}
        transaction = GMMessageToolTransaction.begin(
            registry=self.service.gm_tool_registry,
            context=context,
            state_summary=state_summary,
            side_effect_lock=self.runtime.transaction_lock,
        )
        arguments = {
            "title": "外层提交测试",
        }

        try:
            self.assertEqual(transaction.prepare("end_session", arguments), "")
            receipt = self.service.gm_tool_registry.execute(
                "end_session",
                arguments,
                context,
            )

            self.assertTrue(receipt.ok, receipt.message)
            self.assertEqual(
                receipt.result["summary_enrichment"]["status"],
                "deferred_until_outer_commit",
            )
            self.assertFalse(summary_client.started.wait(timeout=0.05))
            self.assertFalse(
                self.runtime.log_manager.summary_enrichment_path(
                    context.campaign_id,
                    context.session_id,
                ).exists()
            )

            transaction.mark_state_changed()
            self.assertEqual(transaction.commit(), "")

            self.assertEqual(self.runtime.state_version, 1)
            self.assertTrue(summary_client.started.wait(timeout=1))
            self.assertEqual(
                receipt.result["summary_enrichment"]["source_state_version"],
                1,
            )
            summary_client.release.set()
            status = self.runtime.log_manager.wait_for_summary_enrichment(
                context.campaign_id,
                context.session_id,
                timeout=2,
            )
            self.assertEqual(status["status"], "succeeded")
            self.assertEqual(len(summary_client.calls), 1)
        finally:
            summary_client.release.set()
            self.runtime.log_manager.shutdown_summary_enrichment(wait=True)

    def test_end_session_outer_message_rollback_discards_summary_and_artifacts(self) -> None:
        summary_client = _BlockingSessionSummaryClient()
        self.runtime.log_manager.summarizer = LLMStorySummarizer(
            client=summary_client,  # type: ignore[arg-type]
            model="test-model",
        )
        context = runtime_context("这次先不收团。")
        context.gate_status = "pre_session"
        self.service.session_gates.activate(
            context.campaign_id,
            context.channel_id,
            context.session_id,
            status="pre_session",
        )
        self.runtime.log_manager.append_message(
            context.campaign_id,
            context.session_id,
            speaker=context.speaker,
            content="这次先不收团。",
        )
        state_summary: dict[str, object] = {"gate": "pre_session"}
        transaction = GMMessageToolTransaction.begin(
            registry=self.service.gm_tool_registry,
            context=context,
            state_summary=state_summary,
            side_effect_lock=self.runtime.transaction_lock,
        )
        arguments = {
            "title": "外层回滚测试",
        }
        log_manager = self.runtime.log_manager

        try:
            self.assertEqual(transaction.prepare("end_session", arguments), "")
            receipt = self.service.gm_tool_registry.execute(
                "end_session",
                arguments,
                context,
            )

            self.assertTrue(receipt.ok, receipt.message)
            self.assertTrue(
                log_manager.summary_path(
                    context.campaign_id,
                    context.session_id,
                ).exists()
            )
            self.assertEqual(transaction.rollback(), "")

            self.assertFalse(summary_client.started.wait(timeout=0.05))
            for path in (
                log_manager.summary_path(context.campaign_id, context.session_id),
                log_manager.memory_path(context.campaign_id, context.session_id),
                log_manager.summary_enrichment_path(
                    context.campaign_id,
                    context.session_id,
                ),
            ):
                self.assertFalse(path.exists(), str(path))
            gate = self.service.session_gates.get(
                context.campaign_id,
                context.channel_id,
                context.session_id,
            )
            self.assertTrue(gate.active)
            self.assertEqual(gate.status, "pre_session")
            self.assertEqual(self.runtime.state_version, 0)
        finally:
            summary_client.release.set()
            self.runtime.log_manager.shutdown_summary_enrichment(wait=True)

    def test_start_adventure_propagates_core_deadline_to_session_prep(self) -> None:
        world = self.app.session_zero_manager.state.world
        world.selected_first_act_summary = "第一幕从卡里巴村监狱越狱开始。"
        world.starting_region = "卡里巴村"
        context = runtime_context("大家都同意进入第一章。")
        core_deadline = 999999999.0
        context.metadata["_gm_agent_deadline_monotonic"] = core_deadline
        concretizer = (
            self.app.campaign_pacing_manager.contract_planner.concretizer
        )

        with patch.object(
            self.service,
            "_handle_gate_signal",
            return_value={"blocked": False, "reply": "开场。"},
        ), patch.object(
            concretizer,
            "concretize",
            wraps=concretizer.concretize,
        ) as concretize:
            receipt = self.service.gm_runtime_tools.start_session(
                context,
                {
                    "phase": "adventure",
                    "reason": "全桌明确同意进入第一章",
                    "evidence": "大家都同意进入第一章",
                },
            )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(
            concretize.call_args.kwargs["deadline"],
            core_deadline,
        )

    def test_start_adventure_waits_for_player_owned_blockers_without_retry_loop(self) -> None:
        blockers = {
            "ready": False,
            "reason": "session_zero_and_character_creation_incomplete",
            "hero_creation": {
                "ready": False,
                "missing_by_player": {"诺艾尔": ["完整角色草稿"]},
            },
            "session_zero": {
                "ready": False,
                "missing": ["界限与帷幕"],
                "missing_world_fields": ["界限与帷幕"],
                "contribution_gaps": {},
            },
        }
        with patch.object(
            self.service,
            "_handle_gate_signal",
            return_value={
                "blocked": True,
                "blockers": blockers,
                "reply": "先补完角色和安全边界，我们再开第一章。",
            },
        ):
            receipt = self.service.gm_runtime_tools.start_session(
                runtime_context("其余设定由你补充，然后开始第一章。"),
                {
                    "phase": "adventure",
                    "reason": "玩家要求补充设定后开章",
                    "evidence": "开始第一章",
                },
            )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "ADVENTURE_START_BLOCKED")
        self.assertFalse(receipt.retryable)
        self.assertFalse(receipt.state_changed)
        self.assertTrue(receipt.result["player_input_required"])

    def test_start_adventure_keeps_retryable_world_only_blocker(self) -> None:
        blockers = {
            "ready": False,
            "reason": "session_zero_world_incomplete",
            "hero_creation": {"ready": True, "missing_by_player": {}},
            "session_zero": {
                "ready": False,
                "missing": ["主要国家或王国"],
                "missing_world_fields": ["主要国家或王国"],
                "contribution_gaps": {},
            },
        }
        with patch.object(
            self.service,
            "_handle_gate_signal",
            return_value={
                "blocked": True,
                "blockers": blockers,
                "reply": "还缺主要国家或王国。",
            },
        ):
            receipt = self.service.gm_runtime_tools.start_session(
                runtime_context("其余设定由你补充，然后开始第一章。"),
                {
                    "phase": "adventure",
                    "reason": "玩家要求补充设定后开章",
                    "evidence": "开始第一章",
                },
            )

        self.assertFalse(receipt.ok)
        self.assertTrue(receipt.retryable)
        self.assertFalse(receipt.state_changed)
        self.assertFalse(receipt.result["player_input_required"])

    def test_opening_character_state_exposes_exact_authoritative_loadout(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.equipment = ["细剑", "符文盾", "旅行装束"]
        hero.equipment_templates = {
            "细剑": "细剑",
            "符文盾": "符文盾",
            "旅行装束": "旅行装束",
        }
        hero.equipped_main_hand = "细剑"
        hero.equipped_shield = "符文盾"
        hero.equipped_armor = "旅行装束"
        hero.unavailable_equipment = {
            "细剑": {
                "reason": "入狱时被守卫收走",
                "source": "卡里巴村监狱",
            }
        }

        state = self.service.gm_runtime_tools._opening_character_state(
            self.runtime,
            {"confirmed_heroes": ["伊莉雅"]},
        )

        self.assertEqual(len(state), 1)
        self.assertEqual(state[0]["name"], "伊莉雅")
        self.assertEqual(
            state[0]["equipment_inventory"],
            ["细剑", "符文盾", "旅行装束"],
        )
        self.assertEqual(state[0]["equipment_templates"]["细剑"], "细剑")
        self.assertEqual(state[0]["equipped"]["main_hand"], "细剑")
        self.assertEqual(
            state[0]["unavailable_equipment"]["细剑"]["reason"],
            "入狱时被守卫收走",
        )

    def test_first_scene_must_apply_prepared_equipment_restrictions(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.equipment = ["钢匕首", "旅行装束"]
        hero.equipped_main_hand = "钢匕首"
        hero.equipped_armor = "旅行装束"
        world = self.app.session_zero_manager.state.world
        world.selected_first_act_summary = "第一幕从卡里巴村监狱越狱开始。"
        world.starting_region = "卡里巴村"
        world.first_act_opening_equipment_restrictions = [
            {
                "actor": "伊莉雅",
                "items": ["钢匕首"],
                "reason": "入狱时被守卫收缴",
                "location": "卡里巴村监狱值班室证物柜",
            }
        ]
        self.app.world_state.world_profile.first_act_opening_equipment_restrictions = [
            dict(item) for item in world.first_act_opening_equipment_restrictions
        ]
        context = runtime_context("大家同意进入第一章。")
        with patch.object(
            self.service,
            "_handle_gate_signal",
            return_value={"blocked": False},
        ):
            started = self.service.gm_runtime_tools.start_session(
                context,
                {
                    "phase": "adventure",
                    "reason": "全桌同意",
                    "evidence": "同意进入第一章",
                },
            )

        self.assertTrue(started.ok, started.message)
        self.assertEqual(
            started.result["opening_equipment_restrictions"][0]["items"],
            ["钢匕首"],
        )
        arguments = {
            "name": "卡里巴村监狱",
            "scene_type": "standard",
            "location": "相邻石牢",
            "participants": ["伊莉雅"],
            "private_situation": {
                "premise": "牢门封印在地下震动后短暂错位",
                "stakes": "英雄能否取得自由并保住追查监狱异状的证据",
                "current_pressure": "值夜守卫正带着钥匙赶往牢区",
                "visible_elements": ["错位的牢门符文", "走近的守卫灯光"],
                "possible_reveals": ["封印异常来自地下", "囚犯转运记录指向男爵庄园"],
            },
            "public_opening": "雨水敲着牢窗，门上的符文忽然暗了一瞬，走廊尽头正有灯光靠近。",
            "player_handoff": "伊莉雅，你现在怎么做？",
            "evidence": "同意进入第一章",
        }
        missing = self.service.gm_runtime_tools.start_scene(context, arguments)

        self.assertFalse(missing.ok)
        self.assertEqual(
            missing.error_code,
            "OPENING_EQUIPMENT_PLAN_NOT_APPLIED",
        )
        self.assertIsNone(self.app.scene_manager.current_scene)

        complete = self.service.gm_runtime_tools.start_scene(
            context,
            {
                **arguments,
                "equipment_access_changes": [
                    {
                        "actor": "伊莉雅",
                        "mode": "restrict",
                        "items": ["钢匕首"],
                        "reason": "入狱时被守卫收缴",
                        "location": "卡里巴村监狱值班室证物柜",
                    }
                ],
            },
        )

        self.assertTrue(complete.ok, complete.message)
        self.assertIn("钢匕首", hero.unavailable_equipment)
        self.assertEqual(hero.equipped_main_hand, "徒手攻击")

    def test_first_adventure_scene_requires_complete_but_flexible_private_prep(self) -> None:
        world = self.app.session_zero_manager.state.world
        world.selected_first_act_summary = "第一幕从卡里巴村监狱越狱开始。"
        world.starting_region = "卡里巴村"
        context = runtime_context("大家同意进入第一章。")
        with patch.object(
            self.service,
            "_handle_gate_signal",
            return_value={"blocked": False},
        ):
            started = self.service.gm_runtime_tools.start_session(
                context,
                {
                    "phase": "adventure",
                    "reason": "全桌同意",
                    "evidence": "同意进入第一章",
                },
            )

        self.assertTrue(started.ok, started.message)
        sparse = self.service.gm_runtime_tools.start_scene(
            context,
            {
                "name": "卡里巴村监狱",
                "scene_type": "standard",
                "location": "卡里巴村监狱",
                "participants": ["伊莉雅"],
                "private_situation": {},
                "public_opening": "暴雨敲着牢窗，走廊尽头传来钥匙声。",
                "player_handoff": "牢门的符文闪了一下，你准备怎么做？",
                "evidence": "同意进入第一章",
            },
        )

        self.assertFalse(sparse.ok)
        self.assertEqual(sparse.error_code, "OPENING_SCENE_PREP_INCOMPLETE")
        self.assertIn("开场即可接触", sparse.message)
        self.assertIsNone(self.app.scene_manager.current_scene)

        complete = self.service.gm_runtime_tools.start_scene(
            context,
            {
                "name": "卡里巴村监狱",
                "scene_type": "standard",
                "location": "卡里巴村监狱",
                "participants": ["伊莉雅"],
                "objective": "在守卫抵达前判断封印异常并决定如何离开",
                "private_situation": {
                    "premise": "牢门封印在地下震动后短暂错位",
                    "stakes": "英雄能否取得自由并保住追查监狱异状的线索",
                    "current_pressure": "值夜守卫正带着钥匙走近",
                    "dramatic_question": "英雄会以什么代价离开监狱",
                    "signature_image": "积水中的暗金符文在两间牢房之间游动",
                    "opposition_goal": "典狱方要恢复封印并隔离目击者",
                    "dilemma": "立刻逃走，或冒险留下证据帮助其他囚犯",
                    "closure_requirement": "英雄离开牢区且监狱异状产生公开后果",
                    "irreversible_change": "至少一处封印、人物去向或证据状态被改变",
                    "ending_echo": "离开时再次看见暗金符文，并呈现选择造成的变化",
                    "visible_elements": ["错位的牢门符文", "走近的守卫钥匙声"],
                    "clue_pool": ["排水沟里的转运牌", "值班簿缺失的一页"],
                    "secrets": ["监狱地下有人抽取囚犯的灵魂残留"],
                    "possible_reveals": ["封印异常来自地下", "转运物送往男爵庄园"],
                    "escalation_ladder": ["守卫抵达牢区", "地下装置开始销毁记录"],
                    "possible_payoffs": ["带着转运牌逃离", "释放一名知情囚犯"],
                },
                "public_opening": "暴雨敲着牢窗，积水中的暗金符文忽然朝两扇牢门之间游动，走廊尽头也传来钥匙声。",
                "player_handoff": "封印只错位了一瞬，你准备怎么做？",
                "evidence": "同意进入第一章",
            },
        )

        self.assertTrue(complete.ok, complete.message)
        frame = self.app.scene_frame_manager.current_frame
        self.assertEqual(frame.secrets, ["监狱地下有人抽取囚犯的灵魂残留"])
        self.assertEqual(len(frame.escalation_ladder), 2)

    def test_resume_adventure_keeps_existing_scene_without_forcing_new_opening(self) -> None:
        scene = self.app.start_scene(
            "水道外的避雨棚",
            SceneType.STANDARD,
            location="镜之水道入口",
            participants=["伊莉雅"],
            objective="整理守钟日志",
        )
        with patch.object(
            self.service,
            "_handle_gate_signal",
            return_value={"blocked": False},
        ):
            receipt = self.service.gm_runtime_tools.start_session(
                runtime_context("继续上次冒险。"),
                {
                    "phase": "adventure",
                    "reason": "玩家明确要求继续上次冒险",
                    "evidence": "继续上次冒险",
                },
            )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertFalse(receipt.lock_public_reply)
        self.assertTrue(receipt.result["adventure_resumed"])
        self.assertFalse(receipt.result["adventure_opening_required"])
        self.assertNotIn("required_followup_tools", receipt.result)
        self.assertEqual(
            self.app.scene_manager.current_scene.scene_id,
            scene.scene_id,
        )

    def test_start_adventure_persists_session_ledger_and_once_only_award(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.fabula_points = 0
        with (
            patch.object(self.service, "_adventure_start_blockers", return_value={}),
            patch.object(
                self.app,
                "ensure_world_map_for_adventure",
                return_value={"status": "not_ready"},
            ),
        ):
            receipt = self.service.gm_tool_registry.execute(
                "start_session",
                {
                    "phase": "adventure",
                    "reason": "全桌已经同意进入第一章",
                },
                runtime_context("大家都同意进入第一章。"),
            )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertTrue(receipt.result["saved_path"])
        self.assertEqual(hero.fabula_points, 1)
        self.assertTrue(self.app.session_ledger.active)
        self.assertEqual(self.app.session_ledger.participating_pcs, {"伊莉雅"})

        reloaded_service = FUGMHttpService(
            data_root=self.tmpdir.name,
            use_llm=False,
        )
        reloaded = reloaded_service._runtime("runtime-tool-test").app
        self.assertEqual(
            reloaded.character_manager.get("伊莉雅").fabula_points,
            1,
        )
        self.assertTrue(reloaded.session_ledger.active)
        self.assertEqual(reloaded.session_ledger.participating_pcs, {"伊莉雅"})

    def test_start_session_cannot_overwrite_another_active_session_ledger(
        self,
    ) -> None:
        self.app.session_ledger.start(
            "older-session",
            participating_pcs=["伊莉雅"],
        )
        self.app.session_ledger.fabula_spent = 2
        context = runtime_context("开始下一场吧。")
        context.session_id = "new-session"

        receipt = self.service.gm_runtime_tools.start_session(
            context,
            {
                "phase": "adventure",
                "reason": "玩家请求开启下一场",
                "evidence": "开始下一场吧",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "SESSION_LEDGER_ID_MISMATCH",
        )
        self.assertEqual(
            self.app.session_ledger.session_id,
            "older-session",
        )
        self.assertEqual(self.app.session_ledger.fabula_spent, 2)

    def test_end_session_rejects_a_different_active_ledger_without_resetting_it(
        self,
    ) -> None:
        self.service.session_gates.activate(
            "runtime-tool-test",
            "group-1",
            "s1",
            status="adventure",
        )
        self.app.session_ledger.start(
            "older-session",
            participating_pcs=["伊莉雅"],
        )
        self.app.session_ledger.fabula_spent = 2

        receipt = self.service.gm_runtime_tools.end_session(
            runtime_context("今天先收团吧。"),
            {
                "title": "钟塔入口",
                "public_reply": "风铃廊里的灯火比开场时暗了一盏。今天先停在这里。",
                "closing_image": "风铃廊里的灯火比开场时暗了一盏。",
                "evidence": "今天先收团吧",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "SESSION_LEDGER_ID_MISMATCH",
        )
        self.assertTrue(self.app.session_ledger.active)
        self.assertEqual(
            self.app.session_ledger.session_id,
            "older-session",
        )
        self.assertEqual(self.app.session_ledger.fabula_spent, 2)
        self.assertTrue(
            self.service.session_gates.get(
                "runtime-tool-test",
                "group-1",
                "s1",
            ).active
        )

    def test_end_session_exposes_final_locations_and_does_not_lock_false_escape(
        self,
    ) -> None:
        self.service.session_gates.activate(
            "runtime-tool-test",
            "group-1",
            "s1",
            status="adventure",
        )
        self.app.session_ledger.start("s1", participating_pcs=["伊莉雅"])
        self.app.scene_manager.start_scene(
            "监狱分岔口",
            SceneType.STANDARD,
            location="卡里巴村监狱·分岔口",
            participants=["伊莉雅"],
            objective="找到离开监狱的路",
        )

        with patch.object(
            self.app.campaign_pacing_manager,
            "assess_session_completion",
            return_value=(False, ["撤离尚未完成"]),
        ):
            receipt = self.service.gm_runtime_tools.end_session(
                runtime_context("今天先收团。"),
                {
                    "title": "未完的越狱",
                    "public_reply": (
                        "裂纹灵魂灯仍照着监狱分岔口，伊莉雅的影子停在铁门内侧。"
                        "诺艾尔与伊莉雅已经一起逃出了监狱。"
                    ),
                    "closing_image": (
                        "裂纹灵魂灯仍照着监狱分岔口，伊莉雅的影子停在铁门内侧。"
                    ),
                    "evidence": "今天先收团",
                },
            )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertFalse(receipt.lock_public_reply)
        self.assertNotIn("逃出了监狱", receipt.public_fallback_reply)
        self.assertIn("监狱分岔口", receipt.public_fallback_reply)
        self.assertTrue(receipt.result["continuation_required"])
        final_state = receipt.result["final_state_snapshot"]
        self.assertEqual(final_state["scene"]["name"], "监狱分岔口")
        self.assertEqual(
            final_state["player_characters"][0]["location"],
            "卡里巴村监狱·分岔口",
        )
        self.assertEqual(
            receipt.result["closing_image"],
            "裂纹灵魂灯仍照着监狱分岔口，伊莉雅的影子停在铁门内侧。",
        )

    def test_adventure_end_session_requires_evolved_closing_image_in_reply(self) -> None:
        self.service.session_gates.activate(
            "runtime-tool-test",
            "group-1",
            "s1",
            status="adventure",
        )
        self.app.session_ledger.start("s1", participating_pcs=["伊莉雅"])
        self.app.session_episode_tracker.record_opening_image(
            "裂纹灵魂灯在雨里向下燃烧。"
        )

        missing = self.service.gm_runtime_tools.end_session(
            runtime_context("今天先收团。"),
            {
                "title": "未完的越狱",
                "public_reply": "今天先停在这里。",
                "evidence": "今天先收团",
            },
        )
        unchanged = self.service.gm_runtime_tools.end_session(
            runtime_context("今天先收团。"),
            {
                "title": "未完的越狱",
                "public_reply": "裂纹灵魂灯在雨里向下燃烧。今天先停在这里。",
                "closing_image": "裂纹灵魂灯在雨里向下燃烧。",
                "evidence": "今天先收团",
            },
        )

        self.assertFalse(missing.ok)
        self.assertEqual(missing.error_code, "CLOSING_IMAGE_REQUIRED")
        self.assertFalse(unchanged.ok)
        self.assertEqual(unchanged.error_code, "CLOSING_IMAGE_NOT_EVOLVED")
        self.assertTrue(self.app.session_ledger.active)

    def test_start_adventure_rolls_back_gate_and_award_when_save_fails(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.fabula_points = 0
        with (
            patch.object(self.service, "_adventure_start_blockers", return_value={}),
            patch.object(
                self.app,
                "ensure_world_map_for_adventure",
                return_value={"status": "not_ready"},
            ),
            patch.object(
                self.service,
                "_autosave_campaign",
                side_effect=RuntimeError("disk unavailable"),
            ),
        ):
            receipt = self.service.gm_tool_registry.execute(
                "start_session",
                {
                    "phase": "adventure",
                    "reason": "全桌已经同意进入第一章",
                },
                runtime_context("大家都同意进入第一章。"),
            )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "TOOL_EXECUTION_FAILED")
        self.assertEqual(
            self.app.character_manager.get("伊莉雅").fabula_points,
            0,
        )
        self.assertFalse(self.app.session_ledger.active)
        gate = self.service.session_gates.get(
            "runtime-tool-test",
            "group-1",
            "s1",
        )
        self.assertFalse(gate.active)

    def test_start_session_zero_initializes_state_without_legacy_opening(self) -> None:
        context = runtime_context("大家准备好了，请开始第零章，先聊基调和安全边界。")
        receipt = self.service.gm_runtime_tools.start_session(
            context,
            {
                "phase": "session_zero",
                "reason": "大家准备好了",
                "evidence": "请开始第零章",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertFalse(receipt.lock_public_reply)
        self.assertEqual(receipt.public_fallback_reply, "")
        self.assertTrue(receipt.result["session_zero_opening_required"])
        self.assertEqual(receipt.result["opening_instruction"], context.metadata["current_message"])
        self.assertTrue(self.app.session_zero_manager.state.active)
        self.assertEqual(self.app.scene_manager.current_scene.scene_type, SceneType.SESSION_ZERO)

    def test_scene_private_situation_schema_lists_only_supported_frame_fields(self) -> None:
        schema = next(
            item
            for item in self.service.gm_tool_registry.schemas()
            if item["name"] == "start_scene"
        )
        private_schema = schema["parameters"]["properties"]["private_situation"]
        scene_type_schema = schema["parameters"]["properties"]["scene_type"]

        self.assertFalse(private_schema["additionalProperties"])
        self.assertIn("current_pressure", private_schema["properties"])
        self.assertIn("possible_reveals", private_schema["properties"])
        self.assertNotIn("pressure", private_schema["properties"])
        self.assertEqual(
            set(scene_type_schema["enum"]),
            {"standard", "interlude", "gm"},
        )

    def test_scene_opening_rejects_exact_private_secret_leak_atomically(self) -> None:
        message = "大家沿旧路进入潮声钟塔。"
        receipt = self.service.gm_runtime_tools.start_scene(
            runtime_context(message),
            {
                "name": "潮声钟塔",
                "scene_type": "standard",
                "location": "潮声钟塔",
                "participants": ["伊莉雅"],
                "private_situation": {"secrets": ["会长亲手刮掉了旅人的姓氏"]},
                "public_opening": "你们立刻知道，会长亲手刮掉了旅人的姓氏。",
                "player_handoff": "伊莉雅，你先做什么？",
                "evidence": "沿旧路进入潮声钟塔",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "PRIVATE_SCENE_INFORMATION_LEAK")
        self.assertIsNone(self.app.scene_manager.current_scene)

    def test_start_scene_cannot_silently_replace_an_active_scene(self) -> None:
        old_scene = self.app.start_scene("旧路", location="旧路", participants=["伊莉雅"])
        self.app.clock_manager.add(
            Clock(
                name="旧路塌陷",
                max_segments=4,
                current=2,
                clock_type="threat",
                scope="scene",
                scene_id=old_scene.scene_id,
            )
        )
        message = "伊莉雅离开旧路，抵达安全营地。"
        receipt = self.service.gm_runtime_tools.start_scene(
            runtime_context(message),
            {
                "name": "安全营地",
                "scene_type": "standard",
                "location": "林间营地",
                "participants": ["伊莉雅"],
                "private_situation": {},
                "public_opening": "篝火刚被点亮，旧路的轰鸣便隔在了树林另一侧。",
                "player_handoff": "伊莉雅，你准备怎样安顿下来？",
                "evidence": "离开旧路，抵达安全营地",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "SCENE_ALREADY_ACTIVE")
        self.assertEqual(self.app.scene_manager.current_scene.scene_id, old_scene.scene_id)
        self.assertTrue(self.app.clock_manager.exists("旧路塌陷"))

    def test_generic_scene_tool_rejects_managed_scene_types(self) -> None:
        message = "大家走进镜之水道。"

        receipt = self.service.gm_runtime_tools.start_scene(
            runtime_context(message),
            {
                "name": "镜之水道",
                "scene_type": "dungeon",
                "location": "旧王国地下",
                "participants": ["伊莉雅"],
                "private_situation": {},
                "public_opening": "水声从黑暗里传来。",
                "player_handoff": "伊莉雅，你先做什么？",
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "MANAGED_SCENE_TYPE_REQUIRES_TYPED_TOOL",
        )
        self.assertFalse(self.app.dungeon_manager.state.active)
        self.assertIsNone(self.app.scene_manager.current_scene)

    def test_transition_scene_resolves_clear_nearby_movement_without_moving_other_pcs(self) -> None:
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
        self.app.start_scene(
            "登记小室",
            location="白花碑驿站登记小室",
            participants=["伊莉雅", "赛璃", "值守望"],
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
        message = "伊莉雅带着记录离开登记小室，跟着值守望前往旧路闸门。"
        with patch.object(
            self.service,
            "_player_character_control_map",
            return_value={"阿凛": ["伊莉雅"], "南星": ["赛璃"]},
        ):
            receipt = self.service.gm_runtime_tools.transition_scene(
                runtime_context(message),
                {
                    "name": "旧路闸门核对",
                    "scene_type": "standard",
                    "location": "白花碑驿站旧路闸门",
                    "movers": ["伊莉雅"],
                    "npc_companions": ["值守望"],
                    "destination_npcs": [],
                    "objective": "核对候选记录",
                    "private_situation": {
                        "visible_elements": ["关闭的旧路闸门"],
                        "secrets": ["闸门印记曾被人改写"],
                    },
                    "transition_summary": "伊莉雅与值守望离开登记小室。",
                    "public_arrival": "旧路闸门就在廊道尽头。伊莉雅与值守望抵达时，门上的旧印记仍等着核对。",
                    "evidence": message,
                },
            )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(self.app.scene_manager.current_scene.location, "白花碑驿站旧路闸门")
        self.assertEqual(self.app.scene_manager.current_scene.participants, ["伊莉雅", "值守望"])
        self.assertNotIn("赛璃", self.app.scene_manager.current_scene.participants)
        source = next(
            scene
            for scene in self.app.scene_manager.suspended_scenes
            if scene.name == "登记小室"
        )
        self.assertTrue(source.active)
        self.assertEqual(source.participants, ["赛璃"])
        self.assertEqual(self.app.scene_manager.history, [])
        self.assertEqual(
            receipt.result["allowed_followup_tools"],
            ["decide_npc_response", "introduce_npc", "start_conflict"],
        )
        self.assertEqual(
            receipt.result["action_round"]["action_round_progress"]["acted"],
            ["伊莉雅"],
        )
        self.assertEqual(
            receipt.result["action_round"]["action_round_waiting_for"],
            ["赛璃"],
        )
        self.assertEqual(self.app.clock_manager.get("财团巡逻队逼近").current, 0)
        completed = self.app.record_free_scene_player_action("赛璃")
        self.assertTrue(completed["action_round_completed"])
        self.assertEqual(self.app.clock_manager.get("财团巡逻队逼近").current, 1)

    def test_transition_scene_rejects_arrival_that_places_absent_pcs_at_destination(self) -> None:
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
        original = self.app.start_scene(
            "登记小室",
            location="白花碑驿站登记小室",
            participants=["伊莉雅", "赛璃"],
        )
        message = "伊莉雅独自前往旧路闸门。"

        receipt = self.service.gm_runtime_tools.transition_scene(
            runtime_context(message),
            {
                "name": "旧路闸门",
                "scene_type": "standard",
                "location": "白花碑驿站旧路闸门",
                "movers": ["伊莉雅"],
                "npc_companions": [],
                "destination_npcs": [],
                "private_situation": {},
                "transition_summary": "赛璃留在登记小室。",
                "public_arrival": "伊莉雅抵达旧路闸门，赛璃已经在门边等候。",
                "evidence": "伊莉雅独自前往旧路闸门",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "PUBLIC_ARRIVAL_ACTOR_NOT_PRESENT")
        self.assertIs(self.app.scene_manager.current_scene, original)
        self.assertEqual(self.app.scene_manager.history, [])

    def test_transition_scene_rejects_pc_departing_from_another_branch(self) -> None:
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
        original = self.app.start_scene(
            "登记小室",
            location="白花碑驿站登记小室",
            participants=["伊莉雅"],
        )
        self.app.scene_manager.actor_locations["赛璃"] = "风铃廊"

        receipt = self.service.gm_runtime_tools.transition_scene(
            runtime_context("赛璃去旧路闸门。", speaker="南星"),
            {
                "name": "旧路闸门",
                "scene_type": "standard",
                "location": "白花碑驿站旧路闸门",
                "movers": ["赛璃"],
                "npc_companions": [],
                "destination_npcs": [],
                "private_situation": {},
                "transition_summary": "赛璃离开风铃廊。",
                "public_arrival": "赛璃抵达旧路闸门。",
                "evidence": "赛璃去旧路闸门",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "MOVER_NOT_IN_FOCUSED_SCENE")
        self.assertIs(self.app.scene_manager.current_scene, original)

    def test_transition_within_same_complex_carries_open_npc_condition(self) -> None:
        old_scene = self.app.start_scene(
            "风铃廊问路",
            location="白花碑驿站·风铃廊",
            participants=["伊莉雅"],
        )
        self.app.scene_frame_manager.ensure_frame(
            scene=old_scene,
            recent_chat="",
            world_state=self.app.world_state,
            character_manager=self.app.character_manager,
        )
        condition = self.app.scene_frame_manager.record_condition(
            npc="白花守望会会长",
            condition="走完风铃廊至旧路闸门的护送路线",
            promised_result="打开旧路闸门并交出白花通行牌",
            scene=old_scene,
        )
        self.assertIsNotNone(condition)
        message = "伊莉雅离开风铃廊，抵达驿站内的旧路闸门。"

        receipt = self.service.gm_runtime_tools.transition_scene(
            runtime_context(message),
            {
                "name": "旧路闸门前",
                "scene_type": "standard",
                "location": "白花碑驿站·旧路闸门",
                "movers": ["伊莉雅"],
                "npc_companions": [],
                "destination_npcs": [],
                "private_situation": {},
                "transition_summary": "伊莉雅离开风铃廊。",
                "public_arrival": "伊莉雅抵达驿站内的旧路闸门前。",
                "evidence": "离开风铃廊，抵达驿站内的旧路闸门",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertTrue(receipt.result["location_continuity_inherited"])
        current_scene = self.app.scene_manager.current_scene
        current_frame = self.app.scene_frame_manager.current_frame
        inherited = next(
            item
            for item in current_frame.open_conditions
            if item["condition_id"] == condition["condition_id"]
        )
        self.assertEqual(inherited["promised_result"], condition["promised_result"])
        self.assertTrue(
            any(
                item["condition_id"] == condition["condition_id"]
                for item in current_scene.open_conditions
            )
        )

    def test_system_scene_transition_does_not_consume_a_player_action(self) -> None:
        self.app.start_scene(
            "风铃廊",
            location="白花碑驿站风铃廊",
            participants=["伊莉雅"],
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
        message = "风铃廊在夜色里安静下来，镜头转向旧路闸门。"
        context = runtime_context(message)
        context.metadata["system_gm_beat_request"] = True

        receipt = self.service.gm_runtime_tools.transition_scene(
            context,
            {
                "name": "旧路闸门",
                "scene_type": "standard",
                "location": "白花碑驿站旧路闸门",
                "movers": ["伊莉雅"],
                "npc_companions": [],
                "destination_npcs": [],
                "private_situation": {},
                "transition_summary": "镜头离开风铃廊。",
                "public_arrival": "旧路闸门后的锁链在风中轻响。",
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["action_round"], {})
        self.assertEqual(receipt.result["action_round_events"], [])
        self.assertEqual(self.app.clock_manager.get("财团巡逻队逼近").current, 0)
        self.assertEqual(
            self.app.scene_manager.current_scene.action_round_acted_actors,
            [],
        )

    def test_transition_scene_rejects_moving_another_players_character(self) -> None:
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
        original = self.app.start_scene(
            "登记小室",
            location="白花碑驿站登记小室",
            participants=["伊莉雅", "赛璃"],
        )
        message = "伊莉雅前往旧路闸门。"
        with patch.object(
            self.service,
            "_player_character_control_map",
            return_value={"阿凛": ["伊莉雅"], "南星": ["赛璃"]},
        ):
            receipt = self.service.gm_runtime_tools.transition_scene(
                runtime_context(message),
                {
                    "name": "旧路闸门",
                    "scene_type": "standard",
                    "location": "旧路闸门",
                    "movers": ["伊莉雅", "赛璃"],
                    "npc_companions": [],
                    "destination_npcs": [],
                    "private_situation": {},
                    "transition_summary": "众人离开登记小室。",
                    "public_arrival": "众人抵达旧路闸门。",
                    "evidence": message,
                },
            )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "PLAYER_CHARACTER_NOT_CONTROLLED")
        self.assertIs(self.app.scene_manager.current_scene, original)
        self.assertEqual(self.app.scene_manager.history, [])

    def test_focus_scene_branch_preserves_parallel_scene_and_restores_its_frame(self) -> None:
        self.app.character_manager.add(
            Character(
                name="艾薇娅",
                attributes={"DEX": 6, "INS": 10, "MIG": 6, "WLP": 10},
                max_hp=35,
                hp=35,
                max_mp=55,
                mp=55,
                traits=["pc"],
            )
        )
        registration = self.app.start_scene(
            "登记小室查册",
            location="白花碑驿站·登记小室",
            participants=["伊莉雅"],
        )
        self.app.scene_frame_manager.ensure_frame(
            scene=registration,
            recent_chat="伊莉雅在登记小室查阅旧册。",
            world_state=self.app.world_state,
            character_manager=self.app.character_manager,
        )
        registration_frame = self.app.scene_frame_manager.current_frame
        self.app.scene_manager.actor_locations["艾薇娅"] = "白花碑驿站"
        self.app.clock_manager.add(
            Clock(
                name="登记小室封锁",
                max_segments=4,
                current=1,
                clock_type="threat",
                scope="scene",
                scene_id=registration.scene_id,
            )
        )
        message = "艾薇娅留在驿站，走到白花碑后的回撤标记处守住退路。"
        with patch.object(
            self.service,
            "_player_character_control_map",
            return_value={"时雨": ["艾薇娅"], "阿凛": ["伊莉雅"]},
        ):
            receipt = self.service.gm_runtime_tools.focus_scene_branch(
                runtime_context(message, speaker="时雨"),
                {
                    "actor": "艾薇娅",
                    "name": "白花碑回撤点",
                    "scene_type": "standard",
                    "location": "白花碑后方檐柱阴影下",
                    "objective": "守住退路",
                    "private_situation": {"current_pressure": "财团车队仍在靠近。"},
                    "evidence": "走到白花碑后的回撤标记处守住退路",
                },
            )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertFalse(receipt.lock_public_reply)
        self.assertEqual(receipt.public_fallback_reply, "")
        self.assertEqual(receipt.result["mode"], "created")
        self.assertIn("move_scene_group", receipt.result["allowed_followup_tools"])
        self.assertIn("move_scene_group", receipt.result["required_followup_tools"])
        self.assertIn(registration, self.app.scene_manager.suspended_scenes)
        self.assertEqual(self.app.scene_manager.history, [])
        self.assertTrue(self.app.clock_manager.exists("登记小室封锁"))
        self.assertEqual(self.app.scene_manager.current_scene.participants, ["艾薇娅"])
        self.assertIsNot(self.app.scene_frame_manager.current_frame, registration_frame)

        with patch.object(
            self.service,
            "_player_character_control_map",
            return_value={"时雨": ["艾薇娅"], "阿凛": ["伊莉雅"]},
        ):
            restored = self.service.gm_runtime_tools.focus_scene_branch(
                runtime_context("伊莉雅继续在登记小室查册。", speaker="阿凛"),
                {
                    "actor": "伊莉雅",
                    "name": "登记小室查册",
                    "scene_type": "standard",
                    "location": "白花碑驿站·登记小室",
                    "objective": "查阅登记册",
                    "private_situation": {},
                    "evidence": "伊莉雅继续在登记小室查册",
                },
            )

        self.assertTrue(restored.ok, restored.message)
        self.assertEqual(restored.result["mode"], "restored")
        self.assertIs(self.app.scene_manager.current_scene, registration)
        self.assertIs(self.app.scene_frame_manager.current_frame, registration_frame)

    def test_system_defeat_focus_recovers_fallen_pc_and_only_allows_public_opening(self) -> None:
        self.app.character_manager.add(
            Character(
                name="艾薇娅",
                attributes={"DEX": 6, "INS": 10, "MIG": 6, "WLP": 10},
                max_hp=35,
                hp=0,
                max_mp=55,
                mp=55,
                traits=["pc"],
            )
        )
        self.app.start_scene(
            "监狱外的雨夜",
            location="卡里巴村监狱外",
            participants=["伊莉雅"],
        )
        self.app.scene_manager.actor_locations["艾薇娅"] = "卡里巴村监狱值班室"
        self.app.conflict_manager.state.fallen_pcs["艾薇娅"] = "分离：被守卫重新收押"
        context = runtime_context(
            "必须让放弃抵抗的角色进入下一场后果场景。",
            speaker="系统主动节拍",
        )
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "defeat_aftermath",
            }
        )

        receipt = self.service.gm_runtime_tools.focus_scene_branch(
            context,
            {
                "actor": "艾薇娅",
                "name": "重新收押",
                "scene_type": "standard",
                "location": "卡里巴村监狱值班室",
                "objective": "面对败北后的处境",
                "private_situation": {},
                "evidence": "必须让放弃抵抗的角色进入下一场后果场景",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["mode"], "created")
        self.assertEqual(receipt.result["allowed_followup_tools"], ["commit_scene_response"])
        self.assertEqual(receipt.result["required_followup_tools"], ["commit_scene_response"])
        self.assertEqual(self.app.character_manager.get("艾薇娅").hp, 17)
        self.assertNotIn("艾薇娅", self.app.conflict_manager.state.fallen_pcs)

    def test_system_defeat_focus_on_current_fallen_pc_requires_new_scene(self) -> None:
        self.app.character_manager.add(
            Character(
                name="艾薇娅",
                attributes={"DEX": 6, "INS": 10, "MIG": 6, "WLP": 10},
                max_hp=35,
                hp=0,
                max_mp=55,
                mp=55,
                traits=["pc"],
            )
        )
        self.app.start_scene(
            "卡里巴村监狱牢房区",
            location="卡里巴村监狱牢房区",
            participants=["伊莉雅", "艾薇娅"],
        )
        self.app.conflict_manager.state.fallen_pcs["艾薇娅"] = (
            "分离：被守卫重新收押"
        )
        context = runtime_context(
            "必须让放弃抵抗的角色进入下一场后果场景。",
            speaker="系统主动节拍",
        )
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "defeat_aftermath",
            }
        )

        focus = self.service.gm_runtime_tools.focus_scene_branch(
            context,
            {
                "actor": "艾薇娅",
                "evidence": "必须让放弃抵抗的角色进入下一场后果场景",
            },
        )

        self.assertTrue(focus.ok, focus.message)
        self.assertEqual(focus.result["mode"], "current")
        self.assertEqual(focus.result["allowed_followup_tools"], ["transition_scene"])
        self.assertEqual(focus.result["required_followup_tools"], ["transition_scene"])
        self.assertEqual(self.app.character_manager.get("艾薇娅").hp, 0)
        self.assertIn("艾薇娅", self.app.conflict_manager.state.fallen_pcs)

        transition = self.service.gm_runtime_tools.transition_scene(
            context,
            {
                "name": "重新收押",
                "scene_type": "standard",
                "location": "卡里巴村监狱独立牢房",
                "movers": ["艾薇娅"],
                "npc_companions": [],
                "destination_npcs": [],
                "objective": "面对败北后的处境",
                "private_situation": {},
                "transition_summary": "守卫把放弃抵抗的艾薇娅押离原牢区。",
                "public_arrival": "独立牢房的铁门在艾薇娅身后落锁。",
                "evidence": "必须让放弃抵抗的角色进入下一场后果场景",
            },
        )

        self.assertTrue(transition.ok, transition.message)
        self.assertEqual(self.app.character_manager.get("艾薇娅").hp, 17)
        self.assertNotIn("艾薇娅", self.app.conflict_manager.state.fallen_pcs)

    def test_system_defeat_focus_requires_new_scene_after_restoring_old_branch(self) -> None:
        self.app.character_manager.add(
            Character(
                name="艾薇娅",
                attributes={"DEX": 6, "INS": 10, "MIG": 6, "WLP": 10},
                max_hp=35,
                hp=0,
                max_mp=55,
                mp=55,
                traits=["pc"],
            )
        )
        old_branch = self.app.start_scene(
            "卡里巴村监狱牢房区",
            location="卡里巴村监狱牢房区",
            participants=["艾薇娅", "狱卒", "伊莉雅"],
        )
        self.app.scene_manager.move_participants_to_location(
            ["伊莉雅"],
            "卡里巴村监狱外",
            scene_name="监狱外的雨夜",
        )
        self.app.conflict_manager.state.fallen_pcs["艾薇娅"] = "分离：被守卫重新收押"
        context = runtime_context(
            "必须让放弃抵抗的角色进入下一场后果场景。",
            speaker="系统主动节拍",
        )
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "defeat_aftermath",
            }
        )

        receipt = self.service.gm_runtime_tools.focus_scene_branch(
            context,
            {
                "actor": "艾薇娅",
                "name": "重新收押",
                "scene_type": "standard",
                "location": "卡里巴村监狱牢房区",
                "objective": "面对败北后的处境",
                "private_situation": {},
                "evidence": "必须让放弃抵抗的角色进入下一场后果场景",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["mode"], "restored")
        self.assertEqual(receipt.result["scene_id"], old_branch.scene_id)
        self.assertEqual(receipt.result["allowed_followup_tools"], ["transition_scene"])
        self.assertEqual(receipt.result["required_followup_tools"], ["transition_scene"])
        self.assertEqual(self.app.character_manager.get("艾薇娅").hp, 0)
        self.assertIn("艾薇娅", self.app.conflict_manager.state.fallen_pcs)

    def test_focus_scene_branch_restores_managed_type_and_allows_story_item_action(self) -> None:
        self.app.character_manager.add(
            Character(
                name="苍祈",
                attributes={"DEX": 8, "INS": 10, "MIG": 6, "WLP": 8},
                max_hp=35,
                hp=35,
                max_mp=45,
                mp=45,
                traits=["pc"],
            )
        )
        travel = self.app.start_scene(
            "风铃廊守望",
            SceneType.TRAVEL,
            location="白花碑驿站·风铃廊",
            participants=["苍祈"],
        )
        self.app.scene_manager._suspend_current_scene()
        self.app.scene_manager.start_scene(
            "登记小室",
            SceneType.STANDARD,
            location="白花碑驿站·登记小室",
            participants=["伊莉雅"],
        )

        with patch.object(
            self.service,
            "_player_character_control_map",
            return_value={"澄砚": ["苍祈"], "阿凛": ["伊莉雅"]},
        ):
            receipt = self.service.gm_runtime_tools.focus_scene_branch(
                runtime_context("苍祈点亮蓝芯守望灯。", speaker="澄砚"),
                {
                    "actor": "苍祈",
                    "name": "风铃廊守望",
                    "scene_type": "travel",
                    "location": "白花碑驿站·风铃廊",
                    "objective": "示警",
                    "private_situation": {},
                    "evidence": "苍祈点亮蓝芯守望灯",
                },
            )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["mode"], "restored")
        self.assertIs(self.app.scene_manager.current_scene, travel)
        self.assertEqual(travel.scene_type, SceneType.TRAVEL)
        self.assertIn(
            "commit_story_item_action",
            receipt.result["required_followup_tools"],
        )

    def test_transition_scene_rejects_npc_companion_not_in_current_scene(self) -> None:
        original = self.app.start_scene(
            "登记小室",
            location="白花碑驿站登记小室",
            participants=["伊莉雅"],
        )
        message = "伊莉雅跟着值守望前往旧路闸门。"
        receipt = self.service.gm_runtime_tools.transition_scene(
            runtime_context(message),
            {
                "name": "旧路闸门",
                "scene_type": "standard",
                "location": "旧路闸门",
                "movers": ["伊莉雅"],
                "npc_companions": ["值守望"],
                "destination_npcs": [],
                "private_situation": {},
                "transition_summary": "伊莉雅离开登记小室。",
                "public_arrival": "伊莉雅抵达旧路闸门。",
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "NPC_COMPANION_NOT_PRESENT")
        self.assertIs(self.app.scene_manager.current_scene, original)

    def test_transition_scene_resolves_present_npc_public_identity_to_stable_name(self) -> None:
        self.app.start_scene(
            "登记小室",
            location="白花碑驿站登记小室",
            participants=["伊莉雅"],
        )
        self.app.world_state.ensure_npc_persona(
            "白花守望者",
            public_identity="守望者",
            role_in_story="旧路向导",
            current_location="白花碑驿站登记小室",
        )
        self.app.scene_manager.add_participant("白花守望者")
        message = "伊莉雅跟着守望者前往旧路闸门。"

        receipt = self.service.gm_runtime_tools.transition_scene(
            runtime_context(message),
            {
                "name": "旧路闸门",
                "scene_type": "standard",
                "location": "旧路闸门",
                "movers": ["伊莉雅"],
                "npc_companions": ["守望者"],
                "destination_npcs": [],
                "private_situation": {},
                "transition_summary": "伊莉雅与守望者离开登记小室。",
                "public_arrival": "旧路闸门横在前方，白花守望者停在锁轮旁等伊莉雅跟上。",
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(
            self.app.scene_manager.current_scene.participants,
            ["伊莉雅", "白花守望者"],
        )
        self.assertEqual(receipt.result["npc_companions"], ["白花守望者"])

    def test_start_conflict_auto_prepares_enemy_without_combat_profile(self) -> None:
        self.app.start_scene("风铃廊", location="风铃廊", participants=["伊莉雅", "监察官艾蕾娜"])
        message = "监察官艾蕾娜拔出权杖，命令机兵动手。"
        receipt = self.service.gm_runtime_tools.start_conflict(
            runtime_context(message),
            {
                "scene_name": "风铃廊伏击",
                "pcs": ["伊莉雅"],
                "enemies": ["监察官艾蕾娜"],
                "leader": "伊莉雅",
                "objective": "护住失忆旅人并离开驿站",
                "public_opening": "权杖落下时，两侧机兵同时封住廊门。",
                "evidence": "拔出权杖，命令机兵动手",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertIn("监察官艾蕾娜", receipt.result["auto_prepared_npcs"])
        self.assertTrue(self.app.character_manager.exists("监察官艾蕾娜"))
        enemy = self.app.character_manager.get("监察官艾蕾娜")
        self.assertIn("enemy", enemy.traits)
        self.assertTrue(enemy.npc_source_template)
        self.assertTrue(enemy.npc_attacks)

    def test_start_conflict_uses_deterministic_blueprint_when_join_deadline_expires(
        self,
    ) -> None:
        scene = self.app.start_scene(
            "潮门走廊",
            location="潮门走廊",
            participants=["伊莉雅", "潮门守卫"],
        )
        persona = self.app.world_state.ensure_npc_persona(
            "潮门守卫",
            public_identity="潮门守卫",
            role_in_story="守住潮门走廊",
        )
        client = _BlockingNPCSelectionClient()
        designer = self.app.npc_blueprint_designer
        designer.client = client
        designer.model = "test-model"
        defaults = self.app._npc_blueprint_defaults(persona)
        queued = designer.submit(
            persona,
            level=defaults["level"],
            species="",
            rank=defaults["rank"],
            champion_value=defaults["champion_value"],
            combat_side="enemy",
            is_villain=defaults["is_villain"],
            ultima_points=defaults["ultima_points"],
            scene_id=scene.scene_id,
            scene_context=self.app._npc_design_scene_context(persona),
            background=True,
        )
        self.assertTrue(client.started.wait(timeout=2))
        message = "潮门守卫举起长枪，伊莉雅迎战。"
        context = runtime_context(message)
        lease_owner = "start-conflict-deadline-test"
        context.metadata["_gm_active_write_lease_owner"] = lease_owner
        context.metadata["_gm_agent_deadline_monotonic"] = (
            time.monotonic() + 0.05
        )
        with self.runtime.transaction_lock:
            self.runtime.write_lease_owner = lease_owner
        try:
            receipt = self.service.gm_runtime_tools.start_conflict(
                context,
                {
                    "scene_name": "潮门冲突",
                    "pcs": ["伊莉雅"],
                    "enemies": ["潮门守卫"],
                    "leader": "伊莉雅",
                    "objective": "穿过潮门走廊",
                    "public_opening": "潮门守卫的枪尖封住了唯一出口。",
                    "evidence": "潮门守卫举起长枪，伊莉雅迎战",
                },
            )
        finally:
            with self.runtime.write_lease_condition:
                self.runtime.write_lease_owner = ""
                self.runtime.write_lease_started_at = 0.0
                self.runtime.write_lease_condition.notify_all()
            client.release.set()

        completed = designer.wait(str(queued["job_id"]), timeout=2)
        self.assertTrue(receipt.ok, receipt.message)
        self.assertIn("潮门守卫", receipt.result["auto_prepared_npcs"])
        self.assertTrue(self.app.character_manager.exists("潮门守卫"))
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(completed["fallback_used"])
        self.assertEqual(
            completed["publication_source"],
            "foreground_deterministic_fallback",
        )

    def test_start_conflict_uses_existing_profiles_and_team_initiative(self) -> None:
        self._force_successful_initiative()
        self._add_test_enemy()
        self.app.start_scene("风铃廊", location="风铃廊", participants=["伊莉雅", "财团机兵"])
        self.app.scene_frame_manager.ensure_frame(
            scene=self.app.scene_manager.current_scene,
            recent_chat="",
            world_state=self.app.world_state,
            character_manager=self.app.character_manager,
        )
        request = self.app.npc_response_windows.open_request(
            self.app.scene_frame_manager.current_frame,
            npc="财团机兵",
            summary="要求伊莉雅放下武器。",
            required_items=[{"item_id": "disarm", "prompt": "是否放下武器"}],
            scene=self.app.scene_manager.current_scene,
        )
        self.assertIsNotNone(request)
        message = "财团机兵封住廊门，伊莉雅举盾迎战。"
        receipt = self.service.gm_runtime_tools.start_conflict(
            runtime_context(message),
            {
                "scene_name": "风铃廊伏击",
                "pcs": ["伊莉雅"],
                "enemies": ["财团机兵"],
                "leader": "伊莉雅",
                "objective": "护住失忆旅人并突破封锁",
                "public_opening": "机兵的长斧横在廊门前，伊莉雅身后的旅人已经无路可退。",
                "evidence": "财团机兵封住廊门，伊莉雅举盾迎战",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertTrue(self.app.conflict_manager.state.active)
        self.assertEqual(self.app.scene_manager.current_scene.scene_type, SceneType.CONFLICT)
        self.assertEqual(set(receipt.result["turn_order"]), {"伊莉雅", "财团机兵"})
        self.assertIn("先攻团队检定", receipt.public_fallback_reply)
        self.assertNotIn("开始团队先攻检定", receipt.public_fallback_reply)
        self.assertEqual(
            receipt.result["superseded_npc_questions"],
            [request["question_id"]],
        )
        self.assertIsNone(
            self.app.scene_frame_manager.latest_pending_npc_question()
        )

    def test_pending_team_initiative_shows_dice_without_trait_tutorial(self) -> None:
        self._add_test_enemy()
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.identity = "守住旧路的人"
        ilya.fabula_points = 1
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["DEX", "INS"],
                dice=[(8, 1), (10, 2)],
                total=3,
                modifier=0,
                high_roll=2,
                target_number=10,
                success=False,
                critical_success=False,
                fumble=False,
            )
        )
        self.app.start_scene(
            "风铃廊",
            location="风铃廊",
            participants=["伊莉雅", "财团机兵"],
        )

        receipt = self.service.gm_runtime_tools.start_conflict(
            runtime_context("财团机兵封住廊门，伊莉雅举盾迎战。"),
            {
                "scene_name": "风铃廊伏击",
                "pcs": ["伊莉雅"],
                "enemies": ["财团机兵"],
                "leader": "伊莉雅",
                "objective": "突破封锁",
                "public_opening": "机兵的长斧横在廊门前。",
                "evidence": "财团机兵封住廊门，伊莉雅举盾迎战",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertTrue(receipt.result["initiative_pending"])
        self.assertIn("伊莉雅进行团队先攻检定", receipt.public_fallback_reply)
        self.assertIn("d8=1 + d10=2", receipt.public_fallback_reply)
        self.assertNotIn("若玩家想改变结果", receipt.public_fallback_reply)
        self.assertNotIn("冲突回合表暂不建立", receipt.public_fallback_reply)
        self.assertNotIn("【伊莉雅】：", receipt.public_fallback_reply)

    def test_team_initiative_trait_reroll_survives_expression_failure(self) -> None:
        class FailingExpression:
            def __init__(self) -> None:
                self.fallback = Expressor()
                self.last_used_fallback = False

            def render(self, _resolution):
                raise RuntimeError("模拟先攻措辞供应商拒绝")

        self._add_test_enemy()
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.identity = "守住旧路的人"
        ilya.fabula_points = 1
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["DEX", "INS"],
                dice=[(8, 1), (10, 2)],
                total=3,
                modifier=0,
                high_roll=2,
                target_number=10,
                success=False,
                critical_success=False,
                fumble=False,
            )
        )
        self.app.start_scene(
            "风铃廊",
            location="风铃廊",
            participants=["伊莉雅", "财团机兵"],
        )
        started = self.service.gm_runtime_tools.start_conflict(
            runtime_context("财团机兵封住廊门，伊莉雅举盾迎战。"),
            {
                "scene_name": "风铃廊伏击",
                "pcs": ["伊莉雅"],
                "enemies": ["财团机兵"],
                "leader": "伊莉雅",
                "objective": "突破封锁",
                "public_opening": "机兵的长斧横在廊门前。",
                "evidence": "财团机兵封住廊门，伊莉雅举盾迎战",
            },
        )
        self.assertTrue(started.ok, started.message)
        window = self.app.interceptor.decision_window_manager.find_pending(
            kind="trait_invocation",
            owner="伊莉雅",
        )
        self.assertIsNotNone(window)
        rerolled = RollOutcome(
            actor="伊莉雅",
            attributes=["DEX", "INS"],
            dice=[(8, 7), (10, 8)],
            total=15,
            modifier=0,
            high_roll=8,
            target_number=10,
            success=True,
            critical_success=False,
            fumble=False,
        )
        self.app.expressor = FailingExpression()

        with patch.object(
            self.app.interceptor.rules_engine,
            "reroll_outcome",
            return_value=rerolled,
        ):
            resolved = self.service.gm_gameplay_tools.resolve_rule_window(
                runtime_context(
                    "伊莉雅援用守住旧路的人：我不能在伏击开始时丢掉旧路。"
                ),
                {
                    "action_type": "InvokeTrait",
                    "actor": "伊莉雅",
                    "window_id": window.window_id,
                    "choice": "守住旧路的人",
                    "details": {
                        "reroll_dice": 2,
                        "invocation_rationale": "我不能在伏击开始时丢掉旧路",
                    },
                    "evidence": "伊莉雅援用守住旧路的人",
                },
            )

        self.assertTrue(resolved.ok, resolved.message)
        self.assertTrue(self.app.conflict_manager.state.active)
        self.assertEqual(
            self.app.character_manager.get("伊莉雅").fabula_points,
            0,
        )
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=window.window_id,
            )
        )
        self.assertTrue(resolved.public_fallback_reply)
        self.assertTrue(self.app.expressor.last_used_fallback)
        self.assertTrue(self.app.recent_pipeline_spans[-1]["expression_degraded"])

    def test_team_initiative_grace_timeout_builds_turn_order_without_action_failure(self) -> None:
        self._add_test_enemy()
        ilya = self.app.character_manager.get("伊莉雅")
        ilya.identity = "守住旧路的人"
        ilya.fabula_points = 1
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="伊莉雅",
                attributes=["DEX", "INS"],
                dice=[(8, 1), (10, 2)],
                total=3,
                modifier=0,
                high_roll=2,
                target_number=10,
                success=False,
                critical_success=False,
                fumble=False,
            )
        )
        self.app.start_scene(
            "风铃廊",
            location="风铃廊",
            participants=["伊莉雅", "财团机兵"],
        )
        receipt = self.service.gm_runtime_tools.start_conflict(
            runtime_context("财团机兵封住廊门，伊莉雅举盾迎战。"),
            {
                "scene_name": "风铃廊伏击",
                "pcs": ["伊莉雅"],
                "enemies": ["财团机兵"],
                "leader": "伊莉雅",
                "objective": "突破封锁",
                "public_opening": "机兵的长斧横在廊门前。",
                "evidence": "财团机兵封住廊门，伊莉雅举盾迎战",
            },
        )
        self.assertTrue(receipt.result["initiative_pending"])
        pending = self.app.interceptor.decision_window_manager.find_pending(
            kind="trait_invocation",
            owner="伊莉雅",
        )
        self.assertIsNotNone(pending)
        pending.payload["failure_grace_due_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()

        heartbeat = self.service._session_heartbeat(
            {
                "campaign_id": "runtime-tool-test",
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
        conflict = self.app.conflict_manager.state
        self.assertTrue(conflict.active)
        self.assertEqual(conflict.current_turn_index, 0)
        self.assertEqual(conflict.current_actor(), conflict.turn_order[0])
        self.assertIn("团队先攻检定完成", heartbeat["reply"])
        self.assertIn(f"轮到【{conflict.turn_order[0]}】", heartbeat["reply"])
        self.assertNotIn("没能完成这次行动", heartbeat["reply"])
        self.assertNotIn("冲突回合表暂不建立", heartbeat["reply"])
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=pending.window_id
            )
        )

    def test_team_initiative_timeout_publishes_every_roll_exactly_once(self) -> None:
        self._add_test_enemy()
        for name in ("赛璃", "洛岚"):
            self.app.character_manager.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                    max_hp=45,
                    hp=45,
                    max_mp=40,
                    mp=40,
                    traits=["pc"],
                )
            )
        supporter = self.app.character_manager.get("赛璃")
        supporter.identity = "守望风铃的人"
        supporter.fabula_points = 1
        forced = [
            RollOutcome(
                actor="伊莉雅",
                attributes=["DEX", "INS"],
                dice=[(8, 6), (10, 5)],
                total=11,
                modifier=0,
                high_roll=6,
                target_number=5,
                success=True,
                critical_success=False,
                fumble=False,
            ),
            RollOutcome(
                actor="赛璃",
                attributes=["DEX", "INS"],
                dice=[(8, 2), (8, 3)],
                total=5,
                modifier=0,
                high_roll=3,
                target_number=10,
                success=False,
                critical_success=False,
                fumble=False,
            ),
            RollOutcome(
                actor="洛岚",
                attributes=["DEX", "INS"],
                dice=[(8, 6), (8, 5)],
                total=11,
                modifier=0,
                high_roll=6,
                target_number=10,
                success=True,
                critical_success=False,
                fumble=False,
            ),
        ]
        for outcome in forced:
            self.app.interceptor.rules_engine.force_next_check_outcome(outcome)
        self.app.start_scene(
            "风铃廊",
            location="风铃廊",
            participants=["伊莉雅", "赛璃", "洛岚", "财团机兵"],
        )
        message = "伊莉雅、赛璃和洛岚迎战封住风铃廊的财团机兵。"

        receipt = self.service.gm_runtime_tools.start_conflict(
            runtime_context(message),
            {
                "scene_name": "风铃廊伏击",
                "pcs": ["伊莉雅", "赛璃", "洛岚"],
                "enemies": ["财团机兵"],
                "leader": "伊莉雅",
                "objective": "突破封锁",
                "public_opening": "机兵的长斧横在廊门前。",
                "evidence": message,
                "_initiative_support_decisions_confirmed": True,
                "_confirmed_initiative_supporters": ["赛璃", "洛岚"],
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertTrue(receipt.result["initiative_pending"])
        batch = self.app.interceptor.check_batch_manager.find(
            receipt.result["check_batch_id"]
        )
        self.assertEqual(batch.published_roll_actors, ["伊莉雅", "赛璃"])
        pending = self.app.interceptor.decision_window_manager.find_pending(
            kind="trait_invocation",
            owner="赛璃",
        )
        self.assertIsNotNone(pending)
        pending.payload["failure_grace_due_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()

        heartbeat = self.service._session_heartbeat(
            {
                "campaign_id": "runtime-tool-test",
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
        full_public_text = "\n".join(
            (receipt.public_fallback_reply, heartbeat["reply"])
        )
        for name in ("伊莉雅", "赛璃", "洛岚"):
            self.assertEqual(
                full_public_text.count(f"{name}进行团队先攻检定"),
                1,
                full_public_text,
            )
        archived = self.app.interceptor.check_batch_manager.find(
            receipt.result["check_batch_id"]
        )
        self.assertEqual(
            archived.published_roll_actors,
            ["伊莉雅", "赛璃", "洛岚"],
        )
        self.assertIn("团队先攻检定完成", heartbeat["reply"])

    def test_start_conflict_includes_full_turn_ally_on_player_side(self) -> None:
        self._force_successful_initiative()
        self._add_test_enemy()
        self._add_test_ally()
        self.app.start_scene(
            "风铃廊",
            location="风铃廊",
            participants=["伊莉雅", "白花巡守", "财团机兵"],
        )
        message = "白花巡守与伊莉雅一同挡住财团机兵。"

        receipt = self.service.gm_runtime_tools.start_conflict(
            runtime_context(message),
            {
                "scene_name": "风铃廊伏击",
                "pcs": ["伊莉雅"],
                "allied_npcs": ["白花巡守"],
                "enemies": ["财团机兵"],
                "leader": "伊莉雅",
                "objective": "护住旅人并突破封锁",
                "public_opening": "白花巡守把长枪横在旅人身前，与伊莉雅并肩迎向机兵。",
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        state = self.app.conflict_manager.state
        self.assertEqual(state.player_side, ["伊莉雅", "白花巡守"])
        self.assertEqual(state.enemy_side, ["财团机兵"])
        self.assertEqual(
            receipt.result["turn_order"],
            ["伊莉雅", "财团机兵", "白花巡守"],
        )
        status = self.app.conflict_manager.resolution_status()
        self.assertEqual(status["active_allied_npcs"], ["白花巡守"])

        restarted = FUGMHttpService(
            data_root=self.tmpdir.name,
            use_llm=False,
        )
        load_status, loaded = restarted._load_campaign(
            {"campaign_id": "runtime-tool-test"}
        )
        self.assertEqual(load_status, 200, loaded)
        restored = restarted._runtime(
            "runtime-tool-test"
        ).app.conflict_manager.state
        self.assertEqual(restored.player_side, ["伊莉雅", "白花巡守"])
        self.assertEqual(restored.enemy_side, ["财团机兵"])

    def test_run_current_npc_turn_executes_full_turn_ally_without_targeting_pc(self) -> None:
        self._add_test_enemy()
        self._add_test_ally()
        self.app.start_scene(
            "风铃廊伏击",
            SceneType.CONFLICT,
            location="风铃廊",
            participants=["伊莉雅", "白花巡守", "财团机兵"],
        )
        self.app.conflict_manager.start_scene(
            "风铃廊伏击",
            ["白花巡守", "财团机兵", "伊莉雅"],
            player_side=["伊莉雅", "白花巡守"],
            enemy_side=["财团机兵"],
        )

        state_receipt = self.service.gm_runtime_tools.get_runtime_state(
            runtime_context("白花巡守准备出手。"),
            {},
        )
        snapshot = state_receipt.result["conflict"][
            "current_npc_tactical_snapshot"
        ]
        attack = next(
            item
            for item in snapshot["legal_actions"]
            if item["npc_action_type"] == "Attack"
        )
        self.assertEqual(attack["targets"], ["财团机兵"])
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="白花巡守",
                attributes=["DEX", "MIG"],
                dice=[(10, 6), (8, 5)],
                total=11,
                modifier=0,
                high_roll=6,
                target_number=11,
                success=True,
                critical_success=False,
                fumble=False,
                margin=0,
            )
        )

        receipt = self.service.gm_runtime_tools.run_current_npc_turn(
            runtime_context("白花巡守刺向财团机兵。"),
            {
                "expected_actor": "白花巡守",
                "npc_action_type": "Attack",
                "target": "财团机兵",
                "action_description": "白花巡守压低枪锋，迎着机兵的斧刃刺向它的膝部关节。",
            },
        )
        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["actor"], "白花巡守")
        self.assertNotEqual(
            self.app.conflict_manager.state.current_actor(),
            "白花巡守",
        )

    def test_runtime_state_summary_does_not_materialize_scene_or_memory_state(self) -> None:
        self._add_test_enemy()
        self.app.start_scene(
            "风铃廊伏击",
            SceneType.CONFLICT,
            location="风铃廊",
            participants=["伊莉雅", "财团机兵"],
        )
        self.app.conflict_manager.start_scene(
            "风铃廊伏击",
            ["财团机兵", "伊莉雅"],
            player_side=["伊莉雅"],
            enemy_side=["财团机兵"],
        )
        self.app.scene_frame_manager.current_frame = None
        self.app.scene_frame_manager.history = []
        self.app._surfaced_topic_memory_paths.clear()
        pacing_before = self.app.story_arc_manager.state.current_pacing_plan

        state = self.service.gm_runtime_tools.state_summary(
            runtime_context("财团机兵准备行动。")
        )

        self.assertIn(
            "current_npc_tactical_snapshot",
            state["conflict"],
        )
        self.assertIsNone(self.app.scene_frame_manager.current_frame)
        self.assertEqual(self.app.scene_frame_manager.history, [])
        self.assertEqual(self.app._surfaced_topic_memory_paths, set())
        self.assertIs(
            self.app.story_arc_manager.state.current_pacing_plan,
            pacing_before,
        )

    def test_runtime_state_exposes_conflict_resolution_status(self) -> None:
        self._force_successful_initiative()
        self._add_test_enemy()
        self.app.start_scene(
            "风铃廊",
            location="风铃廊",
            participants=["伊莉雅", "财团机兵"],
        )
        started = self.service.gm_runtime_tools.start_conflict(
            runtime_context("财团机兵封住廊门，伊莉雅举盾迎战。"),
            {
                "scene_name": "风铃廊伏击",
                "pcs": ["伊莉雅"],
                "enemies": ["财团机兵"],
                "leader": "伊莉雅",
                "objective": "护住失忆旅人并突破封锁",
                "public_opening": "机兵的长斧横在廊门前。",
                "evidence": "财团机兵封住廊门",
            },
        )
        self.assertTrue(started.ok, started.message)

        before = self.service.gm_runtime_tools.get_runtime_state(
            runtime_context("看看当前冲突。"),
            {},
        )
        self.assertEqual(
            before.result["conflict"]["resolution_status"]["natural_outcome"],
            "both_sides_active",
        )

        self.app.conflict_manager.surrender_combatant("财团机兵")
        after = self.service.gm_runtime_tools.get_runtime_state(
            runtime_context("机兵已经投降。"),
            {},
        )
        status = after.result["conflict"]["resolution_status"]
        self.assertTrue(status["ready_for_natural_end"])
        self.assertEqual(status["natural_outcome"], "hostile_side_removed")
        self.assertEqual(status["surrendered_combatants"], ["财团机兵"])

    def test_end_conflict_rejects_empty_outcome_or_public_closing(self) -> None:
        self._add_test_enemy()
        self.app.start_scene(
            "风铃廊伏击",
            SceneType.CONFLICT,
            participants=["伊莉雅", "财团机兵"],
        )
        self.app.conflict_manager.start_scene(
            "风铃廊伏击",
            ["伊莉雅", "财团机兵"],
            player_side=["伊莉雅"],
            enemy_side=["财团机兵"],
        )
        message = "财团机兵已经倒下。"

        no_outcome = self.service.gm_runtime_tools.end_conflict(
            runtime_context(message),
            {
                "outcome": "",
                "continue_scene": True,
                "public_reply": "机兵倒在风铃廊尽头。",
                "evidence": message,
            },
        )
        no_closing = self.service.gm_runtime_tools.end_conflict(
            runtime_context(message),
            {
                "outcome": "财团机兵失去战斗力。",
                "continue_scene": True,
                "public_reply": "",
                "evidence": message,
            },
        )

        self.assertFalse(no_outcome.ok)
        self.assertEqual(no_outcome.error_code, "CONFLICT_OUTCOME_REQUIRED")
        self.assertFalse(no_closing.ok)
        self.assertEqual(no_closing.error_code, "CONFLICT_CLOSING_REQUIRED")
        self.assertTrue(self.app.conflict_manager.state.active)

    def test_conflict_restores_existing_scene_identity_when_combat_ends(self) -> None:
        self._force_successful_initiative()
        self._add_test_enemy()
        parent = self.app.start_scene(
            "失落庭院",
            SceneType.STANDARD,
            location="旧王城",
            participants=["伊莉雅", "财团机兵"],
            objective="找到通往王座厅的暗门",
            summary="队伍已确认喷泉下藏有机关。",
        )

        started = self.service.gm_runtime_tools.start_conflict(
            runtime_context("财团机兵从喷泉后冲出，伊莉雅拔剑迎战。"),
            {
                "scene_name": "失落庭院伏击",
                "pcs": ["伊莉雅"],
                "enemies": ["财团机兵"],
                "leader": "伊莉雅",
                "objective": "击退伏兵",
                "public_opening": "机兵撞碎藤架，从喷泉后截住通往王座厅的路。",
                "evidence": "财团机兵从喷泉后冲出",
            },
        )
        self.assertTrue(started.ok, started.message)
        self.assertEqual(
            self.app.conflict_manager.state.parent_scene_id,
            parent.scene_id,
        )

        ended = self.service.gm_runtime_tools.end_conflict(
            runtime_context("财团机兵已经倒下，通往王座厅的路重新安静下来。"),
            {
                "outcome": "财团机兵被击退。",
                "continue_scene": True,
                "public_reply": "最后一具机兵倒在碎裂的藤架旁，庭院重新安静下来。",
                "evidence": "财团机兵已经倒下",
            },
        )

        self.assertTrue(ended.ok, ended.message)
        scene = self.app.scene_manager.current_scene
        self.assertEqual(scene.scene_id, parent.scene_id)
        self.assertEqual(scene.name, "失落庭院")
        self.assertEqual(scene.scene_type, SceneType.STANDARD)
        self.assertEqual(scene.objective, "找到通往王座厅的暗门")
        self.assertIn("队伍已确认喷泉下藏有机关。", scene.summary)
        self.assertIn("财团机兵被击退。", scene.summary)

    def test_end_conflict_atomically_commits_declared_pc_exit(self) -> None:
        self._add_test_enemy()
        self.app.start_scene(
            "卡里巴村监狱冲突",
            SceneType.CONFLICT,
            location="卡里巴村监狱囚室走廊",
            participants=["伊莉雅", "财团机兵"],
        )
        self.app.conflict_manager.start_scene(
            "卡里巴村监狱冲突",
            ["伊莉雅", "财团机兵"],
            player_side=["伊莉雅"],
            enemy_side=["财团机兵"],
        )
        message = "伊莉雅沿已经打开的侧门撤到监狱外的雨巷。"

        ended = self.service.gm_runtime_tools.end_conflict(
            runtime_context(message),
            {
                "outcome": "伊莉雅脱离追击，财团机兵留在监狱内。",
                "continue_scene": True,
                "exit_transitions": [
                    {
                        "destination": "卡里巴村监狱外雨巷",
                        "participants": ["伊莉雅"],
                        "scene_name": "雨巷中的喘息",
                    }
                ],
                "public_reply": "伊莉雅冲出侧门，雨水迎面扑来；机兵的脚步被甩在监狱里。",
                "evidence": "伊莉雅沿已经打开的侧门撤到监狱外的雨巷",
            },
        )

        self.assertTrue(ended.ok, ended.message)
        self.assertFalse(self.app.conflict_manager.state.active)
        self.assertEqual(
            self.app.scene_manager.location_of("伊莉雅"),
            "卡里巴村监狱外雨巷",
        )
        self.assertEqual(
            ended.result["post_conflict_transitions"][0]["participants"],
            ["伊莉雅"],
        )
        self.assertEqual(
            self.app.scene_manager.current_scene.participants,
            ["伊莉雅"],
        )
        self.assertEqual(
            self.app.scene_manager.suspended_scenes[0].participants,
            ["财团机兵"],
        )

    def test_end_conflict_accepts_exit_after_success_advanced_turn_pointer(self) -> None:
        self._add_test_enemy()
        self.app.start_scene(
            "卡里巴村监狱冲突",
            SceneType.CONFLICT,
            location="卡里巴村监狱囚室走廊",
            participants=["伊莉雅", "财团机兵"],
        )
        self.app.conflict_manager.start_scene(
            "卡里巴村监狱冲突",
            ["伊莉雅", "财团机兵"],
            player_side=["伊莉雅"],
            enemy_side=["财团机兵"],
        )
        self.app.conflict_manager.register_exit_transition(
            participants=["伊莉雅"],
            destination="卡里巴村监狱外雨巷",
            reason="伊莉雅已经穿过打开的侧门。",
        )
        self.assertNotEqual(
            self.app.conflict_manager.state.current_actor(),
            "伊莉雅",
        )
        message = "伊莉雅已经穿过侧门，结束这场追逐。"

        ended = self.service.gm_runtime_tools.end_conflict(
            runtime_context(message),
            {
                "outcome": "伊莉雅脱离追击。",
                "continue_scene": True,
                "exit_transitions": [
                    {
                        "destination": "卡里巴村监狱外雨巷",
                        "participants": ["伊莉雅"],
                    }
                ],
                "public_reply": "伊莉雅穿过侧门，雨巷隔开了身后的追兵。",
                "evidence": "伊莉雅已经穿过侧门",
            },
        )

        self.assertTrue(ended.ok, ended.message)
        self.assertEqual(
            ended.result["post_conflict_transitions"],
            [
                {
                    "destination": "卡里巴村监狱外雨巷",
                    "participants": ["伊莉雅"],
                    "scene_id": self.app.scene_manager.current_scene.scene_id,
                    "movement_mode": "created",
                }
            ],
        )

    def test_dungeon_conflict_returns_to_dungeon_without_archiving_clocks(self) -> None:
        self._force_successful_initiative()
        self._add_test_enemy()
        parent = self.app.start_scene(
            "镜之水道",
            SceneType.DUNGEON,
            location="旧王国地下",
            participants=["伊莉雅", "财团机兵"],
            objective="找到失踪的守钟人",
        )
        self.app.dungeon_manager.state.active = True
        self.app.dungeon_manager.state.name = "镜之水道"
        self.app.clock_manager.add(
            Clock(
                name="水道完全淹没",
                max_segments=6,
                clock_type="threat",
                completion_consequence="洪水封死所有出口。",
            )
        )
        self.app.dungeon_manager.state.danger_clocks = ["水道完全淹没"]

        started = self.service.gm_runtime_tools.start_conflict(
            runtime_context("财团机兵从水门后现身，挡住伊莉雅。"),
            {
                "scene_name": "水门伏击",
                "pcs": ["伊莉雅"],
                "enemies": ["财团机兵"],
                "leader": "伊莉雅",
                "objective": "突破水门伏击",
                "public_opening": "沉重的机兵踩进水里，把唯一的水门堵得严严实实。",
                "evidence": "财团机兵从水门后现身",
            },
        )
        self.assertTrue(started.ok, started.message)

        rejected = self.service.gm_runtime_tools.end_conflict(
            runtime_context("财团机兵已经倒下。"),
            {
                "outcome": "财团机兵被击退。",
                "continue_scene": False,
                "public_reply": "机兵倒进水里。",
                "evidence": "财团机兵已经倒下",
            },
        )
        self.assertFalse(rejected.ok)
        self.assertEqual(
            rejected.error_code,
            "ACTIVE_DUNGEON_REQUIRES_SCENE_CONTINUATION",
        )
        self.assertTrue(self.app.conflict_manager.state.active)

        ended = self.service.gm_runtime_tools.end_conflict(
            runtime_context("财团机兵已经倒下，队伍继续深入水道。"),
            {
                "outcome": "财团机兵被击退，水道探索继续。",
                "continue_scene": True,
                "public_reply": "机兵沉入水底，前方水道重新露了出来。",
                "evidence": "财团机兵已经倒下",
            },
        )
        self.assertTrue(ended.ok, ended.message)
        scene = self.app.scene_manager.current_scene
        self.assertEqual(scene.scene_id, parent.scene_id)
        self.assertEqual(scene.name, "镜之水道")
        self.assertEqual(scene.scene_type, SceneType.DUNGEON)
        self.assertEqual(scene.objective, "找到失踪的守钟人")
        self.assertTrue(self.app.dungeon_manager.state.active)
        self.assertTrue(self.app.clock_manager.exists("水道完全淹没"))
        self.assertEqual(
            self.app.clock_manager.get("水道完全淹没").scene_id,
            parent.scene_id,
        )

    def test_travel_conflict_must_return_to_the_active_journey(self) -> None:
        self._force_successful_initiative()
        self._add_test_enemy()
        parent = self.app.start_scene(
            "旧王城到钟鸣公国",
            SceneType.TRAVEL,
            location="盐沼堤脊",
            participants=["伊莉雅", "财团机兵"],
            objective="抵达钟鸣公国",
        )
        self.app.travel_manager.begin_journey(
            journey_id="journey-ambush",
            origin="旧王城",
            destination="钟鸣公国",
            threat_levels=["medium"],
            party_names=["伊莉雅"],
        )

        started = self.service.gm_runtime_tools.start_conflict(
            runtime_context("财团机兵从堤脊后冲出，挡住伊莉雅。"),
            {
                "scene_name": "盐沼堤脊伏击",
                "pcs": ["伊莉雅"],
                "enemies": ["财团机兵"],
                "leader": "伊莉雅",
                "objective": "突破伏击",
                "public_opening": "机兵踏碎盐壳，截住了通往钟鸣公国的堤脊。",
                "evidence": "财团机兵从堤脊后冲出",
            },
        )
        self.assertTrue(started.ok, started.message)

        rejected = self.service.gm_runtime_tools.end_conflict(
            runtime_context("财团机兵已经倒下。"),
            {
                "outcome": "财团机兵被击退。",
                "continue_scene": False,
                "public_reply": "机兵倒进盐沼。",
                "evidence": "财团机兵已经倒下",
            },
        )
        self.assertFalse(rejected.ok)
        self.assertEqual(
            rejected.error_code,
            "ACTIVE_JOURNEY_REQUIRES_SCENE_CONTINUATION",
        )
        self.assertTrue(self.app.conflict_manager.state.active)

        ended = self.service.gm_runtime_tools.end_conflict(
            runtime_context("财团机兵已经倒下，队伍继续赶路。"),
            {
                "outcome": "财团机兵被击退，旅程继续。",
                "continue_scene": True,
                "public_reply": "机兵倒进盐沼，通往钟鸣公国的堤脊重新露了出来。",
                "evidence": "财团机兵已经倒下",
            },
        )
        self.assertTrue(ended.ok, ended.message)
        scene = self.app.scene_manager.current_scene
        self.assertEqual(scene.scene_id, parent.scene_id)
        self.assertEqual(scene.scene_type, SceneType.TRAVEL)
        self.assertIsNotNone(self.app.travel_manager.active_journey)

    def test_current_enemy_turn_is_decided_and_advanced_by_npc_tool(self) -> None:
        self.app.character_manager.add(
            Character(
                name="财团机兵",
                attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 6},
                max_hp=70,
                hp=70,
                max_mp=40,
                mp=40,
                defenses={"physical": 11, "magic": 8},
                weapon_damage=14,
                traits=["enemy", "construct"],
            )
        )
        self.app.start_scene("风铃廊伏击", SceneType.CONFLICT, participants=["财团机兵", "伊莉雅"])
        self.app.conflict_manager.start_scene("风铃廊伏击", ["财团机兵", "伊莉雅"])
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="财团机兵",
                attributes=["DEX", "MIG"],
                dice=[(8, 8), (10, 10)],
                total=18,
                modifier=0,
                high_roll=10,
                target_number=10,
                success=True,
                critical_success=False,
                fumble=False,
                margin=8,
            )
        )

        receipt = self.service.gm_runtime_tools.run_current_npc_turn(
            runtime_context("机兵开始行动。"),
            {
                "expected_actor": "财团机兵",
                "npc_action_type": "Attack",
                "target": "伊莉雅",
                "action_description": "机兵抬起长斧，朝伊莉雅的盾缘重重劈下。",
                "scene_brief": "机兵正挡在旅人与出口之间。",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["actor"], "财团机兵")
        self.assertEqual(receipt.result["next_actor"], "伊莉雅")
        self.assertLess(self.app.character_manager.get("伊莉雅").hp, 45)
        self.assertFalse(
            self.app.interceptor.decision_window_manager.pending(
                kind="critical_opportunity",
                owner="财团机兵",
            )
        )

    def test_npc_fumble_waits_for_target_player_opportunity_choice(self) -> None:
        self.app.character_manager.add(
            Character(
                name="财团机兵",
                attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 6},
                max_hp=70,
                hp=70,
                max_mp=40,
                mp=40,
                defenses={"physical": 11, "magic": 8},
                weapon_damage=14,
                traits=["enemy", "construct"],
            )
        )
        self.app.start_scene(
            "风铃廊伏击",
            SceneType.CONFLICT,
            participants=["财团机兵", "伊莉雅"],
        )
        self.app.conflict_manager.start_scene(
            "风铃廊伏击",
            ["财团机兵", "伊莉雅"],
            player_side=["伊莉雅"],
            enemy_side=["财团机兵"],
        )
        self.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="财团机兵",
                attributes=["DEX", "MIG"],
                dice=[(8, 1), (10, 1)],
                total=2,
                modifier=0,
                high_roll=1,
                target_number=10,
                success=False,
                critical_success=False,
                fumble=True,
                opportunity_count=1,
                margin=-8,
            )
        )

        receipt = self.service.gm_runtime_tools.run_current_npc_turn(
            runtime_context("机兵抬斧攻击伊莉雅。"),
            {
                "expected_actor": "财团机兵",
                "npc_action_type": "Attack",
                "target": "伊莉雅",
                "action_description": "机兵抬起长斧劈向伊莉雅。",
                "scene_brief": "机兵挡在旅人与出口之间。",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["required_followup_tools"], [])
        pending = receipt.result["pending_decisions"]
        fumble = next(
            item
            for item in pending
            if item["kind"] == "fumble_opportunity"
            and item["owner"] == "伊莉雅"
        )
        self.assertTrue(
            GMToolReceiptPolicy.terminal_public_change_committed(
                receipt,
                terminal_public_tools=frozenset({"run_current_npc_turn"}),
            )
        )

        resolved = self.service.gm_gameplay_tools.resolve_rule_window(
            runtime_context("我把机兵大失败带来的机会用于情报。"),
            {
                "action_type": "TriggerOpportunity",
                "actor": "伊莉雅",
                "window_id": fumble["window_id"],
                "choice": "情报",
                "details": {
                    "information": "机兵左膝的传动轴已经因盐雾锈蚀。"
                },
                "evidence": "我把机兵大失败带来的机会用于情报",
            },
        )

        self.assertTrue(resolved.ok, resolved.message)
        self.assertFalse(
            self.app.interceptor.decision_window_manager.pending(
                kind="fumble_opportunity",
                owner="伊莉雅",
            )
        )
        self.assertEqual(
            self.app.conflict_manager.state.current_actor(),
            "伊莉雅",
        )

    def test_npc_turn_tool_never_takes_a_player_turn(self) -> None:
        self._add_test_enemy()
        self.app.start_scene(
            "风铃廊伏击",
            SceneType.CONFLICT,
            participants=["伊莉雅", "财团机兵"],
        )
        self.app.conflict_manager.start_scene(
            "风铃廊伏击",
            ["伊莉雅", "财团机兵"],
            player_side=["伊莉雅"],
            enemy_side=["财团机兵"],
        )

        receipt = self.service.gm_runtime_tools.run_current_npc_turn(
            runtime_context("轮到伊莉雅。"),
            {"expected_actor": "伊莉雅", "scene_brief": "机兵封住廊门。"},
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "CURRENT_ACTOR_IS_PLAYER")
        self.assertEqual(self.app.conflict_manager.state.current_actor(), "伊莉雅")

    def test_npc_turn_tool_refuses_to_act_after_player_side_is_removed(self) -> None:
        self._add_test_enemy()
        self.app.start_scene(
            "风铃廊伏击",
            SceneType.CONFLICT,
            participants=["财团机兵", "伊莉雅"],
        )
        self.app.conflict_manager.start_scene(
            "风铃廊伏击",
            ["财团机兵", "伊莉雅"],
            player_side=["伊莉雅"],
            enemy_side=["财团机兵"],
        )
        self.app.character_manager.get("伊莉雅").hp = 0
        self.app.conflict_manager.resolve_zero_hp("伊莉雅")
        self.app.conflict_manager.resolve_pending_zero_hp(
            "伊莉雅",
            choice="give_up_resistance",
            consequence="分离：被财团机兵俘获",
        )
        actor = str(self.app.conflict_manager.state.current_actor() or "")

        receipt = self.service.gm_runtime_tools.run_current_npc_turn(
            runtime_context("财团机兵准备继续行动。"),
            {
                "expected_actor": actor,
                "npc_action_type": "Guard",
                "action_description": "财团机兵守住牢门。",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "CONFLICT_READY_TO_END")
        self.assertEqual(
            receipt.result["conflict_resolution_status"]["natural_outcome"],
            "player_side_removed",
        )

    def test_pause_session_persists_gate_across_service_restart(self) -> None:
        self.service.session_gates.activate(
            "runtime-tool-test",
            "group-1",
            "s1",
            status="adventure",
            reason="正在冒险",
        )

        receipt = self.service.gm_tool_registry.execute(
            "pause_session",
            {"reason": "今晚先停在这里"},
            runtime_context("今晚先暂停跑团。"),
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(
            self.service.session_gates.get(
                "runtime-tool-test",
                "group-1",
                "s1",
            ).status,
            "paused",
        )
        restarted = FUGMHttpService(
            data_root=self.tmpdir.name,
            use_llm=False,
        )
        restored = restarted.session_gates.get(
            "runtime-tool-test",
            "group-1",
            "s1",
        )
        self.assertEqual(restored.status, "paused")
        self.assertEqual(restored.reason, "今晚先停在这里")


if __name__ == "__main__":
    unittest.main()
