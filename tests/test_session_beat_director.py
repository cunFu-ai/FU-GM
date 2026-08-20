from fu_gm.components.session_beat_director import SessionBeatDirector
from fu_gm.models import SessionDramaticContract, SessionEpisodeProgress, SessionSceneProgress


def make_contract() -> SessionDramaticContract:
    return SessionDramaticContract(
        dramatic_question="队伍能否让守望会打开旧路？",
        opening_disruption="会长提出担保条件",
        opposition_goal="巡逻队要带走旅人",
        reversal="铃上的名字属于伊莉雅本应记得的人",
        escalation_ladder=["会长提出担保条件", "巡逻灯熄灭路标"],
        possible_payoffs=["旧路是否开放", "旅人是否安全"],
    )


def test_director_uses_unused_escalation_without_scripting_player_action() -> None:
    directive = SessionBeatDirector().build(
        contract=make_contract(),
        progress=SessionEpisodeProgress(stage="development", opposition_moves=["会长提出担保条件"]),
    )

    assert directive.purpose == "escalation"
    assert "巡逻灯熄灭路标" in directive.instruction
    assert "替英雄行动" in directive.instruction


def test_director_turns_climax_into_local_payoff_not_another_warning() -> None:
    directive = SessionBeatDirector().build(
        contract=make_contract(),
        progress=SessionEpisodeProgress(stage="climax", climax_events=["正面对决开始"]),
    )

    assert directive.purpose == "climax_payoff"
    assert not directive.require_consequence
    assert directive.require_local_change
    assert "旧路是否开放" in directive.instruction
    assert "不要继续预警" in directive.instruction


def test_director_closure_forbids_new_task() -> None:
    directive = SessionBeatDirector().build(
        contract=make_contract(),
        progress=SessionEpisodeProgress(stage="closure", closure_ready=True),
    )

    assert directive.purpose == "aftermath"
    assert directive.require_material_change
    assert directive.require_signature_image_evolution
    assert "不要再加入敌人、线索、任务" in directive.instruction


def test_optional_idle_holds_after_latest_player_material_change() -> None:
    progress = SessionEpisodeProgress(
        stage="development",
        meaningful_turns=5,
        last_player_material_change_turn=5,
        stagnant_player_turns=0,
    )

    directive = SessionBeatDirector().build(
        contract=make_contract(),
        progress=progress,
        requested_instruction=(
            "桌面在一个自然决定点停顿。只在现有NPC、环境或对立方确实应当行动时推进一个新变化；"
            "若玩家正在等彼此回应或局面无需GM介入，就保持静默。"
        ),
    )

    assert directive.purpose == "hold"
    assert not directive.require_material_change


def test_director_uses_current_scene_actions_instead_of_old_session_turn_total() -> None:
    progress = SessionEpisodeProgress(
        stage="development",
        meaningful_turns=20,
        active_scene_id="fresh-scene",
        scene_progress={
            "fresh-scene": SessionSceneProgress(
                scene_id="fresh-scene",
                player_actions=1,
            )
        },
    )

    directive = SessionBeatDirector().build(
        contract=make_contract(),
        progress=progress,
    )

    assert directive.purpose == "escalation"
    assert "理解转折" not in directive.instruction
    assert "尚无公开变化" in directive.instruction


def test_director_does_not_stack_two_beats_before_a_player_can_respond() -> None:
    progress = SessionEpisodeProgress(
        stage="development",
        meaningful_turns=8,
        gm_beat_purposes=["escalation"],
        gm_beat_player_turns=[8],
    )

    directive = SessionBeatDirector().build(
        contract=make_contract(),
        progress=progress,
    )

    assert directive.purpose == "hold"
    assert directive.instruction.startswith("【保持静默】")


def test_director_advances_the_escalation_ladder_when_expression_paraphrased() -> None:
    progress = SessionEpisodeProgress(
        stage="development",
        meaningful_turns=8,
        gm_beat_purposes=["strong_start"],
        gm_beat_player_turns=[4],
    )

    directive = SessionBeatDirector().build(
        contract=make_contract(),
        progress=progress,
    )

    assert directive.purpose == "escalation"
    assert "巡逻灯熄灭路标" in directive.instruction
    assert "会长提出担保条件" not in directive.instruction


def test_unanswered_force_choice_matures_into_a_payoff_instead_of_repeating() -> None:
    progress = SessionEpisodeProgress(
        stage="reversal",
        meaningful_turns=12,
        gm_beat_purposes=["force_choice"],
        gm_beat_player_turns=[9],
    )

    directive = SessionBeatDirector().build(
        contract=make_contract(),
        progress=progress,
    )

    assert directive.purpose == "climax_payoff"
    assert not directive.require_consequence
    assert directive.require_local_change
    assert "不要继续预警" in directive.instruction


def test_explicit_forced_climax_still_requires_irreversible_consequence() -> None:
    directive = SessionBeatDirector().build(
        contract=make_contract(),
        progress=SessionEpisodeProgress(stage="development"),
        force_consequence=True,
    )

    assert directive.purpose == "climax_payoff"
    assert directive.require_consequence
    assert directive.require_local_change


def test_named_situation_commit_remains_the_authoritative_beat() -> None:
    request = (
        "【局势提交】让监察官艾蕾娜带领财团机兵抵达白花碑驿站并封住旧路。"
    )

    directive = SessionBeatDirector().build(
        contract=make_contract(),
        progress=SessionEpisodeProgress(stage="development"),
        requested_instruction=request,
        force_consequence=True,
    )

    assert directive.purpose == "forced_situation_commit"
    assert directive.require_material_change
    assert directive.require_consequence
    assert directive.require_local_change
    assert "监察官艾蕾娜" in directive.instruction
    assert "后台进展审计补充" not in directive.instruction
    assert "泛化" not in directive.instruction


def test_named_climax_commit_is_not_replaced_by_generic_payoff() -> None:
    request = "【高潮提交】艾蕾娜关闭旧路闸门，迫使双方在门前对峙。"

    directive = SessionBeatDirector().build(
        contract=make_contract(),
        progress=SessionEpisodeProgress(stage="climax"),
        requested_instruction=request,
        force_consequence=True,
    )

    assert directive.purpose == "forced_climax_commit"
    assert directive.require_consequence
    assert "艾蕾娜关闭旧路闸门" in directive.instruction
    assert "后台进展审计补充" not in directive.instruction


def test_changed_but_unresolved_force_choice_still_matures_into_payoff() -> None:
    progress = SessionEpisodeProgress(
        stage="reversal",
        meaningful_turns=12,
        local_question_changed=True,
        local_question_resolved=False,
        gm_beat_purposes=["force_choice"],
        gm_beat_player_turns=[9],
    )

    directive = SessionBeatDirector().build(
        contract=make_contract(),
        progress=progress,
    )

    assert directive.purpose == "climax_payoff"
    assert "不要继续预警" in directive.instruction


def test_unresolved_climax_payoff_escalates_to_resolution_commit() -> None:
    progress = SessionEpisodeProgress(
        stage="climax",
        meaningful_turns=18,
        local_question_changed=True,
        local_question_resolved=False,
        gm_beat_purposes=["force_choice", "climax_payoff"],
        gm_beat_player_turns=[11, 15],
    )

    directive = SessionBeatDirector().build(
        contract=make_contract(),
        progress=progress,
    )

    assert directive.purpose == "resolution_commit"
    assert directive.require_consequence
    assert directive.require_local_change
    assert directive.require_local_resolution
    assert "不得新增仪器、标记、记录、取样" in directive.instruction


def test_local_payoff_during_reversal_does_not_force_the_whole_episode_to_end() -> None:
    progress = SessionEpisodeProgress(
        stage="reversal",
        meaningful_turns=18,
        local_question_changed=True,
        local_question_resolved=False,
        gm_beat_purposes=["force_choice", "climax_payoff"],
        gm_beat_player_turns=[11, 15],
    )

    directive = SessionBeatDirector().build(
        contract=make_contract(),
        progress=progress,
    )

    assert directive.purpose == "force_choice"
    assert directive.require_material_change
    assert not directive.require_local_resolution


def test_saturated_action_lane_opens_a_new_situation_instead_of_forcing_episode_resolution() -> None:
    progress = SessionEpisodeProgress(
        stage="reversal",
        meaningful_turns=18,
        local_question_changed=True,
        local_question_resolved=False,
        gm_beat_purposes=["force_choice", "climax_payoff"],
        gm_beat_player_turns=[11, 15],
    )
    request = (
        "【共同动作兑现】英雄已经落实护送与警戒。"
        "让世界发生一个具体变化，给出新的可互动局面。"
    )

    directive = SessionBeatDirector().build(
        contract=make_contract(),
        progress=progress,
        requested_instruction=request,
    )

    assert directive.purpose == "lane_refocus"
    assert directive.require_material_change
    assert not directive.require_consequence
    assert not directive.require_local_resolution
    assert directive.instruction.startswith(request)
    assert "不要用巧合替英雄完成调查" in directive.instruction


def test_saturated_lane_request_overrides_duplicate_heartbeat_hold() -> None:
    progress = SessionEpisodeProgress(
        stage="reversal",
        meaningful_turns=15,
        gm_beat_purposes=["reveal"],
        gm_beat_player_turns=[15],
    )
    request = "【共同动作兑现】队伍已经完成共同撤离；现在让场景抵达新的局面。"

    directive = SessionBeatDirector().build(
        contract=make_contract(),
        progress=progress,
        requested_instruction=request,
    )

    assert directive.purpose == "lane_refocus"
    assert directive.require_material_change
    assert directive.instruction.startswith(request)
    assert "不要直接公开核心谜团答案" in directive.instruction


def test_player_led_transition_offer_overrides_duplicate_heartbeat_hold() -> None:
    progress = SessionEpisodeProgress(
        stage="climax",
        meaningful_turns=15,
        gm_beat_purposes=["climax_payoff"],
        gm_beat_player_turns=[15],
    )
    request = (
        "【玩家主导转场】当前仍在【白花碑驿站·登记小室】。"
        "明确呈现通往【白花碑驿站·旧路闸门】的去路，但不要替英雄出发。"
    )

    directive = SessionBeatDirector().build(
        contract=make_contract(),
        progress=progress,
        requested_instruction=request,
    )

    assert directive.purpose == "scene_transition_offer"
    assert directive.require_material_change
    assert directive.instruction == request


def test_final_closure_window_commits_success_failure_or_costly_resolution() -> None:
    contract = make_contract()
    contract.signature_image = "白花风铃比四周慢半拍。"
    contract.ending_echo = "结尾回到白花风铃，让铃声因实际结局发生变化。"
    progress = SessionEpisodeProgress(
        stage="development",
        meaningful_turns=46,
        gm_beat_purposes=["reversal"],
        gm_beat_player_turns=[46],
    )

    directive = SessionBeatDirector().build(
        contract=contract,
        progress=progress,
        requested_instruction="【最终收束窗口】直接兑现已经成熟的结果。",
    )

    assert directive.purpose == "resolution_commit"
    assert directive.require_material_change
    assert directive.require_consequence
    assert directive.require_local_change
    assert directive.require_local_resolution
    assert directive.require_signature_image_evolution
    assert "成功、失败或付出代价" in directive.instruction
    assert "白花风铃" in directive.instruction
    assert "不得替英雄补做选择" in directive.instruction
