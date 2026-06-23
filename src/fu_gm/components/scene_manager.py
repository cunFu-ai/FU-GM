from __future__ import annotations

from fu_gm.models import SceneRecord, SceneType


class SceneManager:
    """管理冲突以外的场景骨架与场景历史。"""

    def __init__(self) -> None:
        self.current_scene: SceneRecord | None = None
        self.history: list[SceneRecord] = []

    def start_scene(
        self,
        name: str,
        scene_type: SceneType = SceneType.STANDARD,
        *,
        location: str = "",
        participants: list[str] | None = None,
        objective: str = "",
        summary: str = "",
    ) -> SceneRecord:
        if self.current_scene is not None:
            self.end_scene("场景被新的场景切换。")
        self.current_scene = SceneRecord(
            name=name,
            scene_type=scene_type,
            location=location,
            participants=list(participants or []),
            objective=objective,
            summary=summary,
        )
        return self.current_scene

    def end_scene(self, summary: str = "") -> SceneRecord | None:
        if self.current_scene is None:
            return None
        if summary:
            self.current_scene.summary = summary
        self.current_scene.active = False
        ended = self.current_scene
        self.history.append(ended)
        self.current_scene = None
        return ended

    def format_phase(self) -> str:
        if self.current_scene is None:
            return "自由场景"
        scene = self.current_scene
        type_text = {
            SceneType.STANDARD: "普通场景",
            SceneType.SESSION_ZERO: "Session 0 世界创建",
            SceneType.CONFLICT: "冲突场景",
            SceneType.INTERLUDE: "插曲场景",
            SceneType.GM: "GM场景",
            SceneType.REST: "休息场景",
            SceneType.TRAVEL: "旅行场景",
            SceneType.DUNGEON: "地下城场景",
        }[scene.scene_type]
        location = f"，地点：{scene.location}" if scene.location else ""
        objective = f"，目标：{scene.objective}" if scene.objective else ""
        return f"{type_text}（{scene.name}{location}{objective}）"
