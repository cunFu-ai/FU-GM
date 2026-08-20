from __future__ import annotations

import re
from typing import Protocol

from fu_gm.models import ActionResolution


class ResolutionRenderer(Protocol):
    def render(self, resolution: ActionResolution) -> str:
        ...


class TurnResponseRenderer:
    """Select the single public author for a resolved turn.

    Most actions still need the general expressor. Focused interactions such as
    direct NPC dialogue already have a dedicated voice pass, so sending that
    finished speech through the general expressor would add latency and create
    a second, potentially contradictory author.
    """

    _CLOCK_STATE = re.compile(r"【([^】]+)】\s*\d+\s*/\s*\d+")

    def render(self, resolution: ActionResolution, *, expressor: ResolutionRenderer) -> str:
        prepared = str(resolution.action.parameters.get("player_facing_reply") or "").strip()
        lines = [prepared or expressor.render(resolution)]
        self._append_turn_state(lines, resolution)
        return "\n".join(line for line in lines if str(line).strip())

    @staticmethod
    def _normalize_prose(text: str) -> str:
        return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(text or "")).lower()

    def _append_turn_state(self, lines: list[str], resolution: ActionResolution) -> None:
        if not (
            resolution.payload.get("turn_auto_advanced")
            or resolution.payload.get("clock_status_refresh")
        ):
            notice = str(resolution.payload.get("held_action_notice") or "").strip()
            if notice and not self._contains_line(lines, notice):
                lines.append(notice)
            return

        lines.extend(self.public_state_lines(resolution.payload, existing_lines=lines))

        notice = str(resolution.payload.get("held_action_notice") or "").strip()
        if notice and not self._contains_line(lines, notice):
            lines.append(notice)

    @classmethod
    def _contains_line(cls, lines: list[str], candidate: str) -> bool:
        normalized = cls._normalize_prose(candidate)
        return bool(normalized) and any(
            normalized in cls._normalize_prose(line)
            for line in lines
            if str(line or "").strip()
        )

    @classmethod
    def contains_public_text(cls, public_reply: str, required_text: str) -> bool:
        """Compare player-facing prose without depending on punctuation."""

        required = cls._normalize_prose(required_text)
        if not required:
            return True
        return required in cls._normalize_prose(public_reply)

    @classmethod
    def insert_before_public_state(cls, public_reply: str, addition: str) -> str:
        """Keep a check outcome before compact clock/status lines."""

        outcome = str(addition or "").strip()
        if not outcome:
            return str(public_reply or "").strip()
        lines = [
            line
            for line in str(public_reply or "").splitlines()
            if line.strip()
        ]
        insert_at = next(
            (
                index
                for index, line in enumerate(lines)
                if cls._CLOCK_STATE.search(line)
            ),
            len(lines),
        )
        lines.insert(insert_at, outcome)
        return "\n".join(lines).strip()

    @classmethod
    def public_state_lines(
        cls,
        payload: dict[str, object],
        *,
        existing_lines: list[str] | None = None,
    ) -> list[str]:
        """Render compact clock state for a committed nonstandard action."""

        result: list[str] = []
        existing = list(existing_lines or [])
        seen = {
            name
            for line in existing
            for name in cls._CLOCK_STATE.findall(str(line or ""))
        }
        for change in payload.get("auto_clock_changes") or []:
            name = str(getattr(change, "clock_name", "") or "").strip()
            if not name or name in seen:
                continue
            current = getattr(change, "after", None)
            maximum = getattr(change, "max_segments", None)
            if current is None or maximum is None:
                continue
            result.append(f"【{name}】{current}/{maximum}")
            if int(current) >= int(maximum):
                consequence = cls._completion_consequence(change)
                if consequence and consequence not in existing and consequence not in result:
                    result.append(consequence)
            seen.add(name)

        for item in payload.get("clock_progress") or []:
            rendered = str(item or "").strip()
            item_names = set(cls._CLOCK_STATE.findall(rendered))
            if not rendered or (item_names and item_names.issubset(seen)):
                continue
            result.append(rendered)
            seen.update(item_names)
        return result

    @staticmethod
    def _completion_consequence(change: object) -> str:
        text = str(
            getattr(change, "completion_consequence", "")
            or getattr(change, "stakes", "")
            or ""
        ).strip()
        if not text:
            return ""
        text = re.sub(r"^(?:赌注[：:]\s*)", "", text)
        text = re.sub(r"^(?:填满后|完成后|若填满(?:则)?)[，,：:\s]*", "", text)
        if text and text[-1] not in "。！？!?":
            text += "。"
        return text
