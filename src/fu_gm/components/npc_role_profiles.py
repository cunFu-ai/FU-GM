from __future__ import annotations

import re
from typing import Mapping


DEFAULT_AUTHORITY_SCOPE = (
    "只能说明自己的经历、作出自身选择，或处理其公开职责内的事项；"
    "不能替其他人物或势力作出承诺"
)

_GENERIC_GOALS = {
    "",
    "在当前局势中保护自己负有责任的人、地点或职责",
    "面对当前局势，并依照自身处境作出选择",
}
_GENERIC_REFUSALS = {
    "",
    "说明不能配合的具体理由，并采取符合职责的行动",
}
_ACCESS_FOCI = (
    "旧路",
    "闸门",
    "侧门",
    "后门",
    "通道",
    "入口",
    "关口",
    "渡口",
    "航线",
    "码头",
)


def local_role_profile(target: str, *, context: str = "") -> dict[str, str]:
    """Return only the authority naturally implied by a visible local role.

    The profile is deliberately narrow.  It makes a gatekeeper playable without
    inventing a faction-wide mandate, a secret, or a pre-written plot outcome.
    ``context`` may identify the local object under that role's control, such as
    an old road or gate.
    """

    clean = str(target or "").strip()
    focus = _access_focus(context)
    if re.search(r"(?:会长|长老|首领|负责人)", clean):
        if focus:
            return {
                "role_in_story": "当前地点的组织负责人",
                "goal_now": (
                    f"在保护现场人员与本地秩序的前提下，决定【{focus}】是否开放，"
                    "以及由谁承担眼前风险。"
                ),
                "leverage": f"【{focus}】的即时许可、本地巡守与带路安排的调度权",
                "authority_scope": (
                    f"能决定当前地点内【{focus}】的临时开放、放行条件与巡守带路；"
                    "无权替远处势力承诺，也不能改写未公开事实。"
                ),
                "knowledge_scope": (
                    f"熟悉当前地点与【{focus}】入口附近的普通路线、地标和警戒安排；"
                    "不了解远处势力的秘密部署。"
                ),
                "refusal_move": f"维持【{focus}】的警戒，并要求来者先说明去向与责任归属。",
                "voice_cue": "说话稳而简短，先给出自己此刻能承担的部分。",
            }
        return {
            "role_in_story": "当前地点的组织负责人",
            "goal_now": "先保住在场的人与地方秩序，再决定本组织此刻能给出的协助。",
            "leverage": "本地巡守、临时引导与公开安排的调度权",
            "authority_scope": (
                "能决定本组织在当前地点的临时协助、巡守带路与已公开范围内的路线指引；"
                "无权替远处势力承诺，也不能改写未公开的事实。"
            ),
            "knowledge_scope": "熟悉当前地点、日常路线和本组织公开使用的信号；不了解远处势力的秘密部署。",
            "refusal_move": "先把关键人物留在可信任的视线内，并要求把眼前风险说明白。",
            "voice_cue": "说话稳而简短，先给出自己此刻能承担的部分。",
        }
    if re.search(r"(?:失忆|失名).{0,4}(?:旅人|旅客)|(?:旅人|旅客).{0,4}(?:失忆|失名)", clean):
        return {
            "role_in_story": "需要保护、仍在拼回记忆的当事人",
            "goal_now": "留在可信任的人身边，辨认自己尚未消失的记忆与身体反应。",
            "leverage": "自己能确认的感官记忆、恐惧与路线片段",
            "authority_scope": (
                "能决定是否跟随、是否说出自己还记得的事；"
                "不能替守望会等当地组织决定通行许可，也不能替其他人物安排行动。"
            ),
            "knowledge_scope": "只熟悉自己仍能确认的感官记忆、经历和当下所见；记忆空白处可以明确说不确定。",
            "refusal_move": "退到信任的人身边，拒绝单独离开或替别人作决定。",
            "voice_cue": "用短句回答，区分亲眼记得的事与不敢确定的空白。",
        }
    if re.search(r"(?:守闸|闸门.{0,4}(?:守|值守)|闸卫)", clean):
        return {
            "role_in_story": "眼前闸门的值守者",
            "goal_now": "守住眼前闸门，只在现场风险可控时决定暂缓、半开或放行。",
            "leverage": "闸门的即时开闭权",
            "authority_scope": (
                "能决定眼前闸门的暂缓、半开和按当前现场规则放行；"
                "无权替所属势力承诺更大的安排。"
            ),
            "knowledge_scope": "熟悉眼前闸门、相邻通道、日常开闭规则和附近警戒点。",
            "refusal_move": "维持闸门戒备，要求相关人员留在可见范围内。",
            "voice_cue": "说话简短，先问清责任与风险。",
        }
    if re.search(r"(?:守门人|守门者|门卫)", clean):
        return {
            "role_in_story": "眼前入口的守门人",
            "goal_now": "守住入口，只让眼前局面能够承担的人通过。",
            "leverage": "入口的即时开闭与通报权",
            "authority_scope": (
                "能决定眼前入口的暂缓、放行或通报；"
                "无权替所属势力承诺更大的安排。"
            ),
            "knowledge_scope": "熟悉眼前入口、相邻通道、日常访客流程和附近警戒点。",
            "refusal_move": "守住门口并要求来者停在可见范围内。",
            "voice_cue": "说话简短，先问清来意与风险。",
        }
    if re.search(r"(?:巡守|守巡|守卫|哨兵|看守|值守)", clean):
        return {
            "role_in_story": "当前现场的警戒人员",
            "goal_now": "维持眼前警戒线，防止局面失控。",
            "leverage": "现场的拦截、警戒与通报权",
            "authority_scope": (
                "能安排眼前警戒、暂时拦截或通报上级；"
                "无权替所属势力作出更大的承诺。"
            ),
            "knowledge_scope": (
                "熟悉自己正在值守或带领的眼前路线、普通地标、遮蔽点和公开警戒信号；"
                "不能凭空确认远处敌人的秘密部署、能力或意图。"
            ),
            "refusal_move": "维持警戒并把相关人员留在可见范围内。",
            "voice_cue": "措辞克制，优先确认风险与秩序。",
        }
    return {}


def enrich_role_record(
    record: Mapping[str, object],
    *,
    target: str,
    context: str = "",
) -> dict[str, str]:
    """Fill generic role placeholders without overwriting authored details."""

    result = {str(key): str(value or "").strip() for key, value in record.items()}
    profile = local_role_profile(target, context=context)
    if not profile:
        return result
    if result.get("goal_now", "") in _GENERIC_GOALS:
        result["goal_now"] = profile["goal_now"]
    if not result.get("leverage"):
        result["leverage"] = profile["leverage"]
    if result.get("authority_scope", "") in {"", DEFAULT_AUTHORITY_SCOPE}:
        result["authority_scope"] = profile["authority_scope"]
    if not result.get("knowledge_scope"):
        result["knowledge_scope"] = profile.get("knowledge_scope", "")
    if result.get("refusal_move", "") in _GENERIC_REFUSALS:
        result["refusal_move"] = profile["refusal_move"]
    if not result.get("voice_cue"):
        result["voice_cue"] = profile["voice_cue"]
    if result.get("if_blocked", "") in _GENERIC_REFUSALS:
        result["if_blocked"] = profile["refusal_move"]
    if not result.get("if_helped") or result.get("if_helped") == "在自身权限范围内提供明确帮助":
        result["if_helped"] = "在自身权限范围内立即兑现已经答应的帮助"
    return result


def _access_focus(context: str) -> str:
    source = str(context or "")
    return next((item for item in _ACCESS_FOCI if item in source), "")
