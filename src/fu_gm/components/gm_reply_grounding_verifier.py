from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from fu_gm.gm_tool_contracts import GMToolReceipt, json_safe_value
from fu_gm.llm_utils import extract_json_object
from fu_gm.prompt_cache import build_cache_friendly_messages


REPLY_GROUNDING_SYSTEM_PROMPT = """
你是FU-GM公开回复的事实一致性审计器，不负责续写剧情。判断拟发布回复是否只使用了本轮玩家原话、最近公开聊天、当前权威状态和成功工具回执能够支持的事实。

判定规则：
1. 玩家可以声明自己角色的意图、言语和动作，但不能单方面决定NPC反应、敌人落败、物品到手、线索出现、场景抵达、环境变化、检定结果或其他外部结果。
2. 工具回执是本轮新增外部事实的唯一依据。失败回执不能支持成功叙述；当前状态只能支持已经存在的事实。
3. 新的NPC台词、NPC行动、环境反应、战斗结果、位置迁移、获得或失去物品、公开线索和命刻变化，都需要对应成功回执。仅仅纠正玩家不成立的前提、解释规则、回答后台状态查询或询问必需参数，不算新增外部事实。
4. 不要求逐字复述回执，但不能扩大、倒置或补完回执没有提交的结果。玩家说“示意递出牌子”不等于“牌子已经被接走”；“寻找藏身处”不等于“已经抵达藏身处”。
5. 玩家问题中的前提也必须有公开依据。“刚才谁提到了庄园？”不能证明有人提过庄园；若最近公开聊天和权威状态均无依据，确认该前提、为它虚构说话者或在后文沿用它，都属于unsupported_external_result。此类回忆核对只能澄清公开对话中没人提过或玩家可能听错，不能借玩家误提的词在同一回复中首次揭示相关私密事实。
6. 只做审计，不输出给玩家的替代叙事，不泄露私密状态。

只输出一个JSON对象：
{"valid":true|false,"category":"grounded|unsupported_external_result|contradicts_state|failed_receipt_claim|needs_tool","unsupported_claims":["简短列出"],"correction_hint":"告诉核心GM应调用哪类工具或如何只澄清现状"}
""".strip()


TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT = """
你是FU-GM工具写入前的语义一致性审计器，不负责续写剧情。核心GM尚未执行拟议工具；你要判断这项提案是否可以安全写入权威状态。

判定规则：
1. current_message中的玩家话语只能证明该玩家角色明确说过、尝试过或选择过什么，不能证明NPC已经回应、动作成功、线索出现、物品易手、场景抵达、检定通过或环境已经变化。
2. 玩家问题、猜测和条件句中的前提不是既成事实。“刚才谁提到了庄园？”不能证明有人提过庄园；工具不得虚构说话者，再把这一错误前提写入NPC记忆或场景事实。若本轮主要是在核对“刚才谁说过/是否提过X”，只能依据recent_public_context回答这项回忆问题；若没有公开依据，应只澄清没人提过或玩家可能听错，不能借这个错误前提在同一回应里首次揭示X相关的隐藏事实。
3. NPC回应和NPC行动工具可以让NPC在本轮首次说话或行动，但内容必须符合当前NPC人格、动机、知识、权限、位置、既有承诺与场景状态。它可以在玩家真正询问NPC所知内容时揭示NPC原本知道且有理由公开的事实，却不能把一次错误前提核对偷换成新的情报揭示，也不能伪造此前公开发生过的言行。
4. 场景回应、开场和转场工具可以在GM权限内首次建立环境变化或新场景素材，但不得与已公开事实冲突，不得把玩家尚未完成的意图写成结果，也不得把GM私密暗线冒充成玩家已经知道的事实。
5. NPC建档或状态修订只能来自当前玩家明确贡献、当前权威状态、已提交结果，或GM在当前场景中有权新引入的内容；不能把玩家的提问性前提当证据。
6. resolve_rule_window的InvokeTrait必须满足两项：invocation_rationale确实是玩家当前消息中亲自给出的理由；该理由能说明所选身份、主题或故乡为何有助于当前检定。核心GM不得替玩家补写相关性。
7. declare_check_action、declare_movement_check或perform_check_action中的success_observation必须已经填实。若只写“一件能派上用场的物件”“一条具体痕迹”“具体方向与目的地线索”“某种办法”等，却没有给出物件名、痕迹内容、方向地点或办法本身，判为needs_clarification；要求核心GM从当前局面和私有准备中选定实际答案后重提，不能把占位句留到检定成功后。
8. end_session的closing_image必须只含当前公开状态能够支持的画面，并在同一意象中呈现本场实际选择造成的变化；不能为凑漂亮结尾宣称未完成的逃脱、团聚、胜利或取得物。
9. perform_in_scene_action只适合同一场景内的站位和确定性小动作。玩家明确“进入/抵达/离开”另一个具名地点时应使用move_scene_group；同一句先移动、再观察或调查时，不能只提交移动后静默结束，same_batch_proposals中还必须包含对应检定声明，或由最具体的复合工具完整处理。
10. end_conflict若在outcome或public_reply中声称某个玩家角色已经撤离、逃出或抵达另一地点，必须在exit_transitions中为该角色提交实际目的地；只用文字结束冲突而不改变位置，判为needs_clarification。
11. commit_story_item_action必须覆盖current_message中该物件动作结束时的完整最终状态。玩家先捡起、随后抛出、放下或留在别处时，只提交acquire属于半截意图，判为needs_clarification；应使用place和最终to_location一次落位。玩家把物件抛到、推到或放到另一名PC身边，不等于该PC已经接住或取得，除非对方本人已明确接受，否则不得使用transfer或填写to_holder。玩家已经完整公开了确定性动作且没有新的外部裁定结果时，public_result应为空，状态写入可以静默；不得为了确认写入而复述玩家动作。
12. 只审查提案，不执行工具，不输出面向玩家的叙事，也不泄露私密状态。

只输出一个JSON对象：
{"valid":true|false,"category":"grounded|unsupported_external_result|contradicts_state|false_premise|trait_rationale_unverified|needs_clarification","unsupported_claims":["简短列出"],"correction_hint":"告诉核心GM应如何修正提案、澄清错误前提或向玩家追问"}
""".strip()


GROUNDING_EVIDENCE_PROTOCOL = """
## 逐断言证据流程

审计前先把待审内容拆成最小事实断言，每条至少标出主语、动作或状态、对象、地点与时态，再按以下优先级寻找直接依据：成功工具回执高于当前权威状态，当前权威状态高于最近公开聊天，最近公开聊天高于玩家本轮对自己角色意图与动作的声明。低优先级内容不能覆盖高优先级事实；玩家的猜测、目的、条件句、比喻和问题前提都不能补足缺失证据。没有证据只表示不能确认，不要擅自把它判成相反事实。

逐条检查是否发生了“尝试变成功、意图变结果、递出变接收、寻找变抵达、看到变取得、准备变完成、NPC沉默变同意、失败回执变成功叙述、私密准备变公开线索”。只要其中一条外部结果缺少直接支持，整体valid必须为false，并在unsupported_claims中只列真正越界的最小断言。若所有外部变化都有成功回执或既有权威状态支持，措辞差异、合理的感官修饰和不改变事实的简短衔接不应误判。

correction_hint只指出需要补哪类权威工具、删去哪项无依据结论，或应向玩家追问哪个必要参数；不得代写新剧情、补造NPC反应或泄露尚未公开的私密内容。
""".strip()

REPLY_GROUNDING_SYSTEM_PROMPT = (
    REPLY_GROUNDING_SYSTEM_PROMPT
    + "\n\n"
    + GROUNDING_EVIDENCE_PROTOCOL
)
TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT = (
    TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    + "\n\n"
    + GROUNDING_EVIDENCE_PROTOCOL
)


@dataclass(frozen=True)
class GMReplyGroundingReview:
    valid: bool
    category: str = "grounded"
    unsupported_claims: tuple[str, ...] = field(default_factory=tuple)
    correction_hint: str = ""


class GMReplyGroundingVerifier:
    """Semantically reject public prose that outruns authoritative receipts."""

    def __init__(
        self,
        client: Any,
        *,
        model: str,
        max_output_tokens: int = 900,
    ) -> None:
        self.client = client
        self.model = str(model or "").strip()
        self.max_output_tokens = max(256, int(max_output_tokens))

    def verify(
        self,
        *,
        current_message: str,
        recent_context: str,
        observed_state: dict[str, object],
        receipts: list[GMToolReceipt],
        proposed_reply: str,
        message_kind: str,
        decision_reason: str,
        deadline: float,
    ) -> GMReplyGroundingReview:
        request = {
            "current_message": str(current_message or "").strip(),
            "recent_public_context": str(recent_context or "").strip(),
            "current_authoritative_state": observed_state,
            "successful_and_failed_receipts": [
                receipt.to_dict() for receipt in receipts
            ],
            "proposed_public_reply": str(proposed_reply or "").strip(),
            "core_message_kind": str(message_kind or "").strip(),
            "core_decision_reason": str(decision_reason or "").strip(),
        }
        response_format = (
            {"type": "json_object"}
            if bool(
                getattr(
                    getattr(self.client, "config", None),
                    "response_format_enabled",
                    True,
                )
            )
            else None
        )
        request_json = json.dumps(
            json_safe_value(request),
            ensure_ascii=False,
        )
        raw = self.client.create_chat_completion(
            model=self.model,
            messages=build_cache_friendly_messages(
                static_system_prompt=REPLY_GROUNDING_SYSTEM_PROMPT,
                user_content=request_json,
                cache_family="ground-reply",
                user_cache_breakpoint_offsets=(
                    request_json.find('"proposed_public_reply"'),
                ),
            ),
            temperature=0.0,
            response_format=response_format,
            max_tokens=self.max_output_tokens,
            deadline=deadline,
            operation="gm_reply_grounding_verification",
        )
        payload = extract_json_object(raw)
        if not isinstance(payload.get("valid"), bool):
            raise ValueError("回复事实审计缺少布尔字段valid。")
        claims = payload.get("unsupported_claims")
        if not isinstance(claims, list):
            claims = []
        return GMReplyGroundingReview(
            valid=bool(payload["valid"]),
            category=str(payload.get("category") or "grounded").strip(),
            unsupported_claims=tuple(
                str(item or "").strip()[:240]
                for item in claims[:6]
                if str(item or "").strip()
            ),
            correction_hint=str(payload.get("correction_hint") or "").strip()[:500],
        )

    def verify_tool_proposal(
        self,
        *,
        current_message: str,
        recent_context: str,
        observed_state: dict[str, object],
        tool_name: str,
        arguments: object,
        deadline: float,
        batch_context: list[dict[str, object]] | None = None,
    ) -> GMReplyGroundingReview:
        """Review a semantic write before the registry can mutate state."""

        request = {
            "current_message": str(current_message or "").strip(),
            "recent_public_context": str(recent_context or "").strip(),
            "current_authoritative_state": observed_state,
            "proposed_tool": {
                "tool_name": str(tool_name or "").strip(),
                "arguments": arguments,
            },
            "same_batch_proposals": list(batch_context or []),
        }
        response_format = (
            {"type": "json_object"}
            if bool(
                getattr(
                    getattr(self.client, "config", None),
                    "response_format_enabled",
                    True,
                )
            )
            else None
        )
        request_json = json.dumps(
            json_safe_value(request),
            ensure_ascii=False,
        )
        raw = self.client.create_chat_completion(
            model=self.model,
            messages=build_cache_friendly_messages(
                static_system_prompt=TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT,
                user_content=request_json,
                cache_family="ground-tool",
                user_cache_breakpoint_offsets=(
                    request_json.find('"proposed_tool"'),
                ),
            ),
            temperature=0.0,
            response_format=response_format,
            max_tokens=self.max_output_tokens,
            deadline=deadline,
            operation="gm_tool_proposal_grounding_verification",
        )
        payload = extract_json_object(raw)
        if not isinstance(payload.get("valid"), bool):
            raise ValueError("工具提案审计缺少布尔字段valid。")
        claims = payload.get("unsupported_claims")
        if not isinstance(claims, list):
            claims = []
        return GMReplyGroundingReview(
            valid=bool(payload["valid"]),
            category=str(payload.get("category") or "grounded").strip(),
            unsupported_claims=tuple(
                str(item or "").strip()[:240]
                for item in claims[:6]
                if str(item or "").strip()
            ),
            correction_hint=str(payload.get("correction_hint") or "").strip()[:500],
        )
