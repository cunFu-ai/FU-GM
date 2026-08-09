from __future__ import annotations

from dataclasses import dataclass

from fu_gm.models import SessionDramaticContract, SessionEpisodeProgress


@dataclass(frozen=True)
class SessionBeatDirective:
    """One backstage purpose for a proactive GM beat."""

    stage: str
    purpose: str
    instruction: str
    require_material_change: bool = False
    require_consequence: bool = False
    require_local_change: bool = False
    require_local_resolution: bool = False
    require_signature_image_evolution: bool = False


class SessionBeatDirector:
    """Turn episode evidence into one movable GM beat, not a fixed plot.

    The dramatic contract supplies ingredients. Public play decides which
    ingredient is still useful and whether the beat is escalation, climax or
    aftermath. The director never chooses a player action or its result.
    """

    def build(
        self,
        *,
        contract: SessionDramaticContract,
        progress: SessionEpisodeProgress,
        requested_instruction: str = "",
        force_consequence: bool = False,
    ) -> SessionBeatDirective:
        stage = str(progress.stage or "opening").strip().lower()
        evidence = self._evidence_text(progress)
        active_scene = progress.scene_progress.get(progress.active_scene_id)
        scene_player_actions = active_scene.player_actions if active_scene is not None else 0
        scene_material_changes = active_scene.material_changes if active_scene is not None else 0
        last_purpose = progress.gm_beat_purposes[-1] if progress.gm_beat_purposes else ""
        last_beat_turn = (
            progress.gm_beat_player_turns[-1]
            if progress.gm_beat_player_turns
            else -1
        )
        player_turns_since_beat = max(0, progress.meaningful_turns - last_beat_turn)
        escalation = self._next_unused_escalation(
            contract.escalation_ladder,
            evidence,
            purposes=progress.gm_beat_purposes,
        )
        payoff = self._first_unused(contract.possible_payoffs, evidence)
        requested = str(requested_instruction or "").strip()

        # This request is emitted only after several heroes have already
        # committed to the same low-progress lane. It is an authored table
        # intervention, not a second timer poll, so it must be honored before
        # the ordinary duplicate-heartbeat silence guard below.
        if requested.startswith("【共同动作兑现】"):
            return self._directive(
                stage,
                "lane_refocus",
                requested
                + "不要用巧合替英雄完成调查，也不要直接公开核心谜团答案；如果需要新线索，只让可交互的痕迹或阻碍出现。",
                "",
                material=True,
            )

        # A prepared scene is private until the GM names a visible route and
        # returns the movement choice to the table.  This authored hand-off is
        # not a second fictional escalation, so it must not be swallowed by
        # the duplicate-heartbeat guard merely because another NPC just acted.
        if requested.startswith("【玩家主导转场】"):
            return self._directive(
                stage,
                "scene_transition_offer",
                requested,
                "",
                material=True,
            )

        if requested.startswith("【最终收束窗口】"):
            ending_echo = str(contract.ending_echo or contract.signature_image or "").strip()
            core = (
                "【结局提交】常规桌面时间已经用完，当前局面也已经产生足以结算的结果。"
                f"必须直接回答本场核心问题：{contract.dramatic_question}。"
                "答案可以是成功、失败或付出代价；只兑现玩家已经采取的做法与现场已经形成的后果，"
                "不得替英雄补做选择，也不得新增条件、线索、敌人、任务、核验或另一道同类障碍。"
            )
            if ending_echo:
                core += (
                    f"结尾重新落到本场已有的标志画面，并让它因实际结局发生可见变化：{ending_echo}。"
                )
            return self._directive(
                stage,
                "resolution_commit",
                core,
                requested,
                material=True,
                consequence=True,
                local_change=True,
                local_resolution=True,
                signature_image_evolution=True,
            )

        # Two timer polls without a player action in between are not two
        # fictional beats. A human GM would keep the table rather than add a
        # second warning, prop or NPC speech on top of the first one.
        if last_purpose and player_turns_since_beat == 0:
            return self._directive(
                stage,
                "hold",
                "【保持静默】上一项主动变化后还没有玩家行动，不追加第二项局面变化。",
                "",
            )

        if progress.closure_ready or stage == "closure":
            purpose = "aftermath"
            core = (
                "【余波收束】本场局部结果已经落地。只展示这个结果对人物、地点或关系造成的眼前余波，"
                "让标志画面因玩家选择出现可见变化；不要再加入敌人、线索、任务或新的倒计时。"
            )
            return self._directive(
                stage,
                purpose,
                core,
                requested,
                material=True,
                signature_image_evolution=True,
            )

        prior_payoff_did_not_resolve = bool(
            "climax_payoff" in progress.gm_beat_purposes[-3:]
            and player_turns_since_beat >= 2
            and not progress.local_question_resolved
            and (stage == "climax" or bool(progress.climax_events))
        )
        if prior_payoff_did_not_resolve:
            purpose = "resolution_commit"
            core = (
                "【结局提交】上一轮高潮动作已经发生，但本场核心问题仍没有答案。"
                "从最近公开对话中选取英雄已经表明的做法或现场已经形成的二选一，让NPC、对立方或环境"
                "现在完成相应结果，并明确淘汰、撤回、打破或完成当前障碍。"
                f"本场核心问题是：{contract.dramatic_question}。"
                "本段必须回答这个问题在当前场景里的结果；可以成功、失败或付出代价。"
                "不得新增仪器、标记、记录、取样、审批、核验、条件或功能相同的新阻碍，也不得替英雄改选。"
            )
            return self._directive(
                stage,
                purpose,
                core,
                requested,
                material=True,
                consequence=True,
                local_change=True,
                local_resolution=True,
            )

        choice_has_matured = bool(
            last_purpose == "force_choice"
            and player_turns_since_beat >= 2
            and not progress.local_question_resolved
        )
        climax_due = (
            force_consequence
            or stage == "climax"
            or bool(progress.climax_events)
            or choice_has_matured
        )
        if climax_due:
            purpose = "climax_payoff"
            payoff_text = payoff or "让本场核心问题得到明确答案或发生可见改变"
            core = (
                "【高潮提交】不要继续预警、逼近或要求第三次核验。让当前对立方、环境或NPC完成一个已经发生的动作，"
                f"并在现场兑现这一项局部结果：{payoff_text}。"
                f"本场核心问题是：{contract.dramatic_question}。"
                "结果可以成功、失败或带代价，但必须改变这个问题，且不得替英雄选择行动。"
            )
            return self._directive(
                stage,
                purpose,
                core,
                requested,
                material=True,
                # A climax beat must pay off the players' approach, but that
                # payoff need not already be the scene's irreversible ending.
                # If it changes the local problem without resolving it, the
                # director will promote the next beat to resolution_commit.
                consequence=force_consequence,
                local_change=True,
            )

        reversal_due = bool(progress.reversal_reached) or stage == "reversal"
        if reversal_due:
            purpose = "force_choice"
            core = (
                "【对决取舍】转折已经公开。让对立方按自己的目标立即采取一个具体行动，"
                "把两个不能同时保全的方向放到现场；不要再追加同类证据或让NPC重复条件。"
                f"对立方当前目标：{contract.opposition_goal}。"
            )
            return self._directive(stage, purpose, core, requested, material=True)

        if (
            stage == "development"
            and not progress.reversal_reached
            and (
                scene_player_actions >= 4
                or progress.stagnant_player_turns >= 3
            )
        ):
            purpose = "reversal"
            core = (
                "【理解转折】只在玩家已经实际接触过的证据、人物或地点上兑现一条会改变理解的事实；"
                "不要凭空把后台答案念出来，也不要靠偶然翻页、掉落或机关自解替玩家完成调查；"
                "若尚无成功行动或公开触发条件，只呈现可继续追查的矛盾、反应或痕迹，不直接解释答案。"
                f"可移动转折是：{contract.reversal}。"
            )
            return self._directive(stage, purpose, core, requested, material=True)

        prior_setup_beats = sum(
            purpose in {"strong_start", "escalation"}
            for purpose in progress.gm_beat_purposes
        )
        purpose = "escalation" if stage == "development" or prior_setup_beats else "strong_start"
        change = escalation
        if not change and not prior_setup_beats:
            change = contract.opening_disruption
        if not change:
            change = (
                "让当前对立方依据自身目标完成一个具体行动，并淘汰或改变眼前的一项障碍；"
                "不要再增加功能相同的新信标、路障、警告或核验步骤"
            )
        core = (
            "【局面推进】从最近公开行动直接接下去，只落实一个尚未发生的具体变化："
            f"{change}。不要复述玩家意图、背景摘要或已有警告；不要替英雄行动。"
        )
        if scene_player_actions and not scene_material_changes:
            core += (
                "当前镜头已经有人实际行动但尚无公开变化；这次必须让行动对象、现场人物、环境或对立方"
                "作出可观察回应，不能只补气氛。"
            )
        return self._directive(stage, purpose, core, requested, material=True)

    @staticmethod
    def _directive(
        stage: str,
        purpose: str,
        core: str,
        requested: str,
        *,
        material: bool = False,
        consequence: bool = False,
        local_change: bool = False,
        local_resolution: bool = False,
        signature_image_evolution: bool = False,
    ) -> SessionBeatDirective:
        instruction = core
        if requested:
            instruction += (
                "\n后台进展审计补充（它可能比最新公开对话滞后一拍；若玩家刚刚已经完成其中事项，"
                "必须忽略已完成部分，绝不能要求玩家重复）："
                + requested
            )
        return SessionBeatDirective(
            stage=stage,
            purpose=purpose,
            instruction=instruction,
            require_material_change=material,
            require_consequence=consequence,
            require_local_change=local_change,
            require_local_resolution=local_resolution,
            require_signature_image_evolution=signature_image_evolution,
        )

    @staticmethod
    def _evidence_text(progress: SessionEpisodeProgress) -> str:
        values = [
            *progress.concrete_consequences,
            *progress.local_payoffs,
            *progress.revealed_changes,
            *progress.climax_events,
            *progress.opposition_moves,
            *progress.public_images,
            progress.last_event,
        ]
        return "\n".join(str(value or "").strip() for value in values if str(value or "").strip())

    @staticmethod
    def _first_unused(candidates: list[str], evidence: str) -> str:
        compact_evidence = "".join(str(evidence or "").split())
        for candidate in candidates or []:
            clean = str(candidate or "").strip()
            if not clean:
                continue
            compact = "".join(clean.split())
            if compact and compact not in compact_evidence:
                return clean
        return ""

    @classmethod
    def _next_unused_escalation(
        cls,
        candidates: list[str],
        evidence: str,
        *,
        purposes: list[str],
    ) -> str:
        """Advance the prepared escalation ladder even when prose paraphrases it.

        Exact substring matching alone repeatedly selected the first ladder
        entry whenever the expression model reworded it.  The count of
        committed setup beats is authoritative, while the public-evidence
        check still skips entries that players triggered through another path.
        """

        clean_candidates = [str(item or "").strip() for item in candidates if str(item or "").strip()]
        used_count = sum(purpose in {"strong_start", "escalation"} for purpose in purposes)
        for candidate in clean_candidates[used_count:]:
            if cls._first_unused([candidate], evidence):
                return candidate
        return ""
