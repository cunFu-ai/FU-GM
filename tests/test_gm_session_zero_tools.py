from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from unittest.mock import patch

from fu_gm.components.gm_agent_capability_policy import GMToolAgentCapabilityPolicy
from fu_gm.components.gm_agent_prompts import (
    CORE_GM_SYSTEM_PROMPT,
    SESSION_ZERO_SYSTEM_PROMPT,
)
from fu_gm.components.gm_supervisor import GMCapabilityBroker
from fu_gm.gm_tool_agent import GMToolExecutionContext, LLMGMToolAgent
from fu_gm.gm_tool_receipts import GMToolReceiptPolicy
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import HeroDraft


class ScriptedClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = [json.dumps(item, ensure_ascii=False) for item in responses]

    def create_chat_completion(self, **kwargs) -> str:
        if not self.responses:
            raise AssertionError("缺少脚本化模型响应。")
        request = json.loads(kwargs["messages"][1].content)
        available = {
            str(item.get("name") or "")
            for item in list(request.get("available_tools") or [])
            if isinstance(item, dict)
        }
        scripted = json.loads(self.responses[0])
        requested = (
            {str(scripted.get("tool_name") or "")}
            if scripted.get("decision") == "call_tool"
            else set()
        )
        missing = {
            name for name in requested if name and name not in available
        }
        if missing and GMCapabilityBroker.DISCOVERY_TOOL in available:
            domains = GMCapabilityBroker.domains_for_tools(missing)
            if domains:
                discovery = {
                        "decision": "call_tool",
                        "tool_name": "discover_capabilities",
                        "arguments": {
                            "domains": domains[:4],
                            "reason": "测试模型按协议取得所需能力。",
                        },
                    }
                return json.dumps(
                    discovery,
                    ensure_ascii=False,
                )
        return self.responses.pop(0)


class CreativeOnceClient:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = json.dumps(response, ensure_ascii=False)
        self.calls: list[dict[str, object]] = []

    def create_chat_completion(self, **kwargs) -> str:
        self.calls.append(dict(kwargs))
        if len(self.calls) > 1:
            raise AssertionError("单人整包开章不应再次调用创作模型。")
        return self.response


def context(
    message: str,
    *,
    speaker: str = "白河",
    is_private: bool = False,
) -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="第零章工具团",
        session_id="s0",
        channel_id="group-1",
        speaker=speaker,
        gate_status="session_zero",
        is_private=is_private,
        directly_addressed=True,
        metadata={"current_message": message},
    )


_LEGACY_GENERIC_MAP_NAMES = (
    "西部山脉",
    "中央内海",
    "南部海岸",
    "南部驿站",
    "东南群岛",
)


def legacy_generic_map_updates() -> dict[str, object]:
    feature_types = (
        "mountain_range",
        "inland_sea",
        "coast",
        "settlement",
        "archipelago",
    )
    positions = ("west", "center", "south", "south", "southeast")
    return {
        "map_locations": [
            {
                "name": name,
                "description": f"待全桌确认后细化的{name}。",
                "feature_type": feature_type,
                "terrain": "待定",
                "position_hint": position,
            }
            for name, feature_type, position in zip(
                _LEGACY_GENERIC_MAP_NAMES,
                feature_types,
                positions,
            )
        ]
    }


def refined_map_replacement_operations() -> list[dict[str, object]]:
    specs = (
        ("鸦羽山脉", "mountain_range", "高山", "west"),
        ("镜线内海", "inland_sea", "内海", "center"),
        ("雾潮海岸", "coast", "海岸", "south"),
        ("白花碑驿站", "settlement", "驿道", "south"),
        ("潮鸢群岛", "archipelago", "群岛", "southeast"),
    )
    return [
        {
            "operation": "create",
            "category": "map_locations",
            "name": name,
            "value": f"{name}是玩家确认后的细化地图节点。",
            "attributes": {
                "feature_type": feature_type,
                "terrain": terrain,
                "position_hint": position,
            },
            "visibility": "public",
        }
        for name, feature_type, terrain, position in specs
    ]


class GMSessionZeroToolTests(unittest.TestCase):
    def setUp(self) -> None:
        # These scripted fixtures exercise typed Session 0 transactions, not
        # the separate default-on semantic-envelope contract.
        self._legacy_semantics_contract = patch.dict(
            "os.environ",
            {"FU_GM_MESSAGE_SEMANTICS_CONTRACT": "0"},
        )
        self._legacy_semantics_contract.start()
        self.tempdir = tempfile.TemporaryDirectory()
        self.service = FUGMHttpService(data_root=self.tempdir.name, use_llm=False)
        self.runtime = self.service._runtime("第零章工具团")
        self.runtime.app.initialize_session_zero(participants=["白河", "南星"])

    def tearDown(self) -> None:
        self.tempdir.cleanup()
        self._legacy_semantics_contract.stop()

    def test_proposal_scope_subjects_keep_every_authority_surface(self) -> None:
        self.assertEqual(
            self.service.gm_session_zero_tools._proposal_scope_subjects(
                ["kingdoms", "map_locations", "world_shape"]
            ),
            ["world_map", "kingdoms"],
        )

    def test_single_participant_must_contribute_or_skip_each_world_topic(self) -> None:
        with tempfile.TemporaryDirectory() as data_root:
            service = FUGMHttpService(data_root=data_root, use_llm=False)
            runtime = service._runtime("单人贡献检查")
            runtime.app.initialize_session_zero(participants=["白河"])
            manager = runtime.app.session_zero_manager

            self.assertFalse(manager.progress_summary()["kingdom_contributions"])
            manager.state.participants[0].answered_topics.append(
                "kingdom_contributions"
            )
            self.assertTrue(manager.progress_summary()["kingdom_contributions"])

    def test_world_impression_is_independent_from_rendered_map(self) -> None:
        manager = self.runtime.app.session_zero_manager
        manager.apply_world_updates({"world_shape": "普通大陆"})

        progress = manager.progress_summary()

        self.assertTrue(progress["world_shape"])
        self.assertFalse(progress["map_card"])
        self.assertNotIn("世界第一印象或大陆形态", manager.missing_topics())
        self.assertFalse(
            any("地图" in topic for topic in manager.missing_topics()),
            manager.missing_topics(),
        )

    def _make_adventure_ready(self) -> None:
        manager = self.runtime.app.session_zero_manager
        world = manager.state.world
        world.world_shape = "普通大陆"
        world.map_card = "自定义地图"
        world.magic_tech_role = "魔法与科技彼此对立。"
        world.kingdoms = {"索朗帝国": "旧蒸汽帝国。"}
        world.historical_events = ["两百年前的机械战争。"]
        world.mysteries = ["重叠日。"]
        world.world_threats = ["失控的钢铁生命正在扩散。"]
        world.group_concept = "调查重叠日的同行者"
        world.safety_lines = ["不出现性暴力"]
        world.selected_first_act_summary = "从卡里巴村监狱越狱。"
        for participant in manager.state.participants:
            participant.answered_topics.extend(
                [
                    "kingdom_contributions",
                    "historical_event_contributions",
                    "mystery_contributions",
                    "threat_contributions",
                ]
            )
        for key, player, hero in (
            ("白河", "白河", "洛岚"),
            ("南星", "南星", "赛璃"),
        ):
            world.hero_drafts[key] = HeroDraft(
                player_name=player,
                hero_name=hero,
                identity="出逃的魔导工匠",
                theme="希望",
                origin="第七采掘城",
                classes={"造物使": 3, "武器大师": 2},
                attributes={"敏捷": 8, "洞察": 10, "力量": 8, "意志": 6},
                skills={
                    "便携装置": 1,
                    "秘密配方": 1,
                    "先见之明": 1,
                    "碎骨": 1,
                    "破防打击": 1,
                },
                skill_options={"便携装置": ["魔导装置"]},
                equipment=["铁锤", "旅行装束"],
                confirmed=True,
            )
        manager.refresh_stage_from_state()

    def test_session_zero_tools_expose_nested_world_and_hero_shapes(self) -> None:
        schemas = {
            item["name"]: item
            for item in self.service.gm_tool_registry.schemas()
        }
        world_schema = schemas["propose_session_zero_update"]["parameters"][
            "properties"
        ]["updates"]
        operation_schema = schemas["propose_session_zero_update"]["parameters"][
            "properties"
        ]["world_operations"]
        proposal_description = schemas["propose_session_zero_update"]["description"]
        hero_schema = schemas["update_hero_draft"]["parameters"]["properties"][
            "patch"
        ]
        hero_description = schemas["update_hero_draft"]["description"]

        self.assertEqual(
            world_schema["properties"]["kingdoms"]["type"],
            "object",
        )
        self.assertEqual(
            world_schema["properties"]["map_locations"]["type"],
            "array",
        )
        self.assertEqual(operation_schema["type"], "array")
        self.assertEqual(
            operation_schema["items"]["properties"]["operation"]["enum"],
            ["create", "update", "delete", "rename"],
        )
        self.assertIn("逐项调用世界设定CRUD", proposal_description)
        self.assertIn("分句完整性核对", CORE_GM_SYSTEM_PROMPT)
        self.assertIn("只把本句新增或纠正的字段放入patch", hero_description)
        self.assertIn("increment_skills必须放在patch内部", hero_description)
        map_item_schema = world_schema["properties"]["map_locations"]["items"]
        self.assertIn("feature_type", map_item_schema["required"])
        self.assertEqual(
            set(map_item_schema["properties"]["position_hint"]["enum"]),
            {
                "north",
                "northeast",
                "east",
                "southeast",
                "south",
                "southwest",
                "west",
                "northwest",
                "center",
            },
        )

        self.assertIn(
            "inland_sea",
            map_item_schema["properties"]["feature_type"]["enum"],
        )
        self.assertIn(
            "不能因为相同事实已经写进",
            world_schema["properties"]["historical_events"]["description"],
        )
        self.assertIn(
            "即使同一人物也出现在",
            world_schema["properties"]["villain_seeds"]["description"],
        )
        self.assertIn(
            "不得只放进共识",
            world_schema["properties"]["consensus_notes"]["description"],
        )
        self.assertIn(
            "条件危机仍属于世界威胁",
            world_schema["properties"]["world_threats"]["description"],
        )
        self.assertIn(
            "并列清单不构成相对关系",
            map_item_schema["properties"]["relative_to"]["description"],
        )
        self.assertIn(
            "不等于center",
            map_item_schema["properties"]["relative_position"]["description"],
        )
        self.assertIn(
            "record_safety_boundary",
            world_schema["properties"]["violence_guideline"]["description"],
        )
        self.assertIn(
            "不能改写到party_dynamic",
            world_schema["properties"]["group_concept"]["description"],
        )
        self.assertIn(
            "这不是小队原型或共同任务",
            world_schema["properties"]["party_dynamic"]["description"],
        )
        self.assertIn("set_session_zero_nudge_preference", schemas)
        self.assertIn("pause_session_zero_nudges", schemas)
        self.assertIn("set_chapter_one_transition", schemas)
        self.assertIn("record_prologue_setup_answer", schemas)
        self.assertIn("get_session_zero_readiness", schemas)
        self.assertIn(
            "不要用get_hero_drafts",
            schemas["get_session_zero_readiness"]["description"],
        )
        self.assertNotIn("commit_session_zero_update", schemas)
        self.assertNotIn("semantic_profile", schemas["record_safety_boundary"])
        self.assertFalse(world_schema["additionalProperties"])
        self.assertIn("正在征求同伴意见", proposal_description)
        self.assertIn("不必说出‘暂存’", proposal_description)
        self.assertIn("待定提案不是已确认世界事实", proposal_description)
        self.assertNotIn("不要调用", proposal_description)
        self.assertIn(
            "具有明确危险主体、触发条件和地区性危害结果的条件危机",
            SESSION_ZERO_SYSTEM_PROMPT,
        )
        self.assertEqual(
            set(hero_schema["properties"]["attributes"]["properties"]),
            {"敏捷", "洞察", "力量", "意志"},
        )
        self.assertFalse(hero_schema["additionalProperties"])

    def test_ready_state_is_visible_to_gm_before_chapter_one_invitation(self) -> None:
        self._make_adventure_ready()

        summary = self.service.gm_session_zero_tools.state_summary(
            context("从越狱开始吧")
        )

        self.assertTrue(summary["adventure_readiness"]["ready"])
        self.assertEqual(
            summary["chapter_one_transition"]["status"],
            "pending",
        )

    def test_empty_campaign_readiness_does_not_claim_session_zero_is_complete(self) -> None:
        empty_context = GMToolExecutionContext(
            campaign_id="空白单人团",
            session_id="solo",
            channel_id="private-1",
            speaker="白河",
            gate_status="inactive",
            is_private=True,
            directly_addressed=True,
            metadata={"current_message": "第零章准备好了吗？"},
        )

        receipt = self.service.gm_session_zero_tools.get_session_zero_readiness(
            empty_context,
            {},
        )

        self.assertTrue(receipt.ok)
        self.assertFalse(receipt.result["has_session_zero_context"])
        self.assertIs(receipt.result["terminal_public_result"], True)
        self.assertTrue(receipt.lock_public_reply)
        self.assertIn("还没有开启第零章", receipt.public_fallback_reply)
        self.assertNotIn("内容已经齐了", receipt.public_fallback_reply)

    def test_readiness_planning_mode_does_not_lock_a_missing_items_reply(self) -> None:
        receipt = self.service.gm_session_zero_tools.get_session_zero_readiness(
            context("剩下的世界设定由你补。"),
            {"purpose": "gm_planning"},
        )

        self.assertTrue(receipt.ok)
        self.assertFalse(receipt.lock_public_reply)
        self.assertEqual(receipt.public_fallback_reply, "")
        self.assertNotIn("terminal_public_result", receipt.result)
        self.assertIn("session_zero", receipt.result)

    def test_select_first_act_commits_only_the_authorized_custom_opening(self) -> None:
        message = "第一幕也由你决定，就从一场越狱开始。"
        receipt = self.service.gm_tool_registry.execute(
            "select_first_act",
            {
                "custom_summary": (
                    "锅底监牢：灵魂蒸汽管线即将重启，英雄必须在狱卒封锁前逃出牢区。"
                )
            },
            context(message),
        )

        self.assertTrue(receipt.ok, receipt.to_dict())
        self.assertEqual(receipt.tool_name, "select_first_act")
        self.assertEqual(
            self.runtime.app.session_zero_manager.state.world.selected_first_act_summary,
            "锅底监牢：灵魂蒸汽管线即将重启，英雄必须在狱卒封锁前逃出牢区。",
        )
        self.assertEqual(
            receipt.result["applied_fields"],
            ["selected_first_act_summary"],
        )

    def test_gm_can_announce_readiness_while_players_keep_supplementing(self) -> None:
        self._make_adventure_ready()
        message = "开场先定越狱，不过我还想补一下监狱长的背景。"

        receipt = self.service.gm_tool_registry.execute(
            "set_chapter_one_transition",
            {"posture": "supplementing"},
            context(message),
        )

        self.assertTrue(receipt.ok)
        self.assertTrue(receipt.state_changed)
        self.assertTrue(receipt.result["first_announcement"])
        self.assertIn("已经具备进入第一章", receipt.public_fallback_reply)
        status = (
            self.runtime.app.session_zero_manager
            .chapter_one_transition_status(ready=True)
        )
        self.assertEqual(status["status"], "supplementing")
        self.assertEqual(status["evidence"], message)

        repeated = self.service.gm_tool_registry.execute(
            "set_chapter_one_transition",
            {"posture": "supplementing"},
            context(message),
        )
        self.assertTrue(repeated.ok)
        self.assertFalse(repeated.state_changed)
        self.assertEqual(repeated.public_fallback_reply, "")

    def test_gm_invites_once_after_supplementing_naturally_finishes(self) -> None:
        self._make_adventure_ready()
        manager = self.runtime.app.session_zero_manager
        manager.set_chapter_one_transition(
            "supplementing",
            speaker="白河",
            evidence="我还想补一下监狱长。",
        )
        message = "监狱长就叫赫恩，其他没有要补的了。"

        receipt = self.service.gm_tool_registry.execute(
            "set_chapter_one_transition",
            {"posture": "invited"},
            context(message),
        )

        self.assertTrue(receipt.ok)
        self.assertEqual(receipt.result["previous_posture"], "supplementing")
        self.assertTrue(receipt.result["should_ask_to_start"])
        self.assertIn("现在进入第一章吗", receipt.public_fallback_reply)
        self.assertEqual(
            manager.chapter_one_transition_status(ready=True)["status"],
            "invited",
        )
        transition = manager.chapter_one_transition_status(ready=True)
        anchor = transition["conversation_anchor"]
        self.assertEqual(anchor["kind"], "chapter_one_invitation")
        self.assertEqual(anchor["status"], "awaiting_semantic_reply")
        self.assertFalse(anchor["blocking"])
        self.assertFalse(anchor["player_visible"])

        manager.set_chapter_one_transition(
            "supplementing",
            speaker="白河",
            evidence="先补一件事。",
        )
        self.assertNotIn(
            "conversation_anchor",
            manager.chapter_one_transition_status(ready=True),
        )

    def test_chapter_one_invitation_waits_when_player_requested_time(self) -> None:
        self._make_adventure_ready()
        manager = self.runtime.app.session_zero_manager
        manager.pause_proactive_nudges(
            "白河",
            topic="第一幕开场",
            evidence="让我想想。",
        )

        receipt = self.service.gm_tool_registry.execute(
            "set_chapter_one_transition",
            {"posture": "invited"},
            context("让我想想。"),
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "PLAYER_REQUESTED_TIME")
        self.assertEqual(manager.state.chapter_one_transition, {})

    def test_chapter_one_transition_resets_if_setup_becomes_incomplete(self) -> None:
        self._make_adventure_ready()
        manager = self.runtime.app.session_zero_manager
        manager.set_chapter_one_transition(
            "invited",
            speaker="白河",
            evidence="现在开第一章吗？",
        )
        manager.state.world.selected_first_act_summary = ""

        manager.refresh_stage_from_state()

        self.assertEqual(manager.state.chapter_one_transition, {})
        self.assertEqual(
            manager.chapter_one_transition_status(ready=False)["status"],
            "not_ready",
        )

    def test_pending_proposal_uses_same_rollback_boundary_as_other_writes(self) -> None:
        message = "时悠，先把浮空王城作为待定提案记着。"
        with patch.object(
            self.service,
            "_autosave_campaign",
            side_effect=RuntimeError("disk unavailable"),
        ):
            receipt = self.service.gm_tool_registry.execute(
                "propose_session_zero_update",
                {
                    "summary": "世界中央有一座浮空王城",
                    "updates": {"world_shape": "环绕浮空王城展开的大陆"},
                },
                context(message),
            )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "TOOL_EXECUTION_FAILED")
        self.assertEqual(
            self.runtime.app.session_zero_manager.state.world.pending_proposals,
            [],
        )
        self.assertNotEqual(
            self.runtime.app.world_state.world_profile.world_shape,
            "环绕浮空王城展开的大陆",
        )

    def test_named_world_entity_proposal_is_rejected_before_confirmation(self) -> None:
        message = "我提议驿站旁有一种会随风发出铃响的芦苇。"

        receipt = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "驿站旁有一种会随风发出铃响的芦苇。",
                "world_operations": [
                    {
                        "operation": "create",
                        "category": "map_locations",
                        "value": "钟鸣公国驿站附近有会随风发出铃响的芦苇。",
                        "attributes": {
                            "feature_type": "landmark",
                            "relative_to": "钟鸣公国",
                        },
                        "visibility": "public",
                    }
                ],
            },
            context(message),
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "INVALID_WORLD_PROPOSAL_OPERATION")
        self.assertIn("name", receipt.message)
        self.assertEqual(
            self.runtime.app.session_zero_manager.state.world.pending_proposals,
            [],
        )

    def test_scalar_proposal_normalizes_create_to_update_when_slot_exists(self) -> None:
        self.runtime.app.world_state.world_profile.world_shape = "普通大陆"
        message = "我另提一个待讨论的轮廓：大陆中央裂成环形。"

        proposed = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "把普通大陆改成中央断裂的环形大陆。",
                "world_operations": [
                    {
                        "operation": "create",
                        "category": "world_shape",
                        "value": "中央断裂的环形大陆",
                        "visibility": "public",
                    }
                ],
            },
            context(message),
        )

        self.assertTrue(proposed.ok, proposed.to_dict())
        proposal = proposed.result["proposal"]
        self.assertEqual(
            proposal["world_operations"],
            [
                {
                    "operation": "update",
                    "category": "world_shape",
                    "value": "中央断裂的环形大陆",
                    "visibility": "public",
                }
            ],
        )
        self.assertEqual(
            proposal["operation_normalizations"][0]["original_operation"],
            "create",
        )
        self.assertEqual(
            proposal["operation_normalizations"][0]["normalized_operation"],
            "update",
        )
        self.assertNotIn("name", proposal["world_operations"][0])

    def test_scalar_proposal_normalizes_empty_update_and_placeholder_to_create(self) -> None:
        message = "我提议世界是中心大陆外环群岛。"

        proposed = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "世界由中心大陆和外环群岛构成。",
                "world_operations": [
                    {
                        "operation": "update",
                        "category": "world_shape",
                        "name": "__singleton__",
                        "value": "中心大陆外环群岛",
                        "visibility": "public",
                    }
                ],
            },
            context(message),
        )

        self.assertTrue(proposed.ok, proposed.to_dict())
        proposal = proposed.result["proposal"]
        operation = proposal["world_operations"][0]
        self.assertEqual(operation["operation"], "create")
        self.assertNotIn("name", operation)
        normalization = proposal["operation_normalizations"][0]
        self.assertEqual(normalization["supplied_name"], "__singleton__")
        self.assertEqual(normalization["original_operation"], "update")
        self.assertEqual(normalization["normalized_operation"], "create")
        state_summary = self.service.gm_session_zero_tools.state_summary(
            context("查看待定提案。")
        )
        summary_proposal = next(
            item
            for item in state_summary["pending_proposals"]
            if item["id"] == proposal["id"]
        )
        self.assertEqual(
            summary_proposal["subject_keys"],
            [
                {
                    "category": "world_shape",
                    "visibility": "public",
                    "name": "",
                    "singleton": True,
                }
            ],
        )
        self.assertNotIn("__singleton__", str(summary_proposal))

    def test_named_world_entity_proposal_keeps_name_for_signed_followup(self) -> None:
        message = "我提议驿站旁有一种叫风铃芦苇的植物。"

        proposed = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "驿站旁有一种叫风铃芦苇的植物。",
                "world_operations": [
                    {
                        "operation": "create",
                        "category": "map_locations",
                        "name": "风铃芦苇",
                        "value": "钟鸣公国驿站附近有会随风发出铃响的芦苇。",
                        "attributes": {
                            "feature_type": "landmark",
                            "relative_to": "钟鸣公国",
                        },
                        "visibility": "public",
                    }
                ],
            },
            context(message),
        )

        self.assertTrue(proposed.ok, proposed.message)
        proposal_id = proposed.result["proposal"]["id"]
        confirmed = self.service.gm_tool_registry.execute(
            "confirm_session_zero_proposal",
            {"proposal_id": proposal_id},
            context("我同意这个芦苇设定。", speaker="南星"),
        )

        self.assertTrue(confirmed.ok, confirmed.message)
        followup = confirmed.result["required_followup_calls"][0]
        self.assertEqual(followup["arguments"]["name"], "风铃芦苇")

    def test_confirmation_requires_replacement_when_named_target_evolved(self) -> None:
        proposed = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "季风岛居民会在归潮祭让风带走旧伤。",
                "world_operations": [
                    {
                        "operation": "create",
                        "category": "map_locations",
                        "name": "季风岛",
                        "value": "内海东岸的小岛，归潮祭会带走旧伤。",
                        "attributes": {
                            "feature_type": "settlement",
                            "position_hint": "east",
                        },
                        "visibility": "public",
                    }
                ],
            },
            context("我先提议内海东岸有一座季风岛。", speaker="南星"),
        )
        self.assertTrue(proposed.ok, proposed.to_dict())
        proposal_id = proposed.result["proposal"]["id"]

        newer_value = "季风岛是归潮盟所在的内海岛屿，旧伤指心理创伤。"
        newer_attributes = {
            "feature_type": "region",
            "position_hint": "east",
            "terrain": "岛屿",
        }
        contributed = self.service.gm_tool_registry.execute(
            "create_world_setting",
            {
                "category": "map_locations",
                "name": "季风岛",
                "value": newer_value,
                "attributes": newer_attributes,
                "visibility": "public",
                "authority": "player_confirmed",
                "reason": "提案人后来明确补充了更完整的个人设定。",
            },
            context("我把季风岛补完整。", speaker="南星"),
        )
        self.assertTrue(contributed.ok, contributed.to_dict())

        stale_confirmation = self.service.gm_tool_registry.execute(
            "confirm_session_zero_proposal",
            {"proposal_id": proposal_id},
            context("我赞成南星后来补完整的版本。", speaker="白河"),
        )
        self.assertFalse(stale_confirmation.ok)
        self.assertEqual(
            stale_confirmation.error_code,
            "SESSION_ZERO_PROPOSAL_TARGET_EVOLVED",
        )
        self.assertEqual(
            stale_confirmation.result["conflicts"][0]["current_records"][0][
                "value"
            ],
            newer_value,
        )
        self.assertEqual(
            len(self.runtime.app.world_state.world_profile.pending_proposals),
            1,
        )

        confirmed = self.service.gm_tool_registry.execute(
            "confirm_session_zero_proposal",
            {
                "proposal_id": proposal_id,
                "replacement_world_operations": [
                    {
                        "operation": "create",
                        "category": "map_locations",
                        "name": "季风岛",
                        "value": newer_value,
                        "attributes": newer_attributes,
                        "visibility": "public",
                    }
                ],
            },
            context("我赞成南星后来补完整的版本。", speaker="白河"),
        )
        self.assertTrue(confirmed.ok, confirmed.to_dict())
        self.assertEqual(
            confirmed.result["required_followup_calls"][0]["tool_name"],
            "update_world_setting",
        )
        self.assertEqual(
            self.runtime.app.world_state.world_profile.pending_proposals,
            [],
        )

    def test_list_proposal_signs_a_lossless_executable_followup(self) -> None:
        proposal_message = "我提议碎梦海位于遗忘礁附近，平静潮水里藏着别人的梦。"
        proposed = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "碎梦海位于遗忘礁附近，平静潮水里藏着别人的梦。",
                "world_operations": [
                    {
                        "operation": "create",
                        "category": "world_threats",
                        "name": "碎梦海",
                        "value": "碎梦海位于遗忘礁附近，平静潮水里藏着别人的梦。",
                        "visibility": "public",
                    }
                ],
            },
            context(proposal_message),
        )
        self.assertTrue(proposed.ok, proposed.to_dict())

        confirm_context = context("我同意碎梦海这个方向。", speaker="南星")
        confirm_arguments = {"proposal_id": proposed.result["proposal"]["id"]}
        confirmed = self.service.gm_tool_registry.execute(
            "confirm_session_zero_proposal",
            confirm_arguments,
            confirm_context,
        )
        self.assertTrue(confirmed.ok, confirmed.to_dict())
        followup = confirmed.result["required_followup_calls"][0]
        self.assertNotIn("name", followup["arguments"])
        self.assertEqual(
            followup["arguments"]["value"],
            "碎梦海位于遗忘礁附近，平静潮水里藏着别人的梦。",
        )

        GMToolReceiptPolicy.apply_context(
            confirm_context,
            {},
            confirmed,
            tool_arguments=confirm_arguments,
        )
        committed = self.service.gm_tool_registry.execute(
            followup["tool_name"],
            followup["arguments"],
            confirm_context,
        )
        self.assertTrue(committed.ok, committed.to_dict())
        self.assertEqual(
            self.runtime.app.world_state.world_profile.world_threats,
            ["碎梦海位于遗忘礁附近，平静潮水里藏着别人的梦。"],
        )

    def test_list_update_proposal_normalizes_unique_old_value_prefix(self) -> None:
        old_value = (
            "被共鸣波及的旧地会重复过去某一天，踏入者会逐渐同化成回声。"
        )
        created = self.service.gm_tool_registry.execute(
            "create_world_setting",
            {
                "category": "world_threats",
                "value": old_value,
                "visibility": "public",
                "authority": "player_confirmed",
                "reason": "玩家明确贡献世界威胁。",
            },
            context(old_value, speaker="白河"),
        )
        self.assertTrue(created.ok, created.to_dict())
        new_value = old_value + "同化完成后将无法再被唤醒。"

        proposed = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "回声侵蚀完成后不可逆。",
                "world_operations": [
                    {
                        "operation": "update",
                        "category": "world_threats",
                        "name": "回声侵蚀",
                        "value": new_value,
                        "visibility": "public",
                    }
                ],
            },
            context("我倾向于让回声侵蚀不可逆。", speaker="阿凛"),
        )

        self.assertTrue(proposed.ok, proposed.to_dict())
        proposal = proposed.result["proposal"]
        self.assertEqual(
            proposal["world_operations"][0]["name"],
            old_value,
        )
        self.assertEqual(
            proposal["operation_normalizations"][0]["reason"],
            "list_update_unique_value_prefix",
        )

    def test_confirmed_world_proposal_credits_author_not_confirmer(self) -> None:
        proposal_text = "我提议回声潮随机卷走现实中的记忆。"
        proposed = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "回声潮随机卷走现实中的记忆。",
                "world_operations": [
                    {
                        "operation": "create",
                        "category": "world_threats",
                        "value": "回声潮会随机卷走现实中的记忆。",
                        "visibility": "public",
                    }
                ],
            },
            context(proposal_text, speaker="白河"),
        )
        self.assertTrue(proposed.ok, proposed.to_dict())

        confirm_context = context("我同意回声潮这个威胁。", speaker="南星")
        confirm_arguments = {"proposal_id": proposed.result["proposal"]["id"]}
        confirmed = self.service.gm_tool_registry.execute(
            "confirm_session_zero_proposal",
            confirm_arguments,
            confirm_context,
        )
        self.assertTrue(confirmed.ok, confirmed.to_dict())
        self.assertEqual(confirmed.result["contribution_speaker"], "白河")

        GMToolReceiptPolicy.apply_context(
            confirm_context,
            {},
            confirmed,
            tool_arguments=confirm_arguments,
        )
        followup = confirmed.result["required_followup_calls"][0]
        committed = self.service.gm_tool_registry.execute(
            followup["tool_name"],
            followup["arguments"],
            confirm_context,
        )
        self.assertTrue(committed.ok, committed.to_dict())

        contributors = (
            self.runtime.app.session_zero_manager.state.world.threat_contributors
        )
        self.assertEqual(
            contributors,
            {"白河": ["回声潮会随机卷走现实中的记忆。"]},
        )
        self.assertNotIn("南星", contributors)
        author = self.runtime.app.session_zero_manager.find_participant("白河")
        confirmer = self.runtime.app.session_zero_manager.find_participant("南星")
        self.assertIn("threat_contributions", author.answered_topics)
        self.assertNotIn("threat_contributions", confirmer.answered_topics)

    def test_unaddressed_public_contribution_can_commit_without_acknowledgement(self) -> None:
        message = "我希望这团保留明亮冒险感，不要全程压抑。"
        tool_context = context(message)
        tool_context.directly_addressed = False
        tool_context.metadata["force_gm_reply"] = True

        receipt = self.service.gm_session_zero_tools.commit_update(
            tool_context,
            {
                "updates": {"tone_preferences": ["明亮冒险感"]},
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertTrue(receipt.result["silent_commit_allowed"])
        self.assertTrue(receipt.result["source_message_already_public"])
        self.assertEqual(receipt.public_fallback_reply, "好，记下了。")

    def test_actual_gm_address_keeps_session_zero_acknowledgement(self) -> None:
        message = "@时悠，记一下：我希望这团保留明亮冒险感。"
        tool_context = context(message)
        tool_context.metadata["is_at_bot"] = True

        receipt = self.service.gm_session_zero_tools.commit_update(
            tool_context,
            {
                "updates": {"tone_preferences": ["明亮冒险感"]},
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertFalse(receipt.result["silent_commit_allowed"])
        self.assertFalse(receipt.result["source_message_already_public"])

    def test_confirmed_proposal_is_atomic_and_survives_restart(self) -> None:
        proposed = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "小队是护送失忆旅人的临时同盟",
                "updates": {"group_concept": "护送失忆旅人的临时同盟"},
            },
            context("时悠，先把小队是护送失忆旅人的临时同盟作为待定提案。"),
        )
        self.assertTrue(proposed.ok, proposed.message)
        proposal_id = proposed.result["proposal"]["id"]

        confirmed = self.service.gm_tool_registry.execute(
            "confirm_session_zero_proposal",
            {"proposal_id": proposal_id},
            context("我同意这个小队提案。", speaker="南星"),
        )

        self.assertTrue(confirmed.ok, confirmed.message)
        world = self.runtime.app.session_zero_manager.state.world
        self.assertEqual(world.group_concept, "护送失忆旅人的临时同盟")
        self.assertEqual(world.pending_proposals, [])

        restarted = FUGMHttpService(
            data_root=self.tempdir.name,
            use_llm=False,
        )
        restored = restarted._runtime("第零章工具团").app.session_zero_manager.state.world
        self.assertEqual(restored.group_concept, "护送失忆旅人的临时同盟")
        self.assertEqual(restored.pending_proposals, [])

    def test_world_change_proposal_authorizes_exact_crud_followups(self) -> None:
        create_context = context("我贡献一个澜钟公国。")
        created = self.service.gm_tool_registry.execute(
            "create_world_setting",
            {
                "category": "kingdoms",
                "name": "澜钟公国",
                "value": "以潮钟塔校准航路的公国。",
                "visibility": "public",
                "authority": "player_confirmed",
                "reason": "玩家明确贡献国家。",
            },
            create_context,
        )
        self.assertTrue(created.ok, created.message)

        proposal_message = "我提议把澜钟公国改名为澜钟联邦，阿凛同意后执行。"
        proposed = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "把澜钟公国改名为澜钟联邦",
                "world_operations": [
                    {
                        "operation": "rename",
                        "category": "kingdoms",
                        "name": "澜钟公国",
                        "new_name": "澜钟联邦",
                        "visibility": "public",
                    }
                ],
            },
            context(proposal_message, speaker="南星"),
        )
        self.assertTrue(proposed.ok, proposed.message)
        world = self.runtime.app.session_zero_manager.state.world
        self.assertIn("澜钟公国", world.kingdoms)
        self.assertNotIn("澜钟联邦", world.kingdoms)

        proposal_id = proposed.result["proposal"]["id"]
        confirm_context = context(
            "我同意南星刚才的改名提案。",
            speaker="阿凛",
        )
        confirm_arguments = {"proposal_id": proposal_id}
        confirmed = self.service.gm_tool_registry.execute(
            "confirm_session_zero_proposal",
            confirm_arguments,
            confirm_context,
        )
        self.assertTrue(confirmed.ok, confirmed.message)
        self.assertIn("澜钟公国", world.kingdoms)
        self.assertNotIn("澜钟联邦", world.kingdoms)
        self.assertEqual(world.pending_proposals, [])
        self.assertEqual(
            confirmed.result["required_followup_tools"],
            ["rename_world_setting"],
        )
        followup = confirmed.result["required_followup_calls"][0]
        self.assertEqual(followup["tool_name"], "rename_world_setting")
        self.assertEqual(followup["arguments"]["old_name"], "澜钟公国")
        self.assertEqual(followup["arguments"]["new_name"], "澜钟联邦")
        self.assertEqual(followup["arguments"]["authority"], "table_consensus")

        GMToolReceiptPolicy.apply_context(
            confirm_context,
            {},
            confirmed,
            tool_arguments=confirm_arguments,
        )
        renamed = self.service.gm_tool_registry.execute(
            followup["tool_name"],
            followup["arguments"],
            confirm_context,
        )
        self.assertTrue(renamed.ok, renamed.message)
        self.assertNotIn("澜钟公国", world.kingdoms)
        self.assertIn("澜钟联邦", world.kingdoms)

    def test_confirming_exact_existing_personal_fact_promotes_its_authority(self) -> None:
        value = "碎月碎片会缓慢侵蚀附近居民的重要记忆与情感。"
        personal_context = context(
            "我贡献一个世界威胁：碎月碎片会缓慢侵蚀附近居民的重要记忆与情感。",
            speaker="南星",
        )
        created = self.service.gm_tool_registry.execute(
            "create_world_setting",
            {
                "category": "world_threats",
                "value": value,
                "visibility": "public",
                "authority": "player_confirmed",
                "reason": "南星的个人世界威胁贡献。",
            },
            personal_context,
        )
        self.assertTrue(created.ok, created.to_dict())

        proposed = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "把碎片侵蚀定为全桌核心威胁",
                "world_operations": [
                    {
                        "operation": "create",
                        "category": "world_threats",
                        "value": value,
                        "visibility": "public",
                    }
                ],
            },
            context("我们把碎片侵蚀定为核心威胁怎么样？", speaker="南星"),
        )
        self.assertTrue(proposed.ok, proposed.to_dict())

        confirm_context = context("我同意把碎片侵蚀定为核心威胁。", speaker="阿凛")
        confirm_arguments = {"proposal_id": proposed.result["proposal"]["id"]}
        confirmed = self.service.gm_tool_registry.execute(
            "confirm_session_zero_proposal",
            confirm_arguments,
            confirm_context,
        )

        self.assertTrue(confirmed.ok, confirmed.to_dict())
        self.assertEqual(
            confirmed.result["authority_promotions"],
            [
                {
                    "category": "world_threats",
                    "name": value,
                    "visibility": "public",
                    "from_authority": "player_confirmed",
                    "to_authority": "table_consensus",
                }
            ],
        )
        followup = confirmed.result["required_followup_calls"][0]
        self.assertEqual(followup["tool_name"], "update_world_setting")
        self.assertEqual(followup["arguments"]["name"], value)
        self.assertEqual(followup["arguments"]["value"], value)

        GMToolReceiptPolicy.apply_context(
            confirm_context,
            {},
            confirmed,
            tool_arguments=confirm_arguments,
        )
        promoted = self.service.gm_tool_registry.execute(
            followup["tool_name"],
            followup["arguments"],
            confirm_context,
        )
        self.assertTrue(promoted.ok, promoted.to_dict())
        metadata = self.runtime.app.world_state.world_profile.world_setting_metadata
        matching = [
            item
            for item in metadata.values()
            if item.get("category") == "world_threats"
            and item.get("name") == value
        ]
        self.assertEqual(matching[0]["authority"], "table_consensus")

    def test_confirmed_kingdom_and_map_projection_execute_as_one_ordered_packet(
        self,
    ) -> None:
        proposed = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "钟鸣公国坐落在镜线内海北岸",
                "world_operations": [
                    {
                        "operation": "create",
                        "category": "map_locations",
                        "name": "镜线内海",
                        "value": "贯通大陆腹地与外海的狭长内海。",
                        "attributes": {
                            "feature_type": "inland_sea",
                            "position_hint": "center",
                        },
                        "visibility": "public",
                    },
                    {
                        "operation": "create",
                        "category": "map_locations",
                        "name": "钟鸣公国",
                        "value": "坐落在镜线内海北岸的钟楼公国。",
                        "attributes": {
                            "feature_type": "country",
                            "relative_to": "镜线内海",
                            "relative_position": "north",
                        },
                        "visibility": "public",
                    },
                    {
                        "operation": "create",
                        "category": "kingdoms",
                        "name": "钟鸣公国",
                        "value": "以潮钟校准航路的公国。",
                        "visibility": "public",
                    },
                ],
            },
            context("我提议钟鸣公国位于镜线内海北岸，大家觉得呢？"),
        )
        self.assertTrue(proposed.ok, proposed.to_dict())

        confirm_context = context("我赞成这个版本。", speaker="南星")
        arguments = {"proposal_id": proposed.result["proposal"]["id"]}
        confirmed = self.service.gm_tool_registry.execute(
            "confirm_session_zero_proposal",
            arguments,
            confirm_context,
        )

        self.assertTrue(confirmed.ok, confirmed.to_dict())
        calls = confirmed.result["required_followup_calls"]
        self.assertEqual(
            [item["tool_name"] for item in calls],
            [
                "create_world_setting",
                "create_world_setting",
                "update_world_setting",
            ],
        )
        self.assertEqual(calls[1]["arguments"]["category"], "kingdoms")
        self.assertEqual(calls[2]["arguments"]["category"], "map_locations")

        GMToolReceiptPolicy.apply_context(
            confirm_context,
            {},
            confirmed,
            tool_arguments=arguments,
        )
        for call in calls:
            receipt = self.service.gm_tool_registry.execute(
                call["tool_name"],
                call["arguments"],
                confirm_context,
            )
            self.assertTrue(receipt.ok, receipt.to_dict())
            GMToolReceiptPolicy.apply_context(
                confirm_context,
                {},
                receipt,
                tool_arguments=call["arguments"],
            )

        location = self.runtime.app.world_state.map_locations["钟鸣公国"]
        self.assertEqual(location.relative_to, "镜线内海")
        self.assertEqual(location.relative_position, "north")
        self.assertEqual(location.feature_type, "country")

    def test_confirming_revision_can_clear_semantically_superseded_old_draft(
        self,
    ) -> None:
        original = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "北岸国家暂称潮声城邦",
                "world_operations": [
                    {
                        "operation": "create",
                        "category": "kingdoms",
                        "name": "潮声城邦",
                        "value": "位于内海北岸的贸易城邦。",
                        "visibility": "public",
                    }
                ],
            },
            context("北岸国家先叫潮声城邦怎么样？"),
        )
        revised = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "修订潮声城邦的治理方式",
                "superseded_proposal_ids": [
                    original.result["proposal"]["id"]
                ],
                "world_operations": [
                    {
                        "operation": "create",
                        "category": "kingdoms",
                        "name": "潮声城邦",
                        "value": "改由潮钟议会治理的内海城邦。",
                        "visibility": "public",
                    }
                ],
            },
            context(
                "我把上一版潮声城邦改成由潮钟议会治理，大家觉得呢？",
                speaker="南星",
            ),
        )
        self.assertTrue(original.ok, original.to_dict())
        self.assertTrue(revised.ok, revised.to_dict())
        state_summary = self.service.gm_session_zero_tools.state_summary(
            context("查看待定提案。")
        )
        revised_summary = next(
            item
            for item in state_summary["pending_proposals"]
            if item["id"] == revised.result["proposal"]["id"]
        )
        self.assertEqual(
            revised_summary["superseded_proposal_ids"],
            [original.result["proposal"]["id"]],
        )
        self.assertEqual(
            revised_summary["subject_keys"],
            [
                {
                    "category": "kingdoms",
                    "visibility": "public",
                    "name": "潮声城邦",
                }
            ],
        )

        confirmed = self.service.gm_tool_registry.execute(
            "confirm_session_zero_proposal",
            {"proposal_id": revised.result["proposal"]["id"]},
            context("我赞成修订后的版本。", speaker="阿凛"),
        )

        self.assertTrue(confirmed.ok, confirmed.to_dict())
        self.assertEqual(
            confirmed.result["superseded_proposal_ids"],
            [original.result["proposal"]["id"]],
        )
        self.assertEqual(
            self.runtime.app.world_state.world_profile.pending_proposals,
            [],
        )

    def test_revision_cannot_supersede_unrelated_same_category_draft(
        self,
    ) -> None:
        original = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "南方赤砂王国",
                "world_operations": [
                    {
                        "operation": "create",
                        "category": "kingdoms",
                        "name": "赤砂王国",
                        "value": "位于南方沙海的王国。",
                        "visibility": "public",
                    }
                ],
            },
            context("南方可以有赤砂王国，大家觉得呢？"),
        )
        self.assertTrue(original.ok, original.to_dict())

        rejected = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "西海蓝帆共和国",
                "superseded_proposal_ids": [
                    original.result["proposal"]["id"]
                ],
                "world_operations": [
                    {
                        "operation": "create",
                        "category": "kingdoms",
                        "name": "蓝帆共和国",
                        "value": "位于西海群岛的共和国。",
                        "visibility": "public",
                    }
                ],
            },
            context("西海还可以有蓝帆共和国，大家觉得呢？", speaker="南星"),
        )

        self.assertFalse(rejected.ok)
        self.assertEqual(
            rejected.error_code,
            "SUPERSEDED_PROPOSAL_SUBJECT_MISMATCH",
        )
        self.assertEqual(
            [
                item["id"]
                for item in self.runtime.app.world_state.world_profile.pending_proposals
            ],
            [original.result["proposal"]["id"]],
        )

    def test_revision_cannot_supersede_unrelated_pending_topic(self) -> None:
        kingdom = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "钟鸣公国",
                "world_operations": [
                    {
                        "operation": "create",
                        "category": "kingdoms",
                        "name": "钟鸣公国",
                        "value": "内海公国。",
                        "visibility": "public",
                    }
                ],
            },
            context("国家叫钟鸣公国怎么样？"),
        )
        group = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "队伍是巡钟旅团",
                "world_operations": [
                    {
                        "operation": "create",
                        "category": "group_concept",
                        "value": "受托巡查失常古钟的旅团。",
                        "visibility": "public",
                    }
                ],
            },
            context("小队叫巡钟旅团怎么样？", speaker="南星"),
        )
        self.assertTrue(kingdom.ok, kingdom.to_dict())
        self.assertTrue(group.ok, group.to_dict())

        rejected = self.service.gm_tool_registry.execute(
            "confirm_session_zero_proposal",
            {
                "proposal_id": kingdom.result["proposal"]["id"],
                "superseded_proposal_ids": [group.result["proposal"]["id"]],
            },
            context("我赞成钟鸣公国。", speaker="阿凛"),
        )

        self.assertFalse(rejected.ok)
        self.assertEqual(
            rejected.error_code,
            "SUPERSEDED_PROPOSAL_NOT_DECLARED",
        )
        self.assertEqual(
            len(self.runtime.app.world_state.world_profile.pending_proposals),
            2,
        )

    def test_confirmation_cannot_invent_same_category_supersession(self) -> None:
        north = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "北方的钟鸣公国",
                "world_operations": [
                    {
                        "operation": "create",
                        "category": "kingdoms",
                        "name": "钟鸣公国",
                        "value": "位于北方的钟楼公国。",
                        "visibility": "public",
                    }
                ],
            },
            context("北方可以有钟鸣公国，大家觉得呢？"),
        )
        south = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "南方的赤砂王国",
                "world_operations": [
                    {
                        "operation": "create",
                        "category": "kingdoms",
                        "name": "赤砂王国",
                        "value": "位于南方沙海的王国。",
                        "visibility": "public",
                    }
                ],
            },
            context("南方再有赤砂王国怎么样？", speaker="南星"),
        )
        self.assertTrue(north.ok, north.to_dict())
        self.assertTrue(south.ok, south.to_dict())

        rejected = self.service.gm_tool_registry.execute(
            "confirm_session_zero_proposal",
            {
                "proposal_id": north.result["proposal"]["id"],
                "superseded_proposal_ids": [south.result["proposal"]["id"]],
            },
            context("我赞成北方的钟鸣公国。", speaker="阿凛"),
        )

        self.assertFalse(rejected.ok)
        self.assertEqual(rejected.error_code, "SUPERSEDED_PROPOSAL_NOT_DECLARED")
        self.assertEqual(
            len(self.runtime.app.world_state.world_profile.pending_proposals),
            2,
        )

    def test_legacy_map_proposal_replacement_signs_only_refined_operations(self) -> None:
        proposal_message = "我先提议地图用西部山脉、中央内海、南部海岸、南部驿站和东南群岛。"
        proposed = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "待定的大陆地图节点",
                # Reproduce a persisted proposal from before world_operations
                # became the canonical proposal format.
                "updates": legacy_generic_map_updates(),
            },
            context(proposal_message),
        )
        self.assertTrue(proposed.ok, proposed.to_dict())
        proposal_id = proposed.result["proposal"]["id"]

        confirm_message = (
            "我赞成这个地图提案，但细化为鸦羽山脉、镜线内海、"
            "雾潮海岸、白花碑驿站和潮鸢群岛。"
        )
        confirm_context = context(confirm_message, speaker="南星")
        confirm_arguments = {
            "proposal_id": proposal_id,
            "replacement_world_operations": (
                refined_map_replacement_operations()
            ),
        }
        confirmed = self.service.gm_tool_registry.execute(
            "confirm_session_zero_proposal",
            confirm_arguments,
            confirm_context,
        )

        self.assertTrue(confirmed.ok, confirmed.to_dict())
        self.assertEqual(
            confirmed.result["proposal_resolution"],
            "accepted_with_replacement",
        )
        self.assertTrue(confirmed.result["proposal_replacement_used"])
        self.assertEqual(
            self.runtime.app.world_state.world_profile.pending_proposals,
            [],
        )
        signed_packet = json.dumps(
            {
                "authorized_world_operations": confirmed.result[
                    "authorized_world_operations"
                ],
                "required_followup_calls": confirmed.result[
                    "required_followup_calls"
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        for old_name in _LEGACY_GENERIC_MAP_NAMES:
            self.assertNotIn(old_name, signed_packet)

        GMToolReceiptPolicy.apply_context(
            confirm_context,
            {},
            confirmed,
            tool_arguments=confirm_arguments,
        )
        for followup in confirmed.result["required_followup_calls"]:
            receipt = self.service.gm_tool_registry.execute(
                followup["tool_name"],
                followup["arguments"],
                confirm_context,
            )
            self.assertTrue(receipt.ok, receipt.to_dict())
            GMToolReceiptPolicy.apply_context(
                confirm_context,
                {},
                receipt,
                tool_arguments=followup["arguments"],
            )

        map_names = set(self.runtime.app.world_state.map_locations)
        self.assertEqual(
            map_names,
            {
                "鸦羽山脉",
                "镜线内海",
                "雾潮海岸",
                "白花碑驿站",
                "潮鸢群岛",
            },
        )
        self.assertFalse(map_names & set(_LEGACY_GENERIC_MAP_NAMES))

    def test_replacement_splits_explicit_out_of_scope_create_from_consensus(self) -> None:
        proposed = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "待定的大陆地图节点",
                "updates": legacy_generic_map_updates(),
            },
            context("我先提议一组大陆地图节点。"),
        )
        self.assertTrue(proposed.ok, proposed.to_dict())
        proposal_id = proposed.result["proposal"]["id"]
        confirm_message = (
            "我赞成这个地图提案，并细化为鸦羽山脉、镜线内海、"
            "雾潮海岸、白花碑驿站和潮鸢群岛。"
            "它就是普通的类地球大陆，不用异形世界。"
        )
        replacement = [
            *refined_map_replacement_operations(),
            {
                "operation": "create",
                "category": "world_shape",
                "name": "世界形态",
                "value": "普通的类地球大陆，非异形世界。",
                "visibility": "public",
            },
        ]
        confirm_context = context(confirm_message, speaker="南星")
        confirm_arguments = {
            "proposal_id": proposal_id,
            "replacement_world_operations": replacement,
        }

        confirmed = self.service.gm_tool_registry.execute(
            "confirm_session_zero_proposal",
            confirm_arguments,
            confirm_context,
        )

        self.assertTrue(confirmed.ok, confirmed.to_dict())
        self.assertEqual(
            {item["category"] for item in confirmed.result["authorized_world_operations"]},
            {"map_locations"},
        )
        self.assertEqual(
            [
                item["category"]
                for item in confirmed.result["additional_player_world_operations"]
            ],
            ["world_shape"],
        )
        signed = confirmed.result["required_followup_calls"]
        self.assertEqual(
            [item["arguments"]["authority"] for item in signed],
            [*("table_consensus" for _ in range(5)), "player_confirmed"],
        )

        GMToolReceiptPolicy.apply_context(
            confirm_context,
            {},
            confirmed,
            tool_arguments=confirm_arguments,
        )
        for followup in signed:
            receipt = self.service.gm_tool_registry.execute(
                followup["tool_name"],
                followup["arguments"],
                confirm_context,
            )
            self.assertTrue(receipt.ok, receipt.to_dict())
            GMToolReceiptPolicy.apply_context(
                confirm_context,
                {},
                receipt,
                tool_arguments=followup["arguments"],
            )

        world = self.runtime.app.world_state.world_profile
        self.assertEqual(world.pending_proposals, [])
        self.assertEqual(world.world_shape, "普通的类地球大陆，非异形世界。")

    def test_replacement_normalizes_kingdom_map_dependency_across_authorities(
        self,
    ) -> None:
        proposed = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "钟鸣公国位于镜线内海北岸",
                "world_operations": [
                    {
                        "operation": "create",
                        "category": "map_locations",
                        "name": "钟鸣公国",
                        "value": "镜线内海北岸的钟塔聚落。",
                        "attributes": {
                            "feature_type": "country",
                            "relative_to": "镜线内海",
                            "relative_position": "north",
                        },
                        "visibility": "public",
                    }
                ],
            },
            context("我提议地图上把钟鸣公国放在镜线内海北岸。"),
        )
        self.assertTrue(proposed.ok, proposed.to_dict())
        confirm_message = (
            "我赞成这个地点；钟鸣公国也是我贡献的国家，"
            "由潮钟议会治理。"
        )
        confirm_context = context(confirm_message, speaker="南星")
        confirmed = self.service.gm_tool_registry.execute(
            "confirm_session_zero_proposal",
            {
                "proposal_id": proposed.result["proposal"]["id"],
                "replacement_world_operations": [
                    {
                        "operation": "create",
                        "category": "map_locations",
                        "name": "钟鸣公国",
                        "value": "镜线内海北岸的钟塔公国。",
                        "attributes": {
                            "feature_type": "country",
                            "relative_to": "镜线内海",
                            "relative_position": "north",
                        },
                        "visibility": "public",
                    },
                    {
                        "operation": "create",
                        "category": "kingdoms",
                        "name": "钟鸣公国",
                        "value": "由潮钟议会治理的内海公国。",
                        "visibility": "public",
                    },
                ],
            },
            confirm_context,
        )

        self.assertTrue(confirmed.ok, confirmed.to_dict())
        calls = confirmed.result["required_followup_calls"]
        self.assertEqual(
            [
                (
                    item["tool_name"],
                    item["arguments"]["category"],
                    item["arguments"]["authority"],
                )
                for item in calls
            ],
            [
                ("create_world_setting", "kingdoms", "player_confirmed"),
                ("update_world_setting", "map_locations", "table_consensus"),
            ],
        )
        for call in calls:
            self.assertNotIn(
                proposed.result["proposal"]["id"],
                call["arguments"]["reason"],
            )

        GMToolReceiptPolicy.apply_context(
            confirm_context,
            {},
            confirmed,
            tool_arguments={"proposal_id": proposed.result["proposal"]["id"]},
        )
        for call in calls:
            receipt = self.service.gm_tool_registry.execute(
                call["tool_name"],
                call["arguments"],
                confirm_context,
            )
            self.assertTrue(receipt.ok, receipt.to_dict())
            GMToolReceiptPolicy.apply_context(
                confirm_context,
                {},
                receipt,
                tool_arguments=call["arguments"],
            )

        location = self.runtime.app.world_state.map_locations["钟鸣公国"]
        self.assertEqual(location.relative_to, "镜线内海")
        self.assertEqual(location.relative_position, "north")

    def test_confirmation_cannot_authorize_invented_named_entity_details(
        self,
    ) -> None:
        proposed = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "钟鸣公国位于镜线内海北岸",
                "world_operations": [
                    {
                        "operation": "create",
                        "category": "map_locations",
                        "name": "钟鸣公国",
                        "value": "位于镜线内海北岸。",
                        "visibility": "public",
                    }
                ],
            },
            context("我提议钟鸣公国位于镜线内海北岸。"),
        )
        self.assertTrue(proposed.ok, proposed.to_dict())

        rejected = self.service.gm_tool_registry.execute(
            "confirm_session_zero_proposal",
            {
                "proposal_id": proposed.result["proposal"]["id"],
                "replacement_world_operations": [
                    {
                        "operation": "create",
                        "category": "map_locations",
                        "name": "钟鸣公国",
                        "value": "位于镜线内海北岸。",
                        "visibility": "public",
                    },
                    {
                        "operation": "create",
                        "category": "kingdoms",
                        "name": "钟鸣公国",
                        "value": "由秘密议会统治并禁止魔法的公国。",
                        "attributes": {
                            "government": "秘密议会",
                            "magic_policy": "禁止魔法",
                        },
                        "visibility": "public",
                    },
                ],
            },
            context("我赞成，钟鸣公国也是一个国家。", speaker="南星"),
        )

        self.assertFalse(rejected.ok)
        self.assertEqual(
            rejected.error_code,
            "PROPOSAL_REPLACEMENT_ADDITIONAL_OPERATION_UNSAFE",
        )

    def test_map_confirmation_allows_exactly_named_locations_outside_rough_scope(
        self,
    ) -> None:
        proposed = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "白钟大陆的粗略轮廓",
                "world_operations": [
                    {
                        "operation": "create",
                        "category": "continent_name",
                        "value": "白钟大陆",
                        "visibility": "public",
                    },
                    {
                        "operation": "create",
                        "category": "world_shape",
                        "value": "普通的类地球大陆。",
                        "visibility": "public",
                    },
                ],
            },
            context("我先提议把舞台放在一片普通大陆上，具体地点以后再定。"),
        )
        self.assertTrue(proposed.ok, proposed.to_dict())
        proposal_id = proposed.result["proposal"]["id"]
        message = (
            "我赞成，就按白钟大陆来：西侧叫鸦羽山脉，中央是镜线内海，"
            "南岸放雾潮海岸和白花碑驿站，东南是潮鸢群岛。"
            "它就是普通的类地球大陆。"
        )
        replacement = [
            {
                "operation": "create",
                "category": "continent_name",
                "value": "白钟大陆",
                "visibility": "public",
            },
            {
                "operation": "create",
                "category": "world_shape",
                "value": "普通的类地球大陆。",
                "visibility": "public",
            },
            *[
                {
                    "operation": "create",
                    "category": "map_locations",
                    "name": name,
                    "value": value,
                    "attributes": {
                        "feature_type": feature_type,
                        "position_hint": position,
                    },
                    "visibility": "public",
                }
                for name, value, feature_type, position in (
                    ("鸦羽山脉", "西侧的鸦羽山脉", "mountain_range", "west"),
                    ("镜线内海", "中央的镜线内海", "inland_sea", "center"),
                    ("雾潮海岸", "南岸的雾潮海岸", "coast", "south"),
                    ("白花碑驿站", "南岸的白花碑驿站", "settlement", "south"),
                    ("潮鸢群岛", "东南的潮鸢群岛", "archipelago", "southeast"),
                )
            ],
        ]

        confirmed = self.service.gm_tool_registry.execute(
            "confirm_session_zero_proposal",
            {
                "proposal_id": proposal_id,
                "replacement_world_operations": replacement,
            },
            context(message, speaker="南星"),
        )

        self.assertTrue(confirmed.ok, confirmed.to_dict())
        self.assertEqual(
            [
                item["category"]
                for item in confirmed.result["additional_player_world_operations"]
            ],
            ["map_locations"] * 5,
        )
        self.assertEqual(
            [item["arguments"]["authority"] for item in confirmed.result["required_followup_calls"]],
            ["table_consensus", "table_consensus", *(["player_confirmed"] * 5)],
        )

    def test_replacement_rejects_unsafe_out_of_scope_operation(self) -> None:
        proposed = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "待定的大陆地图节点",
                "updates": legacy_generic_map_updates(),
            },
            context("我先提议一组大陆地图节点。"),
        )
        self.assertTrue(proposed.ok, proposed.to_dict())
        proposal_id = proposed.result["proposal"]["id"]
        replacement = [
            *refined_map_replacement_operations(),
            {
                "operation": "create",
                "category": "world_shape",
                "name": "幕后世界形态",
                "value": "不能借公开确认写入私密世界形态。",
                "visibility": "gm_private",
            },
        ]
        receipt = self.service.gm_tool_registry.execute(
            "confirm_session_zero_proposal",
            {
                "proposal_id": proposal_id,
                "replacement_world_operations": replacement,
            },
            context(
                "我赞成地图提案。它就是普通的类地球大陆，不用异形世界。",
                speaker="南星",
            ),
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "PROPOSAL_REPLACEMENT_ADDITIONAL_OPERATION_UNSAFE",
        )
        self.assertEqual(
            [
                item["id"]
                for item in self.runtime.app.world_state.world_profile.pending_proposals
            ],
            [proposal_id],
        )

    def test_replacement_accepts_player_authored_public_consensus_note(self) -> None:
        proposed = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "大陆轮廓提案",
                "world_operations": [
                    {
                        "operation": "create",
                        "category": "world_shape",
                        "value": "一片被古老森林覆盖的荒野，边缘散落着村庄。",
                        "visibility": "public",
                    }
                ],
            },
            context("要不要先定个大陆轮廓？"),
        )
        self.assertTrue(proposed.ok, proposed.to_dict())
        proposal_id = proposed.result["proposal"]["id"]

        receipt = self.service.gm_tool_registry.execute(
            "confirm_session_zero_proposal",
            {
                "proposal_id": proposal_id,
                "replacement_world_operations": [
                    {
                        "operation": "create",
                        "category": "world_shape",
                        "value": "一片被古老森林覆盖的荒野，边缘散落着村庄。",
                        "visibility": "public",
                    },
                    {
                        "operation": "create",
                        "category": "consensus_notes",
                        "value": (
                            "从边境小事开始，比如一个驿站发生的小麻烦，"
                            "从平凡人的视角切入世界。"
                        ),
                        "visibility": "public",
                    },
                ],
            },
            context(
                "我赞成，大陆轮廓可以是一个被古老森林覆盖的荒野，"
                "边缘散落着村庄。我也喜欢从边境小事开始，比如一个驿站"
                "发生的小麻烦，这样我们能从平凡人的视角切入世界。"
                "要不要我们先定下这个方向？",
                speaker="阿凛",
            ),
        )

        self.assertTrue(receipt.ok, receipt.to_dict())
        self.assertEqual(
            receipt.result["additional_player_scope_categories"],
            ["consensus_notes"],
        )
        self.assertEqual(
            [
                call["arguments"]["authority"]
                for call in receipt.result["required_followup_calls"]
            ],
            ["table_consensus", "player_confirmed"],
        )

    def test_proposal_replacement_cannot_cross_category_or_visibility(self) -> None:
        proposal_message = "我先提议一组大陆地图节点。"
        proposed = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "待定的大陆地图节点",
                "updates": legacy_generic_map_updates(),
            },
            context(proposal_message),
        )
        self.assertTrue(proposed.ok, proposed.to_dict())
        proposal_id = proposed.result["proposal"]["id"]
        confirm_context = context(
            "我赞成地图方向，但要细化内容。",
            speaker="南星",
        )

        invalid_replacements = (
            [
                {
                    "operation": "create",
                    "category": "kingdoms",
                    "name": "白花王国",
                    "value": "这是越出地图提案范围的国家。",
                    "visibility": "public",
                }
            ],
            [
                {
                    "operation": "create",
                    "category": "map_locations",
                    "name": "幕后山谷",
                    "value": "这是越出公开提案可见域的地点。",
                    "attributes": {
                        "feature_type": "region",
                        "terrain": "山谷",
                        "position_hint": "north",
                    },
                    "visibility": "gm_private",
                }
            ],
        )
        for replacement in invalid_replacements:
            with self.subTest(replacement=replacement):
                receipt = self.service.gm_tool_registry.execute(
                    "confirm_session_zero_proposal",
                    {
                        "proposal_id": proposal_id,
                        "replacement_world_operations": replacement,
                    },
                    confirm_context,
                )
                self.assertFalse(receipt.ok)
                self.assertEqual(
                    receipt.error_code,
                    "PROPOSAL_REPLACEMENT_SCOPE_MISMATCH",
                )
                self.assertEqual(
                    [
                        item["id"]
                        for item in self.runtime.app.world_state.world_profile.pending_proposals
                    ],
                    [proposal_id],
                )

    def test_confirmed_proposal_rolls_back_when_autosave_fails(self) -> None:
        proposed = self.service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "小队是护送队",
                "updates": {"group_concept": "护送队"},
            },
            context("先把小队是护送队记成待定提案。"),
        )
        self.assertTrue(proposed.ok, proposed.message)
        proposal_id = proposed.result["proposal"]["id"]

        with patch.object(
            self.service,
            "_autosave_campaign",
            side_effect=RuntimeError("disk unavailable"),
        ):
            confirmed = self.service.gm_tool_registry.execute(
                "confirm_session_zero_proposal",
                {"proposal_id": proposal_id},
                context("我同意这个提案。", speaker="南星"),
            )

        self.assertFalse(confirmed.ok)
        self.assertEqual(confirmed.error_code, "TOOL_EXECUTION_FAILED")
        world = self.runtime.app.session_zero_manager.state.world
        self.assertEqual(world.group_concept, "")
        self.assertEqual(
            [item["id"] for item in world.pending_proposals],
            [proposal_id],
        )

    def test_mark_topic_complete_is_persistent_and_idempotent(self) -> None:
        message = "这个世界谜团我暂时没想法，先跳过。"
        first = self.service.gm_tool_registry.execute(
            "mark_session_zero_topic_complete",
            {"topic": "mystery"},
            context(message),
        )
        second = self.service.gm_tool_registry.execute(
            "mark_session_zero_topic_complete",
            {"topic": "mystery"},
            context(message),
        )

        self.assertTrue(first.ok, first.message)
        self.assertTrue(first.state_changed)
        self.assertTrue(first.result["silent_commit_allowed"])
        self.assertTrue(first.result["source_message_already_public"])
        self.assertTrue(second.ok, second.message)
        self.assertFalse(second.state_changed)
        participant = self.runtime.app.session_zero_manager.find_participant("白河")
        self.assertEqual(
            participant.answered_topics.count("mystery_contributions"),
            1,
        )
        self.assertEqual(participant.contributions.count(message), 1)

        restarted = FUGMHttpService(
            data_root=self.tempdir.name,
            use_llm=False,
        )
        restored = (
            restarted._runtime("第零章工具团")
            .app.session_zero_manager.find_participant("白河")
        )
        self.assertIn("mystery_contributions", restored.answered_topics)

    def test_addressed_topic_skip_keeps_a_short_confirmation(self) -> None:
        message = "@时悠，这个世界谜团我暂时没想法，先跳过。"
        tool_context = context(message)
        tool_context.metadata["is_at_bot"] = True

        receipt = self.service.gm_session_zero_tools.mark_topic_complete(
            tool_context,
            {"topic": "mystery", "evidence": message},
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertFalse(receipt.result["silent_commit_allowed"])
        self.assertFalse(receipt.result["source_message_already_public"])
        self.assertEqual(receipt.public_fallback_reply, "好，这一项先跳过。")

    def test_location_contribution_does_not_make_country_skip_a_noop(self) -> None:
        message = (
            "国家我也先跳过。我补西北的第七采掘城；记忆炉吞掉矿道工人的姓名，"
            "停机协议为何只回应赤羽遗民的歌，是我想追的奥秘；财团正在向雾潮海岸扩张，"
            "监察官艾蕾娜相信集中管理记忆能阻止灾难。"
        )
        committed = self.service.gm_session_zero_tools.commit_update(
            context(message),
            {
                "updates": {
                    "major_locations": {"第七采掘城": "受辉钢财团控制的采掘城。"},
                    "historical_events": ["记忆炉第一次启动时吞掉了一整条矿道工人的姓名。"],
                    "mysteries": ["紧急停机协议为何只回应赤羽遗民的歌？"],
                    "world_threats": ["辉钢财团正在向雾潮海岸扩张。"],
                    "villain_seeds": ["监察官艾蕾娜相信集中管理记忆能阻止灾难。"],
                },
                "evidence": message,
            },
        )
        skipped = self.service.gm_session_zero_tools.mark_topic_complete(
            context(message),
            {"topic": "kingdom", "evidence": message},
        )

        self.assertTrue(committed.ok, committed.message)
        self.assertTrue(committed.state_changed)
        self.assertTrue(skipped.ok, skipped.message)
        self.assertTrue(skipped.state_changed)
        self.assertTrue(skipped.result["silent_commit_allowed"])
        self.assertTrue(skipped.result["source_message_already_public"])
        self.assertEqual(skipped.public_fallback_reply, "好，这一项先跳过。")
        published = GMToolReceiptPolicy.receipt_fallback([committed, skipped])
        self.assertEqual(published, "好，记下了。\n好，这一项先跳过。")

    def test_many_contribution_categories_use_compact_public_summary(self) -> None:
        reply = self.service.gm_session_zero_tools._public_update_confirmation(
            [
                "地点",
                "地图地点",
                "重大历史事件",
                "世界奥秘",
                "世界威胁",
                "反派种子",
            ]
        )

        self.assertEqual(reply, "好，记下了。")

    def test_derived_contributor_fields_cannot_be_spoofed_by_agent(self) -> None:
        message = "我贡献一个国家：钟鸣公国。"

        receipt = self.service.gm_session_zero_tools.commit_update(
            context(message),
            {
                "updates": {
                    "kingdoms": {"钟鸣公国": "钟塔之国"},
                    "kingdom_contributors": {"别人": ["钟鸣公国"]},
                },
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "INVALID_SESSION_ZERO_UPDATE")
        self.assertEqual(self.runtime.app.world_state.world_profile.kingdoms, {})

    def test_tentative_proposal_does_not_commit_world_fact(self) -> None:
        message = "@时悠，先帮我把这个记成待定提案：小队是临时护送队。"

        receipt = self.service.gm_session_zero_tools.propose_update(
            context(message),
            {
                "summary": "小队是临时护送队",
                "updates": {"group_concept": "临时护送队"},
                "evidence": "先帮我把这个记成待定提案：小队是临时护送队。",
            },
        )

        self.assertTrue(receipt.ok)
        self.assertEqual(self.runtime.app.world_state.world_profile.group_concept, "")
        proposals = self.runtime.app.world_state.world_profile.pending_proposals
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["proposed_updates"]["group_concept"], "临时护送队")

    def test_addressed_gm_created_proposal_returns_its_concrete_summary(self) -> None:
        message = "@时悠，帮我们想一个小队方向。"
        tool_context = context(message)
        tool_context.metadata["is_at_bot"] = True
        summary = "我提议你们是护送失忆旅人的临时同盟；第一目标是穿过封锁线。"

        receipt = self.service.gm_session_zero_tools.propose_update(
            tool_context,
            {
                "summary": summary,
                "updates": {"group_concept": "护送失忆旅人的临时同盟"},
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok, receipt.to_dict())
        self.assertEqual(receipt.public_fallback_reply, summary)
        self.assertTrue(receipt.lock_public_reply)
        self.assertFalse(receipt.result["silent_commit_allowed"])

    def test_state_summary_exposes_committed_consensus_and_recent_public_support(self) -> None:
        world = self.runtime.app.world_state.world_profile
        world.group_concept = "临时守护者"
        world.starting_region = "白花碑驿站"
        world.playstyle_themes = ["用证据与承诺化解冲突"]
        world.continent_name = "白钟大陆"
        world.world_shape = "普通大陆"
        world.world_threats = ["辉钢财团收购记忆"]
        world.selected_first_act_id = "白花碑驿站的迟响"
        world.selected_first_act_summary = "保护失忆旅人并找到财团收购记忆的证据。"
        participant = self.runtime.app.session_zero_manager.find_participant("南星")
        self.assertIsNotNone(participant)
        participant.contributions.append("我也赞成从白花碑驿站开幕。")

        summary = self.service.gm_session_zero_tools.state_summary(
            context("我们确认第一幕。")
        )

        self.assertEqual(summary["starting_region"], "白花碑驿站")
        self.assertEqual(summary["group_concept"], "临时守护者")
        self.assertEqual(summary["selected_first_act_id"], "白花碑驿站的迟响")
        self.assertIn("用证据与承诺化解冲突", summary["playstyle_themes"])
        self.assertEqual(summary["world_canon"]["continent_name"], "白钟大陆")
        self.assertEqual(summary["world_canon"]["world_shape"], "普通大陆")
        self.assertFalse(summary["world_map_status"]["blocks_world_creation"])
        self.assertEqual(
            summary["world_canon"]["world_threats"],
            ["辉钢财团收购记忆"],
        )
        self.assertEqual(
            summary["recent_contributions"]["南星"],
            ["我也赞成从白花碑驿站开幕。"],
        )

    def test_explicit_custom_first_act_consensus_uses_summary_without_votes(self) -> None:
        message = (
            "我们确认第一幕：白花碑驿站的迟响。"
            "目标是保护失忆旅人并找到财团收购记忆的证据。"
        )
        summary = "白花碑驿站的迟响；保护失忆旅人并找到财团收购记忆的证据。"

        receipt = self.service.gm_session_zero_tools.commit_update(
            context(message),
            {
                "updates": {"selected_first_act_summary": summary},
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertTrue(receipt.result["first_act_selected"])
        self.assertEqual(receipt.result["selected_first_act_summary"], summary)
        self.assertEqual(
            self.runtime.app.world_state.world_profile.selected_first_act_summary,
            summary,
        )
        self.assertEqual(
            self.runtime.app.world_state.world_profile.first_act_votes,
            {},
        )

    def test_standard_first_act_opens_rulebook_setup_questions_one_at_a_time(self) -> None:
        manager = self.runtime.app.session_zero_manager
        manager.state.world.group_concept = "命运相会的临时同伴"
        manager.generate_first_act_candidates(count=6)

        selected = self.service.gm_session_zero_tools.commit_update(
            context("我们确认选第四个开场，明日处刑。"),
            {
                "updates": {"selected_first_act_id": "first_act_4"},
                "evidence": "我们确认选第四个开场，明日处刑。",
            },
        )

        self.assertTrue(selected.ok, selected.message)
        setup = selected.result["first_act_setup"]
        self.assertEqual(setup["title"], "明日处刑")
        self.assertEqual(
            setup["open_questions"],
            [
                "你们为什么会被关起来？",
                "你们是无辜的还是有罪的？",
                "你们能独自逃离吗，还是需要他人的帮助？",
            ],
        )
        self.assertEqual(setup["next_question"], "你们为什么会被关起来？")
        self.assertTrue(setup["guidance_only"])

    def test_any_player_can_answer_or_skip_prologue_setup_questions(self) -> None:
        manager = self.runtime.app.session_zero_manager
        manager.state.world.group_concept = "命运相会的临时同伴"
        manager.generate_first_act_candidates(count=6)
        self.service.gm_session_zero_tools.commit_update(
            context("我们确认选第四个开场。"),
            {
                "updates": {"selected_first_act_id": "first_act_4"},
                "evidence": "我们确认选第四个开场。",
            },
        )

        first = self.service.gm_tool_registry.execute(
            "record_prologue_setup_answer",
            {
                "question": "1",
                "resolution": "answered",
                "answer": "艾丽妮在市集偷吃魔法水果充饥，被卫兵逮捕。",
            },
            context(
                "艾丽妮在市集偷吃魔法水果充饥，被卫兵逮捕。",
                speaker="南星",
            ),
        )

        self.assertTrue(first.ok, first.message)
        self.assertEqual(
            first.result["first_act_setup"]["next_question"],
            "你们是无辜的还是有罪的？",
        )
        world = manager.state.world
        self.assertEqual(
            world.first_act_question_answers["你们为什么会被关起来？"],
            ["南星：艾丽妮在市集偷吃魔法水果充饥，被卫兵逮捕。"],
        )

        skipped = self.service.gm_tool_registry.execute(
            "record_prologue_setup_answer",
            {"question": "2", "resolution": "skipped"},
            context("是否有罪这一问先跳过。", speaker="白河"),
        )

        self.assertTrue(skipped.ok, skipped.message)
        self.assertEqual(
            skipped.result["first_act_setup"]["next_question"],
            "你们能独自逃离吗，还是需要他人的帮助？",
        )

        delegated = self.service.gm_tool_registry.execute(
            "record_prologue_setup_answer",
            {
                "question": "3",
                "resolution": "gm_decides",
                "answer": "牢门外还有一道需要两人同时解除的旧式法阵。",
            },
            context("逃狱需要什么帮助就由你来想。", speaker="白河"),
        )

        self.assertTrue(delegated.ok, delegated.message)
        self.assertTrue(delegated.result["first_act_setup"]["all_resolved"])
        self.assertEqual(delegated.result["first_act_setup"]["open_questions"], [])

    def test_prologue_setup_answers_survive_campaign_restart(self) -> None:
        manager = self.runtime.app.session_zero_manager
        manager.state.world.group_concept = "命运相会的临时同伴"
        manager.generate_first_act_candidates(count=6)
        self.service.gm_session_zero_tools.commit_update(
            context("我们确认选第四个开场。"),
            {
                "updates": {"selected_first_act_id": "first_act_4"},
                "evidence": "我们确认选第四个开场。",
            },
        )
        self.service.gm_tool_registry.execute(
            "record_prologue_setup_answer",
            {
                "question": "你们为什么会被关起来？",
                "resolution": "answered",
                "answer": "诺艾尔洗劫男爵藏品时被法阵困住。",
            },
            context("诺艾尔洗劫男爵藏品时被法阵困住。"),
        )

        restarted = FUGMHttpService(data_root=self.tempdir.name, use_llm=False)
        restored = restarted._runtime("第零章工具团").app.world_state.world_profile

        self.assertEqual(restored.selected_first_act_id, "first_act_4")
        self.assertEqual(
            restored.first_act_question_answers["你们为什么会被关起来？"],
            ["白河：诺艾尔洗劫男爵藏品时被法阵困住。"],
        )

    def test_custom_first_act_title_cannot_be_masqueraded_as_candidate_id(self) -> None:
        message = "我们确认第一幕：白花碑驿站的迟响。"

        receipt = self.service.gm_session_zero_tools.commit_update(
            context(message),
            {
                "updates": {
                    "selected_first_act_id": "白花碑驿站的迟响",
                    "selected_first_act_summary": "从驿站保护旅人离开。",
                },
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "UNKNOWN_FIRST_ACT_CANDIDATE")
        self.assertIn("不需要逐人投票", receipt.correction_hint)
        world = self.runtime.app.world_state.world_profile
        self.assertEqual(world.selected_first_act_id, "")
        self.assertEqual(world.selected_first_act_summary, "")

    def test_confirming_proposal_commits_and_clears_it_atomically(self) -> None:
        proposal_message = "我提议小队是临时护送队，先听听大家意见。"
        proposal = self.service.gm_session_zero_tools.propose_update(
            context(proposal_message),
            {
                "summary": "小队是临时护送队",
                "updates": {"group_concept": "临时护送队"},
                "evidence": "我提议小队是临时护送队",
            },
        ).result["proposal"]

        confirm_message = "我同意，就按临时护送队写入。"
        receipt = self.service.gm_session_zero_tools.confirm_proposal(
            context(confirm_message, speaker="南星"),
            {
                "proposal_id": proposal["id"],
                "evidence": "我同意，就按临时护送队写入。",
            },
        )

        self.assertTrue(receipt.ok)
        world = self.runtime.app.world_state.world_profile
        self.assertEqual(world.group_concept, "临时护送队")
        self.assertEqual(world.pending_proposals, [])

    def test_one_skill_can_be_recorded_without_completing_draft(self) -> None:
        self.runtime.app.world_state.world_profile.hero_drafts["白河"] = HeroDraft(
            player_name="白河",
            hero_name="洛岚",
            classes={"造物使": 3, "旅人": 2},
        )
        message = "洛岚第一项技能选择便携装置。"

        receipt = self.service.gm_session_zero_tools.update_hero_draft(
            context(message),
            {
                "subject": "洛岚",
                "patch": {"skills": {"便携装置": 1}},
                "evidence": "洛岚第一项技能选择便携装置。",
            },
        )

        self.assertTrue(receipt.ok)
        self.assertEqual(
            self.runtime.app.world_state.world_profile.hero_drafts["白河"].skills,
            {"便携装置": 1},
        )
        self.assertFalse(receipt.result["ready"])
        self.assertEqual(receipt.public_fallback_reply, "【便携装置】记下了。")
        self.assertTrue(receipt.result["silent_commit_allowed"])
        self.assertTrue(receipt.result["source_message_already_public"])
        self.assertFalse(receipt.lock_public_reply)

    def test_unallocated_class_choices_use_preferences_instead_of_zero_levels(
        self,
    ) -> None:
        message = "赛璃想选御魂使和旅人，等级还没决定。"

        receipt = self.service.gm_session_zero_tools.update_hero_draft(
            context(message, speaker="南星"),
            {
                "subject": "南星",
                "patch": {"class_preferences": ["御魂使", "旅人"]},
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok, receipt.to_dict())
        draft = self.runtime.app.world_state.world_profile.hero_drafts["南星"]
        self.assertEqual(draft.class_preferences, ["御魂使", "旅人"])
        self.assertEqual(draft.classes, {})
        self.assertEqual(receipt.result["completion_scope"], "source_statement")

    def test_zero_level_classes_are_rejected_with_structured_repair(self) -> None:
        message = "赛璃想选御魂使和旅人，等级还没决定。"

        receipt = self.service.gm_session_zero_tools.update_hero_draft(
            context(message, speaker="南星"),
            {
                "subject": "南星",
                "patch": {"classes": {"御魂使": 0, "旅人": 0}},
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "ZERO_LEVEL_HERO_CLASS")
        self.assertIn("class_preferences", receipt.correction_hint)
        self.assertNotIn(
            "南星",
            self.runtime.app.world_state.world_profile.hero_drafts,
        )

    def test_complete_class_allocation_replaces_transient_preferences(self) -> None:
        draft = HeroDraft(
            player_name="南星",
            hero_name="赛璃",
            class_preferences=["御魂使", "旅人"],
        )
        self.runtime.app.world_state.world_profile.hero_drafts["南星"] = draft
        message = "御魂使3级，旅人2级。"

        receipt = self.service.gm_session_zero_tools.update_hero_draft(
            context(message, speaker="南星"),
            {
                "subject": "南星",
                "patch": {"classes": {"御魂使": 3, "旅人": 2}},
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok, receipt.to_dict())
        stored = self.runtime.app.world_state.world_profile.hero_drafts["南星"]
        self.assertEqual(stored.classes, {"御魂使": 3, "旅人": 2})
        self.assertEqual(stored.class_preferences, [])

    def test_unknown_complete_class_name_is_rejected_before_persistence(self) -> None:
        original = HeroDraft(player_name="阿凛", hero_name="伊莉雅")
        self.runtime.app.world_state.world_profile.hero_drafts["阿凛"] = original
        message = "伊莉雅选择盾誓骑士3级、守护者2级。"

        receipt = self.service.gm_session_zero_tools.update_hero_draft(
            context(message, speaker="阿凛"),
            {
                "subject": "伊莉雅",
                "patch": {"classes": {"盾誓骑士": 3, "守护者": 2}},
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "INVALID_HERO_CLASS")
        self.assertIn("未知职业：盾誓骑士", receipt.public_fallback_reply)
        self.assertEqual(
            self.runtime.app.world_state.world_profile.hero_drafts["阿凛"],
            original,
        )

    def test_invalid_complete_attribute_pattern_is_rejected_before_persistence(self) -> None:
        original = HeroDraft(player_name="南星", hero_name="赛璃")
        self.runtime.app.world_state.world_profile.hero_drafts["南星"] = original
        message = "赛璃走均衡偏洞察，洞察d12、意志d10、力量d8、敏捷d10。"

        receipt = self.service.gm_session_zero_tools.update_hero_draft(
            context(message, speaker="南星"),
            {
                "subject": "赛璃",
                "patch": {
                    "attributes": {
                        "洞察": 12,
                        "意志": 10,
                        "力量": 8,
                        "敏捷": 10,
                    }
                },
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "INVALID_HERO_ATTRIBUTE_COMBINATION")
        self.assertIn("起始属性必须采用规则书组合", receipt.public_fallback_reply)
        self.assertEqual(
            self.runtime.app.world_state.world_profile.hero_drafts["南星"],
            original,
        )

    def test_addressed_hero_draft_update_keeps_short_confirmation(self) -> None:
        self.runtime.app.world_state.world_profile.hero_drafts["白河"] = HeroDraft(
            player_name="白河",
            hero_name="洛岚",
            classes={"造物使": 3, "旅人": 2},
        )
        message = "@时悠，洛岚第一项技能选择便携装置。"
        tool_context = context(message)
        tool_context.metadata["is_at_bot"] = True

        receipt = self.service.gm_session_zero_tools.update_hero_draft(
            tool_context,
            {
                "subject": "洛岚",
                "patch": {"skills": {"便携装置": 1}},
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok)
        self.assertFalse(receipt.result["silent_commit_allowed"])
        self.assertTrue(receipt.lock_public_reply)
        self.assertEqual(receipt.public_fallback_reply, "【便携装置】记下了。")

    def test_named_gm_whole_build_replaces_groups_and_preserves_duplicate_equipment(
        self,
    ) -> None:
        self.runtime.app.world_state.world_profile.hero_drafts["测试玩家乙"] = HeroDraft(
            player_name="测试玩家乙",
            hero_name="伊大石",
            classes={"暗刃骑士": 1, "守护者": 3, "元素使": 1},
            skills={"痛楚": 1, "暗影击": 1, "保镖": 1, "铁壁": 1, "元素魔法": 1},
            equipment=["大黑锅", "符文盾", "青铜板甲"],
            equipment_slots={"main_hand": "大黑锅", "off_hand": "符文盾"},
        )
        message = (
            "@时悠 伊大石的职业变更为守护者4级+元素使1级，技能选择保镖 "
            "防御精通，双盾战士，挺身守护，元素系仪式，装备是大黑锅"
            "(符文盾模板)，大黑锅(符文盾模板)，青铜板甲"
        )
        tool_context = context(message, speaker="测试玩家乙")
        tool_context.metadata["current_turn_events"] = [
            {
                "speaker": "测试玩家乙",
                "text": message,
                "is_named_gm": True,
            }
        ]

        receipt = self.service.gm_session_zero_tools.update_hero_draft(
            tool_context,
            {
                "subject": "伊大石",
                "patch": {
                    "classes": {"守护者": 4, "元素使": 1},
                    "replace_classes": True,
                    "skills": {
                        "保镖": 1,
                        "防御精通": 1,
                        "双盾战士": 1,
                        "挺身守护": 1,
                        "元素系仪式": 1,
                    },
                    "replace_skills": True,
                    "equipment": [
                        "大黑锅（符文盾模板）",
                        "大黑锅（符文盾模板）",
                        "青铜板甲",
                    ],
                    "equipment_slots": {
                        "main_hand": "大黑锅（符文盾模板）",
                        "off_hand": "大黑锅（符文盾模板）",
                        "armor": "青铜板甲",
                    },
                    "replace_equipment": True,
                },
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        draft = self.runtime.app.world_state.world_profile.hero_drafts["测试玩家乙"]
        self.assertEqual(draft.classes, {"守护者": 4, "元素使": 1})
        self.assertEqual(
            draft.skills,
            {
                "保镖": 1,
                "防御精通": 1,
                "双盾战士": 1,
                "挺身守护": 1,
                "元素系仪式": 1,
            },
        )
        self.assertEqual(
            draft.equipment,
            [
                "大黑锅（符文盾模板）",
                "大黑锅（符文盾模板）",
                "青铜板甲",
            ],
        )
        self.assertEqual(
            receipt.result["replacement_modes"],
            {
                "replace_classes": True,
                "replace_skills": True,
                "replace_equipment": True,
            },
        )
        self.assertFalse(receipt.result["silent_commit_allowed"])
        self.assertTrue(receipt.lock_public_reply)
        self.assertEqual(
            receipt.public_fallback_reply,
            "伊大石的职业、技能、装备已经按这次方案更新了。",
        )

    def test_complete_class_replacement_cannot_be_repaired_by_lowering_player_rank(
        self,
    ) -> None:
        original = HeroDraft(
            player_name="测试玩家乙",
            hero_name="伊大石",
            classes={"暗刃骑士": 1, "守护者": 3, "元素使": 1},
        )
        self.runtime.app.world_state.world_profile.hero_drafts["测试玩家乙"] = original
        message = "伊大石的职业变更为守护者4级+元素使1级。"

        receipt = self.service.gm_session_zero_tools.update_hero_draft(
            context(message, speaker="测试玩家乙"),
            {
                "subject": "伊大石",
                "patch": {"classes": {"守护者": 4, "元素使": 1}},
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "HERO_CLASS_REPLACEMENT_REQUIRED")
        self.assertIn("replace_classes=true", receipt.correction_hint)
        self.assertIn("不得降低等级", receipt.correction_hint)
        self.assertEqual(
            self.runtime.app.world_state.world_profile.hero_drafts["测试玩家乙"].classes,
            {"暗刃骑士": 1, "守护者": 3, "元素使": 1},
        )

    def test_cross_player_correction_cannot_overwrite_another_players_draft(self) -> None:
        self.runtime.app.world_state.world_profile.hero_drafts["南星"] = HeroDraft(
            player_name="南星",
            hero_name="旧名字",
        )
        message = "@时悠，刚才对应关系说反了，她的角色其实叫赛璃。"

        receipt = self.service.gm_session_zero_tools.update_hero_draft(
            context(message, speaker="白河"),
            {
                "subject": "南星",
                "patch": {"hero_name": "赛璃"},
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "HERO_DRAFT_UPDATE_NOT_OWNER")
        self.assertEqual(
            self.runtime.app.world_state.world_profile.hero_drafts["南星"].hero_name,
            "旧名字",
        )

    def test_unknown_split_skill_name_is_rejected_without_dirty_write(self) -> None:
        self.runtime.app.world_state.world_profile.hero_drafts["白河"] = HeroDraft(
            player_name="白河",
            hero_name="苍祈",
            classes={"奥灵使": 2, "拟兽使": 2, "暗刃骑士": 1},
        )
        message = "苍祈奥灵使技能先选契约与召唤。"

        receipt = self.service.gm_session_zero_tools.update_hero_draft(
            context(message, speaker="白河"),
            {
                "subject": "苍祈",
                "patch": {"skills": {"契约": 1, "召唤": 1}},
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "UNKNOWN_HERO_SKILL")
        self.assertEqual(
            self.runtime.app.world_state.world_profile.hero_drafts["白河"].skills,
            {},
        )

    def test_canonical_skill_reference_reaches_structural_handler(self) -> None:
        self.runtime.app.world_state.world_profile.hero_drafts["白河"] = HeroDraft(
            player_name="白河",
            hero_name="赛璃",
            classes={"御魂使": 3, "旅人": 2},
            skills={"灵魂魔法": 2},
        )
        message = "赛璃第三项技能选前者。"

        receipt = self.service.gm_session_zero_tools.update_hero_draft(
            context(message, speaker="白河"),
            {
                "subject": "赛璃",
                "patch": {"skills": {"灵魂魔法": 3}},
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok)
        self.assertEqual(
            self.runtime.app.world_state.world_profile.hero_drafts["白河"].skills,
            {"灵魂魔法": 3},
        )
        unresolved = receipt.result["unresolved_skill_choices"]
        spell_choice = next(
            item for item in unresolved if item["skill_name"] == "灵魂魔法"
        )
        self.assertEqual(spell_choice["missing_count"], 3)
        self.assertNotIn("allowed_values", spell_choice)
        self.assertEqual(spell_choice["allowed_value_count"], 13)
        self.assertEqual(
            spell_choice["catalog_query"],
            {
                "kind": "spell",
                "school": "御魂使法术",
                "view": "shortlist",
                "limit": 3,
            },
        )

    def test_duplicate_single_rank_skill_increment_is_rejected_without_write(self) -> None:
        self.runtime.app.world_state.world_profile.hero_drafts["白河"] = HeroDraft(
            player_name="白河",
            hero_name="赛璃",
            classes={"御魂使": 3, "旅人": 2},
            skills={"御魂系仪式": 1},
        )
        message = "赛璃第三项技能选御魂系仪式。"

        receipt = self.service.gm_session_zero_tools.update_hero_draft(
            context(message),
            {
                "subject": "赛璃",
                "patch": {
                    "skills": {"御魂系仪式": 1},
                    "increment_skills": True,
                },
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "HERO_SKILL_RANK_EXCEEDED")
        self.assertEqual(
            self.runtime.app.world_state.world_profile.hero_drafts["白河"].skills,
            {"御魂系仪式": 1},
        )

    def test_complete_skill_list_must_replace_existing_choices(self) -> None:
        original_skills = {"闪避": 1, "窃取灵魂": 1}
        self.runtime.app.world_state.world_profile.hero_drafts["白河"] = HeroDraft(
            player_name="白河",
            hero_name="灰烬",
            classes={"浪客": 2, "暗刃骑士": 1, "武器大师": 2},
            skills=original_skills.copy(),
        )
        message = "技能选择阴狠手段，回见了您呐，苦痛教训，碎骨，反击"
        complete_skills = {
            "阴狠手段": 1,
            "回见了您呐": 1,
            "苦痛教训": 1,
            "碎骨": 1,
            "反击": 1,
        }

        rejected = self.service.gm_session_zero_tools.update_hero_draft(
            context(message),
            {
                "subject": "灰烬",
                "patch": {"skills": complete_skills},
                "evidence": message,
            },
        )

        self.assertFalse(rejected.ok)
        self.assertEqual(rejected.error_code, "HERO_SKILL_ALLOCATION_EXCEEDED")
        self.assertTrue(rejected.retryable)
        self.assertEqual(
            self.runtime.app.world_state.world_profile.hero_drafts["白河"].skills,
            original_skills,
        )

        replaced = self.service.gm_session_zero_tools.update_hero_draft(
            context(message),
            {
                "subject": "灰烬",
                "patch": {"skills": complete_skills, "replace_skills": True},
                "evidence": message,
            },
        )

        self.assertTrue(replaced.ok, replaced.message)
        self.assertEqual(
            self.runtime.app.world_state.world_profile.hero_drafts["白河"].skills,
            complete_skills,
        )

    def test_remove_skill_matches_legacy_alias_and_cleans_options(self) -> None:
        self.runtime.app.world_state.world_profile.hero_drafts["南星"] = HeroDraft(
            player_name="南星",
            hero_name="艾丽妮",
            classes={"元素使": 2, "旅人": 1, "博学家": 2},
            skills={"愤怒": 1, "元素魔法": 1},
            skill_options={"愤怒": ["旧存档残留"]},
        )
        message = "把不属于艾丽妮的职业技能：痛楚删掉。"

        receipt = self.service.gm_session_zero_tools.update_hero_draft(
            context(message, speaker="南星"),
            {
                "subject": "艾丽妮",
                "patch": {"remove_skills": ["痛楚"]},
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        draft = self.runtime.app.world_state.world_profile.hero_drafts["南星"]
        self.assertEqual(draft.skills, {"元素魔法": 1})
        self.assertEqual(draft.skill_options, {})
        self.assertFalse(receipt.result["ready"])
        self.assertNotIn("missing_fields", receipt.result)
        self.assertNotIn("errors", receipt.result)

    def test_no_effect_hero_patch_is_rejected_without_false_success(self) -> None:
        original = HeroDraft(
            player_name="南星",
            hero_name="艾丽妮",
            classes={"元素使": 2, "旅人": 1, "博学家": 2},
            skills={"元素魔法": 1},
        )
        self.runtime.app.world_state.world_profile.hero_drafts["南星"] = original
        message = "把艾丽妮的痛楚删掉。"

        receipt = self.service.gm_session_zero_tools.update_hero_draft(
            context(message, speaker="南星"),
            {
                "subject": "艾丽妮",
                "patch": {"remove_skills": ["痛楚"]},
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "HERO_PATCH_NO_EFFECT")
        self.assertFalse(receipt.state_changed)
        self.assertEqual(
            self.runtime.app.world_state.world_profile.hero_drafts["南星"],
            original,
        )
        participant = self.runtime.app.session_zero_manager.find_participant("南星")
        self.assertNotIn(message, participant.contributions)

    def test_complete_hero_draft_requires_explicit_confirmation_tool(self) -> None:
        self.runtime.app.world_state.world_profile.hero_drafts["白河"] = HeroDraft(
            player_name="白河",
            hero_name="洛岚",
            identity="辉钢财团出逃的魔导工匠",
            theme="赎罪",
            origin="第七采掘城",
            classes={"造物使": 3, "武器大师": 2},
            attributes={"敏捷": 8, "洞察": 10, "力量": 8, "意志": 6},
            skills={
                "便携装置": 1,
                "秘密配方": 1,
                "先见之明": 1,
                "碎骨": 1,
                "破防打击": 1,
            },
            skill_options={"便携装置": ["魔导装置"]},
            equipment=["铁锤", "旅行装束"],
        )
        message = "洛岚确认角色并正式建卡。"

        receipt = self.service.gm_session_zero_tools.confirm_hero_draft(
            context(message),
            {"subject": "洛岚", "evidence": message},
        )

        self.assertTrue(receipt.ok)
        self.assertTrue(
            self.runtime.app.world_state.world_profile.hero_drafts["白河"].confirmed
        )
        self.assertTrue(receipt.result["confirmed"])

    def test_incomplete_hero_draft_is_not_marked_confirmed(self) -> None:
        self.runtime.app.world_state.world_profile.hero_drafts["白河"] = HeroDraft(
            player_name="白河",
            hero_name="洛岚",
        )
        message = "洛岚确认角色并正式建卡。"

        receipt = self.service.gm_session_zero_tools.confirm_hero_draft(
            context(message),
            {"subject": "洛岚", "evidence": message},
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "HERO_DRAFT_INCOMPLETE")
        self.assertFalse(
            self.runtime.app.world_state.world_profile.hero_drafts["白河"].confirmed
        )

    def test_confirmation_failure_exposes_the_actual_rule_blocker(self) -> None:
        self._make_adventure_ready()
        draft = self.runtime.app.world_state.world_profile.hero_drafts["南星"]
        draft.confirmed = False
        draft.attributes = {"敏捷": 10, "洞察": 12, "力量": 8, "意志": 10}
        message = "确认，就按这版正式建卡。"

        receipt = self.service.gm_session_zero_tools.confirm_hero_draft(
            context(message, speaker="南星"),
            {"subject": "赛璃", "evidence": message},
        )

        self.assertFalse(receipt.ok)
        self.assertIn("起始属性必须采用规则书组合", receipt.public_fallback_reply)

    def test_nudge_asks_for_invalid_attribute_correction_not_confirmation(self) -> None:
        self._make_adventure_ready()
        manager = self.runtime.app.session_zero_manager
        draft = manager.state.world.hero_drafts["南星"]
        draft.confirmed = False
        draft.attributes = {"敏捷": 10, "洞察": 12, "力量": 8, "意志": 10}

        plan = manager.session_zero_progress_nudge_plan(preferred_player="南星")

        self.assertEqual(plan["topic"], "hero_creation:hero_attributes")
        self.assertIn("起始属性必须采用规则书组合", plan["prompt_hint"])
        self.assertNotIn("确认建卡", plan["prompt_hint"])
        self.assertIn(
            "赛璃",
            manager.hero_creation_status()["validation_errors_by_player"],
        )

    def test_confirmation_tool_validates_authority_and_state_not_wording(self) -> None:
        self.runtime.app.world_state.world_profile.hero_drafts["测试玩家乙"] = HeroDraft(
            player_name="测试玩家乙",
            hero_name="伊大石",
            identity="原魔法学院厨师",
            theme="守护",
            origin="土豆村",
            classes={"守护者": 4, "元素使": 1},
            attributes={"敏捷": 6, "洞察": 6, "力量": 10, "意志": 10},
            skills={
                "保镖": 1,
                "防御精通": 1,
                "双盾战士": 1,
                "挺身守护": 1,
                "元素系仪式": 1,
            },
            equipment=["符文盾", "符文盾", "青铜板甲"],
        )
        message = "提供给我角色草稿，我确认一下好正式建卡"

        receipt = self.service.gm_session_zero_tools.confirm_hero_draft(
            context(message, speaker="测试玩家乙"),
            {"subject": "伊大石", "evidence": message},
        )

        self.assertTrue(receipt.ok)
        self.assertTrue(
            self.runtime.app.world_state.world_profile.hero_drafts[
                "测试玩家乙"
            ].confirmed
        )

    def test_explicit_safety_label_keeps_player_classification(self) -> None:
        message = "界限：不详细描写性暴力、酷刑、现实仇恨煽动。"

        receipt = self.service.gm_session_zero_tools.record_safety_boundary(
            context(message),
            {
                "kind": "line",
                "content": "不详细描写性暴力、酷刑、现实仇恨煽动",
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok)
        self.assertEqual(receipt.result["kind"], "line")
        self.assertIn(
            "性暴力、酷刑、现实仇恨煽动",
            self.runtime.app.world_state.world_profile.safety_lines,
        )

    def test_non_literal_evidence_causes_zero_write(self) -> None:
        message = "这个国家叫钟鸣公国。"

        receipt = self.service.gm_session_zero_tools.commit_update(
            context(message),
            {
                "updates": {"kingdoms": {"钟鸣公国": "风铃与钟塔之国"}},
                "evidence": "玩家已经确认钟鸣公国",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "EVIDENCE_NOT_IN_CURRENT_MESSAGE")
        self.assertEqual(self.runtime.app.world_state.world_profile.kingdoms, {})

    def test_invalid_world_field_is_rejected_before_write(self) -> None:
        message = "把后台暗线直接写进去。"

        receipt = self.service.gm_session_zero_tools.commit_update(
            context(message),
            {
                "updates": {"gm_secret_notes": ["玩家不知道的真相"]},
                "evidence": "把后台暗线直接写进去。",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "INVALID_SESSION_ZERO_UPDATE")
        self.assertEqual(self.runtime.app.world_state.world_profile.gm_secret_notes, [])

    def test_explicit_safety_tool_records_requested_kind(self) -> None:
        message = "请把虐待儿童设为界限，不要在游戏里出现。"

        receipt = self.service.gm_session_zero_tools.record_safety_boundary(
            context(message),
            {
                "kind": "line",
                "content": "虐待儿童",
                "evidence": "请把虐待儿童设为界限",
            },
        )

        self.assertTrue(receipt.ok)
        self.assertIn("虐待儿童", self.runtime.app.world_state.world_profile.safety_lines)
        self.assertNotIn("虐待儿童", self.runtime.app.world_state.world_profile.safety_veils)

    def test_private_safety_tool_forces_anonymous_persistence(self) -> None:
        message = "界限：不要出现蜘蛛。"

        receipt = self.service.gm_session_zero_tools.record_safety_boundary(
            context(message, speaker="真实玩家名", is_private=True),
            {
                "kind": "line",
                "content": "蜘蛛",
                "evidence": message,
                "anonymous": False,
            },
        )

        self.assertTrue(receipt.ok)
        self.assertTrue(receipt.result["anonymous"])
        memories = self.runtime.app.world_state.memories
        self.assertIn("匿名玩家声明界限：蜘蛛", memories)
        self.assertFalse(any("真实玩家名" in item for item in memories))

    def test_topic_skip_counts_for_named_player(self) -> None:
        message = "历史事件这项我暂时没想法，先跳过。"

        receipt = self.service.gm_session_zero_tools.mark_topic_complete(
            context(message),
            {
                "topic": "historical_event",
                "evidence": "历史事件这项我暂时没想法，先跳过。",
            },
        )

        self.assertTrue(receipt.ok)
        participant = self.runtime.app.session_zero_manager.find_participant("白河")
        self.assertIn("historical_event_contributions", participant.answered_topics)

    def test_safety_discussion_can_complete_without_inventing_a_boundary(self) -> None:
        manager = self.runtime.app.session_zero_manager

        first = self.service.gm_session_zero_tools.mark_topic_complete(
            context("我没有界限或帷幕要补充。", speaker="白河"),
            {
                "topic": "safety",
                "evidence": "我没有界限或帷幕要补充。",
            },
        )
        self.assertTrue(first.ok)
        self.assertFalse(manager.progress_summary()["safety"])
        self.assertEqual(manager.state.world.safety_lines, [])
        self.assertEqual(manager.state.world.safety_veils, [])

        second = self.service.gm_session_zero_tools.mark_topic_complete(
            context("我这边也没有要补充的。", speaker="南星"),
            {
                "topic": "safety",
                "evidence": "我这边也没有要补充的。",
            },
        )
        self.assertTrue(second.ok)
        self.assertTrue(manager.progress_summary()["safety"])
        self.assertEqual(manager.state.world.safety_lines, [])
        self.assertEqual(manager.state.world.safety_veils, [])

    def test_progress_nudge_finishes_shared_foundations_before_contributions(self) -> None:
        manager = self.runtime.app.session_zero_manager
        world = manager.state.world

        plan = manager.session_zero_progress_nudge_plan()
        self.assertEqual(plan["topic"], "tone")

        world.tone_preferences = ["轻松中带点余味，但保留希望。"]
        plan = manager.session_zero_progress_nudge_plan()
        self.assertEqual(plan["topic"], "safety")

        for participant in manager.state.participants:
            participant.answered_topics.append("safety")
        plan = manager.session_zero_progress_nudge_plan()
        self.assertEqual(plan["topic"], "world_shape")

        world.world_shape = "一片被内海分开的大陆。"
        plan = manager.session_zero_progress_nudge_plan()
        self.assertEqual(plan["topic"], "magic_tech_role")

        world.magic_tech_role = "魔法与机械共同进入日常生活。"
        plan = manager.session_zero_progress_nudge_plan()
        self.assertEqual(plan["topic"], "kingdom_contributions")

    def test_recorded_safety_boundary_marks_only_its_speaker_reviewed(self) -> None:
        manager = self.runtime.app.session_zero_manager
        message = "帷幕：亲密场景淡出。"

        receipt = self.service.gm_session_zero_tools.record_safety_boundary(
            context(message, speaker="白河"),
            {
                "kind": "veil",
                "content": "亲密场景",
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok)
        self.assertIn("safety", manager.find_participant("白河").answered_topics)
        self.assertNotIn("safety", manager.find_participant("南星").answered_topics)
        self.assertFalse(manager.progress_summary()["safety"])

    def test_nudge_plan_prefers_less_contributing_player_over_last_speaker(self) -> None:
        manager = self.runtime.app.session_zero_manager
        active = manager.find_participant("白河")
        active.answered_topics.extend(
            ["kingdom_contributions", "historical_event_contributions"]
        )

        plan = manager.session_zero_nudge_plan(
            last_player_speaker="白河",
        )

        self.assertEqual(plan["status"], "targeted")
        self.assertEqual(plan["player"], "南星")
        self.assertEqual(plan["topic"], "kingdom_contributions")

    def test_nudge_plan_does_not_repeat_same_player_topic_after_limit(self) -> None:
        manager = self.runtime.app.session_zero_manager
        active = manager.find_participant("白河")
        active.answered_topics.extend(
            ["kingdom_contributions", "historical_event_contributions"]
        )

        plan = manager.session_zero_nudge_plan(
            last_player_speaker="白河",
            prior_topic_counts={
                ("南星", "kingdom_contributions"): 2,
            },
            topic_nudge_limit=2,
        )

        self.assertEqual(plan["status"], "targeted")
        self.assertEqual(plan["player"], "南星")
        self.assertEqual(plan["topic"], "historical_event_contributions")

    def test_nudge_plan_exposes_prior_contribution_without_asking_for_a_copy(
        self,
    ) -> None:
        manager = self.runtime.app.session_zero_manager
        active = manager.find_participant("白河")
        active.answered_topics.extend(
            ["kingdom_contributions", "historical_event_contributions"]
        )
        quiet = manager.find_participant("南星")
        quiet.answered_topics.append("kingdom_contributions")
        manager.state.world.historical_event_contributors = {
            "白河": ["大寂潮迫使沿海居民迁往浮岛。"],
        }

        plan = manager.session_zero_nudge_plan(last_player_speaker="白河")

        self.assertEqual(plan["player"], "南星")
        self.assertEqual(plan["topic"], "historical_event_contributions")
        self.assertEqual(
            plan["prior_contributions"],
            [
                {
                    "player": "白河",
                    "contributions": ["大寂潮迫使沿海居民迁往浮岛。"],
                }
            ],
        )
        self.assertIn("补充一个不同内容", plan["prompt_hint"])
        self.assertTrue(plan["response_contract"]["duplicate_is_not_required"])

    def test_progress_nudge_hands_completed_world_round_to_character_creation(
        self,
    ) -> None:
        self._make_adventure_ready()
        manager = self.runtime.app.session_zero_manager
        manager.state.world.hero_drafts.clear()
        manager.state.world.selected_first_act_summary = ""

        plan = manager.session_zero_progress_nudge_plan(
            last_player_speaker="白河",
        )

        self.assertEqual(plan["status"], "targeted")
        self.assertEqual(plan["stage"], "character_creation")
        self.assertEqual(plan["player"], "南星")
        self.assertEqual(plan["topic"], "hero_creation:hero_concept")
        self.assertEqual(plan["missing_fields"], ["完整角色草稿"])
        self.assertIn("角色画面", plan["prompt_hint"])
        self.assertNotIn("名字、身份、主题", plan["prompt_hint"])

    def test_character_nudge_uses_saved_class_preferences_for_level_split(
        self,
    ) -> None:
        self._make_adventure_ready()
        manager = self.runtime.app.session_zero_manager
        manager.state.world.selected_first_act_summary = "从旧诊所的熄灯夜开始。"
        manager.state.world.hero_drafts["南星"] = HeroDraft(
            player_name="南星",
            hero_name="赛璃",
            identity="流动诊所的御魂医师",
            theme="慈悲",
            origin="暮钟国",
            class_preferences=["御魂使", "旅人"],
        )

        plan = manager.session_zero_progress_nudge_plan(
            prior_target_counts={"白河": 10},
        )

        self.assertEqual(plan["player"], "南星")
        self.assertEqual(plan["topic"], "hero_creation:hero_classes")
        self.assertIn("御魂使、旅人", plan["prompt_hint"])
        self.assertIn("怎样分配", plan["prompt_hint"])
        self.assertEqual(plan["allowed_values"], [])
        self.assertEqual(plan["allowed_value_count"], 15)
        self.assertEqual(
            plan["catalog_query"],
            {"kind": "class", "view": "shortlist", "limit": 3},
        )
        self.assertIn("身份", plan["authority_note"])

    def test_progress_nudge_asks_one_shared_setup_question_before_heroes(
        self,
    ) -> None:
        self._make_adventure_ready()
        manager = self.runtime.app.session_zero_manager
        manager.state.world.group_concept = ""

        plan = manager.session_zero_progress_nudge_plan()

        self.assertEqual(plan["status"], "shared_setup_pending")
        self.assertEqual(plan["target_scope"], "table")
        self.assertEqual(plan["topic"], "group_concept")
        self.assertIn("为什么会一起行动", plan["prompt_hint"])

    def test_progress_nudge_reaches_first_act_only_after_all_heroes_are_ready(
        self,
    ) -> None:
        self._make_adventure_ready()
        manager = self.runtime.app.session_zero_manager
        manager.state.world.selected_first_act_summary = ""

        plan = manager.session_zero_progress_nudge_plan()

        self.assertEqual(plan["status"], "first_act_pending")
        self.assertEqual(plan["stage"], "first_act")
        self.assertEqual(plan["target_scope"], "table")
        self.assertIn("另一名玩家", plan["prompt_hint"])

        manager.state.world.selected_first_act_summary = "从潮汐升降桥的断裂开始。"
        complete = manager.session_zero_progress_nudge_plan()
        self.assertEqual(complete["status"], "contribution_round_complete")

    def test_complete_but_unconfirmed_draft_is_nudged_before_first_act(self) -> None:
        self._make_adventure_ready()
        manager = self.runtime.app.session_zero_manager
        manager.state.world.selected_first_act_summary = ""
        manager.state.world.hero_drafts["白河"].confirmed = False

        plan = manager.session_zero_progress_nudge_plan()
        status = manager.hero_creation_status()

        self.assertFalse(manager.progress_summary()["heroes"])
        self.assertFalse(status["ready"])
        self.assertEqual(
            status["missing_by_player"]["洛岚"],
            ["确认角色并正式建卡"],
        )
        self.assertEqual(plan["status"], "targeted")
        self.assertEqual(plan["stage"], "character_creation")
        self.assertEqual(plan["player"], "白河")
        self.assertEqual(plan["topic"], "hero_creation:hero_confirmation")
        self.assertIn("正式建卡", plan["prompt_hint"])

    def test_skill_choice_requirement_blocks_false_complete_nudge(self) -> None:
        self._make_adventure_ready()
        manager = self.runtime.app.session_zero_manager
        draft = manager.state.world.hero_drafts["白河"]
        draft.confirmed = False
        draft.skill_options.clear()

        status = manager.hero_creation_status()
        plan = manager.session_zero_progress_nudge_plan(
            preferred_player="白河",
        )

        self.assertFalse(status["ready"])
        self.assertEqual(
            status["missing_by_player"]["洛岚"],
            ["技能附带选择"],
        )
        requirement = status["choice_requirements_by_player"]["洛岚"][0]
        self.assertEqual(requirement["skill_name"], "便携装置")
        self.assertEqual(plan["topic"], "hero_creation:hero_skill_options")
        self.assertEqual(plan["choice_requirement"]["skill_name"], "便携装置")
        self.assertEqual(
            plan["allowed_values"],
            ["炼金装置", "注魔装置", "魔导装置"],
        )
        self.assertNotIn("草稿已经齐", plan["prompt_hint"])

    def test_granted_spell_nudge_uses_skill_metadata(self) -> None:
        self._make_adventure_ready()
        manager = self.runtime.app.session_zero_manager
        draft = manager.state.world.hero_drafts["白河"]
        draft.confirmed = False
        draft.classes = {"元素使": 2, "武器大师": 3}
        draft.skills = {
            "元素魔法": 2,
            "碎骨": 1,
            "破防打击": 1,
            "反击": 1,
        }
        draft.skill_options.clear()
        draft.spells = ["炎弹"]

        plan = manager.session_zero_progress_nudge_plan(
            preferred_player="白河",
        )

        self.assertEqual(plan["topic"], "hero_creation:hero_spells")
        requirement = plan["choice_requirement"]
        self.assertEqual(requirement["skill_name"], "元素魔法")
        self.assertEqual(requirement["missing_count"], 1)
        self.assertEqual(plan["allowed_values"], [])
        self.assertEqual(plan["allowed_value_count"], 13)
        self.assertEqual(
            plan["catalog_query"],
            {
                "kind": "spell",
                "school": "元素使法术",
                "view": "shortlist",
                "limit": 3,
            },
        )
        self.assertIn("措辞由GM自然组织", plan["authority_note"])

    def test_threat_nudge_asks_about_the_world_without_character_or_country_assumptions(
        self,
    ) -> None:
        manager = self.runtime.app.session_zero_manager
        active = manager.find_participant("白河")
        active.answered_topics.extend(
            [
                "kingdom_contributions",
                "historical_event_contributions",
                "mystery_contributions",
                "threat_contributions",
            ]
        )
        quiet = manager.find_participant("南星")
        quiet.answered_topics.extend(
            [
                "kingdom_contributions",
                "historical_event_contributions",
                "mystery_contributions",
            ]
        )

        plan = manager.session_zero_nudge_plan(last_player_speaker="白河")

        self.assertEqual(plan["player"], "南星")
        self.assertEqual(plan["topic"], "threat_contributions")
        self.assertIn("这个世界现在正面临哪些威胁", plan["prompt_hint"])
        self.assertNotIn("国家未来", plan["prompt_hint"])
        self.assertNotIn("角色视角", plan["prompt_hint"].split("；")[0])

    def test_readiness_query_returns_all_blockers_without_materializing_characters(
        self,
    ) -> None:
        manager = self.runtime.app.session_zero_manager
        world = manager.state.world
        world.world_shape = "普通大陆"
        world.map_card = "自定义地图"
        world.magic_tech_role = "魔法与科技彼此对立。"
        world.kingdoms = {"索朗帝国": "旧蒸汽帝国。"}
        world.kingdom_contributors = {
            "白河": ["索朗帝国"],
            "南星": ["本项跳过"],
        }
        world.historical_events = ["两百年前的机械战争。"]
        world.historical_event_contributors = {
            "白河": ["机械战争"],
            "南星": ["本项跳过"],
        }
        world.mysteries = ["重叠日。"]
        world.mystery_contributors = {
            "白河": ["重叠日"],
            "南星": ["本项跳过"],
        }
        world.world_threats = ["失控的钢铁生命正在扩散。"]
        world.threat_contributors = {
            "白河": ["钢铁生命扩散"],
            "南星": ["本项跳过"],
        }
        world.group_concept = "调查重叠日的同行者"
        world.safety_lines = ["不出现性暴力"]
        for key, player, hero in (
            ("白河", "白河", "洛岚"),
            ("南星", "南星", "赛璃"),
        ):
            world.hero_drafts[key] = HeroDraft(
                player_name=player,
                hero_name=hero,
                identity="出逃的魔导工匠",
                theme="希望",
                origin="第七采掘城",
                classes={"造物使": 3, "武器大师": 2},
                attributes={"敏捷": 8, "洞察": 10, "力量": 8, "意志": 6},
                skills={
                    "便携装置": 1,
                    "秘密配方": 1,
                    "先见之明": 1,
                    "碎骨": 1,
                    "破防打击": 1,
                },
                skill_options={"便携装置": ["魔导装置"]},
                equipment=["铁锤", "旅行装束"],
                confirmed=True,
            )
        manager.world_state.apply_world_profile(world)

        receipt = self.service.gm_session_zero_tools.get_session_zero_readiness(
            context("还缺什么内容才能开启第一章？"),
            {},
        )

        self.assertTrue(receipt.ok)
        self.assertFalse(receipt.state_changed)
        self.assertFalse(receipt.result["ready"])
        self.assertEqual(
            receipt.result["session_zero"]["missing_world_fields"],
            ["第一幕开端"],
        )
        self.assertEqual(
            receipt.result["hero_creation"]["missing_by_player"],
            {},
        )
        self.assertFalse(self.runtime.app.character_manager.exists("洛岚"))
        self.assertFalse(self.runtime.app.character_manager.exists("赛璃"))
        self.assertIn("第一幕开端", receipt.public_fallback_reply)
        self.assertNotIn("当前角色草稿", receipt.public_fallback_reply)

        world.selected_first_act_summary = "从机械聚落调查重叠日的第一夜。"
        ready_receipt = (
            self.service.gm_session_zero_tools.get_session_zero_readiness(
                context("现在还缺什么？"),
                {},
            )
        )
        self.assertTrue(ready_receipt.result["ready"])
        self.assertIn("内容已经齐了", ready_receipt.public_fallback_reply)

    def test_player_can_disable_future_session_zero_nudges_semantically(self) -> None:
        message = "第零章缺什么我会自己补，以后别主动点我问了。"

        receipt = self.service.gm_session_zero_tools.set_nudge_preference(
            context(message),
            {
                "enabled": False,
                "evidence": "以后别主动点我问了",
            },
        )

        self.assertTrue(receipt.ok)
        participant = self.runtime.app.session_zero_manager.find_participant("白河")
        self.assertFalse(participant.proactive_questions_enabled)
        plan = self.runtime.app.session_zero_manager.session_zero_nudge_plan(
            last_player_speaker="南星",
        )
        self.assertEqual(plan["player"], "南星")

    def test_player_can_temporarily_pause_session_zero_nudges(self) -> None:
        message = "让我想想第一幕从哪里开始。"

        receipt = self.service.gm_session_zero_tools.pause_nudges(
            context(message),
            {
                "topic": "第一幕开端",
                "evidence": "让我想想第一幕从哪里开始。",
            },
        )

        self.assertTrue(receipt.ok)
        self.assertEqual(
            receipt.result["resume_condition"],
            "setup_progress_or_explicit_resume",
        )
        handoff = receipt.result["same_turn_handoff"]
        self.assertEqual(handoff["status"], "shared_setup_pending")
        self.assertEqual(handoff["topic"], "tone")
        self.assertEqual(handoff["player"], "南星")
        self.assertFalse(handoff["verbalize_skip_permission"])
        self.assertTrue(
            handoff["response_contract"]["ask_next_player_now"]
        )
        self.assertFalse(
            handoff["response_contract"]["wait_for_heartbeat"]
        )
        self.assertIn("南星", receipt.public_fallback_reply)
        manager = self.runtime.app.session_zero_manager
        self.assertEqual(manager.state.proactive_pause["player"], "白河")
        plan = manager.session_zero_nudge_plan(last_player_speaker="白河")
        self.assertEqual(plan["status"], "player_requested_time")
        self.assertEqual(plan["topic"], "第一幕开端")

    def test_consecutive_thinking_players_handoff_without_cycling_back(self) -> None:
        manager = self.runtime.app.session_zero_manager
        manager.ensure_participants(["洛岚"])

        first = self.service.gm_session_zero_tools.pause_nudges(
            context("我还没想好。", speaker="白河"),
            {"topic": "国家贡献", "evidence": "我还没想好。"},
        )
        second = self.service.gm_session_zero_tools.pause_nudges(
            context("我也要想想。", speaker="南星"),
            {"topic": "国家贡献", "evidence": "我也要想想。"},
        )

        self.assertEqual(first.result["same_turn_handoff"]["player"], "南星")
        self.assertEqual(second.result["same_turn_handoff"]["player"], "洛岚")
        self.assertEqual(
            {
                entry["player"]
                for entry in manager.proactive_pause_entries()
            },
            {"白河", "南星"},
        )

    def test_thinking_player_without_another_target_only_gets_acknowledged(self) -> None:
        with tempfile.TemporaryDirectory() as data_root:
            service = FUGMHttpService(data_root=data_root, use_llm=False)
            runtime = service._runtime("单人暂缓")
            runtime.app.initialize_session_zero(participants=["白河"])
            single_context = GMToolExecutionContext(
                campaign_id="单人暂缓",
                session_id="s0",
                channel_id="private-1",
                speaker="白河",
                gate_status="session_zero",
                is_private=True,
                directly_addressed=True,
                metadata={"current_message": "我还没想好。"},
            )

            receipt = service.gm_session_zero_tools.pause_nudges(
                single_context,
                {"topic": "角色主题", "evidence": "我还没想好。"},
            )

            self.assertEqual(receipt.result["same_turn_handoff"], {})
            self.assertEqual(receipt.public_fallback_reply, "没事，先放着。")

    def test_meaningful_setup_progress_resumes_temporarily_paused_nudges(self) -> None:
        manager = self.runtime.app.session_zero_manager
        manager.pause_proactive_nudges(
            "白河",
            topic="第一幕开端",
            evidence="让我想想。",
        )

        changed = manager.resume_proactive_nudges_after_setup_progress()

        self.assertTrue(changed)
        self.assertEqual(manager.state.proactive_pause, {})
        self.assertEqual(
            manager.session_zero_nudge_plan(last_player_speaker="白河")["status"],
            "targeted",
        )

    def test_explicitly_enabling_nudges_clears_temporary_pause(self) -> None:
        manager = self.runtime.app.session_zero_manager
        manager.pause_proactive_nudges(
            "白河",
            topic="第一幕开端",
            evidence="让我想想。",
        )

        receipt = self.service.gm_session_zero_tools.set_nudge_preference(
            context("可以继续问我了。"),
            {
                "enabled": True,
                "evidence": "可以继续问我了。",
            },
        )

        self.assertTrue(receipt.ok)
        self.assertTrue(receipt.state_changed)
        self.assertEqual(manager.state.proactive_pause, {})

    def test_world_contribution_receipt_names_each_recorded_category(self) -> None:
        message = (
            "宁姆格福大陆上科技与魔法是对立的。两百年前索朗帝国与自然联邦"
            "爆发战争，最终禁忌仪式让藤蔓在机械巨兽的齿轮中生长。"
        )

        receipt = self.service.gm_session_zero_tools.commit_update(
            context(message),
            {
                "updates": {
                    "magic_tech_role": "科技与魔法彼此对立。",
                    "historical_events": [
                        "两百年前，索朗帝国与自然联邦爆发战争；禁忌仪式使藤蔓在机械巨兽的齿轮中生长。"
                    ],
                },
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok)
        self.assertEqual(
            receipt.result["recorded_categories"],
            ["魔法与科技的关系", "重大历史事件"],
        )
        self.assertEqual(
            receipt.public_fallback_reply,
            "好，记下了。",
        )
        world = self.runtime.app.world_state.world_profile
        self.assertEqual(world.magic_tech_role, "科技与魔法彼此对立。")
        self.assertIn("禁忌仪式", world.historical_events[-1])

    def test_villain_seed_has_a_distinct_recorded_category(self) -> None:
        message = "监察官艾蕾娜相信集中管理记忆能阻止世界再次遗忘灾难。"

        receipt = self.service.gm_session_zero_tools.commit_update(
            context(message),
            {
                "updates": {"villain_seeds": [message]},
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok)
        self.assertEqual(receipt.result["recorded_categories"], ["反派种子"])
        self.assertIn(
            "监察官艾蕾娜",
            self.runtime.app.world_state.world_profile.villain_seeds[-1],
        )

    def test_agent_commit_uses_typed_session_zero_tool_once(self) -> None:
        message = "我贡献一个国家：钟鸣公国，以钟塔与风铃航路闻名。"
        self.service.session_gates.activate(
            "第零章工具团",
            "group-1",
            "s0",
            status="session_zero",
        )

        self.service.gm_tool_agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    {
                        "decision": "call_tool",
                        "tool_name": "create_world_setting",
                        "arguments": {
                            "category": "kingdoms",
                            "name": "钟鸣公国",
                            "value": "以钟塔与风铃航路闻名",
                            "visibility": "public",
                            "authority": "player_confirmed",
                            "reason": "玩家明确贡献国家。",
                        },
                        "reply": "",
                        "reason": "玩家明确贡献国家。",
                    },
                    {
                        "decision": "final",
                        "tool_name": "",
                        "arguments": {},
                        "reply": "钟鸣公国记下了。",
                        "reason": "工具提交成功。",
                    },
                ]
            ),
            model="fake",
            registry=self.service.gm_tool_registry,
        )

        status, response = self.service.handle(
            "POST",
            "/v1/message/route",
            {
                "campaign_id": "第零章工具团",
                "session_id": "s0",
                "channel_id": "group-1",
                "speaker": "白河",
                "message_id": "session-zero-country-1",
                "message": message,
                "is_at_bot": True,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(response["route"], "gm_agent_tool")
        self.assertEqual(response["reply"], "钟鸣公国记下了。")
        self.assertIn("钟鸣公国", self.runtime.app.world_state.world_profile.kingdoms)

    def test_blank_solo_world_contribution_starts_session_zero_then_commits(self) -> None:
        campaign_id = "空白单人共创团"
        message = (
            "我想创建一个像火锅一样的大陆，左半边以森林为主，"
            "右半边以沙漠为主，中间有座大山间隔"
        )
        self.service.gm_tool_agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    {
                        "decision": "call_tools",
                        "message_kind": "state_contribution",
                        "audience": "gm",
                        "calls": [
                            {
                                "tool_name": "start_session",
                                "arguments": {
                                    "phase": "session_zero",
                                    "reason": "玩家在新建单人档中直接开始世界共创。",
                                },
                            },
                            {
                                "tool_name": "create_world_setting",
                                "arguments": {
                                    "category": "world_shape",
                                    "value": "像火锅一样的锅形大陆",
                                    "visibility": "public",
                                    "authority": "player_confirmed",
                                    "reason": "玩家明确给出世界形状。",
                                },
                            },
                            {
                                "tool_name": "create_world_setting",
                                "arguments": {
                                    "category": "map_locations",
                                    "name": "西部森林",
                                    "value": "覆盖大陆左半边的森林地带。",
                                    "attributes": {"feature_type": "forest", "terrain": "forest", "position_hint": "west", "draw_icon": False},
                                    "visibility": "public",
                                    "authority": "player_confirmed",
                                    "reason": "玩家明确给出西部地形。",
                                },
                            },
                            {
                                "tool_name": "create_world_setting",
                                "arguments": {
                                    "category": "map_locations",
                                    "name": "东部沙漠",
                                    "value": "覆盖大陆右半边的沙漠地带。",
                                    "attributes": {"feature_type": "region", "terrain": "desert", "position_hint": "east", "draw_icon": False},
                                    "visibility": "public",
                                    "authority": "player_confirmed",
                                    "reason": "玩家明确给出东部地形。",
                                },
                            },
                            {
                                "tool_name": "create_world_setting",
                                "arguments": {
                                    "category": "map_locations",
                                    "name": "中央山脉",
                                    "value": "横隔森林与沙漠的中央大山。",
                                    "attributes": {"feature_type": "mountain_range", "terrain": "mountain", "position_hint": "center", "draw_icon": False},
                                    "visibility": "public",
                                    "authority": "player_confirmed",
                                    "reason": "玩家明确给出中央地形。",
                                },
                            },
                        ],
                        "reason": "先进入第零章，再原子记录本句完整世界贡献。",
                    },
                    {
                        "decision": "final",
                        "message_kind": "state_contribution",
                        "audience": "gm",
                        "reply": "好，这个世界的轮廓先立起来了。",
                        "reason": "设定已经权威写入。",
                    },
                ]
            ),
            model="fake",
            registry=self.service.gm_tool_registry,
        )

        status, response = self.service.handle(
            "POST",
            "/v1/message/route",
            {
                "campaign_id": campaign_id,
                "session_id": "solo",
                "channel_id": "private-1",
                "speaker": "白河",
                "message_id": "blank-solo-world-1",
                "message": message,
                "is_private": True,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(response["route"], "gm_agent_tool")
        tool_names = [item["tool_name"] for item in response["tool_receipts"]]
        committed_tool_names = [
            name for name in tool_names if name != "discover_capabilities"
        ]
        self.assertEqual(
            committed_tool_names,
            [
                "start_session",
                "create_world_setting",
                "create_world_setting",
                "create_world_setting",
                "create_world_setting",
            ],
        )
        self.assertFalse(
            GMToolAgentCapabilityPolicy._MAP_MUTATION_SCOPES & set(tool_names)
        )
        self.assertFalse(any(item.get("rolled_back") for item in response["tool_receipts"]))
        gate = self.service.session_gates.get(campaign_id, "private-1", "solo")
        self.assertEqual(gate.status, "session_zero")
        world = self.service._runtime(campaign_id).app.world_state.world_profile
        self.assertEqual(world.world_shape, "像火锅一样的锅形大陆")
        self.assertEqual(
            set(self.service._runtime(campaign_id).app.world_state.map_locations),
            {"西部森林", "东部沙漠", "中央山脉"},
        )

    def test_unaddressed_group_skill_choice_is_persisted_without_gm_echo(self) -> None:
        self.runtime.app.world_state.world_profile.hero_drafts["白河"] = HeroDraft(
            player_name="白河",
            hero_name="洛岚",
            classes={"造物使": 3, "旅人": 2},
        )
        self.service.session_gates.activate(
            "第零章工具团",
            "group-1",
            "s0",
            status="session_zero",
        )
        self.service.gm_tool_agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    {
                        "decision": "call_tool",
                        "message_kind": "state_contribution",
                        # Some capable models use ``gm`` here to mean that the
                        # state write belongs to GM tooling.  That is not proof
                        # that this unaddressed table statement needs an echo.
                        "audience": "gm",
                        "tool_name": "update_hero_draft",
                        "arguments": {
                            "subject": "洛岚",
                            "patch": {"skills": {"便携装置": 1}},
                        },
                        "terminal_decision": "silent",
                        "reply": "",
                        "reason": "玩家已在群里完整说出技能选择。",
                    }
                ]
            ),
            model="fake",
            registry=self.service.gm_tool_registry,
        )

        status, response = self.service.handle(
            "POST",
            "/v1/message/route",
            {
                "campaign_id": "第零章工具团",
                "session_id": "s0",
                "channel_id": "group-1",
                "speaker": "白河",
                "message_id": "session-zero-skill-silent-1",
                "message": "洛岚第一项技能选择便携装置。",
                "is_at_bot": False,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(response["target"], "silent")
        self.assertFalse(response["send_reply"])
        self.assertEqual(response["reply"], "")
        self.assertEqual(
            self.runtime.app.world_state.world_profile.hero_drafts["白河"].skills,
            {"便携装置": 1},
        )
        receipt = response["tool_receipts"][-1]
        self.assertTrue(receipt["result"]["silent_commit_allowed"])
        self.assertFalse(receipt["lock_public_reply"])

    def test_agent_invites_chapter_one_after_final_setup_commit(self) -> None:
        self._make_adventure_ready()
        manager = self.runtime.app.session_zero_manager
        manager.state.world.selected_first_act_summary = ""
        manager.refresh_stage_from_state()
        message = "第一幕就从卡里巴村监狱越狱开始，其他先不补了。"
        self.service.session_gates.activate(
            "第零章工具团",
            "group-1",
            "s0",
            status="session_zero",
        )
        self.service.gm_tool_agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    {
                        "decision": "call_tool",
                        "tool_name": "select_first_act",
                        "arguments": {
                            "custom_summary": (
                                "诺艾尔与艾丽妮从卡里巴村监狱越狱。"
                            ),
                        },
                        "reason": "玩家确定自定义第一幕。",
                    },
                    {
                        "decision": "call_tool",
                        "tool_name": "set_chapter_one_transition",
                        "arguments": {"posture": "invited"},
                        "reason": "准备已齐且玩家没有继续补充的意图。",
                    },
                    {
                        "decision": "final",
                        "reply": "越狱开场定下了。现在进入第一章吗？",
                        "reason": "确认开场并发出一次开章邀请。",
                    },
                ]
            ),
            model="fake",
            registry=self.service.gm_tool_registry,
        )

        status, response = self.service.handle(
            "POST",
            "/v1/message/route",
            {
                "campaign_id": "第零章工具团",
                "session_id": "s0",
                "channel_id": "group-1",
                "speaker": "白河",
                "message_id": "session-zero-first-act-1",
                "message": message,
                "is_at_bot": True,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(response["reply"], "越狱开场定下了。现在进入第一章吗？")
        self.assertEqual(
            [
                item["tool_name"]
                for item in response["tool_receipts"]
                if item["tool_name"] != "discover_capabilities"
            ],
            ["select_first_act", "set_chapter_one_transition"],
        )
        self.assertTrue(
            self.service._adventure_readiness_snapshot(
                self.runtime,
                materialize_confirmed_characters=False,
            )["ready"]
        )
        self.assertEqual(
            manager.chapter_one_transition_status(ready=True)["status"],
            "invited",
        )

    def test_agent_pauses_nudges_until_actual_setup_progress(self) -> None:
        self.service.session_gates.activate(
            "第零章工具团",
            "group-1",
            "s0",
            status="session_zero",
        )
        self.service.gm_tool_agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    {
                        "decision": "call_tool",
                        "tool_name": "pause_session_zero_nudges",
                        "arguments": {
                            "topic": "第一幕开端",
                        },
                        "reason": "玩家明确需要时间考虑当前问题。",
                    },
                    {
                        "decision": "final",
                        "reply": "没事，先放着。南星，你想先从哪个国家说起？",
                        "reason": "本轮直接把问题交给下一位玩家。",
                    },
                    {
                        "decision": "call_tool",
                        "tool_name": "select_first_act",
                        "arguments": {
                            "custom_summary": "从战争遗址开始第一幕。"
                        },
                        "reason": "玩家已经明确决定第一幕开端。",
                    },
                    {
                        "decision": "final",
                        "reply": "好，第一幕从战争遗址开始。",
                        "reason": "第一幕开端已经记下。",
                    },
                ]
            ),
            model="fake",
            registry=self.service.gm_tool_registry,
        )

        status, paused = self.service.handle(
            "POST",
            "/v1/message/route",
            {
                "campaign_id": "第零章工具团",
                "session_id": "s0",
                "channel_id": "group-1",
                "speaker": "白河",
                "message_id": "session-zero-pause-1",
                "message": "让我想想",
                "is_at_bot": False,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            paused["reply"],
            "没事，先放着。南星，你想先从哪个国家说起？",
        )
        pause_receipt = next(
            item
            for item in paused["tool_receipts"]
            if item["tool_name"] == "pause_session_zero_nudges"
        )
        self.assertEqual(
            pause_receipt["result"]["same_turn_handoff"]["player"],
            "南星",
        )
        self.assertEqual(
            self.runtime.app.session_zero_manager.state.proactive_pause["topic"],
            "第一幕开端",
        )

        status, resumed = self.service.handle(
            "POST",
            "/v1/message/route",
            {
                "campaign_id": "第零章工具团",
                "session_id": "s0",
                "channel_id": "group-1",
                "speaker": "白河",
                "message_id": "session-zero-resume-1",
                "message": "那就从战争遗址开始吧",
                "is_at_bot": False,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(resumed["reply"], "")
        self.assertEqual(resumed["target"], "silent")
        self.assertEqual(
            self.runtime.app.session_zero_manager.state.proactive_pause,
            {},
        )

    def test_agent_readiness_query_does_not_fall_back_to_hero_drafts(self) -> None:
        message = "还缺什么内容才能开启第一章？"
        self.service.session_gates.activate(
            "第零章工具团",
            "group-1",
            "s0",
            status="session_zero",
        )
        self.service.gm_tool_agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    {
                        "decision": "call_tool",
                        "tool_name": "get_session_zero_readiness",
                        "arguments": {},
                        "reason": "玩家查询第一章开启条件。",
                    },
                    {
                        "decision": "final",
                        "reply": "不应采用这句模型自由概括。",
                        "reason": "读取完成。",
                    },
                ]
            ),
            model="fake",
            registry=self.service.gm_tool_registry,
        )

        status, response = self.service.handle(
            "POST",
            "/v1/message/route",
            {
                "campaign_id": "第零章工具团",
                "session_id": "s0",
                "channel_id": "group-1",
                "speaker": "白河",
                "message_id": "session-zero-readiness-1",
                "message": message,
                "is_at_bot": True,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            [
                item["tool_name"]
                for item in response["tool_receipts"]
                if item["tool_name"] != "discover_capabilities"
            ],
            ["get_session_zero_readiness"],
        )
        self.assertIn("第零章还差这些", response["reply"])
        self.assertNotIn("当前角色草稿", response["reply"])
        self.assertNotIn("不应采用", response["reply"])

    def test_private_solo_delegation_fills_only_blanks_and_starts_first_scene(self) -> None:
        with tempfile.TemporaryDirectory() as data_root:
            service = FUGMHttpService(data_root=data_root, use_llm=False)
            runtime = service._runtime("单人火锅大陆")
            runtime.app.initialize_session_zero(participants=["测试玩家甲"])
            manager = runtime.app.session_zero_manager
            manager.apply_world_updates(
                {
                    "continent_name": "火锅大陆",
                    "world_shape": "像火锅一样，西半森林、东半沙漠，中央山脉分隔两地。",
                    "map_card": "自定义地图",
                    "safety_lines": ["不出现伤害儿童的情节"],
                    "map_locations": [
                        {
                            "name": "西部森林",
                            "description": "覆盖大陆西半侧的古老森林。",
                            "feature_type": "forest",
                            "terrain": "森林",
                            "position_hint": "west",
                        },
                        {
                            "name": "东部沙漠",
                            "description": "覆盖大陆东半侧的赤色沙漠。",
                            "feature_type": "region",
                            "terrain": "沙漠",
                            "position_hint": "east",
                        },
                        {
                            "name": "中央大山",
                            "description": "自北向南分隔两侧气候的山脉。",
                            "feature_type": "mountain_range",
                            "terrain": "高山",
                            "position_hint": "center",
                        },
                    ],
                }
            )
            message = "还有什么内容你自己发挥一下想象力补充一下吧~下一步我们要直接开始第一章"
            with patch.object(
                runtime.app,
                "ensure_world_map_for_adventure",
                return_value={"status": "generated", "output_path": "/tmp/fake-map.png"},
            ):
                receipt = service.gm_session_zero_tools.prepare_solo_adventure(
                    GMToolExecutionContext(
                        campaign_id="单人火锅大陆",
                        session_id="solo",
                        channel_id="private:100000001",
                        speaker="测试玩家甲",
                        gate_status="session_zero",
                        is_private=True,
                        directly_addressed=True,
                        metadata={"current_message": message},
                    ),
                    {
                        "creative_direction": "沿用火锅大陆，补足空白并准备第一幕。",
                        "start_adventure": True,
                        "evidence": message,
                    },
                )

            self.assertTrue(receipt.ok, receipt.to_dict())
            self.assertFalse(receipt.result["map_generation_deferred"])
            self.assertTrue(receipt.result["required_followup_resolved"])
            self.assertEqual(manager.state.world.continent_name, "火锅大陆")
            self.assertIn("西半森林", manager.state.world.world_shape)
            self.assertEqual(
                manager.state.world.safety_lines,
                ["不出现伤害儿童的情节"],
            )
            self.assertEqual(manager.state.world.safety_veils, [])
            self.assertTrue(manager.state.world.hero_drafts["测试玩家甲"].confirmed)
            self.assertTrue(
                service._adventure_readiness_snapshot(
                    runtime,
                    materialize_confirmed_characters=False,
                )["ready"]
            )
            self.assertEqual(
                receipt.result["adventure"]["world_map"]["status"],
                "generated",
            )
            self.assertEqual(
                service.session_gates.get(
                    "单人火锅大陆",
                    "private:100000001",
                    "solo",
                ).status,
                "adventure",
            )
            self.assertIsNotNone(runtime.app.scene_manager.current_scene)
            self.assertIn("岚辛", runtime.app.scene_manager.current_scene.participants)

    def test_private_solo_delegation_without_start_keeps_map_unrendered(self) -> None:
        with tempfile.TemporaryDirectory() as data_root:
            service = FUGMHttpService(data_root=data_root, use_llm=False)
            runtime = service._runtime("单人先补设定")
            runtime.app.initialize_session_zero(participants=["白河"])
            message = "剩下的内容你帮我补齐，不过先别开始第一章。"
            receipt = service.gm_session_zero_tools.prepare_solo_adventure(
                GMToolExecutionContext(
                    campaign_id="单人先补设定",
                    session_id="solo",
                    channel_id="private:white-river",
                    speaker="白河",
                    gate_status="session_zero",
                    is_private=True,
                    directly_addressed=True,
                    metadata={"current_message": message},
                ),
                {
                    "creative_direction": "补足尚未确定的第零章内容。",
                    "start_adventure": False,
                    "evidence": message,
                },
            )

            self.assertTrue(receipt.ok, receipt.to_dict())
            self.assertTrue(receipt.result["map_generation_deferred"])
            self.assertEqual(
                service.session_gates.get(
                    "单人先补设定",
                    "private:white-river",
                    "solo",
                ).status,
                "inactive",
            )
            self.assertEqual(
                runtime.app.world_map_generation_status().get("status"),
                "idle",
            )
            self.assertEqual(
                runtime.app.scene_manager.current_scene.scene_type.value,
                "session_zero",
            )

    def test_solo_completion_reuses_one_creative_call_for_first_scene(self) -> None:
        with tempfile.TemporaryDirectory() as data_root:
            service = FUGMHttpService(data_root=data_root, use_llm=False)
            runtime = service._runtime("单人单次创作")
            runtime.app.initialize_session_zero(participants=["测试玩家甲"])
            creative = CreativeOnceClient(
                {
                    "continent_name": "火锅大陆",
                    "world_shape": "西部森林与东部沙漠被中央山脉分隔的圆形大陆",
                    "magic_tech_role": "地脉魔法驱动城镇的晶炉与升降索道。",
                    "kingdom": {
                        "name": "沸脊同盟",
                        "description": "控制中央山口与地热工坊的城镇联盟。",
                    },
                    "historical_event": "旧地脉战争烧毁了贯通东西的山腹隧道。",
                    "mystery": "山脉深处每逢无月之夜都会传来第二次心跳。",
                    "world_threat": "失控地脉正在让森林与沙漠同时向山口扩张。",
                    "group_concept": "追查地脉异变的独行英雄",
                    "starting_region": "椒林关",
                    "first_act_summary": "椒林关突发地脉震动，英雄必须救人并找出震源。",
                    "tone_preference": "明快而危险的英雄冒险",
                    "description_style": "具体克制的JRPG式描写",
                    "supplemental_locations": [
                        {
                            "name": "椒林关",
                            "description": "西部林缘的温泉关镇。",
                            "feature_type": "settlement",
                            "terrain": "森林",
                            "position_hint": "west",
                        },
                        {
                            "name": "赤盐商路",
                            "description": "横穿东部荒漠的商道。",
                            "feature_type": "region",
                            "terrain": "沙漠",
                            "position_hint": "east",
                        },
                        {
                            "name": "沸星峰",
                            "description": "大陆中央最高的火山峰。",
                            "feature_type": "mountain_range",
                            "terrain": "高山",
                            "position_hint": "center",
                        },
                    ],
                    "hero": {
                        "name": "岚辛",
                        "identity": "追寻失落地脉歌谣的旅者",
                        "theme": "希望",
                        "origin": "椒林关",
                    },
                    "opening_scene": {
                        "scene_name": "椒林关的第二次心跳",
                        "location": "椒林关",
                        "objective": "救出被困居民，并找出地脉震动的源头。",
                        "private_situation": {
                            "premise": "椒林关在清晨发生异常地脉震动。",
                            "stakes": "被困居民与通往震源的痕迹都可能失去。",
                            "current_pressure": "温泉塔正在向集市倾斜。",
                            "dramatic_question": "英雄能否救人并保住追查震源的线索？",
                            "signature_image": "倒映在温泉水面的赤色山脉裂光",
                            "opposition_goal": "幕后力量想用余震掩埋通往山腹的刻痕。",
                            "dilemma": "先稳住温泉塔，或先抢救正在消失的刻痕。",
                            "closure_requirement": "居民脱险或震源线索被保住，并留下选择造成的结果。",
                            "irreversible_change": "温泉塔、居民关系或震源线索至少一项永久改变。",
                            "ending_echo": "结尾再次呈现水面的赤色裂光。",
                            "visible_elements": ["倾斜的温泉塔", "裂开的山道刻痕"],
                            "clue_pool": ["逆着震波生长的苔藓", "只在第二次震动后出现的刻痕"],
                            "secrets": ["震动由山腹中的旧晶炉人为唤醒。"],
                            "possible_reveals": ["震波来自山腹", "刻痕与旧地脉战争有关"],
                            "escalation_ladder": ["温泉塔继续倾斜", "余震开始覆盖刻痕"],
                            "possible_payoffs": ["救下居民", "保住通往山腹的路线"],
                        },
                        "public_opening": "椒林关的清晨被一声闷响劈开。温泉塔向集市缓缓倾斜，山道上的裂缝里正透出赤红微光。",
                        "player_handoff": "岚辛，你先做什么？",
                    },
                }
            )
            runtime.app.creative_client = creative
            runtime.app.creative_model = "fake-creative"
            runtime.app.scene_creative_writer.client = creative
            runtime.app.scene_creative_writer.model = "fake-creative"
            concretizer = (
                runtime.app.campaign_pacing_manager.contract_planner.concretizer
            )
            concretizer.client = creative
            concretizer.model = "fake-creative"
            concretizer.reachability_reviewer.client = creative
            concretizer.reachability_reviewer.model = "fake-creative"
            message = "剩下的你自由补充，然后直接开始第一章。"

            with patch.object(
                runtime.app,
                "ensure_world_map_for_adventure",
                return_value={"status": "generated", "output_path": "/tmp/fake-map.png"},
            ):
                receipt = service.gm_session_zero_tools.prepare_solo_adventure(
                    GMToolExecutionContext(
                        campaign_id="单人单次创作",
                        session_id="solo",
                        channel_id="private:100000001",
                        speaker="测试玩家甲",
                        gate_status="session_zero",
                        is_private=True,
                        directly_addressed=True,
                        metadata={"current_message": message},
                    ),
                    {
                        "creative_direction": "补齐空白并给出可以立即行动的第一幕。",
                        "start_adventure": True,
                        "evidence": message,
                    },
                )

            self.assertTrue(receipt.ok, receipt.to_dict())
            self.assertEqual(len(creative.calls), 1)
            self.assertEqual(
                creative.calls[0].get("operation"),
                "solo_session_zero_completion",
            )
            self.assertIn("温泉塔向集市缓缓倾斜", receipt.public_fallback_reply)
            self.assertEqual(
                receipt.result["adventure"]["creative_author"]["author"],
                "solo_session_zero_completer",
            )
            self.assertTrue(
                receipt.result["adventure"]["creative_author"][
                    "reused_prepared_packet"
                ]
            )
            self.assertEqual(
                runtime.app.scene_manager.current_scene.location,
                "椒林关",
            )

    def test_solo_completion_macro_is_retired_in_favor_of_world_crud(self) -> None:
        with tempfile.TemporaryDirectory() as data_root:
            service = FUGMHttpService(data_root=data_root, use_llm=False)
            schemas = {
                item["name"]: item
                for item in service.gm_tool_registry.schemas()
            }

        self.assertNotIn("prepare_solo_adventure", schemas)
        self.assertIn("query_world_settings", schemas)
        self.assertIn("create_world_setting", schemas)
        self.assertIn("update_world_setting", schemas)
        self.assertIn("delete_world_setting", schemas)
        self.assertIn("rename_world_setting", schemas)

    def test_private_solo_completion_adopts_legacy_anonymous_participant(self) -> None:
        with tempfile.TemporaryDirectory() as data_root:
            service = FUGMHttpService(data_root=data_root, use_llm=False)
            runtime = service._runtime("旧匿名单人档")
            runtime.app.initialize_session_zero(participants=["匿名玩家"])
            participant = runtime.app.session_zero_manager.state.participants[0]
            participant.contributions.extend(["大陆叫火锅大陆", "西林东漠，中间是山"])
            participant.answered_topics.append("kingdom_contributions")
            message = "剩下的你自由补充，之后直接开始第一章。"

            receipt = service.gm_session_zero_tools.prepare_solo_adventure(
                GMToolExecutionContext(
                    campaign_id="旧匿名单人档",
                    session_id="solo",
                    channel_id="private:100000001",
                    speaker="测试玩家甲",
                    gate_status="session_zero",
                    is_private=True,
                    directly_addressed=True,
                    metadata={"current_message": message},
                ),
                {
                    "creative_direction": "沿用既有设定补齐空白。",
                    "start_adventure": False,
                    "evidence": message,
                },
            )

            self.assertTrue(receipt.ok, receipt.to_dict())
            participants = runtime.app.session_zero_manager.state.participants
            self.assertEqual([item.name for item in participants], ["测试玩家甲"])
            self.assertIn("大陆叫火锅大陆", participants[0].contributions)
            self.assertIn("kingdom_contributions", participants[0].answered_topics)
            self.assertIn(
                "测试玩家甲",
                runtime.app.session_zero_manager.state.world.hero_drafts,
            )
            self.assertNotIn(
                "匿名玩家",
                runtime.app.session_zero_manager.state.world.hero_drafts,
            )

    def test_solo_delegation_rejects_a_multiplayer_session_zero(self) -> None:
        message = "其余内容由你补完，然后直接进入第一章。"
        before = asdict(self.runtime.app.session_zero_manager.state)
        receipt = self.service.gm_session_zero_tools.prepare_solo_adventure(
            context(message, speaker="白河", is_private=True),
            {
                "creative_direction": "补完剩余空白。",
                "start_adventure": True,
                "evidence": message,
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "CAMPAIGN_IS_NOT_SOLO")
        self.assertEqual(
            asdict(self.runtime.app.session_zero_manager.state),
            before,
        )


if __name__ == "__main__":
    unittest.main()
