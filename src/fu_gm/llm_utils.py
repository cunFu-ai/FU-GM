from __future__ import annotations

import json


def close_truncated_json_containers(text: str) -> str | None:
    """Close only provably unterminated trailing JSON containers.

    This deliberately does not repair strings, commas, values, or mismatched
    delimiters.  It is safe for the common provider failure where a complete
    object ends one or more ``}``/``]`` characters early: the original bytes
    remain unchanged and the repaired candidate must decode before it is
    returned.
    """

    stripped = str(text or "").strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2:
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    if start < 0:
        return None
    candidate = stripped[start:]
    stack: list[str] = []
    in_string = False
    escaped = False
    pairs = {"}": "{", "]": "["}
    for char in candidate:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if not stack or stack[-1] != pairs[char]:
                return None
            stack.pop()
    if in_string or escaped or not stack:
        return None
    suffix = "".join("}" if char == "{" else "]" for char in reversed(stack))
    repaired = candidate + suffix
    try:
        extract_json_object_sequence(repaired)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return repaired


def reopen_premature_json_root(text: str) -> str | None:
    """Undo one premature root-object close before trailing members.

    Some OpenAI-compatible providers emit ``{...}},"reason":...}``: the
    first object is valid, but its root is closed immediately before more
    object members.  This repair removes only that one closing brace.  It does
    not alter keys or values, and the candidate must decode as exactly one
    object before it is accepted.
    """

    stripped = str(text or "").strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2:
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    if start < 0:
        return None
    candidate = stripped[start:]
    decoder = json.JSONDecoder()
    try:
        value, end = decoder.raw_decode(candidate)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or end < 1 or candidate[end - 1] != "}":
        return None
    trailing = candidate[end:]
    if not trailing.lstrip().startswith(","):
        return None
    repaired = candidate[: end - 1] + trailing
    try:
        decoded = json.loads(repaired)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return repaired if isinstance(decoded, dict) else None


def extract_json_object(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            stripped = "\n".join(lines[1:-1]).strip()

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("未找到合法 JSON 对象。")
    return json.loads(stripped[start : end + 1])


def extract_json_object_sequence(text: str) -> list[dict]:
    """Decode one or more adjacent JSON objects without executing any of them."""

    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            stripped = "\n".join(lines[1:-1]).strip()
    start = stripped.find("{")
    if start < 0:
        raise ValueError("未找到合法 JSON 对象。")
    stripped = stripped[start:]
    decoder = json.JSONDecoder()
    result: list[dict] = []
    cursor = 0
    while cursor < len(stripped):
        while cursor < len(stripped) and stripped[cursor].isspace():
            cursor += 1
        if cursor >= len(stripped):
            break
        value, end = decoder.raw_decode(stripped, cursor)
        if not isinstance(value, dict):
            raise ValueError("工具智能体的每个 JSON 值都必须是对象。")
        result.append(value)
        cursor = end
    if not result:
        raise ValueError("未找到合法 JSON 对象。")
    return result
