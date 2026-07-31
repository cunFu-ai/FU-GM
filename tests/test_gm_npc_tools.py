from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from fu_gm.components.npc_response_window_manager import NPCResponseWindowManager
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

    def _create_npc(
        self,
        *,
        name: str = "白花守望会会长",
        entity_kind: str = "individual",
        present: bool = True,
    ):
        message = f"{name}从廊柱后走出来。"
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
                "evidence": message,
            },
        )

    @staticmethod
    def _combat_arguments(
        name: str,
        *,
        level: int = 5,
        selected_skills: list[str],
        skill_options: dict[str, object] | None = None,
        attack: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {
            "name": name,
            "level": level,
            "species": "humanoid",
            "rank": "soldier",
            "champion_value": 1,
            "is_villain": False,
            "ultima_points": 0,
            "traits": ["警惕", "克制", "守序", "坚韧"],
            "attribute_spread": "versatile",
            "attribute_order": ["敏捷", "洞察", "力量", "意志"],
            "weaknesses": [],
            "additional_affinities": {},
            "status_immunities": [],
            "skill_options": dict(skill_options or {}),
            "selected_skills": list(selected_skills),
            "attack": attack
            or {
                "name": "守望枪",
                "attributes": ["敏捷", "力量"],
                "damage_type": "物理",
                "damage_bonus": 0,
                "accuracy_modifier": 0,
                "range": "melee",
                "targets_magic_defense": False,
                "multi_attack": 1,
                "status_effect_on_hit": "",
                "notes": [],
            },
            "evidence": f"{name}准备参与冲突。",
        }

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

    def test_core_gm_public_segments_are_committed_without_second_model(self) -> None:
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

    def test_combatant_creation_applies_structured_passive_skills(self) -> None:
        name = "辉钢守卫"
        self.assertTrue(self._create_npc(name=name).ok)
        arguments = self._combat_arguments(
            name,
            selected_skills=["伤害抵抗", "异常状态免疫", "专精"],
            skill_options={
                "伤害抵抗": ["火", "冰"],
                "异常状态免疫": ["眩晕", "中毒"],
                "专精": ["妨碍检定"],
            },
        )

        receipt = self.service.gm_npc_tools.create_npc_combatant(
            npc_context(arguments["evidence"]),
            arguments,
        )

        self.assertTrue(receipt.ok, receipt.message)
        combatant = self.app.character_manager.get(name)
        self.assertEqual(combatant.affinities["fire"], Affinity.RESIST)
        self.assertEqual(combatant.affinities["ice"], Affinity.RESIST)
        self.assertIn(StatusEffect.DAZED, combatant.permanent_status_immunities)
        self.assertIn(StatusEffect.POISONED, combatant.permanent_status_immunities)
        self.assertEqual(combatant.npc_specialty_bonuses["妨碍检定"], 3)

    def test_spellcaster_creation_applies_level_specialty_and_spell_damage(self) -> None:
        name = "火法师"
        self.assertTrue(self._create_npc(name=name).ok)
        arguments = self._combat_arguments(
            name,
            level=20,
            selected_skills=[
                "施法者",
                "专精",
                "强化伤害",
                "强化生命",
                "近战武器精通",
            ],
            skill_options={
                "施法者": ["炎弹"],
                "专精": ["施法检定"],
                "强化伤害": ["炎弹"],
            },
        )

        receipt = self.service.gm_npc_tools.create_npc_combatant(
            npc_context(arguments["evidence"]),
            arguments,
        )

        self.assertTrue(receipt.ok, receipt.message)
        combatant = self.app.character_manager.get(name)
        self.assertEqual(combatant.npc_spell_check_bonus, 5)
        self.assertEqual(combatant.npc_spell_damage_bonus, 5)
        self.assertEqual(combatant.npc_spell_specific_damage_bonuses, {"炎弹": 5})
        self.assertIn("炎弹", combatant.spells)
        self.assertEqual(combatant.max_mp, 70)

    def test_spellcaster_creation_persists_per_spell_check_attributes(self) -> None:
        name = "咒战士"
        self.assertTrue(self._create_npc(name=name).ok)
        arguments = self._combat_arguments(
            name,
            selected_skills=["施法者", "强化生命", "强化先攻"],
            skill_options={"施法者": ["诅咒吐息"]},
        )
        arguments["spell_attributes"] = {
            "诅咒吐息": ["力量", "意志"],
        }

        receipt = self.service.gm_npc_tools.create_npc_combatant(
            npc_context(arguments["evidence"]),
            arguments,
        )

        self.assertTrue(receipt.ok, receipt.message)
        combatant = self.app.character_manager.get(name)
        self.assertEqual(
            combatant.npc_spell_attributes,
            {"诅咒吐息": ["MIG", "WLP"]},
        )

    def test_spellcaster_creation_rejects_illegal_spell_attribute_pair(self) -> None:
        name = "咒战士"
        self.assertTrue(self._create_npc(name=name).ok)
        arguments = self._combat_arguments(
            name,
            selected_skills=["施法者", "强化生命", "强化先攻"],
            skill_options={"施法者": ["诅咒吐息"]},
        )
        arguments["spell_attributes"] = {
            "诅咒吐息": ["敏捷", "意志"],
        }

        receipt = self.service.gm_npc_tools.create_npc_combatant(
            npc_context(arguments["evidence"]),
            arguments,
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "NPC_SPELL_ATTRIBUTES_INVALID",
        )

    def test_combatant_creation_rejects_unconfigured_dynamic_skill(self) -> None:
        name = "自爆机兵"
        self.assertTrue(self._create_npc(name=name).ok)
        arguments = self._combat_arguments(
            name,
            selected_skills=["最后一搏", "强化生命", "强化先攻"],
        )

        receipt = self.service.gm_npc_tools.create_npc_combatant(
            npc_context(arguments["evidence"]),
            arguments,
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "NPC_DYNAMIC_SKILL_REQUIRES_TYPED_PROFILE",
        )

    def test_combatant_creation_rejects_unknown_skill(self) -> None:
        name = "无名斗士"
        self.assertTrue(self._create_npc(name=name).ok)
        arguments = self._combat_arguments(
            name,
            selected_skills=["并不存在的技能", "强化生命", "强化先攻"],
        )

        receipt = self.service.gm_npc_tools.create_npc_combatant(
            npc_context(arguments["evidence"]),
            arguments,
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "NPC_DESIGN_INVALID")

    def test_combatant_creation_can_build_full_turn_ally(self) -> None:
        name = "白花巡守"
        self.assertTrue(self._create_npc(name=name).ok)
        arguments = self._combat_arguments(
            name,
            selected_skills=["伤害抵抗", "异常状态免疫", "专精"],
            skill_options={
                "伤害抵抗": ["火", "冰"],
                "异常状态免疫": ["眩晕", "中毒"],
                "专精": ["妨碍检定"],
            },
        )
        arguments["combat_side"] = "ally"

        receipt = self.service.gm_npc_tools.create_npc_combatant(
            npc_context(arguments["evidence"]),
            arguments,
        )

        self.assertTrue(receipt.ok, receipt.message)
        combatant = self.app.character_manager.get(name)
        self.assertIn("ally", combatant.traits)
        self.assertNotIn("enemy", combatant.traits)
        self.assertFalse(self.app.conflict_manager.is_villain(name))
        self.assertEqual(receipt.result["combat_side"], "ally")

    def test_configure_boss_phases_builds_executable_stages_and_persists(self) -> None:
        name = "潮钟执政官"
        self.assertTrue(self._create_npc(name=name).ok)
        combat = self._combat_arguments(
            name,
            level=20,
            selected_skills=[
                "施法者",
                "专精",
                "强化伤害",
                "强化生命",
                "近战武器精通",
            ],
            skill_options={
                "施法者": ["炎弹"],
                "专精": ["施法检定"],
                "强化伤害": ["炎弹"],
            },
        )
        combat["is_villain"] = True
        combat["ultima_points"] = 5
        combat_receipt = self.service.gm_npc_tools.create_npc_combatant(
            npc_context(combat["evidence"]),
            combat,
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
        self.assertTrue(self._create_npc(name=name).ok)
        combat = self._combat_arguments(
            name,
            selected_skills=["伤害抵抗", "异常状态免疫", "专精"],
            skill_options={
                "伤害抵抗": ["火", "冰"],
                "异常状态免疫": ["眩晕", "中毒"],
                "专精": ["妨碍检定"],
            },
        )
        combat_receipt = self.service.gm_npc_tools.create_npc_combatant(
            npc_context(combat["evidence"]),
            combat,
        )
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
        self.assertTrue(self._create_npc(name=name).ok)
        combat = self._combat_arguments(
            name,
            selected_skills=["伤害抵抗", "异常状态免疫", "专精"],
            skill_options={
                "伤害抵抗": ["火", "冰"],
                "异常状态免疫": ["眩晕", "中毒"],
                "专精": ["妨碍检定"],
            },
        )
        combat["is_villain"] = True
        combat["ultima_points"] = 5
        combat_receipt = self.service.gm_npc_tools.create_npc_combatant(
            npc_context(combat["evidence"]),
            combat,
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
        self.assertTrue(self._create_npc(name=name).ok)
        combat = self._combat_arguments(
            name,
            selected_skills=["伤害抵抗", "异常状态免疫", "专精"],
            skill_options={
                "伤害抵抗": ["火", "冰"],
                "异常状态免疫": ["眩晕", "中毒"],
                "专精": ["妨碍检定"],
            },
        )
        combat["is_villain"] = True
        combat["ultima_points"] = 5
        combat_receipt = self.service.gm_npc_tools.create_npc_combatant(
            npc_context(combat["evidence"]),
            combat,
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
