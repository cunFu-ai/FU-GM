from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from fu_gm.app_factory import _expressor_rule_result_prose_enabled
from fu_gm.deepseek_roleplay import (
    INNER_OS_MARKER,
    NO_INNER_OS_MARKER,
    apply_deepseek_reasoning_style,
    normalize_deepseek_roleplay_mode,
    strip_deepseek_reasoning_leakage,
)
from fu_gm.expressor import LLMExpressor
from fu_gm.models import Action, ActionResolution, ActionType


class FakeClient:
    def __init__(self, content: str = "") -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []
        self.config = SimpleNamespace(thinking_enabled=True)

    def create_chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return self.content


def test_roleplay_mode_supports_selective_inner_os() -> None:
    assert normalize_deepseek_roleplay_mode("inner_os") == "inner_os"
    assert normalize_deepseek_roleplay_mode("immersive") == "inner_os"
    assert normalize_deepseek_roleplay_mode("no_inner_os") == "no_inner_os"


def test_no_inner_os_marker_only_applies_to_thinking_deepseek_v4() -> None:
    styled = apply_deepseek_reasoning_style(
        "动态请求",
        model="deepseek-v4-flash",
        mode="no_inner_os",
        thinking_enabled=True,
    )
    plain = apply_deepseek_reasoning_style(
        "动态请求",
        model="deepseek-v4-flash",
        mode="no_inner_os",
        thinking_enabled=False,
    )

    assert styled.endswith(NO_INNER_OS_MARKER)
    assert plain == "动态请求"


def test_inner_os_marker_and_leakage_filter_are_bounded() -> None:
    styled = apply_deepseek_reasoning_style(
        "动态请求",
        model="deepseek-v4-flash",
        mode="inner_os",
        thinking_enabled=True,
    )

    assert styled.endswith(INNER_OS_MARKER)
    assert strip_deepseek_reasoning_leakage(
        "<think>后台推演</think>（心想：别露馅。）最终公开句。"
    ) == "最终公开句。"


def test_deepseek_rule_result_prose_defaults_off_but_can_be_overridden() -> None:
    with patch.dict("os.environ", {}, clear=False):
        with patch.dict(
            "os.environ",
            {"FU_GM_EXPRESSOR_RULE_RESULT_PROSE_ENABLED": ""},
            clear=False,
        ):
            assert not _expressor_rule_result_prose_enabled("deepseek-v4-flash")
            assert _expressor_rule_result_prose_enabled("gpt-5.6-terra")

        with patch.dict(
            "os.environ",
            {"FU_GM_EXPRESSOR_RULE_RESULT_PROSE_ENABLED": "1"},
            clear=False,
        ):
            assert _expressor_rule_result_prose_enabled("deepseek-v4-flash")


def test_disabled_rule_result_prose_keeps_canonical_panel_without_model_call() -> None:
    client = FakeClient("凭空出现的盾牌与蓄力攻击。")
    expressor = LLMExpressor(
        client=client,
        model="deepseek-v4-flash",
        allow_fallback=False,
        rule_result_prose_enabled=False,
    )
    resolution = ActionResolution(
        action=Action(ActionType.GUARD, {"actor": "诺艾尔"}),
        rules_text="诺艾尔执行防御行动。",
        payload={},
    )

    assert expressor.render(resolution) == "诺艾尔执行防御行动。"
    assert client.calls == []


def test_expressor_appends_analysis_marker_after_static_cache_prefix() -> None:
    client = FakeClient("")
    expressor = LLMExpressor(
        client=client,
        model="deepseek-v4-flash",
        allow_fallback=False,
        deepseek_roleplay_mode="no_inner_os",
        rule_result_prose_enabled=True,
    )
    resolution = ActionResolution(
        action=Action(ActionType.GUARD, {"actor": "诺艾尔"}),
        rules_text="诺艾尔执行防御行动。",
        payload={},
    )

    expressor.render(resolution)

    messages = client.calls[0]["messages"]
    assert NO_INNER_OS_MARKER not in messages[0].content
    assert messages[-1].content.endswith(NO_INNER_OS_MARKER)


def test_inner_os_only_applies_to_immersive_agent_expression() -> None:
    client = FakeClient('{"parts":["自然回应。"]}')
    expressor = LLMExpressor(
        client=client,
        model="deepseek-v4-flash",
        allow_fallback=False,
        deepseek_roleplay_mode="inner_os",
    )

    assert expressor.render_agent_message(
        ["回应玩家。"],
        current_message="悠老师，在吗？",
        recent_context="",
        gate_status="adventure",
        route_mode="gm_agent_reply",
        expression_style="immersive",
    ) == ["自然回应。"]

    call = client.calls[-1]
    assert call["thinking_enabled"] is True
    assert call["messages"][-1].content.endswith(INNER_OS_MARKER)


def test_inner_os_is_forced_off_for_rule_result_expression() -> None:
    client = FakeClient("")
    expressor = LLMExpressor(
        client=client,
        model="deepseek-v4-flash",
        allow_fallback=False,
        deepseek_roleplay_mode="inner_os",
        rule_result_prose_enabled=True,
    )
    resolution = ActionResolution(
        action=Action(ActionType.GUARD, {"actor": "诺艾尔"}),
        rules_text="诺艾尔执行防御行动。",
        payload={},
    )

    expressor.render(resolution)

    call = client.calls[0]
    assert call["thinking_enabled"] is False
    assert INNER_OS_MARKER not in call["messages"][-1].content
