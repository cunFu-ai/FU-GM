import json

from fu_gm.models import SessionDramaticContract, SessionEpisodeProgress
from fu_gm.testing.conversation_quality import ConversationQualityAuditor
from fu_gm.testing.player_simulator import ConstrainedPlayerSimulator
from fu_gm.testing.session_progress_evaluator import (
    SessionProgressAssessment,
    SessionProgressEvaluator,
)


class _Client:
    def __init__(self, payload):
        self.payload = payload
        self.messages = []

    def create_chat_completion(self, **kwargs):
        self.messages = kwargs["messages"]
        return json.dumps(self.payload, ensure_ascii=False)


def test_progress_evaluator_requires_public_evidence_for_memory_anchor() -> None:
    client = _Client(
        {
            "stage": "reversal",
            "scene_change_recommended": True,
            "local_question_changed": True,
            "local_question_resolved": False,
            "deliberate_cliffhanger": False,
            "reversal_reached": True,
            "concrete_consequence": True,
            "opening_signature_present": True,
            "concrete_npc_agenda_present": True,
            "npc_answer_complete": True,
            "player_agency_preserved": True,
            "continuity_ok": True,
            "memory_image": "被潮水倒映成两轮的白钟",
            "memory_choice": "英雄把钥匙交给失忆旅人",
            "memory_consequence": "守望会封闭了正门",
            "unresolved_now": "巡逻队仍在逼近",
            "next_gm_need": "把选择推向高潮",
            "evidence": ["旅人接过钥匙", "正门落闩"],
        }
    )
    evaluator = SessionProgressEvaluator(client=client, model="test-model")

    result = evaluator.evaluate(
        transcript="GM：旅人接过钥匙。守望会会长随即让人落下正门。",
        contract=SessionDramaticContract(dramatic_question="能否带旅人离开？"),
        meaningful_turns=18,
        scene_count=2,
        previous_memory_anchors=[
            {
                "session": "1",
                "image": "白花风铃裂开",
                "choice": "保护旅人",
                "consequence": "守望会封闭正门",
            }
        ],
    )

    assert result.stage == "reversal"
    assert result.memory_anchor_complete
    assert result.opening_signature_present
    assert result.concrete_npc_agenda_present
    assert not result.used_fallback
    assert "只依据玩家实际看见" in client.messages[0].content
    assert "白花风铃裂开" in client.messages[1].content


def test_progress_evaluator_fallback_never_certifies_closure() -> None:
    evaluator = SessionProgressEvaluator(client=None, model="")

    result = evaluator.evaluate(
        transcript="有一些行动。",
        contract=SessionDramaticContract(),
        meaningful_turns=30,
        scene_count=3,
    )

    assert result.used_fallback
    assert result.stage == "reversal"
    assert not result.local_question_resolved
    assert not result.memory_anchor_complete


def test_progress_evaluator_timeout_preserves_only_committed_episode_evidence() -> None:
    class _FailingClient:
        def create_chat_completion(self, **kwargs):
            raise TimeoutError("provider too slow")

    evaluator = SessionProgressEvaluator(client=_FailingClient(), model="test-model")
    progress = SessionEpisodeProgress(
        stage="climax",
        local_question_changed=True,
        reversal_reached=True,
        concrete_consequences=["守望会已经封闭正门。"],
        opposition_moves=["监察官撤走了旧路信标。"],
        local_payoffs=["失名旅人获得临时庇护。"],
        memory_image="白花风铃只剩第四枚仍在响。",
        memory_choice="英雄留下保护旅人。",
        memory_consequence="驿站失去中立地位。",
    )

    result = evaluator.evaluate(
        transcript="公开实录",
        contract=SessionDramaticContract(),
        meaningful_turns=24,
        scene_count=3,
        authoritative_progress=progress,
    )

    assert result.used_fallback
    assert result.stage == "climax"
    assert result.local_question_changed
    assert not result.local_question_resolved
    assert result.opposition_move_present
    assert result.memory_anchor_complete


def test_progress_evaluator_compacts_middle_but_keeps_opening_and_ending() -> None:
    transcript = "开场标志" + ("中段" * 12000) + "最终后果"

    compact = SessionProgressEvaluator._compact_transcript(transcript, max_chars=1000)

    assert compact.startswith("开场标志")
    assert compact.endswith("最终后果")
    assert "已省略" in compact
    assert len(compact) < 1100


def test_progress_evaluator_recommends_new_scene_after_material_loop() -> None:
    client = _Client(
        {
            "stage": "development",
            "scene_change_recommended": False,
            "repeated_loop_detected": True,
            "opposition_move_present": True,
            "concrete_consequence": True,
        }
    )
    evaluator = SessionProgressEvaluator(client=client, model="test-model")

    result = evaluator.evaluate(
        transcript="对立方已经封住正门，众人连续三轮留在门边重复争论。",
        contract=SessionDramaticContract(),
        meaningful_turns=10,
        scene_count=1,
    )

    assert result.scene_change_recommended


def test_progress_evaluator_merge_keeps_earned_reversal_without_sticky_loop() -> None:
    previous = SessionProgressAssessment(
        stage="climax",
        scene_change_recommended=True,
        local_question_changed=True,
        reversal_reached=True,
        concrete_consequence=True,
        opposition_move_present=True,
        local_payoff_present=True,
        repeated_loop_detected=True,
        npc_answer_complete=False,
        memory_image="裂开的白花风铃",
        memory_choice="英雄拦下抽取车",
        memory_consequence="记忆罐留在驿站",
    )
    current = SessionProgressAssessment(
        stage="development",
        scene_change_recommended=False,
        repeated_loop_detected=False,
        npc_answer_complete=True,
    )

    merged = SessionProgressEvaluator.merge_cumulative(previous, current)

    assert merged.stage == "climax"
    assert merged.reversal_reached
    assert merged.local_question_changed
    assert merged.opposition_move_present
    assert merged.local_payoff_present
    assert merged.memory_anchor_complete
    assert not merged.repeated_loop_detected
    assert not merged.scene_change_recommended
    assert merged.npc_answer_complete


def test_progress_evaluator_does_not_keep_a_retracted_resolution_sticky() -> None:
    previous = SessionProgressAssessment(
        stage="climax",
        local_question_changed=True,
        local_question_resolved=True,
        local_payoff_present=True,
    )
    current = SessionProgressAssessment(
        stage="climax",
        local_question_changed=True,
        local_question_resolved=False,
        local_payoff_present=True,
        unresolved_now="财团使者仍在等待队伍回应交换条件。",
    )

    merged = SessionProgressEvaluator.merge_cumulative(previous, current)

    assert not merged.local_question_resolved
    assert merged.local_question_changed
    assert merged.unresolved_now


def test_progress_evaluator_normalizes_resolved_question_to_climax() -> None:
    result = SessionProgressEvaluator._normalize_assessment(
        SessionProgressAssessment(
            stage="development",
            local_question_resolved=True,
            reversal_reached=True,
        )
    )

    assert result.stage == "climax"
    assert result.local_question_changed
    assert result.local_payoff_present


def test_conversation_quality_tracks_silence_echo_latency_and_semantic_failures() -> None:
    calls = [
        {
            "label": "玩家自由讨论 01",
            "message": "谁去盯门外，我继续和会长谈？",
            "reply": "",
            "elapsed_ms": 10,
            "body": {"target": "silent", "send_reply": False},
        },
        {
            "label": "玩家行动 02",
            "message": "伊莉雅把盾顶在门边，挡住第一轮冲击。",
            "reply": "伊莉雅把盾顶在门边，挡住第一轮冲击。随后门轴发出一声脆响。",
            "elapsed_ms": 100,
            "body": {},
        },
        {
            "label": "玩家自由讨论 03",
            "message": "我觉得先撤。",
            "reply": "风越来越急。",
            "elapsed_ms": 1000,
            "body": {"target": "gm", "send_reply": True},
        },
    ]
    assessments = [
        SessionProgressAssessment(
            npc_answer_complete=False,
            player_agency_preserved=False,
            continuity_ok=False,
            memory_image="白钟",
            memory_choice="交出钥匙",
            memory_consequence="正门封闭",
        )
    ]

    report = ConversationQualityAuditor().audit(calls, assessments)

    assert report.p50_latency_ms == 100
    assert report.p95_latency_ms == 1000
    assert report.player_echo_count == 1
    assert report.silence_recall == 0.5
    assert report.unnecessary_reply_count == 1
    assert report.npc_answer_failures == 1
    assert report.agency_violations == 1
    assert report.continuity_failures == 1
    assert report.complete_memory_anchors == 1


def test_conversation_quality_tracks_explicit_silence_precision_and_reply_recall() -> None:
    calls = [
        {
            "label": "桌边讨论",
            "reply": "我也补一句气氛。",
            "body": {"target": "fu_gm", "send_reply": True},
            "expected_target": "silent",
            "expected_send_reply": False,
        },
        {
            "label": "直接询问GM",
            "reply": "",
            "body": {"target": "silent", "send_reply": False},
            "expected_target": "fu_gm",
            "expected_send_reply": True,
        },
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.silence_recall == 0.0
    assert report.silence_precision == 0.0
    assert report.reply_recall == 0.0
    assert report.incorrect_silence_count == 1


def test_conversation_quality_audits_tool_receipts_and_public_state_claims() -> None:
    calls = [
        {
            "reply": "已记录这条世界设定。",
            "body": {
                "tool_receipts": [
                    {
                        "tool_name": "update_world_fact",
                        "ok": False,
                        "state_changed": False,
                        "error_code": "INVALID_ARGUMENTS",
                    },
                    {
                        "tool_name": "run_npc_turn",
                        "ok": False,
                        "state_changed": False,
                        "error_code": "NPC_DECISION_INVALID",
                    },
                    {
                        "tool_name": "update_world_fact",
                        "ok": True,
                        "state_changed": True,
                    },
                ],
            },
        },
        {
            "reply": "已保存当前战役。",
            "body": {
                "tool_receipts": [
                    {
                        "tool_name": "save_campaign",
                        "ok": False,
                        "state_changed": False,
                        "error_code": "TOOL_EXECUTION_FAILED",
                    }
                ]
            },
        },
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.successful_state_tool_receipts == 1
    assert report.failed_tool_receipts == 3
    assert report.recovered_tool_rejections == 0
    assert report.unrecovered_failed_tool_receipts == 3
    assert report.tool_validation_rejections == 1
    assert report.agent_output_retry_failures == 1
    assert report.tool_retry_recoveries == 1
    assert report.core_agent_unavailable_count == 0
    assert report.public_state_change_claims == 2
    assert report.unbacked_state_change_claims == 1
    assert report.failed_tool_success_claims == 1
    assert report.knowledge_action_consistency_rate == 0.5


def test_conversation_quality_detects_structurally_repeated_session_memories() -> None:
    assessments = [
        SessionProgressAssessment(
            memory_image="白花风铃在门边裂开",
            memory_choice="英雄把旧路钥匙交给旅人",
            memory_consequence="守望会封闭正门",
        ),
        SessionProgressAssessment(
            memory_image="白花风铃在门边碎裂",
            memory_choice="英雄把旧路钥匙交给失忆旅人",
            memory_consequence="守望会关闭正门",
        ),
    ]

    report = ConversationQualityAuditor().audit([], assessments)

    assert report.max_memory_anchor_similarity >= 0.72
    assert report.high_similarity_anchor_pairs


def test_conversation_quality_detects_old_npc_turn_embedded_after_new_check() -> None:
    old_reply = (
        "会长随巡守走到旧路路口，在能看见南岸土路的位置停下："
        "‘财团巡逻队还在远处，我们现在出发。’"
        "失忆旅人跟在伊莉雅身后：‘我跟上了。’"
    )
    calls = [
        {"label": "第01场行动 05", "reply": old_reply},
        {
            "label": "第01场待决回应 06.1",
            "reply": (
                "赛璃援用特质重掷，检定成功。旅人的右手立刻按住左侧肋下。"
                + old_reply.replace("。", "；")
            ),
        },
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.embedded_prior_gm_replays == 1


def test_conversation_quality_does_not_treat_reconnect_recap_as_embedded_replay() -> None:
    old_reply = "巡逻灯影在登记小室门缝前停留片刻，随后继续向风铃廊深处移动。"
    calls = [
        {"label": "第01场行动 05", "reply": old_reply},
        {
            "label": "第01场场景2断点现场回顾",
            "route": "/v1/game/scene-recap",
            "reply": "众人仍在白花碑驿站·登记小室。" + old_reply,
        },
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.embedded_prior_gm_replays == 0


def test_conversation_quality_detects_state_regression_promise_reopen_and_action_lane_loop() -> None:
    calls = [
        {"label": "玩家行动 01", "message": "伊莉雅贴近门缝观察巡逻灯火。", "reply": "财团巡逻队已经包围驿站。"},
        {"label": "玩家行动 02", "message": "洛岚蹲在门缝旁检查巡逻灯火。", "reply": "会长终于打开旧路，让你们通过。"},
        {"label": "玩家行动 03", "message": "赛璃继续贴着门缝观察巡逻灯火。", "reply": "财团巡逻队仍在逼近。"},
        {"label": "玩家行动 04", "message": "苍祈又从门缝查看巡逻灯火。", "reply": "只要再带回一份证据，我就开门。"},
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.irreversible_state_regressions >= 1
    assert report.fulfilled_promise_reopens >= 1
    assert report.repeated_player_action_lanes >= 1
    assert report.continuity_failures >= 2


def test_conversation_quality_uses_reviewed_action_goals_to_distinguish_followup_care() -> None:
    calls = [
        {
            "label": "第01场行动 15 白河",
            "message": "洛岚进入登记小室，问失忆旅人是否记得风铃或南岸旧道的称呼。",
            "reply": "我只记得南岸旧道。",
            "body": {
                "decision": {
                    "action_semantics_reviewed": True,
                    "action_goal": "确认失忆旅人是否保留与风铃或南岸旧道相关的称呼记忆。",
                }
            },
        },
        {
            "label": "第01场行动 16 时雨",
            "message": "艾薇娅进入登记小室，安抚失忆旅人并守望南岸旧道的动静。",
            "reply": "好，我留在这里。",
            "body": {
                "decision": {
                    "action_semantics_reviewed": True,
                    "action_goal": "安置并安抚失忆旅人，同时守望登记小室外的动静。",
                }
            },
        },
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.repeated_player_action_lanes == 0


def test_conversation_quality_does_not_treat_safe_passes_as_a_fictional_action_lane() -> None:
    calls = [
        {
            "label": f"第01场行动 {index:02d}",
            "message": f"英雄{index}暂时不采取行动。",
            "reply": "",
        }
        for index in range(1, 6)
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.repeated_player_action_lanes == 0


def test_conversation_quality_accepts_typed_group_movements_as_real_progress() -> None:
    calls = []
    for index, (actor, tool_name) in enumerate(
        [
            ("伊莉雅", "move_group_within_scene"),
            ("洛岚", "move_scene_group"),
            ("赛璃", "move_group_within_scene"),
            ("苍祈", "move_scene_group"),
        ],
        start=1,
    ):
        calls.append(
            {
                "label": f"第01场行动 {index:02d}",
                "message": f"{actor}沿风铃廊内侧跟上护送队，前往旧路入口。",
                "body": {
                    "tool_receipts": [
                        {
                            "tool_name": tool_name,
                            "ok": True,
                            "state_changed": True,
                            "result": {"actor": actor},
                        }
                    ]
                },
            }
        )

    report = ConversationQualityAuditor().audit(calls)

    assert report.repeated_player_action_lanes == 0


def test_conversation_quality_detects_npc_breaking_a_public_inspection_promise() -> None:
    calls = [
        {
            "label": "第01场行动 01",
            "reply": "财团使者说：先验这件遗物。验完之后，我只退开，不再碰登记小室里的别的东西。",
        },
        {
            "label": "第01场GM主动节拍 02",
            "reply": "使者收起验片，低声说：验到这里已经够了。她没有退开，反而屈指敲了敲门板。",
        },
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.npc_commitment_violations == 1
    assert report.continuity_failures >= 1


def test_player_simulator_rejects_a_rephrased_inspection_scope_question() -> None:
    recent_context = (
        "时雨: 我现在把遗物交给你看；你把你们验它的目的当场说清楚。\n"
        "时悠: 财团使者说：本次只验遗物，不验人，也不碰旧路。"
    )
    repeated = (
        "苍祈看着使者：你刚才说只验遗物，那现在把范围说清楚——"
        "是只看这枚遗物，还是连北侧旧阶也要一并记账？"
    )

    assert ConstrainedPlayerSimulator._repeats_recent_npc_question(repeated, recent_context)


def test_conversation_quality_detects_real_reopened_bargain_and_backstage_leak() -> None:
    calls = [
        {
            "reply": (
                "你们已经当众承诺，不会向财团透露旧路；这就够了。"
                "我现在放开风铃廊边门，让你们进入旧路闸门前室。"
            )
        },
        {
            "reply": (
                "我的条件还是这一个：先把碎月遗物交到封柜里。"
                "满足了，我就立刻放开风铃廊边门。"
            )
        },
        {
            "reply": (
                "角色的移动停在门入口当前这一侧。只描述眼前真实可见的阻挡与当前局面，"
                "不要替角色改做调查、交涉或其他行动。"
            )
        },
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.fulfilled_promise_reopens >= 1
    assert report.backstage_instruction_leaks == 1


def test_conversation_quality_detects_cross_family_door_loop_from_real_run() -> None:
    calls = [
        {
            "label": "第01场行动 12 阿凛",
            "message": "伊莉雅跟着会长进入前室，走到门边看木箱和旧锁槽还能不能再压一层。",
        },
        {
            "label": "第01场行动 14 南星",
            "message": "赛璃收住脚步，守在这里等会长拨开门闩。",
        },
        {
            "label": "第01场行动 15 白河",
            "message": "洛岚跟着会长进旧路闸门前室，留意木箱和旧锁槽里的简易门闩。",
        },
        {
            "label": "第01场行动 16 时雨",
            "message": "艾薇娅看着门边两个木箱，把这道门再压紧一层。",
        },
        {
            "label": "第01场行动 20 阿凛",
            "message": "伊莉雅跟着会长进前室，一进门就看木箱和旧锁槽，想把门再压紧一点。",
        },
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.repeated_player_action_lanes >= 1


def test_conversation_quality_does_not_treat_repeated_combat_target_as_stalled_lane() -> None:
    calls = [
        {"label": "第01场行动 01", "message": "伊莉雅挥剑攻击监察官艾蕾娜。"},
        {"label": "第01场行动 02", "message": "洛岚朝监察官艾蕾娜射击。"},
        {"label": "第01场行动 03", "message": "赛璃施放炎弹攻击监察官艾蕾娜。"},
        {"label": "第01场行动 04", "message": "苍祈再次攻击监察官艾蕾娜。"},
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.repeated_player_action_lanes == 0


def test_conversation_quality_treats_decisive_bargain_as_progression() -> None:
    calls = [
        {
            "label": "第01场行动 43 阿凛",
            "message": "伊莉雅问使者：你要的是南侧旧通道方向，还是整条去路？",
        },
        {
            "label": "第01场行动 44 白河",
            "message": "洛岚追问：拿这段方向交换，今晚具体能换来什么？",
        },
        {
            "label": "第01场行动 45 时雨",
            "message": (
                "艾薇娅告诉使者：我们不会交出完整去路，但我可以拿南侧旧通道方向"
                "来换你今晚不搜驿站。"
            ),
        },
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.repeated_player_action_lanes == 0


def test_fulfilled_promise_reopen_does_not_leak_across_sessions() -> None:
    calls = [
        {
            "label": "第01场行动 01",
            "reply": "会长终于打开旧路，让你们进入闸门前室。",
        },
        {
            "label": "第02场行动 01",
            "reply": "只要把遗失的徽章带回来，我就打开王城侧门。",
        },
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.fulfilled_promise_reopens == 0


def test_conversation_quality_does_not_mistake_a_conditional_offer_for_a_paid_payout() -> None:
    calls = [
        {
            "label": "第01场开场 01",
            "reply": "答得上失忆旅人为何对风铃有反应，我就让驿站为你们开门。",
        },
        {
            "label": "第01场行动 06",
            "reply": "这个条件已经满足。我现在为你们打开旧路。",
        },
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.fulfilled_promise_reopens == 0


def test_conversation_quality_does_not_mistake_an_imperative_gate_for_a_paid_payout() -> None:
    calls = [
        {
            "label": "第01场开场 01",
            "reply": "告诉我你们要把旅人送往哪一处安全地，以及为何还信旧路，我就替你们打开白花门。",
        },
        {
            "label": "第01场行动 01",
            "reply": "你们给出的目的地和理由已经足够，我现在就替你们打开白花门。",
        },
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.fulfilled_promise_reopens == 0


def test_fulfilled_promise_auditor_ignores_explicitly_excluded_extra_prices() -> None:
    calls = [
        {
            "label": "第01场行动 06",
            "reply": "白花碑下的通道已经开启，赛璃可以从旧潮汐排水沟离开。",
        },
        {
            "label": "第01场行动 10",
            "reply": (
                "这次只处理眼前提出的登记；交出封管、开启封口，以及把失名旅人交由财团收容，"
                "都不在这次范围内。"
            ),
        },
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.fulfilled_promise_reopens == 0


def test_fulfilled_promise_auditor_does_not_turn_a_discovered_door_into_an_npc_grant() -> None:
    calls = [
        {
            "label": "第02场待决回应 45.1",
            "reply": (
                "检修缝旁还留有一条通往候车厅后侧储物间的窄维护道，"
                "门栓在候车厅这一侧，可以不经过主廊打开。"
            ),
            "body": {
                "tool_receipts": [
                    {
                        "tool_name": "resolve_rule_window",
                        "ok": True,
                        "state_changed": True,
                    }
                ]
            },
        },
        {
            "label": "第02场GM主动节拍 48",
            "reply": (
                "引路人把木楔塞进检修板下沿；候车厅侧若要重新开启它，"
                "必须先拔出这枚木楔。"
            ),
            "body": {
                "tool_receipts": [
                    {
                        "tool_name": "decide_npc_action",
                        "ok": True,
                        "state_changed": True,
                    }
                ]
            },
        },
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.fulfilled_promise_reopens == 0


def test_conversation_quality_detects_clock_reopen_and_check_contradiction() -> None:
    calls = [
        {"reply": "【巡逻队包围】6/6。命刻【巡逻队包围】已完成。"},
        {"reply": "【巡逻队包围】2/6，脚步仍在逼近。"},
        {
            "reply": "伊莉雅：结算值 11 对抗难度等级 10，失败！但守卫宣布条件已经满足，随即放行。"
        },
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.retired_clock_reappearances == 1
    assert report.contradictory_check_responses == 1


def test_conversation_quality_detects_success_that_refuses_the_requested_answer() -> None:
    calls = [
        {
            "message": "洛岚观察冷白灯队列，判断它们会先封住哪一侧。",
            "reply": (
                "洛岚：结算值 14 对抗难度等级 13，成功！"
                "眼前没有足够连续的动向证明哪一侧会先被封住，"
                "因此目前无法可靠判断哪一侧。"
            ),
        }
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.contradictory_check_responses == 1


def test_conversation_quality_allows_success_answer_with_a_narrower_information_limit() -> None:
    calls = [
        {
            "message": "洛岚判断巡逻队是否沿某条路线朝驿站靠近。",
            "reply": (
                "洛岚：结算值 17 对抗难度等级 13，成功！"
                "冷白灯与路标的方向相互对应，巡逻队正沿南侧旧路朝驿站靠近；"
                "这次比对不能确定它们抵达的具体时间或队伍规模。"
            ),
        }
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.contradictory_check_responses == 0


def test_conversation_quality_detects_placeholder_and_premature_clock_consequence() -> None:
    calls = [
        {"reply": "【财团巡逻队逼近】1/8。远处又亮起一盏车灯。"},
        {"reply": "当前目标暂时没有给出更多线索。巡逻队已经停在门外。"},
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.vague_placeholder_gm_outputs == 1
    assert report.premature_clock_consequences == 1


def test_conversation_quality_allows_conditional_near_full_clock_warning() -> None:
    calls = [
        {"reply": "【财团巡逻队逼近】6/8。再拖下去，他们就会包围现场！"},
        {"reply": "【财团巡逻队逼近】7/8。巡逻队即将包围驿站。"},
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.premature_clock_consequences == 0


def test_conversation_quality_ignores_private_audit_drafts() -> None:
    calls = [
        {
            "route": "/v1/message/route",
            "reply": "【财团巡逻队逼近】1/8。远处的车灯仍在靠近。",
        },
        {
            "route": "/v1/audit/dashboard",
            "reply": "候选草稿：巡逻队已经包围驿站。",
        },
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.gm_reply_count == 1
    assert report.premature_clock_consequences == 0


def test_conversation_quality_does_not_treat_lifting_a_blockade_as_completion() -> None:
    calls = [
        {"reply": "【财团巡逻队逼近】5/8。远处灯影继续靠近。"},
        {"reply": "监察官解除一次临检封锁，让队伍一小时内不受巡逻队拦截。"},
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.premature_clock_consequences == 0


def test_conversation_quality_uses_authoritative_clock_consequence_not_unrelated_seal() -> None:
    boundary = {
        "name": "财团巡逻队逼近",
        "current": 1,
        "maximum": 8,
        "clock_type": "threat",
        "stakes": "填满后财团巡逻队包围白花碑驿站。",
        "completion_consequence": "财团巡逻队包围白花碑驿站。",
        "status": "active",
    }
    calls = [
        {
            "clock_boundaries": [boundary],
            "reply": "洛岚把旧闸门压回原位，出口从外侧封死，追兵暂时无法循通道追来。",
        },
        {
            "clock_boundaries": [boundary],
            "reply": "财团巡逻队已经包围白花碑驿站。",
        },
    ]

    report = ConversationQualityAuditor().audit(calls)

    assert report.premature_clock_consequences == 1
