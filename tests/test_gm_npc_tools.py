from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fu_gm.components.npc_response_window_manager import NPCResponseWindowManager
from fu_gm.components.npc_voice_renderer import NPCVoiceRenderer
from fu_gm.gm_tool_agent import GMToolExecutionContext
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import Affinity, Character, SceneType, StatusEffect


def npc_context(
    message: str,
    *,
    system_beat: bool = False,
) -> GMToolExecutionContext:
    metadata: dict[str, object] = {
        "current_message": message,
        "recent_public_context": "众人站在白花碑驿站的风铃廊里。",
    }
    if system_beat:
        metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "npc_move",
                "heartbeat_require_material_change": True,
            }
        )
    return GMToolExecutionContext(
        campaign_id="npc-tool-test",
        session_id="s1",
        channel_id="group-1",
        speaker="阿凛",
        gate_status="adventure",
        directly_addressed=True,
        metadata=metadata,
    )


def direct_response(
    *,
    name: str,
    evidence: str,
    text: str,
    actor: str = "伊莉雅",
    tags: list[str] | None = None,
    **state: object,
) -> dict[str, object]:
    return {
        "name": name,
        "actor": actor,
        "public_segments": [
            {
                "id": "answer",
                "text": text,
                "tags": list(tags or ["direct_answer"]),
            }
        ],
        "speech_act": "answer",
        "condition_outcome": "none",
        "proposal_outcome": "none",
        "promise_kind": "none",
        "commitment_outcome": "none",
        "evidence": evidence,
        **state,
    }


class GMNPCToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.service = FUGMHttpService(
            data_root=self.tmpdir.name,
            use_llm=False,
        )
        self.runtime = self.service._runtime("npc-tool-test")
        self.app = self.runtime.app
        if not self.app.character_manager.exists("伊莉雅"):
            self.app.character_manager.add(
                Character(
                    name="伊莉雅",
                    level=5,
                    attributes={"DEX": 8, "INS": 10, "MIG": 8, "WLP": 8},
                    max_hp=45,
                    hp=45,
                    max_mp=45,
                    mp=45,
                    traits=["pc"],
                )
            )
        scene = self.app.start_scene(
            "白花碑驿站",
            SceneType.STANDARD,
            location="风铃廊",
            participants=["伊莉雅"],
        )
        self.app.scene_frame_manager.ensure_frame(
            scene=scene,
            recent_chat="众人站在白花碑驿站的风铃廊里。",
            world_state=self.app.world_state,
            character_manager=self.app.character_manager,
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_npc_tool_schemas_state_positive_scope_and_keep_authority(self) -> None:
        schemas = {
            item["name"]: item
            for item in self.service.gm_tool_registry.schemas()
        }

        profile = schemas["create_npc_profile"]
        introduction = schemas["introduce_npc"]
        response = schemas["decide_npc_response"]
        response_properties = response["parameters"]["properties"]
        design_commit = schemas["commit_npc_combatant_design"]
        design_finalize = schemas["finalize_npc_combatant_preparation"]

        self.assertIn("已经实际进入当前场景或确定即将登场", profile["description"])
        self.assertNotIn("不要为玩家随口假设", profile["description"])
        self.assertIn("本工具完成的是公开登场", introduction["description"])
        self.assertIn("introduced_npcs", introduction["parameters"]["properties"])
        self.assertEqual(
            introduction["parameters"]["properties"]["introduced_npcs"]["maxItems"],
            4,
        )
        self.assertEqual(response_properties["introduced_npcs"]["maxItems"], 2)
        fact_effects = response_properties["fact_effects"]
        self.assertEqual(fact_effects["maxItems"], 4)
        self.assertEqual(
            set(fact_effects["items"]["properties"]["kind"]["enum"]),
            {"objective", "claim", "rumor", "lie"},
        )
        self.assertIn("调用范围是玩家已经实际提交的NPC交互", response["description"])
        self.assertIn(
            "待答问题ID使用pending_question_id",
            response_properties["condition_id"]["description"],
        )
        self.assertIn(
            "数组仅列玩家本句实际回应的项目",
            response_properties["response_items"]["description"],
        )
        self.assertEqual(
            response_properties["pending_question_handling"]["enum"],
            ["responding", "unrelated"],
        )
        self.assertIn("规则校验", design_commit["description"])
        self.assertIn("不创建参战角色", design_finalize["description"])
        self.assertNotIn("preview_npc_combatant", schemas)
        self.assertNotIn("commit_npc_combatant_preview", schemas)
        self.assertNotIn("create_npc_combatant", schemas)

    def _create_npc(
        self,
        *,
        name: str = "白花守望会会长",
        entity_kind: str = "individual",
        present: bool = True,
    ):
        message = (
            f"{name}从廊柱后走出来。"
            if present
            else f"{name}会在英雄离开驿站后从旧路现身。"
        )
        return self.service.gm_npc_tools.create_npc_profile(
            npc_context(message),
            {
                "name": name,
                "profile": {
                    "entity_kind": entity_kind,
                    "public_identity": "白花守望会的负责人",
                    "role_in_story": "旧路的本地守护者",
                    "core_drive": "保护驿站与受庇护的旅人",
                    "manner": "克制而警惕",
                    "speech_style": "短句，先回答再说明边界",
                    "npc_rank": "supporting",
                    "leverage": "旧路闸门的临时开闭权",
                    "authority_scope": "能决定驿站旧路今晚是否开放",
                    "knowledge_scope": "熟悉驿站、旧路与守望会的公开安排",
                    "refusal_move": "关上闸门并让巡守护送平民撤离",
                    "active_goal": "判断英雄是否值得信任",
                    "goals": ["守住受庇护者", "查明财团为何追来"],
                    "secrets": ["她认得失忆旅人的旧姓"],
                    "voice_examples": ["“先说去向。其他事等门开了再谈。”"],
                },
                "present_in_scene": present,
                "planned_entry": not present,
                "evidence": message,
            },
        )

    def _commit_combatant(
        self,
        name: str,
        *,
        level: int = 5,
        combat_side: str = "enemy",
        is_villain: bool = False,
        ultima_points: int = 0,
    ):
        if name not in self.app.world_state.npc_personas:
            self.assertTrue(self._create_npc(name=name).ok)
        prepared = self.service.gm_npc_tools.prepare_npc_combatant(
            npc_context(f"后台准备{name}的规则卡。"),
            {
                "name": name,
                "level": level,
                "species": "humanoid",
                "rank": "soldier",
                "champion_value": 1,
                "combat_side": combat_side,
                "is_villain": is_villain,
                "ultima_points": ultima_points,
                "preferred_template": "守卫",
                "background": False,
            },
        )
        self.assertTrue(prepared.ok, prepared.message)
        self.assertEqual(prepared.result["status"], "ready")
        queried = self.service.gm_npc_tools.get_npc_combatant_design(
            npc_context(f"查询{name}的规则卡。"),
            {"name": name},
        )
        self.assertTrue(queried.ok, queried.message)
        self.assertEqual(queried.result["npc_name"], name)
        message = f"{name}已经确定参与眼前冲突。"
        return self.service.gm_npc_tools.commit_npc_combatant_design(
            npc_context(message),
            {"name": name, "evidence": message},
        )

    def test_core_gm_state_receives_full_private_npc_profile(self) -> None:
        self.assertTrue(self._create_npc().ok)

        state = self.service.gm_npc_tools.state_summary(
            npc_context("会长，东侧旧路今晚能走吗？")
        )

        profile = next(
            item
            for item in state["present_npcs"]
            if item["name"] == "白花守望会会长"
        )
        self.assertEqual(profile["core_drive"], "保护驿站与受庇护的旅人")
        self.assertEqual(profile["active_goal"], "判断英雄是否值得信任")
        self.assertIn("她认得失忆旅人的旧姓", profile["secrets"])
        self.assertEqual(
            profile["authority_scope"],
            "能决定驿站旧路今晚是否开放",
        )
        self.assertIn("dialogue_authority", state)
        self.assertFalse(hasattr(self.app, "npc_decision_planner"))

    def test_blueprint_commit_keeps_full_turn_ally_on_player_side(self) -> None:
        name = "白花巡守"

        receipt = self._commit_combatant(name, combat_side="ally")

        self.assertTrue(receipt.ok, receipt.message)
        combatant = self.app.character_manager.get(name)
        self.assertIn("ally", combatant.traits)
        self.assertNotIn("enemy", combatant.traits)
        self.assertEqual(self.app.conflict_manager.combat_side(name), "player")
        self.assertFalse(self.app.conflict_manager.is_villain(name))

    def test_background_blueprint_finalization_persists_without_joining_conflict(self) -> None:
        name = "灰衣追猎者"
        self.assertTrue(self._create_npc(name=name, present=False).ok)
        prepared = self.service.gm_npc_tools.prepare_npc_combatant(
            npc_context(f"后台准备{name}的规则卡。"),
            {
                "name": name,
                "level": 10,
                "species": "humanoid",
                "rank": "elite",
                "champion_value": 1,
                "combat_side": "enemy",
                "is_villain": False,
                "ultima_points": 0,
                "preferred_template": "守卫",
                "background": False,
            },
        )
        self.assertTrue(prepared.ok, prepared.message)

        before_version = self.runtime.state_version
        finalized = self.service.gm_tool_registry.execute(
            "finalize_npc_combatant_preparation",
            {"name": name},
            npc_context("把追猎者的后台规则卡准备好，但先不要让他入场。"),
            side_effect_lock=self.runtime.transaction_lock,
        )

        self.assertTrue(finalized.ok, finalized.message)
        self.assertTrue(finalized.state_changed)
        self.assertEqual(self.runtime.state_version, before_version + 1)
        self.assertFalse(self.app.character_manager.exists(name))
        self.assertNotIn(name, self.app.conflict_manager.state.enemy_ranks)
        self.assertTrue(
            any(
                event.kind == "npc_combat_blueprint_prepared"
                and event.payload.get("blueprint_id")
                == prepared.result["blueprint_id"]
                for event in self.app.world_state.memory_events
            )
        )
        snapshot = json.loads(
            Path(finalized.result["saved_path"]).read_text(encoding="utf-8")
        )
        self.assertIn(name, json.dumps(snapshot, ensure_ascii=False))

    def test_planned_npc_stays_offstage_while_private_combat_card_prewarms(self) -> None:
        name = "灰衣追猎者"
        message = "灰衣追猎者会在英雄离开驿站后从旧路现身。"

        receipt = self.service.gm_npc_tools.create_npc_profile(
            npc_context(message),
            {
                "name": name,
                "profile": {
                    "public_identity": "财团追猎者",
                    "role_in_story": "在旧路伏击遗物持有者",
                    "core_drive": "把失落遗物带回财团",
                    "combat_style": "封路后从高处射击",
                    "traits": ["耐心", "冷酷", "谨慎", "惜命"],
                    "npc_rank": "elite",
                    "active_goal": "等英雄进入狭窄旧路再动手",
                },
                "present_in_scene": False,
                "planned_entry": True,
                "evidence": message,
            },
        )
        jobs = [
            job_id
            for job_id, record in self.app.npc_blueprint_designer._jobs.items()
            if record.get("npc_name") == name
        ]
        for job_id in jobs:
            self.app.npc_blueprint_designer.wait(job_id, timeout=3)

        self.assertTrue(receipt.ok, receipt.message)
        self.assertNotIn(name, self.app.scene_manager.current_scene.participants)
        persona = self.app.world_state.npc_personas[name]
        self.assertEqual(persona.current_location, "")
        self.assertEqual(persona.last_seen_scene, "")
        self.assertIn(name, self.app.world_state.npc_combat_blueprints)

    def test_scene_placeholder_can_be_enriched_without_losing_identity(self) -> None:
        scene = self.app.scene_manager.current_scene
        assert scene is not None
        name = "巡守弥纱"
        self.app.scene_manager.add_participant(name)
        placeholder = self.app.world_state.ensure_npc_persona(
            name,
            profile_status="placeholder",
            public_identity=name,
            role_in_story="当前场景中的非玩家角色",
            first_scene=scene.name,
            current_location=scene.location,
            last_seen_scene=scene.scene_id,
        )
        stable_id = placeholder.npc_id
        message = "巡守弥纱已经站在门边，并表明自己负责带队。"

        receipt = self.service.gm_npc_tools.create_npc_profile(
            npc_context(message),
            {
                "name": name,
                "profile": {
                    "public_identity": "白花守望会巡守弥纱",
                    "role_in_story": "旧路带队巡守",
                    "core_drive": "把旅人安全送出驿站",
                    "manner": "谨慎而利落",
                    "speech_style": "短句，先说结论",
                    "npc_rank": "supporting",
                },
                "present_in_scene": True,
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertFalse(receipt.result["profile_created"])
        self.assertTrue(receipt.result["profile_enriched"])
        persona = self.app.world_state.npc_personas[name]
        self.assertEqual(persona.npc_id, stable_id)
        self.assertEqual(persona.profile_status, "established")
        self.assertEqual(persona.public_identity, "白花守望会巡守弥纱")
        self.assertEqual(persona.role_in_story, "旧路带队巡守")
        self.assertEqual(persona.core_drive, "把旅人安全送出驿站")

    def test_introduce_present_placeholder_requests_profile_enrichment(self) -> None:
        scene = self.app.scene_manager.current_scene
        assert scene is not None
        name = "巡守弥纱"
        self.app.scene_manager.add_participant(name)
        self.app.world_state.ensure_npc_persona(
            name,
            profile_status="placeholder",
            public_identity=name,
            role_in_story="当前场景中的非玩家角色",
            current_location=scene.location,
            last_seen_scene=scene.scene_id,
        )
        message = "巡守弥纱已经在门边。"

        receipt = self.service.gm_npc_tools.introduce_npc(
            npc_context(message),
            {
                "name": name,
                "profile": {
                    "public_identity": name,
                    "role_in_story": "旧路带队巡守",
                },
                "public_reply": "巡守弥纱已经在门边。",
                "public_facts": ["巡守弥纱已经在门边。"],
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "NPC_PRESENT_PROFILE_PLACEHOLDER",
        )

    def test_prepared_npc_location_does_not_count_as_scene_presence(self) -> None:
        scene = self.app.scene_manager.current_scene
        assert scene is not None
        self.app.world_state.ensure_npc_persona(
            "监察官艾蕾娜",
            public_identity="监察官",
            role_in_story="财团监察官",
            current_location=scene.location,
        )

        receipt = self.service.gm_npc_tools.introduce_npc(
            npc_context("监察官带队抵达驿站。"),
            {
                "name": "监察官艾蕾娜",
                "profile": {
                    "public_identity": "监察官",
                    "role_in_story": "财团监察官",
                },
                "public_reply": "监察官带队抵达驿站。",
                "public_facts": ["监察官带队抵达驿站。"],
                "evidence": "监察官带队抵达驿站。",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertIn(
            "监察官艾蕾娜",
            self.app.scene_manager.current_scene.participants,
        )

    def test_introduce_npc_can_atomically_bring_publicly_named_retinue(self) -> None:
        message = "监察官艾蕾娜带着财团机兵与财团狙击手抵达风铃廊。"

        receipt = self.service.gm_npc_tools.introduce_npc(
            npc_context(message),
            {
                "name": "监察官艾蕾娜",
                "profile": {
                    "public_identity": "监察官艾蕾娜",
                    "role_in_story": "财团监察官",
                    "npc_rank": "villain",
                    "active_goal": "封锁旧路并扣押失忆旅人",
                },
                "introduced_npcs": [
                    {
                        "name": "财团机兵",
                        "profile": {
                            "public_identity": "财团机兵",
                            "role_in_story": "监察官的近卫",
                            "npc_rank": "supporting",
                            "active_goal": "封住旧路入口",
                        },
                    },
                    {
                        "name": "财团狙击手",
                        "profile": {
                            "public_identity": "财团狙击手",
                            "role_in_story": "监察官的远程支援",
                            "npc_rank": "supporting",
                            "active_goal": "控制风铃廊制高点",
                        },
                    },
                ],
                "public_reply": message,
                "public_facts": [message],
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(
            [item["name"] for item in receipt.result["introduced_npcs"]],
            ["财团机兵", "财团狙击手"],
        )
        self.assertEqual(receipt.public_fallback_reply, message)
        for name in ("监察官艾蕾娜", "财团机兵", "财团狙击手"):
            self.assertIn(name, self.app.world_state.npc_personas)
            self.assertIn(name, self.app.scene_manager.current_scene.participants)

    def test_group_introduction_fails_atomically_when_companion_is_not_public(self) -> None:
        message = "监察官艾蕾娜独自走入风铃廊。"

        receipt = self.service.gm_npc_tools.introduce_npc(
            npc_context(message),
            {
                "name": "监察官艾蕾娜",
                "profile": {
                    "public_identity": "监察官艾蕾娜",
                    "role_in_story": "财团监察官",
                    "npc_rank": "villain",
                    "active_goal": "封锁旧路",
                },
                "introduced_npcs": [
                    {
                        "name": "财团机兵",
                        "profile": {
                            "public_identity": "财团机兵",
                            "role_in_story": "监察官的近卫",
                            "npc_rank": "supporting",
                        },
                    }
                ],
                "public_reply": message,
                "public_facts": [message],
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "NPC_COMPANION_IDENTITY_NOT_PUBLIC")
        self.assertNotIn("监察官艾蕾娜", self.app.world_state.npc_personas)
        self.assertNotIn("财团机兵", self.app.world_state.npc_personas)

    def test_core_gm_segments_remain_safe_fallback_without_voice_renderer(self) -> None:
        self.assertTrue(self._create_npc().ok)
        message = "伊莉雅问会长：东侧旧路今晚能走吗？"
        text = "会长把钥匙压在掌下。“能走，但只能由巡守带你们过第一道门。”"

        receipt = self.service.gm_npc_tools.decide_npc_response(
            npc_context(message),
            direct_response(
                name="白花守望会会长",
                evidence=message,
                text=text,
                stance="允许临时通行，但不交出钥匙",
                intent="安排巡守带队",
                emotion="仍有戒心",
            ),
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.public_fallback_reply, text)
        self.assertTrue(receipt.lock_public_reply)
        persona = self.app.world_state.npc_personas["白花守望会会长"]
        self.assertEqual(persona.current_stance, "允许临时通行，但不交出钥匙")
        self.assertEqual(persona.active_goal, "安排巡守带队")
        self.assertEqual(persona.current_mood, "仍有戒心")
        self.assertTrue(any(text in item for item in persona.memories))

    def test_npc_response_can_atomically_establish_a_local_improvised_fact(self) -> None:
        self.assertTrue(self._create_npc().ok)
        message = "伊莉雅问会长：昨晚是谁负责旧路后门？"
        fact = "昨晚负责旧路后门的是巡守弥纱。"

        receipt = self.service.gm_npc_tools.decide_npc_response(
            npc_context(message),
            direct_response(
                name="白花守望会会长",
                evidence=message,
                text="“昨晚守后门的是弥纱。”",
                fact_effects=[
                    {
                        "kind": "objective",
                        "scope": "local",
                        "fact": fact,
                        "related_entities": ["巡守弥纱", "旧路后门"],
                    }
                ],
            ),
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["committed_fact_effects"][0]["kind"], "objective")
        frame = self.app.scene_frame_manager.current_frame
        self.assertIn(fact, frame.established_facts)
        events = [
            event
            for event in self.app.world_state.memory_events
            if event.kind == "gm_improvised_local_fact"
        ]
        self.assertEqual(events[-1].summary, fact)
        self.assertEqual(events[-1].payload["scope"], "local")

    def test_npc_claim_is_remembered_as_a_statement_not_objective_truth(self) -> None:
        self.assertTrue(self._create_npc().ok)
        message = "伊莉雅问会长：失踪的巡守去了哪里？"
        claim = "失踪的巡守去了北岸。"

        receipt = self.service.gm_npc_tools.decide_npc_response(
            npc_context(message),
            direct_response(
                name="白花守望会会长",
                evidence=message,
                text="“我认为她去了北岸。”",
                fact_effects=[
                    {"kind": "claim", "scope": "local", "fact": claim}
                ],
            ),
        )

        self.assertTrue(receipt.ok, receipt.message)
        frame = self.app.scene_frame_manager.current_frame
        self.assertNotIn(claim, frame.established_facts)
        self.assertTrue(
            any("NPC【白花守望会会长】公开回应" in item for item in frame.established_facts)
        )
        event = next(
            event
            for event in reversed(self.app.world_state.memory_events)
            if event.kind == "npc_public_claim"
        )
        self.assertIn("声称", event.summary)
        self.assertEqual(event.payload["kind"], "claim")

    def test_npc_lie_keeps_truth_status_private_from_public_memory_summary(self) -> None:
        self.assertTrue(self._create_npc().ok)
        message = "伊莉雅问会长：钥匙在你手里吗？"
        false_statement = "旧路钥匙已经被烧毁。"

        receipt = self.service.gm_npc_tools.decide_npc_response(
            npc_context(message),
            direct_response(
                name="白花守望会会长",
                evidence=message,
                text="“钥匙早就烧掉了。”",
                fact_effects=[
                    {"kind": "lie", "scope": "scene", "fact": false_statement}
                ],
            ),
        )

        self.assertTrue(receipt.ok, receipt.message)
        event = next(
            event
            for event in reversed(self.app.world_state.memory_events)
            if event.kind == "npc_public_statement"
        )
        self.assertNotIn("谎", event.summary)
        self.assertNotIn("kind", event.payload)
        private_event = next(
            event
            for event in reversed(self.app.world_state.memory_events)
            if event.kind == "npc_statement_truth"
        )
        self.assertEqual(private_event.visibility.value, "private")
        self.assertEqual(private_event.payload["kind"], "lie")
        self.assertNotIn(
            false_statement,
            self.app.scene_frame_manager.current_frame.established_facts,
        )

    def test_admit_unknown_cannot_commit_fact_effects(self) -> None:
        self.assertTrue(self._create_npc().ok)
        message = "伊莉雅问会长：你见过老科特吗？"
        arguments = direct_response(
            name="白花守望会会长",
            evidence=message,
            text="“我没见过，但他去了北岸。”",
            speech_act="admit_unknown",
            fact_effects=[
                {
                    "kind": "objective",
                    "scope": "local",
                    "fact": "老科特去了北岸。",
                }
            ],
        )

        receipt = self.service.gm_npc_tools.decide_npc_response(
            npc_context(message),
            arguments,
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "NPC_UNKNOWN_CANNOT_ESTABLISH_FACT")
        self.assertFalse(
            any(
                event.summary == "老科特去了北岸。"
                for event in self.app.world_state.memory_events
            )
        )

    def test_validated_npc_voice_is_the_single_public_and_persisted_answer(self) -> None:
        class VoiceClient:
            config = type("Config", (), {"response_format_enabled": True})()

            def create_chat_completion(self, **_kwargs: object) -> str:
                return (
                    '{"rendered_segments":[{"id":"answer",'
                    '"text":"会长用拇指压住钥匙。‘能走，但由我们的巡守领路。’"}]}'
                )

        class AuditClient:
            config = type("Config", (), {"response_format_enabled": True})()

            def create_chat_completion(self, **_kwargs: object) -> str:
                return (
                    '{"valid":true,"missing_segment_ids":[],'
                    '"unsupported_claims":[],"reason":"一致"}'
                )

        self.assertTrue(self._create_npc().ok)
        self.app.npc_voice_renderer = NPCVoiceRenderer(
            client=VoiceClient(),
            model="deepseek-v4-flash",
            audit_client=AuditClient(),
            audit_model="gpt-5.6-terra",
        )
        message = "伊莉雅问会长：东侧旧路今晚能走吗？"
        source_text = "东侧旧路今晚可以通行，但只能由巡守带队。"

        receipt = self.service.gm_npc_tools.decide_npc_response(
            npc_context(message),
            direct_response(
                name="白花守望会会长",
                evidence=message,
                text=source_text,
            ),
        )

        expected = "会长用拇指压住钥匙。‘能走，但由我们的巡守领路。’"
        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.public_fallback_reply, expected)
        self.assertFalse(receipt.result["npc_voice"]["used_fallback"])
        persona_state = self.app.world_state.npc_personas["白花守望会会长"]
        self.assertTrue(any(expected in item for item in persona_state.memories))
        self.assertFalse(any(source_text in item for item in persona_state.memories))

    def test_invalid_direct_output_does_not_partially_change_npc_state(self) -> None:
        self.assertTrue(self._create_npc().ok)
        persona = self.app.world_state.npc_personas["白花守望会会长"]
        before = (
            persona.current_stance,
            persona.active_goal,
            persona.current_mood,
        )
        message = "伊莉雅问会长：能开门吗？"

        receipt = self.service.gm_npc_tools.decide_npc_response(
            npc_context(message),
            {
                **direct_response(
                    name="白花守望会会长",
                    evidence=message,
                    text="这段会被替换为空数组。",
                    stance="错误状态",
                    intent="错误目标",
                    emotion="错误情绪",
                ),
                "public_segments": [],
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "NPC_RESPONSE_TRANSACTION_INVALID")
        after = (
            persona.current_stance,
            persona.active_goal,
            persona.current_mood,
        )
        self.assertEqual(after, before)

    def test_long_player_request_returns_actionable_split_hint(self) -> None:
        self.assertTrue(self._create_npc().ok)
        message = "伊莉雅等待会长提出最后条件。"
        arguments = direct_response(
            name="白花守望会会长",
            evidence=message,
            text="会长先解释整段来龙去脉。" * 20 + "你们答应吗？",
            tags=["player_request"],
        )

        receipt = self.service.gm_npc_tools.decide_npc_response(
            npc_context(message),
            arguments,
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "NPC_RESPONSE_TRANSACTION_INVALID")
        self.assertIn(
            "只把NPC此刻要求玩家回答的最后一个短问题单独成段",
            receipt.correction_hint,
        )

    def test_direct_response_can_open_one_explicit_player_request(self) -> None:
        self.assertTrue(self._create_npc().ok)
        message = "伊莉雅问会长怎样才肯开门。"
        arguments = direct_response(
            name="白花守望会会长",
            evidence=message,
            text="“告诉我你们要去哪里，以及由谁护送那名旅人。”",
            tags=["player_request"],
            response_addressee="伊莉雅",
        )

        receipt = self.service.gm_npc_tools.decide_npc_response(
            npc_context(message),
            arguments,
        )

        self.assertTrue(receipt.ok, receipt.message)
        opened = receipt.result["opened_player_request"]
        self.assertEqual(opened["addressed_actor"], "伊莉雅")
        self.assertEqual(opened["npc"], "白花守望会会长")
        required = NPCResponseWindowManager.required_items(
            opened["required_items"]
        )
        self.assertEqual(
            required[0]["prompt"],
            "告诉我你们要去哪里，以及由谁护送那名旅人。",
        )

    def test_fulfilling_npc_condition_is_local_payoff_not_session_resolution(self) -> None:
        self.assertTrue(self._create_npc().ok)
        condition = self.app.scene_frame_manager.record_condition(
            npc="白花守望会会长",
            condition="答应不把旧路记录外传",
            promised_result="会长开放地下室旧记录。",
            scene=self.app.scene_manager.current_scene,
        )
        self.assertIsNotNone(condition)
        message = "伊莉雅答应不把旧路记录外传，请会长履行承诺。"

        receipt = self.service.gm_npc_tools.decide_npc_response(
            npc_context(message),
            direct_response(
                name="白花守望会会长",
                evidence=message,
                text="会长开放地下室旧记录。",
                tags=["direct_answer", "gate_payoff"],
                condition_id=condition["condition_id"],
                condition_outcome="fulfilled",
            ),
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(
            receipt.pacing_events[0].local_payoff,
            "会长开放地下室旧记录",
        )
        self.assertFalse(receipt.pacing_events[0].local_question_resolved)

    def test_teammate_can_answer_npc_question_addressed_to_another_hero(self) -> None:
        self.assertTrue(self._create_npc().ok)
        if not self.app.character_manager.exists("伊大石"):
            self.app.character_manager.add(
                Character(
                    name="伊大石",
                    level=5,
                    attributes={"DEX": 6, "INS": 6, "MIG": 10, "WLP": 10},
                    max_hp=55,
                    hp=55,
                    max_mp=55,
                    mp=55,
                    traits=["pc"],
                )
            )
        self.app.scene_manager.add_participant("伊大石")
        request = NPCResponseWindowManager.open_request(
            self.app.scene_frame_manager.current_frame,
            npc="白花守望会会长",
            summary="你们要找的人叫什么，有什么特征？",
            required_items=[
                {"item_id": "identity", "prompt": "姓名与外貌特征"}
            ],
            addressed_actor="伊莉雅",
        )
        self.assertIsNotNone(request)
        message = "伊大石从伊莉雅身后走出：我师傅叫老科特，灰白头发，右腿有点瘸。"

        receipt = self.service.gm_npc_tools.decide_npc_response(
            npc_context(message),
            direct_response(
                name="白花守望会会长",
                actor="伊大石",
                evidence=message,
                text="会长记下这些特征。“我会让巡守留意。”",
                pending_question_id=request["question_id"],
                response_items=[{"item_id": "identity", "kind": "answer"}],
            ),
        )

        self.assertTrue(receipt.ok, receipt.message)
        update = receipt.result["player_response_updates"][0]
        self.assertTrue(update["complete"])
        self.assertEqual(update["answered_item_ids"], ["identity"])

    def test_npc_response_requires_explicit_handling_of_compatible_open_request(self) -> None:
        self.assertTrue(self._create_npc().ok)
        request = NPCResponseWindowManager.open_request(
            self.app.scene_frame_manager.current_frame,
            npc="白花守望会会长",
            summary="你们愿意守住入口吗？",
            required_items=[{"item_id": "consent", "prompt": "是否愿意守住入口"}],
            response_scope="party",
        )
        self.assertIsNotNone(request)
        message = "伊莉雅点头：我愿意守住入口。"

        receipt = self.service.gm_npc_tools.decide_npc_response(
            npc_context(message),
            direct_response(
                name="白花守望会会长",
                actor="伊莉雅",
                evidence=message,
                text="会长把入口交给了她。",
            ),
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "NPC_PENDING_QUESTION_HANDLING_REQUIRED",
        )
        self.assertTrue(receipt.retryable)
        self.assertEqual(
            receipt.result["pending_questions"][0]["question_id"],
            request["question_id"],
        )
        self.assertEqual(request["status"], "open")

    def test_npc_response_can_explicitly_leave_unrelated_open_request_pending(self) -> None:
        self.assertTrue(self._create_npc().ok)
        request = NPCResponseWindowManager.open_request(
            self.app.scene_frame_manager.current_frame,
            npc="白花守望会会长",
            summary="你们愿意守住入口吗？",
            required_items=[{"item_id": "consent", "prompt": "是否愿意守住入口"}],
            response_scope="party",
        )
        self.assertIsNotNone(request)
        message = "伊莉雅问：旧路通向哪里？"

        receipt = self.service.gm_npc_tools.decide_npc_response(
            npc_context(message),
            direct_response(
                name="白花守望会会长",
                actor="伊莉雅",
                evidence=message,
                text="“通向南边的废弃驿道。”",
                pending_question_handling="unrelated",
            ),
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["player_response_updates"], [])
        self.assertEqual(request["status"], "open")

    def test_actor_only_npc_question_reports_exact_speaker_mismatch(self) -> None:
        self.assertTrue(self._create_npc().ok)
        if not self.app.character_manager.exists("伊大石"):
            self.app.character_manager.add(
                Character(
                    name="伊大石",
                    level=5,
                    attributes={"DEX": 6, "INS": 6, "MIG": 10, "WLP": 10},
                    max_hp=55,
                    hp=55,
                    max_mp=55,
                    mp=55,
                    traits=["pc"],
                )
            )
        request = NPCResponseWindowManager.open_request(
            self.app.scene_frame_manager.current_frame,
            npc="白花守望会会长",
            summary="伊莉雅本人是否愿意留下？",
            required_items=[
                {"item_id": "consent", "prompt": "你本人是否愿意留下"}
            ],
            addressed_actor="伊莉雅",
            response_scope="actor_only",
        )
        self.assertIsNotNone(request)
        message = "伊大石替伊莉雅答应留下。"

        receipt = self.service.gm_npc_tools.decide_npc_response(
            npc_context(message),
            direct_response(
                name="白花守望会会长",
                actor="伊大石",
                evidence=message,
                text="会长没有把这当成伊莉雅本人的答复。",
                pending_question_id=request["question_id"],
                response_items=[{"item_id": "consent", "kind": "answer"}],
            ),
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "NPC_PLAYER_RESPONSE_ACTOR_MISMATCH",
        )
        self.assertEqual(receipt.result["required_actor"], "伊莉雅")
        self.assertEqual(receipt.result["actual_actor"], "伊大石")

    def test_direct_response_can_introduce_an_authorized_attendant_atomically(self) -> None:
        self.assertTrue(self._create_npc().ok)
        message = "伊莉雅请会长派一名熟悉旧路的人带路。"
        text = "会长朝门侧招手。“巡守弥纱，你带他们过第一道闸门。”"
        arguments = direct_response(
            name="白花守望会会长",
            evidence=message,
            text=text,
            proposal_outcome="accepted",
            introduced_npcs=[
                {
                    "name": "巡守弥纱",
                    "profile": {
                        "public_identity": "白花守望会巡守",
                        "role_in_story": "旧路向导",
                        "core_drive": "安全完成会长交付的引路任务",
                        "authority_scope": "可带人通过旧路第一道闸门",
                        "active_goal": "带英雄抵达第一处安全节点",
                    },
                }
            ],
        )

        receipt = self.service.gm_npc_tools.decide_npc_response(
            npc_context(message),
            arguments,
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertIn("巡守弥纱", self.app.world_state.npc_personas)
        self.assertIn(
            "巡守弥纱",
            self.app.scene_manager.current_scene.participants,
        )
        self.assertEqual(receipt.public_fallback_reply, text)

    def test_absent_npc_cannot_answer_across_scene_boundary(self) -> None:
        self.assertTrue(self._create_npc().ok)
        self.app.scene_manager.current_scene.participants.remove(
            "白花守望会会长"
        )
        persona = self.app.world_state.npc_personas["白花守望会会长"]
        persona.current_location = "白花碑驿站外"
        message = "伊莉雅在风铃廊里问会长：你还在吗？"

        receipt = self.service.gm_npc_tools.decide_npc_response(
            npc_context(message),
            direct_response(
                name="白花守望会会长",
                evidence=message,
                text="“我在。”",
            ),
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "NPC_NOT_PRESENT")

    def test_collective_uses_collective_tool_without_inventing_a_leader(self) -> None:
        self.assertTrue(
            self._create_npc(
                name="白花巡守队",
                entity_kind="collective",
            ).ok
        )
        message = "伊莉雅问白花巡守队：你们愿意守住后门吗？"
        arguments = direct_response(
            name="白花巡守队",
            evidence=message,
            text="巡守们互相看了一眼，随后一齐把长枪转向后门。",
            proposal_outcome="accepted",
        )

        wrong_tool = self.service.gm_npc_tools.decide_npc_response(
            npc_context(message),
            arguments,
        )
        receipt = self.service.gm_npc_tools.decide_collective_response(
            npc_context(message),
            arguments,
        )

        self.assertFalse(wrong_tool.ok)
        self.assertEqual(wrong_tool.error_code, "COLLECTIVE_TOOL_REQUIRED")
        self.assertTrue(receipt.ok, receipt.message)
        self.assertTrue(receipt.result["collective"])
        self.assertEqual(receipt.public_fallback_reply, arguments["public_segments"][0]["text"])

    def test_autonomous_npc_action_requires_trusted_gm_beat(self) -> None:
        self.assertTrue(self._create_npc().ok)
        arguments = {
            "name": "白花守望会会长",
            "public_segments": [
                {
                    "id": "move",
                    "text": "会长忽然合上门闩，示意巡守熄掉廊下的灯。",
                    "tags": ["nonverbal"],
                }
            ],
            "speech_act": "answer",
            "condition_outcome": "none",
            "proposal_outcome": "none",
            "promise_kind": "none",
            "commitment_outcome": "none",
            "stance": "开始封锁驿站",
            "intent": "让外面的追兵失去目标",
        }

        rejected = self.service.gm_npc_tools.decide_npc_action(
            npc_context("会长行动。"),
            arguments,
        )
        accepted = self.service.gm_npc_tools.decide_npc_action(
            npc_context("系统主动节拍", system_beat=True),
            arguments,
        )

        self.assertFalse(rejected.ok)
        self.assertEqual(rejected.error_code, "TRUSTED_GM_BEAT_REQUIRED")
        self.assertTrue(accepted.ok, accepted.message)
        self.assertEqual(
            accepted.public_fallback_reply,
            "会长忽然合上门闩，示意巡守熄掉廊下的灯。",
        )

    def test_autonomous_npc_beat_rejects_rephrased_committed_state(self) -> None:
        self.assertTrue(self._create_npc().ok)
        frame = self.app.scene_frame_manager.current_frame
        frame.committed_consequences.append(
            "会长已经合上门闩，廊下的灯已经熄灭。"
        )
        arguments = {
            "name": "白花守望会会长",
            "public_segments": [
                {
                    "id": "move",
                    "text": "会长又把门闩压紧，示意巡守确认廊灯已经熄灭。",
                    "tags": ["nonverbal"],
                }
            ],
            "speech_act": "answer",
            "condition_outcome": "none",
            "proposal_outcome": "none",
            "promise_kind": "none",
            "commitment_outcome": "none",
            "stance": "继续封锁驿站",
            "intent": "维持已经成立的封锁",
        }

        receipt = self.service.gm_npc_tools.decide_npc_action(
            npc_context("系统主动节拍", system_beat=True),
            arguments,
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "NPC_BEAT_RESTATES_COMMITTED_STATE",
        )
        self.assertNotIn(arguments["public_segments"][0]["text"], frame.recent_beats)

    def test_material_npc_beat_does_not_turn_unknown_into_invented_trigger(self) -> None:
        self.assertTrue(self._create_npc().ok)
        arguments = {
            "name": "白花守望会会长",
            "public_segments": [
                {
                    "id": "unknown",
                    "text": "会长摇了摇头。“这件事我不知道。”",
                    "tags": ["direct_answer"],
                }
            ],
            "speech_act": "admit_unknown",
            "condition_outcome": "none",
            "proposal_outcome": "none",
            "promise_kind": "none",
            "commitment_outcome": "none",
        }

        receipt = self.service.gm_npc_tools.decide_npc_action(
            npc_context("系统主动节拍", system_beat=True),
            arguments,
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "NPC_BEAT_NOT_MATERIAL")
        self.assertIn("保持静默", receipt.correction_hint)
        self.assertIn("不得为满足节拍而引入新人物", receipt.correction_hint)
        self.assertNotIn("改用introduce_npc", receipt.correction_hint)

    def test_legacy_pc_persona_is_hidden_and_cannot_use_npc_action_tools(self) -> None:
        # Older snapshots could accidentally persist a persona record for a
        # player character. Character.traits remains the ownership authority.
        self.app.world_state.ensure_npc_persona(
            "伊莉雅",
            public_identity="伊莉雅",
            role_in_story="误建档的玩家角色",
            current_location="风铃廊",
        )
        arguments = {
            "name": "伊莉雅",
            "public_segments": [
                {
                    "id": "move",
                    "text": "伊莉雅替队伍把引路牌压进识别槽。",
                    "tags": ["nonverbal"],
                }
            ],
            "speech_act": "answer",
            "condition_outcome": "none",
            "proposal_outcome": "none",
            "promise_kind": "none",
            "commitment_outcome": "none",
            "stance": "替玩家决定行动",
            "intent": "推进闸门机关",
        }

        summary = self.service.gm_npc_tools.state_summary(
            npc_context("系统主动节拍", system_beat=True)
        )
        action = self.service.gm_npc_tools.decide_npc_action(
            npc_context("系统主动节拍", system_beat=True),
            arguments,
        )
        response = self.service.gm_npc_tools.decide_npc_response(
            npc_context("伊莉雅，你来回答。"),
            {**arguments, "evidence": "伊莉雅，你来回答。"},
        )

        self.assertNotIn("伊莉雅", [row["name"] for row in summary["present_npcs"]])
        self.assertFalse(action.ok)
        self.assertEqual(action.error_code, "PLAYER_CHARACTER_CANNOT_USE_NPC_TOOL")
        self.assertFalse(response.ok)
        self.assertEqual(response.error_code, "PLAYER_CHARACTER_CANNOT_USE_NPC_TOOL")

    def test_configure_boss_phases_builds_executable_stages_and_persists(self) -> None:
        name = "潮钟执政官"
        combat_receipt = self._commit_combatant(
            name,
            level=20,
            is_villain=True,
            ultima_points=5,
        )
        self.assertTrue(combat_receipt.ok, combat_receipt.message)
        message = "潮钟执政官会在钟壳破碎后显露潮汐核心。"
        receipt = self.service.gm_npc_tools.configure_boss_phases(
            npc_context(message),
            {
                "name": name,
                "phases": [
                    {
                        "name": "潮汐核心",
                        "public_cue": "青铜钟壳裂开，海蓝色核心在胸腔中亮起。",
                        "hp_restore": 80,
                        "mp_restore": 30,
                        "added_statuses": ["激怒"],
                        "affinity_changes": {
                            "火": "resist",
                            "冰": "weak",
                        },
                        "added_spells": ["冰山术"],
                        "action_count": 2,
                        "preferred_actions": ["Spell", "Objective"],
                        "tactic_hints": ["先冻结旧路闸门，再攻击持钟者。"],
                    }
                ],
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        stage = self.app.conflict_manager.state.escalation_stages[name][0]
        self.assertEqual(stage.transition_kind, "boss_phase")
        self.assertTrue(stage.preparation_round)
        self.assertEqual(stage.ultima_points, 0)
        self.assertEqual(stage.hp_restore, 80)
        self.assertEqual(stage.mp_restore, 30)
        self.assertEqual(stage.added_statuses, [StatusEffect.ENRAGED])
        self.assertEqual(stage.affinity_changes["fire"], Affinity.RESIST)
        self.assertEqual(stage.affinity_changes["ice"], Affinity.WEAK)
        self.assertEqual(stage.added_spells, ["冰山术"])
        self.assertEqual(stage.action_count, 2)

        restarted = FUGMHttpService(
            data_root=self.tmpdir.name,
            use_llm=False,
        )
        status, loaded = restarted._load_campaign(
            {"campaign_id": "npc-tool-test"}
        )
        self.assertEqual(status, 200, loaded)
        restored = (
            restarted._runtime("npc-tool-test")
            .app.conflict_manager.state.escalation_stages[name][0]
        )
        self.assertEqual(restored.transition_kind, "boss_phase")
        self.assertEqual(restored.added_statuses, [StatusEffect.ENRAGED])
        self.assertEqual(restored.affinity_changes["ice"], Affinity.WEAK)
        self.assertEqual(restored.added_spells, ["冰山术"])

    def test_configure_boss_phases_rejects_non_villain(self) -> None:
        name = "辉钢守卫"
        combat_receipt = self._commit_combatant(name)
        self.assertTrue(combat_receipt.ok, combat_receipt.message)
        message = "辉钢守卫准备变形。"

        receipt = self.service.gm_npc_tools.configure_boss_phases(
            npc_context(message),
            {
                "name": name,
                "phases": [
                    {
                        "name": "过载形态",
                        "public_cue": "装甲缝隙亮起红光。",
                    }
                ],
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "BOSS_PHASES_REQUIRE_VILLAIN",
        )

    def test_configure_boss_phases_rejects_unknown_spell(self) -> None:
        name = "潮钟执政官"
        combat_receipt = self._commit_combatant(
            name,
            is_villain=True,
            ultima_points=5,
        )
        self.assertTrue(combat_receipt.ok, combat_receipt.message)
        message = "潮钟执政官准备变形。"

        receipt = self.service.gm_npc_tools.configure_boss_phases(
            npc_context(message),
            {
                "name": name,
                "phases": [
                    {
                        "name": "过载形态",
                        "public_cue": "装甲缝隙亮起红光。",
                        "added_spells": ["不存在的法术"],
                    }
                ],
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "BOSS_PHASE_SPELL_UNKNOWN")

    def test_configure_boss_phases_cannot_rewrite_started_phase(self) -> None:
        name = "潮钟执政官"
        combat_receipt = self._commit_combatant(
            name,
            is_villain=True,
            ultima_points=5,
        )
        self.assertTrue(combat_receipt.ok, combat_receipt.message)
        self.app.conflict_manager.state.current_escalation_stage[name] = 0
        message = "潮钟执政官已经进入第二形态。"

        receipt = self.service.gm_npc_tools.configure_boss_phases(
            npc_context(message),
            {
                "name": name,
                "phases": [
                    {
                        "name": "改写形态",
                        "public_cue": "刚才的变化被重新解释。",
                    }
                ],
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "BOSS_PHASE_ALREADY_STARTED")

    def test_update_npc_state_rejects_identical_active_goal_without_side_effects(
        self,
    ) -> None:
        self.assertTrue(self._create_npc().ok)
        persona = self.app.world_state.npc_personas["白花守望会会长"]
        active_goal = persona.active_goal
        self.assertNotIn(active_goal, persona.goals)
        message = "会长仍在判断英雄是否值得信任。"

        with patch.object(self.service, "_autosave_campaign") as autosave:
            receipt = self.service.gm_npc_tools.update_npc_state(
                npc_context(message),
                {
                    "name": persona.name,
                    "patch": {"active_goal": active_goal},
                    "evidence": message,
                },
            )

        self.assertFalse(receipt.ok)
        self.assertFalse(receipt.state_changed)
        self.assertFalse(receipt.retryable)
        self.assertEqual(receipt.error_code, "NPC_STATE_NO_CHANGE")
        self.assertEqual(persona.active_goal, active_goal)
        self.assertNotIn(active_goal, persona.goals)
        autosave.assert_not_called()

    def test_update_npc_state_completed_goal_clears_active_goal_once(self) -> None:
        self.assertTrue(self._create_npc().ok)
        persona = self.app.world_state.npc_personas["白花守望会会长"]
        completed_goal = persona.active_goal
        # 兼容旧存档中的不一致状态：目标虽已列入完成清单，但只要仍是
        # active_goal，本次完成提交就必须清空当前目标，不能误判为no-op。
        persona.completed_goals.append(completed_goal)
        message = "会长已经判断完英雄是否值得信任。"

        first = self.service.gm_npc_tools.update_npc_state(
            npc_context(message),
            {
                "name": persona.name,
                "patch": {"completed_goal": completed_goal},
                "evidence": message,
            },
        )
        repeated = self.service.gm_npc_tools.update_npc_state(
            npc_context(message),
            {
                "name": persona.name,
                "patch": {"completed_goal": completed_goal},
                "evidence": message,
            },
        )

        self.assertTrue(first.ok, first.message)
        self.assertTrue(first.state_changed)
        self.assertEqual(persona.active_goal, "")
        self.assertEqual(persona.completed_goals, [completed_goal])
        self.assertFalse(repeated.ok)
        self.assertFalse(repeated.state_changed)
        self.assertEqual(repeated.error_code, "NPC_STATE_NO_CHANGE")

    def test_update_npc_state_relationship_requires_a_real_mapping_change(self) -> None:
        self.assertTrue(self._create_npc().ok)
        persona = self.app.world_state.npc_personas["白花守望会会长"]
        trusted = "会长决定暂时信任伊莉雅。"
        wary = "会长重新对伊莉雅保持戒备。"

        first = self.service.gm_npc_tools.update_npc_state(
            npc_context(trusted),
            {
                "name": persona.name,
                "patch": {
                    "relationship_target": "伊莉雅",
                    "relationship": "暂时信任",
                },
                "evidence": trusted,
            },
        )
        repeated = self.service.gm_npc_tools.update_npc_state(
            npc_context(trusted),
            {
                "name": persona.name,
                "patch": {
                    "relationship_target": "伊莉雅",
                    "relationship": "暂时信任",
                },
                "evidence": trusted,
            },
        )
        changed = self.service.gm_npc_tools.update_npc_state(
            npc_context(wary),
            {
                "name": persona.name,
                "patch": {
                    "relationship_target": "伊莉雅",
                    "relationship": "保持戒备",
                },
                "evidence": wary,
            },
        )

        self.assertTrue(first.ok, first.message)
        self.assertFalse(repeated.ok)
        self.assertEqual(repeated.error_code, "NPC_STATE_NO_CHANGE")
        self.assertTrue(changed.ok, changed.message)
        self.assertEqual(persona.relationships["伊莉雅"], "保持戒备")

    def test_revise_npc_profile_rejects_only_identical_scalars_and_list_items(
        self,
    ) -> None:
        self.assertTrue(self._create_npc().ok)
        persona = self.app.world_state.npc_personas["白花守望会会长"]
        message = "会长仍以保护驿站为核心，也仍要守住受庇护者。"

        with patch.object(self.service, "_autosave_campaign") as autosave:
            receipt = self.service.gm_npc_tools.revise_npc_profile(
                npc_context(message),
                {
                    "name": persona.name,
                    "set": {
                        "core_drive": persona.core_drive,
                        "active_goal": persona.active_goal,
                    },
                    "add": {"goals": [persona.goals[0]]},
                    "evidence": message,
                },
            )

        self.assertFalse(receipt.ok)
        self.assertFalse(receipt.state_changed)
        self.assertFalse(receipt.retryable)
        self.assertEqual(receipt.error_code, "NPC_PROFILE_NO_CHANGE")
        autosave.assert_not_called()

    def test_revise_npc_profile_filters_duplicates_but_commits_real_differences(
        self,
    ) -> None:
        self.assertTrue(self._create_npc().ok)
        persona = self.app.world_state.npc_personas["白花守望会会长"]
        existing_goal = persona.goals[0]
        new_goal = "护送失忆旅人离开驿站"
        message = "会长换了说话方式，并决定护送失忆旅人离开驿站。"

        receipt = self.service.gm_npc_tools.revise_npc_profile(
            npc_context(message),
            {
                "name": persona.name,
                "set": {
                    "core_drive": persona.core_drive,
                    "speech_style": "先给结论，再说明边界",
                },
                "add": {"goals": [existing_goal, new_goal]},
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertTrue(receipt.state_changed)
        self.assertEqual(receipt.result["changed_scalars"], ["speech_style"])
        self.assertEqual(receipt.result["added_lists"], {"goals": [new_goal]})
        self.assertEqual(persona.goals.count(existing_goal), 1)
        self.assertIn(new_goal, persona.goals)

    def test_revise_npc_profile_persists_stable_revelations(self) -> None:
        self.assertTrue(self._create_npc().ok)
        message = "会长承认她真正想保护的是失忆旅人，并让大家叫她铃霜。"

        receipt = self.service.gm_tool_registry.execute(
            "revise_npc_profile",
            {
                "name": "白花守望会会长",
                "set": {"core_drive": "不惜代价保护失忆旅人"},
                "add": {
                    "aliases": ["铃霜"],
                    "goals": ["把失忆旅人安全送出驿站"],
                },
            },
            npc_context(message),
        )

        self.assertTrue(receipt.ok, receipt.message)
        persona = self.app.world_state.npc_personas["白花守望会会长"]
        self.assertEqual(persona.core_drive, "不惜代价保护失忆旅人")
        self.assertIn("铃霜", persona.aliases)
        self.assertIn("把失忆旅人安全送出驿站", persona.goals)

        restarted = FUGMHttpService(
            data_root=self.tmpdir.name,
            use_llm=False,
        )
        restored_app = restarted._runtime("npc-tool-test").app
        restored = restored_app.world_state.npc_personas["白花守望会会长"]
        self.assertEqual(restored.core_drive, "不惜代价保护失忆旅人")
        self.assertIn("铃霜", restored.aliases)
        self.assertEqual(restored_app.world_state.resolve_npc_name("铃霜"), restored.name)

    def test_revise_npc_profile_rolls_back_on_autosave_failure(self) -> None:
        self.assertTrue(self._create_npc().ok)
        original = self.app.world_state.npc_personas[
            "白花守望会会长"
        ].core_drive
        message = "会长承认她只想抢走旅人的风铃。"

        with patch.object(
            self.service,
            "_autosave_campaign",
            side_effect=RuntimeError("disk unavailable"),
        ):
            receipt = self.service.gm_tool_registry.execute(
                "revise_npc_profile",
                {
                    "name": "白花守望会会长",
                    "set": {"core_drive": "抢走旅人的风铃"},
                },
                npc_context(message),
            )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "TOOL_EXECUTION_FAILED")
        self.assertEqual(
            self.app.world_state.npc_personas[
                "白花守望会会长"
            ].core_drive,
            original,
        )


if __name__ == "__main__":
    unittest.main()
