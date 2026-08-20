from __future__ import annotations

import pytest

from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolReceipt,
    GMToolRegistry,
)
from fu_gm.prompt_cache import prompt_layout_fingerprint


def _read(_context, _arguments):
    return GMToolReceipt.success("read")


def test_registry_schemas_are_stably_sorted_and_expose_execution_metadata() -> None:
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="zeta",
            description="z",
            handler=_read,
            is_concurrency_safe=True,
            max_model_result_chars=123,
        )
    )
    registry.register(
        GMToolDefinition(name="alpha", description="a", handler=_read)
    )

    schemas = registry.schemas()
    assert [schema["name"] for schema in schemas] == ["alpha", "zeta"]
    assert schemas[1]["execution"] == {
        "concurrency_safe": True,
        "destructive": False,
        "defer_group": "",
        "max_model_result_chars": 123,
    }


def test_registry_rejects_unsafe_execution_metadata() -> None:
    registry = GMToolRegistry()
    with pytest.raises(ValueError, match="写工具不能声明为并发安全"):
        registry.register(
            GMToolDefinition(
                name="bad",
                description="bad",
                handler=_read,
                side_effect="write",
                is_concurrency_safe=True,
            )
        )

def test_layout_fingerprint_ignores_dynamic_player_content() -> None:
    schemas = [
        GMToolDefinition(name="alpha", description="a", handler=_read).schema()
    ]
    first = prompt_layout_fingerprint(
        static_system_prompt="稳定系统提示",
        tool_schemas=schemas,
    )
    second = prompt_layout_fingerprint(
        static_system_prompt="稳定系统提示",
        tool_schemas=schemas,
    )
    changed = prompt_layout_fingerprint(
        static_system_prompt="另一版系统提示",
        tool_schemas=schemas,
    )
    assert first == second
    assert first != changed
