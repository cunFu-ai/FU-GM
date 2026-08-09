from __future__ import annotations


def safe_campaign_path_segment(value: object, *, default: str = "default") -> str:
    """Return one filesystem-safe path segment without changing campaign identity.

    Leading underscores, dots inside the name, Unicode letters, and digits are
    preserved. Path separators and other unsafe characters are replaced so all
    campaign-scoped stores resolve the same campaign id to the same directory.
    """

    text = str(value or "").strip()
    cleaned = "".join(
        char if char.isalnum() or char in {"-", "_", "."} else "_"
        for char in text
    )
    if not cleaned or cleaned in {".", ".."}:
        return str(default or "")
    return cleaned
