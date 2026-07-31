from __future__ import annotations

from typing import Any


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
PUBLIC_SEGMENT_INPUT_TAGS = frozenset(
    {*PUBLIC_SEGMENT_TAGS, *PUBLIC_SEGMENT_TAG_ALIASES}
)


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
            tag = PUBLIC_SEGMENT_TAG_ALIASES.get(input_tag, input_tag)
            if tag in tags:
                raise ValueError(
                    f"public_segments[{index}] contains duplicate tag: {tag}"
                )
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
            raise ValueError(
                "each player_request segment must contain one short answerable request"
            )
    addressed_actor = clean_text(data.get("response_addressee"))
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
        "player_response_request": (
            {
                "summary": "、".join(
                    request["prompt"] for request in player_requests
                )[:300],
                "required_items": player_requests,
                "addressed_actor": addressed_actor,
            }
            if player_requests
            else {}
        ),
    }


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
