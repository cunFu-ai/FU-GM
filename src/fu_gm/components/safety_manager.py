from __future__ import annotations

from fu_gm.components.world_state import WorldState
from fu_gm.models import SafetyDeclarationResult
from fu_gm.safety_parser import clean_safety_item, extract_safety_declarations, normalize_safety_type


class SafetyManager:
    """管理界限与帷幕，并把它们写回世界状态。"""

    def __init__(self, world_state: WorldState) -> None:
        self.world_state = world_state

    def declare_line(self, item: str, *, speaker: str = "", anonymous: bool = False) -> SafetyDeclarationResult:
        return self.declare("line", item, speaker=speaker, anonymous=anonymous)

    def declare_veil(self, item: str, *, speaker: str = "", anonymous: bool = False) -> SafetyDeclarationResult:
        return self.declare("veil", item, speaker=speaker, anonymous=anonymous)

    def declare(
        self,
        declaration_type: str,
        item: str,
        *,
        speaker: str = "",
        anonymous: bool = False,
    ) -> SafetyDeclarationResult:
        clean_item = clean_safety_item(item)
        kind = normalize_safety_type(declaration_type)
        public_speaker = "" if anonymous else speaker
        if not clean_item:
            return SafetyDeclarationResult(
                declaration_type=kind,
                item="",
                speaker=public_speaker,
                anonymous=anonymous,
                accepted=False,
                message="我听到了你想设置界限与帷幕，但还需要一个具体元素。",
                guidance=self.render_guidance(),
            )

        target = self.world_state.world_profile.safety_lines if kind == "line" else self.world_state.world_profile.safety_veils
        self._append_unique(target, clean_item)
        self._sync_world_sheet(kind, clean_item)
        label = "界限" if kind == "line" else "帷幕"
        if anonymous:
            memory_prefix = "匿名玩家声明"
        else:
            memory_prefix = f"{speaker}声明" if speaker else "已声明"
        self.world_state._add_memory_once(f"{memory_prefix}{label}：{clean_item}")
        message = self._confirmation_message(kind, clean_item)
        return SafetyDeclarationResult(
            declaration_type=kind,
            item=clean_item,
            speaker=public_speaker,
            anonymous=anonymous,
            accepted=True,
            message=message,
            guidance=self.render_guidance(),
        )

    def parse_and_declare(
        self,
        speaker: str,
        message: str,
        *,
        anonymous: bool = False,
    ) -> list[SafetyDeclarationResult]:
        results: list[SafetyDeclarationResult] = []
        seen: set[tuple[str, str]] = set()
        for kind, item in extract_safety_declarations(message):
            clean_item = clean_safety_item(item)
            if not clean_item or (kind, clean_item) in seen:
                continue
            seen.add((kind, clean_item))
            results.append(self.declare(kind, clean_item, speaker=speaker, anonymous=anonymous))
        return results

    def render_guidance(self) -> str:
        lines = self.world_state.world_profile.safety_lines
        veils = self.world_state.world_profile.safety_veils
        if not lines and not veils:
            return "界限与帷幕：尚未记录。若玩家声明相关内容，只确认处理方式，不追问原因。"
        rendered_lines = "；".join(lines) if lines else "无"
        rendered_veils = "；".join(veils) if veils else "无"
        return (
            "界限与帷幕规则：不要追问玩家为什么不舒服，只确认从现在开始如何处理。\n"
            f"界限（绝不出现、视为未发生且故事不再提及）：{rendered_lines}\n"
            f"帷幕（可以存在并影响角色，但不得明确描写或聚焦，发生在幕后）：{rendered_veils}"
        )

    def review_content(self, content: str) -> dict[str, list[str]]:
        return {
            "line_conflicts": [item for item in self.world_state.world_profile.safety_lines if item and item in content],
            "veil_matches": [item for item in self.world_state.world_profile.safety_veils if item and item in content],
        }

    def _normalize_type(self, declaration_type: str) -> str:
        return normalize_safety_type(declaration_type)

    def _confirmation_message(self, kind: str, item: str) -> str:
        if kind == "line":
            return f"已记录为界限：{item}。这个元素不会出现在游戏中，也不会被当作已经发生过的事实提及。"
        return f"已记录为帷幕：{item}。这个元素如果存在，会发生在幕后，不会被明确描写或带到聚光灯下。"

    def _sync_world_sheet(self, kind: str, item: str) -> None:
        if self.world_state.world_sheet is None:
            return
        target = self.world_state.world_sheet.safety_lines if kind == "line" else self.world_state.world_sheet.safety_veils
        self._append_unique(target, item)

    def _append_unique(self, target: list[str], item: str) -> None:
        if item and item not in target:
            target.append(item)

    def _clean_item(self, item: str) -> str:
        return clean_safety_item(item)
