from __future__ import annotations

import re
from difflib import SequenceMatcher
from collections.abc import Iterable
from typing import Any, Mapping

from fu_gm.components.clock_narrative_boundary import ClockNarrativeBoundary
from fu_gm.models import Action, ActionType


class NPCContinuityPolicy:
    """Keep an NPC's latest public position ahead of private scene prep.

    Prepared demands are useful before an NPC speaks. Once the table hears that
    NPC explicitly replace or withdraw those terms, the spoken revision becomes
    authoritative. This policy detects that narrow transition and guards the
    structured speech plan without trying to judge ordinary characterization.
    """

    _REVISION_PATTERNS = (
        r"(?:我|我们|本方|财团)?(?:刚才|方才|现在|今天|今晚)?(?:已经)?(?:改口|改主意|改变主意)",
        r"(?:我|我们|本方|财团)?(?:收回|撤回|取消|作废|废止)(?:刚才|此前|之前|原先|原本)?",
        r"(?:不再|不必再|不用再|不等了|不收了|不要了).{0,30}(?:改为|改成|改收|换成|现在只|今天只|今晚只)",
        r"(?:原先|原本|此前|之前|刚才).{0,24}(?:不算|作废|取消|改为|改成|换成)",
        r"(?:条件|要求|价码|交易|说法).{0,12}(?:改为|改成|换成|作废|取消)",
    )
    _OBJECT_TERMS = (
        r"(?:记录|登记|档案|名册|账册|册页|文书|卷宗|材料|证据|钥匙|地图|"
        r"通行证|文件|信件|遗物|物件|东西|它们|盒子|匣子)"
    )
    _AWAITING_OBJECT_HANDOFF = re.compile(
        rf"(?:把|将)[^。！？]{{0,56}}{_OBJECT_TERMS}[^。！？]{{0,28}}"
        r"(?:拿到|带到|送到|交到|递到|放到|摆到)(?:我|这边|这里|眼前|面前|手里)"
    )
    _PLAYER_COMPLETES_HANDOFF = re.compile(
        rf"(?:^|[：:。；;！？!?])\s*(?:我|[\u4e00-\u9fff]{{1,8}})"
        r"(?:已经|随即|当场|立刻|这就|便|随后|直接)?\s*(?:把|将)"
        rf"[^。！？]{{0,48}}{_OBJECT_TERMS}?[^。！？]{{0,16}}"
        r"(?:递给|交给|送到[^。！？]{0,8}手里|放到[^。！？]{0,8}面前|"
        r"摆到[^。！？]{0,8}面前|拿到[^。！？]{0,8}眼前)"
    )
    _OBJECT_ACCESS_CLAIM = re.compile(
        rf"(?:接过|收下|收到|拿到|取到|翻开|摊开)[^。！？]{{0,18}}{_OBJECT_TERMS}"
        rf"|(?:现在|当场|立刻)?(?:就|开始)?(?:核对|比对|查阅|翻阅|查看)"
        rf"[^。！？]{{0,20}}{_OBJECT_TERMS}"
    )
    _NON_COMPLETED_ACCESS = re.compile(
        r"(?:还没|尚未|没有|并未|不能|无法|尚不能|等|待|若|如果|只要|"
        r"拿到后|拿到以后|收到后|收到以后|之后|以后|才能|才可以|再)"
        r"[^。！？\n]{0,18}$"
    )
    _FULFILLED_PAYOUT_RESTRICTION = re.compile(
        r"(?P<prefix>[，,；;]\s*(?:但|不过|只是|另外|同时)?\s*|(?:但|不过|只是)\s*)"
        r"(?P<clause>(?:(?:你们|你|英雄|队伍|旅人|失忆旅人|任何人|他|她)"
        r"[^。！？]{0,56}(?:必须|仍需|还得|先|不得|只能|不可|才可|否则)"
        r"[^。！？]{0,90}))(?P<terminal>[。！？]?)"
    )

    @classmethod
    def explicitly_revises_prior_terms(cls, statement: str) -> bool:
        clean = " ".join(str(statement or "").split()).strip()
        return bool(clean and any(re.search(pattern, clean) for pattern in cls._REVISION_PATTERNS))

    @classmethod
    def continuity_instruction(
        cls,
        statement: str,
        *,
        supersedes_prior_terms: bool,
    ) -> str:
        clean = " ".join(str(statement or "").split()).strip()
        if not clean:
            return ""
        base = (
            f"该NPC最近一次已经公开说过：【{clean[:500]}】。"
            "这是桌面上已经发生的事实，本轮必须从这句话之后继续，不能否认自己说过它。"
        )
        if not supersedes_prior_terms:
            return base
        return (
            base
            + "这次公开表态已经替换了更早的准备条件；旧要求、旧接受标准和旧价码只属于历史，"
            "不得自动恢复为当前条件。NPC若此刻再次改变决定，必须明确承认自己又改了主意，"
            "说明原因并立刻采取相应行动；不得声称‘条件不变’，也不得无声倒回旧交易。"
        )

    @classmethod
    def reopens_superseded_bargain(
        cls,
        action: Action | None,
        *,
        prepared: Mapping[str, Any] | None,
        supersedes_prior_terms: bool,
    ) -> bool:
        if (
            not supersedes_prior_terms
            or action is None
            or action.action_type != ActionType.NARRATE
            or not prepared
        ):
            return False
        plan = action.parameters.get("npc_speech_plan")
        if not isinstance(plan, dict):
            return False
        candidate = " ".join(
            str(plan.get(key) or "").strip()
            for key in ("condition", "promised_result", "direct_answer", "settled_terms")
            if str(plan.get(key) or "").strip()
        )
        if not candidate:
            return False
        # A second explicit revision is legal because the NPC owns their
        # decision. It must acknowledge change rather than pretend the prior
        # public revision never happened.
        if cls.explicitly_revises_prior_terms(candidate) and re.search(
            r"(?:刚才|方才|又|再次|重新|现在)", candidate
        ):
            return False
        anchors = [
            str(prepared.get(key) or "").strip()
            for key in ("concrete_demand", "acceptance_rule")
            if str(prepared.get(key) or "").strip()
        ]
        return any(cls._distinctive_overlap(candidate, anchor) for anchor in anchors)

    @classmethod
    def fallback_action(cls, statement: str) -> Action:
        """Preserve public continuity when a corrective planner retry fails."""

        clean = " ".join(str(statement or "").split()).strip()
        return Action(
            ActionType.NARRATE,
            {
                "npc_speech_plan": {
                    "speech_act": "answer",
                    "direct_answer": clean,
                    "facts_to_share": [],
                    "facts_to_withhold": [],
                    "condition": "",
                    "condition_outcome": "none",
                    "promised_result": "",
                    "promise_kind": "none",
                    "promise_subject": "",
                    "proposal_outcome": "none",
                    "settled_terms": "",
                    "stance": clean,
                    "intent": "维持最近一次公开表态",
                    "emotion": "克制",
                    "nonverbal_reaction": "",
                },
                "npc_continuity_fallback_used": True,
            },
        )

    @classmethod
    def unapproved_fulfilled_payout_restriction(
        cls,
        text: str,
        *,
        settled_terms: str,
    ) -> str:
        """Find a newly imposed player-facing restriction after a payoff.

        A fulfilled bargain may still include an already agreed safety boundary,
        but an NPC may not use the payout sentence to add a fresh custody,
        payment, or permission requirement.  This deliberately looks only for
        explicit player/NPC obligations rather than ordinary environmental
        warnings such as a collapsed bridge.
        """

        source = " ".join(str(text or "").split()).strip()
        terms = " ".join(str(settled_terms or "").split()).strip()
        if not source:
            return ""
        for match in cls._FULFILLED_PAYOUT_RESTRICTION.finditer(source):
            clause = str(match.group("clause") or "").strip()
            if clause and not cls._restriction_was_already_settled(clause, terms):
                return clause
        return ""

    @classmethod
    def strip_unapproved_fulfilled_payout_restrictions(
        cls,
        text: str,
        *,
        settled_terms: str,
    ) -> str:
        """Remove only restrictions that a fulfilled exchange never contained."""

        source = " ".join(str(text or "").split()).strip()
        if not source:
            return ""

        def replace(match: re.Match[str]) -> str:
            clause = str(match.group("clause") or "").strip()
            if cls._restriction_was_already_settled(clause, settled_terms):
                return match.group(0)
            return str(match.group("terminal") or "")

        repaired = cls._FULFILLED_PAYOUT_RESTRICTION.sub(replace, source)
        repaired = re.sub(r"[，,；;]\s*(?=[。！？]|$)", "", repaired)
        return repaired.strip()

    @classmethod
    def _restriction_was_already_settled(cls, clause: str, settled_terms: str) -> bool:
        compact_clause = cls._compact(clause)
        compact_terms = cls._compact(settled_terms)
        if not compact_clause or not compact_terms:
            return False
        if compact_clause in compact_terms or compact_terms in compact_clause:
            return True
        match = SequenceMatcher(None, compact_clause, compact_terms, autojunk=False).find_longest_match()
        return match.size >= 6 and match.size / max(1, min(len(compact_clause), len(compact_terms))) >= 0.45

    @classmethod
    def clock_boundary_violation(
        cls,
        action: Action | None,
        boundaries: Iterable[dict[str, object]],
    ) -> str:
        """Reject accidental objective claims beyond an unfinished clock.

        NPCs may deliberately lie, but that must be an explicit private intent
        in the approved plan. Ordinary uncertainty or model drift must not turn
        an unfinished threat into a public fact.
        """

        if action is None or action.action_type != ActionType.NARRATE:
            return ""
        plan = action.parameters.get("npc_speech_plan")
        if not isinstance(plan, dict) or cls.intentionally_deceptive(plan):
            return ""
        chunks: list[str] = []
        for key in (
            "direct_answer",
            "facts_to_share",
            "condition",
            "promised_result",
            "settled_terms",
            "nonverbal_reaction",
        ):
            value = plan.get(key)
            if isinstance(value, (list, tuple, set)):
                chunks.extend(str(item) for item in value if str(item).strip())
            elif str(value or "").strip():
                chunks.append(str(value))
        return ClockNarrativeBoundary.violation("；".join(chunks), boundaries)

    @classmethod
    def object_access_violation(
        cls,
        action: Action | None,
        *,
        player_message: str,
        latest_public_statement: str,
    ) -> str:
        """Reject completed possession that contradicts a pending handoff."""

        if action is None or action.action_type != ActionType.NARRATE:
            return ""
        plan = action.parameters.get("npc_speech_plan")
        if not isinstance(plan, dict):
            return ""
        chunks: list[str] = []
        for key in (
            "direct_answer",
            "facts_to_share",
            "condition",
            "promised_result",
            "settled_terms",
            "nonverbal_reaction",
        ):
            value = plan.get(key)
            if isinstance(value, (list, tuple, set)):
                chunks.extend(str(item) for item in value if str(item).strip())
            elif str(value or "").strip():
                chunks.append(str(value))
        return cls.unsupported_object_access_text(
            "；".join(chunks),
            player_message=player_message,
            latest_public_statement=latest_public_statement,
        )

    @classmethod
    def unsupported_object_access_text(
        cls,
        text: str,
        *,
        player_message: str,
        latest_public_statement: str,
    ) -> str:
        statement = " ".join(str(latest_public_statement or "").split()).strip()
        if not statement or not cls._AWAITING_OBJECT_HANDOFF.search(statement):
            return ""
        if cls._PLAYER_COMPLETES_HANDOFF.search(str(player_message or "")):
            return ""
        candidate = " ".join(str(text or "").split()).strip()
        for match in cls._OBJECT_ACCESS_CLAIM.finditer(candidate):
            prefix = candidate[max(0, match.start() - 24) : match.start()]
            suffix = candidate[match.end() : match.end() + 8]
            if cls._NON_COMPLETED_ACCESS.search(prefix):
                continue
            if re.match(r"(?:后|以后|之后|才|才能|再)", suffix):
                continue
            return (
                "NPC最近公开要求他人先交付物品，但玩家本轮没有完成交付；"
                f"候选却声称已经取得或开始使用该物品：{match.group(0)}"
            )
        return ""

    @classmethod
    def missing_object_fallback_action(cls) -> Action:
        action = cls.fallback_action("东西还没到我手里；拿到以后我才能核对。")
        action.parameters["npc_object_continuity_fallback_used"] = True
        return action

    @staticmethod
    def intentionally_deceptive(plan: Mapping[str, Any] | None) -> bool:
        if not plan:
            return False
        private_intent = " ".join(
            str(plan.get(key) or "").strip()
            for key in ("intent", "stance")
            if str(plan.get(key) or "").strip()
        )
        return bool(
            re.search(
                r"(?:故意|有意|刻意).{0,10}(?:撒谎|说谎|欺骗|误导|谎称|虚报|制造假象)"
                r"|(?:撒谎|说谎|欺骗|误导|谎称|虚报).{0,10}(?:玩家|英雄|对方)",
                private_intent,
            )
        )

    @classmethod
    def clock_safe_fallback_action(
        cls,
        boundaries: Iterable[dict[str, object]],
    ) -> Action:
        context = " ".join(
            str(item.get(key) or "")
            for item in boundaries
            for key in ("name", "stakes", "completion_consequence")
            if isinstance(item, dict)
        )
        if re.search(r"巡逻|追兵|车队|援军|增援|逼近|抵达|赶到|包围|封锁", context):
            answer = "我只能确认动静还在靠近，不能说他们已经到了。"
        elif re.search(r"潮|水位|淹|没顶|洪水", context):
            answer = "水势还在变化，但现在还没有淹到这里。"
        elif re.search(r"警报|警戒|暴露|发现", context):
            answer = "我还不能说警戒已经全面拉响。"
        elif re.search(r"蓄力|过载|仪式|施法|咏唱|充能", context):
            answer = "那股力量还没有完成。"
        else:
            answer = "我现在还不能确认那件事已经发生。"
        action = cls.fallback_action(answer)
        action.parameters["npc_clock_boundary_fallback_used"] = True
        return action

    @classmethod
    def prune_clock_boundary_action(
        cls,
        action: Action | None,
        boundaries: Iterable[dict[str, object]],
    ) -> Action | None:
        """Remove only unsupported clock claims from an otherwise useful reply.

        Replacing the whole answer with a generic clock disclaimer loses the
        player's actual question.  Structured speech plans let us discard the
        offending sentence or fact while preserving a valid requirement,
        refusal, route, or other answer in the same plan.
        """

        if action is None or action.action_type != ActionType.NARRATE:
            return None
        boundaries = list(boundaries)
        params = dict(action.parameters)
        raw_plan = params.get("npc_speech_plan")
        if not isinstance(raw_plan, dict) or cls.intentionally_deceptive(raw_plan):
            return None
        plan = dict(raw_plan)
        for key in ("direct_answer", "condition", "promised_result", "settled_terms", "nonverbal_reaction"):
            plan[key] = cls._prune_clock_boundary_text(str(plan.get(key) or ""), boundaries)
        plan["facts_to_share"] = [
            repaired
            for item in (plan.get("facts_to_share") or [])
            for repaired in [cls._prune_clock_boundary_text(str(item or ""), boundaries)]
            if repaired
        ]
        if bool(plan.get("condition")) != bool(plan.get("promised_result")):
            plan["condition"] = ""
            plan["promised_result"] = ""
            if str(plan.get("speech_act") or "") == "condition":
                plan["speech_act"] = "answer"
        public_chunks = [
            str(plan.get("direct_answer") or "").strip(),
            *(str(item).strip() for item in (plan.get("facts_to_share") or [])),
            str(plan.get("condition") or "").strip(),
            str(plan.get("promised_result") or "").strip(),
            str(plan.get("nonverbal_reaction") or "").strip(),
        ]
        if not any(public_chunks):
            return None
        repaired = Action(ActionType.NARRATE, {**params, "npc_speech_plan": plan})
        if cls.clock_boundary_violation(repaired, boundaries):
            return None
        repaired.parameters["npc_clock_boundary_pruned"] = True
        return repaired

    @classmethod
    def _prune_clock_boundary_text(
        cls,
        text: str,
        boundaries: Iterable[dict[str, object]],
    ) -> str:
        source = " ".join(str(text or "").split()).strip()
        if not source or not ClockNarrativeBoundary.violation(source, boundaries):
            return source
        pieces = [
            part.strip()
            for part in re.split(r"(?<=[。！？!?；;])", source)
            if part.strip()
        ]
        kept = [
            part
            for part in pieces
            if not ClockNarrativeBoundary.violation(part, boundaries)
        ]
        if kept:
            return "".join(kept).strip()
        # A model may put the unsupported arrival claim and the useful answer
        # on opposite sides of a comma.  Preserve only independently safe
        # clauses, then normalize dangling punctuation.
        clauses = [
            part.strip(" ，,：:")
            for part in re.split(r"[，,：:]", source)
            if part.strip(" ，,：:")
        ]
        safe_clauses = [
            part
            for part in clauses
            if not ClockNarrativeBoundary.violation(part, boundaries)
        ]
        return "；".join(safe_clauses).strip()

    @classmethod
    def _distinctive_overlap(cls, candidate: str, anchor: str) -> bool:
        left = cls._compact(candidate)
        right = cls._compact(anchor)
        if not left or not right:
            return False
        if len(right) >= 6 and right in left:
            return True
        match = SequenceMatcher(None, left, right, autojunk=False).find_longest_match()
        return match.size >= 6 and match.size / max(1, min(len(left), len(right))) >= 0.30

    @staticmethod
    def _compact(value: Any) -> str:
        return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(value or "")).lower()


class NPCCommitmentBoundary:
    """Keep explicit public NPC promises from being narratively skipped.

    This is deliberately narrow.  It does not infer a promise from an NPC's
    goals or private prep; it only tracks statements that openly say an
    inspection will be followed by a retreat.  When a later scene beat says
    that inspection is complete, the same NPC must actually move back in the
    described fiction rather than remain at the object they promised to leave.
    """

    _PROMISE_TRIGGER = re.compile(
        r"(?:核验|查验|检验|验)完(?:之后|后)[^。！？\n]{0,72}"
        r"(?:退开|后退|退到|离开|往后退)"
    )
    _COMPLETION = re.compile(
        r"(?:验到这里[^。！？\n]{0,12}(?:已经)?够了|"
        r"(?:核验|查验|检验|验)(?:已经)?(?:完成|完毕)|"
        r"(?:核验|查验|检验|验)完了|"
        r"(?:核验|查验|检验|验)(?:已经)?(?:看过|看完|验过))"
    )
    _RETREAT = re.compile(r"(?:退开|后退|退到|离开|往后退|向后让开)")

    @classmethod
    def due_commitments(cls, ledger: Iterable[dict[str, object]]) -> list[dict[str, str]]:
        """Extract only explicit, public inspection-then-retreat promises."""

        commitments: list[dict[str, str]] = []
        for record in ledger:
            if not isinstance(record, dict):
                continue
            npc = str(record.get("npc") or record.get("name") or "").strip()
            if not npc:
                continue
            aliases = [
                npc,
                str(record.get("public_identity") or "").strip(),
                *(str(item or "").strip() for item in record.get("aliases", []) or []),
            ]
            for statement in record.get("statements", []) or []:
                clean = " ".join(str(statement or "").split()).strip()
                if not clean or not cls._PROMISE_TRIGGER.search(clean):
                    continue
                commitment = {
                    "npc": npc,
                    "statement": clean[:500],
                    "trigger": "核验完成",
                    "required_effect": "退开",
                    "aliases": "|".join(alias for alias in aliases if alias),
                }
                if commitment not in commitments:
                    commitments.append(commitment)
        return commitments

    @classmethod
    def violation(
        cls,
        metadata: Mapping[str, Any] | None,
        commitments: Iterable[dict[str, object]],
    ) -> str:
        """Reject a candidate that completes a promise trigger but ignores it."""

        if not isinstance(metadata, Mapping):
            return ""
        speakers = metadata.get("npc_speakers")
        if not isinstance(speakers, list):
            return ""
        pending = [item for item in commitments if isinstance(item, dict)]
        if not pending:
            return ""
        for speaker in speakers:
            if not isinstance(speaker, Mapping):
                continue
            npc = str(speaker.get("npc") or "").strip()
            statement = str(speaker.get("public_statement") or "").strip()
            if not npc or not statement or not cls._COMPLETION.search(statement):
                continue
            for commitment in pending:
                # A scene may introduce an already speaking descriptive NPC by
                # name later in the same exchange.  With exactly one pending
                # public promise, that rename must not let the candidate evade
                # the promise merely because the aliases were not persisted.
                if not cls._speaker_matches(npc, commitment) and len(pending) != 1:
                    continue
                if not cls._contains_actual_retreat(statement):
                    return (
                        f"【{commitment.get('npc') or npc}】先前公开承诺核验完成后退开；"
                        "本段已经说核验完成，必须让其实际退开，不能留在原处。"
                    )
        return ""

    @classmethod
    def _contains_actual_retreat(cls, statement: str) -> bool:
        """Return true only for a retreat that happened, not its negation."""

        for match in cls._RETREAT.finditer(str(statement or "")):
            prefix = str(statement or "")[max(0, match.start() - 8) : match.start()]
            if re.search(r"(?:没有|未|并未|没|不)\s*$", prefix):
                continue
            return True
        return False

    @staticmethod
    def _speaker_matches(npc: str, commitment: Mapping[str, object]) -> bool:
        left = NPCContinuityPolicy._compact(npc)
        aliases = str(commitment.get("aliases") or "").split("|")
        for alias in aliases:
            right = NPCContinuityPolicy._compact(alias)
            if left and right and (left == right or left in right or right in left):
                return True
        return False
