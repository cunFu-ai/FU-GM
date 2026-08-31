from __future__ import annotations

from typing import Any


class NPCSpeechPlanValidationError(ValueError):
    """A public NPC plan is readable but needs a specific structural repair."""

    def __init__(self, message: str, *, correction_hint: str = "") -> None:
        super().__init__(message)
        self.correction_hint = str(correction_hint or "").strip()


PUBLIC_SEGMENT_TAGS = frozenset(
    {
        "direct_answer",
        "fact",
        "gate_requirement",
        "gate_payoff",
        "nonverbal",
        "settled_terms",
        "deferred_action",
        "deferred_result",
        "deferred_trigger",
        "player_request",
    }
)
PUBLIC_SEGMENT_TAG_ALIASES = {
    "new_gate": "gate_requirement",
}
NPC_FACT_EFFECT_KINDS = frozenset({"objective", "claim", "rumor", "lie"})
PUBLIC_SEGMENT_INPUT_TAGS = frozenset(
    {
        *PUBLIC_SEGMENT_TAGS,
        *PUBLIC_SEGMENT_TAG_ALIASES,
        *NPC_FACT_EFFECT_KINDS,
    }
)

NPC_FACT_EFFECT_SCOPES = frozenset({"scene", "local"})


def normalize_public_segments(value: Any) -> list[dict[str, Any]]:
    """Parse one complete NPC public response or reject the whole transaction."""

    if not isinstance(value, list):
        raise ValueError("public_segments must be an array")
    if not value:
        raise ValueError("public_segments must not be empty")
    if len(value) > 12:
        raise ValueError("public_segments may contain at most 12 segments")
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(value):
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            raise ValueError(
                f"public_segments[{index}] must be a string or object"
            )
        segment_id = clean_text(item.get("id")) or f"segment_{index + 1:02d}"
        text = clean_text(item.get("text"))
        raw_tags = item.get("tags", [])
        if segment_id in seen_ids:
            segment_id = f"segment_{index + 1:02d}"
            if segment_id in seen_ids:
                raise ValueError(f"duplicate public segment id: {segment_id}")
        if not text:
            raise ValueError(f"public_segments[{index}].text is required")
        if raw_tags is None:
            raw_tags = []
        if not isinstance(raw_tags, list):
            raise ValueError(f"public_segments[{index}].tags must be an array")
        tags: list[str] = []
        for raw_tag in raw_tags:
            input_tag = clean_text(raw_tag)
            if input_tag not in PUBLIC_SEGMENT_INPUT_TAGS:
                continue
            # Fact truth status belongs exclusively to fact_effects.  Providers
            # sometimes repeat that classification as a presentation tag; drop
            # the duplicate instead of asking the model to regenerate identical
            # prose.  In particular, claim/rumor/lie must never become "fact".
            if input_tag in NPC_FACT_EFFECT_KINDS:
                continue
            tag = PUBLIC_SEGMENT_TAG_ALIASES.get(input_tag, input_tag)
            if tag in tags:
                continue
            tags.append(tag)
        seen_ids.add(segment_id)
        result.append({"id": segment_id, "text": text, "tags": tags})
    return result


def render_public_segments(segments: list[dict[str, Any]]) -> str:
    rendered = ""
    for segment in segments:
        text = clean_text(segment.get("text"))
        if not text:
            continue
        # Tags must remain split for state derivation, but consecutive lines
        # spoken by the same NPC should still read as one natural utterance.
        if rendered.endswith(("”", "’")) and text.startswith(("“", "‘")):
            rendered = rendered[:-1] + text[1:]
        else:
            rendered += text
    return rendered.strip()


def normalize_speech_plan(
    raw: dict[str, Any] | None,
    *,
    public_segments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive every public state field from the text that players will see."""

    data = dict(raw or {})
    model_speech_act = clean_text(data.get("speech_act")) or "answer"
    if model_speech_act not in {
        "answer",
        "refuse",
        "new_gate",
        "admit_unknown",
        "deflect",
    }:
        model_speech_act = "answer"
    speech_act = "condition" if model_speech_act == "new_gate" else model_speech_act
    condition_outcome = clean_text(data.get("condition_outcome")) or "none"
    if condition_outcome not in {"none", "fulfilled", "incomplete", "rejected"}:
        condition_outcome = "none"
    promise_kind = clean_text(data.get("promise_kind")) or "none"
    if promise_kind not in {
        "none",
        "access",
        "escort",
        "disclose",
        "item",
        "aid",
        "other",
    }:
        promise_kind = "other"
    proposal_outcome = clean_text(data.get("proposal_outcome")) or "none"
    if proposal_outcome not in {"none", "accepted", "rejected", "countered"}:
        proposal_outcome = "none"
    commitment_outcome = clean_text(data.get("commitment_outcome")) or "none"
    if commitment_outcome not in {"none", "fulfilled", "cancelled"}:
        commitment_outcome = "none"

    tagged: dict[str, list[str]] = {tag: [] for tag in PUBLIC_SEGMENT_TAGS}
    for segment in public_segments:
        text = public_field_text(segment.get("text"))
        for tag in segment.get("tags") or []:
            if tag in tagged and text and text not in tagged[tag]:
                tagged[tag].append(text)

    player_requests = [
        {
            "item_id": clean_text(segment.get("id")),
            "prompt": public_field_text(segment.get("text")),
        }
        for segment in public_segments
        if "player_request" in list(segment.get("tags") or [])
    ][:6]
    for request in player_requests:
        if len(request["prompt"]) > 180:
            raise NPCSpeechPlanValidationError(
                "each player_request segment must contain one short answerable request",
                correction_hint=(
                    "把NPC动作、背景、威胁和条件分别放进不带player_request标签的短段；"
                    "只把NPC此刻要求玩家回答的最后一个短问题单独成段并标记player_request，"
                    "该问题段不得超过180个字符。不要把整篇NPC发言都标成player_request。"
                ),
            )
    addressed_actor = clean_text(data.get("response_addressee"))
    response_scope = clean_text(data.get("response_scope") or "party").lower()
    if response_scope not in {"party", "actor_only"}:
        response_scope = "party"
    if player_requests and response_scope == "actor_only" and not addressed_actor:
        raise ValueError(
            "actor_only player_response_request requires response_addressee"
        )
    return {
        "speech_act": speech_act,
        "stance": clean_text(data.get("stance")),
        "intent": clean_text(data.get("intent")),
        "facts_to_share": tagged["fact"],
        "facts_to_withhold": clean_text_list(data.get("facts_to_withhold")),
        "condition": join_text(tagged["gate_requirement"]),
        "condition_outcome": condition_outcome,
        "promised_result": join_text(tagged["gate_payoff"]),
        "promise_kind": promise_kind,
        "promise_subject": clean_text(data.get("promise_subject")),
        "proposal_outcome": proposal_outcome,
        "settled_terms": (
            join_text(tagged["settled_terms"])
            if proposal_outcome in {"accepted", "rejected"}
            else ""
        ),
        "emotion": clean_text(data.get("emotion")),
        "nonverbal_reaction": join_text(tagged["nonverbal"]),
        "direct_answer": join_text(tagged["direct_answer"]),
        "deferred_action": join_text(tagged["deferred_action"]),
        "deferred_result": join_text(tagged["deferred_result"]),
        "deferred_trigger": join_text(tagged["deferred_trigger"]),
        "commitment_id": clean_text(data.get("commitment_id")),
        "commitment_outcome": commitment_outcome,
        "introduced_npcs": normalize_introduced_npcs(data.get("introduced_npcs")),
        "fact_effects": normalize_npc_fact_effects(data.get("fact_effects")),
        "player_response_request": (
            {
                "summary": "、".join(
                    request["prompt"] for request in player_requests
                )[:300],
                "required_items": player_requests,
                "addressed_actor": addressed_actor,
                "response_scope": response_scope,
            }
            if player_requests
            else {}
        ),
    }


def normalize_npc_fact_effects(value: Any) -> list[dict[str, Any]]:
    """Normalize newly established facts without conflating speech with truth."""

    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("fact_effects must be an array")
    if len(value) > 4:
        raise ValueError("fact_effects may contain at most 4 effects")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"fact_effects[{index}] must be an object")
        kind = clean_text(item.get("kind")).lower()
        fact = clean_text(item.get("fact"))
        scope = clean_text(item.get("scope") or "scene").lower()
        if kind not in NPC_FACT_EFFECT_KINDS:
            raise ValueError(f"fact_effects[{index}].kind is invalid")
        if scope not in NPC_FACT_EFFECT_SCOPES:
            raise ValueError(f"fact_effects[{index}].scope is invalid")
        if not fact:
            raise ValueError(f"fact_effects[{index}].fact is required")
        if len(fact) > 500:
            raise ValueError(f"fact_effects[{index}].fact is too long")
        related_entities = clean_text_list(item.get("related_entities"))[:6]
        result.append(
            {
                "kind": kind,
                "scope": scope,
                "fact": fact,
                "related_entities": related_entities,
            }
        )
    return result


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def public_field_text(value: Any) -> str:
    text = clean_text(value)
    quote_pairs = (("“", "”"), ('"', '"'), ("‘", "’"))
    for left, right in quote_pairs:
        if text.startswith(left) and text.endswith(right) and len(text) > 1:
            return text[len(left) : -len(right)].strip()
    return text


def clean_text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        text = clean_text(item)
        if text and text not in result:
            result.append(text)
    return result


def join_text(values: list[str]) -> str:
    return " ".join(values).strip()


def normalize_introduced_npcs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value[:2]:
        if not isinstance(item, dict):
            continue
        name = clean_text(item.get("name"))
        profile = item.get("profile")
        if not name or not isinstance(profile, dict):
            continue
        result.append({"name": name, "profile": dict(profile)})
    return result
