from __future__ import annotations

from typing import Any


def receipt_result(receipt: dict[str, Any]) -> dict[str, Any]:
    result = receipt.get("result")
    return result if isinstance(result, dict) else {}


def is_recovered_rejection(receipt: dict[str, Any]) -> bool:
    """Return true only for a rejection linked to its successful retry."""

    return not bool(receipt.get("ok")) and bool(
        receipt_result(receipt).get("recovered_precondition")
    )


def is_unrecovered_rejection(receipt: dict[str, Any]) -> bool:
    return not bool(receipt.get("ok")) and not is_recovered_rejection(receipt)
