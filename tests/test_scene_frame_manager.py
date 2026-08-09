from __future__ import annotations

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.scene_frame_manager import SceneFrame, SceneFrameManager
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.session_ledger import SessionLedger
from fu_gm.components.world_state import WorldState
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import (
    Action,
    ActionResolution,
    ActionType,
    ChapterPackage,
    Character,
    ClockChange,
    NPCPersona,
    RollOutcome,
    SceneRecord,
    SceneType,
    SessionClueRoute,
    SessionDramaticContract,
    SessionNPCRole,
    SessionSceneOpportunity,
)
from fu_gm.scene_orchestrator import SceneOrchestrator
from fu_gm.turn_pipeline import TurnReplyContext


class FixedBrain:
    def decide(self, panel):
        return Action(ActionType.NARRATE, {"summary": "镜头继续。"})


class FixedExpressor:
    def render(self, resolution: ActionResolution) -> str:
        return str(resolution.payload.get("summary") or resolution.action.parameters.get("summary") or "ok")


def _world_with_scene_seeds() -> WorldState:
    world = WorldState()
    profile = world.world_profile
    profile.group_concept = "护送失忆旅人穿过白钟大陆"
    profile.selected_first_act_summary = "旅人胸口的白钟印记开始倒数，财团与守望会都想先一步夺走答案。"
    profile.major_locations["白花碑驿站"] = "建在白色钟碑旁的边境驿站，晶炉钟声会扰动记忆。"
    profile.factions["辉钢财团"] = "用契约和魔导账本收购他人的记忆。"
    profile.mysteries.append("白钟印记为什么会在失忆旅人身上倒数。")
    profile.world_threats.append("辉钢财团正在扩张记忆收购网络。")
    profile.gm_secret_notes.append("钟匠其实替守望会藏起了一枚能倒转钟声的旧钥匙。")
    world.subject_facts["白花碑驿站"] = ["昨夜有人擦掉了货栈门口的财团封蜡。"]
    world.npc_personas["钟匠阿瑟"] = NPCPersona(
        name="钟匠阿瑟",
        public_identity="白花碑驿站的钟匠",
        role_in_story="知情者",
        core_drive="保护驿站旧规矩",
        first_scene="白花碑驿站",
        secrets=["他知道晶炉钟声能短暂唤回被收购的记忆。"],
    )
    return world


def test_current_location_supplies_local_gatekeeper_without_chat_repeat() -> None:
    manager = SceneFrameManager()
    frame = manager.ensure_frame(
        scene=SceneRecord(
            name="第一章：白花碑驿站",
            scene_type=SceneType.STANDARD,
            location="白花碑驿站",
        ),
        recent_chat="冒险刚刚开始。",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )

    assert any("白花守望会" in item for item in frame.npc_functions)


def test_cross_scene_anchor_updates_live_routing_location_before_next_frame() -> None:
    manager = SceneFrameManager()
    manager.current_frame = SceneFrame(
        scene_key="白花碑驿站·风铃廊",
        scene_name="风铃廊",
        location="白花碑驿站·风铃廊",
    )

    changed = manager.synchronize_current_location("白花碑驿站·登记小室")

    assert changed is True
    assert manager.routing_context()["location"] == "白花碑驿站·登记小室"
    assert manager.synchronize_current_location("白花碑驿站·登记小室") is False


def test_same_location_frame_coalescing_preserves_public_and_pending_state() -> None:
    manager = SceneFrameManager()
    manager.current_frame = SceneFrame(
        scene_key="scene-4|登记小室",
        scene_name="登记小室查册",
        source_scene_id="scene-4",
        location="白花碑驿站·登记小室",
        premise="当前镜头的局面",
        public_facts=["财团巡逻队已经抵达门外。"],
        pending_npc_questions=[
            {
                "question_id": "q-current",
                "npc": "财团巡逻队",
                "kind": "player_response",
                "status": "open",
            }
        ],
    )
    manager.suspended_frames["scene-3"] = SceneFrame(
        scene_key="scene-3|登记小室",
        scene_name="登记小室查册",
        source_scene_id="scene-3",
        location="白花碑驿站·登记小室",
        premise="不应覆盖当前镜头的旧局面",
        public_facts=["洛岚已经找到空白的完成栏。"],
        revealed_clues=["册页边缘留有新鲜辉钢粉。"],
        pending_npc_questions=[
            {
                "question_id": "q-old",
                "npc": "守册人",
                "kind": "identity_check",
                "status": "open",
            }
        ],
        history_reconciliation_markers={"turn-1": "reconciled"},
    )

    merged = manager.coalesce_suspended_frames("scene-4", ["scene-3"])

    assert merged is manager.current_frame
    assert merged.premise == "当前镜头的局面"
    assert merged.public_facts == [
        "财团巡逻队已经抵达门外。",
        "洛岚已经找到空白的完成栏。",
    ]
    assert merged.revealed_clues == ["册页边缘留有新鲜辉钢粉。"]
    assert {item["question_id"] for item in merged.pending_npc_questions} == {
        "q-current",
        "q-old",
    }
    assert merged.history_reconciliation_markers == {"turn-1": "reconciled"}
    assert manager.suspended_frames == {}


def test_loaded_same_source_resolved_question_supersedes_legacy_open_duplicate() -> None:
    manager = SceneFrameManager()
    manager.current_frame = SceneFrame(
        scene_key="scene-4|登记小室",
        scene_name="登记小室查册",
        source_scene_id="scene-4",
        location="白花碑驿站·登记小室",
        pending_npc_questions=[
            {
                "question_id": "q-resolved",
                "npc": "白花守望会会长",
                "kind": "player_response",
                "addressed_actor": "",
                "summary": "你们决定留下、查册，还是先争取旧路",
                "speaker_evidence": "你们决定留下、查册，还是先争取旧路；我只在你们作出选择后执行安排。",
                "status": "resolved",
                "resolved_by": "伊莉雅",
            },
            {
                "question_id": "q-open-duplicate",
                "npc": "白花守望会会长",
                "kind": "player_response",
                "addressed_actor": "",
                "summary": "留下、查册、先争取旧路",
                "speaker_evidence": "你们决定留下、查册，还是先争取旧路",
                "status": "open",
            },
            {
                "question_id": "q-other",
                "npc": "白花守望会会长",
                "kind": "player_response",
                "addressed_actor": "",
                "summary": "谁负责护送旅人",
                "speaker_evidence": "请告诉我谁负责护送旅人。",
                "status": "open",
            },
        ],
    )

    manager.normalize_loaded_state()

    duplicate = next(
        item
        for item in manager.current_frame.pending_npc_questions
        if item["question_id"] == "q-open-duplicate"
    )
    assert duplicate["status"] == "superseded"
    assert duplicate["superseded_by"] == "q-resolved"
    assert manager.latest_pending_npc_question()["question_id"] == "q-other"


def test_routing_context_exposes_only_open_npc_response_obligations() -> None:
    manager = SceneFrameManager()
    manager.current_frame = SceneFrame(
        scene_key="白花碑驿站·风铃廊",
        scene_name="风铃廊",
        location="白花碑驿站·风铃廊",
        pending_npc_questions=[
            {
                "question_id": "q-open",
                "npc": "白花守望会会长",
                "addressed_actor": "",
                "kind": "player_response",
                "summary": "继续追问还是维持现有安排",
                "required_items": (
                    '[{"item_id":"choice","prompt":"继续追问还是维持现有安排"}]'
                ),
                "answered_item_ids": "[]",
                "status": "open",
            },
            {
                "question_id": "q-closed",
                "npc": "监察官艾蕾娜",
                "addressed_actor": "伊莉雅",
                "kind": "player_response",
                "summary": "是否接受登记",
                "required_items": (
                    '[{"item_id":"registration","prompt":"是否接受登记"}]'
                ),
                "answered_item_ids": '["registration"]',
                "status": "resolved",
            },
        ],
    )

    context = manager.routing_context()

    assert context["pending_npc_questions"] == [
        {
            "question_id": "q-open",
            "npc": "白花守望会会长",
            "addressed_actor": "",
            "summary": "继续追问还是维持现有安排",
            "remaining_items": [
                {
                    "item_id": "choice",
                    "prompt": "继续追问还是维持现有安排",
                }
            ],
        }
    ]
    assert "白花守望会会长" in context["known_npcs"]
    assert "监察官艾蕾娜" not in context["known_npcs"]


def test_session_situation_pack_is_attached_to_scene_without_becoming_fixed_plot() -> None:
    manager = SceneFrameManager()
    contract = SessionDramaticContract(
        session_number=1,
        title="第01场·失名旅人的归路",
        location="白花碑驿站",
        dramatic_question="英雄能否在归潮前替旅人找回去路？",
        potential_scenes=[
            SessionSceneOpportunity(
                scene_key="s01-strong-start",
                scene_role="strong_start",
                title="后门上的财团封蜡",
                location="白花碑驿站",
                situation="封蜡正沿门缝自行生长。",
                purpose="把一个立即可回应的局面交给英雄",
                pressure="巡逻队正在接近",
                entry_points=["检查封蜡", "保护旅人"],
                possible_changes=["保住后门", "惊动财团"],
                required_elements=["白花风铃", "失忆旅人"],
                required_npc_names=["守望会会长"],
                optional=False,
            ),
            SessionSceneOpportunity(
                scene_key="s01-choice",
                title="旧路闸门的条件",
                location="白花碑驿站",
                situation="会长不会无条件交出钥匙。",
                purpose="让人际选择改变路线",
                pressure="归潮将至",
                entry_points=["争取信任"],
                possible_changes=["获得带路"],
            ),
        ],
        clue_routes=[
            SessionClueRoute(
                route_id="physical",
                conclusion="封蜡来自财团巡逻队",
                approach="调查环境",
                visible_lead="封蜡里混着巡逻甲胄的辉钢粉",
                success_reveal="财团巡逻队已经标记后门",
                fallback="仍能看出封蜡不是驿站所有，但需要别的证据确认来源",
            )
        ],
        important_npcs=[
            SessionNPCRole(
                name="守望会会长",
                public_role="旧路守门人",
                goal_now="保护旧路",
                leverage="掌握钥匙",
                if_blocked="亲自封门",
            )
        ],
        fantastic_details=["白花风铃的影子正逆着月光摆动。"],
    )

    scene = SceneRecord(
        name="第一章：白花碑驿站后门",
        scene_type=SceneType.STANDARD,
        location="白花碑驿站",
    )
    frame = manager.ensure_frame(
        scene=scene,
        recent_chat="众人刚发现后门上的财团封蜡。",
        world_state=_world_with_scene_seeds(),
        character_manager=_character_manager(),
        contract=contract,
    )

    assert frame.session_opportunity_key == "s01-strong-start"
    assert frame.session_opportunity_role == "strong_start"
    assert frame.session_opportunity_purpose == "把一个立即可回应的局面交给英雄"
    assert frame.session_opportunity_situation == "封蜡正沿门缝自行生长。"
    assert scene.session_opportunity_key == "s01-strong-start"
    assert scene.session_opportunity_role == "strong_start"
    assert scene.session_opportunity_title == "后门上的财团封蜡"
    assert scene.session_opportunity_purpose == "把一个立即可回应的局面交给英雄"
    assert scene.session_opportunity_situation == "封蜡正沿门缝自行生长。"
    assert "可换序、合并或丢弃" in manager.format_for_prompt(include_private=True)
    assert any("辉钢粉" in item for item in frame.clue_pool)
    assert any("保护旧路" in item for item in frame.npc_functions)
    assert frame.session_npc_records[0]["name"] == "守望会会长"
    private_packet = manager.expression_packet(include_private=True)
    assert private_packet["required_opening_npc_names"] == ["守望会会长", "失忆旅人"]
    assert private_packet["required_opening_elements"] == ["白花风铃"]
    assert private_packet["prepared_npcs"][0]["public_role"] == "旧路守门人"
    assert private_packet["selected_scene_role"] == "strong_start"
    assert private_packet["selected_scene_purpose"] == "把一个立即可回应的局面交给英雄"
    assert frame.story_outline[0].startswith("当前只准备局面")


def test_contract_from_another_location_is_not_projected_into_current_scene() -> None:
    manager = SceneFrameManager()
    contract = SessionDramaticContract(
        session_number=1,
        title="星落尖塔的封锁",
        location="星落尖塔",
        dramatic_question="英雄能否穿过尖塔封锁？",
        signature_image="塔顶星盘一格格熄灭。",
        escalation_ladder=["尖塔守卫封死升降梯。"],
        possible_payoffs=["星盘重新点亮。"],
    )

    frame = manager.ensure_frame(
        scene=SceneRecord(
            name="第一章：卡里巴村监狱",
            scene_type=SceneType.STANDARD,
            location="卡里巴村监狱",
        ),
        recent_chat="众人仍在牢区里。",
        world_state=WorldState(),
        character_manager=CharacterManager(),
        contract=contract,
    )

    assert frame.session_title == ""
    assert frame.dramatic_question == ""
    assert frame.signature_image == ""
    assert frame.escalation_ladder == []
    assert frame.possible_payoffs == []


def test_saved_opportunity_key_rehydrates_opening_requirements_from_contract() -> None:
    manager = SceneFrameManager()
    scene = SceneRecord(
        name="第一章：风铃廊",
        scene_type=SceneType.STANDARD,
        location="白花碑驿站·风铃廊",
        session_opportunity_key="opening",
    )
    contract = SessionDramaticContract(
        title="迟响",
        potential_scenes=[
            SessionSceneOpportunity(
                scene_key="opening",
                scene_role="strong_start",
                title="风铃廊问路",
                location="白花碑驿站·风铃廊",
                purpose="争取守望会开放旧路",
                situation="会长拦在旅人与后门之间。",
                required_elements=["白花风铃"],
                required_npc_names=["白花守望会会长", "失忆旅人"],
            )
        ],
    )

    frame = manager.ensure_frame(
        scene=scene,
        recent_chat="众人刚进入风铃廊。",
        world_state=WorldState(),
        character_manager=CharacterManager(),
        contract=contract,
    )

    assert frame.required_opening_elements == ["白花风铃"]
    assert frame.required_opening_npc_names == ["白花守望会会长", "失忆旅人"]
    assert scene.session_opportunity_role == "strong_start"
    assert scene.session_opportunity_purpose == "争取守望会开放旧路"


def test_first_scene_uses_strong_start_even_when_opening_text_mentions_climax_terms() -> None:
    manager = SceneFrameManager()
    contract = SessionDramaticContract(
        session_number=1,
        title="白花碑驿站的迟响",
        location="白花碑驿站",
        potential_scenes=[
            SessionSceneOpportunity(
                scene_key="start",
                scene_role="strong_start",
                title="风铃廊问路",
                required_elements=["白花风铃", "失忆旅人"],
                optional=False,
            ),
            SessionSceneOpportunity(
                scene_key="climax",
                scene_role="climax_candidate",
                title="旧路闸门与巡逻队",
                required_elements=["旧路闸门", "财团巡逻队"],
                optional=False,
            ),
        ],
    )

    frame = manager.ensure_frame(
        scene=SceneRecord(
            name="第一章：旧路闸门前的巡逻压力",
            scene_type=SceneType.STANDARD,
            location="白花碑驿站",
        ),
        recent_chat="远处传来巡逻队的金属回声。",
        world_state=WorldState(),
        character_manager=CharacterManager(),
        contract=contract,
    )

    assert frame.session_opportunity_key == "start"
    assert frame.required_opening_elements == ["白花风铃"]
    assert frame.required_opening_npc_names == ["失忆旅人"]


def test_renaming_same_scene_does_not_consume_an_extra_session_opportunity() -> None:
    manager = SceneFrameManager()
    contract = SessionDramaticContract(
        title="白花碑驿站的迟响",
        potential_scenes=[
            SessionSceneOpportunity(
                scene_key="start",
                scene_role="strong_start",
                title="风铃廊问路",
                location="白花碑驿站·风铃廊",
            ),
            SessionSceneOpportunity(
                scene_key="alternate",
                scene_role="alternate_approach",
                title="风铃回声仪式",
                location="白花碑驿站",
            ),
            SessionSceneOpportunity(
                scene_key="climax",
                scene_role="climax_candidate",
                title="旧路闸门与巡逻队",
                location="白花碑驿站·旧路闸门",
            ),
        ],
    )
    scene = SceneRecord(
        name="第一章开场",
        scene_type=SceneType.STANDARD,
        location="白花碑驿站",
        scene_id="scene-2",
    )
    manager.ensure_frame(
        scene=scene,
        recent_chat="众人刚抵达驿站。",
        world_state=WorldState(),
        character_manager=CharacterManager(),
        contract=contract,
    )

    scene.name = "第01场·场景1：风铃廊问路"
    scene.location = "白花碑驿站·风铃廊"
    renamed = manager.ensure_frame(
        scene=scene,
        recent_chat="白花风铃在廊下轻响。",
        world_state=WorldState(),
        character_manager=CharacterManager(),
        contract=contract,
    )

    assert manager.history == []
    assert renamed.source_scene_id == "scene-2"
    assert renamed.session_opportunity_key == "start"
    assert manager.scene_navigator.select(
        contract,
        act_number=2,
        used_keys={renamed.session_opportunity_key},
    ).scene_key == "alternate"


def test_signature_image_is_required_once_then_becomes_an_optional_evolving_reference() -> None:
    manager = SceneFrameManager()
    contract = SessionDramaticContract(
        session_number=1,
        title="白花碑驿站的迟响",
        location="白花碑驿站",
        signature_image="石碑前的失声风铃与结霜记忆罐",
        potential_scenes=[
            SessionSceneOpportunity(
                scene_key="start",
                scene_role="strong_start",
                title="失声风铃",
                situation="记忆罐正在结霜",
                optional=False,
            ),
            SessionSceneOpportunity(
                scene_key="climax",
                scene_role="climax_candidate",
                title="卸货坡道",
                situation="财团正把记忆罐转入车厢",
                optional=False,
            ),
        ],
    )
    manager.ensure_frame(
        scene=SceneRecord(
            name="第一场景",
            scene_type=SceneType.STANDARD,
            location="白花碑驿站·中庭",
        ),
        recent_chat="",
        world_state=WorldState(),
        character_manager=CharacterManager(),
        contract=contract,
    )

    first_packet = manager.expression_packet(include_private=True)

    assert first_packet["opening_image_mode"] == "establish"
    assert first_packet["required_opening_image"] == "石碑前的失声风铃与结霜记忆罐"
    assert first_packet["signature_image_reference"] == ""

    manager.ensure_frame(
        scene=SceneRecord(
            name="第三场景",
            scene_type=SceneType.STANDARD,
            location="白花碑驿站·卸货坡道",
        ),
        recent_chat="众人已经离开中庭，来到卸货坡道。",
        world_state=WorldState(),
        character_manager=CharacterManager(),
        contract=contract,
    )
    later_packet = manager.expression_packet(include_private=True)

    assert later_packet["selected_scene_role"] == "climax_candidate"
    assert later_packet["opening_image_mode"] == "evolve"
    assert later_packet["required_opening_image"] == ""
    assert later_packet["signature_image_reference"] == "石碑前的失声风铃与结霜记忆罐"


def test_expression_packet_separates_opening_cast_from_full_session_npc_library() -> None:
    manager = SceneFrameManager()
    contract = SessionDramaticContract(
        session_number=1,
        title="白花碑驿站的迟响",
        location="白花碑驿站",
        important_npcs=[
            SessionNPCRole(name="白棘会长", public_role="白花守望会会长"),
            SessionNPCRole(name="失忆旅人岑舟", public_role="失忆旅人"),
            SessionNPCRole(name="监察官艾蕾娜", public_role="财团监察官"),
        ],
        potential_scenes=[
            SessionSceneOpportunity(
                scene_key="start",
                scene_role="strong_start",
                title="迟响",
                situation="风铃叫出了旅人的旧名。",
                required_npc_names=["白棘会长", "失忆旅人岑舟"],
                optional=False,
            )
        ],
    )
    manager.ensure_frame(
        scene=SceneRecord(
            name="第一章开场",
            scene_type=SceneType.STANDARD,
            location="白花碑驿站",
        ),
        recent_chat="",
        world_state=WorldState(),
        character_manager=CharacterManager(),
        contract=contract,
    )

    packet = manager.expression_packet(include_private=True)

    assert [item["name"] for item in packet["prepared_npcs"]] == [
        "白棘会长",
        "失忆旅人岑舟",
        "监察官艾蕾娜",
    ]
    assert [item["name"] for item in packet["opening_prepared_npcs"]] == [
        "白棘会长",
        "失忆旅人岑舟",
    ]


def test_legacy_required_elements_promote_obvious_npcs_but_keep_objects_as_elements() -> None:
    manager = SceneFrameManager()
    contract = SessionDramaticContract(
        session_number=1,
        title="白花碑驿站的迟响",
        location="白花碑驿站",
        potential_scenes=[
            SessionSceneOpportunity(
                scene_key="start",
                scene_role="strong_start",
                title="迟响",
                situation="风铃叫出了旅人的旧名。",
                required_elements=["白花风铃", "白花守望会会长", "失忆旅人"],
                optional=False,
            )
        ],
    )

    manager.ensure_frame(
        scene=SceneRecord(
            name="第一章开场",
            scene_type=SceneType.STANDARD,
            location="白花碑驿站",
        ),
        recent_chat="",
        world_state=WorldState(),
        character_manager=CharacterManager(),
        contract=contract,
    )
    packet = manager.expression_packet(include_private=True)

    assert packet["required_opening_elements"] == ["白花风铃"]
    assert packet["required_opening_npc_names"] == ["白花守望会会长", "失忆旅人"]
    assert [item["name"] for item in packet["opening_prepared_npcs"]] == [
        "白花守望会会长",
        "失忆旅人",
    ]


def _character_manager() -> CharacterManager:
    characters = CharacterManager()
    characters.add(
        Character(
            name="洛岚",
            attributes={"DEX": 8, "MIG": 8, "INS": 10, "WLP": 8},
            max_hp=45,
            hp=45,
            max_mp=45,
            mp=45,
            traits=["pc"],
        )
    )
    return characters


def test_committed_post_check_investigation_records_original_reveal() -> None:
    manager = SceneFrameManager()
    manager.ensure_frame(
        scene=SceneRecord(
            name="风铃墙",
            scene_type=SceneType.STANDARD,
            location="白花碑驿站",
        ),
        recent_chat="赛璃正在检查白瓷铃的刻痕。",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )
    original = Action(
        ActionType.INVESTIGATE,
        {"actor": "赛璃", "target": "白瓷铃刻痕"},
    )
    manager.update_from_resolution(
        ActionResolution(
            action=Action(
                ActionType.INVOKE_TRAIT,
                {"actor": "赛璃", "trait": "钟鸣公国的御魂医师"},
            ),
            rules_text="援用特质后检定成功。",
            payload={
                "committed_source_action": original,
                "information": ["刻痕末端有一段新近刮除的名字。"],
            },
        )
    )

    assert manager.current_frame is not None
    assert manager.current_frame.revealed_clues == ["刻痕末端有一段新近刮除的名字。"]


def test_runtime_check_information_is_not_public_until_final_reply_delivers_it() -> None:
    manager = SceneFrameManager()
    manager.ensure_frame(
        scene=SceneRecord(
            name="风铃墙",
            scene_type=SceneType.STANDARD,
            location="白花碑驿站",
        ),
        recent_chat="赛璃正在检查白瓷铃的刻痕。",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )
    clue = "刻痕末端有一段新近刮除的名字。"
    resolution = ActionResolution(
        action=Action(
            ActionType.INVESTIGATE,
            {"actor": "赛璃", "target": "白瓷铃刻痕"},
        ),
        rules_text="调查成功。",
        payload={"information": [clue], "_defer_public_information": True},
    )

    manager.update_from_resolution(resolution)

    assert clue not in manager.current_frame.public_facts
    assert manager.publish_resolution_information(
        resolution,
        public_reply="你确认铃片近期被人动过。",
    ) == []
    assert clue not in manager.current_frame.public_facts

    assert manager.publish_resolution_information(
        resolution,
        public_reply=f"你确认铃片近期被人动过。\n{clue}",
    ) == [clue]
    assert clue in manager.current_frame.public_facts
    assert clue in manager.current_frame.revealed_clues


def test_provisional_check_does_not_deliver_or_publish_failure_fiction() -> None:
    manager = SceneFrameManager()
    manager.ensure_frame(
        scene=SceneRecord(
            name="旧路闸门",
            scene_type=SceneType.STANDARD,
            location="白花碑驿站",
        ),
        recent_chat="伊莉雅正在尝试改变分流记号。",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )
    consequence = "远处马蹄声仍朝旧路闸门方向逼近。"
    resolution = ActionResolution(
        action=Action(ActionType.REQUEST_ROLL, {"actor": "伊莉雅"}),
        rules_text="检定暂时失败。",
        payload={
            "information": [consequence],
            "check_result_provisional": True,
            "_defer_public_information": True,
        },
    )

    reply = SceneOrchestrator._ensure_resolution_information_in_reply(
        "伊莉雅检定结果为 6；你可以援用特质重掷，或接受结果。",
        resolution,
    )

    assert consequence not in reply
    assert manager.publish_resolution_information(resolution, public_reply=reply) == []
    assert consequence not in manager.current_frame.public_facts


def test_delivered_final_failure_is_visible_to_the_next_scene_beat() -> None:
    manager = SceneFrameManager()
    manager.ensure_frame(
        scene=SceneRecord(
            name="卡里巴村监狱牢区",
            scene_type=SceneType.STANDARD,
            location="卡里巴村监狱",
        ),
        recent_chat="艾丽妮正在寻找封印漏洞。",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )
    consequence = (
        "回流的蓝光骤然反噬，牢门与铁栏上的封印提前重新亮起；"
        "牢区的动静也会更容易被值班室外的人察觉。"
    )
    resolution = ActionResolution(
        action=Action(
            ActionType.HINDER,
            {
                "actor": "艾丽妮",
                "scene_check_planned": True,
                "failure_consequence": consequence,
            },
        ),
        rules_text="检定失败。",
        payload={
            "roll": RollOutcome(
                actor="艾丽妮",
                attributes=["INS", "WLP"],
                dice=[(10, 1), (10, 5)],
                total=6,
                modifier=0,
                high_roll=5,
                target_number=10,
                success=False,
                critical_success=False,
                fumble=False,
                margin=-4,
                reason="寻找封印漏洞",
            )
        },
    )

    manager.update_from_resolution(resolution)

    assert consequence in manager.current_frame.committed_consequences
    assert consequence not in manager.current_frame.public_facts
    assert manager.publish_resolution_information(
        resolution,
        public_reply=f"艾丽妮未能找出漏洞。{consequence}",
    ) == [consequence]
    assert consequence in manager.current_frame.public_facts
    assert consequence in manager.current_frame.established_facts
    assert manager.current_frame.recent_beats[-1] == consequence


def test_provisional_failure_never_enters_committed_scene_consequences() -> None:
    manager = SceneFrameManager()
    manager.ensure_frame(
        scene=SceneRecord(
            name="卡里巴村监狱牢区",
            scene_type=SceneType.STANDARD,
            location="卡里巴村监狱",
        ),
        recent_chat="艾丽妮正在寻找封印漏洞。",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )
    consequence = "牢门封印提前重新亮起。"
    resolution = ActionResolution(
        action=Action(
            ActionType.HINDER,
            {
                "actor": "艾丽妮",
                "scene_check_planned": True,
                "failure_consequence": consequence,
            },
        ),
        rules_text="检定结果仍可重掷。",
        payload={
            "check_result_provisional": True,
            "roll": RollOutcome(
                actor="艾丽妮",
                attributes=["INS", "WLP"],
                dice=[(10, 1), (10, 5)],
                total=6,
                modifier=0,
                high_roll=5,
                target_number=10,
                success=False,
                critical_success=False,
                fumble=False,
                margin=-4,
                reason="寻找封印漏洞",
            ),
        },
    )

    manager.update_from_resolution(resolution)

    assert consequence not in manager.current_frame.committed_consequences
    assert "_pending_scene_public_consequences" not in resolution.payload


def test_final_check_restores_authoritative_outcome_before_clock_state() -> None:
    observation = (
        "赛璃及时压住失忆旅人探头的动作，并看出车队继续沿旧路基驶向白花碑后的废弃岔道。"
    )
    resolution = ActionResolution(
        action=Action(
            ActionType.HINDER,
            {
                "actor": "赛璃",
                "scene_check_planned": True,
                "success_observation": observation,
            },
        ),
        rules_text="赛璃检定成功。",
        payload={
            "roll": RollOutcome(
                actor="赛璃",
                attributes=["DEX", "INS"],
                dice=[(6, 4), (10, 7)],
                total=11,
                modifier=0,
                high_roll=7,
                target_number=9,
                success=True,
                critical_success=False,
                fumble=False,
                margin=2,
                reason="遮蔽中的监视",
            )
        },
    )

    reply = SceneOrchestrator._ensure_resolution_information_in_reply(
        "【妨碍】赛璃进行遮蔽中的监视检定，成功！\n【财团巡逻队逼近】0/8",
        resolution,
    )

    assert observation in reply
    assert reply.index(observation) < reply.index("【财团巡逻队逼近】0/8")


def test_final_check_does_not_duplicate_authoritative_outcome() -> None:
    observation = "最前方的车灯偏向路肩，整列车队明显减速。"
    resolution = ActionResolution(
        action=Action(
            ActionType.HINDER,
            {
                "actor": "伊莉雅",
                "scene_check_planned": True,
                "success_observation": observation,
            },
        ),
        rules_text="伊莉雅检定成功。",
        payload={
            "roll": RollOutcome(
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
                reason="反光干扰车队",
            )
        },
    )

    reply = SceneOrchestrator._ensure_resolution_information_in_reply(
        f"【妨碍】伊莉雅检定成功！\n{observation}\n【财团巡逻队逼近】0/8",
        resolution,
    )

    assert reply.count(observation) == 1


def test_resolution_fact_delivery_keeps_only_new_same_scene_investigation_facts() -> None:
    known = "牢门蓝色符文的元素余波仍集中在铁栏根部。"
    new = "铁栏第三根立柱内侧新露出一道逆向导流刻痕。"
    panel = "【调查】艾丽妮检查牢门符文：d10=7 + d10=5 = 12，成功！"
    resolution = ActionResolution(
        action=Action(
            ActionType.INVESTIGATE,
            {"actor": "艾丽妮", "target": "牢门蓝色符文"},
        ),
        rules_text=panel,
        payload={"information": [known, new, new.rstrip("。") + "！"]},
    )
    pipeline = object.__new__(SceneOrchestrator)._build_turn_reply_pipeline()
    original_information = list(resolution.payload["information"])

    reply, changed = pipeline.run(
        f"{panel}\n{known}\n{new}\n{new.rstrip('。')}！",
        resolution,
        TurnReplyContext(
            recent_chat="艾丽妮再次检查同一扇牢门。",
            prior_public_facts=(known.rstrip("。") + "！",),
        ),
    )

    assert panel in reply
    assert known not in reply
    assert reply.count(new) == 1
    assert changed == ["resolution_fact_delivery"]
    assert resolution.payload["information"] == original_information

    known_only = ActionResolution(
        action=resolution.action,
        rules_text=panel,
        payload={"information": [known]},
    )
    reply, _ = pipeline.run(
        f"{panel}\n{known}",
        known_only,
        TurnReplyContext(
            recent_chat="艾丽妮第三次检查同一扇牢门。",
            prior_public_facts=(known,),
        ),
    )

    assert reply == panel
    assert known_only.payload["information"] == [known]


def test_resolving_one_npc_bargain_closes_same_promise_and_never_reopens_it() -> None:
    manager = SceneFrameManager()
    scene = SceneRecord(name="门厅", scene_type=SceneType.STANDARD, location="白花碑驿站")
    manager.ensure_frame(
        scene=scene,
        recent_chat="",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )
    first = manager.record_condition(
        npc="白花守望会会长",
        condition="只要带回可核验的巡逻消息，我就打开旧路。",
        promised_result="开放旧路",
        scene=scene,
    )
    duplicate = manager.record_condition(
        npc="白花守望会会长",
        condition="证明巡逻队的去向后，我会放行。",
        promised_result="放行",
        scene=scene,
    )

    assert first is duplicate
    manager.resolve_condition(first["condition_id"], scene=scene)
    reopened = manager.record_condition(
        npc="白花守望会会长",
        condition="再带一份证据回来，我才开门。",
        promised_result="开门",
        scene=scene,
    )

    assert reopened["status"] == "resolved"
    assert manager.latest_open_condition(npc="白花守望会会长") is None
    packet = manager.expression_packet()
    assert packet["open_conditions"] == []
    assert packet["resolved_conditions"]
    assert any("已经兑现承诺" in fact for fact in packet["public_facts"])


def test_player_fulfillment_keeps_condition_open_until_npc_delivers_payoff() -> None:
    manager = SceneFrameManager()
    scene = SceneRecord(
        name="风铃廊问路",
        scene_type=SceneType.STANDARD,
        location="白花碑驿站·风铃廊",
    )
    manager.ensure_frame(
        scene=scene,
        recent_chat="",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )
    condition = manager.record_condition(
        npc="白花守望会会长",
        condition="实际走完风铃廊至旧路闸门的护送路线",
        promised_result="打开旧路闸门并交出白花通行牌",
        scene=scene,
    )

    fulfilled = manager.mark_condition_fulfilled(
        condition["condition_id"],
        scene=scene,
        actor="艾薇娅",
        public_evidence="艾薇娅沿风铃廊内侧把护送路线实际走完。",
    )

    assert fulfilled is condition
    assert condition["status"] == "open"
    assert condition["player_fulfillment"] == "fulfilled"
    assert scene.open_conditions[0]["status"] == "open"
    assert manager.latest_open_condition(npc="白花守望会会长") is condition
    routed = manager.routing_context()["open_conditions"][0]
    assert routed["player_fulfillment"] == "fulfilled"
    assert routed["promised_result"] == "打开旧路闸门并交出白花通行牌"
    assert not any("已经兑现承诺" in fact for fact in manager.current_frame.public_facts)

    resolved = manager.resolve_condition(
        condition["condition_id"],
        scene=scene,
        actor="艾薇娅",
        public_evidence="会长卸下门栓，把白花通行牌交给艾薇娅。",
    )

    assert resolved is condition
    assert condition["status"] == "resolved"
    assert scene.open_conditions[0]["status"] == "resolved"
    assert any("卸下门栓" in fact for fact in manager.current_frame.public_facts)


def test_scene_frame_does_not_persist_more_review_as_a_condition_payout() -> None:
    manager = SceneFrameManager()
    scene = SceneRecord(name="风铃廊", scene_type=SceneType.STANDARD, location="白花碑驿站")
    manager.ensure_frame(
        scene=scene,
        recent_chat="",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )

    recorded = manager.record_condition(
        npc="白花守望会会长",
        condition="先解释风铃断拍。",
        promised_result="我会继续审查旧路与放行事宜。",
        scene=scene,
    )

    assert recorded is None
    assert manager.current_frame is not None
    assert manager.current_frame.open_conditions == []


def test_fulfilled_bargain_is_carried_to_a_different_scene_by_session_ledger() -> None:
    ledger = SessionLedger()
    ledger.start("session-1")
    manager = SceneFrameManager(session_ledger=ledger)
    first_scene = SceneRecord(
        name="风铃廊问路",
        scene_type=SceneType.STANDARD,
        location="白花碑驿站·风铃廊",
    )
    manager.ensure_frame(
        scene=first_scene,
        recent_chat="",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )
    condition = manager.record_condition(
        npc="白花守望会会长",
        condition="留下署名担保后开放旧路",
        promised_result="打开旧路闸门",
        promise_kind="access",
        promise_subject="旧路闸门",
        scene=first_scene,
    )
    manager.resolve_condition(condition["condition_id"], scene=first_scene)

    second_scene = SceneRecord(
        name="旧路出口余波",
        scene_type=SceneType.STANDARD,
        location="旧路出口外",
    )
    frame = manager.ensure_frame(
        scene=second_scene,
        recent_chat="队伍已经穿过闸门。",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )
    reopened = manager.record_condition(
        npc="白花守望会会长",
        condition="再留一份担保才会放行",
        promised_result="开放旧路",
        promise_kind="access",
        promise_subject="旧路",
        scene=second_scene,
    )

    assert reopened["status"] == "resolved"
    assert manager.latest_open_condition(npc="白花守望会会长") is None
    assert any(item["status"] == "resolved" for item in frame.open_conditions)
    assert any("已经兑现承诺" in fact for fact in frame.public_facts)
    assert len(ledger.fulfilled_promises) == 1


def test_scene_frame_builds_backstage_clue_web_without_public_secrets() -> None:
    manager = SceneFrameManager()
    world = _world_with_scene_seeds()
    characters = _character_manager()

    frame = manager.ensure_frame(
        scene=None,
        recent_chat="南星：请时悠把第一章镜头打开到白花碑驿站，我们想先询问钟匠。",
        world_state=world,
        character_manager=characters,
    )
    prompt = manager.format_for_prompt(include_private=True)
    public_audit = manager.audit_payload()
    private_audit = manager.audit_payload(include_private=True)

    assert frame.scene_name == "白花碑驿站"
    assert frame.location == "白花碑驿站"
    assert "当前场景框架" in prompt
    assert "辉钢财团" in prompt
    assert "危险候选" in prompt
    assert "特殊机制候选" in prompt
    assert "NPC回应原则" in prompt
    assert "调查结果原则" in prompt
    assert "普通调查失败不应凭空推进无关威胁命刻" in prompt
    assert any("钟匠" in item for item in frame.npc_functions)
    assert any("财团封蜡" in item for item in frame.clue_pool)
    assert frame.danger_candidates
    assert frame.special_mechanism_candidates
    assert frame.npc_response_guidance
    assert frame.investigation_guidance
    assert frame.failure_guidance
    assert "public_facts" in private_audit
    assert "open_questions" in private_audit
    assert "secrets" not in public_audit
    assert "possible_reveals" not in public_audit
    assert any("旧钥匙" in item for item in private_audit["secrets"])


def test_scene_frame_does_not_pull_a_global_seed_from_incidental_word_overlap() -> None:
    manager = SceneFrameManager()
    world = _world_with_scene_seeds()
    world.world_profile.mysteries.append(
        "苍白司教团把灰晶病说成灵魂升格，诱导病人主动交出名字与记忆。"
    )

    frame = manager.ensure_frame(
        scene=SceneRecord(
            name="第一章：白花碑驿站",
            scene_type=SceneType.STANDARD,
            location="白花碑驿站",
        ),
        recent_chat="英雄们刚刚走进驿站，正观察门口。",
        world_state=world,
        character_manager=_character_manager(),
    )

    self_contained = [*frame.clue_pool, *frame.possible_reveals]
    assert not any("苍白司教团" in item for item in self_contained)


def test_scene_frame_does_not_carry_an_unplaced_npc_into_another_location() -> None:
    manager = SceneFrameManager()
    world = _world_with_scene_seeds()
    world.npc_personas["无场景守门人"] = NPCPersona(
        name="无场景守门人",
        public_identity="白花守望会守门人",
        role_in_story="守门者",
    )

    frame = manager.ensure_frame(
        scene=SceneRecord(
            name="雾潮旧路",
            scene_type=SceneType.TRAVEL,
            location="雾潮海岸旧路",
        ),
        recent_chat="队伍在背风石坎旁观察远处巡灯。",
        world_state=world,
        character_manager=_character_manager(),
    )

    assert not any("白花碑驿站" in item or "守门人" in item for item in frame.npc_functions)


def test_new_scene_at_same_location_inherits_public_npc_answers() -> None:
    manager = SceneFrameManager()
    world = _world_with_scene_seeds()
    characters = _character_manager()
    manager.ensure_frame(
        scene=SceneRecord(
            name="驿站门前",
            scene_type=SceneType.STANDARD,
            location="白花碑驿站",
        ),
        recent_chat="队伍正在向会长说明来意。",
        world_state=world,
        character_manager=characters,
    )
    manager.record_npc_answer(
        "白花守望会会长",
        "白花守望会会长说明旧路可以借，但钥匙不会交到你们手里。",
    )

    frame = manager.ensure_frame(
        scene=SceneRecord(
            name="风铃廊下的追问",
            scene_type=SceneType.STANDARD,
            location="白花碑驿站",
        ),
        recent_chat="队伍移到风铃廊下继续谈话。",
        world_state=world,
        character_manager=characters,
    )

    assert any("旧路可以借" in fact for fact in frame.public_facts)
    assert any("旧路可以借" in fact for fact in frame.established_facts)
    assert frame.last_npc_speaker == "白花守望会会长"


def test_sublocations_inherit_committed_material_changes() -> None:
    manager = SceneFrameManager()
    world = _world_with_scene_seeds()
    characters = _character_manager()
    manager.ensure_frame(
        scene=SceneRecord(
            name="风铃廊",
            scene_type=SceneType.STANDARD,
            location="白花碑驿站·风铃廊",
        ),
        recent_chat="众人正在处理碎月遗物。",
        world_state=world,
        character_manager=characters,
    )
    manager.update_from_resolution(
        ActionResolution(
            action=Action(
                ActionType.NARRATE,
                {
                    "summary": "碎月遗物已经放入白布封盒，盒扣被完整锁住。",
                    "establish_fact": True,
                    "material_change": True,
                    "public_facts": ["碎月遗物现已封存在白布封盒里。"],
                },
            ),
            rules_text="",
            payload={"summary": "碎月遗物已经放入白布封盒，盒扣被完整锁住。"},
        )
    )

    frame = manager.ensure_frame(
        scene=SceneRecord(
            name="祭铃室",
            scene_type=SceneType.STANDARD,
            location="白花碑驿站·祭铃室",
        ),
        recent_chat="众人带着封盒走进祭铃室。",
        world_state=world,
        character_manager=characters,
    )

    assert any("盒扣被完整锁住" in fact for fact in frame.committed_consequences)
    assert any("封存在白布封盒" in fact for fact in frame.public_facts)


def test_nearby_location_does_not_inherit_without_explicit_parent_delimiter() -> None:
    assert not SceneFrameManager._same_physical_location("白花碑驿站", "白花碑驿站外的旧路")


def test_new_scene_at_different_location_does_not_inherit_local_answers() -> None:
    manager = SceneFrameManager()
    world = _world_with_scene_seeds()
    characters = _character_manager()
    manager.ensure_frame(
        scene=SceneRecord(
            name="驿站门前",
            scene_type=SceneType.STANDARD,
            location="白花碑驿站",
        ),
        recent_chat="队伍正在向会长说明来意。",
        world_state=world,
        character_manager=characters,
    )
    manager.record_npc_answer(
        "白花守望会会长",
        "白花守望会会长说明旧路可以借，但钥匙不会交到你们手里。",
    )

    frame = manager.ensure_frame(
        scene=SceneRecord(
            name="雾潮旧路",
            scene_type=SceneType.TRAVEL,
            location="雾潮海岸旧路",
        ),
        recent_chat="队伍已经离开驿站。",
        world_state=world,
        character_manager=characters,
    )

    assert not any("旧路可以借" in fact for fact in frame.public_facts)
    assert frame.last_npc_speaker != "白花守望会会长"


def test_gm_beat_is_continuity_context_not_investigation_evidence() -> None:
    manager = SceneFrameManager()
    frame = manager.ensure_frame(
        scene=SceneRecord(
            name="风铃廊",
            scene_type=SceneType.STANDARD,
            location="白花碑驿站",
        ),
        recent_chat="队伍正在观察门外动静。",
        world_state=_world_with_scene_seeds(),
        character_manager=_character_manager(),
    )

    beat = "门外巡逻灯逐盏熄灭，会长把登记簿推到桌沿。"
    manager.record_gm_beat(beat)

    assert beat in frame.recent_beats
    assert beat not in frame.public_facts
    assert beat not in frame.revealed_clues


def test_scene_frame_records_story_changes_and_followup_requests() -> None:
    manager = SceneFrameManager()
    manager.ensure_frame(
        scene=None,
        recent_chat="南星：请时悠把第一章镜头打开到白花碑驿站。",
        world_state=_world_with_scene_seeds(),
        character_manager=_character_manager(),
    )

    resolution = ActionResolution(
        action=Action(ActionType.ACCEPT_STORY_CHANGE, {}),
        rules_text="",
        payload={
            "fact": "洛岚确认驿站后门有一条只给旧邮差使用的窄巷。",
            "followup_intent": "洛岚想从窄巷绕到货栈背后观察。",
        },
    )
    manager.update_from_resolution(resolution)

    assert any("旧邮差" in item for item in manager.current_frame.established_facts)
    assert any("旧邮差" in item for item in manager.current_frame.public_facts)
    assert any("货栈背后" in item for item in manager.current_frame.unresolved_requests)
    assert any("货栈背后" in item for item in manager.current_frame.open_questions)


def test_scene_frame_keeps_identity_question_for_addressed_hero_only() -> None:
    manager = SceneFrameManager()
    manager.current_frame = SceneFrame(scene_key="闸门", scene_name="旧路闸门")
    manager.current_frame.pending_npc_questions.append(
        {
            "question_id": "legacy-identity-1",
            "npc": "监察官艾蕾娜",
            "addressed_actor": "伊莉雅",
            "kind": "identity_check",
            "summary": "姓名、关系与说法真伪",
            "required_parts": "name,relation,truth",
            "status": "open",
        }
    )

    pending = manager.latest_pending_npc_question()
    assert pending is not None
    assert pending["addressed_actor"] == "伊莉雅"
    assert not manager.resolve_pending_npc_question(
        actor="洛岚",
        player_message="我叫洛岚，和柱影里那位是护送关系，这些都属实。",
    )
    assert manager.resolve_pending_npc_question(
        actor="伊莉雅",
        player_message="我是伊莉雅，和柱影里那位是护送关系；我刚才说的属实。",
    )
    assert manager.latest_pending_npc_question() is None


def test_identity_question_accepts_natural_name_offer_and_npc_agency_confirmation() -> None:
    manager = SceneFrameManager()
    manager.current_frame = SceneFrame(scene_key="闸门", scene_name="旧路闸门")
    manager.current_frame.pending_npc_questions.append(
        {
            "question_id": "legacy-identity-2",
            "npc": "监察官艾蕾娜",
            "addressed_actor": "苍祈",
            "kind": "identity_check",
            "summary": "姓名与是否代替会长作答",
            "required_parts": "name,agency",
            "status": "open",
        }
    )

    assert manager.resolve_pending_npc_question(
        actor="苍祈",
        player_message="苍祈抬眼说：你若要登记，就把我的名字记上。",
        npc_response="苍祈，你的名字记上了；但个人拒绝不能替白花守望会会长作答。",
    )
    assert manager.latest_pending_npc_question() is None


def test_condition_with_an_explicit_required_actor_rejects_another_hero() -> None:
    manager = SceneFrameManager()
    scene = SceneRecord(
        name="旧路闸门",
        scene_type=SceneType.STANDARD,
        location="白花碑驿站",
    )
    manager.ensure_frame(
        scene=scene,
        recent_chat="",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )
    condition = manager.record_condition(
        npc="监察官艾蕾娜",
        condition="答清自己的姓名、与柱影里那位的关系，以及刚才说法是否属实。",
        promised_result="允许队伍通过旧路闸门。",
        scene=scene,
    )

    assert condition is not None
    condition["required_actor"] = "伊莉雅"

    assert condition["required_actor"] == "伊莉雅"
    assert manager.resolve_condition(condition["condition_id"], scene=scene, actor="洛岚") is None
    assert str(condition["status"]) == "open"
    assert manager.resolve_condition(condition["condition_id"], scene=scene, actor="伊莉雅") is not None


def test_recording_npc_prose_does_not_keyword_create_a_pending_request() -> None:
    manager = SceneFrameManager()
    manager.current_frame = SceneFrame(scene_key="闸门", scene_name="旧路闸门")

    manager.record_npc_answer(
        "失忆旅人",
        "还是先转头，不是靠近；我现在不能照做。",
        addressed_actor="伊莉雅",
    )

    assert manager.current_frame.pending_npc_questions == []


def test_pending_question_history_never_drops_open_obligations() -> None:
    records = [
        {
            "question_id": f"closed-{index}",
            "npc": "巡守",
            "kind": "player_response",
            "status": "resolved",
        }
        for index in range(30)
    ]
    records.insert(
        0,
        {
            "question_id": "old-open",
            "npc": "监察官",
            "kind": "player_response",
            "status": "open",
        },
    )

    bounded = SceneFrameManager._bounded_pending_npc_questions(records)

    assert any(item["question_id"] == "old-open" for item in bounded)
    assert len(bounded) == 24


def test_scene_frame_tracks_and_resolves_an_npc_public_condition() -> None:
    manager = SceneFrameManager()
    scene = SceneRecord(
        name="风铃廊交涉",
        scene_type=SceneType.STANDARD,
        location="白花碑驿站",
        scene_id="scene-1",
    )
    manager.ensure_frame(
        scene=scene,
        recent_chat="英雄正在向会长借用旧路。",
        world_state=_world_with_scene_seeds(),
        character_manager=_character_manager(),
    )
    offered = ActionResolution(
        action=Action(
            ActionType.NARRATE,
            {
                "summary": "会长要求留下担保。",
                "npc_answer_generated": True,
                "npc_answer_target": "白花守望会会长",
                "npc_speech_plan": {
                    "speech_act": "condition",
                    "condition": "在誓约匣留下名字和当值巡守的见证印。",
                    "direct_answer": "做到这件事，我就开放旧路。",
                    "promised_result": "开放旧路",
                    "promise_kind": "access",
                    "promise_subject": "旧路",
                },
            },
        ),
        rules_text="",
        payload={"summary": "会长要求留下担保。"},
    )

    manager.update_from_resolution(offered, scene=scene)
    condition = manager.latest_open_condition()

    assert condition is not None
    assert condition["npc"] == "白花守望会会长"
    assert scene.open_conditions[0]["status"] == "open"

    fulfilled = ActionResolution(
        action=Action(
            ActionType.NARRATE,
            {
                "summary": "会长收下担保并打开旧路。",
                "npc_answer_generated": True,
                "npc_answer_target": "白花守望会会长",
                "resolved_scene_condition_id": condition["condition_id"],
            },
        ),
        rules_text="",
        payload={"summary": "会长收下担保并打开旧路。"},
    )
    manager.update_from_resolution(fulfilled, scene=scene)

    assert manager.latest_open_condition() is None
    assert scene.open_conditions[0]["status"] == "resolved"


def test_npc_condition_is_recorded_when_answer_also_establishes_a_fact() -> None:
    manager = SceneFrameManager()
    scene = SceneRecord(name="风铃廊", scene_type=SceneType.STANDARD, location="白花碑驿站")
    manager.ensure_frame(
        scene=scene,
        recent_chat="",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )
    action = Action(
        ActionType.NARRATE,
        {
            "summary": "会长说清了担保条件。",
            "establish_fact": True,
            "npc_answer_generated": True,
            "npc_answer_target": "梅鸥会长",
            "prepared_bargain_bound": True,
            # Route metadata uses the literal string "none" when this answer
            # opens a condition rather than resolving an existing one.
            "condition_fulfillment": "none",
            "npc_speech_plan": {
                "speech_act": "condition",
                "condition": "当众承诺由守望会保管碎月遗物到日落前",
                "promised_result": "打开旧路外闸",
                "condition_outcome": "none",
            },
        },
    )

    manager.update_from_resolution(
        ActionResolution(action=action, rules_text="", payload={"summary": action.parameters["summary"]}),
        scene=scene,
    )

    condition = manager.latest_open_condition(npc="梅鸥会长")
    assert condition is not None
    assert "保管碎月遗物" in condition["condition"]
    assert condition["promised_result"] == "打开旧路外闸"
    assert manager.current_frame.last_npc_speaker == "梅鸥会长"


def test_explicit_condition_payoff_atomically_resolves_current_condition() -> None:
    manager = SceneFrameManager()
    scene = SceneRecord(name="风铃廊", scene_type=SceneType.STANDARD, location="白花碑驿站")
    manager.ensure_frame(
        scene=scene,
        recent_chat="",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )
    condition = manager.record_condition(
        npc="缇兰会长",
        condition="当众承诺不向财团透露旧路走法",
        promised_result="放开边门，让队伍进入旧路前室",
        scene=scene,
    )
    assert condition is not None

    action = Action(
        ActionType.NARRATE,
        {
            "summary": "缇兰会长确认承诺有效，随即放开边门。",
            "npc_answer_generated": True,
            "npc_answer_target": "缇兰会长",
            "resolved_scene_condition_id": condition["condition_id"],
            "npc_speech_plan": {
                "speech_act": "answer",
                "direct_answer": "够了，我现在开门。",
                "condition_outcome": "fulfilled",
            },
        },
    )
    manager.update_from_resolution(
        ActionResolution(action=action, rules_text="", payload={"summary": action.parameters["summary"]}),
        scene=scene,
    )

    assert manager.latest_open_condition(npc="缇兰会长") is None
    assert scene.open_conditions[0]["status"] == "resolved"
    assert manager.routing_context()["open_conditions"] == []


def test_structured_promise_wins_over_condition_sentence_extraction() -> None:
    manager = SceneFrameManager()
    scene = SceneRecord(name="静室", scene_type=SceneType.STANDARD, location="白花碑驿站")
    manager.ensure_frame(
        scene=scene,
        recent_chat="",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )

    recorded = manager.record_condition(
        npc="监察官艾蕾娜",
        condition=(
            "交出碎月遗物的临时查看权，或签下短期契约；"
            "满足标准：只要有人按下手印，或将遗物交进封存盒中，就算满足"
        ),
        promised_result="她会解除一次临检封锁，让队伍一小时内不受巡逻队拦截",
        promise_kind="access",
        promise_subject="临检封锁",
        scene=scene,
    )

    assert recorded is not None
    assert recorded["promised_result"] == "她会解除一次临检封锁，让队伍一小时内不受巡逻队拦截"
    assert "就算满足" not in recorded["promised_result"]


def test_prepared_bargain_structure_survives_voice_plan_drift() -> None:
    manager = SceneFrameManager()
    scene = SceneRecord(name="风铃廊", scene_type=SceneType.STANDARD, location="白花碑驿站")
    manager.ensure_frame(
        scene=scene,
        recent_chat="",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )
    action = Action(
        ActionType.NARRATE,
        {
            "summary": "韩砚说清条件后把手按回钥匙上。",
            "npc_answer_generated": True,
            "npc_answer_target": "白守会长韩砚",
            "prepared_bargain_bound": True,
            "prepared_bargain_offered": True,
            "prepared_bargain_public_condition": "提交署名担保，或证明碎月遗物没有异常共振",
            "prepared_bargain_promised_result": "开启旧路闸门外锁",
            "prepared_bargain_promise_kind": "access",
            "prepared_bargain_promise_subject": "旧路闸门外锁",
            "condition_fulfillment": "none",
            # Simulate a voice-model normalization that no longer labels the
            # public plan as a condition. The structured bargain still wins.
            "npc_speech_plan": {
                "speech_act": "answer",
                "direct_answer": "可以，但先给我担保或证明遗物安全。",
                "condition_outcome": "none",
            },
        },
    )

    manager.update_from_resolution(
        ActionResolution(action=action, rules_text="", payload={"summary": action.parameters["summary"]}),
        scene=scene,
    )

    condition = manager.latest_open_condition(npc="白守会长韩砚")
    assert condition is not None
    assert "或" in condition["condition"]
    assert condition["promised_result"] == "开启旧路闸门外锁"


def test_prepared_bargain_recovers_concession_when_voice_repeats_acceptance_clause() -> None:
    manager = SceneFrameManager()
    scene = SceneRecord(name="风铃廊", scene_type=SceneType.STANDARD, location="白花碑驿站")
    contract = SessionDramaticContract(
        title="白花碑驿站的核验",
        important_npcs=[
            SessionNPCRole(
                name="监察官艾蕾娜",
                concrete_demand="交出碎月遗物，并签下短期核验契约",
                acceptance_rule="碎月遗物进入封存盒，且旅人站到她指定的核验圈内",
                promised_result="当场解除风铃廊入口的第一层封锁，允许队伍靠近旧路闸门十分钟",
            )
        ]
    )
    manager.ensure_frame(
        scene=scene,
        recent_chat="",
        world_state=WorldState(),
        character_manager=CharacterManager(),
        contract=contract,
    )
    action = Action(
        ActionType.NARRATE,
        {
            "summary": "艾蕾娜给出核验条件。",
            "npc_answer_generated": True,
            "npc_answer_target": "监察官艾蕾娜",
            "npc_speech_plan": {
                "speech_act": "condition",
                "condition": "交出碎月遗物，并签下短期核验契约；碎月遗物进入封存盒，且旅人站到她指定的核验圈内",
                "promised_result": "且旅人站到她指定的核验圈内",
                "condition_outcome": "none",
            },
        },
    )

    manager.update_from_resolution(
        ActionResolution(action=action, rules_text="", payload={"summary": action.parameters["summary"]}),
        scene=scene,
    )

    condition = manager.latest_open_condition(npc="监察官艾蕾娜")
    assert condition is not None
    assert condition["promised_result"] == contract.important_npcs[0].promised_result


def test_unprepared_ad_hoc_bargain_keeps_its_public_concession() -> None:
    manager = SceneFrameManager()
    scene = SceneRecord(name="门厅", scene_type=SceneType.STANDARD, location="白花碑驿站")
    manager.ensure_frame(
        scene=scene,
        recent_chat="",
        world_state=WorldState(),
        character_manager=CharacterManager(),
        contract=SessionDramaticContract(
            title="白花碑驿站的核验",
            important_npcs=[
                SessionNPCRole(
                    name="监察官艾蕾娜",
                    concrete_demand="交出碎月遗物",
                    acceptance_rule="遗物进入封存盒",
                    promised_result="解除入口封锁",
                )
            ]
        ),
    )

    recorded = manager.record_condition(
        npc="监察官艾蕾娜",
        condition="替她找到失踪的巡查员",
        promised_result="交出财团仓库的侧门钥匙",
        scene=scene,
    )

    assert recorded is not None
    assert recorded["promised_result"] == "交出财团仓库的侧门钥匙"


def test_npc_cannot_stack_incremental_bargains_in_one_scene() -> None:
    manager = SceneFrameManager()
    scene = SceneRecord(name="门厅", scene_type=SceneType.STANDARD, location="白花碑驿站")
    manager.ensure_frame(
        scene=scene,
        recent_chat="",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )

    first = manager.record_condition(
        npc="白花守望会守门人",
        condition="把门口那波人挡住，我就带你们去后院。",
        promised_result="带你们去后院",
        promise_kind="escort",
        promise_subject="后院",
        scene=scene,
    )
    stacked = manager.record_condition(
        npc="白花守望会守门人",
        condition="继续压住门口，我再把后院那条线往下说。",
        promised_result="说明后院的线索",
        promise_kind="disclose",
        promise_subject="后院",
        scene=scene,
    )

    assert stacked is first
    assert len(manager.current_frame.open_conditions) == 1

    manager.resolve_condition(first["condition_id"], scene=scene)
    reopened = manager.record_condition(
        npc="白花守望会守门人",
        condition="再守住门缝，我才继续说后院。",
        promised_result="继续说明后院",
        promise_kind="disclose",
        promise_subject="后院",
        scene=scene,
    )

    assert reopened is first
    assert reopened["status"] == "resolved"
    assert manager.latest_open_condition(npc="白花守望会守门人") is None


def test_authoritative_prepared_bargain_replaces_malformed_early_condition() -> None:
    manager = SceneFrameManager()
    scene = SceneRecord(name="风铃廊", scene_type=SceneType.STANDARD, location="白花碑驿站")
    manager.ensure_frame(
        scene=scene,
        recent_chat="",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )
    early = manager.record_condition(
        npc="艾蕾娜·赫铎",
        condition="别逼我把这里并入巡查登记。",
        promised_result="把这里并入巡查登记",
        scene=scene,
    )
    assert early is None

    manager.record_condition(
        npc="艾蕾娜·赫铎",
        condition="把碎月遗物放进可封存容器，并把失忆旅人带到指定记录点",
        promised_result="发放临时放行纸",
        scene=scene,
    )
    replaced = manager.record_condition(
        npc="艾蕾娜·赫铎",
        condition="交出碎月遗物；满足标准：遗物放进可封存容器，并把失忆旅人带到指定记录点",
        promised_result="立即发放盖有财团印章的临时放行纸",
        scene=scene,
        replace_existing=True,
    )

    assert replaced is not None
    assert "指定记录点" in replaced["condition"]
    assert "临时放行纸" in replaced["promised_result"]
    assert scene.open_conditions[0] == replaced


def test_answer_plan_with_advice_does_not_open_scene_condition() -> None:
    manager = SceneFrameManager()
    scene = SceneRecord(name="后院", scene_type=SceneType.STANDARD, location="白花碑驿站")
    manager.ensure_frame(
        scene=scene,
        recent_chat="",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )
    resolution = ActionResolution(
        action=Action(
            ActionType.NARRATE,
            {
                "npc_answer_generated": True,
                "npc_answer_target": "守门人",
                "npc_speech_plan": {
                    "speech_act": "answer",
                    "condition": "进去后别碰左边的账册。",
                    "direct_answer": "跟我来后院。",
                },
            },
        ),
        rules_text="",
        payload={"summary": "守门人让众人跟上。"},
    )

    manager.update_from_resolution(resolution, scene=scene)

    assert manager.latest_open_condition(npc="守门人") is None


def test_completed_clock_consequence_becomes_committed_scene_fact() -> None:
    manager = SceneFrameManager()
    manager.ensure_frame(
        scene=SceneRecord(
            name="驿站门前",
            scene_type=SceneType.STANDARD,
            location="白花碑驿站",
        ),
        recent_chat="财团巡逻队正在逼近。",
        world_state=_world_with_scene_seeds(),
        character_manager=_character_manager(),
    )
    resolution = ActionResolution(
        action=Action(ActionType.NARRATE, {}),
        rules_text="",
        payload={
            "auto_clock_changes": [
                ClockChange(
                    clock_name="财团巡逻队逼近",
                    before=5,
                    after=6,
                    max_segments=6,
                    delta=1,
                    clock_type="threat",
                    completion_consequence="填满后财团巡逻队包围白花碑驿站",
                )
            ]
        },
    )

    manager.update_from_resolution(resolution)
    packet = manager.expression_packet(include_private=True)

    assert manager.current_frame.committed_consequences == ["财团巡逻队包围白花碑驿站。"]
    assert packet["committed_consequences"] == ["财团巡逻队包围白花碑驿站。"]
    assert manager.current_frame.current_pressure == "财团巡逻队包围白花碑驿站。"


def test_orchestrator_injects_scene_frame_into_panel_guidance() -> None:
    characters = _character_manager()
    clocks = ClockManager()
    conflict = ConflictManager(characters)
    world = _world_with_scene_seeds()
    rules = RulesEngine(seed=0)
    scene_manager = SceneManager()
    scene_manager.start_scene(
        "白花碑驿站",
        SceneType.STANDARD,
        location="白花碑驿站",
        objective="找出财团为什么盯上失忆旅人。",
        summary="白花碑驿站的晶炉钟声忽然错拍。",
    )
    app = SceneOrchestrator(
        character_manager=characters,
        clock_manager=clocks,
        conflict_manager=conflict,
        world_state=world,
        interceptor=ActionInterceptor(rules, characters, clocks, conflict, world),
        expressor=FixedExpressor(),
        scene_manager=scene_manager,
    )

    panel = app.build_panel("洛岚：我想询问钟匠昨夜财团有没有来过。")

    assert "当前场景框架" in panel.memory_guidance
    assert "线索池" in panel.memory_guidance
    assert "白花碑驿站" in panel.memory_guidance


def test_orchestrator_injects_chapter_package_and_iconic_guidance() -> None:
    characters = _character_manager()
    clocks = ClockManager()
    conflict = ConflictManager(characters)
    world = _world_with_scene_seeds()
    world.register_chapter_package(
        ChapterPackage(
            chapter_title="白钟旧路",
            synopsis="英雄护送失忆旅人穿越旧路。",
            iconic_elements=["白花风铃"],
            shared_creation_slots=["旧路由谁守护"],
        )
    )
    rules = RulesEngine(seed=0)
    app = SceneOrchestrator(
        character_manager=characters,
        clock_manager=clocks,
        conflict_manager=conflict,
        world_state=world,
        interceptor=ActionInterceptor(rules, characters, clocks, conflict, world),
        expressor=FixedExpressor(),
    )

    panel = app.build_panel("洛岚：我们想借白花风铃确认旧路。")

    assert "当前章节包【白钟旧路】" in panel.memory_guidance
    assert "标志性元素保护" in panel.memory_guidance
    assert "白花风铃" in panel.memory_guidance


def test_story_change_iconic_violation_does_not_spend_fabula() -> None:
    characters = _character_manager()
    clocks = ClockManager()
    conflict = ConflictManager(characters)
    world = _world_with_scene_seeds()
    world.register_iconic_element("白花风铃", element_type="chapter", description="章节关键线索。")
    interceptor = ActionInterceptor(RulesEngine(seed=0), characters, clocks, conflict, world)
    before = characters.get("洛岚").fabula_points

    resolution = interceptor.resolve(
        Action(
            ActionType.ACCEPT_STORY_CHANGE,
            {
                "target": "洛岚",
                "fact": "白花风铃其实是洛岚的姐姐，并且已经死亡。",
            },
        )
    )

    assert resolution.payload["story_change_failed"] is True
    assert resolution.payload["iconic_violation"] is True
    assert characters.get("洛岚").fabula_points == before


def test_accepted_npc_offer_is_settled_without_becoming_an_open_condition() -> None:
    manager = SceneFrameManager()
    scene = SceneRecord(
        name="驿站门外交涉",
        scene_type=SceneType.STANDARD,
        location="白花碑驿站",
        scene_id="scene-settlement",
    )
    frame = manager.ensure_frame(
        scene=scene,
        recent_chat="英雄正在与财团使者确认情报交换的范围。",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )
    resolution = ActionResolution(
        action=Action(
            ActionType.NARRATE,
            {
                "summary": "使者同意只听英雄已经确认的那一小段去路。",
                "npc_answer_generated": True,
                "npc_answer_target": "灰金短斗篷的财团使者",
                "npc_player_message": "我们只说确认过的那一小段，不替其他人作证。",
                "npc_speech_plan": {
                    "speech_act": "answer",
                    "direct_answer": "可以，就按这个范围谈。",
                    "proposal_outcome": "accepted",
                    "settled_terms": "英雄只说明已经确认的那一小段去路，财团使者按此范围听取",
                },
            },
        ),
        rules_text="",
        payload={"summary": "使者同意只听英雄已经确认的那一小段去路。"},
    )

    manager.update_from_resolution(resolution, scene=scene)

    assert frame.open_conditions == []
    assert len(frame.settled_exchanges) == 1
    exchange = frame.settled_exchanges[0]
    assert exchange["outcome"] == "accepted"
    assert exchange["npc"] == "灰金短斗篷的财团使者"
    assert "只说明已经确认" in exchange["settled_terms"]
    assert "不替其他人作证" in exchange["player_offer"]
    assert manager.expression_packet()["settled_exchanges"] == [exchange]
    assert manager.routing_context()["settled_exchanges"] == [exchange]
    assert exchange["player_performance"] == "complete"
    assert manager.pending_settled_exchanges() == []
    assert "条款已接受、但玩家尚未实际履行" not in manager.format_for_prompt(include_private=True)
    assert "不得重新索要" in manager.format_for_prompt(include_private=True)


def test_unconditional_accepted_exchange_never_blocks_scene_closure() -> None:
    manager = SceneFrameManager()
    manager.current_frame = SceneFrame(scene_key="风铃廊", scene_name="风铃廊")

    exchange = manager.record_settled_exchange(
        npc="失名旅人",
        player_offer="跟紧赛璃，我们带你离开这里。",
        npc_response="好，我跟着她。",
        outcome="accepted",
        settled_terms="失名旅人愿意跟随赛璃离开风铃廊",
    )

    assert exchange is not None
    assert exchange["condition"] == ""
    assert exchange["player_performance"] == "complete"
    assert manager.pending_settled_exchanges() == []


def test_npc_self_condition_does_not_become_unpaid_player_commitment() -> None:
    manager = SceneFrameManager()
    manager.current_frame = SceneFrame(scene_key="风铃廊", scene_name="风铃廊")

    exchange = manager.record_settled_exchange(
        npc="失忆旅人",
        player_offer="我已经把撤回流程向你说明清楚。",
        npc_response="如果我想撤回，我会亲口说停下。",
        outcome="accepted",
        settled_terms="如果我想撤回，我会亲口说停下",
    )

    assert exchange is not None
    assert manager.settled_exchange_condition(exchange) == ""
    assert exchange["player_performance"] == "complete"
    assert manager.pending_settled_exchanges() == []


def test_legacy_npc_self_condition_cannot_block_scene_closure() -> None:
    manager = SceneFrameManager()
    manager.current_frame = SceneFrame(
        scene_key="风铃廊",
        scene_name="风铃廊",
        settled_exchanges=[
            {
                "exchange_id": "legacy-1",
                "npc": "失忆旅人",
                "outcome": "accepted",
                "settled_terms": "如果我想撤回，我会亲口说停下",
                "condition": "我想撤回",
                "promised_result": "亲口说停下",
                "player_performance": "pending",
            }
        ],
    )

    assert manager.pending_settled_exchanges() == []

    manager.normalize_loaded_state()

    migrated = manager.current_frame.settled_exchanges[0]
    assert migrated["condition"] == ""
    assert migrated["promised_result"] == ""
    assert migrated["player_performance"] == "complete"


def test_pending_accepted_exchange_completes_only_on_actual_player_performance() -> None:
    manager = SceneFrameManager()
    scene = SceneRecord(
        name="记忆交换",
        scene_type=SceneType.STANDARD,
        location="白花碑驿站",
        scene_id="scene-memory-settlement",
    )
    manager.ensure_frame(
        scene=scene,
        recent_chat="",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )
    exchange = manager.record_settled_exchange(
        npc="灰金短斗篷的财团使者",
        player_offer="我愿意让你拿走一段与驿站有关的旧识。",
        npc_response="可以，就取这一段。",
        outcome="accepted",
        settled_terms="英雄交出一段与驿站有关的旧识，使者提供旧路通行牌",
    )

    assert exchange is not None
    assert exchange["player_performance"] == "pending"
    assert manager.complete_pending_exchange_from_player_action(
        npc="财团使者",
        player_message="我仍然愿意之后交出这段记忆。",
    ) is None

    completed = manager.complete_pending_exchange_from_player_action(
        npc="灰金短斗篷的财团使者",
        player_message="伊莉雅按约把与驿站有关的那段记忆交出，让使者现在取走。",
    )

    assert completed is exchange
    assert exchange["player_performance"] == "complete"
    assert "按约" in exchange["player_fulfillment"]
    assert manager.pending_settled_exchanges() == []
    assert "已完成或已拒绝的交涉" in manager.format_for_prompt(include_private=True)


def test_conditional_settlement_completes_when_player_establishes_the_requested_fact() -> None:
    manager = SceneFrameManager()
    scene = SceneRecord(
        name="风铃廊约定",
        scene_type=SceneType.STANDARD,
        location="白花碑驿站",
        scene_id="scene-explanation-settlement",
    )
    manager.ensure_frame(
        scene=scene,
        recent_chat="",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )
    exchange = manager.record_settled_exchange(
        npc="白花守望会会长",
        player_offer="我们先查清失忆旅人为什么会对风铃有反应。",
        npc_response="答得上，我就让驿站开放旧路。",
        outcome="accepted",
        settled_terms="接受先查明失忆旅人反应原因再决定开放旧路；原因未明前不放行。",
    )

    assert exchange is not None
    assert exchange["condition"] == "查明失忆旅人反应原因"
    assert exchange["promised_result"] == "开放旧路"
    assert manager.complete_pending_exchange_from_player_action(
        npc="白花守望会会长",
        player_message="我准备去查明旅人为什么会对风铃有反应。",
    ) is None

    completed = manager.complete_pending_exchange_from_player_action(
        npc="会长",
        player_message=(
            "赛璃解释：迟响让旅人想起他亲手刻下亡者名字、又在她死后补完刻痕的经历；"
            "这就是他反应的来源。现在按约履行。"
        ),
    )

    assert completed is exchange
    assert exchange["player_performance"] == "complete"
    assert "这就是他反应的来源" in exchange["player_fulfillment"]


def test_near_duplicate_accepted_exchanges_compact_to_one_settlement() -> None:
    manager = SceneFrameManager()
    scene = SceneRecord(
        name="驿站门外交涉",
        scene_type=SceneType.STANDARD,
        location="白花碑驿站",
        scene_id="scene-settlement-dedupe",
    )
    manager.ensure_frame(
        scene=scene,
        recent_chat="英雄正在与财团使者谈一小段去路。",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )
    terms = [
        "英雄只说明已经确认的那一小段去路，财团使者按此范围听取",
        "英雄当场说出已经知道的那条去路，但不继续补全，财团使者接受这个范围",
        "能当场完整说出且不再继续补全，就算达到财团使者要求的程度",
        "今天只认英雄已经说出的这段去路，财团使者不再要求其他交换",
    ]
    npc_names = [
        "门外那位灰金短斗篷的使者",
        "灰金短斗篷的财团使者",
        "财团使者",
        "灰金短斗篷的财团使者",
    ]
    for index, settled_terms in enumerate(terms):
        manager.record_settled_exchange(
            npc=npc_names[index],
            player_offer=f"第{index + 1}次复述同一范围",
            npc_response="可以，就到这里。",
            outcome="accepted",
            settled_terms=settled_terms,
        )

    assert len(manager.current_frame.settled_exchanges) == 1
    assert manager.current_frame.settled_exchanges[0]["settled_terms"] == terms[-1]
    assert manager.current_frame.settled_exchanges[0]["npc"] == "灰金短斗篷的财团使者"

    manager.record_settled_exchange(
        npc="失名旅人",
        player_offer="我们只问你愿意公开的部分。",
        npc_response="我可以说一小段方向。",
        outcome="accepted",
        settled_terms="失名旅人只公开一小段方向感，不说完整名字与终点",
    )
    assert len(manager.current_frame.settled_exchanges) == 2


def test_loaded_scene_state_prunes_transient_questions_and_compacts_settlements() -> None:
    manager = SceneFrameManager()
    scene = SceneRecord(
        name="旧档迁移",
        scene_type=SceneType.STANDARD,
        location="白花碑驿站",
        scene_id="scene-settlement-migration",
    )
    frame = manager.ensure_frame(
        scene=scene,
        recent_chat="",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )
    frame.settled_exchanges = [
        {
            "exchange_id": "old-1",
            "npc": "财团使者",
            "outcome": "accepted",
            "settled_terms": "英雄只说明已经确认的一小段去路，使者接受这个范围",
            "player_offer": "只说一小段",
            "npc_response": "可以",
        },
        {
            "exchange_id": "old-2",
            "npc": "灰金短斗篷的财团使者",
            "outcome": "accepted",
            "settled_terms": "英雄当场说出这段去路且不再补全，财团使者按此范围听取",
            "player_offer": "当场说出这一段",
            "npc_response": "到这里就够了",
        },
    ]
    frame.open_questions = [
        "我现在直接转向财团使者追问这段话够不够。",
        "白钟为何会抹去亡者的名字？",
    ]
    frame.public_facts = [
        "使者接受只听这一小段去路。",
        "使者接受只听这一小段去路。 【财团巡逻队逼近】6/8。再拖下去，他们就会包围现场！",
    ]
    frame.established_facts = list(frame.public_facts)
    frame.last_npc_speaker = "门缝和入口方向"
    frame.visible_elements = [
        "地点：白花碑驿站",
        "把地点设定“白花碑驿站”变成可触碰、可破坏或可利用的现场事物，而非背景说明。",
    ]

    manager.normalize_loaded_state()

    assert len(frame.settled_exchanges) == 1
    assert frame.open_questions == ["白钟为何会抹去亡者的名字？"]
    assert frame.public_facts == ["使者接受只听这一小段去路。"]
    assert frame.established_facts == ["使者接受只听这一小段去路。"]
    assert frame.last_npc_speaker == ""
    assert frame.visible_elements == ["地点：白花碑驿站"]


def test_resolved_access_condition_clears_only_matching_stale_pressure() -> None:
    manager = SceneFrameManager()
    scene = SceneRecord(
        name="驿站问路",
        scene_type=SceneType.STANDARD,
        location="白花碑驿站",
        scene_id="scene-access-pressure",
    )
    frame = manager.ensure_frame(
        scene=scene,
        recent_chat="",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )
    frame.current_pressure = "旅人仍在等待安全路线，守门人尚未决定是否放行。"
    condition = manager.record_condition(
        npc="白花守望会会长",
        condition="说清如何护住旅人",
        promised_result="放行北侧旧路",
        promise_kind="access_granted",
        scene=scene,
    )

    manager.resolve_condition(condition["condition_id"], scene=scene)

    assert frame.current_pressure == ""

    frame.current_pressure = "财团巡逻队正在逼近。"
    manager._clear_pressure_resolved_by_condition(frame, condition)
    assert frame.current_pressure == "财团巡逻队正在逼近。"


def test_resolving_latest_request_does_not_leave_it_as_a_scene_mystery() -> None:
    manager = SceneFrameManager()
    scene = SceneRecord(
        name="请求清理",
        scene_type=SceneType.STANDARD,
        location="白花碑驿站",
        scene_id="scene-request-cleanup",
    )
    frame = manager.ensure_frame(
        scene=scene,
        recent_chat="",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )
    latest = "我问财团使者，这段去路说到什么程度才算成立。"
    frame.unresolved_requests = ["先前尚未回答的请求", latest]
    frame.open_questions = ["世界为何遗忘归潮祭？", latest]

    manager._resolve_matching_request(
        frame,
        Action(ActionType.NARRATE, {"target": "灰金短斗篷的财团使者"}),
    )

    assert frame.unresolved_requests == ["先前尚未回答的请求"]
    assert frame.open_questions == ["世界为何遗忘归潮祭？"]


def test_current_action_permission_never_persists_as_scene_condition() -> None:
    manager = SceneFrameManager()
    scene = SceneRecord(
        name="风铃廊",
        scene_type=SceneType.STANDARD,
        location="白花碑驿站",
        scene_id="scene-current-permission",
    )
    frame = manager.ensure_frame(
        scene=scene,
        recent_chat="",
        world_state=WorldState(),
        character_manager=CharacterManager(),
    )
    action = Action(
        ActionType.NARRATE,
        {
            "npc_answer_target": "白花守望会会长",
            "npc_player_message": "我先把这根细丝从梁木和印记纹路里处理掉。",
            "npc_speech_plan": {
                "speech_act": "condition",
                "condition": "先把细丝从梁木下沿剥离出来，且不碰白花印记纹路。",
                "promised_result": "允许你把细丝从印记边缘移开。",
                "direct_answer": "做完后，我会允许你把细丝从印记边缘移开。",
            },
        },
    )

    assert manager._record_npc_condition(frame, action, scene=scene) is None
    assert frame.open_conditions == []
