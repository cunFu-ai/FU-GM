from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from fu_gm.components.npc_speech_plan import render_public_segments
from fu_gm.components.scene_moment_policy import SceneMomentPolicy
from fu_gm.deepseek_roleplay import (
    apply_deepseek_reasoning_style,
    normalize_deepseek_roleplay_mode,
)
from fu_gm.llm_utils import extract_json_object
from fu_gm.prompt_cache import build_cache_friendly_messages


NPC_VOICE_SYSTEM_PROMPT = """
你是FU-GM的NPC声线演员。核心GM已经决定了NPC的立场、公开事实、拒绝、条件、承诺与行动；你只负责把这些权威内容写成该NPC此刻自然说出口或做出来的表现。

硬性边界：
1. 输入中的content_segments是完整且唯一的公开内容来源。逐段改写语气，不得新增、删除、合并、拆分、倒置或扩大任何事实、条件、承诺、人物行动和结果。
2. rendered_segments必须与输入保持完全相同的id、数量和顺序。每段text只表现该id对应的内容，但所有段落最终会按顺序连在一起，因此要把它们写成同一次自然发言：后续段落可以加入“不过”“只是”“至于”“所以”等不改变事实的衔接词，不能让每个条目都像独立公告。
3. npc_profile中的目标、立场、情绪、权限和知识范围只用于表演方向，不自动属于NPC本轮公开说出的内容。尤其不能泄露秘密、幕后动机或输入没有授权公开的名字。
4. NPC可以命令、请求或询问英雄，但不能替玩家角色点头、移动、拿取、回答、同意、失败或作决定。玩家已经说过的话也不要复述一遍。
5. 使用自然、完整的中文口语。简洁不等于电报体：不要为了显得干练而连续省略主语、谓语或连接关系，不要写成“能走。钥匙不交。人，我派。”这类断裂残句；“短句”表示少解释，不表示把一句正常的话切碎。除非人物被明确设定为机械式说话，否则相邻短意群应自然连成一句或两句。NPC直接回答时，优先形成一段连贯台词，并可用一个极短的神态或动作承载语气。content_segments可能采用“可以通行”“由巡守带队”一类结构化书面措辞；除非人物本来就说官话，不要照抄成公告，应在不改变事实的前提下说成日常对话。不要写主持说明、规则标签、JSON术语、互动焦点、玩家选项教学、内心独白或思考过程。
6. 不要为了文采添加新的道具、气味、声响、伤势、关系、地名、追兵、危险或情报。语气可以鲜明，事实必须克制。
7. 除非任何改写都会损失精确含义，否则至少通过句式、停顿、称谓或语气体现一项speech_style；speech_style和voice_examples只提供人物倾向，不要模仿其中偶然出现的语病、残句或过度停顿。自然口语始终优先。不要把所有条目逐字原样返回，也不要为了显得有文采而扩写事实。

只输出一个JSON对象：
{"rendered_segments":[{"id":"与输入一致","text":"最终公开文字"}]}
""".strip()


NPC_VOICE_AUDIT_SYSTEM_PROMPT = """
你是FU-GM的NPC台词一致性审计器，不负责续写或润色。比较authoritative_segments与candidate_reply，确认候选只改变表达风格，没有改变权威决定。

逐项检查：
1. 每个segment_id表达的事实、直接回答、拒绝、条件、回报、承诺、请求、行动和结果都完整保留。
2. 候选没有增加权威条目中不存在的人名、关系、知识、物件、地点、行动、承诺、条件、态度变化或环境结果。
3. private_performance_direction只可影响语气，不能被当作公开事实说出。
4. 候选没有替任何玩家角色行动、回答、同意或作决定。
5. 措辞变化和不改变事实的短动作描写可以通过；缺失、扩大、倒置、暗中补完或泄密必须拒绝。

只输出一个JSON对象：
{"valid":true|false,"missing_segment_ids":[],"unsupported_claims":[],"reason":"一句简短理由"}
""".strip()


@dataclass(frozen=True)
class NPCVoiceRenderResult:
    text: str
    used_model: bool
    used_fallback: bool
    audit_performed: bool
    audit_passed: bool
    model: str = ""
    latency_ms: int = 0
    fallback_reason: str = ""

    def telemetry(self) -> dict[str, object]:
        return {
            "used_model": self.used_model,
            "used_fallback": self.used_fallback,
            "audit_performed": self.audit_performed,
            "audit_passed": self.audit_passed,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "fallback_reason": self.fallback_reason,
        }


class NPCVoiceRenderer:
    """Render one already-decided NPC response without owning game state."""

    _DEFAULT_RENDER_TIMEOUT_SECONDS = 10.0
    _DEFAULT_AUDIT_TIMEOUT_SECONDS = 5.0
    _MIN_CALL_BUDGET_SECONDS = 0.25

    _BACKSTAGE_MARKERS = (
        "<think>",
        "</think>",
        "content_segments",
        "rendered_segments",
        "segment_id",
        "系统提示",
        "作为AI",
        "作为 AI",
        "核心GM",
        "规则层",
        "（心想",
        "(心想",
        "内心OS",
        "内心 OS",
        "内心独白",
    )
    _HIGH_RISK_TAGS = frozenset(
        {
            "direct_answer",
            "fact",
            "gate_requirement",
            "gate_payoff",
            "settled_terms",
            "deferred_action",
            "deferred_result",
            "deferred_trigger",
            "player_request",
        }
    )

    def __init__(
        self,
        *,
        client: Any | None,
        model: str,
        audit_client: Any | None = None,
        audit_model: str = "",
        audit_mode: str = "off",
        enabled: bool = True,
        max_output_tokens: int = 900,
        deepseek_roleplay_mode: str = "default",
        render_timeout_seconds: float = _DEFAULT_RENDER_TIMEOUT_SECONDS,
        audit_timeout_seconds: float = _DEFAULT_AUDIT_TIMEOUT_SECONDS,
    ) -> None:
        self.client = client
        self.model = str(model or "").strip()
        self.audit_client = audit_client
        self.audit_model = str(audit_model or "").strip()
        normalized_mode = str(audit_mode or "off").strip().lower()
        self.audit_mode = (
            normalized_mode
            if normalized_mode in {"off", "high_risk", "all"}
            else "off"
        )
        self.enabled = bool(enabled and client is not None and self.model)
        self.max_output_tokens = max(256, int(max_output_tokens))
        self.render_timeout_seconds = max(
            self._MIN_CALL_BUDGET_SECONDS,
            float(render_timeout_seconds),
        )
        self.audit_timeout_seconds = max(
            self._MIN_CALL_BUDGET_SECONDS,
            float(audit_timeout_seconds),
        )
        self.deepseek_roleplay_mode = normalize_deepseek_roleplay_mode(
            deepseek_roleplay_mode
        )
        self.last_raw_content = ""
        self.last_audit_raw_content = ""
        self.last_result: NPCVoiceRenderResult | None = None

    def render(
        self,
        *,
        persona: Any,
        public_segments: list[dict[str, Any]],
        speech_plan: dict[str, Any],
        current_message: str,
        recent_context: str,
        scene: Any | None,
        introduced_names: list[str] | None = None,
        system_gm_beat: bool = False,
        deadline: float | None = None,
    ) -> NPCVoiceRenderResult:
        fallback = render_public_segments(public_segments)
        started = time.monotonic()
        self.last_raw_content = ""
        self.last_audit_raw_content = ""
        if not self.enabled:
            return self._finish(
                text=fallback,
                used_model=False,
                used_fallback=True,
                audit_performed=False,
                audit_passed=False,
                started=started,
                reason="npc_voice_disabled",
            )

        try:
            request = self._voice_request(
                persona=persona,
                public_segments=public_segments,
                speech_plan=speech_plan,
                current_message=current_message,
                recent_context=recent_context,
                scene=scene,
                system_gm_beat=system_gm_beat,
            )
            request_json = json.dumps(request, ensure_ascii=False)
            render_deadline = self._child_deadline(
                deadline,
                timeout_seconds=self.render_timeout_seconds,
            )
            self._require_call_budget(
                render_deadline,
                operation="npc_voice_render",
            )
            # The current DeepSeek path deliberately keeps reasoning disabled.
            # Roleplay style remains a compatibility setting, but cannot turn
            # thinking back on for this latency-sensitive side call.
            request_thinking_enabled = False
            styled_request_json = apply_deepseek_reasoning_style(
                request_json,
                model=self.model,
                mode=self.deepseek_roleplay_mode,
                thinking_enabled=request_thinking_enabled,
            )
            self.last_raw_content = self.client.create_chat_completion(
                model=self.model,
                messages=build_cache_friendly_messages(
                    static_system_prompt=NPC_VOICE_SYSTEM_PROMPT,
                    user_content=styled_request_json,
                    cache_family="npc-voice",
                    user_cache_breakpoint_offsets=(
                        request_json.find('"interaction"'),
                    ),
                ),
                temperature=0.65,
                response_format=self._response_format(self.client),
                max_tokens=self.max_output_tokens,
                deadline=render_deadline,
                operation="npc_voice_render",
                thinking_enabled=request_thinking_enabled,
                max_recovery_retries=1,
                retry_without_response_format_on_empty=True,
            )
            payload = extract_json_object(self.last_raw_content)
            rendered_segments = self._validated_rendered_segments(
                payload.get("rendered_segments"),
                source_segments=public_segments,
            )
            candidate = render_public_segments(rendered_segments)
            self._validate_candidate_locally(
                candidate,
                fallback=fallback,
                persona=persona,
                introduced_names=introduced_names or [],
            )
            audit_required = self._audit_required(public_segments, speech_plan)
            audit_passed = True
            if audit_required:
                if self.audit_client is None or not self.audit_model:
                    raise ValueError("npc_voice_auditor_unavailable")
                audit_deadline = self._child_deadline(
                    deadline,
                    timeout_seconds=self.audit_timeout_seconds,
                )
                self._require_call_budget(
                    audit_deadline,
                    operation="npc_voice_grounding_audit",
                )
                audit_passed, audit_reason = self._audit(
                    persona=persona,
                    public_segments=public_segments,
                    speech_plan=speech_plan,
                    candidate=candidate,
                    current_message=current_message,
                    recent_context=recent_context,
                    deadline=audit_deadline,
                )
                if not audit_passed:
                    raise ValueError(f"npc_voice_audit_rejected:{audit_reason}")
            return self._finish(
                text=candidate,
                used_model=True,
                used_fallback=False,
                audit_performed=audit_required,
                audit_passed=audit_passed,
                started=started,
            )
        except Exception as exc:
            return self._finish(
                text=fallback,
                used_model=bool(self.last_raw_content),
                used_fallback=True,
                audit_performed=bool(self.last_audit_raw_content),
                audit_passed=False,
                started=started,
                reason=str(exc)[:300],
            )

    def _audit_required(
        self,
        public_segments: list[dict[str, Any]],
        speech_plan: dict[str, Any],
    ) -> bool:
        if self.audit_mode == "off":
            return False
        if self.audit_mode == "all":
            return True
        if any(
            self._HIGH_RISK_TAGS.intersection(segment.get("tags") or [])
            for segment in public_segments
        ):
            return True
        return bool(
            str(speech_plan.get("speech_act") or "answer") != "answer"
            or str(speech_plan.get("proposal_outcome") or "none") != "none"
            or str(speech_plan.get("condition_outcome") or "none") != "none"
            or str(speech_plan.get("commitment_outcome") or "none") != "none"
            or speech_plan.get("introduced_npcs")
        )

    def _audit(
        self,
        *,
        persona: Any,
        public_segments: list[dict[str, Any]],
        speech_plan: dict[str, Any],
        candidate: str,
        current_message: str,
        recent_context: str,
        deadline: float,
    ) -> tuple[bool, str]:
        request = {
            "npc": str(getattr(persona, "name", "") or ""),
            "authoritative_segments": public_segments,
            "authoritative_decision": self._public_decision(speech_plan),
            "private_performance_direction": self._private_direction(persona),
            "current_player_message": str(current_message or "")[-1200:],
            "recent_public_context": str(recent_context or "")[-1800:],
            "candidate_reply": candidate,
        }
        request_json = json.dumps(request, ensure_ascii=False)
        self.last_audit_raw_content = self.audit_client.create_chat_completion(
            model=self.audit_model,
            messages=build_cache_friendly_messages(
                static_system_prompt=NPC_VOICE_AUDIT_SYSTEM_PROMPT,
                user_content=request_json,
                cache_family="npc-voice-audit",
                user_cache_breakpoint_offsets=(
                    request_json.find('"candidate_reply"'),
                ),
            ),
            temperature=0.0,
            response_format=self._response_format(self.audit_client),
            max_tokens=500,
            deadline=deadline,
            operation="npc_voice_grounding_audit",
            thinking_enabled=False,
            max_recovery_retries=1,
            retry_without_response_format_on_empty=True,
        )
        payload = extract_json_object(self.last_audit_raw_content)
        if not isinstance(payload.get("valid"), bool):
            raise ValueError("npc_voice_audit_missing_valid")
        reasons = [
            str(payload.get("reason") or "").strip(),
            *[
                str(item or "").strip()
                for item in list(payload.get("unsupported_claims") or [])[:4]
            ],
            *[
                f"缺少段落:{str(item or '').strip()}"
                for item in list(payload.get("missing_segment_ids") or [])[:4]
            ],
        ]
        return bool(payload["valid"]), "；".join(item for item in reasons if item)[:400]

    @classmethod
    def _validated_rendered_segments(
        cls,
        value: Any,
        *,
        source_segments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise ValueError("npc_voice_rendered_segments_required")
        source_ids = [str(item.get("id") or "").strip() for item in source_segments]
        rendered_ids = [
            str(item.get("id") or "").strip() if isinstance(item, dict) else ""
            for item in value
        ]
        if rendered_ids != source_ids:
            raise ValueError("npc_voice_segment_ids_changed")
        result: list[dict[str, Any]] = []
        for source, rendered in zip(source_segments, value):
            if not isinstance(rendered, dict):
                raise ValueError("npc_voice_segment_must_be_object")
            text = " ".join(str(rendered.get("text") or "").split()).strip()
            if not text:
                raise ValueError("npc_voice_segment_text_required")
            if len(text) > 480:
                raise ValueError("npc_voice_segment_too_long")
            result.append(
                {
                    "id": str(source.get("id") or "").strip(),
                    "text": text,
                    "tags": list(source.get("tags") or []),
                }
            )
        return result

    @classmethod
    def _validate_candidate_locally(
        cls,
        candidate: str,
        *,
        fallback: str,
        persona: Any,
        introduced_names: list[str],
    ) -> None:
        if not candidate:
            raise ValueError("npc_voice_empty_reply")
        if len(candidate) > max(1000, len(fallback) * 3 + 240):
            raise ValueError("npc_voice_reply_expanded_too_far")
        if any(marker.lower() in candidate.lower() for marker in cls._BACKSTAGE_MARKERS):
            raise ValueError("npc_voice_backstage_text_leaked")
        if SceneMomentPolicy.looks_like_backstage_formula(candidate):
            raise ValueError("npc_voice_backstage_formula")
        agency_error = SceneMomentPolicy.player_agency_violation(
            candidate,
            {
                "prepared_npcs": [
                    {
                        "name": str(getattr(persona, "name", "") or ""),
                        "public_role": str(
                            getattr(persona, "public_identity", "") or ""
                        ),
                    }
                ]
            },
        )
        if agency_error:
            raise ValueError("npc_voice_player_agency_violation")
        missing_names = [
            name for name in introduced_names if name and name not in candidate
        ]
        if missing_names:
            raise ValueError("npc_voice_omitted_introduced_npc")

    @staticmethod
    def _voice_request(
        *,
        persona: Any,
        public_segments: list[dict[str, Any]],
        speech_plan: dict[str, Any],
        current_message: str,
        recent_context: str,
        scene: Any | None,
        system_gm_beat: bool,
    ) -> dict[str, object]:
        return {
            "npc_profile": {
                "name": str(getattr(persona, "name", "") or ""),
                "public_identity": str(
                    getattr(persona, "public_identity", "") or ""
                ),
                "role_in_story": str(
                    getattr(persona, "role_in_story", "") or ""
                ),
                "manner": str(getattr(persona, "manner", "") or ""),
                "speech_style": str(
                    getattr(persona, "speech_style", "") or ""
                ),
                "traits": list(getattr(persona, "traits", []) or [])[:4],
                "voice_examples": list(
                    getattr(persona, "voice_examples", []) or []
                )[:4],
                "current_mood": str(
                    getattr(persona, "current_mood", "") or ""
                ),
                "current_stance": str(
                    getattr(persona, "current_stance", "") or ""
                ),
                "private_performance_direction": NPCVoiceRenderer._private_direction(
                    persona
                ),
            },
            "scene": {
                "name": str(getattr(scene, "name", "") or ""),
                "location": str(getattr(scene, "location", "") or ""),
            },
            "interaction": {
                "source": "gm_beat" if system_gm_beat else "player_message",
                "player_message": str(current_message or "")[-1200:],
                "recent_public_context": str(recent_context or "")[-1800:],
            },
            "decision": {
                **NPCVoiceRenderer._public_decision(speech_plan),
                "content_segments": public_segments,
            },
        }

    @staticmethod
    def _private_direction(persona: Any) -> dict[str, object]:
        return {
            "core_drive": str(getattr(persona, "core_drive", "") or ""),
            "active_goal": str(getattr(persona, "active_goal", "") or ""),
            "authority_scope": str(
                getattr(persona, "authority_scope", "") or ""
            ),
            "knowledge_scope": str(
                getattr(persona, "knowledge_scope", "") or ""
            ),
            "refusal_move": str(getattr(persona, "refusal_move", "") or ""),
            "taboos": list(getattr(persona, "taboos", []) or [])[:4],
        }

    @staticmethod
    def _public_decision(speech_plan: dict[str, Any]) -> dict[str, object]:
        return {
            key: speech_plan.get(key)
            for key in (
                "speech_act",
                "condition_outcome",
                "proposal_outcome",
                "promise_kind",
                "promise_subject",
                "commitment_outcome",
                "required_outcome",
            )
            if speech_plan.get(key) not in (None, "", "none", [])
        }

    @staticmethod
    def _response_format(client: Any) -> dict[str, str] | None:
        enabled = bool(
            getattr(getattr(client, "config", None), "response_format_enabled", True)
        )
        return {"type": "json_object"} if enabled else None

    @staticmethod
    def _child_deadline(
        outer_deadline: float | None,
        *,
        timeout_seconds: float,
    ) -> float:
        local_deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        if outer_deadline is None:
            return local_deadline
        try:
            return min(local_deadline, float(outer_deadline))
        except (TypeError, ValueError):
            return local_deadline

    @classmethod
    def _require_call_budget(cls, deadline: float, *, operation: str) -> None:
        if float(deadline) - time.monotonic() < cls._MIN_CALL_BUDGET_SECONDS:
            raise TimeoutError(f"{operation}_deadline_budget_exhausted")

    def _finish(
        self,
        *,
        text: str,
        used_model: bool,
        used_fallback: bool,
        audit_performed: bool,
        audit_passed: bool,
        started: float,
        reason: str = "",
    ) -> NPCVoiceRenderResult:
        result = NPCVoiceRenderResult(
            text=str(text or "").strip(),
            used_model=used_model,
            used_fallback=used_fallback,
            audit_performed=audit_performed,
            audit_passed=audit_passed,
            model=self.model,
            latency_ms=max(0, int((time.monotonic() - started) * 1000)),
            fallback_reason=str(reason or "").strip()[:300],
        )
        self.last_result = result
        return result


__all__ = ["NPCVoiceRenderer", "NPCVoiceRenderResult"]
