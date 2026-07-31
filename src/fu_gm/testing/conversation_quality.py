from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from statistics import median
from typing import Any, Iterable

from fu_gm.components.clock_narrative_boundary import ClockNarrativeBoundary
from fu_gm.testing.session_progress_evaluator import SessionProgressAssessment


@dataclass
class ConversationQualityReport:
    total_calls: int = 0
    gm_reply_count: int = 0
    p50_latency_ms: int = 0
    p95_latency_ms: int = 0
    max_latency_ms: int = 0
    exact_long_reply_repetitions: int = 0
    near_duplicate_gm_replies: int = 0
    embedded_prior_gm_replays: int = 0
    player_echo_count: int = 0
    player_echo_rate: float = 0.0
    expected_silence_count: int = 0
    correct_silence_count: int = 0
    actual_silence_count: int = 0
    silence_recall: float = 1.0
    silence_precision: float = 1.0
    expected_reply_count: int = 0
    correct_reply_count: int = 0
    reply_recall: float = 1.0
    incorrect_silence_count: int = 0
    unnecessary_reply_count: int = 0
    unnecessary_reply_rate: float = 0.0
    npc_answer_failures: int = 0
    npc_personality_failures: int = 0
    agency_violations: int = 0
    agency_preservation_rate: float = 1.0
    continuity_failures: int = 0
    continuity_success_rate: float = 1.0
    irreversible_state_regressions: int = 0
    fulfilled_promise_reopens: int = 0
    npc_commitment_violations: int = 0
    retired_clock_reappearances: int = 0
    contradictory_check_responses: int = 0
    repeated_player_action_lanes: int = 0
    cause_effect_failures: int = 0
    gm_control_failures: int = 0
    indistinct_session_count: int = 0
    irrelevant_gm_response_sessions: int = 0
    complete_memory_anchors: int = 0
    opposition_move_session_count: int = 0
    opening_signature_present_count: int = 0
    concrete_npc_agenda_session_count: int = 0
    signature_image_evolved_count: int = 0
    local_payoff_session_count: int = 0
    previous_consequence_callback_count: int = 0
    earned_session_closure_count: int = 0
    repeated_loop_session_count: int = 0
    max_memory_anchor_similarity: float = 0.0
    mean_memory_anchor_similarity: float = 0.0
    high_similarity_anchor_pairs: list[dict[str, Any]] = field(default_factory=list)
    vague_placeholder_gm_outputs: int = 0
    premature_clock_consequences: int = 0
    backstage_instruction_leaks: int = 0
    successful_state_tool_receipts: int = 0
    failed_tool_receipts: int = 0
    tool_validation_rejections: int = 0
    agent_output_retry_failures: int = 0
    tool_retry_recoveries: int = 0
    core_agent_unavailable_count: int = 0
    public_state_change_claims: int = 0
    unbacked_state_change_claims: int = 0
    failed_tool_success_claims: int = 0
    knowledge_action_consistency_rate: float = 1.0

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["high_similarity_anchor_pairs"] = list(self.high_similarity_anchor_pairs)
        return payload


class ConversationQualityAuditor:
    """Stable quantitative checks complementing offline session evaluation."""

    _SPACE = re.compile(r"[\s\W_]+", re.UNICODE)

    def audit(
        self,
        calls: Iterable[dict[str, Any]],
        assessments: Iterable[SessionProgressAssessment] = (),
    ) -> ConversationQualityReport:
        rows = list(calls)
        # Long-run harnesses also retain audit/dashboard requests.  Those
        # payloads intentionally contain rejected draft narrations and private
        # diagnostics; treating them as chat would turn an internal repair into
        # a false player-facing quality failure.
        public_rows = [row for row in rows if self._is_player_visible_row(row)]
        semantic = list(assessments)
        latencies = sorted(max(0, int(row.get("elapsed_ms") or 0)) for row in rows)
        replies = [
            str(row.get("reply") or "").strip()
            for row in public_rows
            if str(row.get("reply") or "").strip()
        ]
        echo_count = sum(1 for row in public_rows if self._echoes_player(row))
        evaluated_route_rows = [
            row for row in public_rows if self._has_explicit_reply_expectation(row)
        ]
        silence_rows = [row for row in evaluated_route_rows if self._expects_silence(row)]
        reply_rows = [row for row in evaluated_route_rows if self._expects_reply(row)]
        correct_silence = sum(1 for row in silence_rows if self._is_silent(row))
        actual_silence = sum(1 for row in evaluated_route_rows if self._is_silent(row))
        correct_reply = sum(1 for row in reply_rows if not self._is_silent(row))
        normalized_long = [self._normalize(reply) for reply in replies if len(self._normalize(reply)) >= 45]
        repeated = sum(count - 1 for count in Counter(normalized_long).values() if count > 1)
        semantic_count = len(semantic)
        agency_violations = sum(1 for item in semantic if not item.player_agency_preserved)
        continuity_failures = sum(1 for item in semantic if not item.continuity_ok)
        state_regressions = self._irreversible_state_regressions(public_rows)
        promise_reopens = self._fulfilled_promise_reopens(public_rows)
        commitment_violations = self._npc_commitment_violations(public_rows)
        clock_reopens = self._retired_clock_reappearances(public_rows)
        contradictory_checks = self._contradictory_check_responses(public_rows)
        repeated_action_lanes = self._repeated_player_action_lanes(public_rows)
        continuity_failures += state_regressions + promise_reopens + commitment_violations + clock_reopens
        unnecessary_replies = len(silence_rows) - correct_silence
        incorrect_silence = len(reply_rows) - correct_reply
        tool_metrics = self._tool_consistency_metrics(public_rows)
        anchor_similarities, similar_pairs = self._memory_anchor_similarities(semantic)
        return ConversationQualityReport(
            total_calls=len(rows),
            gm_reply_count=len(replies),
            p50_latency_ms=self._percentile(latencies, 0.50),
            p95_latency_ms=self._percentile(latencies, 0.95),
            max_latency_ms=max(latencies, default=0),
            exact_long_reply_repetitions=repeated,
            near_duplicate_gm_replies=self._near_duplicate_gm_replies(public_rows),
            embedded_prior_gm_replays=self._embedded_prior_gm_replays(public_rows),
            player_echo_count=echo_count,
            player_echo_rate=(echo_count / len(replies)) if replies else 0.0,
            expected_silence_count=len(silence_rows),
            correct_silence_count=correct_silence,
            actual_silence_count=actual_silence,
            silence_recall=(correct_silence / len(silence_rows)) if silence_rows else 1.0,
            silence_precision=(
                correct_silence / actual_silence if actual_silence else 1.0
            ),
            expected_reply_count=len(reply_rows),
            correct_reply_count=correct_reply,
            reply_recall=(correct_reply / len(reply_rows)) if reply_rows else 1.0,
            incorrect_silence_count=incorrect_silence,
            unnecessary_reply_count=unnecessary_replies,
            unnecessary_reply_rate=(
                unnecessary_replies / len(silence_rows)
                if silence_rows
                else 0.0
            ),
            npc_answer_failures=sum(1 for item in semantic if not item.npc_answer_complete),
            npc_personality_failures=sum(
                1 for item in semantic if not item.npc_personality_consistent
            ),
            agency_violations=agency_violations,
            agency_preservation_rate=(
                (semantic_count - agency_violations) / semantic_count
                if semantic_count
                else 1.0
            ),
            continuity_failures=continuity_failures,
            continuity_success_rate=(
                max(0, semantic_count - continuity_failures) / semantic_count
                if semantic_count
                else (1.0 if continuity_failures == 0 else 0.0)
            ),
            irreversible_state_regressions=state_regressions,
            fulfilled_promise_reopens=promise_reopens,
            npc_commitment_violations=commitment_violations,
            retired_clock_reappearances=clock_reopens,
            contradictory_check_responses=contradictory_checks,
            repeated_player_action_lanes=repeated_action_lanes,
            cause_effect_failures=sum(1 for item in semantic if not item.cause_effect_linked),
            gm_control_failures=sum(1 for item in semantic if not item.gm_control_present),
            indistinct_session_count=sum(
                1 for item in semantic if not item.session_identity_distinct
            ),
            irrelevant_gm_response_sessions=sum(
                1 for item in semantic if not item.gm_response_relevant
            ),
            complete_memory_anchors=sum(1 for item in semantic if item.memory_anchor_complete),
            opposition_move_session_count=sum(
                1 for item in semantic if item.opposition_move_present
            ),
            opening_signature_present_count=sum(
                1 for item in semantic if item.opening_signature_present
            ),
            concrete_npc_agenda_session_count=sum(
                1 for item in semantic if item.concrete_npc_agenda_present
            ),
            signature_image_evolved_count=sum(
                1 for item in semantic if item.signature_image_evolved
            ),
            local_payoff_session_count=sum(
                1 for item in semantic if item.local_payoff_present
            ),
            previous_consequence_callback_count=sum(
                1 for item in semantic if item.previous_consequence_recalled
            ),
            earned_session_closure_count=sum(
                1
                for item in semantic
                if item.memory_anchor_complete
                and item.local_payoff_present
                and item.opposition_move_present
                and item.signature_image_evolved
                and (
                    item.local_question_changed
                    or item.local_question_resolved
                    or (item.deliberate_cliffhanger and item.reversal_reached)
                )
            ),
            repeated_loop_session_count=sum(
                1 for item in semantic if item.repeated_loop_detected
            ),
            max_memory_anchor_similarity=max(anchor_similarities, default=0.0),
            mean_memory_anchor_similarity=(
                sum(anchor_similarities) / len(anchor_similarities)
                if anchor_similarities
                else 0.0
            ),
            high_similarity_anchor_pairs=similar_pairs,
            vague_placeholder_gm_outputs=self._vague_placeholder_gm_outputs(public_rows),
            premature_clock_consequences=self._premature_clock_consequences(public_rows),
            backstage_instruction_leaks=self._backstage_instruction_leaks(public_rows),
            **tool_metrics,
        )

    @staticmethod
    def _is_player_visible_row(row: dict[str, Any]) -> bool:
        """Exclude inspection endpoints and explicitly private trace entries.

        A missing route remains public by default for small unit fixtures and
        legacy transcripts.  This is intentionally a narrow filter: game
        messages, scene openings, and GM beats must continue to be audited.
        """

        if row.get("player_visible") is False or row.get("visibility") == "private":
            return False
        route = str(row.get("route") or "").strip()
        return not route.startswith(("/v1/audit/", "/v1/dashboard", "/health"))

    @staticmethod
    def _has_explicit_reply_expectation(row: dict[str, Any]) -> bool:
        if "expected_send_reply" in row or "expected_target" in row:
            return True
        return "玩家自由讨论" in str(row.get("label") or "")

    @staticmethod
    def _expects_silence(row: dict[str, Any]) -> bool:
        if "expected_send_reply" in row:
            return not bool(row.get("expected_send_reply"))
        if "expected_target" in row:
            return str(row.get("expected_target") or "") != "fu_gm"
        return "玩家自由讨论" in str(row.get("label") or "")

    @staticmethod
    def _expects_reply(row: dict[str, Any]) -> bool:
        if "expected_send_reply" in row:
            return bool(row.get("expected_send_reply"))
        return str(row.get("expected_target") or "") == "fu_gm"

    @classmethod
    def _tool_consistency_metrics(cls, rows: list[dict[str, Any]]) -> dict[str, object]:
        successful_state_receipts = 0
        failed_receipts = 0
        validation_rejections = 0
        agent_output_failures = 0
        retry_recoveries = 0
        core_agent_unavailable = 0
        public_claims = 0
        unbacked_claims = 0
        failed_tool_success_claims = 0
        agent_output_error_codes = {
            "NPC_DECISION_FAILED",
            "NPC_DECISION_INVALID",
            "NPC_SPEECH_PLAN_REQUIRED",
            "NPC_PUBLIC_REPLY_REQUIRED",
        }
        non_validation_error_codes = agent_output_error_codes | {
            "STALE_AGENT_REQUEST",
            "TOOL_EXECUTION_FAILED",
            "TOOL_TRANSACTION_START_FAILED",
            "TOOL_ROLLBACK_FAILED",
            "TOOL_COMMIT_FAILED",
            "INVALID_TOOL_RECEIPT",
        }

        for row in rows:
            body = row.get("body") if isinstance(row.get("body"), dict) else {}
            receipts = [
                receipt
                for receipt in (body.get("tool_receipts") or [])
                if isinstance(receipt, dict)
            ]
            successful = [
                receipt
                for receipt in receipts
                if bool(receipt.get("ok")) and bool(receipt.get("state_changed"))
            ]
            failures = [receipt for receipt in receipts if not bool(receipt.get("ok"))]
            successful_state_receipts += len(successful)
            failed_receipts += len(failures)
            row_agent_output_failures = sum(
                1
                for receipt in failures
                if str(receipt.get("error_code") or "")
                in agent_output_error_codes
            )
            row_validation_rejections = sum(
                1
                for receipt in failures
                if str(receipt.get("error_code") or "")
                and str(receipt.get("error_code") or "") not in non_validation_error_codes
            )
            agent_output_failures += row_agent_output_failures
            validation_rejections += row_validation_rejections
            if failures and successful:
                retry_recoveries += 1
            route = str(body.get("route") or "")
            if (
                route.startswith("gm_agent_unavailable")
                or (
                    str(body.get("agent_error") or "").strip()
                    and not successful
                    and not str(body.get("reply") or "").strip()
                )
            ):
                core_agent_unavailable += 1

            reply = str(row.get("reply") or body.get("reply") or "").strip()
            agent_managed = (
                "tool_receipts" in body
                or "agent_trace" in body
                or str(body.get("route") or "").startswith("gm_agent")
            )
            claims_change = agent_managed and cls._claims_public_state_change(reply)
            if claims_change:
                public_claims += 1
                if not successful:
                    unbacked_claims += 1
                    if failures:
                        failed_tool_success_claims += 1

        return {
            "successful_state_tool_receipts": successful_state_receipts,
            "failed_tool_receipts": failed_receipts,
            "tool_validation_rejections": validation_rejections,
            "agent_output_retry_failures": agent_output_failures,
            "tool_retry_recoveries": retry_recoveries,
            "core_agent_unavailable_count": core_agent_unavailable,
            "public_state_change_claims": public_claims,
            "unbacked_state_change_claims": unbacked_claims,
            "failed_tool_success_claims": failed_tool_success_claims,
            "knowledge_action_consistency_rate": (
                (public_claims - unbacked_claims) / public_claims
                if public_claims
                else 1.0
            ),
        }

    @staticmethod
    def _claims_public_state_change(reply: str) -> bool:
        """Detect narrow, player-facing claims that should have a write receipt."""

        return bool(
            re.search(
                r"(?:"
                r"(?:已经|已|刚刚)(?:记录|记下|写入|保存|读档|载入|新建|删除|归档|更新|修改|创建)"
                r"|(?:命刻【[^】]+】|【[^】]+】)(?:变化|推进|倒转|完成|关闭|归档)"
                r"|(?:生命值|精神值|物资点|HP|MP)\s*\d+\s*(?:->|→)\s*\d+"
                r"|(?:战役|场景|冲突|仪式|工程)(?:已经|已)(?:开始|结束|建立|创建|暂停|恢复)"
                r")",
                str(reply or ""),
            )
        )

    @staticmethod
    def _backstage_instruction_leaks(rows: list[dict[str, Any]]) -> int:
        """Count internal planner/validator prose that reached players."""

        pattern = re.compile(
            r"(?:后台指令|不得原样输出|只描述眼前真实可见|不要替角色改做|"
            r"不要替角色执行其他行动|scene_access_blocked|world_response_contract|"
            r"response_instruction|ActionType|规则结算拦截|当前仪式参数不符合规则)"
        )
        return sum(
            1
            for row in rows
            if pattern.search(str(row.get("reply") or ""))
        )

    @staticmethod
    def _vague_placeholder_gm_outputs(rows: list[dict[str, Any]]) -> int:
        pattern = re.compile(
            r"(?:当前目标|当前线索|现场关键人物|那件东西|那块东西|某种担保|"
            r"合适对象|可互动的焦点|这一步的重点|你的行动重点)"
        )
        return sum(
            1
            for row in rows
            if pattern.search(str(row.get("reply") or ""))
        )

    @staticmethod
    def _premature_clock_consequences(rows: list[dict[str, Any]]) -> int:
        states: dict[str, dict[str, object]] = {}
        progress = re.compile(r"【([^】]{2,60})】\s*(\d+)\s*/\s*(\d+)")
        violations = 0
        for row in rows:
            authoritative = row.get("clock_boundaries")
            if isinstance(authoritative, list):
                states = {
                    str(item.get("name") or "").strip(): {
                        "name": str(item.get("name") or "").strip(),
                        "current": int(item.get("current") or 0),
                        "maximum": int(item.get("maximum") or 0),
                        "stakes": str(item.get("stakes") or ""),
                        "completion_consequence": str(
                            item.get("completion_consequence") or ""
                        ),
                    }
                    for item in authoritative
                    if isinstance(item, dict)
                    and str(item.get("name") or "").strip()
                    and int(item.get("current") or 0) < int(item.get("maximum") or 0)
                    and str(item.get("status") or "active")
                    not in {"resolved", "abandoned", "archived"}
                }
            reply = str(row.get("reply") or "")
            for match in progress.finditer(reply):
                name = match.group(1).strip()
                prior = dict(states.get(name) or {})
                states[name] = {
                    "name": name,
                    "current": int(match.group(2)),
                    "maximum": int(match.group(3)),
                    "stakes": str(prior.get("stakes") or name),
                    "completion_consequence": str(
                        prior.get("completion_consequence") or name
                    ),
                }
            boundaries = list(states.values())
            if ClockNarrativeBoundary.violation(reply, boundaries):
                violations += 1
        return violations

    @classmethod
    def _near_duplicate_gm_replies(cls, rows: list[dict[str, Any]]) -> int:
        recent: list[str] = []
        duplicates = 0
        for row in rows:
            reply = cls._normalize(str(row.get("reply") or ""))
            if len(reply) < 45:
                continue
            if any(SequenceMatcher(None, prior, reply).ratio() >= 0.92 for prior in recent[-5:]):
                duplicates += 1
            recent.append(reply)
        return duplicates

    @classmethod
    def _embedded_prior_gm_replays(cls, rows: list[dict[str, Any]]) -> int:
        """Catch an old GM result pasted inside a newer, otherwise distinct reply.

        Whole-reply similarity misses the common failure mode where a valid
        new check result is followed by an earlier NPC turn.  Punctuation may
        also be rewritten into semicolons, so comparison uses the same compact
        normalization as the other transcript auditors.
        """

        recent: list[str] = []
        replays = 0
        for row in rows:
            # A reconnect recap intentionally restates already-public state.
            # It is neither a fresh ruling nor evidence that a previous NPC
            # turn leaked into an unrelated result, so keep it out of both
            # sides of this detector.
            if str(row.get("route") or "").strip() == "/v1/game/scene-recap":
                continue
            current = cls._normalize(str(row.get("reply") or ""))
            if len(current) < 48:
                continue
            if any(
                len(prior) >= 48 and prior in current and prior != current
                for prior in recent[-8:]
            ):
                replays += 1
            recent.append(current)
        return replays

    @staticmethod
    def _contradictory_check_responses(rows: list[dict[str, Any]]) -> int:
        contradictions = 0
        success_denial = re.compile(
            r"(?:本次|这次|此次|该次)?(?:检定)?失败|没有看出|没能发现|判断停在表层"
            r"|(?:无法|不能|未能|尚不能|暂时不能)(?:可靠地?)?"
            r"(?:判断|确认|确定|分辨|看清|辨认|证明|得知)"
            r"|(?:没有|缺少|不足)(?:足够|充分)?[^。；\n]{0,16}(?:线索|证据|信息|动向)"
        )
        for row in rows:
            reply = str(row.get("reply") or "")
            outcomes = re.findall(r"结算值[^。！？\n]{0,80}?[，,]\s*(成功|失败)[！!]?", reply)
            if len(outcomes) != 1:
                continue
            if outcomes[0] == "成功":
                denial = success_denial.search(reply)
                if denial is not None and not ConversationQualityAuditor._has_success_answer_before_limit(
                    reply[: denial.start()]
                ):
                    contradictions += 1
            elif outcomes[0] == "失败" and re.search(
                r"条件(?:已经)?满足|检定成功|成功(?:打开|说服|完成|发现)|顺利(?:打开|完成|通过)",
                reply,
            ):
                contradictions += 1
        return contradictions

    @staticmethod
    def _has_success_answer_before_limit(prefix: str) -> bool:
        """Allow a concrete success followed by an honest information limit."""

        for sentence in re.split(r"[。！？!?；\n]+", str(prefix or "")):
            clean = sentence.strip()
            if not clean:
                continue
            if any(
                marker in clean
                for marker in (
                    "结算值",
                    "掷骰",
                    "援用特质",
                    "援用羁绊",
                    "重掷",
                    "修正值",
                )
            ):
                continue
            normalized = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", clean)
            if len(normalized) >= 8:
                return True
        return False

    @staticmethod
    def _retired_clock_reappearances(rows: list[dict[str, Any]]) -> int:
        completed: set[str] = set()
        reappearances = 0
        clock_pattern = re.compile(r"【([^】]{2,50})】\s*(\d+)\s*/\s*(\d+)")
        completion_pattern = re.compile(r"命刻【([^】]{2,50})】(?:已经|已)?(?:完成|填满|触发)")
        for row in rows:
            reply = str(row.get("reply") or "")
            completed.update(match.group(1).strip() for match in completion_pattern.finditer(reply))
            for match in clock_pattern.finditer(reply):
                name = match.group(1).strip()
                current = int(match.group(2))
                maximum = int(match.group(3))
                if name in completed and current < maximum:
                    reappearances += 1
                if current >= maximum:
                    completed.add(name)
        return reappearances

    @classmethod
    def _irreversible_state_regressions(cls, rows: list[dict[str, Any]]) -> int:
        """Catch a completed threat later narrated as merely approaching."""

        committed: set[str] = set()
        regressions = 0
        completion_pattern = re.compile(
            r"(?P<subject>[\u4e00-\u9fffA-Za-z0-9·]{2,20}?(?:巡逻队|追兵|敌人|潮水|仪式|正门|闸门|通道|旧路))"
            r"[^。！？\n]{0,12}(?:已经|终于|彻底|随即)?(?:包围|抵达|封锁|攻入|闯入|开启|打开|没顶)"
        )
        downgrade_markers = ("仍在逼近", "正在逼近", "越来越近", "还在接近", "寻找入口", "尚未抵达", "还没到")
        for row in rows:
            reply = str(row.get("reply") or "")
            for match in completion_pattern.finditer(reply):
                subject = cls._normalize(match.group("subject"))
                if subject:
                    committed.add(subject[-10:])
            if not any(marker in reply for marker in downgrade_markers):
                continue
            normalized = cls._normalize(reply)
            if any(subject and subject[-4:] in normalized for subject in committed):
                regressions += 1
        return regressions

    @staticmethod
    def _fulfilled_promise_reopens(rows: list[dict[str, Any]]) -> int:
        last_grant_at = -100
        active_session = ""
        reopens = 0
        grant_pattern = re.compile(
            r"(?:放开|拨开|打开|开启)[^。！？\n]{0,16}(?:门|旧路|通道)"
            r"|(?:门|旧路|通道)[^。！？\n]{0,18}(?:打开|开启|开放)"
            r"|(?:准许|允许|让)[^。！？\n]{0,22}(?:进入|通过)"
            r"|(?:已经|随即|终于)[^。！？\n]{0,16}放行"
        )
        renewed_price = re.compile(
            r"(?:我的条件(?:还是|仍是)?|条件还是|只要|如果|若|等到|带回|证明|交出|"
            r"先把|先要|当众承诺|满足(?:了|后)?|还得|仍需|必须先)[^。！？\n]{0,90}"
            r"(?:放开|拨开|开门|放行|打开|开启|开放旧路|进入旧路)"
        )
        repeated_payout = re.compile(
            r"(?:我现在|我这就|现在就|立刻|继续)[^。！？\n]{0,28}"
            r"(?:放开|拨开|开门|放行|打开|开启)[^。！？\n]{0,18}(?:门|旧路|通道)?"
        )
        for index, row in enumerate(rows):
            label = str(row.get("label") or "")
            session_match = re.search(r"第0*(\d+)场", label)
            session = session_match.group(1) if session_match else active_session
            if session and active_session and session != active_session:
                last_grant_at = -100
            if session:
                active_session = session
            reply = str(row.get("reply") or "")
            renewed_match = renewed_price.search(reply)
            if renewed_match and ConversationQualityAuditor._renewed_price_is_negated(
                reply,
                renewed_match,
            ):
                renewed_match = None
            if index - last_grant_at <= 8 and (renewed_match or repeated_payout.search(reply)):
                reopens += 1
            for match in grant_pattern.finditer(reply):
                # "答得上，我就开门" is an offer, not an already-paid
                # concession.  Treating it as a grant makes the later real
                # opening look like an illegal repeated payout and hides the
                # actual continuity failure we want this auditor to catch.
                if ConversationQualityAuditor._conditional_offer_contains_grant(reply, match):
                    continue
                last_grant_at = index
        return reopens

    @staticmethod
    def _renewed_price_is_negated(reply: str, match: re.Match[str]) -> bool:
        """Whether a condition-looking phrase is explicitly excluded.

        Clarifying that handing over an item or opening it is *not* part of an
        agreement must not be audited as charging that price.  The renewed-price
        pattern intentionally begins at the candidate demand, so inspect the
        complete surrounding sentence rather than only the regex match.
        """

        source = str(reply or "")
        start = max(source.rfind(marker, 0, match.start()) for marker in ("。", "！", "？", "\n")) + 1
        endings = [source.find(marker, match.end()) for marker in ("。", "！", "？", "\n")]
        endings = [ending for ending in endings if ending >= 0]
        end = min(endings) if endings else len(source)
        sentence = source[start:end]
        return bool(
            re.search(
                r"(?:都|均|也|一概|并)?不(?:在|属于|算作|作为|列入|包含|包括|需要|要求)"
                r"[^。！？\n]{0,30}(?:范围|条件|代价|登记|协议|交易|要求|之内|以内)?",
                sentence,
            )
            or re.search(
                r"(?:无需|不必|不会要求|不需要)[^。！？\n]{0,40}"
                r"(?:交出|开启|打开|放行|开门|进入旧路)",
                sentence,
            )
        )

    @staticmethod
    def _conditional_offer_contains_grant(reply: str, match: re.Match[str]) -> bool:
        """Whether a door/passages phrase remains inside an if-then offer."""

        source = str(reply or "")
        start = max(
            source.rfind(marker, 0, match.start())
            for marker in ("。", "！", "？", "\n")
        ) + 1
        prefix = source[start : match.end()]
        return bool(
            re.search(
                # An NPC may phrase a gate as an imperative rather than an
                # explicit "if": "告诉我目的地和理由，我就开门".  That is
                # still an offer, not an already opened door.  Without these
                # verbs the long-run gate records the promise as paid and
                # falsely flags the real later payoff as a reopening.
                r"(?:若|如果|只要|答得上|等到|完成|先|告诉我|说明|回答|交代|给出|带来)[^。！？\n]{0,150}"
                r"(?:就|才|会|便|再)[^。！？\n]{0,84}$",
                prefix,
            )
        )

    @staticmethod
    def _npc_commitment_violations(rows: list[dict[str, Any]]) -> int:
        """Catch an explicit promise being contradicted in later public prose.

        The detector intentionally requires both a narrow public promise and
        an explicit contradiction (for example, “没有退开，反而……”); it does
        not attempt to judge ordinary NPC hesitation or infer hidden intent.
        """

        promise = re.compile(
            r"(?:核验|查验|检验|验)完(?:之后|后)[^。！？\n]{0,72}"
            r"(?:退开|后退|退到|离开|往后退)"
        )
        completion = re.compile(
            r"(?:验到这里[^。！？\n]{0,12}(?:已经)?够了|"
            r"(?:核验|查验|检验|验)(?:已经)?(?:完成|完毕)|"
            r"(?:核验|查验|检验|验)完了|"
            r"(?:核验|查验|检验|验)(?:已经)?(?:看过|看完|验过))"
        )
        retreat = re.compile(r"(?:退开|后退|退到|离开|往后退|向后让开)")
        pending_at = -100
        active_session = ""
        violations = 0
        for index, row in enumerate(rows):
            label = str(row.get("label") or "")
            session_match = re.search(r"第0*(\d+)场", label)
            session = session_match.group(1) if session_match else active_session
            if session and active_session and session != active_session:
                pending_at = -100
            if session:
                active_session = session
            reply = str(row.get("reply") or "")
            if (
                index - pending_at <= 14
                and completion.search(reply)
                and not ConversationQualityAuditor._contains_actual_retreat(reply, retreat)
            ):
                violations += 1
                pending_at = -100
            if promise.search(reply):
                pending_at = index
        return violations

    @staticmethod
    def _contains_actual_retreat(text: str, retreat: re.Pattern[str]) -> bool:
        for match in retreat.finditer(str(text or "")):
            prefix = str(text or "")[max(0, match.start() - 8) : match.start()]
            if re.search(r"(?:没有|未|并未|没|不)\s*$", prefix):
                continue
            return True
        return False

    @staticmethod
    def _repeated_player_action_lanes(rows: list[dict[str, Any]]) -> int:
        from fu_gm.testing.player_simulator import ConstrainedPlayerSimulator

        recent: list[tuple[str, set[str], str]] = []
        repeats = 0
        for row in rows:
            label = str(row.get("label") or "")
            if "行动" not in label or "待决回应" in label:
                continue
            if ConversationQualityAuditor._is_resolved_scene_relocation(row):
                # Every player must authorize their own movement. Several PCs
                # independently following an agreed route is therefore valid
                # table progression, not a stale action loop, once typed tool
                # receipts prove that each relocation actually committed.
                continue
            message = str(row.get("message") or "")
            if re.search(r"(?:暂时|本轮)?不采取行动|(?:暂时|本轮)?放弃行动", message):
                # Passing is deliberately the absence of an action lane. A
                # simulator that falls back to repeated passes is reported by
                # its dedicated fallback metric instead of being mislabeled as
                # repeated fictional intent.
                continue
            family = ConstrainedPlayerSimulator._action_family(message)
            lane_text = ConversationQualityAuditor._semantic_action_lane_text(row) or message
            tokens = ConstrainedPlayerSimulator._action_lane_tokens(lane_text)
            if family in {"attack", "magic"} or len(tokens) < 3:
                continue
            phase = "settlement" if ConversationQualityAuditor._is_negotiation_settlement(message) else "action"
            same_family_matches = 0
            cross_family_matches = 0
            for prior_family, prior_tokens, prior_phase in recent[-5:]:
                if prior_family in {"attack", "magic"}:
                    continue
                # Asking what a bargain covers and then accepting, rejecting,
                # or counter-offering it is progression, not repetition. Keep
                # settlement moves in their own phase so genuinely repeated
                # commitments can still be noticed.
                if prior_phase != phase:
                    continue
                overlap = tokens & prior_tokens
                if len(overlap) >= 3 and len(overlap) / max(1, min(len(tokens), len(prior_tokens))) >= 0.25:
                    cross_family_matches += 1
                    if family and family == prior_family:
                        same_family_matches += 1
            # A loop may keep the same fictional object while changing verbs:
            # repeatedly asking about, inspecting, entering, and reinforcing the
            # same unopened door is still one saturated action lane. Requiring
            # three cross-family matches avoids flagging an ordinary three-step
            # investigate/manipulate/move sequence.
            if same_family_matches >= 2 or cross_family_matches >= 3:
                repeats += 1
            recent.append((family, tokens, phase))
        return repeats

    @staticmethod
    def _is_resolved_scene_relocation(row: dict[str, Any]) -> bool:
        body = row.get("body")
        if not isinstance(body, dict):
            return False
        receipts = body.get("tool_receipts")
        if not isinstance(receipts, list):
            return False
        for receipt in receipts:
            if not isinstance(receipt, dict):
                continue
            if not bool(receipt.get("ok")) or not bool(receipt.get("state_changed")):
                continue
            tool_name = str(receipt.get("tool_name") or "").strip()
            result = receipt.get("result")
            if not isinstance(result, dict):
                result = {}
            if tool_name == "transition_scene":
                return True
            if tool_name in {"move_group_within_scene", "move_scene_group"}:
                return True
            if tool_name == "perform_in_scene_action" and bool(
                result.get("joined_current_focus")
            ):
                return True
        return False

    @staticmethod
    def _semantic_action_lane_text(row: dict[str, Any]) -> str:
        """Use the Luna-reviewed goal instead of surface noun overlap.

        Two consecutive actions can naturally mention the same NPC and place
        while doing different things: asking a traveler what they remember and
        then reassuring them are not one saturated action lane.  The semantic
        router already records that distinction in ``action_goal``.  Falling
        back to the raw utterance remains useful for old transcripts and small
        unit fixtures that predate structured route decisions.
        """

        body = row.get("body")
        if not isinstance(body, dict):
            return ""
        decision = body.get("decision")
        if not isinstance(decision, dict):
            return ""
        if not bool(decision.get("action_semantics_reviewed")):
            return ""
        goal = " ".join(str(decision.get("action_goal") or "").split()).strip()
        if goal:
            return goal
        return " ".join(str(decision.get("action_summary") or "").split()).strip()

    @staticmethod
    def _is_negotiation_settlement(message: str) -> bool:
        """Return whether a player actually settles or changes a bargain.

        Questions, tentative table talk, and requests for clarification are not
        settlements. This intentionally requires an explicit acceptance,
        refusal, or exchange stated in the player's own voice.
        """

        clean = re.sub(r"\s+", "", str(message or ""))
        if not clean:
            return False
        if re.search(
            r"(?:^|[，。；：:—-])(?:我|我们|小队)[^。！？]{0,24}"
            r"(?:接受|同意|答应|拒绝|不接受|不答应|成交|不成交|就按|照这个条件)",
            clean,
        ):
            return True
        if re.search(
            r"(?:我|我们)[^。！？]{0,90}(?:拿|用|给|交出|提供|告诉)[^。！？]{0,70}"
            r"(?:来换|换取|交换)",
            clean,
        ):
            return True
        return bool(re.search(r"(?:^|[，。；：:—-])(?:成交|就这么办|按这个条件来)(?:[，。！？；]|$)", clean))

    @classmethod
    def _memory_anchor_similarities(
        cls,
        assessments: list[SessionProgressAssessment],
    ) -> tuple[list[float], list[dict[str, Any]]]:
        anchors = [
            "|".join((item.memory_image, item.memory_choice, item.memory_consequence))
            for item in assessments
            if item.memory_anchor_complete
        ]
        similarities: list[float] = []
        high_pairs: list[dict[str, Any]] = []
        for index, current in enumerate(anchors):
            for prior_index in range(max(0, index - 3), index):
                score = cls._character_ngram_similarity(anchors[prior_index], current)
                similarities.append(score)
                if score >= 0.72:
                    high_pairs.append(
                        {
                            "earlier_anchor": prior_index + 1,
                            "later_anchor": index + 1,
                            "similarity": round(score, 3),
                        }
                    )
        return similarities, high_pairs

    @classmethod
    def _character_ngram_similarity(cls, left: str, right: str) -> float:
        def grams(value: str) -> set[str]:
            text = cls._normalize(value)
            if len(text) < 3:
                return {text} if text else set()
            return {text[index : index + 3] for index in range(len(text) - 2)}

        left_grams = grams(left)
        right_grams = grams(right)
        if not left_grams or not right_grams:
            return 0.0
        jaccard = len(left_grams & right_grams) / len(left_grams | right_grams)
        sequence = SequenceMatcher(
            None,
            cls._normalize(left),
            cls._normalize(right),
        ).ratio()
        return max(jaccard, sequence)

    def _echoes_player(self, row: dict[str, Any]) -> bool:
        message = self._normalize(str(row.get("message") or ""))
        reply = self._normalize(str(row.get("reply") or ""))
        if len(message) < 12 or len(reply) < 20:
            return False
        prefix = reply[: min(len(reply), max(40, len(message) * 2))]
        ratio = SequenceMatcher(None, message, prefix).ratio()
        contained = len(message) >= 28 and message in reply
        return contained or ratio >= 0.66

    @staticmethod
    def _is_silent(row: dict[str, Any]) -> bool:
        body = row.get("body") if isinstance(row.get("body"), dict) else {}
        return (
            not str(row.get("reply") or "").strip()
            and not bool(body.get("send_reply"))
            and str(body.get("target") or "silent") == "silent"
        )

    @classmethod
    def _normalize(cls, text: str) -> str:
        return cls._SPACE.sub("", str(text or "")).lower()

    @staticmethod
    def _percentile(values: list[int], ratio: float) -> int:
        if not values:
            return 0
        if ratio == 0.5:
            return int(median(values))
        index = min(len(values) - 1, max(0, round((len(values) - 1) * ratio)))
        return int(values[index])
