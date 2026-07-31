from __future__ import annotations

import re
from collections.abc import Mapping


FULFILLED_PROMISE_PREFIX = "已满足条件，必须在本轮实际兑现："


_CURRENT_ACTION_DECLARATION = re.compile(
    r"(?:我|我们|本人)(?:现在|先|马上|立刻|直接|就)?(?:把|将)"
    r"[^。！？!?]{0,72}"
    r"(?:移开|移走|挪开|拿开|剥离|拆开|拆下|取下|处理掉|处理|清除|清理|"
    r"查看|检查|观察|检视|核对|翻看|交出|交给|递给|放入|放下|归还|带走|"
    r"取走|打开|开启|关上|封住|解开|修复|修理|补上|加固|改写)"
)
_PERMISSION_WORDS = re.compile(
    r"(?:允许|准许|同意|答应|接受|可以|照做|按[^。！？!?]{0,12}"
    r"(?:做|处理|移开|查看|检查|交出|打开|进入))"
)
_ACTION_FAMILY_PATTERNS: dict[str, re.Pattern[str]] = {
    "remove": re.compile(
        r"(?:移开|移走|挪开|拿开|剥离|拆开|拆下|取下|处理掉|处理|清除|清理|"
        r"拔出|切断)"
    ),
    "inspect": re.compile(r"(?:查看|检查|观察|检视|核对|翻看|辨认)"),
    "transfer": re.compile(r"(?:交出|交给|递给|放入|放下|归还|带走|取走|拿走)"),
    "access": re.compile(r"(?:打开|开启|关上|封住|解开|进入|通过|放行)"),
    "repair": re.compile(r"(?:修复|修理|补上|缝合|加固|改写)"),
}
_GENERIC_ACTION_FRAGMENTS = {
    "现在",
    "先把",
    "之后",
    "做完",
    "完成",
    "当场",
    "允许",
    "可以",
    "同意",
    "答应",
    "我们",
    "你们",
    "他们",
    "这件",
    "这个",
    "一下",
    "范围",
}


def fulfilled_promise_contract(promised_result: str) -> str:
    """Turn an already-earned NPC promise into a strict response contract."""

    clean = str(promised_result or "").strip()
    return f"{FULFILLED_PROMISE_PREFIX}{clean}" if clean else ""


def is_fulfilled_promise_contract(contract: str) -> bool:
    return str(contract or "").strip().startswith(FULFILLED_PROMISE_PREFIX)


def promised_result_from_contract(contract: str) -> str:
    text = str(contract or "").strip()
    if not is_fulfilled_promise_contract(text):
        return ""
    return text[len(FULFILLED_PROMISE_PREFIX) :].strip()


def is_disclosure_promise(promised_result: str) -> bool:
    """Return whether paying the promise requires saying actual information."""

    text = str(promised_result or "")
    disclosure = r"(?:说出|说清|说明|告诉|告知|透露|公开|交代|指出|指明|念出|读出|回答|揭示)"
    route_delivery = r"(?:给出|提供|给|交给|指出|指明|告诉|告知|说明|说清)"
    subject = (
        r"(?:信息|情报|线索|内容|答案|事实|真相|原因|目标|动机|名字|人名|地名|"
        r"地点|方向|走法|去路|路线|路径|入口|出口|口令|暗号|字|音节|片段|一段)"
    )
    return bool(
        re.search(rf"{disclosure}.{{0,30}}{subject}", text)
        or re.search(rf"{subject}.{{0,20}}{disclosure}", text)
        # NPCs often phrase a promise as “继续谈旧路，把我知道的内容说
        # 出来”.  That is still an information payoff, not permission to
        # announce that they will speak on a later turn.  The earlier matcher
        # only recognised an explicit disclosure verb next to a named subject,
        # so this common natural wording could silently defer an earned clue.
        or re.search(
            r"(?:把|将).{0,18}(?:所知|知道|掌握).{0,12}"
            r"(?:内容|详情|情报|线索|来历|情况).{0,12}"
            r"(?:说|讲|告诉|交代|说明|透露)",
            text,
        )
        or re.search(
            r"(?:继续|接着|现在|随后)?(?:谈|说|讲).{0,24}"
            r"(?:旧路|路线|来历|真相|情况|内容|详情|情报|线索)",
            text,
        )
        # "给出一条安全路线" is an information payoff even though it does
        # not use the narrower verbs above.  Treat it as disclosure so a
        # fulfilled condition has to produce an actual direction, rather than
        # leaking the promise wording back to players as a fake answer.
        or re.search(
            rf"{route_delivery}.{{0,30}}(?:方向|走法|去路|路线|路径|入口|出口)",
            text,
        )
        or re.search(
            rf"(?:方向|走法|去路|路线|路径|入口|出口).{{0,20}}{route_delivery}",
            text,
        )
    )


def disclosure_delivery_failure(contract: str, plan: Mapping[str, object]) -> str:
    """Reject a fulfilled information promise that only announces later speech."""

    promised_result = promised_result_from_contract(contract)
    if not promised_result or not is_disclosure_promise(promised_result):
        return ""

    speech_act = str(plan.get("speech_act") or "answer").strip()
    direct = str(plan.get("direct_answer") or "").strip()
    facts = [
        str(item).strip()
        for item in (plan.get("facts_to_share") or [])
        if str(item).strip()
    ]
    answer = "；".join(item for item in (direct, *facts) if item)
    if speech_act in {"refuse", "admit_unknown", "condition"}:
        return "NPC refused, deferred, or reopened an already-earned disclosure promise"
    if _contains_promised_information(answer, facts=facts, promised_result=promised_result):
        return ""
    return "NPC announced an already-earned disclosure but did not provide its concrete content"


def is_decision_promise(promised_result: str) -> bool:
    """Return whether the promised result requires an explicit yes/no judgment.

    ``决定是否放行`` is not itself a result.  The NPC must say whether the
    road is open, denied, or otherwise allowed with a concrete limitation.
    """

    return bool(
        re.search(
            r"(?:决定|判断|裁定|表态).{0,12}(?:是否|要不要|能否|可否|该不该|让不让|准不准|放不放)"
            r"|(?:是否|要不要|能否|可否).{0,18}(?:放行|开放|准许|允许|通行|离开|进入|通过)",
            str(promised_result or ""),
        )
    )


def is_nonfinal_promise_result(promised_result: str) -> bool:
    """Return whether a stated payoff only defers the NPC's real response.

    A scene condition must buy a player-visible concession.  "I will keep
    reviewing it" and "I will decide whether to allow it" merely describe the
    NPC's internal process, so persisting them as promises creates a loop: the
    heroes fulfil a price and the NPC can ask them to wait again.
    """

    text = " ".join(str(promised_result or "").split()).strip()
    if not text:
        return True
    if is_decision_promise(text):
        return True

    deferred_process = bool(
        re.search(
            r"(?:继续|再|仍|先|随后|之后|等到|待).{0,10}"
            r"(?:审查|核验|查验|评估|衡量|考虑|观察|确认|看看|处理|研究)"
            r"|(?:审查|核验|查验|评估|衡量|考虑).{0,20}"
            r"(?:旧路|放行|资格|风险|这件事|此事)"
            r"|(?:视情况|看情况|以后再说|之后再定|容后再议)",
            text,
        )
    )
    if not deferred_process:
        return False

    # A promise may mention an investigation while still committing a concrete
    # outcome, such as opening a gate or giving the heroes a named document.
    # Keep those bargains valid; only reject a process with no actual payoff.
    visible_outcome = bool(
        re.search(
            r"(?:我|我们|守望会|这边)?(?:会|将|就|便|立刻|马上|当场).{0,4}"
            r"(?:放行|开放|准许|允许|开门|移开|交出|交给|提供|归还|"
            r"带路|领路|护送|派(?:人|巡守|向导)|说出|告诉|告知|透露|公开|"
            r"作证|登记|记录|保管|保护|协助|支援|撤离|解除|停止|取消|恢复)"
            r"|(?:旧路|通道|门|闸门).{0,10}(?:获准|可(?:以)?通行|已(?:经)?开放|会(?:被)?开放)"
            r"|(?:交给|交出|提供|带路|领路|护送|说出|告诉|告知|透露|公开|作证|登记|保管)"
            r".{0,16}(?:你们|你|英雄|队伍)",
            text,
        )
    )
    return not visible_outcome


def is_current_action_permission_bargain(
    player_message: str,
    plan: Mapping[str, object],
) -> bool:
    """Return whether an NPC mislabeled permission for *this* action as a bargain.

    A player may already be carrying out an action while asking the NPC to stay
    within a visible boundary.  ``可以，但别碰印记`` is an immediate answer,
    not a condition whose reward is ``允许你这么做``.  Treating that exchange as
    a bargain creates a self-referential clockwork loop: the player must first
    do the action in order to earn permission to do that same action.

    This deliberately requires all three texts to share both an action family
    and a concrete reference.  It therefore leaves real conditions intact, for
    example ``先签担保，才允许你走旧路``: signing and travelling are different
    actions and their concrete references do not overlap.
    """

    message = " ".join(str(player_message or "").split()).strip()
    condition = " ".join(str(plan.get("condition") or "").split()).strip()
    promised_result = " ".join(str(plan.get("promised_result") or "").split()).strip()
    direct_answer = " ".join(str(plan.get("direct_answer") or "").split()).strip()
    speech_act = str(plan.get("speech_act") or "").strip()
    if not message or not condition or speech_act != "condition":
        return False
    permission_text = " ".join(item for item in (promised_result, direct_answer) if item)
    if not permission_text or not _CURRENT_ACTION_DECLARATION.search(message):
        return False
    if not _PERMISSION_WORDS.search(permission_text):
        return False

    message_families = _action_families(message)
    condition_families = _action_families(condition)
    permission_families = _action_families(permission_text)
    if not (
        message_families
        and message_families & condition_families
        and message_families & permission_families
    ):
        return False
    return (
        _shares_concrete_action_reference(message, condition)
        and _shares_concrete_action_reference(message, permission_text)
    )


def _action_families(text: str) -> set[str]:
    return {
        family
        for family, pattern in _ACTION_FAMILY_PATTERNS.items()
        if pattern.search(str(text or ""))
    }


def _shares_concrete_action_reference(left: str, right: str) -> bool:
    """Use short Chinese fragments as a conservative, local referent check."""

    left_pairs = _action_fragments(left, width=2)
    right_pairs = _action_fragments(right, width=2)
    left_triples = _action_fragments(left, width=3)
    right_triples = _action_fragments(right, width=3)
    if left_triples & right_triples:
        return True
    return len(left_pairs & right_pairs) >= 3


def _action_fragments(text: str, *, width: int) -> set[str]:
    clean = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(text or ""))
    if len(clean) < width:
        return set()
    return {
        clean[index : index + width]
        for index in range(len(clean) - width + 1)
        if clean[index : index + width] not in _GENERIC_ACTION_FRAGMENTS
    }


def decision_delivery_failure(contract: str, plan: Mapping[str, object]) -> str:
    """Reject a fulfilled decision promise that merely postpones its answer."""

    promised_result = promised_result_from_contract(contract)
    if not promised_result or not is_decision_promise(promised_result):
        return ""

    speech_act = str(plan.get("speech_act") or "answer").strip()
    direct = str(plan.get("direct_answer") or "").strip()
    facts = [
        str(item).strip()
        for item in (plan.get("facts_to_share") or [])
        if str(item).strip()
    ]
    answer = "；".join(item for item in (direct, *facts) if item)
    if speech_act in {"condition", "admit_unknown", "deflect"}:
        return "NPC deferred an already-earned decision promise"
    if _contains_explicit_decision(answer):
        return ""
    return "NPC announced that it would decide but did not actually allow or refuse"


def promise_delivery_failure(contract: str, plan: Mapping[str, object]) -> str:
    """Return the first unmet concrete-delivery requirement for a promise."""

    return disclosure_delivery_failure(contract, plan) or decision_delivery_failure(contract, plan)


def plan_delivers_disclosure(plan: Mapping[str, object], promised_result: str) -> bool:
    contract = fulfilled_promise_contract(promised_result)
    return bool(contract and not disclosure_delivery_failure(contract, plan))


def _contains_explicit_decision(answer: str) -> bool:
    """Recognise the actual public result of a gatekeeper-like decision."""

    text = " ".join(str(answer or "").split()).strip()
    if not text:
        return False
    denial = (
        r"不(?:予|给)?(?:放行|开放|准许|允许)|不让(?:你们|你)?(?:走|过|进入|通过)|"
        r"不能(?:让|准许|允许|放)|拒绝|暂不(?:放行|开放|准许|允许)"
    )
    allowance = (
        r"(?:准许|允许|放行|开放|同意)(?:你们|你)?(?:走|过去|通过|进入|离开)?|"
        r"(?:可以|准)(?:走|过去|通过|进入|离开)|"
        r"(?:决定|裁定)(?![^。！？]{0,12}(?:是否|要不要|能否|可否|该不该|让不让))"
        r"[^。！？]{0,10}(?:让|准许|允许|放行).{0,12}(?:走|过|进入|通过|离开)|"
        r"(?:旧路|通道|门|闸门).{0,10}(?:开了|已开|开放|可通行)|"
        r"(?:你们|你).{0,8}(?:可以|准许|获准).{0,8}(?:走|过|进入|通过|离开)"
    )
    return bool(re.search(denial, text) or re.search(allowance, text))


def _contains_promised_information(
    answer: str,
    *,
    facts: list[str],
    promised_result: str,
) -> bool:
    text = " ".join(str(answer or "").split()).strip()
    if not text:
        return False
    if _is_route_disclosure(promised_result):
        return _contains_route_payload(text)
    if re.search(r"(?:名字|人名|地名|叫什么|身份|哪一位|谁)", promised_result):
        return _contains_named_payload(text, promised_result=promised_result)

    for fact in facts:
        if _is_concrete_fact(fact, promised_result=promised_result):
            return True
    quoted = re.findall(r"[‘“「『\"']([^’”」』\"']{2,40})[’”」』\"']", text)
    if any(_is_new_payload(item, promised_result) for item in quoted):
        return True
    colon_payload = re.search(r"(?:是|为|如下|内容)[：:]\s*(?P<value>[^。！？]{2,80})", text)
    return bool(
        colon_payload
        and _is_new_payload(colon_payload.group("value"), promised_result)
        and not _looks_deferred(colon_payload.group("value"))
    )


def _is_route_disclosure(promised_result: str) -> bool:
    return bool(re.search(r"(?:方向|走法|去路|路线|路径|入口|出口|怎么走|如何走)", promised_result))


def _contains_route_payload(text: str) -> bool:
    if re.search(r"(?:东北|东南|西北|西南|东|南|西|北)(?:边|面|方|侧|方向)?", text):
        return True
    if re.search(
        r"从[^。；]{1,36}(?:往|向|沿|穿过|经过|绕过|走到|走进|转向|拐进|进入)",
        text,
    ):
        return True
    if re.search(r"(?:沿着|顺着|穿过|经过|绕过|越过|转向|拐进)[^。；]{2,40}", text):
        return True
    if re.search(
        r"(?:先|第一步|出门后|离开后)[^。；]{2,40}(?:然后|再|之后|接着|直到|转|拐|进入)",
        text,
    ):
        return True
    return bool(re.search(r"(?:入口|出口|路口)[^。；]{0,16}(?:在|位于|藏在|通向|通往)", text))


def _contains_named_payload(text: str, *, promised_result: str) -> bool:
    candidates = re.findall(
        r"(?:叫|名叫|名为|名字是|身份是|那个人是|由)([\u4e00-\u9fffA-Za-z0-9·]{2,20})",
        text,
    )
    if any(_is_new_payload(item, promised_result) for item in candidates):
        return True
    quoted = re.findall(r"[‘“「『\"']([^’”」』\"']{1,24})[’”」』\"']", text)
    return any(_is_new_payload(item, promised_result) for item in quoted)


def _is_concrete_fact(fact: str, *, promised_result: str) -> bool:
    clean = str(fact or "").strip()
    return bool(
        len(_normalize(clean)) >= 6
        and _is_new_payload(clean, promised_result)
        and not _looks_deferred(clean)
    )


def _is_new_payload(value: str, promised_result: str) -> bool:
    key = _normalize(value)
    promise_key = _normalize(promised_result)
    return bool(key and key not in promise_key and promise_key not in key)


def _looks_deferred(text: str) -> bool:
    return bool(
        re.search(
            r"(?:我|他|她)?(?:现在|接下来|之后)?(?:会|可以|能|愿意|准备|打算)"
            r"(?:先|只|马上|现在)?(?:说|告诉|说明|透露|公开|交代|指出|给出)"
            r"|(?:等|待|之后|下一步).{0,12}(?:再说|再告诉|再说明|再透露)",
            str(text or ""),
        )
    )


def _normalize(value: str) -> str:
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(value or "")).lower()
