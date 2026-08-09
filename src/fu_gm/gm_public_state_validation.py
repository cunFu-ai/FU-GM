from __future__ import annotations

from typing import Any, Iterable


def known_actor_names(app: Any) -> set[str]:
    """Return stable PC/NPC names that public scene prose can contradict."""

    names = {
        str(character.name or "").strip()
        for character in app.character_manager.all()
        if str(character.name or "").strip()
    }
    names.update(
        str(name or "").strip()
        for name in getattr(app.world_state, "npc_personas", {})
        if str(name or "").strip()
    )
    names.update(
        str(name or "").strip()
        for name in getattr(app.scene_manager, "actor_locations", {})
        if str(name or "").strip()
    )
    return names


def unexpected_actor_mentions(
    app: Any,
    public_text: str,
    *,
    allowed_names: Iterable[str],
) -> list[str]:
    """Find known actors named in destination-only prose but absent there."""

    text = str(public_text or "")
    allowed = {
        str(name or "").strip()
        for name in allowed_names
        if str(name or "").strip()
    }
    return sorted(
        (
            name
            for name in known_actor_names(app)
            if name not in allowed and name in text
        ),
        key=lambda value: (-len(value), value),
    )
