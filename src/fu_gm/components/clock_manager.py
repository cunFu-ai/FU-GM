from __future__ import annotations

import re

from fu_gm.models import Clock, ClockChange


class ClockManager:
    AUTO_ADVANCE_TIMINGS = frozenset(
        {
            "action_round_end",
            "owner_turn_start",
            "owner_turn_end",
            "scene_beat",
            "failed_check",
            "rest",
            "session_end",
        }
    )

    def __init__(self) -> None:
        self._clocks: dict[str, Clock] = {}
        self._archived_clocks: list[Clock] = []
        self._current_scene_id: str = ""

    def add(self, clock: Clock) -> None:
        name = str(clock.name or "").strip()
        if not name:
            raise ValueError("命刻必须有名称。")
        if int(clock.max_segments or 0) <= 0:
            raise ValueError("命刻格数必须大于0。")
        if (
            "scene_intent_contract" in name
            or "意图契约" in name
            or "后台使用" in name
            or "不得原样输出" in name
        ):
            raise ValueError("后台控制信息不能被创建为命刻。")
        if name in self._clocks:
            raise ValueError(f"命刻【{name}】已经存在，不能覆盖当前进度。")
        clock.name = name
        clock.max_segments = int(clock.max_segments)
        clock.current = max(0, min(clock.max_segments, int(clock.current or 0)))
        declared_scope = str(clock.scope or "").strip()
        if not declared_scope and not self._current_scene_id:
            clock.scope = "campaign"
        else:
            clock.scope = self._normalize_scope(declared_scope, clock.clock_type)
        if clock.scope == "scene" and not clock.scene_id:
            clock.scene_id = self._current_scene_id
        if not clock.status:
            clock.status = "active"
        if clock.status not in {"resolved", "abandoned", "archived"}:
            if clock.current >= clock.max_segments:
                clock.status = "ready" if clock.clock_type == "ritual" else "fulfilled"
            elif clock.status in {"ready", "fulfilled"}:
                clock.status = "active"
        if clock.auto_advance:
            timing = self._clock_auto_advance_timing(clock)
            clock.auto_advance_timing = timing
            if timing == "action_round_end":
                # Legacy saves and early prompts used "每次行动后". Generic
                # automatic clocks use a complete action round, never one chat
                # message or one character action.
                clock.auto_advance = self._canonical_auto_advance_text(
                    clock.auto_advance
                )
            if timing in {"owner_turn_start", "owner_turn_end"}:
                clock.auto_advance_owner = str(
                    clock.auto_advance_owner
                    or self._owner_from_auto_advance_text(clock.auto_advance)
                    or (
                        clock.owner
                        if str(clock.owner or "").strip().lower()
                        not in {"", "gm", "游戏主持人"}
                        else ""
                    )
                ).strip()
                if not clock.auto_advance_owner:
                    raise ValueError("按角色回合推进的命刻必须指定触发角色。")
        self._clocks[clock.name] = clock

    def get(self, name: str) -> Clock:
        return self._clocks[self._resolve_name(name)]

    def exists(self, name: str) -> bool:
        return self._resolve_name(name) in self._clocks

    def advance(self, name: str, delta: int) -> tuple[int, int]:
        clock = self.get(name)
        if clock.status in {"resolved", "abandoned", "archived"}:
            raise ValueError(f"命刻【{clock.name}】已经结束，不能继续推进。")
        before = clock.current
        clock.current = max(0, min(clock.max_segments, clock.current + delta))
        if clock.current >= clock.max_segments:
            clock.status = "ready" if clock.clock_type == "ritual" else "fulfilled"
        elif clock.status in {"fulfilled", "ready"}:
            clock.status = "active"
        return before, clock.current

    def begin_scene(self, scene_id: str) -> None:
        self._current_scene_id = str(scene_id or "").strip()

    def end_scene(self, scene_id: str, *, reason: str = "场景结束") -> list[Clock]:
        """Archive scene-scoped clocks while preserving session/campaign clocks."""

        target = str(scene_id or self._current_scene_id or "").strip()
        archived: list[Clock] = []
        for name, clock in list(self._clocks.items()):
            if self._normalize_scope(clock.scope, clock.clock_type) != "scene":
                continue
            if target and clock.scene_id and clock.scene_id != target:
                continue
            clock.status = "archived"
            if reason:
                clock.resolution_note = reason
            archived.append(clock)
            self._archived_clocks.append(clock)
            del self._clocks[name]
        if not target or target == self._current_scene_id:
            self._current_scene_id = ""
        return archived

    def end_session(self, *, reason: str = "场次结束") -> list[Clock]:
        archived: list[Clock] = []
        for name, clock in list(self._clocks.items()):
            if self._normalize_scope(clock.scope, clock.clock_type) != "session":
                continue
            clock.status = "archived"
            clock.resolution_note = reason
            archived.append(clock)
            self._archived_clocks.append(clock)
            del self._clocks[name]
        return archived

    def resolve(self, name: str, *, note: str = "", archive: bool = False) -> Clock:
        clock = self.get(name)
        clock.status = "resolved"
        if note:
            clock.resolution_note = note
        if archive:
            self._archived_clocks.append(clock)
            del self._clocks[clock.name]
        return clock

    def abandon(self, name: str, *, note: str = "") -> Clock:
        clock = self.get(name)
        clock.status = "abandoned"
        if note:
            clock.resolution_note = note
        self._archived_clocks.append(clock)
        del self._clocks[clock.name]
        return clock

    def auto_advance_after_turn(
        self,
        *,
        skip_names: set[str] | None = None,
        allowed_names: set[str] | None = None,
        limit: int | None = None,
        event_timing: str = "action_round_end",
    ) -> list[ClockChange]:
        """Advance clocks whose fictional cadence matches the emitted event.

        ``action_round_end`` is emitted only after every participating PC has
        contributed a meaningful action in a free scene, or every base
        combatant has completed a turn in a conflict.  ``after_action`` remains
        a non-matching legacy event so an accidental per-message call cannot
        speed up an automatic clock again.
        """

        return self.emit_auto_advance_event(
            event_timing,
            skip_names=skip_names,
            allowed_names=allowed_names,
            limit=limit,
        )

    def emit_auto_advance_event(
        self,
        event_timing: str,
        *,
        actor: str = "",
        skip_names: set[str] | None = None,
        allowed_names: set[str] | None = None,
        limit: int | None = None,
    ) -> list[ClockChange]:
        """Advance clocks subscribed to one typed fictional-time event."""

        if self._normalize_auto_advance_timing(event_timing) == "after_action":
            # Keep this guard even though current orchestrators only emit
            # action_round_end.  It protects old integrations from restoring
            # the former per-message acceleration by calling this method after
            # each individual action.
            return []

        skip = skip_names or set()
        allowed = allowed_names
        event = self._normalize_auto_advance_timing(event_timing)
        changes: list[ClockChange] = []
        for clock in self.subscribed_auto_clocks(event, actor=actor):
            if clock.name in skip:
                continue
            if allowed is not None and clock.name not in allowed:
                continue
            if clock.current >= clock.max_segments:
                continue
            if limit is not None and len(changes) >= max(0, limit):
                break
            every = self._auto_advance_every(clock)
            clock.auto_advance_progress = max(0, int(clock.auto_advance_progress or 0)) + 1
            if clock.auto_advance_progress < every:
                continue
            clock.auto_advance_progress %= every
            delta = self._auto_advance_delta(clock.auto_advance)
            if delta == 0:
                continue
            before, after = self.advance(clock.name, delta)
            actual_delta = after - before
            if actual_delta == 0:
                continue
            changes.append(
                ClockChange(
                    clock_name=clock.name,
                    before=before,
                    after=after,
                    delta=actual_delta,
                    max_segments=clock.max_segments,
                    reason=f"自动推进：{clock.auto_advance}",
                    clock_type=clock.clock_type,
                    stakes=clock.stakes,
                    completion_consequence=clock.completion_consequence,
                )
            )
        return changes

    def subscribed_auto_clocks(
        self,
        event_timing: str,
        *,
        actor: str = "",
    ) -> list[Clock]:
        """Return active clocks subscribed to one typed timeline event."""

        event = self._normalize_auto_advance_timing(event_timing)
        clean_actor = str(actor or "").strip()
        subscribed: list[Clock] = []
        for clock in self._clocks.values():
            if not clock.auto_advance or clock.current >= clock.max_segments:
                continue
            if self._clock_auto_advance_timing(clock) != event:
                continue
            if event in {"owner_turn_start", "owner_turn_end"}:
                owner = str(clock.auto_advance_owner or "").strip()
                if not clean_actor or owner != clean_actor:
                    continue
            subscribed.append(clock)
        return subscribed

    def _clock_auto_advance_timing(self, clock: Clock) -> str:
        text = str(clock.auto_advance or "").strip().lower()
        declared = str(clock.auto_advance_timing or "").strip()
        normalized_declared = self._normalize_auto_advance_timing(declared)
        if declared and normalized_declared != "action_round_end":
            return normalized_declared
        if any(token in text for token in ("回合开始", "turn start", "turn_start")):
            return "owner_turn_start"
        if any(token in text for token in ("回合结束", "turn end", "turn_end")):
            return "owner_turn_end"
        if any(token in text for token in ("场景节拍", "scene beat", "scene_beat")):
            return "scene_beat"
        if any(token in text for token in ("检定失败", "failed check", "failed_check")):
            return "failed_check"
        if any(token in text for token in ("休息后", "休息时", "rest")):
            return "rest"
        if any(
            token in text
            for token in (
                "行动轮",
                "每轮",
                "轮结束",
                "每次行动",
                "有效行动",
                "行动后",
                "round end",
                "round_end",
                "turn",
                "action",
            )
        ):
            return "action_round_end"
        return self._normalize_auto_advance_timing(clock.auto_advance_timing)

    @staticmethod
    def _normalize_auto_advance_timing(value: str) -> str:
        normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        if normalized in {"after_action", "single_action", "每次行动"}:
            return "after_action"
        aliases = {
            "owner_turn_start": "owner_turn_start",
            "turn_start": "owner_turn_start",
            "角色回合开始": "owner_turn_start",
            "owner_turn_end": "owner_turn_end",
            "turn_end": "owner_turn_end",
            "角色回合结束": "owner_turn_end",
            "scene_beat": "scene_beat",
            "场景节拍": "scene_beat",
            "failed_check": "failed_check",
            "检定失败": "failed_check",
            "rest": "rest",
            "休息": "rest",
            "session_end": "session_end",
            "场次结束": "session_end",
        }
        if normalized in aliases:
            return aliases[normalized]
        if normalized in {
            "action_round",
            "action_round_end",
            "after_turn",
            "round",
            "round_end",
            "after_round",
            "每轮",
            "每轮结束",
            "行动轮",
            "行动轮结束",
        }:
            return "action_round_end"
        return "action_round_end"

    @staticmethod
    def _canonical_auto_advance_text(value: str) -> str:
        text = str(value or "").strip()
        replacements = (
            (r"每\s*次(?:有效)?行动(?:结束)?后", "每个行动轮结束时"),
            (r"每\s*个(?:有效)?行动(?:结束)?后", "每个行动轮结束时"),
            (r"单(?:次|个)行动(?:结束)?后", "每个行动轮结束时"),
        )
        for pattern, replacement in replacements:
            text = re.sub(pattern, replacement, text)
        return text

    @staticmethod
    def _owner_from_auto_advance_text(value: str) -> str:
        match = re.search(r"【(?P<owner>[^】]+)】[^。；]*回合", str(value or ""))
        return str(match.group("owner") if match else "").strip()

    @staticmethod
    def _auto_advance_every(clock: Clock) -> int:
        declared = max(1, int(clock.auto_advance_every or 1))
        text = str(clock.auto_advance or "")
        match = re.search(
            r"每\s*(?P<count>\d+)\s*(?:(?:次|个)?(?:有效)?行动|个?行动轮)",
            text,
        )
        if match:
            declared = max(declared, int(match.group("count")))
        return min(declared, 99)

    def formatted(self) -> list[str]:
        return [self.format_clock(clock) for clock in self._clocks.values()]

    def formatted_public(self, *, max_completed: int = 0) -> list[str]:
        """Render a player-facing clock board without repeating backstage chores."""

        unfinished = [
            clock
            for clock in self._clocks.values()
            if clock.current < clock.max_segments or clock.status == "ready"
        ]
        completed = [
            clock
            for clock in self._clocks.values()
            if clock.current >= clock.max_segments and clock.status != "ready"
        ]
        rendered = [self.format_clock(clock, public=True) for clock in unfinished]
        if max_completed > 0:
            rendered.extend(self.format_clock(clock, public=True) for clock in completed[:max_completed])
        if max_completed > 0 and len(completed) > max_completed:
            rendered.append(f"另有 {len(completed) - max_completed} 个已填满命刻等待结算后果。")
        return rendered

    def format_clock(self, clock: Clock, *, public: bool = False, include_hint: bool = True) -> str:
        """把命刻渲染成 GM/PL 每轮都能看懂的焦点。"""

        if public:
            base = f"【{clock.name}】{clock.current}/{clock.max_segments}"
            hint = self._public_pressure_hint(clock) if include_hint else ""
            return f"{base}。{hint}" if hint else base

        parts = [f"[{clock.name}] {clock.current}/{clock.max_segments}"]
        clock_type = self._clock_type_label(clock.clock_type)
        if clock_type:
            parts.append(clock_type)
        if clock.stakes:
            parts.append(f"赌注：{clock.stakes}")
        if clock.auto_advance and not public:
            parts.append(f"自动推进：{clock.auto_advance}")
        if clock.gm_note and not public:
            parts.append(f"提示：{clock.gm_note}")
        if clock.current >= clock.max_segments:
            if clock.status == "ready":
                parts.append("准备完成")
                if not public:
                    parts.append("等待施法者在其下个回合进行最终施法检定")
            else:
                parts.append("已填满")
                if not public:
                    parts.append("等待 GM 结算后果或移除")
        elif not public:
            remaining = clock.max_segments - clock.current
            if clock.clock_type in {"threat", "villain", "dungeon", "boss"}:
                parts.append(f"焦点：还剩 {remaining} 格，英雄会想阻止或倒转它")
            elif clock.clock_type in {"objective", "ritual"}:
                parts.append(f"焦点：还差 {remaining} 格，对手可能会阻止它")
        return "；".join(parts)

    def all(self) -> list[Clock]:
        """返回当前所有命刻的只读快照入口。"""

        return list(self._clocks.values())

    def archived(self) -> list[Clock]:
        return list(self._archived_clocks)

    def archived_match(self, name: str) -> Clock | None:
        """Return the newest archived clock matching a public name reference."""

        target = self._clean_name_reference(name)
        if not target:
            return None
        for clock in reversed(self._archived_clocks):
            if self._clean_name_reference(clock.name) == target:
                return clock
        return None

    def is_retired(self, name: str) -> bool:
        """Whether a completed clock name is a tombstone, not a free slot.

        Scene cleanup archives unfinished clocks too; those are intentionally
        not tombstones.  A resolved/fulfilled clock, however, represents a
        consequence that has already entered the fiction and must not silently
        restart at zero when a later LLM action repeats its name.
        """

        clock = self.archived_match(name)
        if clock is None:
            return False
        return clock.status in {"resolved", "fulfilled"} or (
            clock.max_segments > 0 and clock.current >= clock.max_segments
        )

    def _normalize_scope(self, scope: str, clock_type: str) -> str:
        normalized = str(scope or "").strip().lower()
        aliases = {
            "场景": "scene",
            "本场景": "scene",
            "阶段": "session",
            "场次": "session",
            "章节": "session",
            "战役": "campaign",
            "长期": "campaign",
            "永久": "campaign",
        }
        normalized = aliases.get(normalized, normalized)
        if normalized in {"scene", "session", "campaign"}:
            return normalized
        if str(clock_type or "").strip() == "villain":
            return "campaign"
        return "scene"

    def _auto_advance_delta(self, text: str) -> int:
        normalized = str(text or "").strip().lower()
        if not normalized or any(token in normalized for token in ("不自动", "手动", "关闭", "暂停", "none", "false", "off")):
            return 0
        directional = re.search(
            r"(?:推进|填充|增加|前进|擦除|倒转|倒退|回退|减少)\s*(?P<amount>\d+)\s*格?",
            normalized,
        )
        match = directional or re.search(r"(?P<amount>\d+)\s*格(?:$|[^次个])", normalized)
        amount = int(match.group("amount")) if match else 1
        amount = max(0, min(amount, 99))
        if any(token in normalized for token in ("擦除", "倒转", "倒退", "回退", "减少", "-")):
            return -amount
        return amount

    def _resolve_name(self, name: str) -> str:
        text = str(name or "").strip()
        if text in self._clocks:
            return text
        # LLM 常会从面板里复制 "[命刻名] 0/6" 或 "【命刻名】"；这里统一还原成真实键名。
        bracket_pairs = (("[", "]"), ("【", "】"))
        for left, right in bracket_pairs:
            if text.startswith(left) and right in text:
                candidate = text[len(left) : text.index(right)].strip()
                if candidate in self._clocks:
                    return candidate
        if " " in text:
            candidate = text.split(" ", 1)[0].strip()
            for left, right in bracket_pairs:
                if candidate.startswith(left) and candidate.endswith(right):
                    candidate = candidate[len(left) : -len(right)].strip()
                    break
            if candidate in self._clocks:
                return candidate
        for separator in ("；", ";", "（", "("):
            if separator in text:
                candidate = text.split(separator, 1)[0].strip()
                if candidate in self._clocks:
                    return candidate
                for left, right in bracket_pairs:
                    if candidate.startswith(left) and right in candidate:
                        inner = candidate[len(left) : candidate.index(right)].strip()
                        if inner in self._clocks:
                            return inner
        return text

    @staticmethod
    def _clean_name_reference(name: str) -> str:
        text = str(name or "").strip()
        for left, right in (("[", "]"), ("【", "】")):
            if text.startswith(left) and right in text:
                text = text[len(left) : text.index(right)].strip()
                break
        text = re.sub(r"\s+\d+\s*/\s*\d+.*$", "", text).strip()
        return text

    def _clock_type_label(self, clock_type: str) -> str:
        labels = {
            "objective": "目标命刻",
            "threat": "威胁命刻",
            "ritual": "仪式命刻",
            "villain": "反派命刻",
            "dungeon": "地下城危机命刻",
            "boss": "首领机制命刻",
        }
        return labels.get(str(clock_type or "").strip(), str(clock_type or "").strip())

    def _public_pressure_hint(self, clock: Clock) -> str:
        """玩家前台只需要感到压力，不需要看到命刻后台字段。"""

        current = int(clock.current or 0)
        max_segments = max(1, int(clock.max_segments or 0))
        remaining = max_segments - current
        clock_type = str(clock.clock_type or "").strip()
        is_pressure = clock_type in {"threat", "villain", "dungeon", "boss"}
        is_goal = clock_type in {"objective", "ritual"}
        if current >= max_segments:
            if is_pressure:
                return self._pressure_sentence(clock, completed=True)
            if clock_type == "ritual":
                return "仪式的准备已经抵达临界点。"
            if is_goal:
                return "这件事已经到了可以收束的时刻。"
            return "已经填满。"
        if is_pressure and remaining <= max(1, max_segments // 4):
            return self._pressure_sentence(clock, completed=False)
        if is_goal and remaining == 1:
            if clock_type == "ritual":
                return "只差最后一点回响，仪式就能成形。"
            return "只差最后一步。"
        return ""

    def _pressure_sentence(self, clock: Clock, *, completed: bool) -> str:
        text = " ".join(
            str(part or "")
            for part in (clock.name, clock.stakes, clock.gm_note, clock.auto_advance)
        )
        if any(token in text for token in ("巡逻", "追兵", "包围", "封锁", "脚步")):
            return "再拖下去，他们就会包围现场！" if not completed else "他们已经压到现场边缘。"
        if any(token in text for token in ("潮", "水", "海", "淹", "没顶")):
            return "水声已经漫过退路边缘。" if not completed else "潮水已经封住最危险的方向。"
        if any(token in text for token in ("警报", "铃", "钟", "号角")):
            return "警报声越来越近。" if not completed else "警报已经把整片现场惊醒。"
        if any(token in text for token in ("权杖", "魔法", "法术", "蓄力", "仪式", "咒")):
            return "魔力已经满溢，下一瞬就可能爆开！" if not completed else "那股魔力已经越过临界。"
        if any(token in text for token in ("崩", "塌", "火", "爆", "裂")):
            return "危险正在失控。" if not completed else "危险已经真正爆发。"
        return "危险已经逼到眼前。" if not completed else "威胁已经兑现。"
