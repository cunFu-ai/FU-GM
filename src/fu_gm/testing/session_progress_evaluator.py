from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from fu_gm.llm_client import ChatMessage
from fu_gm.models import SessionDramaticContract, SessionEpisodeProgress


_STAGES = ("opening", "development", "reversal", "climax", "closure")


@dataclass
class SessionProgressAssessment:
    """Semantic audit of what the table has actually played so far."""

    stage: str = "opening"
    scene_change_recommended: bool = False
    local_question_changed: bool = False
    local_question_resolved: bool = False
    deliberate_cliffhanger: bool = False
    reversal_reached: bool = False
    concrete_consequence: bool = False
    npc_answer_complete: bool = True
    npc_personality_consistent: bool = True
    player_agency_preserved: bool = True
    continuity_ok: bool = True
    cause_effect_linked: bool = True
    gm_control_present: bool = True
    session_identity_distinct: bool = True
    gm_response_relevant: bool = True
    opposition_move_present: bool = False
    signature_image_evolved: bool = False
    opening_signature_present: bool = False
    concrete_npc_agenda_present: bool = False
    local_payoff_present: bool = False
    scene_topology_ok: bool = False
    distinct_functional_scene_count: int = 0
    distinct_location_count: int = 0
    distinct_camera_count: int = 0
    previous_consequence_recalled: bool = True
    repeated_loop_detected: bool = False
    memory_image: str = ""
    memory_choice: str = ""
    memory_consequence: str = ""
    unresolved_now: str = ""
    next_gm_need: str = ""
    evidence: list[str] = field(default_factory=list)
    used_fallback: bool = False
    model_error: str = ""

    @property
    def memory_anchor_complete(self) -> bool:
        return bool(self.memory_image and self.memory_choice and self.memory_consequence)


class SessionProgressEvaluator:
    """Judges session progress from public play, never from intended outcomes."""

    def __init__(self, *, client: Any | None, model: str) -> None:
        self.client = client
        self.model = str(model or "").strip()

    def evaluate(
        self,
        *,
        transcript: str,
        contract: SessionDramaticContract,
        meaningful_turns: int,
        scene_count: int,
        scene_roles: list[str] | None = None,
        scene_locations: list[str] | None = None,
        scene_names: list[str] | None = None,
        previous_memory_anchors: list[dict[str, str]] | None = None,
        authoritative_progress: SessionEpisodeProgress | None = None,
    ) -> SessionProgressAssessment:
        if self.client is None or not self.model:
            return self._fallback(
                transcript=transcript,
                meaningful_turns=meaningful_turns,
                scene_count=scene_count,
                error="semantic evaluator unavailable",
                authoritative_progress=authoritative_progress,
            )
        try:
            compact_transcript = self._compact_transcript(transcript)
            raw = self.client.create_chat_completion(
                model=self.model,
                messages=[
                    ChatMessage(role="system", content=self._system_prompt()),
                    ChatMessage(
                        role="user",
                        content=(
                            "后台戏剧契约（只用于核对目标，不代表已经发生）：\n"
                            f"{json.dumps(self._contract_payload(contract), ensure_ascii=False)}\n\n"
                            f"有意义玩家行动数：{meaningful_turns}\n"
                            f"当前场景段落数：{scene_count}\n\n"
                            f"已实际进入的功能场景：{json.dumps(scene_roles or [], ensure_ascii=False)}\n"
                            f"已实际进入的地点/子地点：{json.dumps(scene_locations or [], ensure_ascii=False)}\n\n"
                            f"已实际进入的镜头名称：{json.dumps(scene_names or [], ensure_ascii=False)}\n\n"
                            "最近场次的实际记忆锚点（用于检查承接与重复，不代表本场已经发生）：\n"
                            f"{json.dumps(previous_memory_anchors or [], ensure_ascii=False)}\n\n"
                            "本场玩家实际看见的对话：\n"
                            f"{compact_transcript}"
                        ),
                    ),
                ],
                temperature=0,
                response_format={"type": "json_object"},
                operation="session_progress.audit",
            )
            payload = self._parse_json(raw)
            return self._merge_authoritative(
                self._normalize_assessment(self._from_payload(payload)),
                authoritative_progress,
            )
        except Exception as exc:
            return self._fallback(
                transcript=transcript,
                meaningful_turns=meaningful_turns,
                scene_count=scene_count,
                error=str(exc),
                authoritative_progress=authoritative_progress,
            )

    @staticmethod
    def _contract_payload(contract: SessionDramaticContract) -> dict[str, Any]:
        """Keep the audit brief focused on claims the transcript can verify."""

        return {
            "title": contract.title,
            "location": contract.location,
            "dramatic_question": contract.dramatic_question,
            "opening_disruption": contract.opening_disruption,
            "signature_image": contract.signature_image,
            "opposition_goal": contract.opposition_goal,
            "dilemma": contract.dilemma,
            "reversal": contract.reversal,
            "closure_requirement": contract.closure_requirement,
            "possible_payoffs": list(contract.possible_payoffs[:3]),
            "irreversible_change": contract.irreversible_change,
            "memory_anchor": contract.memory_anchor,
        }

    @staticmethod
    def _compact_transcript(transcript: str, *, max_chars: int = 20000) -> str:
        """Retain the opening and latest consequences without a 36k replay."""

        clean = str(transcript or "")
        if len(clean) <= max_chars:
            return clean
        opening_chars = min(5000, max_chars // 3)
        ending_chars = max_chars - opening_chars
        return (
            clean[:opening_chars]
            + "\n\n[中段较早、且已被后续公开结果覆盖的对话已省略]\n\n"
            + clean[-ending_chars:]
        )

    @staticmethod
    def merge_cumulative(
        previous: SessionProgressAssessment | None,
        current: SessionProgressAssessment,
    ) -> SessionProgressAssessment:
        """Preserve public session facts while keeping momentary judgments fresh.

        The evaluator is sampled repeatedly during one session. A later sample
        may focus on the newest exchange and call the overall stage
        ``development`` even after an earlier sample cited a public reversal.
        Reversals and paid-off consequences cannot become un-happened. In
        contrast, loop detection, the need for a scene cut, and whether the
        latest NPC answer is complete are intentionally not sticky.
        """

        if previous is None:
            return current

        stage_rank = {stage: index for index, stage in enumerate(_STAGES)}
        if stage_rank.get(previous.stage, 0) > stage_rank.get(current.stage, 0):
            current.stage = previous.stage

        cumulative_true_fields = (
            "local_question_changed",
            "deliberate_cliffhanger",
            "reversal_reached",
            "concrete_consequence",
            "opposition_move_present",
            "signature_image_evolved",
            "opening_signature_present",
            "concrete_npc_agenda_present",
            "local_payoff_present",
            "previous_consequence_recalled",
        )
        for field_name in cumulative_true_fields:
            setattr(
                current,
                field_name,
                bool(getattr(previous, field_name) or getattr(current, field_name)),
            )

        current.distinct_functional_scene_count = max(
            previous.distinct_functional_scene_count,
            current.distinct_functional_scene_count,
        )
        current.distinct_location_count = max(
            previous.distinct_location_count,
            current.distinct_location_count,
        )
        current.distinct_camera_count = max(
            previous.distinct_camera_count,
            current.distinct_camera_count,
        )
        current.scene_topology_ok = bool(
            previous.scene_topology_ok or current.scene_topology_ok
        )
        for field_name in ("memory_image", "memory_choice", "memory_consequence"):
            if not str(getattr(current, field_name) or "").strip():
                setattr(current, field_name, getattr(previous, field_name))
        return SessionProgressEvaluator._normalize_assessment(current)

    @staticmethod
    def _system_prompt() -> str:
        return (
            "你是严格的真人TRPG实录审计员。只依据玩家实际看见的对话判断，不得把后台契约、标题或计划当作已发生。"
            "finding one clue 不等于收束；固定轮数不等于一场跑团结束；只有现场局面被玩家选择真正改变，或在明确转折后形成有意悬念，才可接近收束。"
            "reversal_reached 只在某项公开事实改变了玩家对本场核心问题、人物立场或代价的理解时为true；"
            "普通物证、更多细节、核验步骤或只指向下一条线索都不是转折。"
            "scene_change_recommended 仅在地点、时间、参与者、目标或局面已经发生实质变化，继续留在原镜头会重复时为 true。"
            "若 repeated_loop_detected=true，且对立方已经行动、已有具体后果或本场问题已经变化，继续停留只会复读，"
            "scene_change_recommended 应为 true；这表示应换镜头，不表示本场已经结束。"
            "若玩家等待NPC答复而GM没有给出实际答案，npc_answer_complete=false。"
            "若NPC无缘无故改变目标、语气、已公开立场或掌握不可能知道的信息，npc_personality_consistent=false。"
            "若GM替玩家决定行动、情绪、立场或结果，player_agency_preserved=false。"
            "若玩家行动后GM只复述意图，没有展示对象反应、环境发现、规则结果或局面变化，cause_effect_linked=false。"
            "若整场始终由玩家凭空提出地点、人物和事件，而GM没有用NPC目标、环境变化、对手行动或后果主持局面，gm_control_present=false。"
            "若本场缺少区别于其他普通场次的具体画面、抉择或结果，session_identity_distinct=false。"
            "opposition_move_present 只有NPC、反派、环境或倒计时根据自身目标主动改变过局面时才为true。"
            "opening_signature_present 只有后台契约的标志画面或明确同一物件在本场开局前段实际出现时才为true；"
            "只存在于后台契约、标题或后半场首次出现都不算。"
            "concrete_npc_agenda_present 只有至少一名NPC在公开对话中提出过具体条件、拒绝、目标或主动行动时才为true；"
            "泛称现场人物、只回答问句或单纯站在背景里不算。"
            "signature_image_evolved 只有同一标志性景物至少出现两次，且后一次因玩家选择、反转或后果发生可见变化时才为true；只重复描写不算。"
            "local_payoff_present 只有本场至少兑现一项局部成果、损失、关系变化、地点变化或明确证伪时才为true；只得到下一条线索不算。"
            "scene_topology_ok 只有实际游玩至少进入三种功能不同的场景，并且不是只给同一段对话换标题时才为true；"
            "distinct_functional_scene_count、distinct_location_count与distinct_camera_count必须根据提供的实际场景列表去重计数，不能根据后台候选场景计算。"
            "同一大型地点内若镜头名称、子区域和功能都实质不同，可以算不同camera；仅换标题但局面不变不算。"
            "previous_consequence_recalled 仅在本场开局或早期局面可见地承接最近一场的选择后果时为true；"
            "若没有上一场记忆锚点则为true。只复述摘要、报出同一地名或开启无关新任务不算承接。"
            "判断session_identity_distinct时必须与提供的最近记忆锚点比较；仅替换人名地名但画面、选择和结果结构相同，应为false。"
            "repeated_loop_detected 在玩家重复同一问题、GM重复同一答复或场景连续三轮没有新事实/决定/后果时为true。"
            "若GM回应与玩家消息无关、回答了没问的问题或在玩家自由讨论时插入无必要气氛复述，gm_response_relevant=false。"
            "记忆锚点必须分别是已出现的具体感官画面、玩家作出的选择、该选择产生的可追踪后果；没有就留空。"
            "stage只能是 opening/development/reversal/climax/closure。"
            "只输出JSON对象，字段为：stage, scene_change_recommended, local_question_changed, local_question_resolved, "
            "deliberate_cliffhanger, reversal_reached, concrete_consequence, npc_answer_complete, npc_personality_consistent, "
            "player_agency_preserved, continuity_ok, cause_effect_linked, gm_control_present, session_identity_distinct, "
            "gm_response_relevant, opposition_move_present, opening_signature_present, concrete_npc_agenda_present, "
            "signature_image_evolved, local_payoff_present, scene_topology_ok, distinct_functional_scene_count, "
            "distinct_location_count, distinct_camera_count, previous_consequence_recalled, repeated_loop_detected, "
            "memory_image, memory_choice, memory_consequence, unresolved_now, next_gm_need, evidence。"
            "evidence最多3条，每条必须引用或准确概括实际对话中的可验证事件。"
        )

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        text = str(raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("session progress response is not an object")
        return value

    @staticmethod
    def _from_payload(payload: dict[str, Any]) -> SessionProgressAssessment:
        stage = str(payload.get("stage") or "opening").strip().lower()
        if stage not in _STAGES:
            stage = "opening"
        return SessionProgressAssessment(
            stage=stage,
            scene_change_recommended=bool(payload.get("scene_change_recommended")),
            local_question_changed=bool(payload.get("local_question_changed")),
            local_question_resolved=bool(payload.get("local_question_resolved")),
            deliberate_cliffhanger=bool(payload.get("deliberate_cliffhanger")),
            reversal_reached=bool(payload.get("reversal_reached")),
            concrete_consequence=bool(payload.get("concrete_consequence")),
            npc_answer_complete=bool(payload.get("npc_answer_complete", True)),
            npc_personality_consistent=bool(payload.get("npc_personality_consistent", False)),
            player_agency_preserved=bool(payload.get("player_agency_preserved", True)),
            continuity_ok=bool(payload.get("continuity_ok", True)),
            cause_effect_linked=bool(payload.get("cause_effect_linked", False)),
            gm_control_present=bool(payload.get("gm_control_present", False)),
            session_identity_distinct=bool(payload.get("session_identity_distinct", False)),
            gm_response_relevant=bool(payload.get("gm_response_relevant", False)),
            opposition_move_present=bool(payload.get("opposition_move_present", False)),
            opening_signature_present=bool(payload.get("opening_signature_present", False)),
            concrete_npc_agenda_present=bool(
                payload.get("concrete_npc_agenda_present", False)
            ),
            signature_image_evolved=bool(payload.get("signature_image_evolved", False)),
            local_payoff_present=bool(payload.get("local_payoff_present", False)),
            scene_topology_ok=bool(payload.get("scene_topology_ok", False)),
            distinct_functional_scene_count=max(
                0, int(payload.get("distinct_functional_scene_count", 0) or 0)
            ),
            distinct_location_count=max(
                0, int(payload.get("distinct_location_count", 0) or 0)
            ),
            distinct_camera_count=max(
                0, int(payload.get("distinct_camera_count", 0) or 0)
            ),
            previous_consequence_recalled=bool(
                payload.get("previous_consequence_recalled", True)
            ),
            repeated_loop_detected=bool(payload.get("repeated_loop_detected", False)),
            memory_image=str(payload.get("memory_image") or "").strip(),
            memory_choice=str(payload.get("memory_choice") or "").strip(),
            memory_consequence=str(payload.get("memory_consequence") or "").strip(),
            unresolved_now=str(payload.get("unresolved_now") or "").strip(),
            next_gm_need=str(payload.get("next_gm_need") or "").strip(),
            evidence=[str(item).strip() for item in payload.get("evidence", []) if str(item).strip()][:3],
        )

    @staticmethod
    def _normalize_assessment(
        assessment: SessionProgressAssessment,
    ) -> SessionProgressAssessment:
        """Repair internally inconsistent semantic labels conservatively."""

        stage_rank = {stage: index for index, stage in enumerate(_STAGES)}
        if assessment.reversal_reached and stage_rank.get(assessment.stage, 0) < stage_rank["reversal"]:
            assessment.stage = "reversal"
        if assessment.repeated_loop_detected and (
            assessment.opposition_move_present
            or assessment.concrete_consequence
            or assessment.local_question_changed
            or assessment.local_question_resolved
        ):
            assessment.scene_change_recommended = True
        if assessment.local_question_resolved:
            assessment.local_question_changed = True
            assessment.local_payoff_present = True
            if stage_rank.get(assessment.stage, 0) < stage_rank["climax"]:
                # The aftermath still needs its own scene, so a resolved core
                # question earns the climax boundary rather than certifying
                # closure outright.
                assessment.stage = "climax"
        if assessment.deliberate_cliffhanger and not assessment.reversal_reached:
            # An arbitrary interruption is not an earned cliffhanger.
            assessment.deliberate_cliffhanger = False
        return assessment

    @staticmethod
    def _fallback(
        *,
        transcript: str,
        meaningful_turns: int,
        scene_count: int,
        error: str,
        authoritative_progress: SessionEpisodeProgress | None = None,
    ) -> SessionProgressAssessment:
        # This keeps a diagnostic run moving, but deliberately cannot certify
        # closure or a memory anchor. Strict long tests treat fallback as a fail.
        stage = "opening"
        if meaningful_turns >= 8:
            stage = "development"
        if meaningful_turns >= 18 and scene_count >= 2:
            stage = "reversal"
        result = SessionProgressAssessment(
            stage=stage,
            scene_change_recommended=meaningful_turns >= 10,
            reversal_reached=stage == "reversal",
            continuity_ok=bool(str(transcript or "").strip()),
            used_fallback=True,
            model_error=error,
            next_gm_need="离线场次评估不可用，不能据此认定本场已经收束。",
            scene_topology_ok=scene_count >= 3,
            distinct_functional_scene_count=min(scene_count, 3),
            distinct_camera_count=scene_count,
        )
        return SessionProgressEvaluator._merge_authoritative(
            result,
            authoritative_progress,
        )

    @staticmethod
    def _merge_authoritative(
        assessment: SessionProgressAssessment,
        progress: SessionEpisodeProgress | None,
    ) -> SessionProgressAssessment:
        """Preserve committed rules evidence if the offline evaluator is slow.

        This never lets the evaluator invent a resolution.  It only copies
        events already committed by the rules/scene lifecycle so an evaluator
        timeout cannot freeze the table on an earlier act. The caller still
        sees ``used_fallback`` and reports the degraded evaluation.
        """

        if progress is None:
            return assessment
        rank = {stage: index for index, stage in enumerate(_STAGES)}
        authoritative_stage = str(progress.stage or "opening").strip().lower()
        if rank.get(authoritative_stage, 0) > rank.get(assessment.stage, 0):
            assessment.stage = authoritative_stage
        assessment.local_question_changed = bool(
            assessment.local_question_changed or progress.local_question_changed
        )
        assessment.local_question_resolved = bool(
            assessment.local_question_resolved or progress.local_question_resolved
        )
        assessment.deliberate_cliffhanger = bool(
            assessment.deliberate_cliffhanger or progress.deliberate_cliffhanger
        )
        assessment.reversal_reached = bool(
            assessment.reversal_reached or progress.reversal_reached
        )
        assessment.concrete_consequence = bool(
            assessment.concrete_consequence or progress.concrete_consequences
        )
        assessment.opposition_move_present = bool(
            assessment.opposition_move_present or progress.opposition_moves
        )
        assessment.signature_image_evolved = bool(
            assessment.signature_image_evolved or progress.signature_image_evolved
        )
        assessment.local_payoff_present = bool(
            assessment.local_payoff_present
            or progress.local_payoffs
            or progress.local_question_changed
            or progress.local_question_resolved
        )
        assessment.memory_image = assessment.memory_image or progress.memory_image
        assessment.memory_choice = assessment.memory_choice or progress.memory_choice
        assessment.memory_consequence = (
            assessment.memory_consequence or progress.memory_consequence
        )
        return SessionProgressEvaluator._normalize_assessment(assessment)
