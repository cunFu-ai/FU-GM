from __future__ import annotations

import tempfile
import unittest

from fu_gm.gm_tool_contracts import GMToolExecutionContext, GMToolReceipt
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import Character, EnemyRank, RollOutcome


CAMPAIGN_ID = "post-chapter-lifecycle"
SESSION_ID = "session-01"
CHANNEL_ID = "group-1"


def context(
    message: str,
    *,
    session_id: str = SESSION_ID,
) -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id=CAMPAIGN_ID,
        session_id=session_id,
        channel_id=CHANNEL_ID,
        speaker="阿凛",
        gate_status="adventure",
        directly_addressed=True,
        metadata={
            "current_message": message,
            "recent_public_context": message,
        },
    )


class PostChapterToolLifecycleTests(unittest.TestCase):
    """Exercise one authoritative path across every core post-Chapter-1 phase."""

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
                hp=45,
                max_mp=35,
                mp=25,
                crisis_threshold=22,
                inventory_points=6,
                max_inventory_points=6,
                fabula_points=3,
                zenit=500,
                experience_points=5,
                level=5,
                classes={"武器大师": 3, "旅人": 2},
                skills={
                    "碎骨": 1,
                    "近战武器精通": 1,
                    "宝物猎人": 1,
                },
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
        self.service.session_gates.activate(
            CAMPAIGN_ID,
            CHANNEL_ID,
            SESSION_ID,
            status="adventure",
        )
        self.app.start_session_tracking(
            SESSION_ID,
            participating_pcs=["伊莉雅"],
        )
        self.service._autosave_campaign(self.runtime, CAMPAIGN_ID)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, object],
        message: str,
        *,
        session_id: str = SESSION_ID,
    ) -> GMToolReceipt:
        receipt = self.service.gm_tool_registry.execute(
            tool_name,
            arguments,
            context(message, session_id=session_id),
        )
        self.assertTrue(
            receipt.ok,
            f"{tool_name}: {receipt.error_code} {receipt.message} "
            f"{receipt.correction_hint}",
        )
        return receipt

    def reload(self) -> None:
        self.service = FUGMHttpService(
            data_root=self.tmpdir.name,
            use_llm=False,
        )
        self.runtime = self.service._runtime(CAMPAIGN_ID)
        self.app = self.runtime.app

    @staticmethod
    def force_check(
        app,
        *,
        actor: str,
        attributes: list[str],
        total: int,
        target_number: int,
        target: str,
        reason: str,
        damage: int = 0,
    ) -> None:
        first = max(1, total // 2)
        second = max(1, total - first)
        app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor=actor,
                attributes=attributes,
                dice=[(12, first), (12, second)],
                total=total,
                modifier=0,
                high_roll=max(first, second),
                target_number=target_number,
                success=total >= target_number,
                critical_success=False,
                fumble=False,
                margin=total - target_number,
                target=target,
                reason=reason,
                damage=damage,
            )
        )

    def test_full_post_chapter_tool_lifecycle_survives_restarts(self) -> None:
        scene = self.execute(
            "start_scene",
            {
                "name": "白花碑驿站的旧闸",
                "scene_type": "standard",
                "location": "白花碑驿站",
                "participants": ["伊莉雅"],
                "objective": "取得进入镜之水道的旧路许可",
                "private_situation": {
                    "current_pressure": "闸后的水位正在上涨",
                    "visible_elements": ["锈蚀锁轮", "刻着旧王徽的水门"],
                    "clue_pool": ["守钟人的钥匙能解除最后一道锁舌"],
                },
                "public_opening": (
                    "旧闸后的水声一阵高过一阵，锈蚀锁轮却纹丝不动。"
                    "伊莉雅刚走到水门前，廊下便传来一串钥匙碰响。"
                ),
                "player_handoff": "钥匙声正朝旧闸靠近，伊莉雅，你先做什么？",
            },
            "伊莉雅来到白花碑驿站的旧闸，准备寻找进入水道的办法。",
        )
        self.assertTrue(scene.result["saved_path"])

        introduced = self.execute(
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
                "public_reply": (
                    "守钟人阿莱提着一串铜钥匙从廊柱后走出来，"
                    "停在锁轮旁打量伊莉雅。"
                ),
                "public_facts": [
                    "守钟人阿莱提着一串铜钥匙从廊柱后走出来，停在锁轮旁打量伊莉雅。"
                ],
            },
            "伊莉雅循着钥匙声望向廊柱后方。",
        )
        self.assertTrue(introduced.result["profile_created"])

        response = self.execute(
            "decide_npc_response",
            {
                "name": "守钟人阿莱",
                "actor": "伊莉雅",
                "public_segments": [
                    {
                        "text": "阿莱把最旧的那枚钥匙挑出来：“水门能开，但进去以后别碰会自己转动的钟。”",
                        "tags": ["direct_answer", "fact"],
                    }
                ],
                "speech_act": "answer",
                "stance": "愿意有限合作",
                "intent": "让伊莉雅安全通过旧闸",
                "emotion": "谨慎",
            },
            "伊莉雅问阿莱，怎样才能安全进入镜之水道。",
        )
        self.assertIn("水门能开", response.public_fallback_reply)

        self.execute(
            "create_clock",
            {
                "name": "解除旧闸锁舌",
                "segments": 4,
                "clock_type": "objective",
                "scope": "scene",
                "stakes": "锁舌全部解除后旧闸开启",
                "completion_consequence": "旧闸开启，队伍可以进入镜之水道",
                "auto_advance": False,
                "visibility": "foreground",
                "public_reply": "最后几道锁舌仍卡在水门深处。\n【解除旧闸锁舌】0/4",
            },
            "阿莱说明还要逐一解除卡死的锁舌。",
        )
        declared = self.execute(
            "declare_check_action",
            {
                "action_type": "Objective",
                "actor": "伊莉雅",
                "target": "解除旧闸锁舌",
                "clock_name": "解除旧闸锁舌",
                "clock_direction": "填充",
                "attributes": ["敏捷", "洞察"],
                "difficulty": 9,
                "purpose": "借阿莱的钥匙解除第一道锁舌",
                "check_label": "解除旧闸锁舌",
                "success_observation": "第一道锁舌缩回水门，锁轮终于转过四分之一圈。",
                "failure_consequence": (
                    "伊莉雅这次未能借阿莱的钥匙解除第一道锁舌；"
                    "本次尝试没有造成其他现场变化。"
                ),
                "failure_authority": {"kind": "attempt"},
            },
            "伊莉雅接过钥匙，试着解除第一道锁舌。",
        )
        self.assertEqual(self.app.clock_manager.get("解除旧闸锁舌").current, 0)
        pending_roll = self.app.interceptor.decision_window_manager.find_pending(
            kind="check_roll_confirmation",
            owner="伊莉雅",
        )
        self.assertIsNotNone(pending_roll)
        self.assertEqual(declared.result["window_id"], pending_roll.window_id)

        # The roll question is part of the authoritative campaign state. A
        # process restart must restore it before the player answers.
        self.reload()
        restored_roll = self.app.interceptor.decision_window_manager.find_pending(
            window_id=pending_roll.window_id,
        )
        self.assertIsNotNone(restored_roll)
        self.force_check(
            self.app,
            actor="伊莉雅",
            attributes=["DEX", "INS"],
            total=9,
            target_number=9,
            target="解除旧闸锁舌",
            reason="转动第一道锁舌",
        )
        advanced = self.execute(
            "resolve_rule_window",
            {
                "action_type": "ResolveDecision",
                "actor": "伊莉雅",
                "window_id": restored_roll.window_id,
                "choice": "roll",
                "details": {},
            },
            "投。",
        )
        self.assertEqual(
            self.app.clock_manager.get("解除旧闸锁舌").current,
            1,
        )
        self.assertIsNone(
            self.app.interceptor.decision_window_manager.find_pending(
                window_id=restored_roll.window_id,
            )
        )
        self.assertEqual(advanced.result["pending_decisions"], [])

        completed = self.execute(
            "fill_clock",
            {
                "name": "解除旧闸锁舌",
                "amount": 3,
                "cause": "direct_action_success",
                "reason": "阿莱与伊莉雅依次解开剩余锁舌",
                "public_reply": (
                    "剩余锁舌接连缩回，旧闸终于向内升起。"
                    "\n【解除旧闸锁舌】4/4"
                ),
                "completion_facts": ["旧闸终于向内升起"],
            },
            "伊莉雅和阿莱一起解开了剩余锁舌。",
        )
        self.assertEqual(completed.result["status"], "resolved")
        self.assertFalse(self.app.clock_manager.exists("解除旧闸锁舌"))
        self.assertIsNotNone(
            self.app.clock_manager.archived_match("解除旧闸锁舌")
        )

        self.execute(
            "end_scene",
            {
                "summary": "阿莱开启旧闸，伊莉雅取得前往镜之水道的路线。",
                "public_reply": "旧闸在身后停稳，通往镜之水道的石路已经露了出来。",
            },
            "伊莉雅确认路线后离开旧闸。",
        )

        self.app.interceptor.rules_engine.roll_die = lambda _sides: 8
        journey = self.execute(
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
            "伊莉雅沿旧路徒步前往镜之水道。",
        )
        self.assertEqual(journey.result["status"], "event_pending")

        self.reload()
        self.assertIsNotNone(self.app.travel_manager.active_journey)
        self.assertEqual(
            self.app.travel_manager.active_journey.status,
            "event_pending",
        )
        self.app.interceptor.rules_engine.roll_die = lambda _sides: 4
        continued = self.execute(
            "continue_travel",
            {
                "event_resolution": "伊莉雅用绳索固定松动石板，安全越过塌陷路段。",
            },
            "松动石板已经固定，伊莉雅安全越过塌陷路段并继续赶路。",
        )
        self.assertEqual(continued.result["status"], "arrived")
        self.assertEqual(
            self.app.scene_manager.current_scene.location,
            "镜之水道",
        )

        dungeon = self.execute(
            "start_dungeon_exploration",
            {
                "name": "镜之水道",
                "location": "镜之水道",
                "importance": "major",
                "preparation": "prepared",
                "mode": "detailed",
                "purpose": "找到失踪的守钟人记录",
                "concept": "倒映旧日景象的地下水道",
                "focus": "被封存的守钟日志",
                "inhabitants": "失控的古代构装体",
                "peculiarity": "水面会映出一天后的景象",
                "participants": ["伊莉雅"],
            },
            "伊莉雅推开水门，进入镜之水道。",
        )
        self.assertTrue(dungeon.result["state"]["active"])
        area_names = [
            area.name for area in self.app.dungeon_manager.state.areas
        ]
        self.assertGreaterEqual(len(area_names), 2)
        target_area = area_names[1]
        self.execute(
            "perform_scene_action",
            {
                "action_type": "ExploreDungeon",
                "actor": "伊莉雅",
                "details": {
                    "area_name": target_area,
                    "mode": "enter",
                },
            },
            f"伊莉雅从入口进入{target_area}。",
        )
        self.force_check(
            self.app,
            actor="伊莉雅",
            attributes=["INS", "INS"],
            total=10,
            target_number=9,
            target=target_area,
            reason="搜索守钟日志",
        )
        declared_search = self.execute(
            "declare_check_action",
            {
                "action_type": "Investigate",
                "actor": "伊莉雅",
                "target": f"{target_area}的积水与石柜",
                "attributes": ["洞察", "洞察"],
                "difficulty": 9,
                "purpose": "寻找被封存的守钟日志",
                "check_label": "搜索水道石柜",
                "success_observation": "石柜底层压着一册封蜡完整的守钟日志。",
                "failure_consequence": (
                    "伊莉雅这次未能寻找被封存的守钟日志；"
                    "本次尝试没有造成其他现场变化。"
                ),
                "failure_authority": {"kind": "attempt"},
                "details": {"dungeon_area": target_area},
            },
            f"伊莉雅搜索{target_area}的积水与石柜。",
        )
        searched = self.execute(
            "resolve_rule_window",
            {
                "action_type": "ResolveDecision",
                "actor": "伊莉雅",
                "window_id": declared_search.result["window_id"],
                "choice": "roll",
                "details": {},
            },
            "投。",
        )
        check_receipt_id = str(
            searched.result["check_receipt"]["receipt_id"]
        )
        self.execute(
            "perform_scene_action",
            {
                "action_type": "ExploreDungeon",
                "actor": "伊莉雅",
                "details": {
                    "area_name": target_area,
                    "mode": "search",
                    "check_receipt_id": check_receipt_id,
                },
            },
            "伊莉雅从石柜底层取出守钟日志。",
        )
        self.execute(
            "finish_dungeon_exploration",
            {
                "outcome": "completed",
                "completion_reason": "守钟日志已经找到，伊莉雅退出水道。",
                "exit_location": "镜之水道入口",
            },
            "伊莉雅带着守钟日志离开镜之水道。",
        )

        enemy = Character(
            name="水道机兵",
            attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 6},
            max_hp=70,
            hp=70,
            max_mp=40,
            mp=40,
            defenses={"physical": 10, "magic": 8},
            initiative=5,
            weapon_accuracy_attributes=["MIG", "MIG"],
            weapon_damage=50,
            traits=["enemy", "construct"],
        )
        self.app.character_manager.add(enemy)
        self.app.conflict_manager.register_enemy(
            "水道机兵",
            EnemyRank.SOLDIER,
        )
        self.app.character_manager.get("伊莉雅").hp = 1
        self.force_check(
            self.app,
            actor="伊莉雅",
            attributes=["DEX", "INS"],
            total=9,
            target_number=5,
            target="团队先攻",
            reason="团队先攻",
        )
        conflict = self.execute(
            "start_conflict",
            {
                "scene_name": "水道入口伏击",
                "pcs": ["伊莉雅"],
                "enemies": ["水道机兵"],
                "leader": "伊莉雅",
                "objective": "带着守钟日志突破机兵封锁",
                "public_opening": "水道机兵从退潮后的石槽里站起，铁臂已经封住出口。",
            },
            "水道机兵封住出口，伊莉雅举剑迎战。",
        )
        self.assertTrue(conflict.result["turn_order"])
        self.assertEqual(
            self.app.conflict_manager.state.current_actor(),
            "伊莉雅",
        )
        self.execute(
            "perform_character_action",
            {
                "action_type": "Guard",
                "actor": "伊莉雅",
                "details": {},
            },
            "伊莉雅举盾防御，护住装着守钟日志的背包。",
        )
        self.assertEqual(
            self.app.conflict_manager.state.current_actor(),
            "水道机兵",
        )
        self.force_check(
            self.app,
            actor="水道机兵",
            attributes=["MIG", "MIG"],
            total=12,
            target_number=8,
            target="伊莉雅",
            reason="铁臂横扫",
            damage=50,
        )
        npc_turn = self.execute(
            "run_current_npc_turn",
            {
                "expected_actor": "水道机兵",
                "npc_action_type": "Attack",
                "attack_name": self.app.character_manager.get(
                    "水道机兵"
                ).npc_attacks[0].name,
                "target": "伊莉雅",
                "action_description": "水道机兵踏碎浅水，抡起铁臂横扫伊莉雅的盾侧。",
            },
            "轮到水道机兵行动。",
        )
        pending = [
            item
            for item in npc_turn.result["pending_decisions"]
            if item["kind"] == "zero_hp"
        ]
        self.assertEqual(len(pending), 1)

        self.reload()
        restored_window = self.app.interceptor.decision_window_manager.find_pending(
            window_id=pending[0]["window_id"]
        )
        self.assertIsNotNone(restored_window)
        self.execute(
            "resolve_rule_window",
            {
                "action_type": "ResolveZeroHP",
                "actor": "伊莉雅",
                "window_id": restored_window.window_id,
                "choice": "give_up_resistance",
                "details": {
                    "consequence_type": "分离",
                    "consequence": "被铁臂击落到入口下方的安全浅滩",
                },
            },
            "伊莉雅选择放弃抵抗，被击落到入口下方的安全浅滩。",
        )
        self.execute(
            "end_conflict",
            {
                "outcome": "伊莉雅保住日志后被迫撤出水道入口。",
                "continue_scene": False,
                "public_reply": "机兵没有追下浅滩；伊莉雅抱紧日志，从另一侧水沟撤了出去。",
            },
            "伊莉雅已经脱离机兵的追击，这场冲突结束。",
        )

        self.execute(
            "start_scene",
            {
                "name": "水道外的避雨棚",
                "scene_type": "standard",
                "location": "镜之水道入口",
                "participants": ["伊莉雅"],
                "objective": "休整并整理守钟日志",
                "private_situation": {},
                "public_opening": "雨水敲在旧棚顶上，水道机兵的脚步声已经听不见了。",
                "player_handoff": "伊莉雅，你准备怎样利用这段喘息？",
            },
            "伊莉雅抵达水道外的避雨棚，准备休整。",
        )
        hero = self.app.character_manager.get("伊莉雅")
        self.assertEqual(hero.hp, hero.crisis_threshold)
        rested = self.execute(
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
            "伊莉雅在安全的避雨棚里支起魔法帐篷休息。",
        )
        self.assertTrue(rested.state_changed)
        self.assertEqual(hero.hp, hero.max_hp)
        self.assertEqual(hero.inventory_points, 2)

        bought = self.execute(
            "perform_scene_action",
            {
                "action_type": "Shop",
                "actor": "伊莉雅",
                "details": {
                    "mode": "buy",
                    "item_name": "丝质衬衫",
                    "quantity": 1,
                    "equip": False,
                },
            },
            "回到入口商棚后，伊莉雅买下一件丝质衬衫。",
        )
        self.assertTrue(bought.state_changed)
        self.execute(
            "perform_character_action",
            {
                "action_type": "Equip",
                "actor": "伊莉雅",
                "details": {
                    "slots": {
                        "armor": "丝质衬衫",
                    }
                },
            },
            "伊莉雅换上刚买的丝质衬衫。",
        )
        self.assertEqual(
            self.app.character_manager.get("伊莉雅").equipped_armor,
            "丝质衬衫",
        )

        ended = self.execute(
            "end_session",
            {
                "title": "镜之水道",
                "closing_image": "雨水仍敲着旧棚顶，伊莉雅怀里的守钟日志已经沾上从水道带出的泥痕。",
                "public_reply": (
                    "今晚先停在避雨棚。雨水仍敲着旧棚顶，伊莉雅怀里的守钟日志"
                    "已经沾上从水道带出的泥痕。守钟日志和所有进度都已经保存。"
                ),
            },
            "大家决定今晚先收团。",
        )
        self.assertTrue(ended.result["experience"])
        self.assertIn("伊莉雅", ended.result["level_up_available"])

        self.reload()
        progression = self.execute(
            "get_progression_state",
            {},
            "查看伊莉雅的升级状态。",
        )
        hero_progression = next(
            row
            for row in progression.result["characters"]
            if row["name"] == "伊莉雅"
        )
        self.assertTrue(hero_progression["can_level_up"])
        leveled = self.execute(
            "level_up_character",
            {
                "character_name": "伊莉雅",
                "class_name": "武器大师",
                "skill_name": "碎骨",
            },
            "伊莉雅把这一级投入武器大师，并提升碎骨。",
        )
        self.assertEqual(leveled.result["level_up"]["level_after"], 6)

        saved = self.execute(
            "save_campaign",
            {
                "campaign_id": CAMPAIGN_ID,
                "slot": "镜之水道收团后",
            },
            "把镜之水道收团后的进度另存一份。",
        )
        self.assertEqual(saved.result["slot"], "镜之水道收团后")
        self.app.character_manager.get("伊莉雅").zenit = 1
        loaded = self.execute(
            "load_campaign",
            {
                "campaign_id": CAMPAIGN_ID,
                "slot": "镜之水道收团后",
            },
            "读取镜之水道收团后的存档。",
        )
        self.assertEqual(loaded.result["slot"], "镜之水道收团后")
        self.assertGreater(
            self.service._runtime(CAMPAIGN_ID)
            .app.character_manager.get("伊莉雅")
            .zenit,
            1,
        )


if __name__ == "__main__":
    unittest.main()
