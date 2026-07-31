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
        "AI GM（assistant）",
        "当前玩家输入（只把这一段当作本轮新行动",
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
            elif expected.startswith("reply_not_contains:"):
                needle = expected.split(":", 1)[1]
                if needle and needle in reply:
                    result.errors.append(f"{step.label or step.id}: forbidden_reply_text={needle}")
            elif expected.startswith("status:"):
                expected_status = int(expected.split(":", 1)[1])
                if record.status != expected_status:
                    result.errors.append(
                        f"{step.label or step.id}: expected_status={expected_status}, got={record.status}"
                    )
            elif expected.startswith("target:"):
                expected_target = expected.split(":", 1)[1].strip()
                body = record.body if isinstance(record.body, dict) else {}
                actual_target = str(body.get("target") or "")
                if actual_target != expected_target:
                    result.errors.append(
                        f"{step.label or step.id}: expected_target={expected_target}, got={actual_target or 'missing'}"
                    )
            elif expected.startswith("send_reply:"):
                expected_bool = expected.split(":", 1)[1].strip().lower() in {"1", "true", "yes"}
                body = record.body if isinstance(record.body, dict) else {}
                actual_bool = bool(body.get("send_reply"))
                if actual_bool != expected_bool:
                    result.errors.append(
                        f"{step.label or step.id}: expected_send_reply={expected_bool}, got={actual_bool}"
                    )
            elif expected.startswith("decision_mode:"):
                expected_mode = expected.split(":", 1)[1].strip()
                body = record.body if isinstance(record.body, dict) else {}
                decision = body.get("decision") if isinstance(body.get("decision"), dict) else {}
                actual_mode = str(decision.get("mode") or "")
                if actual_mode != expected_mode:
                    result.errors.append(
                        f"{step.label or step.id}: expected_decision_mode={expected_mode}, got={actual_mode or 'missing'}"
                    )
            elif expected.startswith("decision_tag:"):
                expected_tag = expected.split(":", 1)[1].strip()
                body = record.body if isinstance(record.body, dict) else {}
                decision = body.get("decision") if isinstance(body.get("decision"), dict) else {}
                tags = decision.get("tags") if isinstance(decision.get("tags"), list) else []
                if expected_tag not in tags:
                    result.errors.append(f"{step.label or step.id}: missing_decision_tag={expected_tag}")
            elif expected == "no_rules_blocked":
                body = record.body if isinstance(record.body, dict) else {}
                if body.get("rules_blocked"):
                    result.errors.append(f"{step.label or step.id}: unexpected_rules_blocked")
            elif expected == "not_blocked":
                body = record.body if isinstance(record.body, dict) else {}
                if body.get("blocked"):
                    result.errors.append(f"{step.label or step.id}: unexpected_blocked")
            elif expected.startswith("gate_status:"):
                expected_status = expected.split(":", 1)[1].strip()
                body = record.body if isinstance(record.body, dict) else {}
                gate = body.get("gate") if isinstance(body.get("gate"), dict) else {}
                actual_status = str(gate.get("status") or "")
                if actual_status != expected_status:
                    result.errors.append(
                        f"{step.label or step.id}: expected_gate_status={expected_status}, got={actual_status or 'missing'}"
                    )
        if step.kind == "game_turn" and "allow_rules_blocked" not in step.expected:
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
