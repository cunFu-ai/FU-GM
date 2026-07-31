from __future__ import annotations

import json


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
