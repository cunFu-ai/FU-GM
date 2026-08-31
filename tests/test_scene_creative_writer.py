from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from fu_gm.components.scene_creative_writer import (
    SceneCreativeWriter,
    SceneCreativeWriterError,
)
from fu_gm.deepseek_roleplay import INNER_OS_MARKER


class ScriptedClient:
    def __init__(self, *payloads: object, thinking_enabled: bool = False) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict[str, object]] = []
        self.config = SimpleNamespace(thinking_enabled=thinking_enabled)

    def create_chat_completion(self, **kwargs: object) -> str:
        self.calls.append(dict(kwargs))
        if not self.payloads:
            raise TimeoutError("creative provider timed out")
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return json.dumps(payload, ensure_ascii=False)


def test_deepseek_authors_private_situation_and_public_opening() -> None:
    client = ScriptedClient(
        {
            "private_situation": {
                "premise": "牢门符文短暂失效。",
                "stakes": "英雄能否在封锁恢复前离开牢区。",
                "visible_elements": ["熄灭的符文", "走廊尽头的值班室"],
                "secrets": ["震动来自地下旧兵器"],
            },
            "public_opening": "蓝色符文同时熄灭，走廊尽头传来一声惊叫。",
            "player_handoff": "诺艾尔和艾丽妮，此刻你们先做什么？",
        }
    )
    writer = SceneCreativeWriter(
        client=client,
        model="deepseek-v4-flash",
    )

    result = writer.compose_scene_opening(
        scene_request={"name": "卡里巴村监狱", "participants": ["诺艾尔", "艾丽妮"]},
        session_contract={"dramatic_question": "两人能否越狱？"},
        opening_contract={"opening_disruption": "监狱突然震动。"},
        current_message="进入第一章。",
        recent_public_messages=[],
        deadline=12345.0,
    )

    assert result.used_model is True
    assert result.model == "deepseek-v4-flash"
    assert result.private_situation["secrets"] == ["震动来自地下旧兵器"]
    assert result.player_handoff.endswith("？")
    assert client.calls[0]["model"] == "deepseek-v4-flash"
    assert client.calls[0]["operation"] == "scene_opening"
    assert client.calls[0]["max_tokens"] == 2400
    assert client.calls[0]["deadline"] == 12345.0
    assert client.calls[0]["thinking_enabled"] is False
    assert client.calls[0]["max_recovery_retries"] == 1
    assert client.calls[0]["retry_without_response_format_on_empty"] is True
    messages = client.calls[0]["messages"]
    assert messages[0].cache_family == "scene-creative-writer"
    assert "你叫时悠" not in messages[0].content
    assert "语言像真人主持人：具体、顺畅、克制" in messages[0].content
    assert '"operation": "scene_opening"' in messages[1].content
    assert "不得写成‘你们可以先A、B或C’" in messages[0].content
    assert "眼下的难题很明确" in messages[0].content
    assert "150到250个中文字符" in messages[0].content
    assert "不得超过500字" in messages[0].content


def test_scene_opening_compacts_paragraphs_without_extra_repair() -> None:
    client = ScriptedClient(
        {
            "private_situation": {
                "premise": "牢门符文短暂失效。",
                "current_pressure": "守卫正在接近。",
            },
            "public_opening": "牢门符文刚刚熄灭。\n\n走廊里传来铁靴声。",
            "player_handoff": "诺艾尔和艾丽妮，\n你们先做什么？",
        }
    )
    writer = SceneCreativeWriter(client=client, model="deepseek-v4-flash")

    result = writer.compose_scene_opening(
        scene_request={"participants": ["诺艾尔", "艾丽妮"]},
        session_contract={},
        opening_contract={},
        current_message="进入第一章。",
        recent_public_messages=[],
    )

    assert result.public_opening == "牢门符文刚刚熄灭。 走廊里传来铁靴声。"
    assert result.player_handoff == "诺艾尔和艾丽妮， 你们先做什么？"
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("operation", "expected_max_tokens"),
    [
        ("scene_response", 1800),
        ("npc_combat_action", 800),
        ("session_closure", 2400),
        ("future_public_operation", 2400),
    ],
)
def test_public_scene_operations_use_bounded_output_budgets(
    operation: str,
    expected_max_tokens: int,
) -> None:
    client = ScriptedClient(
        {"public_reply": "权威事实已经公开。余音未散。", "closing_image": "余音未散。"}
    )
    writer = SceneCreativeWriter(client=client, model="deepseek-v4-flash")

    writer.compose_public_scene_text(
        operation=operation,
        facts={},
        recent_public_messages=[],
        require_closing_image=operation == "session_closure",
    )

    assert client.calls[0]["max_tokens"] == expected_max_tokens


def test_scene_opening_repairs_output_beyond_tolerant_hard_limit() -> None:
    client = ScriptedClient(
        {
            "private_situation": {"premise": "牢门符文失效。"},
            "public_opening": "雨" * 501,
            "player_handoff": "你们怎么做？",
        },
        {
            "private_situation": {
                "premise": "牢门符文失效。",
                "current_pressure": "守卫正在接近。",
            },
            "public_opening": "牢门符文熄灭，走廊里传来逼近的铁靴声。",
            "player_handoff": "你们怎么做？",
        },
    )
    writer = SceneCreativeWriter(client=client, model="deepseek-v4-flash")

    result = writer.compose_scene_opening(
        scene_request={"participants": ["诺艾尔", "艾丽妮"]},
        session_contract={},
        opening_contract={},
        current_message="进入第一章。",
        recent_public_messages=[],
    )

    assert result.public_opening.startswith("牢门符文熄灭")
    assert len(client.calls) == 2
    assert client.calls[1]["max_tokens"] == 2400
    assert "超过宽容上限" in client.calls[1]["messages"][1].content


def test_scene_opening_repairs_string_private_packet_and_repeated_handoff() -> None:
    client = ScriptedClient(
        {
            "private_situation": "牢门符文短暂失效。",
            "public_opening": "牢门符文刚刚熄灭。你们要做什么？",
            "player_handoff": "你们要做什么？",
        },
        {
            "private_situation": {
                "premise": "牢门符文短暂失效。",
                "current_pressure": "守卫正在接近。",
                "visible_elements": ["熄灭的符文"],
                "secrets": ["失灵并非偶然"],
            },
            "public_opening": "牢门符文刚刚熄灭。",
            "player_handoff": "诺艾尔和艾丽妮，你们此刻先做什么？",
        },
    )
    writer = SceneCreativeWriter(client=client, model="deepseek-v4-flash")

    result = writer.compose_scene_opening(
        scene_request={"participants": ["诺艾尔", "艾丽妮"]},
        session_contract={"dramatic_question": "两人能否越狱？"},
        opening_contract={"required_public_facts": ["牢门符文刚刚熄灭。"]},
        current_message="进入第一章。",
        recent_public_messages=[],
        deadline=23456.0,
    )

    assert result.private_situation["current_pressure"] == "守卫正在接近。"
    assert result.public_opening == "牢门符文刚刚熄灭。"
    assert len(client.calls) == 2
    assert '"operation": "scene_opening_repair"' in client.calls[1]["messages"][1].content


def test_scene_opening_uses_semantic_auditor_for_natural_fact_rewrite() -> None:
    author = ScriptedClient(
        {
            "private_situation": {
                "premise": "牢门符文失效。",
                "current_pressure": "守卫正在接近。",
            },
            "public_opening": "相邻的两间牢房之间，蓝色门符就在刚才一齐暗了。",
            "player_handoff": "诺艾尔和艾丽妮，你们先做什么？",
        }
    )
    auditor = ScriptedClient(
        {
            "valid": True,
            "missing_facts": [],
            "contradictions": [],
            "private_leaks": [],
            "handoff_repeated": False,
            "reason": "两项事实均已自然保留。",
        }
    )
    writer = SceneCreativeWriter(
        client=author,
        model="deepseek-v4-flash",
        audit_client=auditor,
        audit_model="gpt-5.6-terra",
    )

    result = writer.compose_scene_opening(
        scene_request={"participants": ["诺艾尔", "艾丽妮"]},
        session_contract={},
        opening_contract={
            "required_public_facts": [
                "诺艾尔与艾丽妮身处相邻牢房。",
                "牢门符文刚刚熄灭。",
            ]
        },
        current_message="进入第一章。",
        recent_public_messages=[],
        deadline=23456.0,
    )

    assert "一齐暗了" in result.public_opening
    assert writer.last_audit_status == "approved"
    assert auditor.calls[0]["model"] == "gpt-5.6-terra"
    assert auditor.calls[0]["max_tokens"] == 900
    assert auditor.calls[0]["deadline"] == 23456.0
    assert auditor.calls[0]["thinking_enabled"] is False
    assert auditor.calls[0]["max_recovery_retries"] == 1
    assert auditor.calls[0]["retry_without_response_format_on_empty"] is True


def test_scene_response_uses_semantic_auditor_for_natural_fact_rewrite() -> None:
    author = ScriptedClient(
        {
            "public_reply": "升降台离开矿道深处，正朝采掘城上层升去，三名英雄仍站在平台上。",
            "awaits_player_response": False,
        }
    )
    auditor = ScriptedClient(
        {
            "valid": True,
            "missing_facts": [],
            "contradictions": [],
            "private_leaks": [],
            "handoff_repeated": False,
            "reason": "移动主体、方向和时序均完整保留。",
        }
    )
    fact = "升降台正在缓缓上升，带着英雄们离开矿道深处，向采掘城的上层移动。"
    writer = SceneCreativeWriter(
        client=author,
        model="deepseek-v4-flash",
        audit_client=auditor,
        audit_model="deepseek-v4-flash",
    )

    result = writer.compose_public_scene_text(
        operation="scene_response",
        facts={"public_facts": [fact]},
        recent_public_messages=[],
        deadline=34567.0,
    )

    assert result.public_reply.startswith("升降台离开矿道")
    assert result.grounded_public_facts == (fact,)
    assert writer.last_audit_status == "approved"
    assert auditor.calls[0]["deadline"] == 34567.0


def test_scene_response_rejects_semantically_missing_fact() -> None:
    author = ScriptedClient({"public_reply": "公告钟在城中响起。"})
    auditor = ScriptedClient(
        {
            "valid": False,
            "missing_facts": ["升降台正在前往采掘城上层。"],
            "contradictions": [],
            "private_leaks": [],
            "handoff_repeated": False,
            "reason": "公开回复没有提及升降台移动。",
        }
    )
    writer = SceneCreativeWriter(
        client=author,
        model="deepseek-v4-flash",
        audit_client=auditor,
        audit_model="deepseek-v4-flash",
    )

    with pytest.raises(SceneCreativeWriterError, match="语义事实审计未通过"):
        writer.compose_public_scene_text(
            operation="scene_response",
            facts={"public_facts": ["升降台正在前往采掘城上层。"]},
            recent_public_messages=[],
        )


def test_final_player_agency_audit_rejects_completed_pc_action() -> None:
    auditor = ScriptedClient(
        {
            "reviews": [
                {
                    "clause_index": 0,
                    "classification": "player_action",
                    "player_action_phrases": ["你再次辨认批注"],
                    "reason": "文本替玩家完成了主动查看。",
                }
            ]
        }
    )
    writer = SceneCreativeWriter(
        client=None,
        model="",
        audit_client=auditor,
        audit_model="gpt-5.6-luna",
    )

    with pytest.raises(SceneCreativeWriterError, match="你再次辨认批注"):
        writer.validate_player_agency(
            public_text="你再次辨认批注，隐约可见‘碎片’和‘回响’。",
            player_characters=["洛岚"],
            npc_characters=["维蕾娅"],
            deadline=45678.0,
        )

    assert writer.last_agency_audit_status == "rejected"
    assert auditor.calls[0]["deadline"] == 45678.0
    assert auditor.calls[0]["thinking_enabled"] is False
    assert auditor.calls[0]["operation"] == "scene_creative_player_agency_review"
    assert auditor.calls[0]["messages"][0].cache_family == "scene-player-agency"


def test_final_player_agency_audit_accepts_environment_and_npc_action() -> None:
    auditor = ScriptedClient(
        {
            "reviews": [
                {
                    "clause_index": 0,
                    "classification": "environment_change",
                    "player_action_phrases": [],
                    "reason": "门后声音来自环境。",
                },
                {
                    "clause_index": 1,
                    "classification": "npc_action",
                    "player_action_phrases": [],
                    "reason": "行动者是维蕾娅。",
                },
            ]
        }
    )
    writer = SceneCreativeWriter(
        client=None,
        model="",
        audit_client=auditor,
        audit_model="gpt-5.6-luna",
    )

    writer.validate_player_agency(
        public_text="门后传来一声换挡轻响。维蕾娅把钥匙环挂回腰间。",
        player_characters=["洛岚"],
        npc_characters=["维蕾娅"],
    )

    assert writer.last_agency_audit_status == "approved"


def test_final_player_agency_audit_fails_closed_on_missing_clause() -> None:
    auditor = ScriptedClient(
        {
            "reviews": [
                {
                    "clause_index": 0,
                    "classification": "environment_change",
                    "player_action_phrases": [],
                    "reason": "第一句来自环境。",
                }
            ]
        }
    )
    writer = SceneCreativeWriter(
        client=None,
        model="",
        audit_client=auditor,
        audit_model="gpt-5.6-luna",
    )

    with pytest.raises(SceneCreativeWriterError, match="没有逐句覆盖"):
        writer.validate_player_agency(
            public_text="门后传来一声换挡轻响。你翻到报告结论页。",
            player_characters=["洛岚"],
            npc_characters=[],
        )


def test_deepseek_public_writer_keeps_core_facts_out_of_static_prompt() -> None:
    client = ScriptedClient(
        {
            "public_reply": "门外的铁靴声压近了一层。\n【财团巡逻队逼近】5/6"
        }
    )
    writer = SceneCreativeWriter(client=client, model="deepseek-v4-flash")

    result = writer.compose_public_scene_text(
        operation="clock_change",
        facts={
            "progress_marker": "【财团巡逻队逼近】5/6",
            "near_completion": True,
        },
        recent_public_messages=[{"role": "player", "content": "我们快走。"}],
    )

    assert "【财团巡逻队逼近】5/6" in result.public_reply
    messages = client.calls[0]["messages"]
    assert "财团巡逻队逼近" not in messages[0].content
    assert "财团巡逻队逼近" in messages[1].content


def test_scene_writer_enables_inner_os_but_grounding_audit_stays_plain() -> None:
    author = ScriptedClient(
        {
            "private_situation": {
                "premise": "封印失效。",
                "current_pressure": "守卫接近。",
            },
            "public_opening": "相邻牢房的蓝色门符一齐暗了。",
            "player_handoff": "诺艾尔和艾丽妮，你们先做什么？",
        },
        thinking_enabled=True,
    )
    auditor = ScriptedClient(
        {
            "valid": True,
            "missing_facts": [],
            "contradictions": [],
            "private_leaks": [],
            "handoff_repeated": False,
            "reason": "一致",
        },
        thinking_enabled=True,
    )
    writer = SceneCreativeWriter(
        client=author,
        model="deepseek-v4-flash",
        audit_client=auditor,
        audit_model="gpt-5.6-terra",
        deepseek_roleplay_mode="inner_os",
    )

    writer.compose_scene_opening(
        scene_request={"participants": ["诺艾尔", "艾丽妮"]},
        session_contract={},
        opening_contract={"required_public_facts": ["两人身处相邻牢房。"]},
        current_message="进入第一章。",
        recent_public_messages=[],
    )

    assert author.calls[0]["thinking_enabled"] is True
    assert author.calls[0]["messages"][-1].content.endswith(INNER_OS_MARKER)
    assert auditor.calls[0]["thinking_enabled"] is False


def test_creative_provider_failure_is_reported_without_model_fallback() -> None:
    writer = SceneCreativeWriter(
        client=ScriptedClient(TimeoutError("provider timeout")),
        model="deepseek-v4-flash",
    )

    with pytest.raises(SceneCreativeWriterError, match="provider timeout"):
        writer.compose_public_scene_text(
            operation="scene_response",
            facts={"public_facts": ["牢门已经打开。"]},
            recent_public_messages=[],
        )

    assert writer.last_error == "provider timeout"


def test_scene_writer_rejects_public_inner_monologue_leakage() -> None:
    writer = SceneCreativeWriter(
        client=ScriptedClient(
            {
                "public_reply": "（心想：先把答案藏好。）牢门已经打开。",
            }
        ),
        model="deepseek-v4-flash",
    )

    with pytest.raises(SceneCreativeWriterError, match="思考过程或内心独白"):
        writer.compose_public_scene_text(
            operation="scene_response",
            facts={"public_facts": ["牢门已经打开。"]},
            recent_public_messages=[],
        )


def test_offline_mode_keeps_explicit_fallback_for_deterministic_tests() -> None:
    writer = SceneCreativeWriter(client=None, model="")

    result = writer.compose_public_scene_text(
        operation="scene_response",
        facts={},
        recent_public_messages=[],
        fallback_public_reply="牢门已经打开。",
    )

    assert result.public_reply == "牢门已经打开。"
    assert result.used_model is False
