import json
import unittest

from fu_gm.components.session_prep_concretizer import SessionPrepConcretizer
from fu_gm.models import (
    SessionClueRoute,
    SessionDramaticContract,
    SessionNPCRole,
    SessionSceneOpportunity,
)


class PrepClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls = 0

    def create_chat_completion(self, **_kwargs) -> str:
        self.calls += 1
        return json.dumps(self.payload, ensure_ascii=False)


class SequencedPrepClient:
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self.payloads = payloads
        self.calls = 0

    def create_chat_completion(self, **_kwargs) -> str:
        index = min(self.calls, len(self.payloads) - 1)
        self.calls += 1
        return json.dumps(self.payloads[index], ensure_ascii=False)


class FailingRepairPrepClient:
    def __init__(self, initial_payload: dict[str, object]) -> None:
        self.initial_payload = initial_payload
        self.calls = 0

    def create_chat_completion(self, **_kwargs) -> str:
        self.calls += 1
        if self.calls == 1:
            return json.dumps(self.initial_payload, ensure_ascii=False)
        raise TimeoutError("focused gatekeeper repair timed out")


class AlwaysFailingPrepClient:
    def __init__(self) -> None:
        self.calls = 0

    def create_chat_completion(self, **_kwargs) -> str:
        self.calls += 1
        raise TimeoutError("session prep provider timed out")


def base_contract() -> SessionDramaticContract:
    return SessionDramaticContract(
        session_number=1,
        title="第01场·失名旅人的归路",
        location="白花碑驿站",
        dramatic_question="英雄能否改变失名旅人的处境？",
        opening_disruption="现场出现变化。",
        signature_image="选定一件具体事物作为标志画面。",
        opposition_goal="财团要带走旅人。",
        reversal="旧说法并不完整。",
        important_npcs=[SessionNPCRole(name="守望会会长", goal_now="守住旧路")],
        clue_routes=[
            SessionClueRoute(route_id=f"s01-route-{index}", approach="调查")
            for index in range(1, 4)
        ],
        potential_scenes=[
            SessionSceneOpportunity(
                scene_key=f"s01-scene-{index}",
                scene_role=role,
                title=role,
            )
            for index, role in enumerate(
                ("strong_start", "alternate_approach", "climax_candidate"),
                start=1,
            )
        ],
    )


class SessionPrepConcretizerTests(unittest.TestCase):
    def test_required_chapter_cast_separates_named_npc_from_visible_props(self) -> None:
        visible, names = SessionPrepConcretizer._required_scene_cast(
            ["白花风铃", "失忆旅人", "白花守望会会长"],
            [
                SessionNPCRole(
                    name="岚栖会长",
                    public_role="白花守望会会长",
                )
            ],
        )

        self.assertEqual(visible, ["白花风铃", "失忆旅人"])
        self.assertEqual(names, ["岚栖会长"])

    def _payload(self) -> dict[str, object]:
        return {
            "title": "迟响的白花铃",
            "dramatic_question": "旧路开启前，谁愿意为旅人的名字承担风险？",
            "opening_disruption": "巡守弥珂把一枚染血的铜钥匙拍在柜台上，门外第三盏路灯同时熄灭。",
            "signature_image": "无风的门廊里，白花风铃迟响一拍，铃舌上沾着一滴逆流的雨。",
            "opposition_goal": "巡守长弥珂要保住旧路名单，财团搜查队要在午夜前封门。",
            "dilemma": "交出通行牌能立刻开门，却会暴露托伦的逃亡路线；拒绝则必须另找办法。",
            "reversal_evidence": "铜钥匙齿槽里的灰晶粉证明财团昨夜已经使用过旧路。",
            "irreversible_change": "高潮后，旧路名单、驿站中立地位或旅人的去向至少有一项被永久改变。",
            "closure_requirement": "本场结束前明确旧路是否开放，以及驿站为此付出的代价。",
            "ending_echo": "结尾同一只风铃再次响起，铃舌上的雨滴会因队伍选择落下或冻结。",
            "memory_anchor": "迟响风铃的画面；是否交出通行牌的选择；驿站中立地位被改变的后果。",
            "fantastic_details": [
                "旧钟铜锈闻起来像雨前泥土。",
                "灰晶粉在谎言附近会逆着纹路爬行。",
                "门廊白花只朝被遗忘姓名的人转动。",
            ],
            "escalation_ladder": [
                "巡守长弥珂锁住正门，只留旧路谈判。",
                "财团熄灭最后两盏路灯并切断驿站传讯。",
                "失忆旅人无意识念出钥匙原主人的名字。",
            ],
            "possible_payoffs": [
                "旧路正式开放。",
                "弥珂成为可以求援的联系人。",
                "财团失去一条秘密路线。",
            ],
            "npcs": [
                {
                    "name": "巡守长弥珂",
                    "public_role": "白花守望会夜巡长",
                    "goal_now": "在不泄露完整名单的前提下送旅人离开",
                    "leverage": "持有旧路铜钥匙",
                    "authority_scope": "可以开启驿站旧路侧门，但无权决定钟鸣公国是否接纳旅人",
                    "concrete_demand": "查看伊莉雅随身的托伦铜质通行牌，并抄下牌背编号作为担保",
                    "acceptance_rule": "通行牌真实且允许抄号后，立即打开旧路侧门",
                    "promised_result": "立即打开白花碑驿站的旧路侧门，并亲自带到第一处岔道",
                    "refusal_move": "若被拒绝，弥珂锁死侧门并带旅人转移到钟仓",
                    "voice_cue": "短句，先说条件再说理由",
                    "private_secret": "她知道财团昨夜已经用复制钥匙走过旧路",
                    "if_helped": "亲自带路并说明第一处岔道",
                    "if_blocked": "先保旅人，不与英雄争辩",
                }
            ],
            "clues": [
                {
                    "approach": "检验物证",
                    "source": "染血铜钥匙的齿槽",
                    "visible_lead": "齿槽里卡着只用于财团记忆炉的灰晶粉",
                    "success_reveal": "粉末磨损方向表明复制钥匙昨夜从旧路内侧拔出",
                    "fallback": "粉末被雨水冲散，但能追到负责清洗钥匙的钟仓",
                },
                {
                    "approach": "取得口供",
                    "source": "巡守长弥珂",
                    "visible_lead": "她每提到昨夜巡表就会摸一下空白行",
                    "success_reveal": "她承认巡表少了一名本应值夜的巡守",
                    "fallback": "她拒绝说姓名，却给出失踪巡守的铜哨",
                },
                {
                    "approach": "读取钟声回响",
                    "source": "门廊旧钟的第七道裂纹",
                    "visible_lead": "裂纹会在灰晶靠近时重复昨夜最后一次开门声",
                    "success_reveal": "回响里有财团制式靴跟与复制钥匙同时落地的声音",
                    "fallback": "回响断裂，只留下钟仓方向的一段脚步",
                },
            ],
            "scenes": [
                {
                    "scene_role": "strong_start",
                    "title": "第三盏灯熄灭",
                    "location": "白花碑驿站门廊",
                    "situation": "弥珂拍下染血铜钥匙，要求在搜查队抵达前决定担保。",
                    "purpose": "把旧路条件和逼近压力同时摆到现场",
                    "pressure": "财团正在逐盏熄灭路灯",
                    "entry_points": ["核验钥匙", "回应担保条件", "保护旅人"],
                    "possible_changes": ["侧门开放", "旅人被转移"],
                    "npc_names": ["巡守长弥珂"],
                },
                {
                    "scene_role": "alternate_approach",
                    "title": "钟仓的空白巡表",
                    "location": "白花碑驿站钟仓",
                    "situation": "一行被刮空的巡表与仍温热的清洗盆留在旧钟下。",
                    "purpose": "允许从物证或仪式确认昨夜入侵",
                    "pressure": "进入钟仓会暂时离开门廊",
                    "entry_points": ["调查巡表", "读取钟声"],
                    "possible_changes": ["证明复制钥匙存在", "找到失踪巡守"],
                    "npc_names": ["巡守长弥珂"],
                },
                {
                    "scene_role": "climax_candidate",
                    "title": "旧路与正门",
                    "location": "白花碑驿站双门厅",
                    "situation": "搜查队撞响正门时，旧路侧门只能从厅内保持开启。",
                    "purpose": "让队伍决定谁离开、谁留下以及驿站是否暴露",
                    "pressure": "两扇门不能同时守住",
                    "entry_points": ["谈判", "守门", "撤离"],
                    "possible_changes": ["驿站公开站队", "旧路永久封闭"],
                    "npc_names": ["巡守长弥珂"],
                },
            ],
        }

    def test_concretizes_npc_contract_clues_and_movable_scenes(self) -> None:
        client = PrepClient(self._payload())
        concrete = SessionPrepConcretizer(client=client, model="fake").concretize(
            base_contract(),
            world_context={"location": "白花碑驿站"},
        )

        self.assertEqual(client.calls, 2)
        self.assertEqual(concrete.title, "迟响的白花铃")
        self.assertEqual(concrete.important_npcs[0].name, "巡守长弥珂")
        self.assertIn("通行牌", concrete.important_npcs[0].concrete_demand)
        self.assertIn("无权决定", concrete.important_npcs[0].authority_scope)
        self.assertIn("打开", concrete.important_npcs[0].promised_result)
        self.assertEqual(len(concrete.clue_routes), 3)
        self.assertTrue(all(route.conclusion == concrete.reversal for route in concrete.clue_routes))
        self.assertEqual(len(concrete.potential_scenes), 4)
        self.assertEqual(concrete.potential_scenes[-1].scene_role, "aftermath")
        self.assertEqual(len({item.location for item in concrete.potential_scenes}), 4)
        self.assertEqual(
            concrete.potential_scenes[0].clue_route_ids,
            [route.route_id for route in concrete.clue_routes],
        )
        self.assertIn("永久改变", concrete.irreversible_change)
        self.assertIn("风铃", concrete.ending_echo)

    def test_non_transactional_npc_is_preserved_without_fake_bargain(self) -> None:
        payload = self._payload()
        payload["npcs"].append(
            {
                "name": "阿缇娅",
                "public_role": "失忆旅人",
                "goal_now": "弄清白花风铃为何记得她的名字",
                "authority_scope": "只能说明自己的记忆与感受，不能替守望会开放旧路",
                "voice_cue": "回答前会先确认风铃是否仍在响",
            }
        )

        concrete = SessionPrepConcretizer(
            client=PrepClient(payload),
            model="fake",
        ).concretize(base_contract(), world_context={})

        traveler = next(item for item in concrete.important_npcs if item.name == "阿缇娅")
        self.assertEqual(traveler.public_role, "失忆旅人")
        self.assertEqual(traveler.concrete_demand, "")
        self.assertEqual(traveler.acceptance_rule, "")
        self.assertEqual(traveler.promised_result, "")

    def test_partial_bargain_is_cleared_instead_of_becoming_an_open_condition(self) -> None:
        payload = self._payload()
        payload["npcs"][0]["promised_result"] = ""

        concrete = SessionPrepConcretizer(
            client=PrepClient(payload),
            model="fake",
        ).concretize(base_contract(), world_context={})

        npc = concrete.important_npcs[0]
        self.assertEqual(npc.name, "巡守长弥珂")
        self.assertEqual(npc.concrete_demand, "")
        self.assertEqual(npc.acceptance_rule, "")
        self.assertEqual(npc.promised_result, "")

    def test_reluctant_gatekeeper_missing_a_contract_gets_focused_llm_repair(self) -> None:
        contract = base_contract()
        contract.opening_disruption = (
            "失忆旅人听见自己的名字，守望会会长不愿轻易开放旧路。"
        )
        contract.dramatic_question = "英雄能否争取守望会开放旧路？"
        payload = self._payload()
        payload["opening_disruption"] = contract.opening_disruption
        payload["npcs"] = [
            {
                "name": "守望会会长",
                "public_role": "守望会会长",
                "goal_now": "确认英雄不会把追兵直接带进旧路",
                "leverage": "旧路的临时通行权",
                "authority_scope": "能决定旧路是否开放并安排巡守带路",
                "refusal_move": "关闭侧门并把旅人转移到钟仓",
            }
        ]
        repair = {
            "npcs": [
                {
                    "name": "守望会会长",
                    "concrete_demand": "说明旅人的去向，并由一名英雄承担沿途护送责任",
                    "acceptance_rule": "队伍说清目的地，且至少一名英雄当面承诺全程护送",
                    "promised_result": "立即开放旧路侧门，并派一名巡守带到第一处界碑",
                }
            ]
        }
        client = SequencedPrepClient([payload, repair])

        concrete = SessionPrepConcretizer(client=client, model="fake").concretize(
            contract,
            world_context={"location": "白花碑驿站"},
        )

        self.assertEqual(client.calls, 3)
        gatekeeper = next(item for item in concrete.important_npcs if "会长" in item.name)
        self.assertIn("旅人的去向", gatekeeper.concrete_demand)
        self.assertIn("至少一名英雄", gatekeeper.acceptance_rule)
        self.assertIn("开放旧路侧门", gatekeeper.promised_result)

    def test_focused_repair_binds_a_shortened_public_title_to_the_full_npc_name(self) -> None:
        contract = base_contract()
        contract.opening_disruption = "守望会会长不愿轻易开放旧路。"
        contract.dramatic_question = "英雄能否争取旧路通行？"
        payload = self._payload()
        payload["opening_disruption"] = contract.opening_disruption
        payload["npcs"] = [
            {
                "name": "白花守望会会长",
                "public_role": "白花守望会会长",
                "goal_now": "决定旧路是否开放以及风险由谁承担",
                "authority_scope": "能决定旧路临时开放并安排巡守带路",
                "refusal_move": "维持旧路警戒并要求说明去向",
            }
        ]
        repair = {
            "npcs": [
                {
                    "name": "守望会会长",
                    "concrete_demand": "说明失忆旅人的去向并安排护送责任",
                    "acceptance_rule": "目的地明确，且有一人承担护送或提出等价安全安排",
                    "promised_result": "立即开放旧路并安排巡守带过第一处界碑",
                }
            ]
        }
        concretizer = SessionPrepConcretizer(
            client=SequencedPrepClient([payload, repair]),
            model="fake",
        )

        concrete = concretizer.concretize(contract, world_context={})

        gatekeeper = next(item for item in concrete.important_npcs if "会长" in item.name)
        self.assertIn("失忆旅人的去向", gatekeeper.concrete_demand)
        self.assertIn("等价安全安排", gatekeeper.acceptance_rule)
        self.assertEqual(concretizer.last_gatekeeper_repair_status, "repaired_by_llm")

    def test_route_gate_gets_a_bounded_fallback_when_semantic_repair_times_out(self) -> None:
        contract = base_contract()
        contract.opening_disruption = "守望会会长不愿轻易开放旧路。"
        contract.dramatic_question = "英雄能否取得旧路通行并护送失忆旅人？"
        contract.closure_requirement = "本场结束前明确旧路是否开放。"
        payload = self._payload()
        payload["opening_disruption"] = contract.opening_disruption
        payload["dramatic_question"] = contract.dramatic_question
        payload["closure_requirement"] = contract.closure_requirement
        payload["npcs"] = [
            {
                "name": "白花守望会会长",
                "public_role": "白花守望会会长",
                "goal_now": "决定旧路是否开放以及风险由谁承担",
                "authority_scope": "能决定旧路临时开放并安排巡守带路",
                "refusal_move": "维持旧路警戒并要求说明去向与责任",
            }
        ]
        client = FailingRepairPrepClient(payload)
        concretizer = SessionPrepConcretizer(client=client, model="fake")

        concrete = concretizer.concretize(contract, world_context={})

        gatekeeper = next(item for item in concrete.important_npcs if "会长" in item.name)
        self.assertIn("明确去向", gatekeeper.concrete_demand)
        self.assertIn("或", gatekeeper.acceptance_rule)
        self.assertIn("开放旧路", gatekeeper.promised_result)
        self.assertEqual(client.calls, 4)
        self.assertEqual(
            concretizer.last_gatekeeper_repair_status,
            "fallback_after_llm_failure",
        )
        self.assertIn("TimeoutError", concretizer.last_gatekeeper_repair_error)

    def test_main_prep_failure_still_runs_the_gatekeeper_completion_boundary(self) -> None:
        contract = base_contract()
        contract.opening_disruption = "守望会会长不愿轻易开放旧路。"
        contract.dramatic_question = "英雄能否取得旧路通行并护送失忆旅人？"
        contract.closure_requirement = "本场结束前明确旧路是否开放。"
        contract.important_npcs = [
            SessionNPCRole(
                name="白花守望会会长",
                public_role="白花守望会会长",
                goal_now="决定旧路是否开放以及风险由谁承担",
                authority_scope="能决定旧路临时开放并安排巡守带路",
                refusal_move="维持旧路警戒并要求说明去向与责任",
            )
        ]
        client = AlwaysFailingPrepClient()
        concretizer = SessionPrepConcretizer(client=client, model="fake")

        concrete = concretizer.concretize(contract, world_context={})

        gatekeeper = concrete.important_npcs[0]
        self.assertIn("明确去向", gatekeeper.concrete_demand)
        self.assertIn("或", gatekeeper.acceptance_rule)
        self.assertIn("开放旧路", gatekeeper.promised_result)
        self.assertEqual(client.calls, 4)
        self.assertIn("session prep provider timed out", concretizer.last_error)
        self.assertEqual(
            concretizer.last_gatekeeper_repair_status,
            "fallback_after_llm_failure",
        )

    def test_no_llm_still_keeps_a_confirmed_route_gate_playable(self) -> None:
        contract = base_contract()
        contract.opening_disruption = "守望会会长不愿轻易开放旧路。"
        contract.dramatic_question = "英雄能否取得旧路通行？"
        contract.important_npcs = [
            SessionNPCRole(
                name="白花守望会会长",
                public_role="白花守望会会长",
                goal_now="决定旧路是否开放",
                authority_scope="能决定旧路临时开放和放行条件",
            )
        ]
        concretizer = SessionPrepConcretizer(client=None, model="")

        concrete = concretizer.concretize(contract, world_context={})

        self.assertTrue(concrete.important_npcs[0].concrete_demand)
        self.assertTrue(concrete.important_npcs[0].acceptance_rule)
        self.assertTrue(concrete.important_npcs[0].promised_result)
        self.assertEqual(
            concretizer.last_gatekeeper_repair_status,
            "fallback_no_model",
        )

    def test_shortened_gatekeeper_title_and_structured_session_goal_trigger_repair(self) -> None:
        contract = base_contract()
        contract.opening_disruption = "守望会会长尚未决定是否放行，远处车轮声正在接近。"
        contract.dramatic_question = "英雄能否在本场取得旧路通行？"
        contract.closure_requirement = "本场结束前明确旧路是否开放。"
        contract.important_npcs = [
            SessionNPCRole(
                name="白花守望会会长",
                public_role="白花守望会会长",
                goal_now="决定旧路是否开放，以及由谁承担风险",
                authority_scope="能决定旧路临时开放、放行条件与巡守带路",
                refusal_move="维持旧路警戒，并要求说明去向与责任归属",
            )
        ]

        missing = SessionPrepConcretizer._missing_gatekeeper_contracts(contract)

        self.assertEqual([item.name for item in missing], ["白花守望会会长"])

    def test_structured_gate_goal_finds_controller_even_without_exact_name_in_opening(self) -> None:
        contract = base_contract()
        contract.opening_disruption = "远处的财团车灯已经越过南岸界碑。"
        contract.dramatic_question = "英雄能否获得旧路通行并护送旅人离开？"
        contract.closure_requirement = "取得旧路许可，或让不开放旧路的后果落地。"
        contract.important_npcs = [
            SessionNPCRole(
                name="岚栖",
                public_role="白花守望会会长",
                goal_now="决定旧路是否开放，并确定风险由谁承担",
                authority_scope="掌握旧路许可和巡守带路安排",
                refusal_move="维持旧路警戒，要求来者先说明去向与责任",
            ),
            SessionNPCRole(
                name="失忆旅人",
                public_role="失忆旅人",
                goal_now="留在可信任的人身边",
                authority_scope="只能决定自己是否跟随，不能开放旧路",
            ),
        ]

        missing = SessionPrepConcretizer._missing_gatekeeper_contracts(contract)

        self.assertEqual([item.name for item in missing], ["岚栖"])

    def test_witness_or_traveller_never_gets_a_forced_gatekeeper_bargain(self) -> None:
        contract = base_contract()
        contract.opening_disruption = "失忆旅人不愿轻易说出旧路的记忆。"
        contract.important_npcs = [
            SessionNPCRole(
                name="失忆旅人",
                public_role="失忆旅人",
                goal_now="留在可信任的人身边",
                authority_scope="只能决定是否说出自己的记忆，不能开放旧路",
            )
        ]

        missing = SessionPrepConcretizer._missing_gatekeeper_contracts(contract)

        self.assertEqual(missing, [])

    def test_required_chapter_npc_gets_a_minimal_non_transactional_profile(self) -> None:
        payload = self._payload()
        payload["npcs"] = payload["npcs"][:1]
        chapter = {
            "chapter_title": "迟响的白花铃",
            "scenes": [
                {
                    "title": "风铃廊问路",
                    "scene_type": "social_conflict",
                    "location": "白花碑驿站·风铃廊",
                    "purpose": "决定是否开放旧路",
                    "required_elements": ["白花风铃", "失忆旅人", "白花守望会会长"],
                },
                {
                    "title": "旧钟回声",
                    "scene_type": "investigation",
                    "location": "白花碑驿站·钟仓",
                    "purpose": "确认迟响来自哪里",
                    "required_elements": ["第七道钟裂"],
                },
                {
                    "title": "旧路闸门",
                    "scene_type": "conflict",
                    "location": "白花碑驿站·旧路闸门",
                    "purpose": "决定旅人的去向",
                    "required_elements": ["旧路闸门"],
                },
            ],
        }

        concrete = SessionPrepConcretizer(
            client=PrepClient(payload),
            model="fake",
        ).concretize(
            base_contract(),
            world_context={
                "location": "白花碑驿站",
                "active_chapter_package": chapter,
            },
        )

        names = [item.name for item in concrete.important_npcs]
        self.assertIn("失忆旅人", names)
        traveler = next(item for item in concrete.important_npcs if item.name == "失忆旅人")
        self.assertEqual(traveler.concrete_demand, "")
        self.assertIn("不能替其他人物", traveler.authority_scope)
        chair = next(
            item for item in concrete.important_npcs if "会长" in item.name
        )
        self.assertIn("旧路", chair.goal_now)
        self.assertIn("放行条件", chair.authority_scope)
        self.assertNotEqual(
            chair.goal_now,
            "在当前局势中保护自己负有责任的人、地点或职责",
        )
        self.assertIn("失忆旅人", concrete.potential_scenes[0].required_npc_names)
        self.assertNotIn("失忆旅人", concrete.potential_scenes[0].required_elements)

    def test_chapter_concretization_preserves_locations_and_appends_aftermath(self) -> None:
        contract = base_contract()
        contract.potential_scenes.append(
            SessionSceneOpportunity(
                scene_key="s01-chapter-aftermath",
                scene_role="aftermath",
                title="旧路闸门之后",
                location="白花碑驿站·旧路出口外",
                situation="闸门一役的结果已经落地。",
            )
        )
        chapter_scenes = [
            {
                "title": "风铃廊问路",
                "scene_type": "social_conflict",
                "location": "白花碑驿站·风铃廊",
                "purpose": "取得旧路通行条件",
                "required_elements": ["白花风铃"],
            },
            {
                "title": "风铃回声仪式",
                "scene_type": "ritual",
                "location": "白花碑驿站·登记小室",
                "purpose": "确认复制钥匙的回声",
                "required_elements": ["第七道钟裂"],
            },
            {
                "title": "旧路闸门与巡逻队",
                "scene_type": "conflict",
                "location": "白花碑驿站·旧路闸门",
                "purpose": "在封锁前决定旅人的去向",
                "required_elements": ["旧路闸门"],
            },
        ]

        concrete = SessionPrepConcretizer(
            client=PrepClient(self._payload()),
            model="fake",
        ).concretize(
            contract,
            world_context={
                "location": "白花碑驿站",
                "active_chapter_package": {
                    "chapter_title": "迟响的白花铃",
                    "scenes": chapter_scenes,
                },
            },
        )

        self.assertEqual(len(concrete.potential_scenes), 4)
        self.assertEqual(
            [scene.scene_role for scene in concrete.potential_scenes],
            [
                "strong_start",
                "alternate_approach",
                "climax_candidate",
                "aftermath",
            ],
        )
        self.assertEqual(
            [scene.location for scene in concrete.potential_scenes],
            [
                "白花碑驿站·风铃廊",
                "白花碑驿站·登记小室",
                "白花碑驿站·旧路闸门",
                "白花碑驿站·旧路出口外",
            ],
        )

    def test_generic_npc_and_placeholder_sections_do_not_replace_stable_base(self) -> None:
        payload = self._payload()
        payload["signature_image"] = "选定一件具体事物作为标志画面"
        payload["npcs"] = [
            {
                "name": "现场关键人物",
                "goal_now": "继续推进当前局面",
                "concrete_demand": "交出某种担保",
                "acceptance_rule": "根据玩家方法判断",
            }
        ]
        contract = base_contract()

        concrete = SessionPrepConcretizer(
            client=PrepClient(payload),
            model="fake",
        ).concretize(contract, world_context={})

        self.assertEqual(concrete.signature_image, contract.signature_image)
        self.assertEqual(concrete.important_npcs[0].name, "守望会会长")

    def test_report_style_world_threat_and_meta_image_are_rejected(self) -> None:
        payload = self._payload()
        payload["signature_image"] = (
            "在【白花碑驿站】选定一件相关、可被触碰或改变的具体事物；"
            "首次出镜时固定其感官细节。"
        )
        payload["npcs"] = [
            {
                "name": "世界威胁",
                "public_role": "对立方或其现场代理人",
                "goal_now": "监察官艾蕾娜应主动推进财团目标",
            }
        ]
        payload["clues"][1]["source"] = "世界威胁"
        for scene in payload["scenes"]:
            scene["npc_names"] = ["世界威胁"]
        contract = base_contract()
        contract.signature_image = (
            "白花碑驿站门廊下，一枚白花风铃无风自响；"
            "瓷面凝着潮盐，铃舌每次都比四周慢半拍。"
        )

        concrete = SessionPrepConcretizer(
            client=PrepClient(payload),
            model="fake",
        ).concretize(contract, world_context={})

        self.assertEqual(concrete.signature_image, contract.signature_image)
        self.assertEqual([item.name for item in concrete.important_npcs], ["守望会会长"])
        self.assertTrue(
            all("世界威胁" not in scene.npc_names for scene in concrete.potential_scenes)
        )
        self.assertTrue(
            all("世界威胁" not in route.source for route in concrete.clue_routes)
        )

    def test_story_proposition_cannot_be_persisted_as_an_npc_name(self) -> None:
        payload = self._payload()
        payload["npcs"].append(
            {
                "name": "监察官艾蕾娜曾是赤羽遗民；她认为记忆必须被集中保管",
                "public_role": "监察官",
                "goal_now": "带走失忆旅人",
            }
        )

        concrete = SessionPrepConcretizer(
            client=PrepClient(payload),
            model="fake",
        ).concretize(base_contract(), world_context={})

        self.assertTrue(all("；" not in item.name for item in concrete.important_npcs))
        self.assertNotIn(
            "监察官艾蕾娜曾是赤羽遗民；她认为记忆必须被集中保管",
            [item.name for item in concrete.important_npcs],
        )

    def test_story_proposition_without_punctuation_is_trimmed_to_npc_identity(self) -> None:
        payload = self._payload()
        payload["npcs"].append(
            {
                "name": "监察官艾蕾娜曾是赤羽遗民",
                "public_role": "监察官",
                "goal_now": "带走失忆旅人",
            }
        )

        concrete = SessionPrepConcretizer(
            client=PrepClient(payload),
            model="fake",
        ).concretize(base_contract(), world_context={})

        names = [item.name for item in concrete.important_npcs]
        self.assertIn("监察官艾蕾娜", names)
        self.assertNotIn("监察官艾蕾娜曾是赤羽遗民", names)

    def test_scene_locations_stay_inside_current_public_location(self) -> None:
        payload = self._payload()
        payload["scenes"][0]["location"] = "门廊"
        payload["scenes"][1]["location"] = "噬神古林"

        concrete = SessionPrepConcretizer(
            client=PrepClient(payload),
            model="fake",
        ).concretize(
            base_contract(),
            world_context={
                "location": "白花碑驿站",
                "allowed_locations": ["白花碑驿站", "钟鸣公国"],
                "forbidden_backstage_locations": ["噬神古林"],
            },
        )

        self.assertEqual(concrete.potential_scenes[0].location, "白花碑驿站·门廊")
        self.assertEqual(concrete.potential_scenes[1].location, "白花碑驿站")
        self.assertNotIn("噬神古林", json.dumps(concrete, default=lambda value: value.__dict__, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
