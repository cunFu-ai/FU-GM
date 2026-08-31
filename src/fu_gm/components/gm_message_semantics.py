from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from fu_gm.components.world_setting_catalog import WorldSettingCatalog


MESSAGE_SEMANTICS_VERSION = "1"

MESSAGE_RELATIONS = frozenset(
    {"gm", "player", "table", "npc", "mixed", "unclear"}
)
DIALOGUE_ACTS = frozenset(
    {
        "discussion",
        "question",
        "request",
        "answer",
        "proposal",
        "agreement",
        "disagreement",
        "correction",
        "acknowledgement",
        "action_declaration",
        "action_withdrawal",
        "state_contribution",
        "roleplay_speech",
        "other",
    }
)

# OpenAI-compatible providers occasionally emit this common protocol synonym
# even after receiving the enum correction.  Normalize the structured value at
# the schema boundary; this does not inspect or reinterpret the player's text.
DIALOGUE_ACT_ALIASES = {
    "confirmation": "agreement",
}
ACTION_COMMITMENTS = frozenset(
    {"none", "tentative", "committed", "withdrawn", "answer"}
)
RESPONSE_EXPECTATIONS = frozenset({"none", "gm", "npc", "table"})
STATE_SCOPES = frozenset(
    {
        "none",
        "hero",
        "group",
        "world",
        "scene",
        "rules",
        "safety",
        "npc",
        "mixed",
    }
)
STATE_INTENT_SCOPES = STATE_SCOPES - {"none", "mixed"}
STATE_INTENT_OPERATIONS = frozenset(
    {
        "propose",
        "confirm",
        "contribute",
        "correct",
        "withdraw",
        "skip",
        "defer",
    }
)
STATE_INTENT_SUBJECTS = frozenset(
    set(WorldSettingCatalog.CATEGORIES)
    | {
        "world_map",
        "safety_boundary",
        "hero_profile",
        "hero_build",
        "hero_name",
        "hero_identity",
        "hero_theme",
        "hero_origin",
        "hero_class_preferences",
        "hero_classes",
        "hero_attributes",
        "hero_skills",
        "hero_skill_options",
        "hero_spells",
        "hero_bound_arcana",
        "hero_equipment",
        "hero_equipment_slots",
        "hero_bonds",
        "hero_notes",
        "hero_open_questions",
        "hero_confirmation",
        "scene_fact",
        "rule_choice",
        "npc_profile",
        "other",
    }
)

# These names are authored by the semantic model, not inferred from player
# wording in Python.  The mapping lets later layers verify that the model's
# selected hero field and its actual tool patch describe the same change.
HERO_STATE_INTENT_SUBJECT_TO_PATCH_FIELD = {
    "hero_name": "hero_name",
    "hero_identity": "identity",
    "hero_theme": "theme",
    "hero_origin": "origin",
    "hero_class_preferences": "class_preferences",
    "hero_classes": "classes",
    "hero_attributes": "attributes",
    "hero_skills": "skills",
    "hero_skill_options": "skill_options",
    "hero_spells": "spells",
    "hero_bound_arcana": "bound_arcana",
    "hero_equipment": "equipment",
    "hero_equipment_slots": "equipment_slots",
    "hero_bonds": "bonds",
    "hero_notes": "notes",
    "hero_open_questions": "open_questions",
}
_DECISION_WRAPPER_FIELDS = frozenset(
    {
        "decision",
        "message_semantics",
        "message_kind",
        "has_independent_followup",
        "audience",
        "tool_name",
        "arguments",
        "calls",
        "claims",
        "terminal_decision",
        "reply",
        "reply_parts",
        "delivery",
        "reason",
    }
)


class GMMessageSemanticsError(ValueError):
    """The core GM returned an invalid or unstable message interpretation."""

    def __init__(self, code: str, message: str, correction_hint: str) -> None:
        super().__init__(message)
        self.code = str(code or "MESSAGE_SEMANTICS_INVALID")
        self.correction_hint = str(correction_hint or "")

    def to_protocol_error(self) -> dict[str, object]:
        return {
            "protocol_error": {
                "error_code": self.code,
                "message": str(self)[:500],
                "correction_hint": self.correction_hint[:1000],
                "retryable": True,
            }
        }


@dataclass(frozen=True)
class GMMessageStateIntent:
    """One persistent-state meaning inside a possibly compound message."""

    operation: str
    scope: str
    subject: str
    summary: str
    target: str = ""
    proposal_id: str = ""

    def to_dict(self) -> dict[str, str]:
        payload = {
            "operation": self.operation,
            "scope": self.scope,
            "subject": self.subject,
            "summary": self.summary,
        }
        if self.target:
            payload["target"] = self.target
        if self.proposal_id:
            payload["proposal_id"] = self.proposal_id
        return payload


@dataclass(frozen=True)
class GMMessageSemanticEvent:
    event_id: str
    speaker: str
    relation: str
    targets: tuple[str, ...]
    dialogue_act: str
    action_commitment: str
    response_expectation: str
    state_scope: str
    state_intents: tuple[GMMessageStateIntent, ...]
    responds_to_event_id: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "event_id": self.event_id,
            "speaker": self.speaker,
            "relation": self.relation,
            "targets": list(self.targets),
            "dialogue_act": self.dialogue_act,
            "action_commitment": self.action_commitment,
            "responds_to_event_id": self.responds_to_event_id,
            "reason": self.reason,
        }
        if self.response_expectation != "none":
            payload["response_expectation"] = self.response_expectation
        # Keep version-1 fixtures and frozen transactions byte-compatible when
        # no persistent state is involved.  Non-empty scopes remain explicit
        # and therefore survive retries as part of the authority contract.
        if self.state_scope != "none":
            payload["state_scope"] = self.state_scope
        if self.state_intents:
            payload["state_intents"] = [
                intent.to_dict() for intent in self.state_intents
            ]
        return payload


@dataclass(frozen=True)
class GMMessageSemantics:
    events: tuple[GMMessageSemanticEvent, ...]
    version: str = MESSAGE_SEMANTICS_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "events": [event.to_dict() for event in self.events],
        }

    def event(self, event_id: str) -> GMMessageSemanticEvent | None:
        clean_id = str(event_id or "").strip()
        return next(
            (item for item in self.events if item.event_id == clean_id),
            None,
        )

    def source_event(
        self,
        arguments: object,
    ) -> GMMessageSemanticEvent | None:
        source_event_id = ""
        if isinstance(arguments, Mapping):
            source_event_id = str(
                arguments.get("source_event_id") or ""
            ).strip()
        if source_event_id:
            return self.event(source_event_id)
        if len(self.events) == 1:
            return self.events[0]
        return None

    def superseding_event(
        self,
        event_id: str,
    ) -> GMMessageSemanticEvent | None:
        """Return a later correction or withdrawal of one source event.

        The model decides the relationship. Python only enforces the frozen
        interpretation so an earlier action cannot execute after the same
        player has withdrawn or corrected it in the buffered turn.
        """

        clean_id = str(event_id or "").strip()
        source_index = next(
            (
                index
                for index, item in enumerate(self.events)
                if item.event_id == clean_id
            ),
            -1,
        )
        if source_index < 0:
            return None
        source = self.events[source_index]
        later_events = self.events[source_index + 1 :]
        for item in later_events:
            if item.speaker != source.speaker:
                continue
            if item.responds_to_event_id == source.event_id and (
                item.dialogue_act in {"action_withdrawal", "correction"}
                or item.action_commitment == "withdrawn"
            ):
                return item

        # Short withdrawals such as "还是算了" may not name the earlier event.
        # Apply them only to the most recent prior committed action by the same
        # speaker, never to another player's action.
        source_is_latest_committed = not any(
            item.speaker == source.speaker
            and item.action_commitment == "committed"
            for item in later_events
        )
        if source.action_commitment == "committed" and source_is_latest_committed:
            for item in later_events:
                if (
                    item.speaker == source.speaker
                    and item.action_commitment == "withdrawn"
                    and not item.responds_to_event_id
                ):
                    return item
        return None

    @classmethod
    def parse(
        cls,
        raw: object,
        *,
        source_events: Sequence[Mapping[str, object]],
    ) -> "GMMessageSemantics":
        raw = cls._unwrap_decision_wrapper(raw)
        raw = cls._strip_embedded_decision_fields(raw)
        if not isinstance(raw, Mapping):
            raise GMMessageSemanticsError(
                "MESSAGE_SEMANTICS_REQUIRED",
                "初始决策缺少message_semantics对象。",
                "逐条阅读current_turn.events，按输出协议补全version与events；不得只填写message_kind或audience。",
            )
        unknown_root = set(raw) - {"version", "events"}
        if unknown_root:
            raise GMMessageSemanticsError(
                "MESSAGE_SEMANTICS_SCHEMA_INVALID",
                "message_semantics含未声明字段："
                + "、".join(sorted(str(item) for item in unknown_root)),
                "message_semantics最外层只能填写version和events。",
            )
        version = str(raw.get("version") or "").strip()
        if version != MESSAGE_SEMANTICS_VERSION:
            raise GMMessageSemanticsError(
                "MESSAGE_SEMANTICS_VERSION_INVALID",
                f"message_semantics.version必须是{MESSAGE_SEMANTICS_VERSION}。",
                f"逐字填写version={MESSAGE_SEMANTICS_VERSION}。",
            )
        raw_items = raw.get("events")
        if not isinstance(raw_items, list):
            raise GMMessageSemanticsError(
                "MESSAGE_SEMANTICS_SCHEMA_INVALID",
                "message_semantics.events必须是JSON数组。",
                "为current_turn.events中的每条消息提供一个语义对象。",
            )

        sources = [dict(item) for item in source_events if isinstance(item, Mapping)]
        source_by_id: dict[str, dict[str, object]] = {}
        for source in sources:
            event_id = str(source.get("event_id") or "").strip()
            if not event_id:
                raise GMMessageSemanticsError(
                    "MESSAGE_SEMANTICS_SOURCE_INVALID",
                    "current_turn中存在没有event_id的消息，无法建立逐事件语义契约。",
                    "不要调用写工具；保留当前消息，等待运行时提供完整事件标识。",
                )
            if event_id in source_by_id:
                raise GMMessageSemanticsError(
                    "MESSAGE_SEMANTICS_SOURCE_INVALID",
                    f"current_turn包含重复event_id：{event_id}。",
                    "不要自行合并或重命名事件；等待运行时修复当前桌面轮次。",
                )
            source_by_id[event_id] = source

        parsed: list[GMMessageSemanticEvent] = []
        seen_ids: set[str] = set()
        for index, raw_item in enumerate(raw_items, start=1):
            if not isinstance(raw_item, Mapping):
                raise GMMessageSemanticsError(
                    "MESSAGE_SEMANTICS_SCHEMA_INVALID",
                    f"message_semantics.events[{index}]必须是JSON对象。",
                    "按逐事件语义Schema重新填写。",
                )
            allowed = {
                "event_id",
                "speaker",
                "relation",
                "targets",
                "dialogue_act",
                "action_commitment",
                "response_expectation",
                "state_scope",
                "state_intents",
                "responds_to_event_id",
                "reason",
            }
            redundant_source_fields = {"speaker_id", "text"}
            unknown = set(raw_item) - allowed - redundant_source_fields
            if unknown:
                raise GMMessageSemanticsError(
                    "MESSAGE_SEMANTICS_SCHEMA_INVALID",
                    f"message_semantics.events[{index}]含未声明字段："
                    + "、".join(sorted(str(item) for item in unknown)),
                    "删除未声明字段，按输出协议重新提交。",
                )
            event_id = str(raw_item.get("event_id") or "").strip()
            source = source_by_id.get(event_id)
            if source is None:
                raise GMMessageSemanticsError(
                    "MESSAGE_SEMANTICS_EVENT_INVALID",
                    f"语义对象引用了不属于current_turn的event_id：{event_id or '（空）'}。",
                    "event_id必须逐字复制current_turn.events中的值。",
                )
            if event_id in seen_ids:
                raise GMMessageSemanticsError(
                    "MESSAGE_SEMANTICS_EVENT_DUPLICATED",
                    f"同一个event_id被解释了多次：{event_id}。",
                    "每条current_turn事件恰好提供一个语义对象。",
                )
            seen_ids.add(event_id)

            speaker = str(raw_item.get("speaker") or "").strip()
            expected_speaker = str(source.get("speaker") or "").strip()
            if speaker != expected_speaker:
                raise GMMessageSemanticsError(
                    "MESSAGE_SEMANTICS_SPEAKER_MISMATCH",
                    f"事件{event_id}的speaker应为{expected_speaker or '（空）'}，不能改成{speaker or '（空）'}。",
                    "speaker必须逐字复制对应current_turn事件，不能把行动归给别的玩家或角色。",
                )
            for redundant_key in redundant_source_fields & set(raw_item):
                repeated_value = str(raw_item.get(redundant_key) or "")
                expected_value = str(source.get(redundant_key) or "")
                if repeated_value != expected_value:
                    raise GMMessageSemanticsError(
                        "MESSAGE_SEMANTICS_SOURCE_MISMATCH",
                        f"事件{event_id}重复填写的{redundant_key}与current_turn原值不一致。",
                        f"删除重复的{redundant_key}字段；若保留，必须逐字复制current_turn事件，不能改写玩家原话或身份。",
                    )

            relation = str(raw_item.get("relation") or "").strip().lower()
            if relation not in MESSAGE_RELATIONS:
                raise GMMessageSemanticsError(
                    "MESSAGE_SEMANTICS_ENUM_INVALID",
                    f"事件{event_id}的relation不合法：{relation or '（空）'}。",
                    "relation只能是gm、player、table、npc、mixed或unclear。",
                )
            dialogue_act = str(
                raw_item.get("dialogue_act") or ""
            ).strip().lower()
            dialogue_act = DIALOGUE_ACT_ALIASES.get(dialogue_act, dialogue_act)
            if dialogue_act not in DIALOGUE_ACTS:
                raise GMMessageSemanticsError(
                    "MESSAGE_SEMANTICS_ENUM_INVALID",
                    f"事件{event_id}的dialogue_act不合法：{dialogue_act or '（空）'}。",
                    "从输出协议声明的dialogue_act枚举中重新选择；撤回行动必须逐字填写action_withdrawal，不能填写withdrawal。最外层decision、message_kind、audience等字段仍留在message_semantics之外。",
                )
            action_commitment = str(
                raw_item.get("action_commitment") or ""
            ).strip().lower()
            if action_commitment not in ACTION_COMMITMENTS:
                raise GMMessageSemanticsError(
                    "MESSAGE_SEMANTICS_ENUM_INVALID",
                    f"事件{event_id}的action_commitment不合法：{action_commitment or '（空）'}。",
                    "action_commitment只能是none、tentative、committed、withdrawn或answer。",
                )
            response_expectation = str(
                raw_item.get("response_expectation") or "none"
            ).strip().lower()
            if response_expectation not in RESPONSE_EXPECTATIONS:
                raise GMMessageSemanticsError(
                    "MESSAGE_SEMANTICS_ENUM_INVALID",
                    (
                        f"事件{event_id}的response_expectation不合法："
                        f"{response_expectation or '（空）'}。"
                    ),
                    "response_expectation只能是none、gm、npc或table。",
                )
            state_scope = str(raw_item.get("state_scope") or "none").strip().lower()
            if state_scope not in STATE_SCOPES:
                raise GMMessageSemanticsError(
                    "MESSAGE_SEMANTICS_ENUM_INVALID",
                    f"事件{event_id}的state_scope不合法：{state_scope or '（空）'}。",
                    "state_scope只能是none、hero、group、world、scene、rules、safety、npc或mixed。",
                )

            raw_state_intents = raw_item.get("state_intents", [])
            if not isinstance(raw_state_intents, list):
                raise GMMessageSemanticsError(
                    "MESSAGE_SEMANTICS_SCHEMA_INVALID",
                    f"事件{event_id}的state_intents必须是JSON数组。",
                    "没有持久状态含义时填写空数组；有多个含义时逐项填写。",
                )
            if len(raw_state_intents) > 8:
                raise GMMessageSemanticsError(
                    "MESSAGE_SEMANTICS_SCHEMA_INVALID",
                    f"事件{event_id}的state_intents最多允许8项。",
                    "只保留本句真正提出、确认、贡献、纠正或撤回的持久状态含义。",
                )
            state_intents: list[GMMessageStateIntent] = []
            seen_state_intents: set[
                tuple[str, str, str, str, str, str]
            ] = set()
            for intent_index, raw_intent in enumerate(raw_state_intents, start=1):
                if not isinstance(raw_intent, Mapping):
                    raise GMMessageSemanticsError(
                        "MESSAGE_SEMANTICS_SCHEMA_INVALID",
                        f"事件{event_id}的state_intents[{intent_index}]必须是JSON对象。",
                        "按operation、scope、subject、summary四个字段重新填写。",
                    )
                unknown_intent_fields = set(raw_intent) - {
                    "operation",
                    "scope",
                    "subject",
                    "summary",
                    "target",
                    "proposal_id",
                }
                if unknown_intent_fields:
                    raise GMMessageSemanticsError(
                        "MESSAGE_SEMANTICS_SCHEMA_INVALID",
                        f"事件{event_id}的state_intents[{intent_index}]含未声明字段："
                        + "、".join(sorted(str(item) for item in unknown_intent_fields)),
                        "每项只能填写operation、scope、subject、target、proposal_id和summary。",
                    )
                operation = str(raw_intent.get("operation") or "").strip().lower()
                intent_scope = str(raw_intent.get("scope") or "").strip().lower()
                subject = str(raw_intent.get("subject") or "").strip().lower()
                summary = " ".join(
                    str(raw_intent.get("summary") or "").split()
                ).strip()
                target = " ".join(
                    str(raw_intent.get("target") or "").split()
                ).strip()
                proposal_id = str(
                    raw_intent.get("proposal_id") or ""
                ).strip()
                if operation not in STATE_INTENT_OPERATIONS:
                    raise GMMessageSemanticsError(
                        "MESSAGE_SEMANTICS_ENUM_INVALID",
                        f"事件{event_id}的state_intents[{intent_index}].operation不合法。",
                        (
                            "operation只能是propose、confirm、contribute、correct、"
                            "withdraw、skip或defer。"
                        ),
                    )
                if intent_scope not in STATE_INTENT_SCOPES:
                    raise GMMessageSemanticsError(
                        "MESSAGE_SEMANTICS_ENUM_INVALID",
                        f"事件{event_id}的state_intents[{intent_index}].scope不合法。",
                        "scope只能是hero、group、world、scene、rules、safety或npc。",
                    )
                if subject not in STATE_INTENT_SUBJECTS:
                    raise GMMessageSemanticsError(
                        "MESSAGE_SEMANTICS_ENUM_INVALID",
                        f"事件{event_id}的state_intents[{intent_index}].subject不合法：{subject or '（空）'}。",
                        "从输出协议声明的state_intents.subject枚举中选择最贴近的一项。",
                    )
                if intent_scope == "hero" and operation == "confirm":
                    if subject != "hero_confirmation":
                        raise GMMessageSemanticsError(
                            "MESSAGE_HERO_FIELD_CONFIRMATION_INVALID",
                            (
                                f"事件{event_id}把角色字段{subject}标成了整卡确认。"
                            ),
                            (
                                "confirm/hero只用于玩家明确确认整张角色卡，且subject必须是"
                                "hero_confirmation。赞同另一名玩家已经写入的角色字段不产生"
                                "持久意图；玩家为自己的角色明确给出字段值时，使用contribute，"
                                "明确修改既有字段时使用correct。"
                            ),
                        )
                if subject == "hero_confirmation" and (
                    intent_scope != "hero" or operation != "confirm"
                ):
                    raise GMMessageSemanticsError(
                        "MESSAGE_HERO_CONFIRMATION_OPERATION_INVALID",
                        f"事件{event_id}错误使用了hero_confirmation。",
                        (
                            "hero_confirmation只用于confirm/hero，表示所属玩家明确将整张"
                            "角色草稿定稿；单个字段仍使用对应hero_* subject。"
                        ),
                    )
                if not summary:
                    raise GMMessageSemanticsError(
                        "MESSAGE_SEMANTICS_REASON_REQUIRED",
                        f"事件{event_id}的state_intents[{intent_index}]缺少summary。",
                        "用一句短语概括这一项具体确认、提出或修改的内容。",
                    )
                if proposal_id and operation != "confirm":
                    raise GMMessageSemanticsError(
                        "MESSAGE_SEMANTICS_SCHEMA_INVALID",
                        f"事件{event_id}的state_intents[{intent_index}]只有confirm可以填写proposal_id。",
                        "非确认意图删除proposal_id；确认待定提案时逐字复制权威pending_proposals中的id。",
                    )
                identity = (
                    operation,
                    intent_scope,
                    subject,
                    target,
                    proposal_id,
                    summary,
                )
                if identity in seen_state_intents:
                    continue
                seen_state_intents.add(identity)
                state_intents.append(
                    GMMessageStateIntent(
                        operation=operation,
                        scope=intent_scope,
                        subject=subject,
                        summary=summary[:240],
                        target=target[:120],
                        proposal_id=proposal_id[:120],
                    )
                )

            if state_scope != "none" and not state_intents:
                raise GMMessageSemanticsError(
                    "MESSAGE_STATE_INTENTS_REQUIRED",
                    f"事件{event_id}声明了state_scope={state_scope}，但没有逐项填写state_intents。",
                    "把本句每个持久含义分别写入state_intents；确认旧设定与提出新设定必须拆成两项。",
                )
            if state_scope == "none" and state_intents:
                raise GMMessageSemanticsError(
                    "MESSAGE_STATE_SCOPE_MISMATCH",
                    f"事件{event_id}含state_intents，但state_scope仍为none。",
                    "只有一个scope时将state_scope改为该值；涉及多个scope时填写mixed。",
                )
            if state_intents:
                intent_scopes = {intent.scope for intent in state_intents}
                expected_scope = (
                    next(iter(intent_scopes))
                    if len(intent_scopes) == 1
                    else "mixed"
                )
                if state_scope != expected_scope:
                    raise GMMessageSemanticsError(
                        "MESSAGE_STATE_SCOPE_MISMATCH",
                        f"事件{event_id}的state_scope应为{expected_scope}，当前为{state_scope}。",
                        "state_scope必须概括state_intents：单一范围填该范围，多个范围填mixed。",
                    )

            raw_targets = raw_item.get("targets")
            if not isinstance(raw_targets, list):
                raise GMMessageSemanticsError(
                    "MESSAGE_SEMANTICS_SCHEMA_INVALID",
                    f"事件{event_id}的targets必须是JSON数组。",
                    "没有明确对象时填写空数组。",
                )
            targets: list[str] = []
            for raw_target in raw_targets:
                target = str(raw_target or "").strip()
                if target and target not in targets:
                    targets.append(target[:120])
            if len(targets) > 8:
                raise GMMessageSemanticsError(
                    "MESSAGE_SEMANTICS_SCHEMA_INVALID",
                    f"事件{event_id}的targets最多允许8项。",
                    "只保留当前话语真正指向的对象。",
                )

            responds_to_event_id = str(
                raw_item.get("responds_to_event_id") or ""
            ).strip()
            if responds_to_event_id:
                if responds_to_event_id not in source_by_id:
                    raise GMMessageSemanticsError(
                        "MESSAGE_SEMANTICS_RESPONSE_EVENT_INVALID",
                        f"事件{event_id}引用的responds_to_event_id不属于current_turn。",
                        "只有回应同一current_turn中的另一条消息时才填写其event_id；回应更早消息时留空。",
                    )
                if responds_to_event_id == event_id:
                    raise GMMessageSemanticsError(
                        "MESSAGE_SEMANTICS_RESPONSE_EVENT_INVALID",
                        f"事件{event_id}不能回应自身。",
                        "填写真正被回应的另一条current_turn事件，或留空。",
                    )

            reason = " ".join(str(raw_item.get("reason") or "").split()).strip()
            if not reason:
                raise GMMessageSemanticsError(
                    "MESSAGE_SEMANTICS_REASON_REQUIRED",
                    f"事件{event_id}缺少简短语义依据。",
                    "用一句短语说明称呼、引用、相邻问答或行动措辞如何支持该判断。",
                )
            parsed.append(
                GMMessageSemanticEvent(
                    event_id=event_id,
                    speaker=speaker,
                    relation=relation,
                    targets=tuple(targets),
                    dialogue_act=dialogue_act,
                    action_commitment=action_commitment,
                    response_expectation=response_expectation,
                    state_scope=state_scope,
                    state_intents=tuple(state_intents),
                    responds_to_event_id=responds_to_event_id,
                    reason=reason[:300],
                )
            )

        missing = [event_id for event_id in source_by_id if event_id not in seen_ids]
        if missing:
            raise GMMessageSemanticsError(
                "MESSAGE_SEMANTICS_EVENT_MISSING",
                "message_semantics遗漏current_turn事件：" + "、".join(missing),
                "多人缓冲轮次中的每条消息都要独立判断，不能只解释最后一名发言者。",
            )
        ordered = tuple(parsed)
        if tuple(item.event_id for item in ordered) != tuple(source_by_id):
            raise GMMessageSemanticsError(
                "MESSAGE_SEMANTICS_EVENT_ORDER_INVALID",
                "message_semantics.events没有保持current_turn的原始顺序。",
                "按current_turn.events顺序逐条输出，不要重排玩家消息。",
            )
        return cls(events=ordered)

    @staticmethod
    def _unwrap_decision_wrapper(raw: object) -> object:
        """Recover one harmless JSON nesting error without changing meaning.

        Some providers occasionally place the complete decision object inside
        its own ``message_semantics`` field during a protocol retry.  Unwrapping
        that exact structural shell avoids another model call; the inner
        semantics still undergo every strict field, enum and event check.
        """

        current = raw
        for _depth in range(2):
            if not isinstance(current, Mapping):
                break
            nested = current.get("message_semantics")
            if not isinstance(nested, Mapping):
                break
            if not set(current).issubset(_DECISION_WRAPPER_FIELDS):
                break
            current = nested
        return current

    @staticmethod
    def _strip_embedded_decision_fields(raw: object) -> object:
        """Ignore known outer fields accidentally nested beside version/events."""

        if not isinstance(raw, Mapping):
            return raw
        if "version" not in raw or "events" not in raw:
            return raw
        extras = set(raw) - {"version", "events"}
        allowed_extras = _DECISION_WRAPPER_FIELDS - {"message_semantics"}
        if extras and extras.issubset(allowed_extras):
            return {
                "version": raw.get("version"),
                "events": raw.get("events"),
            }
        return raw


PLAYER_ACTION_TOOLS = frozenset(
    {
        "declare_check_action",
        "declare_movement_check",
        "perform_check_action",
        "perform_character_action",
        "perform_scene_action",
        "perform_in_scene_action",
        "commit_story_item_action",
        "move_group_within_scene",
        "move_scene_group",
        "perform_ritual_project_action",
        "pass_in_scene_action",
        "start_conflict",
    }
)
NPC_RESPONSE_TOOLS = frozenset(
    {"decide_npc_response", "decide_collective_response"}
)
HERO_STATE_TOOLS = frozenset({"update_hero_draft", "confirm_hero_draft"})
SESSION_ZERO_SEMANTIC_TOOLS = frozenset(
    {
        "propose_session_zero_update",
        "confirm_session_zero_proposal",
        "create_world_setting",
        "update_world_setting",
        "delete_world_setting",
        "rename_world_setting",
        "record_safety_boundary",
        "mark_session_zero_topic_complete",
        "pause_session_zero_nudges",
    }
)

SESSION_ZERO_CONTRIBUTION_TOPIC_BY_SUBJECT = {
    "kingdoms": "kingdom",
    "historical_events": "historical_event",
    "mysteries": "mystery",
    "world_threats": "threat",
}
SESSION_ZERO_COMPLETION_TOPIC_BY_SUBJECT = {
    **SESSION_ZERO_CONTRIBUTION_TOPIC_BY_SUBJECT,
    "safety_boundary": "safety",
}


def semantic_change_tool_names(
    semantics: GMMessageSemantics,
) -> frozenset[str]:
    """Return the narrow tool family implied by frozen model semantics.

    This function does not interpret player prose.  It only translates the
    model-authored, validated state-intent protocol into capability names so a
    retry can see the right schemas without retaining every Session Zero CRUD
    schema.  Python remains responsible for permissions and argument validity.
    """

    names: set[str] = set()
    for event in semantics.events:
        for intent in event.state_intents:
            if intent.subject == "safety_boundary":
                if intent.operation == "skip":
                    names.add("mark_session_zero_topic_complete")
                elif intent.operation == "defer":
                    names.add("pause_session_zero_nudges")
                else:
                    names.add("record_safety_boundary")
                continue
            if intent.operation == "skip" and intent.subject in (
                SESSION_ZERO_CONTRIBUTION_TOPIC_BY_SUBJECT
            ):
                names.add("mark_session_zero_topic_complete")
                continue
            if intent.operation == "defer" and intent.subject in (
                SESSION_ZERO_CONTRIBUTION_TOPIC_BY_SUBJECT
            ):
                names.add("pause_session_zero_nudges")
                continue
            if intent.scope == "hero":
                if intent.operation in {"contribute", "correct"}:
                    names.update({"get_hero_drafts", "update_hero_draft"})
                elif (
                    intent.operation == "confirm"
                    and intent.subject == "hero_confirmation"
                ):
                    names.update({"get_hero_drafts", "confirm_hero_draft"})
                # A tentative hero idea stays in table conversation until its
                # owner commits it; there is no authoritative draft proposal.
                continue
            if intent.scope == "scene" and intent.operation in {"propose", "confirm"}:
                names.add(
                    "propose_session_zero_update"
                    if intent.operation == "propose"
                    else "confirm_session_zero_proposal"
                )
                continue
            if intent.scope not in {"group", "world"}:
                continue
            if intent.operation == "propose":
                names.update(
                    {"propose_session_zero_update", "query_world_settings"}
                )
            elif intent.operation == "confirm":
                names.update(
                    {
                        "confirm_session_zero_proposal",
                        "query_world_settings",
                    }
                )
            elif intent.operation == "contribute":
                names.update(
                    {
                        "create_world_setting",
                        "update_world_setting",
                        "query_world_settings",
                    }
                )
            elif intent.operation == "correct":
                names.update(
                    {
                        "update_world_setting",
                        "rename_world_setting",
                        "query_world_settings",
                    }
                )
            elif intent.operation == "withdraw":
                names.update({"delete_world_setting", "query_world_settings"})
    return frozenset(names)


def _session_zero_tool_intent_error(
    *,
    tool_name: str,
    source: GMMessageSemanticEvent,
    arguments: object,
) -> GMMessageSemanticsError | None:
    """Keep Session Zero writes aligned with the frozen semantic plan."""

    intents = tuple(source.state_intents)
    if not intents:
        return GMMessageSemanticsError(
            "MESSAGE_STATE_INTENT_REQUIRED",
            f"事件{source.event_id}没有持久状态计划，不能调用{tool_name}。",
            (
                "保持玩家原话不变，重新判断该消息是否真的会改变世界、角色或安全状态；"
                "会改变时补全state_intents，不改变时选择silent、final或普通回应。"
            ),
        )

    relevant: tuple[GMMessageStateIntent, ...] = ()

    if tool_name == "record_safety_boundary":
        relevant = tuple(
            intent for intent in intents if intent.subject == "safety_boundary"
        )
    elif tool_name == "propose_session_zero_update":
        relevant = tuple(
            intent for intent in intents
            if
            intent.operation == "propose"
            and intent.scope in {"group", "world", "scene"}
        )
    elif tool_name == "confirm_session_zero_proposal":
        relevant = tuple(
            intent for intent in intents
            if
            intent.operation == "confirm"
            and intent.scope in {"group", "world", "scene"}
        )
    elif tool_name == "mark_session_zero_topic_complete":
        relevant = tuple(
            intent
            for intent in intents
            if intent.operation == "skip"
            and (
                (
                    intent.scope == "world"
                    and intent.subject in SESSION_ZERO_CONTRIBUTION_TOPIC_BY_SUBJECT
                )
                or (
                    intent.scope == "safety"
                    and intent.subject == "safety_boundary"
                )
            )
        )
    elif tool_name == "pause_session_zero_nudges":
        relevant = tuple(
            intent
            for intent in intents
            if intent.operation == "defer"
            and (
                (
                    intent.scope == "world"
                    and intent.subject in SESSION_ZERO_CONTRIBUTION_TOPIC_BY_SUBJECT
                )
                or (
                    intent.scope == "safety"
                    and intent.subject == "safety_boundary"
                )
            )
        )
    elif tool_name == "update_hero_draft":
        relevant = tuple(
            intent for intent in intents
            if
            intent.scope == "hero"
            and intent.operation in {"contribute", "correct"}
        )
    elif tool_name == "confirm_hero_draft":
        relevant = tuple(
            intent for intent in intents
            if
            intent.scope == "hero"
            and intent.operation == "confirm"
            and intent.subject == "hero_confirmation"
        )
    elif tool_name == "create_world_setting":
        relevant = tuple(
            intent for intent in intents
            if
            intent.scope in {"group", "world"}
            and intent.operation == "contribute"
        )
    elif tool_name == "update_world_setting":
        relevant = tuple(
            intent for intent in intents
            if
            intent.scope in {"group", "world"}
            and intent.operation in {"contribute", "correct"}
        )
    elif tool_name in {"delete_world_setting", "rename_world_setting"}:
        relevant = tuple(
            intent for intent in intents
            if
            intent.scope in {"group", "world"}
            and intent.operation in {"correct", "withdraw"}
        )
    else:
        return None
    if relevant:
        if tool_name == "update_hero_draft" and isinstance(arguments, Mapping):
            planned_fields = {
                field_name
                for intent in relevant
                if (
                    field_name := HERO_STATE_INTENT_SUBJECT_TO_PATCH_FIELD.get(
                        intent.subject
                    )
                )
            }
            patch = arguments.get("patch")
            actual_fields = set(patch) if isinstance(patch, Mapping) else set()
            missing_fields = sorted(planned_fields - actual_fields)
            if missing_fields:
                return GMMessageSemanticsError(
                    "MESSAGE_HERO_FIELD_TOOL_MISMATCH",
                    (
                        f"事件{source.event_id}计划填写角色字段"
                        f"{sorted(planned_fields)}，工具patch却只包含"
                        f"{sorted(actual_fields)}。"
                    ),
                    (
                        "保持已冻结语义不变，把玩家已经明确回答的内容写入patch中的"
                        + "、".join(missing_fields)
                        + "；可以同时保留notes细节，但不能只写notes代替必填字段。"
                    ),
                )
        if (
            tool_name == "confirm_session_zero_proposal"
            and isinstance(arguments, Mapping)
        ):
            planned_proposal_ids = {
                intent.proposal_id
                for intent in relevant
                if intent.proposal_id
            }
            actual_proposal_id = str(
                arguments.get("proposal_id") or ""
            ).strip()
            if (
                planned_proposal_ids
                and actual_proposal_id
                and actual_proposal_id not in planned_proposal_ids
            ):
                return GMMessageSemanticsError(
                    "MESSAGE_CONFIRM_PROPOSAL_TOOL_MISMATCH",
                    (
                        f"事件{source.event_id}确认的是{sorted(planned_proposal_ids)}，"
                        f"工具却指向{actual_proposal_id}。"
                    ),
                    "保持已冻结语义不变，使用state_intents.proposal_id中的权威提案ID。",
                )
        if (
            tool_name == "mark_session_zero_topic_complete"
            and isinstance(arguments, Mapping)
        ):
            planned_topics = {
                SESSION_ZERO_COMPLETION_TOPIC_BY_SUBJECT[intent.subject]
                for intent in relevant
            }
            actual_topic = str(arguments.get("topic") or "").strip()
            if actual_topic and actual_topic not in planned_topics:
                return GMMessageSemanticsError(
                    "MESSAGE_SKIP_TOPIC_TOOL_MISMATCH",
                    (
                        f"事件{source.event_id}跳过的是{sorted(planned_topics)}，"
                        f"工具却提交了{actual_topic}。"
                    ),
                    "保持已冻结语义不变，把topic改为语义计划对应的贡献主题。",
                )
        if tool_name in {
            "create_world_setting",
            "update_world_setting",
            "delete_world_setting",
            "rename_world_setting",
        } and isinstance(arguments, Mapping):
            planned_categories = {
                intent.subject
                for intent in relevant
                if intent.subject in WorldSettingCatalog.CATEGORIES
            }
            actual_category = str(arguments.get("category") or "").strip()
            if (
                planned_categories
                and actual_category
                and actual_category not in planned_categories
            ):
                return GMMessageSemanticsError(
                    "MESSAGE_STATE_CATEGORY_TOOL_MISMATCH",
                    (
                        f"事件{source.event_id}计划修改类别"
                        f"{sorted(planned_categories)}，工具却提交了{actual_category}。"
                    ),
                    "保持已冻结语义不变，把工具category改为语义计划中的类别。",
                )
            planned_targets = {
                intent.target for intent in relevant if intent.target
            }
            # List records are keyed by their complete committed sentence,
            # while semantic ``target`` deliberately carries a human-sized
            # label such as “大迁徙”. For a create, the intent summary is the
            # complete new fact and can still be checked. For an update or
            # delete, however, ``name`` is the *old* complete sentence while
            # the summary describes the new state. Requiring either human
            # label or new summary to equal that storage key traps a correct
            # list update forever. The CRUD tool still requires the exact old
            # record to exist, and proposal grounding still binds the chosen
            # record to the player's source event, so skip only this
            # impossible byte-equality check for existing list records.
            is_list_record = actual_category in (
                WorldSettingCatalog.PUBLIC_LISTS
                | WorldSettingCatalog.PRIVATE_LISTS
            )
            if is_list_record and tool_name == "create_world_setting":
                planned_targets.update(
                    intent.summary for intent in relevant if intent.summary
                )
            actual_target = str(
                arguments.get("old_name")
                if tool_name == "rename_world_setting"
                else arguments.get("name")
                or ""
            ).strip()
            if (
                planned_targets
                and actual_target
                and actual_target not in planned_targets
                and not (
                    is_list_record
                    and tool_name
                    in {
                        "update_world_setting",
                        "delete_world_setting",
                        "rename_world_setting",
                    }
                )
            ):
                return GMMessageSemanticsError(
                    "MESSAGE_STATE_TARGET_TOOL_MISMATCH",
                    (
                        f"事件{source.event_id}计划修改{sorted(planned_targets)}，"
                        f"工具却指向{actual_target}。"
                    ),
                    "保持已冻结语义不变，先查询并使用state_intents.target指定的准确对象。",
                )
        return None

    planned = sorted(semantic_change_tool_names(GMMessageSemantics(events=(source,))))
    targets = [intent.target for intent in intents if intent.target]
    target_hint = f"；语义对象为：{'、'.join(targets)}" if targets else ""
    if not planned:
        correction = (
            "这句话只是在讨论尚未确认的角色候选，不写入权威状态；"
            "保持已冻结语义并选择silent或自然回应。"
        )
    else:
        correction = (
            "保持已冻结语义不变，改用与语义计划一致的工具："
            + "、".join(planned)
            + target_hint
            + "。"
        )
    return GMMessageSemanticsError(
        "MESSAGE_STATE_INTENT_TOOL_MISMATCH",
        f"事件{source.event_id}的持久状态计划不允许调用{tool_name}。",
        correction,
    )


def tool_semantic_authority_error(
    *,
    tool_name: str,
    arguments: object,
    semantics: GMMessageSemantics,
) -> GMMessageSemanticsError | None:
    """Check semantic authority without reinterpreting any natural-language text."""

    clean_name = str(tool_name or "").strip()
    if clean_name not in (
        PLAYER_ACTION_TOOLS
        | NPC_RESPONSE_TOOLS
        | HERO_STATE_TOOLS
        | SESSION_ZERO_SEMANTIC_TOOLS
        | {"resolve_rule_window"}
    ):
        return None
    source = semantics.source_event(arguments)
    if source is None:
        return GMMessageSemanticsError(
            "MESSAGE_SEMANTICS_SOURCE_REQUIRED",
            f"工具{clean_name}无法绑定到唯一的current_turn语义事件。",
            "多人轮次中的写调用必须填写对应source_event_id；不要用一名玩家的话替另一名玩家行动。",
        )
    if clean_name in HERO_STATE_TOOLS:
        has_hero_intent = any(
            intent.scope == "hero" for intent in source.state_intents
        )
        if source.state_scope not in {"none", "hero"} and not (
            source.state_scope == "mixed" and has_hero_intent
        ):
            correction = (
                "这是全队共同来历、同行理由、共同使命或小队关系，"
                "保持已冻结的message_semantics不变，改用"
                "propose_session_zero_update的group_concept保存为待确认小队提案；"
                "不要写入任何单个角色的notes。"
                if source.state_scope == "group"
                else (
                    "保持已冻结的message_semantics不变；选择与state_scope匹配的"
                    "世界、场景、规则或NPC工具，不能写入个人角色草稿。"
                )
            )
            return GMMessageSemanticsError(
                "MESSAGE_STATE_SCOPE_TOOL_MISMATCH",
                (
                    f"事件{source.event_id}的事实范围是{source.state_scope}，"
                    f"不能调用{clean_name}写入个人角色草稿。"
                ),
                correction,
            )
        return _session_zero_tool_intent_error(
            tool_name=clean_name,
            source=source,
            arguments=arguments,
        )
    if clean_name in SESSION_ZERO_SEMANTIC_TOOLS:
        return _session_zero_tool_intent_error(
            tool_name=clean_name,
            source=source,
            arguments=arguments,
        )
    if clean_name == "resolve_rule_window":
        if (
            source.relation not in {"gm", "mixed"}
            or source.dialogue_act not in {"answer", "correction"}
            or source.action_commitment != "answer"
        ):
            return GMMessageSemanticsError(
                "RULE_WINDOW_NOT_ANSWERED_BY_SOURCE_MESSAGE",
                f"事件{source.event_id}不是对GM待处理事项的明确回答，不能调用resolve_rule_window。",
                "保持已冻结的message_semantics不变；按这句话真实受众处理。玩家间同意、讨论或暂定方案应保持silent，不得结算先前行动。",
            )
        return None
    if clean_name in PLAYER_ACTION_TOOLS:
        superseding = semantics.superseding_event(source.event_id)
        if superseding is not None:
            return GMMessageSemanticsError(
                "PLAYER_ACTION_SUPERSEDED",
                f"事件{source.event_id}已被同一玩家后续事件{superseding.event_id}撤回或纠正，不能调用{clean_name}。",
                "保持已冻结的message_semantics不变；不要执行已撤回的旧行动。若玩家改成另一项正式行动，只能使用那条新行动对应的source_event_id。",
            )
        if source.action_commitment != "committed":
            return GMMessageSemanticsError(
                "PLAYER_ACTION_NOT_COMMITTED",
                f"事件{source.event_id}没有提交正式角色行动，不能调用{clean_name}。",
                "保持已冻结的message_semantics不变；action_commitment是行动是否已经落实的唯一语义权威。none、tentative、withdrawn或answer都不执行角色行动。",
            )
        return None
    npc_response_requested = source.relation in {"npc", "mixed"} and (
        source.response_expectation == "npc"
        or source.dialogue_act in {"question", "request"}
        or (
            source.dialogue_act == "answer"
            and source.action_commitment in {"committed", "answer"}
        )
        or (
            source.action_commitment == "committed"
            and source.dialogue_act
            in {
                "roleplay_speech",
                "action_declaration",
                "state_contribution",
                "correction",
            }
        )
    )
    if not npc_response_requested:
        return GMMessageSemanticsError(
            "NPC_RESPONSE_NOT_REQUESTED_BY_SOURCE_MESSAGE",
            f"事件{source.event_id}没有提交需要NPC或集体产生新回应的互动，不能调用{clean_name}。",
            "玩家只是在与队友讨论，或对NPC作没有新增命题的鼓励、附和、感谢和重复指令时保持silent；只有已经向NPC提出问题、要求、方案或会改变其判断的新信息时，才让NPC回应。",
        )
    return None
