from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from fu_gm.components.scene_moment_policy import SceneMomentPolicy
from fu_gm.deepseek_roleplay import (
    apply_deepseek_reasoning_style,
    normalize_deepseek_roleplay_mode,
    strip_deepseek_reasoning_leakage,
)
from fu_gm.llm_utils import extract_json_object
from fu_gm.prompt_cache import build_cache_friendly_messages


SCENE_CREATIVE_GROUNDING_PROMPT = (
    "你是FU-GM创作文本的事实审计器，不负责写作或提供替代句。"
    "判断公开开场是否在自然改写后仍完整表达required_public_facts，是否与这些事实矛盾，"
    "并检查它是否与无需在本段复述的authoritative_public_facts矛盾，"
    "是否泄露forbidden_private_facts，以及player_handoff是否在public_opening里重复出现。"
    "事实无需逐字相同，只要主语、状态、时序和因果没有改变即可。"
    "只输出JSON：{\"valid\":true|false,\"missing_facts\":[\"...\"],"
    "\"contradictions\":[\"...\"],\"private_leaks\":[\"...\"],"
    "\"handoff_repeated\":true|false,\"reason\":\"...\"}。"
)


class SceneCreativeWriterError(RuntimeError):
    """Raised when the configured creative author cannot return usable prose."""


@dataclass(frozen=True)
class SceneOpeningComposition:
    private_situation: dict[str, object]
    public_opening: str
    player_handoff: str
    model: str = ""
    used_model: bool = False


@dataclass(frozen=True)
class SceneTransitionComposition:
    private_situation: dict[str, object]
    public_arrival: str
    model: str = ""
    used_model: bool = False


@dataclass(frozen=True)
class PublicSceneComposition:
    public_reply: str
    closing_image: str = ""
    model: str = ""
    used_model: bool = False


class SceneCreativeWriter:
    """DeepSeek-backed author for private situation prep and table-facing prose.

    The core GM remains responsible for intent, participants, destinations and
    tool choice.  This component receives those already bounded facts and writes
    only the creative layer.  Python tool handlers validate the returned packet
    before any state is committed.
    """

    _PRIVATE_SCALAR_FIELDS = {
        "premise",
        "stakes",
        "current_pressure",
        "dramatic_question",
        "signature_image",
        "opposition_goal",
        "dilemma",
        "reversal",
        "climax_type",
        "closure_requirement",
        "irreversible_change",
        "ending_echo",
    }
    _PRIVATE_LIST_FIELDS = {
        "fantastic_details",
        "escalation_ladder",
        "possible_payoffs",
        "visible_elements",
        "npc_functions",
        "clue_pool",
        "secrets",
        "possible_reveals",
        "telegraphed_threats",
        "danger_candidates",
        "discovery_candidates",
        "special_mechanism_candidates",
        "story_outline",
    }
    _OPENING_MAX_OUTPUT_TOKENS = 2400
    # Public scene sidecars have small, fixed JSON shapes.  A per-operation
    # ceiling prevents one verbose creative call from consuming the remainder
    # of the player transaction while leaving enough room for authoritative
    # facts and Chinese prose.  Unknown future operations retain a conservative
    # 2400-token ceiling instead of inheriting the old 6000-token blanket cap.
    _OPERATION_MAX_OUTPUT_TOKENS = {
        "scene_transition": 3000,
        "scene_response": 1800,
        "npc_introduction": 1800,
        "npc_combat_action": 800,
        "clock_change": 1200,
        "conflict_opening": 1800,
        "scene_closure": 1800,
        "conflict_closure": 1800,
        "session_closure": 2400,
    }
    _DEFAULT_MAX_OUTPUT_TOKENS = 2400
    # 150-250 Chinese characters is the writing target. The hard bound stays
    # deliberately generous so several long authoritative facts do not cause
    # an avoidable repair round.
    _OPENING_PUBLIC_HARD_MAX_CHARS = 500
    _OPENING_HANDOFF_HARD_MAX_CHARS = 120
    _AUDIT_MAX_OUTPUT_TOKENS = 900

    def __init__(
        self,
        *,
        client: Any | None,
        model: str,
        audit_client: Any | None = None,
        audit_model: str = "",
        deepseek_roleplay_mode: str = "default",
    ) -> None:
        self.client = client
        self.model = str(model or "").strip()
        self.audit_client = audit_client
        self.audit_model = str(audit_model or "").strip()
        self.deepseek_roleplay_mode = normalize_deepseek_roleplay_mode(
            deepseek_roleplay_mode
        )
        self.last_operation = ""
        self.last_error = ""
        self.last_audit_error = ""
        self.last_audit_status = "not_run"
        self.last_raw_content = ""
        self.call_count = 0
        self.audit_call_count = 0

    @property
    def available(self) -> bool:
        return self.client is not None and bool(self.model)

    def compose_scene_opening(
        self,
        *,
        scene_request: dict[str, object],
        session_contract: dict[str, object],
        opening_contract: dict[str, object],
        current_message: str,
        recent_public_messages: list[dict[str, object]],
        fallback_private_situation: dict[str, object] | None = None,
        fallback_public_opening: str = "",
        fallback_player_handoff: str = "",
        deadline: float | None = None,
    ) -> SceneOpeningComposition:
        if not self.available:
            return SceneOpeningComposition(
                private_situation=self._private_packet(
                    fallback_private_situation or {}
                ),
                public_opening=self._clean(fallback_public_opening),
                player_handoff=self._clean(fallback_player_handoff),
            )
        payload = {
            "operation": "scene_opening",
            "scene_request": scene_request,
            "session_contract": session_contract,
            "opening_contract": opening_contract,
            "current_message": self._clean(current_message),
            "recent_public_messages": recent_public_messages[-8:],
        }
        result = self._complete(
            payload,
            operation="scene_opening",
            deadline=deadline,
        )
        try:
            return self._scene_opening_composition(
                result,
                opening_contract=opening_contract,
                deadline=deadline,
            )
        except SceneCreativeWriterError as first_error:
            repair_payload = {
                **payload,
                "operation": "scene_opening_repair",
                "previous_response": result,
                "repair_instruction": str(first_error),
            }
            repaired = self._complete(
                repair_payload,
                operation="scene_opening_repair",
                deadline=deadline,
            )
            return self._scene_opening_composition(
                repaired,
                opening_contract=opening_contract,
                deadline=deadline,
            )

    def validate_prepared_scene_opening(
        self,
        packet: dict[str, object],
    ) -> SceneOpeningComposition:
        """校验受信复合工具已生成的开场包，不再次调用任何模型。"""

        return self._scene_opening_composition(
            packet,
            opening_contract={},
        )

    def _scene_opening_composition(
        self,
        result: dict[str, object],
        *,
        opening_contract: dict[str, object],
        deadline: float | None = None,
    ) -> SceneOpeningComposition:
        private_situation = self._private_packet(result.get("private_situation"))
        public_opening = self._compact_paragraph(result.get("public_opening"))
        player_handoff = self._compact_paragraph(result.get("player_handoff"))
        if not private_situation or not public_opening or not player_handoff:
            raise SceneCreativeWriterError(
                "创作模型没有完整返回private_situation、public_opening和player_handoff。"
            )
        self._validate_public_text(public_opening)
        self._validate_public_text(player_handoff)
        if self._visible_length(public_opening) > self._OPENING_PUBLIC_HARD_MAX_CHARS:
            raise SceneCreativeWriterError(
                "public_opening超过宽容上限，请压缩为单段现场画面与即时压力。"
            )
        if self._visible_length(player_handoff) > self._OPENING_HANDOFF_HARD_MAX_CHARS:
            raise SceneCreativeWriterError(
                "player_handoff过长，请只保留一句开放问题。"
            )
        question_marks = player_handoff.count("？") + player_handoff.count("?")
        if question_marks != 1 or not re.search(
            r"[？?][\"'”’」』】）)]*$",
            player_handoff,
        ):
            raise SceneCreativeWriterError(
                "player_handoff必须是恰好一句、以问号结束的开放问题。"
            )
        if player_handoff in public_opening:
            raise SceneCreativeWriterError(
                "player_handoff已经重复出现在public_opening中。"
            )
        self._validate_opening_grounding(
            opening_contract=opening_contract,
            public_opening=public_opening,
            player_handoff=player_handoff,
            deadline=deadline,
        )
        return SceneOpeningComposition(
            private_situation=private_situation,
            public_opening=public_opening,
            player_handoff=player_handoff,
            model=self.model,
            used_model=True,
        )

    def compose_transition(
        self,
        *,
        transition_request: dict[str, object],
        session_contract: dict[str, object],
        current_scene: dict[str, object],
        recent_public_messages: list[dict[str, object]],
        fallback_private_situation: dict[str, object] | None = None,
        fallback_public_arrival: str = "",
        deadline: float | None = None,
    ) -> SceneTransitionComposition:
        if not self.available:
            return SceneTransitionComposition(
                private_situation=self._private_packet(
                    fallback_private_situation or {}
                ),
                public_arrival=self._clean(fallback_public_arrival),
            )
        payload = {
            "operation": "scene_transition",
            "transition_request": transition_request,
            "session_contract": session_contract,
            "current_scene": current_scene,
            "recent_public_messages": recent_public_messages[-8:],
        }
        result = self._complete(
            payload,
            operation="scene_transition",
            deadline=deadline,
        )
        private_situation = self._private_packet(result.get("private_situation"))
        public_arrival = self._clean(result.get("public_arrival"))
        if not private_situation or not public_arrival:
            raise SceneCreativeWriterError(
                "创作模型没有完整返回private_situation和public_arrival。"
            )
        self._validate_public_text(public_arrival)
        return SceneTransitionComposition(
            private_situation=private_situation,
            public_arrival=public_arrival,
            model=self.model,
            used_model=True,
        )

    def compose_public_scene_text(
        self,
        *,
        operation: str,
        facts: dict[str, object],
        recent_public_messages: list[dict[str, object]],
        fallback_public_reply: str = "",
        require_closing_image: bool = False,
        deadline: float | None = None,
    ) -> PublicSceneComposition:
        if not self.available:
            return PublicSceneComposition(public_reply=self._clean(fallback_public_reply))
        payload = {
            "operation": operation,
            "facts": facts,
            "recent_public_messages": recent_public_messages[-8:],
            "require_closing_image": require_closing_image,
        }
        result = self._complete(
            payload,
            operation=operation,
            deadline=deadline,
        )
        public_reply = self._clean(result.get("public_reply"))
        closing_image = self._clean(result.get("closing_image"))
        if not public_reply:
            raise SceneCreativeWriterError("创作模型没有返回public_reply。")
        if require_closing_image and not closing_image:
            raise SceneCreativeWriterError("创作模型没有返回closing_image。")
        self._validate_public_text(public_reply)
        self._validate_required_public_content(
            operation=operation,
            facts=facts,
            public_reply=public_reply,
            closing_image=closing_image,
            require_closing_image=require_closing_image,
        )
        return PublicSceneComposition(
            public_reply=public_reply,
            closing_image=closing_image,
            model=self.model,
            used_model=True,
        )

    def diagnostics(self) -> dict[str, object]:
        return {
            "available": self.available,
            "model": self.model,
            "call_count": self.call_count,
            "last_operation": self.last_operation,
            "last_error": self.last_error,
            "audit_model": self.audit_model,
            "audit_call_count": self.audit_call_count,
            "last_audit_status": self.last_audit_status,
            "last_audit_error": self.last_audit_error,
        }

    def _validate_opening_grounding(
        self,
        *,
        opening_contract: dict[str, object],
        public_opening: str,
        player_handoff: str,
        deadline: float | None = None,
    ) -> None:
        required_facts = self._clean_list(
            opening_contract.get("required_public_facts")
        )
        authoritative_facts = self._clean_list(
            opening_contract.get("authoritative_public_facts")
        )
        forbidden_private_facts = self._clean_list(
            opening_contract.get("forbidden_private_facts")
        )
        if not required_facts and not authoritative_facts and not forbidden_private_facts:
            self.last_audit_status = "not_needed"
            self.last_audit_error = ""
            return
        if self.audit_client is None or not self.audit_model:
            missing = [fact for fact in required_facts if fact not in public_opening]
            leaked = [
                fact for fact in forbidden_private_facts if fact in public_opening
            ]
            if missing or leaked:
                detail = missing or leaked
                raise SceneCreativeWriterError(
                    "离线事实校验未通过：" + "、".join(detail[:4])
                )
            self.last_audit_status = "validated_locally"
            self.last_audit_error = ""
            return
        request = {
            "required_public_facts": required_facts,
            "authoritative_public_facts": authoritative_facts,
            "forbidden_private_facts": forbidden_private_facts,
            "public_opening": public_opening,
            "player_handoff": player_handoff,
        }
        self.audit_call_count += 1
        try:
            raw = self.audit_client.create_chat_completion(
                model=self.audit_model,
                messages=build_cache_friendly_messages(
                    static_system_prompt=SCENE_CREATIVE_GROUNDING_PROMPT,
                    user_content=json.dumps(request, ensure_ascii=False),
                    cache_family="scene-creative-grounding",
                ),
                temperature=0.0,
                response_format={"type": "json_object"},
                max_tokens=self._AUDIT_MAX_OUTPUT_TOKENS,
                deadline=deadline,
                operation="scene_creative_grounding_review",
                thinking_enabled=False,
                max_recovery_retries=1,
                retry_without_response_format_on_empty=True,
            )
            review = extract_json_object(raw)
            if not isinstance(review.get("valid"), bool):
                raise ValueError("创作事实审计缺少布尔字段valid。")
            if not review["valid"]:
                problems = []
                for key in ("missing_facts", "contradictions", "private_leaks"):
                    problems.extend(self._clean_list(review.get(key)))
                if bool(review.get("handoff_repeated")):
                    problems.append("开场正文重复了行动权交接句")
                reason = self._clean(review.get("reason"))
                self.last_audit_status = "rejected"
                raise SceneCreativeWriterError(
                    "语义事实审计未通过："
                    + "、".join((problems or [reason or "未说明原因"])[:4])
                )
            self.last_audit_status = "approved"
            self.last_audit_error = ""
        except SceneCreativeWriterError:
            raise
        except Exception as exc:
            self.last_audit_status = "error"
            self.last_audit_error = str(exc)[:500]
            raise SceneCreativeWriterError(
                "创作事实审计失败：" + self.last_audit_error
            ) from exc

    def _complete(
        self,
        payload: dict[str, object],
        *,
        operation: str,
        deadline: float | None = None,
    ) -> dict[str, object]:
        self.last_operation = operation
        self.last_error = ""
        self.last_raw_content = ""
        self.call_count += 1
        try:
            request_thinking_enabled = bool(
                getattr(
                    getattr(self.client, "config", None),
                    "thinking_enabled",
                    False,
                )
            )
            user_content = apply_deepseek_reasoning_style(
                json.dumps(payload, ensure_ascii=False, default=str),
                model=self.model,
                mode=self.deepseek_roleplay_mode,
                thinking_enabled=request_thinking_enabled,
            )
            raw = self.client.create_chat_completion(
                model=self.model,
                messages=build_cache_friendly_messages(
                    static_system_prompt=self._system_prompt(),
                    user_content=user_content,
                    cache_family="scene-creative-writer",
                ),
                temperature=0.65,
                response_format={"type": "json_object"},
                max_tokens=self._max_output_tokens(operation),
                deadline=deadline,
                operation=operation,
                thinking_enabled=request_thinking_enabled,
                max_recovery_retries=1,
                retry_without_response_format_on_empty=True,
            )
            self.last_raw_content = str(raw or "")
            result = extract_json_object(raw)
            if not isinstance(result, dict):
                raise ValueError("返回内容不是JSON对象。")
            return result
        except Exception as exc:
            self.last_error = str(exc)[:500]
            raise SceneCreativeWriterError(self.last_error) from exc

    @classmethod
    def _max_output_tokens(cls, operation: str) -> int:
        if operation in {"scene_opening", "scene_opening_repair"}:
            return cls._OPENING_MAX_OUTPUT_TOKENS
        return cls._OPERATION_MAX_OUTPUT_TOKENS.get(
            str(operation or "").strip(),
            cls._DEFAULT_MAX_OUTPUT_TOKENS,
        )

    def _system_prompt(self) -> str:
        contract = (
            "你是FU-GM的创作作者，只负责GM私密局面和面向玩家的自然中文表达。"
            "核心GM已经决定工具、人物、地点、目标和已发生事实；不得改变这些事实，"
            "不得替玩家角色行动，也不得新增未授权的在场人物。只输出JSON对象。\n"
            "operation=scene_opening或scene_opening_repair时，必须严格输出这个JSON形状："
            "{\"private_situation\":{\"premise\":\"...\",\"stakes\":\"...\","
            "\"current_pressure\":\"...\",\"opposition_goal\":\"...\",\"dilemma\":\"...\","
            "\"visible_elements\":[\"...\"],\"clue_pool\":[\"...\"],\"secrets\":[\"...\"],"
            "\"escalation_ladder\":[\"...\"],\"possible_payoffs\":[\"...\"]},"
            "\"public_opening\":\"...\",\"player_handoff\":\"...\"}。"
            "private_situation必须是JSON对象，绝不能写成字符串；它是可修改的局面准备，不是固定剧情。"
            "private_situation的标量各用一个短句，数组最多3项且每项只写一句；不要在不同字段重复同一解释。"
            "opening_contract.required_public_facts中的每项事实都必须在public_opening中完整保留；"
            "可以自然改写，但不能改变主语、状态、时序或因果。"
            "public_opening只写角色此刻能看到、"
            "听到或接触的内容，不能泄露秘密、幕后答案、互动焦点、设计意图或字段名；"
            "public_opening写成一个自然段，以150到250个中文字符为目标，只保留一幅现场画面和一项正在逼近的即时压力；"
            "若required_public_facts较长可以适当超过250字，但不得超过500字，也不要为了凑字数复述背景。"
            "public_opening不要包含提问或交接句；player_handoff单独用一句面向所有在场玩家的自然开放问题"
            "交还行动权，以问号结束且不超过120字，并不得与public_opening重复。player_handoff不得写成‘你们可以先A、B或C’之类的"
            "动作菜单，也不要使用‘眼下的难题很明确’‘可互动焦点’‘接下来可以’等幕后归纳；"
            "让眼前人物、声音或正在变化的事物自然逼出一句‘你们怎么做？’或同等开放的问句。"
            "scene_opening_repair时根据repair_instruction修正"
            "previous_response，不得删除已经正确的事实。\n"
            "operation=scene_transition时输出private_situation、public_arrival。抵达描述只出现"
            "transition_request.participants中的人物，不得提前完成抵达后的调查、谈判或战斗。\n"
            "operation=scene_response时输出public_reply；facts.public_facts中的每个完整句子都必须"
            "逐字出现在public_reply中且只出现一次，其他内容只能连接或表现这些已授权事实。"
            "operation=npc_introduction时输出public_reply；必须逐字写出facts.required_identities中的"
            "每个名称或公开身份，只描述其此刻如何进入现场，不泄露profile里的秘密。"
            "operation=npc_combat_action时输出public_reply，限一到两句，只写NPC正在尝试的"
            "可见动作；不得预告命中、失败、伤害、异常状态或命刻结果。"
            "operation=clock_change时输出public_reply；必须逐字包含facts.progress_marker，"
            "不得显示命刻类型、赌注、自动推进或其他后台字段。facts.completion_facts中的"
            "每个句子必须逐字出现，不能擅自扩大后果。"
            "operation=conflict_opening时输出public_reply，说明冲突为何在此刻爆发及可观察局势，"
            "但不代替先攻与规则结算。operation=scene_closure、conflict_closure或session_closure时"
            "输出public_reply；session_closure还输出closing_image。收束只能描述facts中已经成立的"
            "结果，不得把未完成目标写成成功；closing_image必须逐字完整出现在public_reply中。\n"
            "语言像真人主持人：具体、顺畅、克制。合并同义信息，不复述玩家刚说的话，不使用"
            "‘这一步的重点是’‘可互动焦点’‘当前目标’‘接下来可以’等后台或教学措辞。"
        )
        return contract

    @staticmethod
    def _validate_public_text(value: str) -> None:
        if strip_deepseek_reasoning_leakage(value) != str(value or "").strip():
            raise SceneCreativeWriterError("创作文本泄露了思考过程或内心独白。")
        if SceneMomentPolicy.looks_like_backstage_formula(value):
            raise SceneCreativeWriterError("创作文本泄露了后台规划措辞。")
        agency_error = SceneMomentPolicy.player_agency_violation(value)
        if agency_error:
            raise SceneCreativeWriterError(agency_error)

    @classmethod
    def _validate_required_public_content(
        cls,
        *,
        operation: str,
        facts: dict[str, object],
        public_reply: str,
        closing_image: str,
        require_closing_image: bool,
    ) -> None:
        required: list[str] = []
        if operation == "scene_response":
            required.extend(cls._clean_list(facts.get("public_facts")))
        elif operation == "npc_introduction":
            required.extend(cls._clean_list(facts.get("required_identities")))
            required.extend(cls._clean_list(facts.get("public_facts")))
        elif operation == "clock_change":
            marker = cls._clean(facts.get("progress_marker"))
            if marker:
                required.append(marker)
            required.extend(cls._clean_list(facts.get("completion_facts")))
        elif operation == "npc_combat_action":
            actor = cls._clean(facts.get("actor"))
            public_identity = cls._clean(facts.get("public_identity"))
            if actor and actor not in public_reply and public_identity not in public_reply:
                raise SceneCreativeWriterError("NPC战斗动作没有指明实际行动者。")
        if require_closing_image:
            required.append(closing_image)
        missing = [item for item in required if item and item not in public_reply]
        if missing:
            raise SceneCreativeWriterError(
                "创作文本漏掉必须逐字公开的内容："
                + "、".join(missing[:4])
            )

    @classmethod
    def _clean_list(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [cls._clean(item) for item in value if cls._clean(item)]

    @staticmethod
    def _compact_paragraph(value: object) -> str:
        return " ".join(str(value or "").split()).strip()

    @staticmethod
    def _visible_length(value: str) -> int:
        return len(re.sub(r"\s+", "", str(value or "")))

    @classmethod
    def _private_packet(cls, value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, object] = {}
        for key in cls._PRIVATE_SCALAR_FIELDS:
            text = cls._clean(value.get(key))
            if text:
                result[key] = text
        for key in cls._PRIVATE_LIST_FIELDS:
            raw = value.get(key)
            if not isinstance(raw, list):
                continue
            items = list(
                dict.fromkeys(
                    cls._clean(item) for item in raw if cls._clean(item)
                )
            )[:20]
            if items:
                result[key] = items
        return result

    @staticmethod
    def _clean(value: object) -> str:
        return str(value or "").strip()


__all__ = [
    "PublicSceneComposition",
    "SceneCreativeWriter",
    "SceneCreativeWriterError",
    "SceneOpeningComposition",
    "SceneTransitionComposition",
]
