from __future__ import annotations

from fu_gm.gm_tool_contracts import GMToolExecutionContext


def normalize_literal_evidence(value: object) -> str:
    """Normalize transport-only whitespace without changing message semantics."""

    return " ".join(str(value or "").split()).strip()


def is_current_message_evidence(
    context: GMToolExecutionContext,
    value: object,
) -> bool:
    """Return whether evidence is a continuous literal span of this message.

    AstrBot and JSON transports may represent the same whitespace as spaces,
    newlines, tabs, or CRLF. Comparing both sides after whitespace-only
    normalization keeps the literal-evidence boundary while avoiding false
    rejections caused by transport formatting.
    """

    evidence = normalize_literal_evidence(value)
    current = normalize_literal_evidence(
        context.metadata.get("current_message")
    )
    return bool(evidence and evidence in current)
