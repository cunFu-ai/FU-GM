from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
import hashlib
from typing import Any

from fu_gm.gm_tool_contracts import json_safe_value
from fu_gm.gm_tool_protocol import GMToolProtocol


SEMANTIC_TOOL_ATTESTATIONS_METADATA_KEY = "_gm_semantic_tool_attestations"
CURRENT_SEMANTIC_TOOL_ATTESTATION_METADATA_KEY = (
    "_gm_current_semantic_tool_attestation"
)


def clear_semantic_tool_attestations(context: Any) -> None:
    """Drop review attestations before evaluating a new model decision."""

    metadata = getattr(context, "metadata", None)
    if isinstance(metadata, dict):
        metadata.pop(SEMANTIC_TOOL_ATTESTATIONS_METADATA_KEY, None)


def remember_semantic_tool_attestations(
    context: Any,
    proposals: list[dict[str, object]],
) -> None:
    """Sign the exact argument payloads approved by semantic preflight.

    The attestation is request-local metadata. It is not persisted and it does
    not authorize a modified retry, so handlers can prefer a full-context
    semantic review without weakening their fallback checks for direct calls.
    """

    metadata = getattr(context, "metadata", None)
    if not isinstance(metadata, dict):
        return
    rows: list[dict[str, str]] = []
    for proposal in proposals:
        tool_name = str(proposal.get("tool_name") or "").strip()
        if not tool_name:
            continue
        rows.append(
            {
                "tool_name": tool_name,
                "fingerprint": _proposal_fingerprint(
                    tool_name,
                    proposal.get("arguments"),
                ),
            }
        )
    if rows:
        metadata[SEMANTIC_TOOL_ATTESTATIONS_METADATA_KEY] = rows


def semantic_tool_proposal_attested(
    context: Any,
    tool_name: str,
    arguments: object,
) -> bool:
    """Return whether semantic preflight approved this exact tool call."""

    metadata = getattr(context, "metadata", None)
    if not isinstance(metadata, dict):
        return False
    clean_name = str(tool_name or "").strip()
    expected = _proposal_fingerprint(clean_name, arguments)
    return any(
        isinstance(row, dict)
        and str(row.get("tool_name") or "").strip() == clean_name
        and str(row.get("fingerprint") or "").strip() == expected
        for row in list(
            metadata.get(SEMANTIC_TOOL_ATTESTATIONS_METADATA_KEY) or []
        )
    )


@contextmanager
def semantic_tool_attestation_scope(
    context: Any,
    tool_name: str,
    arguments: object,
) -> Iterator[bool]:
    """Bind an exact semantic approval only while that call is executing.

    Registry validation may add defaults or otherwise normalize arguments before
    the handler receives them. The ledger still has the model's original call,
    so it verifies the signed fingerprint there and exposes only a temporary,
    non-persistent marker to the handler.
    """

    metadata = getattr(context, "metadata", None)
    clean_name = str(tool_name or "").strip()
    attested = semantic_tool_proposal_attested(context, clean_name, arguments)
    if not isinstance(metadata, dict):
        yield False
        return

    missing = object()
    previous = metadata.get(
        CURRENT_SEMANTIC_TOOL_ATTESTATION_METADATA_KEY,
        missing,
    )
    if attested:
        metadata[CURRENT_SEMANTIC_TOOL_ATTESTATION_METADATA_KEY] = {
            "tool_name": clean_name,
        }
    else:
        metadata.pop(CURRENT_SEMANTIC_TOOL_ATTESTATION_METADATA_KEY, None)
    try:
        yield attested
    finally:
        if previous is missing:
            metadata.pop(CURRENT_SEMANTIC_TOOL_ATTESTATION_METADATA_KEY, None)
        else:
            metadata[CURRENT_SEMANTIC_TOOL_ATTESTATION_METADATA_KEY] = previous


def current_semantic_tool_call_attested(context: Any, tool_name: str) -> bool:
    """Return whether the tool currently executing passed exact preflight."""

    metadata = getattr(context, "metadata", None)
    if not isinstance(metadata, dict):
        return False
    current = metadata.get(CURRENT_SEMANTIC_TOOL_ATTESTATION_METADATA_KEY)
    return bool(
        isinstance(current, dict)
        and str(current.get("tool_name") or "").strip()
        == str(tool_name or "").strip()
    )


def _proposal_fingerprint(tool_name: str, arguments: object) -> str:
    canonical = GMToolProtocol.call_fingerprint(
        str(tool_name or "").strip(),
        json_safe_value(arguments),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
