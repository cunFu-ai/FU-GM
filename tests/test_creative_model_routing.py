from __future__ import annotations

import tempfile
from types import SimpleNamespace

from fu_gm.components.scene_creative_writer import PublicSceneComposition
from fu_gm.gm_tool_contracts import GMToolExecutionContext
from fu_gm.http_server import FUGMHttpService


class RecordingCreativeWriter:
    available = True

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.agency_calls: list[dict[str, object]] = []

    def compose_public_scene_text(self, **kwargs: object) -> PublicSceneComposition:
        self.calls.append(dict(kwargs))
        operation = str(kwargs.get("operation") or "")
        facts = dict(kwargs.get("facts") or {})
        if operation == "clock_change":
            reply = f"远处的警铃骤然响起。\n{facts['progress_marker']}"
        else:
            public_facts = list(facts.get("public_facts") or [])
            reply = "\n".join(["雨水从松开的门轴上滴落。", *public_facts])
        return PublicSceneComposition(
            public_reply=reply,
            model="deepseek-v4-flash",
            used_model=True,
        )

    def validate_player_agency(self, **kwargs: object) -> None:
        self.agency_calls.append(dict(kwargs))


def context(message: str) -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="creative-routing",
        session_id="s1",
        channel_id="group-1",
        speaker="阿凛",
        gate_status="adventure",
        directly_addressed=True,
        metadata={
            "current_message": message,
            "recent_public_context": "众人站在卡里巴村监狱的走廊里。",
        },
    )


def test_exact_scene_followup_bypasses_creative_rewrite() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        runtime = service._runtime("creative-routing")
        runtime.app.scene_manager.start_scene(
            "卡里巴村监狱",
            location="牢房走廊",
            participants=["诺艾尔", "艾丽妮"],
        )
        writer = RecordingCreativeWriter()
        runtime.app.scene_creative_writer = writer

        tool_context = context("我把牢门推开。")
        tool_context.metadata["_gm_agent_required_followup_context"] = {
            "source_tool": "perform_scene_action",
            "required_tools": ["commit_scene_response"],
            "scene_response_followup": {
                "public_reply": "牢门已经打开。",
                "public_facts": ["牢门已经打开。"],
            },
        }
        receipt = service.gm_scene_tools.commit_scene_response(
            tool_context,
            {
                "public_facts": ["牢门已经打开。"],
                "creative_direction": "让门轴的声音带出紧张感",
                "evidence": "我把牢门推开。",
            },
        )

        assert receipt.ok is True
        assert receipt.lock_public_reply is True
        assert receipt.public_fallback_reply == "牢门已经打开。"
        assert receipt.result["creative_author"] == {}
        assert writer.calls == []


def test_complete_core_authored_scene_opening_bypasses_second_writer_call() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        runtime = service._runtime("creative-routing")
        runtime.app.scene_manager.start_scene(
            "鸣钟驿站地下室",
            location="旧档案库",
            participants=["伊莉雅", "赛璃"],
        )
        writer = RecordingCreativeWriter()
        runtime.app.scene_creative_writer = writer
        tool_context = context("系统场景开场请求")
        tool_context.speaker = "系统主动节拍"
        tool_context.metadata.update(
            {
                "system_gm_beat_request": True,
                "gm_authored_scene_opening": True,
                "heartbeat_action": "scene_opening",
            }
        )
        fact = "霍恩推开木门，露出堆满旧卷宗的储藏室。"

        receipt = service.gm_scene_tools.commit_scene_response(
            tool_context,
            {
                "public_reply": f"油灯照亮门后的灰尘。{fact}",
                "public_facts": [fact],
                "awaits_player_response": True,
                "evidence": "系统场景开场请求",
            },
        )

        assert receipt.ok is True
        assert receipt.public_fallback_reply == f"油灯照亮门后的灰尘。{fact}"
        assert receipt.result["creative_author"]["author"] == "core_gm"
        assert receipt.result["creative_author"]["scene_writer_bypassed"] is True
        assert writer.calls == []
        assert len(writer.agency_calls) == 1


def test_semantically_grounded_scene_opening_commits_canonical_fact() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        runtime = service._runtime("creative-routing")
        runtime.app.scene_manager.start_scene(
            "第七采掘城升降台",
            location="矿道升降台",
            participants=["伊莉雅", "赛璃"],
        )
        fact = "升降台正在缓缓上升，带着英雄们离开矿道深处，向采掘城的上层移动。"
        runtime.app.scene_creative_writer = SimpleNamespace(
            available=True,
            compose_public_scene_text=lambda **_kwargs: PublicSceneComposition(
                public_reply="升降台离开矿道深处，正朝采掘城上层升去，两名英雄仍站在平台上。",
                grounded_public_facts=(fact,),
                model="deepseek-v4-flash",
                used_model=True,
            ),
            validate_player_agency=lambda **_kwargs: None,
        )
        tool_context = context("系统场景开场请求")
        tool_context.speaker = "系统主动节拍"
        tool_context.metadata.update(
            {
                "system_gm_beat_request": True,
                "gm_authored_scene_opening": True,
                "heartbeat_action": "scene_opening",
            }
        )

        receipt = service.gm_scene_tools.commit_scene_response(
            tool_context,
            {
                "public_reply": "平台正在上升。",
                "public_facts": [fact],
                "evidence": "系统场景开场请求",
            },
        )

        assert receipt.ok is True
        assert receipt.result["public_facts"] == [fact]
        assert fact in runtime.app.scene_frame_manager.current_frame.public_facts


def test_foreground_clock_prose_uses_creative_writer_but_python_owns_progress() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        runtime = service._runtime("creative-routing")
        runtime.app.scene_manager.start_scene(
            "卡里巴村监狱",
            location="牢房走廊",
            participants=["诺艾尔", "艾丽妮"],
        )
        writer = RecordingCreativeWriter()
        runtime.app.scene_creative_writer = writer
        message = "卫兵已经听见牢门的响动。"

        receipt = service.gm_clock_tools.create_clock(
            context(message),
            {
                "name": "监狱进入全面警戒",
                "segments": 6,
                "clock_type": "threat",
                "scope": "scene",
                "stakes": "守卫逐步封锁出口",
                "completion_consequence": "所有通往村外的门都被守卫封住",
                "auto_advance": True,
                "auto_advance_timing": "action_round_end",
                "auto_advance_every": 1,
                "advance_on_rest": False,
                "visibility": "foreground",
                "creative_direction": "用逐层亮起的警灯表现",
                "evidence": message,
            },
        )

        assert receipt.ok is True
        assert "【监狱进入全面警戒】0/6" in receipt.public_fallback_reply
        assert runtime.app.clock_manager.get("监狱进入全面警戒").current == 0
        assert receipt.result["creative_author"]["model"] == "deepseek-v4-flash"
