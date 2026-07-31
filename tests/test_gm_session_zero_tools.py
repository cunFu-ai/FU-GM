from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch

from fu_gm.components.gm_supervisor import GMCapabilityBroker
from fu_gm.gm_tool_agent import GMToolExecutionContext, LLMGMToolAgent
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
                return json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "discover_capabilities",
                        "arguments": {
                            "domains": domains[:4],
                            "reason": "测试模型按协议取得所需能力。",
                        },
                    },
                    ensure_ascii=False,
                )
        return self.responses.pop(0)


def context(message: str, *, speaker: str = "白河") -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="第零章工具团",
        session_id="s0",
        channel_id="group-1",
        speaker=speaker,
        gate_status="session_zero",
        directly_addressed=True,
        metadata={"current_message": message},
    )


class GMSessionZeroToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.service = FUGMHttpService(data_root=self.tempdir.name, use_llm=False)
        self.runtime = self.service._runtime("第零章工具团")
        self.runtime.app.initialize_session_zero(participants=["白河", "南星"])

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_session_zero_tools_expose_nested_world_and_hero_shapes(self) -> None:
        schemas = {
            item["name"]: item
            for item in self.service.gm_tool_registry.schemas()
        }
        world_schema = schemas["commit_session_zero_update"]["parameters"][
            "properties"
        ]["updates"]
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
            "仍要另写historical_events",
            schemas["commit_session_zero_update"]["description"],
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
        self.assertIn("set_session_zero_nudge_preference", schemas)
        self.assertIn("pause_session_zero_nudges", schemas)
        self.assertIn("get_session_zero_readiness", schemas)
        self.assertIn(
            "不要用get_hero_drafts",
            schemas["get_session_zero_readiness"]["description"],
        )
        self.assertNotIn("semantic_profile", schemas["commit_session_zero_update"])
        self.assertNotIn("semantic_profile", schemas["record_safety_boundary"])
        self.assertFalse(world_schema["additionalProperties"])
        self.assertIn("直接要求GM暂存", proposal_description)
        self.assertIn("不要调用", proposal_description)
        self.assertEqual(
            set(hero_schema["properties"]["attributes"]["properties"]),
            {"敏捷", "洞察", "力量", "意志"},
        )
        self.assertFalse(hero_schema["additionalProperties"])

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

    def test_state_summary_exposes_committed_consensus_and_recent_public_support(self) -> None:
        world = self.runtime.app.world_state.world_profile
        world.group_concept = "临时守护者"
        world.starting_region = "白花碑驿站"
        world.playstyle_themes = ["用证据与承诺化解冲突"]
        world.continent_name = "白钟大陆"
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

    def test_cross_player_correction_uses_explicit_typed_subject(self) -> None:
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

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(
            self.runtime.app.world_state.world_profile.hero_drafts["南星"].hero_name,
            "赛璃",
        )

    def test_unknown_split_skill_name_is_rejected_without_dirty_write(self) -> None:
        self.runtime.app.world_state.world_profile.hero_drafts["白河"] = HeroDraft(
            player_name="白河",
            hero_name="苍祈",
            classes={"奥灵使": 2, "拟兽使": 2, "暗刃骑士": 1},
        )
        message = "苍祈奥灵使技能先选契约与召唤。"

        receipt = self.service.gm_session_zero_tools.update_hero_draft(
            context(message),
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
            context(message),
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
            context(message),
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
        self.assertIn("起始装备", receipt.result["missing_fields"])
        self.assertNotIn("痛楚", receipt.result["errors"])

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
            context(message),
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
        self.assertEqual(receipt.result["resume_condition"], "next_player_message")
        manager = self.runtime.app.session_zero_manager
        self.assertEqual(manager.state.proactive_pause["player"], "白河")
        plan = manager.session_zero_nudge_plan(last_player_speaker="白河")
        self.assertEqual(plan["status"], "player_requested_time")
        self.assertEqual(plan["topic"], "第一幕开端")

    def test_new_player_message_resumes_temporarily_paused_nudges(self) -> None:
        manager = self.runtime.app.session_zero_manager
        manager.pause_proactive_nudges(
            "白河",
            topic="第一幕开端",
            evidence="让我想想。",
        )

        changed = manager.resume_proactive_nudges_for_new_player_message()

        self.assertTrue(changed)
        self.assertEqual(manager.state.proactive_pause, {})
        self.assertEqual(
            manager.session_zero_nudge_plan(last_player_speaker="白河")["status"],
            "targeted",
        )

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
            "好，魔法与科技的关系和重大历史事件都记下了。",
        )
        world = self.runtime.app.world_state.world_profile
        self.assertEqual(world.magic_tech_role, "科技与魔法彼此对立。")
        self.assertIn("禁忌仪式", world.historical_events[-1])

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
                        "tool_name": "commit_session_zero_update",
                        "arguments": {
                            "updates": {"kingdoms": {"钟鸣公国": "以钟塔与风铃航路闻名"}},
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
                "message": message,
                "is_at_bot": True,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(response["route"], "gm_agent_tool")
        self.assertEqual(response["reply"], "钟鸣公国记下了。")
        self.assertIn("钟鸣公国", self.runtime.app.world_state.world_profile.kingdoms)

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
                        "reply": "好，你慢慢想。",
                        "reason": "临时等待已经记录。",
                    },
                    {
                        "decision": "call_tool",
                        "tool_name": "commit_session_zero_update",
                        "arguments": {
                            "updates": {
                                "selected_first_act_summary": "从战争遗址开始第一幕。"
                            }
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
                "message": "让我想想",
                "is_at_bot": False,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(paused["reply"], "好，你慢慢想。")
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
                "message": "那就从战争遗址开始吧",
                "is_at_bot": False,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(resumed["reply"], "好，第一幕从战争遗址开始。")
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


if __name__ == "__main__":
    unittest.main()
