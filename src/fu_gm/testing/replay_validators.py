from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from fu_gm.testing.replay_models import ReplayCallRecord, ReplayStep


@dataclass
class ReplayValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class ReplayValidators:
    INTERNAL_LEAK_TOKENS = [
        "Traceback",
        "KeyError",
        "is not a valid",
        "npc_action_type",
        "action_type",
        "commit",
        "内部恢复重试",
        "规则结算拦截：'",
    ]

    def validate_record(self, record: ReplayCallRecord, step: ReplayStep) -> ReplayValidationResult:
        result = ReplayValidationResult()
        reply = record.reply or ""
        if record.status >= 400 or not record.ok:
            result.errors.append(f"{step.label or step.id}: http_failed status={record.status}")
        for token in self.INTERNAL_LEAK_TOKENS:
            if token in reply:
                result.errors.append(f"{step.label or step.id}: leaked_internal_token={token}")
                break
        if re.search(r"\bDL\s*0\b|vs\s+DL\s+0|vs\s+0[:：，, ]", reply):
            result.errors.append(f"{step.label or step.id}: invalid_dl_zero")
        self._validate_dice_panel(reply, result, step)
        for expected in step.expected:
            if expected.startswith("reply_contains:"):
                needle = expected.split(":", 1)[1]
                if needle and needle not in reply:
                    result.errors.append(f"{step.label or step.id}: missing_reply_text={needle}")
            elif expected.startswith("status:"):
                expected_status = int(expected.split(":", 1)[1])
                if record.status != expected_status:
                    result.errors.append(
                        f"{step.label or step.id}: expected_status={expected_status}, got={record.status}"
                    )
            elif expected == "no_rules_blocked":
                body = record.body if isinstance(record.body, dict) else {}
                if body.get("rules_blocked"):
                    result.errors.append(f"{step.label or step.id}: unexpected_rules_blocked")
        return result

    def _validate_dice_panel(self, reply: str, result: ReplayValidationResult, step: ReplayStep) -> None:
        for line in reply.splitlines():
            if "掷骰" not in line:
                continue
            dice = re.findall(r"d\d+\s*=\s*\d+", line)
            if len(dice) > 2:
                result.errors.append(f"{step.label or step.id}: too_many_dice_in_panel={len(dice)}")
            if "大成功" in line:
                values = [int(match.split("=")[1]) for match in dice]
                if len(values) == 2 and not (values[0] == values[1] and values[0] >= 6):
                    result.errors.append(f"{step.label or step.id}: invalid_critical_success_text")
            if "大失败" in line:
                values = [int(match.split("=")[1]) for match in dice]
                if len(values) == 2 and values != [1, 1]:
                    result.errors.append(f"{step.label or step.id}: invalid_fumble_text")

    def validate_snapshot(self, snapshot: dict[str, Any]) -> ReplayValidationResult:
        result = ReplayValidationResult()
        phase = snapshot.get("phase") or {}
        if isinstance(phase, dict):
            current_scene = str(phase.get("current_scene") or "")
            if "Session 0" in current_scene and snapshot.get("session_ended"):
                result.errors.append("phase_stuck_on_session_zero_after_session_end")
        return result
