import json
import unittest

from fu_gm.action_brain import HeuristicActionBrain
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.session_zero_manager import DEFAULT_EIGHT_PILLARS, SessionZeroManager
from fu_gm.components.world_state import WorldState
from fu_gm.config import LLMConfig
from fu_gm.expressor import Expressor
from fu_gm.interceptor import ActionInterceptor
from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.models import HeroDraft, SceneType, SecretLockLevel, SessionZeroResponse, SessionZeroStage
from fu_gm.prompts import SESSION_ZERO_SYSTEM_PROMPT
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.scene_orchestrator import SceneOrchestrator
from fu_gm.session_zero_facilitator import HeuristicSessionZeroFacilitator, LLMSessionZeroFacilitator


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post_json(self, url, headers, payload, timeout):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout": timeout,
            }
        )
        content = self.responses.pop(0)
        return {"choices": [{"message": {"content": content}}]}


class SessionZeroTests(unittest.TestCase):
    def _make_world_ready_state(self, participants=None):
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        participant_names = participants or ["阿凛"]
        state = manager.start(participants=participant_names)
        contributor_map = {name: ["已贡献"] for name in participant_names}
        manager.apply_world_updates(
            {
                "world_style": "科技奇幻",
                "map_card": "沿海大陆与近海群岛地图卡",
                "travel_day_length": "一天路程",
                "magic_tech_role": "魔法与齿轮科技并存，灵魂藤蔓让机械获得生命。",
                "group_concept": "调查钢铁生命的临时同路人",
                "starting_region": "齿藤边境",
                "major_locations": {"齿藤城": "机械齿轮与有意识藤蔓交织的边境城市。"},
                "kingdoms": {"托伦": "以秘宝贸易与旧贵族闻名的美丽城市国家。"},
                "kingdom_contributors": contributor_map,
                "historical_events": ["灵魂藤蔓首次让无意识钢铁诞生自我，改变了诸国关系。"],
                "historical_event_contributors": contributor_map,
                "factions": {"灵藤学会": "研究钢铁生命的学派，内部对新生命意见分裂。"},
                "villain_seeds": ["想把钢铁生命纳入统治秩序的执政者。"],
                "mysteries": ["灵魂藤蔓为何能让无意识钢铁诞生自我？"],
                "mystery_contributors": contributor_map,
                "world_threats": ["想把钢铁生命纳入统治秩序的执政者。"],
                "threat_contributors": contributor_map,
                "safety_lines": ["不描写血腥暴力。"],
            }
        )
        return world_state, manager, state

    def _complete_hero(self, player_name="阿凛", hero_name="露娜") -> HeroDraft:
        return HeroDraft(
            player_name=player_name,
            hero_name=hero_name,
            identity="失国公主",
            theme="正义",
            origin="水晶王国",
            classes={"元素使": 2, "守护者": 3},
            attributes={"DEX": 8, "INS": 10, "MIG": 6, "WLP": 8},
            skills={"元素魔法": 1, "元素系仪式": 1, "铁壁": 1, "保镖": 1, "挺身守护": 1},
            spells=["元素幕障"],
            equipment=["法杖", "青铜盾"],
        )

    def test_manager_starts_with_eight_pillars_and_writes_world_state(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)

        state = manager.start()

        self.assertTrue(state.active)
        self.assertEqual(state.stage, SessionZeroStage.TONE)
        self.assertEqual(len(state.world.pillars), len(DEFAULT_EIGHT_PILLARS))
        self.assertIn("危险中的世界", " ".join(world_state.session_pillars))
        self.assertIn("危险中的世界", state.world.pillars)

    def test_manager_records_structured_map_locations_without_name_guessing(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        manager.start()

        manager.apply_world_updates(
            {
                "map_locations": [
                    {
                        "name": "潮鸢群岛",
                        "description": "东南海域的飞翼船群岛。",
                        "feature_type": "archipelago",
                        "position_hint": "southeast",
                        "draw_icon": False,
                    },
                    {
                        "name": "钟鸣公国",
                        "description": "位于镜线内海北岸。",
                        "feature_type": "country",
                        "relative_to": "镜线内海",
                        "relative_position": "north",
                        "faction": "钟鸣公国",
                    },
                ]
            }
        )

        islands = world_state.map_locations["潮鸢群岛"]
        duchy = world_state.map_locations["钟鸣公国"]
        self.assertEqual(islands.feature_type, "archipelago")
        self.assertEqual(islands.position_hint, "southeast")
        self.assertFalse(islands.draw_icon)
        self.assertEqual(duchy.relative_to, "镜线内海")
        self.assertEqual(duchy.relative_position, "north")
        self.assertIn("潮鸢群岛", manager.state.world.major_locations)

    def test_manager_tracks_participants_and_rotates_spotlight(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)

        state = manager.start(participants=["阿凛", "白河", "阿凛", "  "])

        self.assertEqual([participant.name for participant in state.participants], ["阿凛", "白河"])
        self.assertEqual(manager.current_participant_name(), "阿凛")
        self.assertTrue(any("每位玩家" in topic for topic in manager.missing_topics()))

        manager.record_player_input("阿凛", "我想看永雨工业城的下层街垒。")

        snapshot = manager.snapshot()
        self.assertEqual(snapshot["current_participant"], "白河")
        self.assertEqual(snapshot["participants"][0]["answered_topics"], ["tone"])
        self.assertEqual(snapshot["participants"][0]["contributions"], ["我想看永雨工业城的下层街垒。"])

    def test_manager_applies_world_updates_to_world_state(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        manager.start()

        manager.apply_response(
            SessionZeroResponse(
                message="记录世界事实。",
                stage=SessionZeroStage.THREATS,
                world_updates={
                    "campaign_title": "永雨之下",
                    "group_concept": "反抗腐败强权的革命者小队",
                    "major_locations": {"永雨工业城": "上层偷走阳光，下层承受魔导烟雨。"},
                    "factions": {"辉钢财团": "垄断灵魂能源的企业贵族。"},
                },
            )
        )

        self.assertEqual(world_state.world_profile.campaign_title, "永雨之下")
        self.assertEqual(world_state.map_notes["永雨工业城"], "上层偷走阳光，下层承受魔导烟雨。")
        self.assertIn("垄断灵魂能源的企业贵族。", world_state.npc_relationships["辉钢财团"])
        self.assertTrue(any("反抗腐败强权" in memory for memory in world_state.memories))

    def test_heuristic_facilitator_has_personality_and_extracts_world_facts(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        state = manager.start()
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(
            state,
            "玩家",
            "我想要科技奇幻，主角是反抗财阀的革命者，从下层城市开始，有公司和污染，不要血腥细节。",
        )

        self.assertIn("玩家，安全边界记好了。", response.message)
        self.assertNotIn("隐藏宝箱味", response.message)
        self.assertEqual(response.world_updates["world_style"], "科技奇幻")
        self.assertEqual(response.world_updates["group_concept"], "反抗腐败强权的革命者小队")
        self.assertIn("永雨工业城", response.world_updates["major_locations"])
        self.assertIn("辉钢财团", response.world_updates["factions"])
        self.assertTrue(response.world_updates["villain_seeds"])
        self.assertIn("techno_pressure", response.world_updates["gm_inspiration_tags"])
        self.assertIn("企业星城", response.world_updates["gm_prepared_locations"])
        self.assertTrue(any("压迫" in note or "系统" in note for note in response.world_updates["gm_guidance_notes"]))
        self.assertNotIn("mysteries", response.world_updates)
        self.assertTrue(response.world_updates["safety_lines"])

    def test_heuristic_facilitator_accepts_freeform_world_tone_without_three_choice(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        state = manager.start()
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(
            state,
            "玩家",
            "我希望是个有地下城宝箱和奇遇的奇幻故事，大家像旅行英雄一样探索。",
        )

        self.assertEqual(response.world_updates["world_style"], "地下城奇遇幻想")
        self.assertNotIn(response.world_updates["world_style"], {"高度奇幻", "自然奇幻", "科技奇幻"})
        self.assertEqual(response.world_updates["group_concept"], "追寻遗失传说的旅行英雄团")
        self.assertEqual(response.world_updates["starting_region"], "星尘迷宫入口")
        self.assertIn("星尘迷宫", response.world_updates["major_locations"])
        self.assertNotIn("mysteries", response.world_updates)
        self.assertTrue(any("收藏家" in item for item in response.world_updates["villain_seeds"]))

    def test_heuristic_facilitator_lists_fixed_classes_when_asked_about_options(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        state = manager.start()
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(state, "玩家", "有什么职业可以选择？")

        self.assertIn("奥灵使", response.message)
        self.assertIn("武器大师", response.message)
        self.assertIn("起始角色通常为 5 级", response.message)
        self.assertEqual(response.stage, state.stage)

    def test_heuristic_facilitator_does_not_treat_short_skill_aliases_in_world_text_as_pc_skills(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        state = manager.start(participants=["白河"])
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(
            state,
            "白河",
            "反派种子：监察官艾蕾娜认为只有把记忆集中管理，世界才不会再遗忘灾难。",
        )

        self.assertNotIn("hero_drafts", response.world_updates)

    def test_explicit_skill_list_replaces_previous_polluted_skill_draft(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        state = manager.start(participants=["白河"])
        state.world.hero_drafts["白河"] = HeroDraft(
            player_name="白河",
            hero_name="洛岚",
            identity="魔导工匠",
            theme="赎罪",
            origin="第七采掘城",
            classes={"造物使": 3, "武器大师": 2},
            attributes={"DEX": 8, "INS": 10, "MIG": 8, "WLP": 6},
            skills={"集中心智": 1},
            equipment=["铁锤", "旅行装束"],
        )
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(
            state,
            "白河",
            "角色名洛岚。职业技能：便携装置1、秘密配方1、先见之明1、碎骨1、破防打击1。洛岚确认角色并正式建卡。",
        )
        manager.apply_response(response)

        draft = manager.state.world.hero_drafts["白河"]
        self.assertNotIn("集中心智", draft.skills)
        self.assertEqual(draft.skills["便携装置"], 1)
        self.assertEqual(draft.skills["破防打击"], 1)

    def test_heuristic_facilitator_polls_each_player_before_ready(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        state = manager.start(participants=["阿凛", "白河"])
        facilitator = HeuristicSessionZeroFacilitator()

        opening = facilitator.opening(state)
        manager.apply_response(opening)
        self.assertIn("阿凛", opening.message)
        self.assertEqual(manager.snapshot()["current_participant"], "阿凛")
        self.assertTrue(manager.snapshot()["participants"][0]["pending_question"])

        manager.record_player_input(
            "阿凛",
            "我想要科技奇幻，主角是反抗财阀的革命者，从下层城市开始，有公司和污染，不要血腥细节。",
        )
        response = facilitator.respond(
            manager.state,
            "阿凛",
            "我想要科技奇幻，主角是反抗财阀的革命者，从下层城市开始，有公司和污染，不要血腥细节。",
        )
        manager.apply_response(response)

        self.assertNotEqual(manager.state.stage, SessionZeroStage.READY)
        self.assertFalse(manager.state.world.completed)
        self.assertEqual(manager.snapshot()["current_participant"], "白河")
        self.assertNotIn("你来接这一笔", response.message)
        self.assertNotIn("谁有灵感就接", response.message)

        manager.record_player_input("白河", "帷幕：儿童遇险淡出处理，我想补一个云海空港。")
        second_response = facilitator.respond(manager.state, "白河", "帷幕：儿童遇险淡出处理。")
        manager.apply_response(second_response)

        self.assertTrue(manager.participant_polling_ready())
        manager.apply_world_updates(
            {
                "map_card": "大陆地图卡",
                "travel_day_length": "一天路程",
                "magic_tech_role": "工业化魔导技术与传统魔法并存。",
                "hero_drafts": {
                    "阿凛": self._complete_hero().__dict__,
                    "白河": self._complete_hero(player_name="白河", hero_name="白河角色").__dict__,
                },
                "kingdoms": {"永雨公国": "下层工业城市与上层财阀共同构成的国家。"},
                "kingdom_contributors": {"阿凛": ["永雨公国"], "白河": ["暂时跳过"]},
                "historical_events": ["财阀垄断灵魂能源后，永雨工业城分裂为上下层。"],
                "historical_event_contributors": {"阿凛": ["财阀垄断灵魂能源"], "白河": ["暂时跳过"]},
                "factions": {"辉钢财团": "垄断灵魂能源的企业贵族。"},
                "villain_seeds": ["辉钢财团的继承人把剥削包装成奇迹。"],
                "mysteries": ["被抽取的灵魂能源最终流向了哪里？"],
                "mystery_contributors": {"阿凛": ["灵魂能源流向"], "白河": ["暂时跳过"]},
                "world_threats": ["辉钢财团正在把灵魂能源垄断扩展到邻国。"],
                "threat_contributors": {"阿凛": ["辉钢财团扩张"], "白河": ["暂时跳过"]},
            }
        )
        if not manager.state.world.first_act_candidates:
            manager.generate_first_act_candidates()
        manager.record_first_act_vote("阿凛", "1")
        manager.record_first_act_vote("白河", "1")
        manager.confirm_first_act("1")
        self.assertTrue(manager.finish_if_ready())
        self.assertEqual(manager.state.stage, SessionZeroStage.READY)

    def test_world_creation_invites_each_player_and_does_not_turn_history_into_current_threat(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        state = manager.start(participants=["村夫", "loading"])
        manager.apply_world_updates(
            {
                "map_card": "大陆地图卡",
                "travel_day_length": "一天路程",
            }
        )
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(
            state,
            "村夫",
            "像一块完整的大陆，科技与魔法是对立的，大约两百年前一个很强盛蒸汽帝国索朗为了发展肆意砍伐森林，崇尚自然的王国组成联邦展开了一场跨越数年的战争，最后禁忌仪式让藤蔓在索朗帝国战争巨兽的齿轮中生长，世界也因此出现了让无意识的钢铁生出生命的藤蔓",
        )
        manager.apply_response(response)

        public_text = response.message + " ".join(response.questions) + " ".join(manager.state.world.open_questions)
        self.assertEqual(response.world_updates.get("map_card"), "大陆地图卡")
        self.assertTrue(response.world_updates.get("kingdoms"))
        self.assertTrue(response.world_updates.get("historical_events"))
        self.assertEqual(response.world_updates.get("mysteries", []), [])
        self.assertEqual(response.world_updates.get("world_threats", []), [])
        self.assertNotIn("覆盖了世界七步", public_text)
        self.assertNotIn("你来接这一笔", public_text)
        self.assertNotIn("谁有灵感就接", response.message)
        self.assertIn("奥秘", public_text)
        self.assertFalse(manager.state.world.mysteries)
        self.assertFalse(manager.state.world.world_threats)

    def test_mystery_is_recorded_only_when_step_six_is_active(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        state = manager.start(participants=["村夫"])
        manager.apply_world_updates(
            {
                "map_card": "大陆地图卡",
                "travel_day_length": "一天路程",
                "magic_tech_role": "科技与魔法对立。",
                "kingdoms": {"索朗帝国": "曾经强盛的蒸汽帝国。"},
                "kingdom_contributors": {"村夫": ["索朗帝国"]},
                "historical_events": ["两百年前索朗帝国与自然联邦爆发战争。"],
                "historical_event_contributors": {"村夫": ["索朗-自然战争"]},
            }
        )
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(manager.state, "村夫", "藤蔓为何能让钢铁生出生命？")

        self.assertEqual(response.world_updates["mysteries"], ["藤蔓为何能让钢铁生出生命"])
        self.assertEqual(response.world_updates["mystery_contributors"], {"村夫": ["藤蔓为何能让钢铁生出生命"]})

    def test_skip_marks_player_world_contribution_without_blocking(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        state = manager.start(participants=["村夫", "loading"])
        manager.apply_world_updates(
            {
                "map_card": "大陆地图卡",
                "travel_day_length": "一天路程",
                "magic_tech_role": "科技与魔法对立。",
                "kingdoms": {"索朗": "曾以蒸汽飞艇和机械巨兽称雄的帝国。"},
                "kingdom_contributors": {"村夫": ["索朗"]},
            }
        )
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(state, "loading", "我这轮没想法，先跳过")
        manager.apply_response(response)

        self.assertIn("loading", manager.state.world.historical_event_contributors)
        self.assertTrue(any("重大历史事件" in question for question in manager.state.world.open_questions))

    def test_first_act_candidates_wait_until_every_participant_has_complete_hero(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        state = manager.start(participants=["阿凛", "南星"])
        manager.apply_world_updates(
            {
                "map_card": "大陆地图卡",
                "magic_tech_role": "魔法与科技并存。",
                "kingdoms": {"钟鸣公国": "内海北岸的钟楼公国。"},
                "historical_events": ["碎月坠落。"],
                "mysteries": ["失落名字为何会出现在风铃内侧？"],
                "world_threats": ["辉钢财团买卖记忆。"],
                "group_concept": "临时守护者",
                "safety_lines": ["不详细描写酷刑"],
                "hero_drafts": {
                    "阿凛": {
                        "player_name": "阿凛",
                        "hero_name": "伊莉雅",
                        "identity": "盾誓骑士",
                        "theme": "责任",
                        "origin": "白花碑驿站",
                        "classes": {"守护者": 3, "元素使": 2},
                        "attributes": {"DEX": 8, "INS": 8, "MIG": 10, "WLP": 6},
                        "skills": {"保镖": 1, "防御精通": 1, "挺身守护": 1, "元素魔法": 1, "元素系仪式": 1},
                        "spells": ["元素幕障"],
                        "equipment": ["钢匕首", "青铜盾", "旅行装束"],
                        "confirmed": True,
                    }
                },
            }
        )
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(state, "阿凛", "伊莉雅确认角色并正式建卡。")

        self.assertEqual(response.stage, SessionZeroStage.HEROES)
        self.assertNotIn("first_act_candidates", response.world_updates)
        self.assertIn("南星 的英雄还没创建", response.message)

    def test_explicit_world_contribution_bundle_marks_all_four_world_fields(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        state = manager.start(participants=["白河", "南星"])
        manager.apply_world_updates(
            {
                "map_card": "大陆地图卡",
                "magic_tech_role": "灵魂晶炉驱动的魔科技与御魂术并存。",
            }
        )
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(
            state,
            "白河",
            "为了补齐第零章流程，我明确给出白河的四类贡献："
            "王国或国家是第七采掘城这个移动城邦，由辉钢财团统治；"
            "重大历史事件是记忆炉第一次启动时吞掉了一整条矿道工人的姓名；"
            "世界奥秘是第七采掘城的紧急停机协议为何只回应赤羽遗民的歌；"
            "世界威胁是监察官艾蕾娜要把所有人的记忆集中管理。",
        )
        manager.apply_response(response)

        world = manager.state.world
        self.assertIn("第七采掘城", world.kingdoms)
        self.assertEqual(world.kingdom_contributors["白河"], ["第七采掘城"])
        self.assertEqual(
            world.historical_event_contributors["白河"],
            ["记忆炉第一次启动时吞掉了一整条矿道工人的姓名"],
        )
        self.assertEqual(
            world.mystery_contributors["白河"],
            ["第七采掘城的紧急停机协议为何只回应赤羽遗民的歌"],
        )
        self.assertEqual(
            world.threat_contributors["白河"],
            ["监察官艾蕾娜要把所有人的记忆集中管理"],
        )

    def test_world_creation_status_query_is_read_only_and_points_to_earliest_gap(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        state = manager.start(participants=["测试玩家甲", "loading"])
        manager.apply_world_updates(
            {
                "map_card": "一块完整大陆",
                "travel_day_length": "1天（步行）",
                "magic_tech_role": "科技与魔法对立。",
                "kingdoms": {"索朗帝国": "蒸汽帝国。", "自然联邦": "崇尚自然的王国联邦。"},
                "kingdom_contributors": {"测试玩家甲": ["索朗帝国", "自然联邦"]},
                "historical_events": ["两百年前索朗帝国与自然联邦爆发战争。"],
                "historical_event_contributors": {"测试玩家甲": ["索朗-自然战争"]},
                "mysteries": ["藤蔓为何能让无意识钢铁生出生命？"],
                "mystery_contributors": {"测试玩家甲": ["钢铁生命之谜"]},
            }
        )
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(state, "测试玩家甲", "创建世界还缺什么？")

        self.assertEqual(response.world_updates, {})
        self.assertEqual(response.accepted_facts, [])
        self.assertNotIn("我先记下这个想法", response.message)
        self.assertNotIn("还需要轮流确认", response.message)
        self.assertNotIn("下一步先处理", response.message)
        self.assertNotIn("等待loading贡献", response.message)
        self.assertIn("还没完成", response.message)
        self.assertIn("仍有玩家可以补", response.message)
        self.assertIn("一个王国或国家", response.message)
        self.assertIn("第6步 世界性威胁", response.message)
        self.assertIn("有灵感的人可以直接接", response.message)

    def test_llm_status_query_short_circuits_without_recording_idea(self) -> None:
        transport = FakeTransport(
            [
                json.dumps(
                    {
                        "message": "这条不该调用模型。",
                        "stage": "tone",
                        "world_updates": {},
                    },
                    ensure_ascii=False,
                )
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-pro",
            expressor_model="deepseek-v4-pro",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        facilitator = LLMSessionZeroFacilitator(client=client, model=config.action_model)
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        state = manager.start(participants=["测试玩家甲", "loading"])

        response = facilitator.respond(state, "测试玩家甲", "创建世界还缺什么？")

        self.assertEqual(len(transport.calls), 0)
        self.assertEqual(response.world_updates, {})
        self.assertNotIn("我先记下这个想法", response.message)
        self.assertNotIn("下一步先处理", response.message)
        self.assertIn("第1步 地图卡与主要陆地", response.message)

    def test_llm_facilitator_does_not_echo_existing_world_records_when_prompting(self) -> None:
        transport = FakeTransport(
            [
                json.dumps(
                    {
                        "message": "好~先跳过王国没问题，那我们来看看世界的谜团吧——“藤蔓为何能让钢铁生出生命？”已经有一个，你觉得还有什么未解之谜？",
                        "stage": "tone",
                        "questions": ["loading，你想补一个世界奥秘或谜团吗？"],
                        "world_updates": {},
                    },
                    ensure_ascii=False,
                )
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-pro",
            expressor_model="deepseek-v4-pro",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        facilitator = LLMSessionZeroFacilitator(client=client, model=config.action_model)
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        manager.start(participants=["村夫", "loading"])
        manager.apply_world_updates(
            {
                "map_card": "大陆地图卡",
                "travel_day_length": "一天路程",
                "magic_tech_role": "科技与魔法对立。",
                "kingdoms": {"索朗帝国": "曾经强盛的蒸汽帝国。"},
                "kingdom_contributors": {"村夫": ["索朗帝国"]},
                "mysteries": ["藤蔓为何能让钢铁生出生命？"],
                "mystery_contributors": {"村夫": ["钢铁生命之谜"]},
            }
        )

        response = facilitator.respond(manager.state, "loading", "先跳过王国")

        visible = response.message + " ".join(response.questions)
        self.assertNotIn("藤蔓为何能让钢铁生出生命", visible)
        self.assertNotIn("已经有一个", visible)
        self.assertIn("重大历史事件", visible)
        self.assertEqual(response.world_updates["kingdom_contributors"], {"loading": ["暂时跳过"]})

    def test_first_act_candidates_vote_and_confirm_write_world_profile(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        manager.start(participants=["阿凛", "白河"])
        manager.apply_world_updates(
            {
                "world_style": "高度奇幻",
                "group_concept": "追寻遗失传说的旅行英雄团",
                "starting_region": "云海边境",
                "major_locations": {"星尘迷宫": "每次开门都会换位的古代地下城。"},
                "factions": {"苍蓝探险者协会": "垄断遗迹地图的半官方组织。"},
                "villain_seeds": ["收藏英雄命运的宝箱王。"],
                "villain_mirrors": ["宝箱王映照英雄对奇遇与财宝的渴望。"],
                "mysteries": ["星尘迷宫为何会回应英雄的愿望？"],
                "safety_lines": ["详细酷刑"],
            }
        )

        candidates = manager.generate_first_act_candidates()
        manager.record_first_act_vote("阿凛", "2")
        manager.record_first_act_vote("白河", "2")
        winner = manager.confirm_first_act("2")

        self.assertEqual(len(candidates), 3)
        self.assertIsNotNone(winner)
        self.assertEqual(manager.state.world.selected_first_act_id, "first_act_2")
        self.assertIn("第一幕选择", manager.state.world.selected_first_act_summary)
        self.assertTrue(manager.state.world.starting_bond_suggestions)
        self.assertEqual(world_state.world_profile.selected_first_act_id, "first_act_2")

    def test_gm_secret_audit_view_reports_private_risks_without_public_leak(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        manager.start()
        world_state.upsert_gm_secret(
            "mirror_villain",
            title="镜像反派",
            content="宝箱王其实是未来的阿凛。",
            lock_level=SecretLockLevel.SEEDED,
            related_entities=["阿凛"],
            tags=["villain"],
        )
        manager.apply_world_updates({"gm_secret_notes": ["不要给玩家看：宝箱王会在第一幕观察英雄。"]})

        public_report = manager.gm_secret_audit_report(include_content=False)
        private_report = manager.gm_secret_audit_report(include_content=True)

        self.assertEqual(public_report.entries[0].content, "")
        self.assertIn("缺少公开线索", "；".join(public_report.entries[0].risks))
        self.assertIn("未来的阿凛", private_report.entries[0].content)
        self.assertTrue(public_report.orphan_notes)

    def test_session_zero_tracks_incremental_hero_draft_and_missing_details(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        state = manager.start(participants=["阿凛"])
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(
            state,
            "阿凛",
            "我的角色叫露娜，身份是失国公主，主题是正义，来自水晶王国，职业选元素使2级和守护者3级，装备法杖和青铜盾。",
        )
        manager.apply_response(response)

        draft = manager.state.world.hero_drafts["阿凛"]
        self.assertEqual(draft.hero_name, "露娜")
        self.assertEqual(draft.identity, "失国公主")
        self.assertEqual(draft.theme, "正义")
        self.assertEqual(draft.classes["元素使"], 2)
        self.assertEqual(draft.classes["守护者"], 3)
        self.assertIn("青铜盾", draft.equipment)
        self.assertFalse(any("属性骰" in question for question in manager.state.world.open_questions))
        self.assertTrue(any("地图" in question or "创建世界第1步" in question for question in manager.state.world.open_questions))
        public_text = response.message + " ".join(response.accepted_facts) + " ".join(response.questions)
        self.assertNotIn("身份是失国公主", public_text)
        self.assertNotIn("元素使2级", public_text)
        self.assertNotIn("青铜盾", public_text)
        self.assertTrue(manager.state.world.villain_mirrors)
        self.assertTrue(manager.state.world.gm_secret_notes)

    def test_session_zero_does_not_default_unranked_class_to_one(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        state = manager.start(participants=["阿凛"])
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(
            state,
            "阿凛",
            "我的角色叫露娜，身份是失国公主，主题是正义，故乡是水晶王国，职业元素使和武器大师。",
        )
        manager.apply_response(response)

        draft = manager.state.world.hero_drafts["阿凛"]
        self.assertEqual(draft.classes, {})
        self.assertFalse(any("职业分配" in question for question in manager.state.world.open_questions))
        self.assertTrue(any("地图" in question or "创建世界第1步" in question for question in manager.state.world.open_questions))

    def test_skill_choice_updates_mentioned_existing_draft_and_prompts_spell_choice(self) -> None:
        world_state, manager, state = self._make_world_ready_state(participants=["loading"])
        state.world.hero_drafts["艾丽妮"] = HeroDraft(
            player_name="loading",
            hero_name="艾丽妮",
            identity="被放逐的学徒",
            theme="归属",
            origin="星落尖塔",
            classes={"元素使": 2, "旅人": 1, "博学家": 2},
        )
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(state, "loading", "艾丽妮元素使的技能我选元素魔法和元素系仪式")
        manager.apply_response(response)

        draft = manager.state.world.hero_drafts["艾丽妮"]
        self.assertNotIn("loading", manager.state.world.hero_drafts)
        self.assertEqual(draft.skills["元素魔法"], 1)
        self.assertEqual(draft.skills["元素系仪式"], 1)
        public_text = response.message + " ".join(response.questions) + " ".join(manager.state.world.open_questions)
        self.assertIn("元素使法术", public_text)

    def test_skill_choice_with_spell_records_both_without_spell_prompt(self) -> None:
        world_state, manager, state = self._make_world_ready_state(participants=["村夫"])
        state.world.hero_drafts["诺艾尔"] = HeroDraft(
            player_name="村夫",
            hero_name="诺艾尔",
            identity="秘宝猎人",
            theme="野心",
            origin="托伦",
            classes={"元素使": 1, "武器大师": 2, "旅人": 1, "游说家": 1},
        )
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(state, "村夫", "诺艾尔元素使的技能我选元素魔法，法术选择元素幕障")
        manager.apply_response(response)

        draft = manager.state.world.hero_drafts["诺艾尔"]
        self.assertEqual(draft.skills["元素魔法"], 1)
        self.assertIn("元素幕障", draft.spells)
        public_text = response.message + " ".join(response.questions) + " ".join(manager.state.world.open_questions)
        self.assertNotIn("元素使法术（还需", public_text)

    def test_unqualified_spell_choice_does_not_update_other_players_draft(self) -> None:
        world_state, manager, state = self._make_world_ready_state(participants=["村夫"])
        state.world.hero_drafts["诺艾尔"] = HeroDraft(
            player_name="村夫",
            hero_name="诺艾尔",
            identity="秘宝猎人",
            theme="野心",
            origin="托伦",
            classes={"元素使": 1, "武器大师": 2, "旅人": 1, "游说家": 1},
            attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
            skills={"元素魔法": 1, "宝物猎人": 1, "碎骨": 1, "破防打击": 1, "谴责": 1},
            spells=["元素幕障"],
            equipment=["细剑", "旅行装束"],
        )
        state.world.hero_drafts["艾丽妮"] = HeroDraft(
            player_name="loading",
            hero_name="艾丽妮",
            identity="被放逐的学徒",
            theme="归属",
            origin="星落尖塔",
            classes={"元素使": 2, "旅人": 1, "博学家": 2},
            skills={"元素魔法": 1, "元素系仪式": 1},
        )
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(state, "村夫", "元素使法术选择元素幕障")
        manager.apply_response(response)

        self.assertEqual(manager.state.world.hero_drafts["诺艾尔"].spells, ["元素幕障"])
        self.assertEqual(manager.state.world.hero_drafts["艾丽妮"].spells, [])
        self.assertFalse(any("诺艾尔】的法术" in fact for fact in response.accepted_facts))
        public_text = response.message + " ".join(response.questions) + " ".join(manager.state.world.open_questions)
        self.assertNotIn("记录来自村夫", public_text)
        self.assertNotIn("艾丽妮】的法术", public_text)

    def test_missing_spell_prompt_lists_only_standard_spellbook_options(self) -> None:
        world_state, manager, state = self._make_world_ready_state(participants=["村夫", "loading"])
        state.world.hero_drafts["艾丽妮"] = HeroDraft(
            player_name="loading",
            hero_name="艾丽妮",
            identity="被放逐的学徒",
            theme="归属",
            origin="星落尖塔",
            classes={"元素使": 2, "旅人": 1, "博学家": 2},
            skills={"元素魔法": 1, "元素系仪式": 1},
        )
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(state, "村夫", "哦是艾丽妮没选法术@loading")

        public_text = response.message + " ".join(response.questions) + " ".join(response.world_updates.get("open_questions", []))
        self.assertIn("元素幕障", public_text)
        self.assertIn("元素武器", public_text)
        self.assertIn("巨岩", public_text)
        self.assertNotIn("火焰箭", public_text)
        self.assertNotIn("冰霜之触", public_text)
        self.assertNotIn("土石铠甲", public_text)

    def test_session_zero_spell_options_short_circuit_uses_spellbook(self) -> None:
        state = SessionZeroManager(WorldState()).start(participants=["阿凛"])
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(state, "阿凛", "元素使法术有哪些？")

        self.assertIn("元素幕障", response.message)
        self.assertIn("元素武器", response.message)
        self.assertIn("巨岩", response.message)
        self.assertNotIn("火焰箭", response.message)
        self.assertNotIn("本地法术表", response.message)
        self.assertNotIn("不会临场", response.message)
        self.assertNotIn("不会编", response.message)

    def test_character_update_does_not_leak_private_villain_mirror_notes(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        state = manager.start(participants=["村夫"])
        state.world.hero_drafts["诺艾尔"] = HeroDraft(
            player_name="村夫",
            hero_name="诺艾尔",
            identity="秘宝猎人",
            theme="野心",
            origin="托伦",
            classes={"元素使": 1, "武器大师": 2, "旅人": 1, "游说家": 1},
        )
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(state, "村夫", "诺艾尔的旅人技能选择宝物猎人")

        visible = response.message + " ".join(response.accepted_facts) + " ".join(response.questions)
        self.assertNotIn("反派映照原则", visible)
        self.assertNotIn("GM暗线", visible)
        self.assertNotIn("建议：", response.message)
        self.assertNotIn("问题：", response.message)
        self.assertIn("villain_mirrors", response.world_updates)

    def test_character_update_message_does_not_render_internal_structured_labels(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        state = manager.start(participants=["村夫", "loading"])
        state.world.hero_drafts["诺艾尔"] = HeroDraft(
            player_name="村夫",
            hero_name="诺艾尔",
            identity="秘宝猎人",
            theme="野心",
            origin="托伦",
            classes={"元素使": 1, "武器大师": 2, "旅人": 1, "游说家": 1},
            skills={"元素魔法": 1},
            spells=["元素幕障"],
        )
        state.world.hero_drafts["艾丽妮"] = HeroDraft(
            player_name="loading",
            hero_name="艾丽妮",
            identity="被放逐的学徒",
            theme="归属",
            origin="星落尖塔",
            classes={"元素使": 2, "旅人": 1, "博学家": 2},
            skills={"元素魔法": 1, "元素系仪式": 1},
        )
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(state, "村夫", "诺艾尔的旅人技能选择宝物猎人")

        self.assertNotIn("建议：", response.message)
        self.assertNotIn("问题：", response.message)
        self.assertNotIn("反派映照原则", response.message)
        self.assertNotIn("后台", response.message)
        self.assertNotIn("隐藏宝箱味", response.message)
        self.assertNotIn("接下来先确认", response.message)
        self.assertTrue(response.suggestions)
        self.assertTrue(response.questions)

    def test_plain_learn_known_spell_records_missing_spell_choice(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        state = manager.start(participants=["loading"])
        state.world.hero_drafts["艾丽妮"] = HeroDraft(
            player_name="loading",
            hero_name="艾丽妮",
            identity="被放逐的学徒",
            theme="归属",
            origin="星落尖塔",
            classes={"元素使": 2, "旅人": 1, "博学家": 2},
            skills={"元素魔法": 1, "元素系仪式": 1},
        )
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(state, "loading", "学一个元素武器")
        manager.apply_response(response)

        draft = manager.state.world.hero_drafts["艾丽妮"]
        self.assertEqual(draft.spells, ["元素武器"])
        self.assertIn("法术选择记好了", response.message)
        self.assertNotIn("记录来自loading", response.message)
        self.assertNotIn("隐藏宝箱味", response.message)

    def test_session_zero_accepts_die_notation_in_hero_draft_attributes(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        manager.start(participants=["阿凛"])

        manager.apply_response(
            SessionZeroResponse(
                message="记录角色草稿。",
                stage=SessionZeroStage.HEROES,
                world_updates={
                    "hero_drafts": {
                        "阿凛": {
                            "hero_name": "露米娅",
                            "classes": {"造物使": "2", "浪客": "2", "元素使": "1"},
                            "attributes": {"DEX": "d10", "INS": "d8", "MIG": "d6", "WLP": "d8"},
                            "skills": {"便携装置": "2", "秘密配方": "1"},
                        }
                    }
                },
            )
        )

        draft = manager.state.world.hero_drafts["阿凛"]
        self.assertEqual(draft.attributes["DEX"], 10)
        self.assertEqual(draft.attributes["WLP"], 8)
        self.assertEqual(draft.classes["造物使"], 2)
        self.assertEqual(draft.skills["便携装置"], 2)

    def test_heuristic_facilitator_parses_natural_character_sentence(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        state = manager.start(participants=["阿凛"])
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(
            state,
            "阿凛",
            "我的角色露米娅，爱拆宝箱的魔导机关师，主题好奇，故乡阿斯特拉庭。"
            "职业：造物使3、浪客1、元素使1。属性：DEX d10、INS d8、MIG d6、WLP d8。"
            "技能：便携装置2、秘密配方1、疾速身法1、元素魔法1。法术：电流术。装备：钢匕首、丝质衬衫。",
        )
        manager.apply_response(response)

        draft = manager.state.world.hero_drafts["阿凛"]
        self.assertEqual(draft.hero_name, "露米娅")
        self.assertEqual(draft.identity, "爱拆宝箱的魔导机关师")
        self.assertEqual(draft.theme, "好奇")
        self.assertEqual(draft.origin, "阿斯特拉庭")
        self.assertTrue(any("支配行动" in question or "如何推动" in question for question in draft.open_questions))
        validation = manager.world_state.world_profile.hero_drafts["阿凛"]
        self.assertEqual(validation.skills["便携装置"], 2)
        self.assertEqual(validation.skills["疾速身法"], 1)

    def test_session_zero_stringifies_structured_world_updates_from_llm(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        manager.start(participants=["阿凛"])

        manager.apply_response(
            SessionZeroResponse(
                message="记录结构化世界更新。",
                stage=SessionZeroStage.THREATS,
                world_updates={
                    "villain_seeds": [{"name": "银面校长", "goal": "夺走痛苦记忆"}],
                    "mysteries": [{"question": "星匣为何回应英雄？"}],
                    "major_locations": {"星匣迷宫": {"theme": "宝箱与星辰记忆"}},
                    "factions": {"灰烬帝国": {"method": "灵魂能源军事化"}},
                    "open_questions": [{"ask": "第一幕从哪里开始？"}],
                },
            )
        )

        self.assertIsInstance(manager.state.world.villain_seeds[0], str)
        self.assertIsInstance(manager.state.world.mysteries[0], str)
        self.assertIsInstance(manager.state.world.major_locations["星匣迷宫"], str)
        self.assertIsInstance(manager.state.world.factions["灰烬帝国"], str)
        self.assertIsInstance(manager.state.world.open_questions[0], str)
        self.assertIn("星匣迷宫", world_state.map_notes)

    def test_empty_hero_draft_patch_does_not_create_placeholder_character(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        manager.start(participants=["阿凛", "白河"])

        manager.apply_response(
            SessionZeroResponse(
                message="先问白河。",
                stage=SessionZeroStage.HEROES,
                world_updates={"hero_drafts": {"白河": {}}},
            )
        )

        self.assertNotIn("白河", manager.state.world.hero_drafts)

    def test_session_zero_can_remove_world_and_hero_draft_items(self) -> None:
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        manager.start()
        manager.apply_response(
            SessionZeroResponse(
                message="记录。",
                stage=SessionZeroStage.HEROES,
                world_updates={
                    "major_locations": {"云海空港": "走私商和旧飞艇停靠处。"},
                    "hero_drafts": {
                        "阿凛": {
                            "hero_name": "露娜",
                            "identity": "失国公主",
                            "classes": {"元素使": 2, "守护者": 3},
                            "equipment": ["法杖", "青铜盾"],
                        }
                    },
                },
            )
        )

        manager.apply_response(
            SessionZeroResponse(
                message="移除。",
                stage=SessionZeroStage.HEROES,
                world_updates={
                    "world_removals": {"major_locations": ["云海空港"]},
                    "hero_drafts": {"阿凛": {"remove_classes": ["元素使"], "remove_equipment": ["法杖"]}},
                },
            )
        )

        self.assertNotIn("云海空港", manager.state.world.major_locations)
        self.assertNotIn("元素使", manager.state.world.hero_drafts["阿凛"].classes)
        self.assertNotIn("法杖", manager.state.world.hero_drafts["阿凛"].equipment)

    def test_llm_facilitator_uses_configured_model_and_session_zero_prompt(self) -> None:
        transport = FakeTransport(
            [
                json.dumps(
                    {
                        "message": "我会把水晶尖塔和飞艇舰队变成第一幕的舞台。",
                        "stage": "group",
                        "accepted_facts": ["世界风格为高度奇幻。"],
                        "suggestions": ["小队可以是神器守护者。"],
                        "questions": ["你们守护的神器是什么？"],
                        "world_updates": {
                            "world_style": "高度奇幻",
                            "hero_drafts": {"阿凛": {"hero_name": "露娜", "theme": "正义"}},
                            "villain_mirrors": ["反派映照露娜的正义主题。"],
                            "gm_secret_notes": ["不要给玩家看：首个反派认识露娜。"],
                            "open_questions": ["你们守护的神器是什么？"],
                        },
                    },
                    ensure_ascii=False,
                )
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.apiyi.com",
            api_key="test-key",
            action_model="gpt-5.4-nano",
            expressor_model="gpt-5.4-nano",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        facilitator = LLMSessionZeroFacilitator(client=client, model=config.action_model)
        state = SessionZeroManager(WorldState()).start()

        response = facilitator.respond(state, "玩家", "想玩高度奇幻。")

        self.assertEqual(response.stage, SessionZeroStage.TONE)
        self.assertEqual(response.world_updates["world_style"], "高度奇幻")
        self.assertIn("hero_drafts", response.world_updates)
        self.assertIn("gm_secret_notes", response.world_updates)
        self.assertIn("地图", response.message + " ".join(response.questions))
        self.assertEqual(transport.calls[0]["payload"]["model"], "gpt-5.4-nano")
        self.assertIn(SESSION_ZERO_SYSTEM_PROMPT, transport.calls[0]["payload"]["messages"][0]["content"])

    def test_llm_facilitator_merges_deterministic_character_fields(self) -> None:
        transport = FakeTransport(
            [
                json.dumps(
                    {
                        "message": "露米娅的宝箱机关味很棒，我记下这个方向。",
                        "stage": "heroes",
                        "accepted_facts": ["阿凛想玩机关师。"],
                        "world_updates": {},
                    },
                    ensure_ascii=False,
                )
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-pro",
            expressor_model="deepseek-v4-pro",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        facilitator = LLMSessionZeroFacilitator(client=client, model=config.action_model)
        state = SessionZeroManager(WorldState()).start(participants=["阿凛"])

        response = facilitator.respond(
            state,
            "阿凛",
            "我的角色露米娅，爱拆宝箱的魔导机关师，主题好奇，故乡阿斯特拉庭。"
            "职业：造物使3、浪客1、元素使1。属性：DEX d10、INS d8、MIG d6、WLP d8。"
            "技能：便携装置2、秘密配方1、疾速身法1、元素魔法1。",
        )

        draft_patch = response.world_updates["hero_drafts"]["阿凛"]
        self.assertEqual(draft_patch["hero_name"], "露米娅")
        self.assertEqual(draft_patch["identity"], "爱拆宝箱的魔导机关师")
        self.assertEqual(draft_patch["theme"], "好奇")
        self.assertTrue(any("如何推动" in question or "如何支配" in question for question in draft_patch["open_questions"]))
        self.assertEqual(draft_patch["attributes"]["DEX"], 10)

    def test_character_name_label_does_not_capture_leading_name_character(self) -> None:
        facilitator = HeuristicSessionZeroFacilitator()
        state = SessionZeroManager(WorldState()).start(participants=["阿凛"])

        response = facilitator.respond(
            state,
            "阿凛",
            "我的玩家名是阿凛，角色名伊莉雅。身份：赤羽遗民的盾誓骑士；主题：责任；故乡：白花碑驿站。"
            "职业分配：守护者3级、元素使2级。属性骰：敏捷d8、洞察d8、力量d10、意志d6。"
            "职业技能：保镖1、防御精通1、挺身守护1、元素魔法1、元素系仪式1。"
            "法术选择：元素幕障。初始装备：钢匕首、青铜盾、旅行装束。",
        )

        draft_patch = response.world_updates["hero_drafts"]["阿凛"]
        self.assertEqual(draft_patch["hero_name"], "伊莉雅")
        self.assertNotEqual(draft_patch["hero_name"], "名伊莉雅")

    def test_llm_facilitator_sends_participant_polling_state(self) -> None:
        transport = FakeTransport(
            [
                json.dumps(
                    {
                        "message": "阿凛，我先听你的开场画面。",
                        "stage": "tone",
                        "questions": ["阿凛，你想看到什么画面？"],
                        "world_updates": {"open_questions": ["阿凛，你想看到什么画面？"]},
                    },
                    ensure_ascii=False,
                )
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.apiyi.com",
            api_key="test-key",
            action_model="gpt-5.4-nano",
            expressor_model="gpt-5.4-nano",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        facilitator = LLMSessionZeroFacilitator(client=client, model=config.action_model)
        state = SessionZeroManager(WorldState()).start(participants=["阿凛", "白河"])
        state.world.hero_drafts["阿凛"] = HeroDraft(player_name="阿凛", hero_name="露娜")

        facilitator.opening(state)

        prompt_payload = transport.calls[0]["payload"]["messages"][1]["content"]
        self.assertIn('"participants"', prompt_payload)
        self.assertIn('"current_participant": "阿凛"', prompt_payload)
        self.assertIn('"hero_drafts"', prompt_payload)

    def test_llm_facilitator_injects_gm_personality_prompt(self) -> None:
        transport = FakeTransport(
            [
                json.dumps(
                    {
                        "message": "夜色像翻开的卡牌，我先听你们的第一幕。",
                        "stage": "tone",
                        "questions": ["你们想让命运从哪座门后开始？"],
                        "world_updates": {},
                    },
                    ensure_ascii=False,
                )
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-pro",
            expressor_model="deepseek-v4-pro",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        facilitator = LLMSessionZeroFacilitator(
            client=client,
            model=config.action_model,
            gm_personality_prompt="她像温柔但危险的占星师，说话短促，偏爱卡牌、星尘和门。",
        )
        state = SessionZeroManager(WorldState()).start(participants=["阿凛"])

        facilitator.opening(state)

        system_prompt = transport.calls[0]["payload"]["messages"][0]["content"]
        user_prompt = transport.calls[0]["payload"]["messages"][1]["content"]
        self.assertNotIn("温柔但危险的占星师", system_prompt)
        self.assertIn("当前 GM 人格档案", user_prompt)
        self.assertIn("温柔但危险的占星师", user_prompt)

    def test_llm_facilitator_short_circuits_class_list_questions(self) -> None:
        transport = FakeTransport(
            [
                json.dumps(
                    {
                        "message": "这个回复不该被用到。",
                        "stage": "tone",
                        "questions": [],
                        "world_updates": {},
                    },
                    ensure_ascii=False,
                )
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-pro",
            expressor_model="deepseek-v4-pro",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        facilitator = LLMSessionZeroFacilitator(client=client, model=config.action_model)
        state = SessionZeroManager(WorldState()).start(participants=["阿凛"])

        response = facilitator.respond(state, "阿凛", "有什么职业可以选择？")

        self.assertEqual(len(transport.calls), 0)
        self.assertIn("奥灵使", response.message)
        self.assertIn("武器大师", response.message)
        self.assertIn("起始角色通常为 5 级", response.message)

    def test_llm_facilitator_prefers_deterministic_reply_for_skill_choices(self) -> None:
        transport = FakeTransport(
            [
                json.dumps(
                    {
                        "message": "记录反派映照原则：艾丽妮元素使2级带来两个法术，你想选哪两个？",
                        "stage": "heroes",
                        "questions": ["GM暗线：艾丽妮要选两个元素使法术。"],
                        "world_updates": {
                            "hero_drafts": {
                                "诺艾尔": {
                                    "skills": {"元素魔法": 2},
                                    "open_questions": ["错误地要求两个元素法术。"],
                                }
                            }
                        },
                    },
                    ensure_ascii=False,
                )
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-pro",
            expressor_model="deepseek-v4-pro",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        facilitator = LLMSessionZeroFacilitator(client=client, model=config.action_model)
        state = SessionZeroManager(WorldState()).start(participants=["村夫"])
        state.world.hero_drafts["诺艾尔"] = HeroDraft(
            player_name="村夫",
            hero_name="诺艾尔",
            identity="秘宝猎人",
            theme="野心",
            origin="托伦",
            classes={"元素使": 1, "武器大师": 2, "旅人": 1, "游说家": 1},
            skills={"元素魔法": 1},
            spells=["元素幕障"],
        )

        response = facilitator.respond(
            state,
            "村夫",
            "旅人技能选择宝物猎人，武器大师选择碎骨和破防打击，游说家技能选择谴责",
        )

        self.assertNotIn("两个法术", response.message)
        self.assertNotIn("反派映照原则", response.message)
        self.assertNotIn("建议：", response.message)
        self.assertNotIn("问题：", response.message)
        self.assertNotIn("GM暗线", " ".join(response.questions))
        self.assertIn("诺艾尔", response.message)
        self.assertEqual(response.world_updates["hero_drafts"]["诺艾尔"]["skills"]["谴责"], 1)
        self.assertNotEqual(response.world_updates["hero_drafts"]["诺艾尔"]["skills"].get("元素魔法"), 2)

    def test_llm_facilitator_does_not_jump_to_first_act_before_hero_ready(self) -> None:
        transport = FakeTransport(
            [
                json.dumps(
                    {
                        "message": "核心素材齐了，第一幕从哪里开始？我给你们三个候选投票。",
                        "stage": "prologue",
                        "questions": ["第一幕从哪里开始？"],
                        "world_updates": {},
                    },
                    ensure_ascii=False,
                )
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-pro",
            expressor_model="deepseek-v4-pro",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        facilitator = LLMSessionZeroFacilitator(client=client, model=config.action_model)
        _, _, state = self._make_world_ready_state(participants=["村夫"])
        state.world.hero_drafts["诺艾尔"] = HeroDraft(
            player_name="村夫",
            hero_name="诺艾尔",
            identity="秘宝猎人",
            theme="野心",
            origin="托伦",
            classes={"元素使": 1, "武器大师": 2, "旅人": 1, "游说家": 1},
            attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
        )

        response = facilitator.respond(state, "村夫", "好的")

        visible = response.message + " ".join(response.questions)
        self.assertNotEqual(response.stage, SessionZeroStage.PROLOGUE)
        self.assertNotIn("第一幕从哪里开始", visible)
        self.assertIn("职业技能", visible)

    def test_llm_facilitator_rejects_premature_world_completion_claim(self) -> None:
        transport = FakeTransport(
            [
                json.dumps(
                    {
                        "message": "超级详细的世界背景！这已经覆盖了世界七步了。先确认小队类型吧。",
                        "stage": "group",
                        "questions": ["你们的小队类型是什么？"],
                        "world_updates": {
                            "map_card": "大陆地图卡",
                            "magic_tech_role": "科技与魔法对立。",
                            "kingdoms": {"索朗": "蒸汽帝国。"},
                            "historical_events": ["两百年前索朗帝国与自然联邦的战争。"],
                            "mysteries": ["钢铁生命藤蔓之谜。"],
                            "world_threats": ["索朗帝国威胁世界。"],
                        },
                    },
                    ensure_ascii=False,
                )
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-pro",
            expressor_model="deepseek-v4-pro",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        facilitator = LLMSessionZeroFacilitator(client=client, model=config.action_model)
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        state = manager.start(participants=["村夫", "loading"])
        manager.apply_world_updates({"travel_day_length": "一天路程"})

        response = facilitator.respond(
            state,
            "村夫",
            "像一块完整的大陆，科技与魔法是对立的，大约两百年前索朗帝国和自然联邦爆发战争，最后禁忌仪式让藤蔓在机械巨兽齿轮中生长，世界也因此出现了让无意识钢铁生出生命的藤蔓",
        )

        visible = response.message + " ".join(response.questions)
        self.assertEqual(response.stage, SessionZeroStage.TONE)
        self.assertNotIn("覆盖了世界七步", visible)
        self.assertNotIn("小队类型", visible)
        self.assertNotIn("mysteries", response.world_updates)
        self.assertNotIn("world_threats", response.world_updates)
        self.assertNotIn("谁有灵感就接", response.message)
        self.assertNotIn("你来接这一笔", visible)
        self.assertIn("奥秘", visible)

    def test_llm_facilitator_does_not_record_rejected_cross_player_spell_choice(self) -> None:
        transport = FakeTransport(
            [
                json.dumps(
                    {
                        "message": "收到！已记录诺艾尔的元素使法术为【元素幕障】。",
                        "stage": "heroes",
                        "accepted_facts": ["诺艾尔元素使法术选择：元素幕障"],
                        "questions": ["诺艾尔初始装备怎么选？"],
                        "world_updates": {"hero_drafts": {"诺艾尔": {"spells": ["元素幕障"]}}},
                    },
                    ensure_ascii=False,
                )
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-pro",
            expressor_model="deepseek-v4-pro",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        facilitator = LLMSessionZeroFacilitator(client=client, model=config.action_model)
        _, _, state = self._make_world_ready_state(participants=["村夫", "loading"])
        state.world.hero_drafts["诺艾尔"] = HeroDraft(
            player_name="村夫",
            hero_name="诺艾尔",
            identity="秘宝猎人",
            theme="野心",
            origin="托伦",
            classes={"元素使": 1, "武器大师": 2, "旅人": 1, "游说家": 1},
            attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
            skills={"元素魔法": 1, "宝物猎人": 1, "碎骨": 1, "破防打击": 1, "谴责": 1},
            spells=["元素幕障"],
            equipment=["细剑", "旅行装束"],
        )
        state.world.hero_drafts["艾丽妮"] = HeroDraft(
            player_name="loading",
            hero_name="艾丽妮",
            identity="被放逐的学徒",
            theme="归属",
            origin="星落尖塔",
            classes={"元素使": 2, "旅人": 1, "博学家": 2},
            skills={"元素魔法": 1, "元素系仪式": 1},
        )

        response = facilitator.respond(state, "村夫", "元素使法术选择元素幕障")

        self.assertEqual(response.world_updates.get("hero_drafts"), {})
        visible = response.message + " ".join(response.accepted_facts)
        self.assertNotIn("已记录诺艾尔的元素使法术", visible)
        self.assertIn("艾丽妮", visible)

    def test_llm_facilitator_overrides_hallucinated_spell_options(self) -> None:
        transport = FakeTransport(
            [
                json.dumps(
                    {
                        "message": "艾丽妮还需要选一个元素使法术，可选：元素幕障、火焰箭、冰霜之触、雷霆打击、土石铠甲。",
                        "stage": "heroes",
                        "questions": ["@loading 请选择：火焰箭、冰霜之触、土石铠甲。"],
                        "world_updates": {},
                    },
                    ensure_ascii=False,
                )
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-pro",
            expressor_model="deepseek-v4-pro",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        facilitator = LLMSessionZeroFacilitator(client=client, model=config.action_model)
        _, _, state = self._make_world_ready_state(participants=["村夫", "loading"])
        state.world.hero_drafts["艾丽妮"] = HeroDraft(
            player_name="loading",
            hero_name="艾丽妮",
            identity="被放逐的学徒",
            theme="归属",
            origin="星落尖塔",
            classes={"元素使": 2, "旅人": 1, "博学家": 2},
            skills={"元素魔法": 1, "元素系仪式": 1},
        )

        response = facilitator.respond(state, "村夫", "哦是艾丽妮没选法术@loading")

        visible = response.message + " ".join(response.questions)
        self.assertIn("元素幕障", visible)
        self.assertIn("元素武器", visible)
        self.assertIn("巨岩", visible)
        self.assertNotIn("火焰箭", visible)
        self.assertNotIn("冰霜之触", visible)
        self.assertNotIn("土石铠甲", visible)

    def test_llm_facilitator_short_circuits_spell_options_questions(self) -> None:
        transport = FakeTransport(
            [
                json.dumps(
                    {
                        "message": "这个回复不该被用到。",
                        "stage": "heroes",
                        "questions": ["火焰箭？"],
                        "world_updates": {},
                    },
                    ensure_ascii=False,
                )
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-pro",
            expressor_model="deepseek-v4-pro",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        facilitator = LLMSessionZeroFacilitator(client=client, model=config.action_model)
        state = SessionZeroManager(WorldState()).start(participants=["阿凛"])

        response = facilitator.respond(state, "阿凛", "元素使法术有哪些？")

        self.assertEqual(len(transport.calls), 0)
        self.assertIn("元素幕障", response.message)
        self.assertIn("巨岩", response.message)
        self.assertNotIn("火焰箭", response.message)
        self.assertNotIn("本地法术表", response.message)
        self.assertNotIn("不会临场", response.message)

    def test_heuristic_facilitator_gives_starting_equipment_advice_without_recording(self) -> None:
        _, _, state = self._make_world_ready_state(participants=["村夫"])
        state.world.hero_drafts["村夫"] = HeroDraft(
            player_name="村夫",
            hero_name="诺艾尔",
            identity="秘宝猎人",
            theme="野心",
            origin="托伦",
            classes={"元素使": 1, "武器大师": 2, "旅人": 1, "游说家": 1},
            attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
            skills={"元素魔法": 1, "碎骨": 1, "破防打击": 1, "宝物猎人": 1, "谴责": 1},
            spells=["元素幕障"],
            equipment=["旅行装束"],
        )
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(state, "村夫", "诺艾尔初始装备怎么选？")

        self.assertEqual(response.world_updates, {})
        self.assertIn("500Z", response.message)
        self.assertIn("模板", response.message)
        self.assertIn("2d6x10", response.message)
        self.assertIn("3 点物语点", response.message)
        self.assertTrue(any(name in response.message for name in ("细剑", "武士刀", "钢匕首", "法杖", "魔典")))

    def test_llm_facilitator_short_circuits_equipment_reference_questions(self) -> None:
        transport = FakeTransport(
            [
                json.dumps(
                    {
                        "message": "这个回复不该被用到。",
                        "stage": "heroes",
                        "questions": ["巨剑还是光剑？"],
                        "world_updates": {},
                    },
                    ensure_ascii=False,
                )
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-pro",
            expressor_model="deepseek-v4-pro",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        facilitator = LLMSessionZeroFacilitator(client=client, model=config.action_model)
        state = SessionZeroManager(WorldState()).start(participants=["阿凛"])

        response = facilitator.respond(state, "阿凛", "基础武器有哪些？")

        self.assertEqual(len(transport.calls), 0)
        self.assertIn("法杖", response.message)
        self.assertIn("临时武器(近战)", response.message)
        self.assertIn("手里剑", response.message)
        self.assertIn("500Z", response.message)
        self.assertNotIn("光剑", response.message)

    def test_llm_facilitator_can_append_deepseek_roleplay_marker(self) -> None:
        transport = FakeTransport(
            [
                json.dumps(
                    {
                        "message": "我会先冷静整理故事钩子。",
                        "stage": "tone",
                        "questions": ["第一个画面是什么？"],
                        "world_updates": {},
                    },
                    ensure_ascii=False,
                )
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-pro",
            expressor_model="deepseek-v4-pro",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        facilitator = LLMSessionZeroFacilitator(
            client=client,
            model=config.action_model,
            deepseek_roleplay_mode="analysis",
        )
        state = SessionZeroManager(WorldState()).start(participants=["阿凛"])

        facilitator.respond(state, "阿凛", "我想看地下城和宝箱。")

        user_prompt = transport.calls[0]["payload"]["messages"][1]["content"]
        self.assertIn("【思维模式要求】", user_prompt)
        self.assertIn("禁止使用圆括号包裹内心独白", user_prompt)

    def test_session_zero_prompt_constrains_output_length_and_counts(self) -> None:
        self.assertIn("最多约 220 个中文字符", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("suggestions 最多 2 条", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("不要重复自我介绍", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("不是三选一", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("初始角色通常为 5 级", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("不要沿用旧模板里的“LV1 起手”", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("总点数为 32", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("固定职业只有 15 个", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("奥灵使", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("武器大师", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("身份 identity：一小句话", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("推荐主题只有：慈悲、愤怒、复仇、归属、愧疚、使命、希望、野心、疑虑、正义", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("故乡 origin：角色来自何处", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("多面手 d8/d8/d8/d8", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("专业领域 d10/d10/d6/d6", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("基础武器模板只有：法杖、魔典", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("剩余金币会加入初始资金", SESSION_ZERO_SYSTEM_PROMPT)

    def test_orchestrator_starts_and_discusses_session_zero(self) -> None:
        characters = CharacterManager()
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        world_state = WorldState()
        rules = RulesEngine(seed=0)
        manager = SessionZeroManager(world_state)
        app = SceneOrchestrator(
            action_brain=HeuristicActionBrain(),
            character_manager=characters,
            clock_manager=clocks,
            conflict_manager=conflict,
            world_state=world_state,
            interceptor=ActionInterceptor(rules, characters, clocks, conflict, world_state),
            expressor=Expressor(),
            scene_manager=SceneManager(),
            session_zero_manager=manager,
            session_zero_facilitator=HeuristicSessionZeroFacilitator(),
        )

        opening = app.start_session_zero()
        response = app.discuss_session_zero(
            "玩家",
            "我们想玩自然奇幻，同村的年轻英雄守护家乡，森林深处有古代遗迹和被污染的灾害。",
        )

        self.assertIn("Session 0 世界创建", app.build_panel("继续讨论").game_phase)
        self.assertEqual(app.scene_manager.current_scene.scene_type, SceneType.SESSION_ZERO)
        self.assertIn("时悠", opening.message)
        self.assertEqual(response.world_updates["world_style"], "自然奇幻")
        self.assertEqual(world_state.world_profile.world_style, "自然奇幻")

    def test_session_zero_kingdom_extraction_ignores_hero_origin_noise(self) -> None:
        manager = SessionZeroManager(WorldState())
        manager.start(participants=["阿凛", "南星"])
        facilitator = HeuristicSessionZeroFacilitator()

        hero_response = facilitator.respond(
            manager.state,
            "阿凛",
            "我的角色洛岚是钟鸣公国流亡者，主题是使命。",
        )
        self.assertNotIn("kingdoms", hero_response.world_updates)

        world_response = facilitator.respond(
            manager.state,
            "南星",
            "我补充主要国家：钟鸣公国的大钟能安抚灵魂，赤羽联盟控制旧铁路。",
        )
        self.assertIn("钟鸣公国", world_response.world_updates["kingdoms"])
        self.assertIn("赤羽联盟", world_response.world_updates["kingdoms"])
        self.assertNotIn("的大钟能安抚灵魂", world_response.world_updates["kingdoms"])
        self.assertFalse(any(key.startswith("我的角色") for key in world_response.world_updates["kingdoms"]))
        self.assertNotEqual(world_response.world_updates.get("starting_region"), "水晶王国边境")
        self.assertNotIn("水晶尖塔城", world_response.world_updates.get("major_locations", {}))

        located_response = facilitator.respond(
            manager.state,
            "南星",
            "我贡献一个国家：钟鸣公国在镜线内海北岸，正午大钟能安抚灵魂。",
        )
        self.assertIn("钟鸣公国", located_response.world_updates["kingdoms"])
        self.assertNotIn("钟鸣公国在镜线内海北岸", located_response.world_updates["kingdoms"])

    def test_session_zero_targeted_missing_contributor_question_is_not_reassigned(self) -> None:
        manager = SessionZeroManager(WorldState())
        state = manager.start(participants=["阿凛", "南星", "白河"])
        state.world.map_card = "大陆地图卡"
        state.world.magic_tech_role = "魔法与科技并存。"
        state.world.kingdoms = {"钟鸣公国": "正午大钟能安抚灵魂。"}
        state.world.kingdom_contributors = {"阿凛": ["钟鸣公国"]}
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(
            state,
            "白河",
            "我补一个威胁：辉钢财团正在向雾潮海岸移动。",
        )

        self.assertNotIn("白河，你来接这一笔：南星", response.message)
        self.assertNotIn("南星，你来接这一笔：南星", response.message)
        self.assertIn("重大历史事件", response.message)

    def test_session_zero_pending_world_contribution_answer_prevents_repeat_loop(self) -> None:
        manager = SessionZeroManager(WorldState())
        state = manager.start(participants=["阿凛", "南星", "白河"])
        state.world.map_card = "landmass_main"
        state.world.magic_tech_role = "魔法与科技并存。"
        state.world.kingdoms = {"钟鸣公国": "正午大钟能安抚灵魂。"}
        state.world.kingdom_contributors = {"阿凛": ["钟鸣公国"]}
        state.current_participant_index = 1
        state.participants[1].pending_question = "创建世界第3步补充：南星，你也想贡献一个王国或国家吗？"

        manager.record_player_input("南星", "我先补一个地区事件：白花碑驿站的风铃会保存失去的名字。")
        facilitator = HeuristicSessionZeroFacilitator()
        response = facilitator.respond(state, "南星", "我先补一个地区事件：白花碑驿站的风铃会保存失去的名字。")

        self.assertIn("kingdom_contributions", state.participants[1].answered_topics)
        self.assertNotIn("南星，你也想贡献一个王国或国家吗", response.message)
        self.assertNotIn("你来接这一笔", response.message)
        self.assertIn("重大历史事件", response.message)

    def test_session_zero_world_contribution_can_be_quietly_recorded(self) -> None:
        manager = SessionZeroManager(WorldState())
        state = manager.start(participants=["阿凛", "南星"])
        state.world.map_card = "landmass_main"
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(
            state,
            "阿凛",
            "魔法与科技定位：魔法即科技，工坊会把风铃里的灵魂回声铸进机械核心。",
        )

        self.assertIn("记下", response.message)
        self.assertEqual(response.message.count("魔法和科技"), 0)
        self.assertNotIn("谁有灵感就接", response.message)
        self.assertNotIn("接下来先确认", response.message)
        self.assertNotIn("你来接这一笔", response.message)
        self.assertNotIn("南星，", response.message)

        manager.apply_response(response)
        guidance_response = facilitator.respond(manager.state, "南星", "下一步呢？")
        self.assertIn("世界创建下一步", guidance_response.message)
        self.assertIn("国家", guidance_response.message)
        self.assertNotIn("你来接这一笔", guidance_response.message)

    def test_session_zero_does_not_store_vote_and_villain_seed_as_mystery(self) -> None:
        manager = SessionZeroManager(WorldState())
        state = manager.start(participants=["阿凛", "白河"])
        state.world.map_card = "landmass_main"
        state.world.magic_tech_role = "魔法与科技并存。"
        state.world.kingdoms = {"钟鸣公国": "正午大钟能安抚灵魂。"}
        state.world.kingdom_contributors = {"阿凛": ["钟鸣公国"], "白河": ["第七采掘城"]}
        state.world.historical_events = ["碎月坠落。"]
        state.world.historical_event_contributors = {"阿凛": ["碎月坠落"], "白河": ["记忆炉事故"]}
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(
            state,
            "白河",
            "我投这个第一幕。额外补一个反派种子：第七采掘城的监察官艾蕾娜曾是赤羽遗民，认为只有把记忆集中管理，世界才不会再遗忘灾难。",
        )

        self.assertNotIn("mysteries", response.world_updates)
        self.assertNotIn("mystery_contributors", response.world_updates)

    def test_session_zero_explicit_mystery_is_trimmed_before_other_labels(self) -> None:
        facilitator = HeuristicSessionZeroFacilitator()

        mysteries = facilitator._infer_mysteries(
            "世界奥秘是第七采掘城的紧急停机协议为何只回应赤羽遗民的歌；世界威胁是监察官艾蕾娜要集中管理记忆。"
        )

        self.assertEqual(mysteries, ["第七采掘城的紧急停机协议为何只回应赤羽遗民的歌"])

    def test_session_zero_public_message_does_not_include_commit_facts(self) -> None:
        manager = SessionZeroManager(WorldState())
        state = manager.start(participants=["阿凛"])
        facilitator = HeuristicSessionZeroFacilitator()

        response = facilitator.respond(
            state,
            "阿凛",
            "我的角色叫伊莉雅，身份是赤羽遗民盾誓骑士，主题使命，故乡白花碑驿站。职业守护者2级、武器大师2级、御魂使1级。",
        )

        self.assertNotIn("已记录【伊莉雅】", response.message)
        self.assertNotIn("正式 PC", response.message)
        self.assertIn("记", response.message)

    def test_hero_draft_empty_patch_does_not_clear_existing_profile(self) -> None:
        manager = SessionZeroManager(WorldState())
        state = manager.start(participants=["阿凛"])
        state.world.hero_drafts["阿凛"] = HeroDraft(
            player_name="阿凛",
            hero_name="伊莉雅",
            identity="赤羽遗民的盾誓骑士",
            theme="使命",
            origin="白花碑驿站",
        )

        manager.apply_world_updates(
            {
                "hero_drafts": {
                    "阿凛": {
                        "hero_name": "",
                        "identity": "",
                        "theme": "",
                        "origin": "",
                        "notes": ["伊莉雅确认角色并正式建卡。"],
                        "confirmed": True,
                    }
                }
            }
        )

        draft = state.world.hero_drafts["阿凛"]
        self.assertEqual(draft.hero_name, "伊莉雅")
        self.assertEqual(draft.identity, "赤羽遗民的盾誓骑士")
        self.assertEqual(draft.theme, "使命")
        self.assertEqual(draft.origin, "白花碑驿站")
        self.assertTrue(draft.confirmed)

    def test_orchestrator_formats_session_zero_summary_without_private_spoilers(self) -> None:
        characters = CharacterManager()
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        world_state = WorldState()
        manager = SessionZeroManager(world_state)
        manager.start()
        manager.apply_response(
            SessionZeroResponse(
                message="记录。",
                stage=SessionZeroStage.READY,
                world_updates={
                    "world_style": "高度奇幻",
                    "group_concept": "追寻遗失传说的旅行英雄团",
                    "starting_region": "水晶王国边境",
                    "major_locations": {"星落地下城": "传说藏有会唱歌的宝箱。"},
                    "factions": {"星辉教会": "守护灵魂之流。"},
                    "villain_seeds": ["宝箱王其实在收集英雄欲望。"],
                    "mysteries": ["第一只宝箱怪为什么会说人话？"],
                    "gm_secret_notes": ["不要给玩家看：宝箱王是某位英雄的未来倒影。"],
                },
            )
        )
        rules = RulesEngine(seed=0)
        app = SceneOrchestrator(
            action_brain=HeuristicActionBrain(),
            character_manager=characters,
            clock_manager=clocks,
            conflict_manager=conflict,
            world_state=world_state,
            interceptor=ActionInterceptor(rules, characters, clocks, conflict, world_state),
            expressor=Expressor(),
            scene_manager=SceneManager(),
            session_zero_manager=manager,
        )

        public_summary = app.format_session_zero_summary()
        private_summary = app.format_session_zero_summary(include_private=True)

        self.assertNotIn("世界风格：高度奇幻", public_summary)
        self.assertIn("GM私密暗线：1 条已保存", public_summary)
        self.assertNotIn("未来倒影", public_summary)
        self.assertIn("未来倒影", private_summary)

    def test_orchestrator_starts_session_zero_with_participants(self) -> None:
        characters = CharacterManager()
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        world_state = WorldState()
        rules = RulesEngine(seed=0)
        manager = SessionZeroManager(world_state)
        app = SceneOrchestrator(
            action_brain=HeuristicActionBrain(),
            character_manager=characters,
            clock_manager=clocks,
            conflict_manager=conflict,
            world_state=world_state,
            interceptor=ActionInterceptor(rules, characters, clocks, conflict, world_state),
            expressor=Expressor(),
            scene_manager=SceneManager(),
            session_zero_manager=manager,
            session_zero_facilitator=HeuristicSessionZeroFacilitator(),
        )

        opening = app.start_session_zero(participants=["阿凛", "白河"])
        snapshot = app.session_zero_snapshot()

        self.assertIn("阿凛", opening.message)
        self.assertEqual(snapshot["current_participant"], "阿凛")
        self.assertEqual([participant["name"] for participant in snapshot["participants"]], ["阿凛", "白河"])
        self.assertIn("界限与帷幕", app.scene_manager.current_scene.objective)

    def test_orchestrator_restores_present_players_when_discussing_loaded_session_zero(self) -> None:
        characters = CharacterManager()
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        world_state = WorldState()
        world_state.present_players = ["测试玩家甲", "loading"]
        world_state.world_profile.map_card = "一块完整大陆"
        world_state.world_profile.travel_day_length = "1天（步行）"
        world_state.world_profile.magic_tech_role = "科技与魔法对立。"
        world_state.world_profile.kingdoms = {"索朗帝国": "蒸汽帝国。"}
        world_state.world_profile.kingdom_contributors = {"测试玩家甲": ["索朗帝国"]}
        rules = RulesEngine(seed=0)
        manager = SessionZeroManager(world_state)
        app = SceneOrchestrator(
            action_brain=HeuristicActionBrain(),
            character_manager=characters,
            clock_manager=clocks,
            conflict_manager=conflict,
            world_state=world_state,
            interceptor=ActionInterceptor(rules, characters, clocks, conflict, world_state),
            expressor=Expressor(),
            scene_manager=SceneManager(),
            session_zero_manager=manager,
            session_zero_facilitator=HeuristicSessionZeroFacilitator(),
        )

        response = app.discuss_session_zero("测试玩家甲", "创建世界还缺什么？")

        self.assertEqual([participant.name for participant in manager.state.participants], ["测试玩家甲", "loading"])
        self.assertIn("仍有玩家可以补", response.message)
        self.assertNotIn("我先记下这个想法", response.message)
        self.assertNotIn("还需要轮流确认", response.message)
        self.assertNotIn("下一步先处理", response.message)
        self.assertEqual(manager.state.participants[0].contributions, [])


if __name__ == "__main__":
    unittest.main()
