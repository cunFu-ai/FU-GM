from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timezone

from fu_gm.components.campaign_feedback_controller import CampaignFeedbackControl
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.npc_role_profiles import (
    DEFAULT_AUTHORITY_SCOPE,
    local_role_profile,
)
from fu_gm.components.session_prep_concretizer import SessionPrepConcretizer
from fu_gm.components.story_arc_manager import StoryArcManager
from fu_gm.components.world_state import WorldState
from fu_gm.models import (
    CampaignPacingProfile,
    NPCPersona,
    SessionClueRoute,
    SessionDramaticContract,
    SessionEpisodeProgress,
    SessionNPCRole,
    SessionSceneOpportunity,
    StoryArcPhase,
)


class SessionContractPlanner:
    """Prepare a playable situation, never a predetermined episode script."""

    _GENERIC_PRESSURE_ACTORS = {
        "",
        "世界威胁",
        "现场阻力",
        "对立方",
        "未知敌人",
        "反派",
    }
    _NPC_ROLE_TERMS = (
        "监察官",
        "会长",
        "巡守长",
        "守门人",
        "钟匠",
        "掌柜",
        "祭司",
        "书记官",
        "队长",
        "领主",
        "旅人",
        "使者",
        "船长",
        "村长",
        "骑士",
        "神官",
        "学者",
        "商人",
    )
    _FACTION_SUFFIXES = (
        "财团",
        "司教团",
        "教团",
        "守望会",
        "行会",
        "王室",
        "帝国",
        "王国",
        "公国",
        "联盟",
        "军团",
        "教会",
        "协会",
        "公司",
    )

    def __init__(
        self,
        story_arc_manager: StoryArcManager,
        world_state: WorldState,
        *,
        character_manager: CharacterManager | None = None,
        client=None,
        model: str = "",
        review_client=None,
        review_model: str = "",
    ) -> None:
        self.story_arc_manager = story_arc_manager
        self.world_state = world_state
        self.character_manager = character_manager
        self.concretizer = SessionPrepConcretizer(
            client=client,
            model=model,
            review_client=review_client,
            review_model=review_model,
        )

    def create(
        self,
        *,
        session_number: int,
        phase: StoryArcPhase,
        profile: CampaignPacingProfile,
        feedback: CampaignFeedbackControl,
        allow_model_prep: bool = True,
        deadline: float | None = None,
        register_npcs: bool = True,
        preparation_source: str = "foreground",
    ) -> SessionDramaticContract:
        del profile  # The campaign phase already reflects the selected length.
        state = self.story_arc_manager.state
        agenda = state.agenda
        recent_contracts = state.session_contract_history[-3:]
        recent_focus = {item.focus_thread for item in recent_contracts if item.focus_thread}
        recent_locations = {item.location for item in recent_contracts[-2:] if item.location}
        previous_progress = (
            state.session_progress_history[-1]
            if state.session_progress_history
            else None
        )
        inherited_consequence = str(
            getattr(previous_progress, "memory_consequence", "")
            or getattr(previous_progress, "last_event", "")
            or ""
        ).strip()
        active_chapter = self.world_state.active_chapter()
        chapter_already_used = bool(
            active_chapter
            and any(
                active_chapter.chapter_title
                and active_chapter.chapter_title in str(item.title or "")
                and item.status == "completed"
                for item in state.session_contract_history
            )
        )
        chapter = active_chapter if active_chapter is not None and not chapter_already_used else None

        active_threads = [
            item for item in state.threads if item.status not in {"resolved", "abandoned"}
        ]
        fresh_threads = [item for item in active_threads if item.title not in recent_focus]
        thread_pool = fresh_threads or active_threads
        confirmed_first_act = self._confirmed_first_act_thread(
            active_threads,
            session_number=session_number,
        )
        focus = confirmed_first_act or max(
            thread_pool,
            key=lambda item: (int(item.priority or 0), int(item.progress or 0)),
            default=None,
        )

        chapter_location = self._chapter_location_anchor(chapter)
        if chapter_location:
            # 章节包是本场已确认的可玩骨架。Session 0 可能只把
            # starting_region 写成“边境驿站”这类宽泛描述，但不能因此
            # 让“白花碑驿站·风铃廊”中的必需人物和场景失效。
            location = chapter_location
            location_state = next(
                (
                    item
                    for item in state.locations
                    if str(item.location or "").strip() == chapter_location
                ),
                None,
            )
        else:
            location_state = self._select_location(
                session_number=session_number,
                focus=focus,
                recent_contracts=recent_contracts,
                recent_locations=recent_locations,
            )
            location = (
                location_state.location
                if location_state is not None
                else self.world_state.world_profile.starting_region or "当前地点"
            )

        hero_drafts = [
            draft
            for draft in self.world_state.world_profile.hero_drafts.values()
            if draft.hero_name or draft.player_name
        ]
        recent_spotlights = {
            item.spotlight_hero for item in recent_contracts[-2:] if item.spotlight_hero
        }
        fresh_heroes = [
            draft
            for draft in hero_drafts
            if (draft.hero_name or draft.player_name) not in recent_spotlights
        ]
        hero_pool = fresh_heroes or hero_drafts
        spotlight = ""
        if hero_pool:
            draft = hero_pool[(session_number - 1) % len(hero_pool)]
            spotlight = draft.hero_name or draft.player_name

        agenda_focus = next(
            (str(item).strip() for item in agenda.recommended_focus if str(item).strip()),
            "",
        )
        focus_title = (
            self._playable_focus_title(focus.title, focus.summary)
            if focus is not None
            else agenda_focus or "眼前尚未解决的麻烦"
        )
        focus_summary = (
            focus.summary
            if focus is not None
            else "英雄必须决定眼前的人与代价哪一个更重要"
        )
        if chapter is not None:
            focus_title = str(chapter.chapter_title or focus_title).strip()
            focus_summary = str(chapter.synopsis or focus_summary).strip()
        elif confirmed_first_act is not None:
            focus_title = self._concise_first_act_title(focus_summary)

        ready_reveals = [
            item for item in state.reveals if item.status in {"ready", "seeded"}
        ]
        reveal = (
            ready_reveals[0]
            if (
                confirmed_first_act is None
                and ready_reveals
                and not feedback.clarify_reveal_due
            )
            else None
        )
        villain_tracks = [
            item for item in state.villain_pressure if item.current < item.segments
        ]
        # 已确认的第一幕是当前场次的局部权威边界。全局威胁仍保留在
        # 战役状态中，但不能越过玩家选定的开场，冒充监狱现场的对立方。
        villain = (
            villain_tracks[0]
            if villain_tracks and confirmed_first_act is None
            else None
        )
        opposition_goal = (
            villain.goal
            if villain is not None
            else f"现场阻力希望维持【{focus_title}】当前的糟糕状态"
        )
        if confirmed_first_act is not None:
            opposition_goal = self._first_act_local_opposition(
                focus_title=focus_title,
                focus_summary=focus_summary,
                location=location,
            )
        if chapter is not None and chapter.adversary_notes:
            opposition_goal = self._chapter_opposition_goal(
                chapter.adversary_notes[0],
                fallback=opposition_goal,
            )

        climax_type = self._climax_type(
            phase,
            agenda.suggested_scene_type,
            focus.thread_type if focus else "",
        )
        opposition_actor = self._named_actor_from_goal(opposition_goal)
        pressure_actor = self._usable_pressure_actor(
            str(getattr(villain, "villain", "") or "")
        )
        villain_move = (
            f"【{opposition_actor or pressure_actor}】推进“{villain.goal}”造成的公开后果"
            if villain is not None and (opposition_actor or pressure_actor)
            else (
                f"{opposition_goal.rstrip('。')}，并让后果在现场变得可见"
                if opposition_goal
                else "现场阻力采取一次合乎动机的行动"
            )
        )
        reveal_subject = self._playable_reveal_title(reveal) if reveal is not None else ""
        reveal_text = (
            f"有关【{reveal_subject}】的一条可验证证据"
            if reveal is not None
            else f"【{focus_title}】表面解释中的一处可验证矛盾"
        )
        fresh_disruption = (
            f"与【{focus_title}】直接相关的一次突发机会打破【{location}】的僵局。"
            if confirmed_first_act is not None
            else (
                str(agenda.pressure_moves[0]).strip()
                if agenda.pressure_moves
                else f"上场选择的一项后果打破【{location}】的日常。"
            )
        )
        if feedback.villain_move_due:
            fresh_disruption = f"{villain_move}直接打破【{location}】的日常。"
        opening_disruption = (
            f"先让上一场后果在现场可见：{inherited_consequence}；随后，{fresh_disruption}"
            if inherited_consequence
            else fresh_disruption
        )
        if chapter is not None and chapter.intro_prompt:
            opening_disruption = str(chapter.intro_prompt).strip()
        signature_image = self._signature_image(
            location=location,
            location_state=location_state,
            focus=focus,
            inherited_consequence=inherited_consequence,
            recent_contracts=recent_contracts,
            location_detail=(
                self.world_state.world_profile.major_locations.get(location)
                or self.world_state.world_profile.kingdoms.get(location)
                or ""
            ),
            iconic_elements=(list(chapter.iconic_elements) if chapter is not None else []),
        )
        important_npcs = self._important_npcs(
            location=location,
            focus=focus,
            villain=villain,
            opposition_goal=opposition_goal,
            chapter=chapter,
        )
        clue_routes = self._clue_routes(
            session_number=session_number,
            location=location,
            conclusion=reveal_text,
            signature_image=signature_image,
            important_npcs=important_npcs,
        )
        fantastic_details = self._fantastic_details(
            location=location,
            location_state=location_state,
            signature_image=signature_image,
            opening_disruption=opening_disruption,
        )
        potential_scenes = self._potential_scenes(
            session_number=session_number,
            location=location,
            focus_title=focus_title,
            focus_summary=focus_summary,
            opening_disruption=opening_disruption,
            opposition_goal=opposition_goal,
            villain_move=villain_move,
            signature_image=signature_image,
            clue_routes=clue_routes,
            important_npcs=important_npcs,
        )
        if chapter is not None and chapter.scenes:
            potential_scenes = self._chapter_scene_opportunities(
                session_number=session_number,
                location=location,
                chapter=chapter,
                clue_routes=clue_routes,
                important_npcs=important_npcs,
                opposition_goal=opposition_goal,
            )

        title = (
            str(chapter.chapter_title).strip()
            if chapter is not None and str(chapter.chapter_title or "").strip()
            else f"第{session_number:02d}场·{focus_title}"
        )
        dramatic_question = self._dramatic_question(
            location=location,
            focus_title=focus_title,
            focus_summary=focus_summary,
            chapter=chapter,
        )
        dilemma = (
            f"处理“{focus_summary}”会迫使英雄在两种都合理、代价不同的方向间选择；"
            "不预设答案。"
        )
        closure_requirement = (
            str(chapter.conclusion_prompt).strip()
            if chapter is not None and str(chapter.conclusion_prompt or "").strip()
            else (
                f"本场必须解决或实质改变【{focus_title}】；"
                "不能只发现线索就收团。"
            )
        )
        escalation_ladder = self._playable_escalation_ladder(
            potential_scenes=potential_scenes,
            villain_move=villain_move,
            focus_title=focus_title,
        )
        possible_payoffs = self._playable_payoffs(potential_scenes)
        irreversible_change = self._irreversible_change_requirement(
            potential_scenes=potential_scenes,
            closure_requirement=closure_requirement,
        )
        ending_echo = self._ending_echo(signature_image)
        contract = SessionDramaticContract(
            session_number=session_number,
            title=title,
            location=location,
            dramatic_question=dramatic_question,
            local_question_key=f"{location}::{focus_title}",
            opening_disruption=opening_disruption,
            signature_image=signature_image,
            spotlight_hero=spotlight,
            focus_thread=focus_title,
            opposition_goal=opposition_goal,
            dilemma=dilemma,
            reversal=f"{reveal_text}改变英雄对眼前问题的理解，但不替他们决定下一步。",
            climax_type=climax_type,
            closure_requirement=closure_requirement,
            situation_facts=[
                f"当前地点是【{location}】，现场问题围绕【{focus_title}】。",
                f"对立方当前目标是：{opposition_goal}。",
                "玩家已经得知的事实不可被幕后修改。",
            ],
            flexible_secrets=[
                f"{reveal_text}可附着在玩家实际调查、交涉或仪式触及的合适对象上。",
                "尚未公开的解释可随玩家行动调整，但不能抹去已公开事实。",
            ],
            opening_equipment_restrictions=[
                dict(item)
                for item in list(
                    self.world_state.world_profile.first_act_opening_equipment_restrictions
                    or []
                )
                if isinstance(item, dict)
            ],
            potential_scenes=potential_scenes,
            clue_routes=clue_routes,
            important_npcs=important_npcs,
            fantastic_details=fantastic_details,
            escalation_ladder=escalation_ladder,
            possible_payoffs=possible_payoffs,
            irreversible_change=irreversible_change,
            ending_echo=ending_echo,
            stinger="先让本场结局落地，再用一个短画面展示后果继续扩散。",
            callback_seed=(
                f"开局前段必须让上一场后果“{inherited_consequence}”以人物反应、地点变化或现实代价出现；不要复述摘要。"
                if inherited_consequence
                else "下一场回收本场的一项选择后果、NPC态度或物件变化，不复述摘要。"
            ),
            inherited_consequence=inherited_consequence,
            memory_anchor=(
                f"一个画面：{signature_image}；"
                f"一个玩家选择：玩家如何回答“{dramatic_question}”；"
                f"一个可追踪后果：{irreversible_change}"
            ),
        )
        world_context = self._world_context(
            focus_title=focus_title,
            focus_summary=focus_summary,
            location=location,
            opposition_goal=opposition_goal,
            spotlight=spotlight,
            chapter=chapter,
        )
        contract = self.concretizer.concretize(
            contract,
            world_context=world_context,
            recent_contracts=recent_contracts,
            allow_model=allow_model_prep,
            deadline=deadline,
        )
        contract.preparation_fingerprint = str(
            self.concretizer.last_request_fingerprint or ""
        )
        contract.preparation_status = (
            "degraded"
            if str(self.concretizer.last_error or "").strip()
            else "ready"
        )
        contract.preparation_source = str(
            preparation_source or "foreground"
        ).strip()
        contract.prepared_at = datetime.now(timezone.utc).isoformat()
        if register_npcs:
            self._register_session_npcs(contract)
        return contract

    @staticmethod
    def _playable_payoffs(
        potential_scenes: list[SessionSceneOpportunity],
    ) -> list[str]:
        """Use prepared scene outcomes when the optional prep LLM times out."""

        candidates: list[str] = []
        for scene in potential_scenes:
            if scene.scene_role == "aftermath":
                continue
            candidates.extend(
                str(item).strip()
                for item in scene.possible_changes
                if str(item).strip()
            )
        candidates.extend(
            [
                "一个具名NPC或派系依据英雄的做法改变公开立场。",
                "一个已经出现的地点、命刻或资源状态因玩家选择而改变。",
                "一条已经出现的线索获得用途或被证伪，而不是只增加新谜团。",
            ]
        )
        return list(dict.fromkeys(candidates))[:4]

    @staticmethod
    def _playable_escalation_ladder(
        *,
        potential_scenes: list[SessionSceneOpportunity],
        villain_move: str,
        focus_title: str,
    ) -> list[str]:
        active = [item for item in potential_scenes if item.scene_role != "aftermath"]
        first = active[0] if active else None
        climax = next(
            (item for item in reversed(active) if item.scene_role == "climax_candidate"),
            active[-1] if active else None,
        )
        actor = ""
        if first is not None and first.npc_names:
            actor = next(
                (name for name in first.npc_names if "旅人" not in name),
                first.npc_names[0],
            )
        first_move = (
            f"【{actor}】围绕【{first.title}】公开一项可完成的条件，并立即改变现场处置。"
            if first is not None and actor
            else f"【{first.title}】中的现场人物按自身目标采取一个可观察行动。"
            if first is not None
            else f"与【{focus_title}】直接相关的对立方采取一个可观察行动。"
        )
        climax_move = (
            f"若英雄仍未改变局势，【{climax.title}】立即发生：{climax.situation}"
            if climax is not None
            else f"若英雄仍未改变局势，【{focus_title}】产生一项眼前可处理的后果。"
        )
        return list(
            dict.fromkeys(
                item
                for item in (first_move, str(villain_move or "").strip(), climax_move)
                if item
            )
        )

    @staticmethod
    def _irreversible_change_requirement(
        *,
        potential_scenes: list[SessionSceneOpportunity],
        closure_requirement: str,
    ) -> str:
        climax = next(
            (
                item
                for item in reversed(potential_scenes)
                if item.scene_role == "climax_candidate"
            ),
            None,
        )
        if climax is not None:
            subjects = "、".join(climax.required_elements[:2]) or climax.title
            return (
                f"本场结束后，记录【{climax.title}】实际落地的结果；"
                f"已经公开的{subjects}状态或关键人物去向不得在下一场无故复原。"
            )
        return (
            f"本场结束后，把“{closure_requirement}”对应的实际结果记录为公开事实；"
            "不得在下一场无故复原。"
        )

    @staticmethod
    def _ending_echo(signature_image: str) -> str:
        return (
            f"收束时再次呈现这一标志画面：{signature_image}；"
            "让其中一个已经出现的细节因玩家的实际选择发生可见变化。"
        )

    @staticmethod
    def _playable_focus_title(title: str, summary: str) -> str:
        """Use complete thread text in play; ellipsized titles are UI labels."""

        clean_title = " ".join(str(title or "").split()).strip()
        clean_summary = " ".join(str(summary or "").split()).strip()
        if clean_summary and (clean_title.endswith("...") or clean_title.endswith("…")):
            return clean_summary
        return clean_title or clean_summary or "眼前尚未解决的麻烦"

    @staticmethod
    def _concise_first_act_title(summary: str) -> str:
        """从已确认第一幕摘要中提取稳定的局部主题名。"""

        clean = " ".join(str(summary or "").split()).strip()
        for separator in ("：", ":"):
            prefix = clean.split(separator, 1)[0].strip()
            if prefix and len(prefix) <= 24:
                return prefix
        return clean or "第一幕"

    @staticmethod
    def _first_act_local_opposition(
        *,
        focus_title: str,
        focus_summary: str,
        location: str,
    ) -> str:
        """只从第一幕公开共识生成眼前阻力，不借用全局威胁。"""

        evidence = f"{focus_title} {focus_summary} {location}"
        if any(marker in evidence for marker in ("越狱", "监狱", "关押", "牢房")):
            return f"【{location}】的看守与追捕者要恢复封锁，阻止英雄逃出监狱"
        return f"【{location}】的现场对立方要维持现状，阻止英雄推进【{focus_title}】"

    @staticmethod
    def _playable_reveal_title(reveal) -> str:
        title = " ".join(str(getattr(reveal, "title", "") or "").split()).strip()
        secret = " ".join(str(getattr(reveal, "secret", "") or "").split()).strip()
        if secret and (title.endswith("...") or title.endswith("…")):
            return secret
        return title or secret or "眼前谜团"

    @classmethod
    def _dramatic_question(
        cls,
        *,
        location: str,
        focus_title: str,
        focus_summary: str,
        chapter,
    ) -> str:
        conclusion = str(getattr(chapter, "conclusion_prompt", "") or "").strip()
        if conclusion:
            concrete = conclusion.rstrip("。！？? ")
            concrete = re.sub(r"^当(?:队伍|英雄们?|玩家们?)", "", concrete)
            concrete = re.sub(r"时[，,]?本章进入收束$", "", concrete)
            concrete = re.sub(r"^本场结束前(?:必须|需要)", "", concrete)
            concrete = concrete.strip("，,；; ")
            if concrete:
                return f"英雄能否在本场结束前{concrete}？"
        summary = " ".join(str(focus_summary or "").split()).strip()
        if summary and summary != focus_title:
            return f"在【{location}】，英雄会如何处理这件事：{summary.rstrip('。')}？"
        return f"英雄会如何改变【{focus_title}】，谁又要承担结果？"

    @classmethod
    def _chapter_opposition_goal(cls, raw: object, *, fallback: str) -> str:
        text = " ".join(str(raw or "").split()).strip()
        if not text:
            return fallback
        actor = cls._named_actor_from_goal(text)
        backstage = any(
            marker in text
            for marker in (
                "不要替玩家",
                "不得替玩家",
                "不能替玩家",
                "应主动推进",
                "需要主动推进",
            )
        )
        if backstage:
            if actor and fallback:
                return f"{actor}要推进这项计划：{fallback.rstrip('。')}"
            return fallback or text.split("但", 1)[0].rstrip("。")
        return text

    @classmethod
    def _named_actor_from_goal(cls, goal: str) -> str:
        text = " ".join(str(goal or "").split()).strip()
        if not text:
            return ""
        roles = "|".join(re.escape(item) for item in cls._NPC_ROLE_TERMS)
        match = re.search(
            rf"((?:{roles})[\u4e00-\u9fffA-Za-z0-9·]{{1,12}}?)(?="
            r"曾经是|曾是|原本是|原是|认为|相信|主张|应|要|想|正|必须|试图|希望|计划|企图|负责|率领|会|把|将|[，,。；;：:]|$)",
            text,
        )
        if match:
            return match.group(1).strip()
        return ""

    @classmethod
    def _usable_pressure_actor(cls, actor: str) -> str:
        clean = " ".join(str(actor or "").split()).strip()
        return "" if clean in cls._GENERIC_PRESSURE_ACTORS else clean

    @classmethod
    def _looks_like_person(cls, name: str) -> bool:
        clean = cls._usable_pressure_actor(name)
        if not clean or clean.endswith(cls._FACTION_SUFFIXES):
            return False
        if any(term in clean for term in cls._NPC_ROLE_TERMS):
            return cls._named_actor_from_goal(clean) == clean
        return bool(re.fullmatch(r"[\u4e00-\u9fffA-Za-z·]{2,10}", clean))

    def repair_legacy_contract_identity(
        self,
        contract: SessionDramaticContract,
    ) -> SessionDramaticContract:
        """Replace an old ellipsized thread label with its complete summary."""

        short = str(contract.focus_thread or "").strip()
        if not short or not (short.endswith("...") or short.endswith("…")):
            return contract
        prefix = short.rstrip(".…")
        thread = next(
            (
                item
                for item in self.story_arc_manager.state.threads
                if str(item.title or "").strip() == short
                or str(item.summary or "").strip().startswith(prefix)
            ),
            None,
        )
        full = " ".join(str(getattr(thread, "summary", "") or "").split()).strip()
        if not full or full == short:
            return contract

        def repaired(value: str) -> str:
            return str(value or "").replace(short, full)

        fixed = replace(
            contract,
            title=repaired(contract.title),
            dramatic_question=repaired(contract.dramatic_question),
            local_question_key=repaired(contract.local_question_key),
            focus_thread=full,
            closure_requirement=repaired(contract.closure_requirement),
            situation_facts=[repaired(item) for item in contract.situation_facts],
            escalation_ladder=[repaired(item) for item in contract.escalation_ladder],
        )
        return self.rebuild_scene_opportunities(fixed)

    @staticmethod
    def _chapter_scene_opportunities(
        *,
        session_number: int,
        location: str,
        chapter,
        clue_routes: list[SessionClueRoute],
        important_npcs: list[SessionNPCRole],
        opposition_goal: str,
    ) -> list[SessionSceneOpportunity]:
        """Translate an active chapter packet into the session's stable skeleton.

        The language model may enrich these situations later, but it must not
        replace them with a different scenario. Keeping the skeleton in the
        typed contract also makes chapter packages useful when no LLM is
        available during prep.
        """

        scenes = list(chapter.scenes)[:5]
        result: list[SessionSceneOpportunity] = []
        for index, scene in enumerate(scenes):
            role = SessionContractPlanner._chapter_scene_role(
                str(scene.scene_type or ""),
                index=index,
                count=len(scenes),
            )
            required = [str(item).strip() for item in scene.required_elements if str(item).strip()]
            optional = [str(item).strip() for item in scene.optional_elements if str(item).strip()]
            situation_parts = [str(scene.purpose or "").strip()]
            if required:
                situation_parts.append("必须出现：" + "、".join(required))
            situation = "；".join(item for item in situation_parts if item) or str(scene.title)
            possible_changes = [
                str(item).strip()
                for item in (scene.success_condition, scene.exit_condition)
                if str(item).strip()
            ]
            visible_elements, required_npc_names = (
                SessionPrepConcretizer._required_scene_cast(required, important_npcs)
            )
            scene_npc_names = required_npc_names or [
                item.name for item in important_npcs[:2]
            ]
            result.append(
                SessionSceneOpportunity(
                    scene_key=f"s{session_number:02d}-chapter-{index + 1}",
                    scene_role=role,
                    title=str(scene.title or f"章节场景{index + 1}").strip(),
                    location=str(scene.location or location).strip(),
                    situation=situation,
                    purpose=str(scene.purpose or "").strip(),
                    pressure=str(scene.when_to_use or opposition_goal).strip(),
                    entry_points=(optional or required)[:4],
                    possible_changes=possible_changes[:4],
                    clue_route_ids=[item.route_id for item in clue_routes[:3]],
                    npc_names=scene_npc_names[:3],
                    required_elements=visible_elements,
                    required_npc_names=required_npc_names,
                    optional=index not in {0, len(scenes) - 1},
                )
            )
        if result and not any(item.scene_role == "aftermath" for item in result):
            climax = next(
                (item for item in reversed(result) if item.scene_role == "climax_candidate"),
                result[-1],
            )
            result.append(
                SessionSceneOpportunity(
                    scene_key=f"s{session_number:02d}-chapter-aftermath",
                    scene_role="aftermath",
                    title=f"{climax.title}之后",
                    location=SessionContractPlanner._chapter_aftermath_location(
                        location,
                        climax.title,
                    ),
                    situation=(
                        f"【{climax.title}】的局部结果已经落地；同一关键人物、物件或地点因英雄的选择呈现新状态"
                    ),
                    purpose="兑现本场结果与代价，让角色短暂回应后收束，不再开启新的条件、敌人或调查目标。",
                    pressure="只在高潮已经产生明确结果后使用；不得用新谜团覆盖结局。",
                    entry_points=["处理伤者与资源", "确认人物态度", "回应刚才的选择", "决定离场方式"],
                    possible_changes=["标志画面发生可见变化", "关系或地点状态被记录", "一个长期后果被带入下一场"],
                    clue_route_ids=[],
                    npc_names=[item.name for item in important_npcs[:2]],
                    required_elements=climax.required_elements[:1],
                    optional=False,
                )
            )
        return result

    @staticmethod
    def _chapter_scene_role(scene_type: str, *, index: int, count: int) -> str:
        if index == 0:
            return "strong_start"
        clean = str(scene_type or "").strip().lower()
        if clean in {"aftermath", "epilogue", "interlude", "rest", "余波", "尾声"}:
            return "aftermath"
        if clean in {"climax", "boss", "finale", "conflict", "combat", "高潮", "首领"}:
            return "climax_candidate"
        if clean in {"social", "social_conflict", "dialogue", "negotiation", "社交", "交涉"}:
            return "social_or_investigation"
        if clean in {"investigation", "exploration", "ritual", "dungeon", "调查", "探索", "仪式"}:
            return "alternate_approach"
        if index == count - 1:
            return "climax_candidate"
        return "alternate_approach"

    @staticmethod
    def _chapter_aftermath_location(location: str, climax_title: str) -> str:
        base = str(location or "").strip()
        title = str(climax_title or "").strip()
        if any(token in title for token in ("旧路", "闸门", "门", "关口")):
            return f"{base}·旧路出口外" if base else "旧路出口外"
        if any(token in title for token in ("船", "港", "海", "码头")):
            return f"{base}·靠岸处" if base else "靠岸处"
        if any(token in title for token in ("遗迹", "迷宫", "塔", "矿井", "洞窟")):
            return f"{base}·出口" if base else "出口"
        return f"{base}·事后落脚处" if base else "事后落脚处"

    def _world_context(
        self,
        *,
        focus_title: str,
        focus_summary: str,
        location: str,
        opposition_goal: str,
        spotlight: str,
        chapter=None,
    ) -> dict[str, object]:
        profile = self.world_state.world_profile
        location_detail = (
            profile.major_locations.get(location)
            or profile.kingdoms.get(location)
            or ""
        )
        return {
            "world_name": profile.continent_name or profile.campaign_title,
            "tone": list(profile.tone_preferences[:5]),
            "themes": list((profile.core_themes or profile.playstyle_themes)[:5]),
            "magic_and_technology": profile.magic_tech_role,
            "location": location,
            "location_detail": location_detail,
            "allowed_locations": self._public_location_names(),
            "forbidden_backstage_locations": [
                item.location
                for item in self.story_arc_manager.state.locations
                if item.status == "backstage_candidate" and item.location
            ],
            "scene_location_rule": (
                f"本场所有场景都发生在【{location}】及其内部子区域；"
                "除非玩家在实际游玩中主动转场，不得把其他国家、城市或远方地点搬入本场。"
            ),
            "focus_thread": focus_title,
            "focus_summary": focus_summary,
            "opposition_goal": opposition_goal,
            "spotlight_hero": spotlight,
            "public_mysteries": list(profile.mysteries[:4]),
            "public_threats": list(profile.world_threats[:4]),
            "known_npcs": [
                {
                    "name": persona.name,
                    "identity": persona.public_identity,
                    "drive": persona.core_drive,
                    "active_goal": persona.active_goal or (persona.goals[0] if persona.goals else ""),
                    "location": persona.current_location,
                }
                for persona in list(self.world_state.npc_personas.values())[-10:]
            ],
            "heroes": [
                {
                    "name": draft.hero_name or draft.player_name,
                    "identity": draft.identity,
                    "theme": draft.theme,
                    "origin": draft.origin,
                    "equipment": list(draft.equipment),
                    "background_notes": list(draft.notes[-3:]),
                }
                for draft in profile.hero_drafts.values()
                if draft.hero_name or draft.player_name
            ],
            "first_act_setup": {
                "summary": str(profile.selected_first_act_summary or "").strip(),
                "starting_region": str(profile.starting_region or "").strip(),
                "questions": list(profile.first_act_questions),
                "answers": {
                    str(question): [str(item) for item in list(answers or [])]
                    for question, answers in dict(
                        profile.first_act_question_answers or {}
                    ).items()
                    if str(question).strip()
                },
                "skipped_questions": list(profile.first_act_skipped_questions),
                "opening_equipment_restrictions": [
                    dict(item)
                    for item in list(
                        profile.first_act_opening_equipment_restrictions or []
                    )
                    if isinstance(item, dict)
                ],
            },
            "active_chapter_package": self._chapter_packet(chapter),
        }

    @staticmethod
    def _chapter_packet(chapter) -> dict[str, object]:
        if chapter is None:
            return {}
        return {
            "chapter_title": str(chapter.chapter_title or ""),
            "synopsis": str(chapter.synopsis or ""),
            "intro_prompt": str(chapter.intro_prompt or ""),
            "conclusion_prompt": str(chapter.conclusion_prompt or ""),
            "timebox_minutes": int(chapter.timebox_minutes or 0),
            "iconic_elements": list(chapter.iconic_elements),
            "scenes": [
                {
                    "title": scene.title,
                    "scene_type": scene.scene_type,
                    "location": scene.location,
                    "purpose": scene.purpose,
                    "when_to_use": scene.when_to_use,
                    "required_elements": list(scene.required_elements),
                    "optional_elements": list(scene.optional_elements),
                    "success_condition": scene.success_condition,
                    "exit_condition": scene.exit_condition,
                }
                for scene in chapter.scenes
            ],
            "adversary_notes": list(chapter.adversary_notes),
            "reward_notes": list(chapter.reward_notes),
            "gm_notes": list(chapter.gm_notes),
        }

    @staticmethod
    def _chapter_location_anchor(chapter) -> str:
        """从章节包中取出不会丢失子场景的当前地点。

        同一地点的“·风铃廊/·登记小室/·旧路闸门”应共用父地点；
        若章节包本身横跨多个地点，则以强开场的精确地点为锚点。
        """

        if chapter is None:
            return ""
        locations = [
            str(scene.location or "").strip()
            for scene in list(getattr(chapter, "scenes", []) or [])
            if str(getattr(scene, "location", "") or "").strip()
        ]
        if not locations:
            return ""
        first = locations[0]
        parent = first.split("·", 1)[0].strip()
        if parent and all(
            location == parent or location.startswith(parent + "·")
            for location in locations
        ):
            return parent
        return first

    def _select_location(
        self,
        *,
        session_number: int,
        focus,
        recent_contracts: list[SessionDramaticContract],
        recent_locations: set[str],
    ):
        """Choose a public, continuity-safe place for the next session.

        Backstage inspiration locations are not part of the shared world until
        play reveals them.  The first session belongs at the Session 0 starting
        region.  Later sessions follow an explicitly related public location or
        the place most recently reached in play instead of rotating through the
        entire location catalogue.
        """

        state = self.story_arc_manager.state
        public = [
            item
            for item in state.locations
            if item.status != "destroyed"
            and (item.status == "public" or bool(item.last_seen))
        ]
        by_name = {item.location: item for item in public if item.location}
        starting_region = str(self.world_state.world_profile.starting_region or "").strip()
        if session_number <= 1 and starting_region:
            return by_name.get(starting_region) or next(
                (item for item in state.locations if item.location == starting_region),
                None,
            )
        if session_number <= 1:
            first_act_location = self._first_act_location_from_summary(public)
            if first_act_location is not None:
                return first_act_location

        focus_text = " ".join(
            str(value or "")
            for value in (
                getattr(focus, "title", ""),
                getattr(focus, "summary", ""),
                " ".join(getattr(focus, "entities", []) or []),
                " ".join(getattr(focus, "related_tags", []) or []),
                " ".join(getattr(focus, "public_clues", []) or []),
            )
        )
        linked = [item for item in public if item.location and item.location in focus_text]
        if linked:
            fresh = [item for item in linked if item.location not in recent_locations]
            return (fresh or linked)[0]

        processed = list(state.processed_session_ids)
        seen_order = {session_id: index for index, session_id in enumerate(processed)}
        visited = [item for item in public if item.last_seen]
        if visited:
            return max(
                visited,
                key=lambda item: (
                    seen_order.get(item.last_seen, -1),
                    state.locations.index(item),
                ),
            )

        if recent_contracts:
            previous = by_name.get(recent_contracts[-1].location)
            if previous is not None:
                return previous
        if starting_region:
            return by_name.get(starting_region)
        return public[0] if public else None

    def should_rebuild_first_session_contract(
        self,
        contract: SessionDramaticContract,
        *,
        session_number: int,
    ) -> bool:
        """识别与已确认第一幕明显错配的旧首场契约。

        这里只修复可由公开设定确定的错配：焦点必须对应当前第一幕，且当
        摘要明确点名公开地点时，契约地点也必须与之相符。没有第一幕摘要、
        后续场次或仍符合当前章节包的契约均保持原样，避免破坏旧存档连续性。
        """

        if session_number != 1:
            return False
        summary = str(
            self.world_state.world_profile.selected_first_act_summary or ""
        ).strip()
        if not summary:
            return False

        chapter = self.world_state.active_chapter()
        chapter_title = str(getattr(chapter, "chapter_title", "") or "").strip()
        if chapter_title and self._contract_mentions(
            contract,
            [chapter_title],
        ):
            return False

        active_threads = [
            item
            for item in self.story_arc_manager.state.threads
            if item.status not in {"resolved", "abandoned"}
        ]
        first_act = self._confirmed_first_act_thread(
            active_threads,
            session_number=session_number,
        )
        expected_focus = [summary]
        if first_act is not None:
            expected_focus.extend(
                [
                    str(getattr(first_act, "title", "") or ""),
                    str(getattr(first_act, "summary", "") or ""),
                ]
            )
        if not self._contract_mentions(contract, expected_focus):
            return True

        public = [
            item
            for item in self.story_arc_manager.state.locations
            if item.status != "destroyed"
            and (item.status == "public" or bool(item.last_seen))
        ]
        expected_location = self._first_act_location_from_summary(public)
        if expected_location is None:
            return False
        actual_key = self._match_key(contract.location)
        expected_key = self._match_key(expected_location.location)
        return not (
            actual_key
            and expected_key
            and (expected_key in actual_key or actual_key in expected_key)
        )

    def _confirmed_first_act_thread(self, threads, *, session_number: int):
        """返回与当前 Session 0 第一幕确认项一致的活动线程。"""

        if session_number != 1:
            return None
        summary_key = self._match_key(
            self.world_state.world_profile.selected_first_act_summary
        )
        if not summary_key:
            return None
        candidates = [
            item
            for item in threads
            if getattr(item, "thread_type", "") == "first_act"
            or getattr(item, "source", "") == "world.selected_first_act_summary"
        ]
        if not candidates:
            return None

        def score(item) -> tuple[int, int, int, int]:
            item_summary = self._match_key(getattr(item, "summary", ""))
            item_title = self._match_key(getattr(item, "title", ""))
            matches_summary = int(
                item_summary == summary_key
                or bool(item_summary and item_summary in summary_key)
                or bool(item_title and item_title in summary_key)
            )
            return (
                matches_summary,
                int(
                    getattr(item, "source", "")
                    == "world.selected_first_act_summary"
                ),
                int(getattr(item, "priority", 0) or 0),
                int(getattr(item, "progress", 0) or 0),
            )

        return max(candidates, key=score)

    def _first_act_location_from_summary(self, public):
        """从第一幕摘要中选择名称匹配最长的公开地点。"""

        summary_key = self._match_key(
            self.world_state.world_profile.selected_first_act_summary
        )
        if not summary_key:
            return None
        matches = [
            item
            for item in public
            if self._match_key(getattr(item, "location", ""))
            and self._match_key(getattr(item, "location", "")) in summary_key
        ]
        return max(
            matches,
            key=lambda item: len(self._match_key(getattr(item, "location", ""))),
            default=None,
        )

    @classmethod
    def _contract_mentions(
        cls,
        contract: SessionDramaticContract,
        expected_values: list[str],
    ) -> bool:
        actual_values = [
            contract.focus_thread,
            contract.title,
            contract.local_question_key,
            contract.dramatic_question,
        ]
        actual_keys = [cls._match_key(item) for item in actual_values]
        expected_keys = [cls._match_key(item) for item in expected_values]
        return any(
            expected
            and actual
            and (expected in actual or actual in expected)
            for expected in expected_keys
            for actual in actual_keys
        )

    @staticmethod
    def _match_key(value: object) -> str:
        return re.sub(r"[\W_]+", "", str(value or "").casefold())

    def _public_location_names(self) -> list[str]:
        profile = self.world_state.world_profile
        names = [
            profile.starting_region,
            *profile.major_locations.keys(),
            *profile.kingdoms.keys(),
            *(
                item.location
                for item in self.story_arc_manager.state.locations
                if item.status == "public" or item.last_seen
            ),
        ]
        return list(dict.fromkeys(str(item).strip() for item in names if str(item).strip()))

    def _register_session_npcs(self, contract: SessionDramaticContract) -> None:
        """Persist prepared NPCs so later dialogue uses the same motives."""

        for role in contract.important_npcs:
            if (
                self.character_manager is not None
                and self.character_manager.exists(role.name)
                and "pc" in self.character_manager.get(role.name).traits
            ):
                continue
            persona = self.world_state.ensure_npc_persona(
                role.name,
                public_identity=role.public_role or role.name,
                role_in_story=role.public_role,
                core_drive=role.goal_now,
                manner=role.voice_cue,
                speech_style=role.voice_cue,
                first_scene=contract.title,
                goals=[role.goal_now] if role.goal_now else [],
                secrets=[role.private_secret] if role.private_secret else [],
                custom_prompt=(
                    f"自身权限范围：{role.authority_scope}。"
                    f"本场具体要求：{role.concrete_demand}。"
                    f"接受标准：{role.acceptance_rule}。"
                    f"满足后只能兑现：{role.promised_result}。"
                    f"提出要求时必须公开的起步方向：{role.public_lead}。"
                    f"可行达成路径：{'；'.join(role.fulfillment_routes)}。"
                    f"若被拒绝或受阻：{role.refusal_move or role.if_blocked}。"
                    "玩家满足接受标准后必须立即兑现，不得继续含糊拖延。"
                ),
                current_stance=role.leverage,
                active_goal=role.goal_now,
            )
            role.persona_id = persona.npc_id

    def _important_npcs(
        self,
        *,
        location: str,
        focus,
        villain,
        opposition_goal: str,
        chapter=None,
    ) -> list[SessionNPCRole]:
        focus_entities = set(getattr(focus, "entities", []) or [])
        local: list[NPCPersona] = []
        for persona in self.world_state.npc_personas.values():
            if persona.status not in {"", "active"}:
                continue
            markers = {
                persona.name,
                persona.public_identity,
                *persona.aliases,
            }
            relevant = bool(focus_entities.intersection(markers))
            relevant = relevant or bool(
                location
                and (
                    location == persona.current_location
                    or location in persona.first_scene
                    or persona.first_scene in location
                )
            )
            relevant = relevant or bool(villain and villain.villain in markers)
            if relevant:
                local.append(persona)

        roles: list[SessionNPCRole] = []
        for persona in local[:3]:
            goal = persona.active_goal or next(iter(persona.goals), "在局势变化前保护自己的底线")
            roles.append(
                SessionNPCRole(
                    name=persona.name,
                    persona_id=persona.npc_id,
                    public_role=persona.public_identity or persona.role_in_story or "现场人物",
                    goal_now=goal,
                    leverage=(
                        f"掌握与【{getattr(focus, 'title', '') or location}】有关的关系、权限或情报"
                    ),
                    if_helped=f"更接近“{goal}”，并以符合其性格的方式回报英雄",
                    if_blocked=f"改用拖延、拒绝、交易或求援继续追求“{goal}”",
                )
            )

        for label in self._chapter_required_npc_labels(chapter):
            if any(
                SessionPrepConcretizer._npc_matches_required_element(label, item)
                for item in roles
            ):
                continue
            role_context = self._chapter_role_context(
                chapter,
                location=location,
                opposition_goal=opposition_goal,
            )
            profile = local_role_profile(label, context=role_context)
            roles.append(
                SessionNPCRole(
                    name=label,
                    public_role=label,
                    goal_now=profile.get(
                        "goal_now",
                        "在当前局势中保护自己负有责任的人、地点或职责",
                    ),
                    leverage=profile.get("leverage", ""),
                    authority_scope=profile.get(
                        "authority_scope",
                        DEFAULT_AUTHORITY_SCOPE,
                    ),
                    refusal_move=profile.get("refusal_move", ""),
                    voice_cue=profile.get("voice_cue", ""),
                    if_helped=(
                        "在自身权限范围内立即兑现已经答应的帮助"
                        if profile
                        else "在自身权限范围内提供明确帮助"
                    ),
                    if_blocked=profile.get(
                        "refusal_move",
                        "说明不能配合的具体理由，并采取符合职责的行动",
                    ),
                )
            )

        named_actor = self._named_actor_from_goal(opposition_goal)
        if named_actor:
            opposition_role = SessionNPCRole(
                name=named_actor,
                public_role=next(
                    (term for term in self._NPC_ROLE_TERMS if term in named_actor),
                    "对立方现场人物",
                ),
                goal_now=opposition_goal,
                leverage="能调动与当前计划直接相关的人手、权限或资源",
                authority_scope=(
                    "可以指挥自己的部下并处理职责范围内的交涉；"
                    "不能替英雄决定行动，也不能承诺其他势力必然服从"
                ),
                if_helped="把英雄的让步转化为计划进展，而不是无缘无故收手",
                if_blocked="留下可见后果并改变手段，不重复同一种阻挠",
            )
            existing_index = next(
                (index for index, item in enumerate(roles) if item.name == named_actor),
                -1,
            )
            if existing_index >= 0:
                roles[existing_index] = opposition_role
            else:
                roles.append(opposition_role)

        villain_name = str(getattr(villain, "villain", "") or "").strip()
        if (
            self._looks_like_person(villain_name)
            and all(item.name != villain_name for item in roles)
        ):
            roles.append(
                SessionNPCRole(
                    name=villain_name,
                    public_role="对立方或其现场代理人",
                    goal_now=opposition_goal,
                    leverage="能调动资源、制度、部下或倒计时压力",
                    if_helped="把英雄的让步转化为计划进展，而不是无缘无故收手",
                    if_blocked="留下可见后果并改变手段，不重复同一种阻挠",
                )
            )
        return roles[:4]

    @staticmethod
    def _chapter_role_context(
        chapter,
        *,
        location: str,
        opposition_goal: str,
    ) -> str:
        if chapter is None:
            return "；".join(item for item in (location, opposition_goal) if item)
        parts = [
            location,
            opposition_goal,
            str(getattr(chapter, "synopsis", "") or ""),
            str(getattr(chapter, "intro_prompt", "") or ""),
            str(getattr(chapter, "conclusion_prompt", "") or ""),
        ]
        for scene in list(getattr(chapter, "scenes", []) or []):
            parts.extend(
                str(getattr(scene, field, "") or "")
                for field in (
                    "title",
                    "location",
                    "purpose",
                    "success_condition",
                    "exit_condition",
                )
            )
        return "；".join(" ".join(item.split()) for item in parts if item.strip())

    @classmethod
    def _chapter_required_npc_labels(cls, chapter) -> list[str]:
        if chapter is None:
            return []
        labels: list[str] = []
        for scene in list(getattr(chapter, "scenes", []) or []):
            values = [
                *list(getattr(scene, "required_npc_names", []) or []),
                *list(getattr(scene, "required_elements", []) or []),
            ]
            for raw in values:
                label = " ".join(str(raw or "").split()).strip()
                if not label or label in labels:
                    continue
                if any(term in label for term in cls._NPC_ROLE_TERMS):
                    labels.append(label)
        return labels[:4]

    @staticmethod
    def _clue_routes(
        *,
        session_number: int,
        location: str,
        conclusion: str,
        signature_image: str,
        important_npcs: list[SessionNPCRole],
    ) -> list[SessionClueRoute]:
        prefix = f"s{session_number:02d}"
        witness = important_npcs[0].name if important_npcs else "最先承受此事后果的现场人物"
        return [
            SessionClueRoute(
                route_id=f"{prefix}-physical",
                conclusion=conclusion,
                approach="观察、调查或利用环境",
                source=signature_image,
                visible_lead=f"【{location}】里有一处物理痕迹与表面解释不一致",
                success_reveal=conclusion,
                fallback="失败时仍显出异常存在，但要换角度、付代价或稍后从另一条路径确认",
            ),
            SessionClueRoute(
                route_id=f"{prefix}-witness",
                conclusion=conclusion,
                approach="交涉、安抚、施压或建立信任",
                source=witness,
                visible_lead=f"【{witness}】的反应表明其知道的比公开说法更多",
                success_reveal=conclusion,
                fallback="对方拒绝时给出理由、条件或无意间暴露的矛盾，不能只说无可奉告",
            ),
            SessionClueRoute(
                route_id=f"{prefix}-record",
                conclusion=conclusion,
                approach="查阅记录、仪式、魔法或制度流程",
                source=f"【{location}】的账册、仪式回声、设备记录或公共规程",
                visible_lead="制度留下的记录与某个人的说法无法同时成立",
                success_reveal=conclusion,
                fallback="关键记录可以残缺或受保护，但必须指向另一名见证者或可验证地点",
            ),
        ]

    def _fantastic_details(
        self,
        *,
        location: str,
        location_state,
        signature_image: str,
        opening_disruption: str,
    ) -> list[str]:
        profile = self.world_state.world_profile
        location_detail = (
            profile.major_locations.get(location)
            or profile.kingdoms.get(location)
            or (getattr(location_state, "next_prompt", "") if location_state else "")
            or "这个地点的形态、习俗或用途"
        )
        magic_tech = profile.magic_tech_role or "本世界的魔法、科技与灵魂能量"
        return [
            f"地点质感：{location_detail}",
            f"魔法与科技在现场的表现依据：{magic_tech}",
            f"标志画面：{signature_image}",
            f"开场时已经发生的变化：{opening_disruption}",
        ]

    @staticmethod
    def _potential_scenes(
        *,
        session_number: int,
        location: str,
        focus_title: str,
        focus_summary: str,
        opening_disruption: str,
        opposition_goal: str,
        villain_move: str,
        signature_image: str,
        clue_routes: list[SessionClueRoute],
        important_npcs: list[SessionNPCRole],
    ) -> list[SessionSceneOpportunity]:
        prefix = f"s{session_number:02d}"
        npc = important_npcs[0] if important_npcs else None
        npc_name = npc.name if npc else "现场关键人物"
        route_ids = [item.route_id for item in clue_routes]
        while len(route_ids) < 3:
            route_ids.append(f"{prefix}-route-{len(route_ids) + 1}")
        scene_locations = SessionContractPlanner._fallback_scene_locations(location)
        return [
            SessionSceneOpportunity(
                scene_key=f"{prefix}-strong-start",
                scene_role="strong_start",
                title=f"{location}被打断的当下",
                location=scene_locations[0],
                situation=opening_disruption,
                purpose="先让局面发生变化，再把一个具体、紧迫但开放的决定交给英雄",
                pressure=opposition_goal,
                entry_points=["立刻保护某人或某物", "追查扰动来源", "先与现场人物交涉"],
                possible_changes=["谁控制现场", "谁愿意相信英雄", "哪条退路或资源仍可使用"],
                clue_route_ids=route_ids[:2],
                npc_names=[npc_name] if npc else [],
                optional=False,
            ),
            SessionSceneOpportunity(
                scene_key=f"{prefix}-human-cost",
                scene_role="social_or_investigation",
                title=f"{npc_name}守住的条件",
                location=scene_locations[1],
                situation=(
                    f"{npc_name}正在追求“{npc.goal_now}”，不会只等英雄提问"
                    if npc
                    else f"有人因【{focus_title}】承受具体代价，并掌握部分真相"
                ),
                purpose="让人物的目标、恐惧和筹码成为可互动局面，而非情报售货机",
                pressure=focus_summary,
                entry_points=["取得信任", "提出交换", "观察其反应", "绕过其权限"],
                possible_changes=["NPC态度永久改变", "得到帮助但欠下人情", "对方主动采取下一步"],
                clue_route_ids=route_ids[1:],
                npc_names=[npc_name] if npc else [],
            ),
            SessionSceneOpportunity(
                scene_key=f"{prefix}-alternate-route",
                scene_role="alternate_approach",
                title=f"{focus_title}留下的另一条路",
                location=scene_locations[2],
                situation=f"物证、制度记录或魔法回声让英雄能从人物交涉之外处理【{focus_title}】",
                purpose="保证失败或拒绝不会卡死剧情，也给不同职业和角色概念发挥空间",
                pressure="使用这条路会花时间、资源，或暴露英雄正在调查什么",
                entry_points=["调查具体痕迹", "启动仪式", "使用职业技能", "推进目标命刻"],
                possible_changes=["证伪一种解释", "打开新路线", "把隐藏压力变成可对抗目标"],
                clue_route_ids=[route_ids[0], route_ids[2]],
            ),
            SessionSceneOpportunity(
                scene_key=f"{prefix}-decision-point",
                scene_role="climax_candidate",
                title=f"{focus_title}的决断点",
                location=scene_locations[3],
                situation=f"{villain_move}，迫使所有相关方在同一现场表态或行动",
                purpose="让本场核心问题获得答案或不可逆改变；形式由玩家实际方法决定",
                pressure=opposition_goal,
                entry_points=["对决", "追逐", "仪式", "谈判", "牺牲资源换取结果"],
                possible_changes=["解决局部问题", "接受明确代价", "赢得结果但改变阵营关系"],
                clue_route_ids=route_ids,
                npc_names=[item.name for item in important_npcs[:3]],
            ),
            SessionSceneOpportunity(
                scene_key=f"{prefix}-aftermath",
                scene_role="aftermath",
                title=f"{signature_image}之后",
                location=scene_locations[4],
                situation="同一标志物、地点或人物因英雄选择呈现出新的状态",
                purpose="兑现局部结果、记录代价，并让角色有短暂反应时间后再给片尾钩子",
                pressure="不得用一个全新谜团盖过刚刚取得的结果",
                entry_points=["处理伤者与资源", "确认NPC态度", "记录地点变化", "角色之间回应选择"],
                possible_changes=["标志画面发生可见变化", "一项线索获得用途", "一个长期后果被写入战役"],
                npc_names=[item.name for item in important_npcs[:2]],
            ),
        ]

    @staticmethod
    def _fallback_scene_locations(location: str) -> list[str]:
        """Give one large location several playable camera positions."""

        root = str(location or "当前地点").strip().split("·", 1)[0] or "当前地点"
        if "驿站" in root:
            areas = ["风铃廊", "候车厅", "旧钟仓", "旧路闸门", "背风庭院"]
        elif any(token in root for token in ("港", "海岸", "群岛", "海湾")):
            areas = ["靠岸栈桥", "潮湿货棚", "瞭望台", "外港航道", "退潮石滩"]
        elif any(token in root for token in ("森林", "林地", "古林")):
            areas = ["林缘旧径", "树冠阴影", "倒木空地", "封闭祭坛", "回声溪谷"]
        elif any(token in root for token in ("塔", "遗迹", "神殿", "宫殿")):
            areas = ["断裂门厅", "回廊", "记录室", "核心阶梯", "高台余迹"]
        elif any(token in root for token in ("矿", "采掘", "熔炉", "工厂")):
            areas = ["升降台", "工人休息层", "废弃矿道", "主熔炉", "排风高架"]
        else:
            areas = ["入口", "会面处", "侧路", "冲突中心", "事后落脚处"]
        return [f"{root}·{area}" for area in areas]

    def rebuild_scene_opportunities(
        self,
        contract: SessionDramaticContract,
    ) -> SessionDramaticContract:
        """Regenerate scene support after a contract identity is edited."""

        villain_move = next(
            (
                str(item).strip()
                for item in reversed(contract.escalation_ladder)
                if str(item).strip()
            ),
            contract.opposition_goal or "对立方采取一次符合自身目标的行动",
        )
        return replace(
            contract,
            potential_scenes=self._potential_scenes(
                session_number=contract.session_number,
                location=contract.location,
                focus_title=contract.focus_thread or contract.title,
                focus_summary=contract.dramatic_question,
                opening_disruption=contract.opening_disruption,
                opposition_goal=contract.opposition_goal,
                villain_move=villain_move,
                signature_image=contract.signature_image,
                clue_routes=list(contract.clue_routes),
                important_npcs=list(contract.important_npcs),
            ),
        )

    def continue_from(
        self,
        *,
        previous: SessionDramaticContract,
        previous_progress: SessionEpisodeProgress,
        session_number: int,
    ) -> SessionDramaticContract:
        """Carry an unfinished local situation into the next table session.

        Only public consequences are carried forward.  Unrevealed explanations
        remain flexible, so continuation never turns the situation brief into
        a predetermined plot.
        """

        consequence = str(
            getattr(previous_progress, "memory_consequence", "")
            or getattr(previous_progress, "last_event", "")
            or previous.opening_disruption
        ).strip()
        continued = replace(
            previous,
            session_number=session_number,
            title=f"第{session_number:02d}场·{previous.focus_thread or previous.title}（续）",
            opening_disruption=(
                f"上场已发生的后果继续改变现场：{consequence}"
                if consequence
                else "上场未收束的局面在新的当下压力中继续。"
            ),
            signature_image=(
                f"{previous.signature_image}这个画面必须因上场选择的后果发生可见变化。"
            ),
            closure_requirement=(
                f"本场必须对续接的核心问题「{previous.dramatic_question}」给出局部结果；"
                "不能再以只获得一条线索收团。"
            ),
            memory_anchor=(
                f"第{session_number:02d}场需要让上场的标志画面发生变化，并留下新的玩家选择与后果。"
            ),
            inherited_consequence=consequence,
            status="planned",
        )
        # A continuation preserves the unresolved dramatic question, not the
        # exact camera plan.  Reusing the previous opportunities would make a
        # new table session reopen the same gatekeeper conversation under the
        # same scene keys.  Rebuild movable situations around the changed
        # opening state so the continuation has a new start, decision point
        # and aftermath while all public facts remain intact.
        return self.rebuild_scene_opportunities(continued)

    @staticmethod
    def _signature_image(
        *,
        location: str,
        location_state,
        focus,
        inherited_consequence: str,
        recent_contracts: list[SessionDramaticContract],
        location_detail: str = "",
        iconic_elements: list[str] | None = None,
    ) -> str:
        sources: list[str] = [
            str(item).strip()
            for item in list(iconic_elements or [])
            if str(item).strip()
        ]
        if location_state is not None:
            sources.extend(reversed(list(location_state.changes[-2:])))
            if location_state.next_prompt:
                sources.append(location_state.next_prompt)
        if focus is not None:
            sources.extend(reversed(list(focus.public_clues[-2:])))
        if inherited_consequence:
            sources.append(inherited_consequence)
        if location_detail:
            sources.append(location_detail)
        anchor = SessionContractPlanner._signature_anchor(sources, location=location)
        recent_images = {item.signature_image for item in recent_contracts if item.signature_image}
        image = SessionContractPlanner._render_signature_image(location, anchor)
        if image in recent_images:
            image = SessionContractPlanner._render_signature_image(
                location,
                SessionContractPlanner._fallback_signature_anchor(location, alternate=True),
            )
        return image

    @staticmethod
    def _signature_anchor(sources: list[str], *, location: str) -> str:
        known = (
            "迟响一拍的白花风铃",
            "白花风铃",
            "碎月遗物",
            "旧路铜钥匙",
            "染血铜钥匙",
            "铜钥匙",
            "旧路闸门",
            "风铃",
            "旧钟",
            "大钟",
            "钟碑",
            "路灯",
            "晶炉",
            "账册",
            "石碑",
            "遗物",
        )
        joined = "；".join(str(item or "") for item in sources)
        for candidate in known:
            if candidate in joined:
                return candidate
        return SessionContractPlanner._fallback_signature_anchor(location)

    @staticmethod
    def _fallback_signature_anchor(location: str, *, alternate: bool = False) -> str:
        text = str(location or "")
        if "驿站" in text:
            return "门楣下的铜制驿牌" if alternate else "门廊下的铜制驿铃"
        if any(token in text for token in ("港", "码头", "海岸")):
            return "潮痕斑驳的引航灯" if alternate else "系船桩上的潮位铜环"
        if any(token in text for token in ("森林", "林", "树海")):
            return "林缘悬着的旧路灯" if alternate else "入口处刻有旧记号的树"
        if any(token in text for token in ("塔", "尖塔")):
            return "塔窗内的星轨镜" if alternate else "塔心悬着的校准摆锤"
        if any(token in text for token in ("城", "公国", "王国", "帝国")):
            return "广场边缘的灵魂路灯" if alternate else "城门内的公告钟"
        if any(token in text for token in ("遗迹", "迷宫", "矿井", "洞窟")):
            return "石门上的裂纹浮雕" if alternate else "入口墙上的导路石"
        return "入口处一盏带裂纹的灵魂灯" if not alternate else "门边一面失去光泽的铜镜"

    @staticmethod
    def _render_signature_image(location: str, anchor: str) -> str:
        prefix = f"【{location}】" if location else "现场"
        if "风铃" in anchor or "驿铃" in anchor:
            return f"{prefix}门廊下，{anchor}无风自响；表面凝着潮盐，铃舌每次都比四周慢半拍。"
        if "钟" in anchor or "摆锤" in anchor:
            return f"{prefix}中央，{anchor}停在将响未响的位置；铜面冰凉，低鸣从地板下传来。"
        if "钥匙" in anchor:
            return f"{prefix}柜台上放着{anchor}；齿槽沾有灰亮粉末，碰动时发出两声不同的金属回响。"
        if "闸门" in anchor or "石门" in anchor:
            return f"{prefix}深处，{anchor}只开启一掌宽；冷风从缝里吹出，门轴上的旧油泛着铁腥味。"
        if "灯" in anchor:
            return f"{prefix}入口旁，{anchor}忽明忽暗；灯罩内有细小光屑逆着火焰落下。"
        if "树" in anchor:
            return f"{prefix}边缘，{anchor}正落下没有声音的叶片；树皮上的刻痕摸起来仍带温度。"
        return f"{prefix}入口处，{anchor}留在所有人都能看见的位置；表面冰凉，边缘正随远处震动轻颤。"

    @staticmethod
    def _climax_type(
        phase: StoryArcPhase,
        suggested_scene_type: str,
        thread_type: str,
    ) -> str:
        if phase in {StoryArcPhase.CRISIS, StoryArcPhase.FINALE}:
            return "高压冲突、首领机制、仪式或改变世界的集体选择"
        text = f"{suggested_scene_type} {thread_type}".lower()
        if "dungeon" in text or "探索" in text:
            return "险境探索后的发现、逃离或机关抉择"
        if "social" in text or "faction" in text or "关系" in text:
            return "公开交涉、关系决裂或派系选择"
        if "travel" in text or "旅行" in text:
            return "旅途险境、路线选择或抵达时的代价"
        return "由玩家方法决定的对决、仪式、追逐、谈判或艰难取舍"
