from __future__ import annotations

import json
import os
import re
from difflib import SequenceMatcher
from dataclasses import dataclass

from fu_gm.config import LLMConfig
from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.llm_utils import extract_json_object
from fu_gm.prompt_cache import build_cache_friendly_messages
from fu_gm.testing.legal_actions import LegalActionLayer
from fu_gm.testing.replay_models import LegalActionContext, ReplayStep
from fu_gm.testing.rule_glossary import FINAL_FABULA_GLOSSARY, RuleGlossary


PLAYER_NPC_RESPONSE_CONTRACT_PROMPT = """
你是多人跑团长测中的玩家回应契约审计器。判断candidate是否已经由actor直接回应NPC当前公开索要的事项。

必须满足：
1. candidate是在对requester或公开场景中的发问来源说话，而不是只对队友复述“之后我会怎么回答”。
2. remaining_items中的每一项prompt都在本句得到实际回应。回应可以是给出已知答案、明确拒绝回答，或明确承认不知道；
   不能把商量“要不要说”、准备稍后说、让别人回答、另做调查或提出反问算作回应。
3. 不要求玩家提供自己不知道的事实，也不评判回答是否能说服NPC；这里只判断玩家是否真的作出了回应。
4. answered_item_ids只能从remaining_items中的item_id选择。evidence必须是candidate中的连续逐字片段。

只输出JSON：
{"directed_to_requester":false,"answered_item_ids":[""],"complete":false,
 "evidence":"候选原文连续片段","reason":"一句中文理由"}
""".strip()


PLAYER_TABLE_DISCUSSION_REVIEW_PROMPT = """
你是多人跑团长测中的桌边讨论契约审计器。判断 candidate 是否仍然只是玩家对玩家的意见、疑问、优先级或
分工建议，而没有替任何角色或整个队伍执行虚构世界中的行动。你不写剧情、不修改候选，也不判断建议好坏。

以下属于桌边讨论：询问谁愿意负责某事；说自己倾向哪种方案；提醒同伴注意公开危险；尚未落实的条件式建议。
以下已经是角色行动：说“我先看/确认/调查/掩护/安抚”；虽然先说“如果”，但同时宣布角色现在开始观察、
移动、交付、施法或守住某处；以“那我们就/咱们先”直接替全队移动或执行方案；直接向 GM 或 NPC 提交行动。
“如果还没接稳，咱们就守住”前面若已经写“先看巡守是否接稳”，整句仍包含一次观察行动，不能算纯讨论。

evidence 必须逐字摘录 candidate 中最能证明是否越界的连续短片段。只输出 JSON：
{
  "pure_table_discussion": true/false,
  "commits_character_action": true/false,
  "commits_party_action": true/false,
  "directed_at_gm_or_npc": true/false,
  "evidence": "candidate中的连续短片段",
  "reason": "一句中文理由"
}
""".strip()


PLAYER_ACTION_PROGRESS_REVIEW_PROMPT = """
你是多人跑团长测中的玩家行动进展审计器。只判断 candidate 能否作为当前角色的一次新行动发给 GM；
不写剧情、不修改候选、不替 GM 判定结果。

请对照最近公开对话，严格区分“引用必要信息后采取新行动”和“把已知线索重新整理一遍冒充行动”：
- 有效：角色回应 GM 刚公开的新变化；对尚未处理的具体人物、物件或危险采取一个当前可执行的动作；
  利用已有线索作出选择；有明确战术目的的移动、防御、等待、交涉或调查。
- 无效：主体内容只是复述、总结、记下或转告已经公开的线索；重复已经完成的调查、警戒、交付或询问；
  只在末尾补一个没有新目的的站位、姿态、点头或“继续观察”，借此伪装成新行动。
- “GM还能再描述一个细节”不等于剧情有进展。若候选只是在上一次调查刚揭示的细小部件上继续拆出更小部件，
  而不会改变角色眼前的抉择、NPC关系或条件、威胁压力、资源、位置或行动方案，应判定为调查支线原地打转。
- 有意义的调查应当回答一个会影响下一步选择的问题，或取得能够交涉、避险、追踪、开启路线、对抗威胁的证据；
  同一物件的连续深入也只有在公开局面明确显示“还有关键问题待解”时才算推进。
- 新出现的纸角、划痕、粉末、锁槽、脚印等细节不会自动成为新的调查路线。如果最近一次调查已经给出足以行动的结论，
  或 NPC 已提出明确交换、邀请、阻挡与取舍，下一次行动应利用结论或回应取舍。除非 GM 明说该细节会阻挡当前决定、
  构成眼前危险或必须先查清，否则继续检查衍生细节应视为原路线套娃。
- 对同一物件、门路或证据最多允许一次为当前决定服务的深入追查；公开对话里若已连续出现两次观察/检查及其结果，
  第三次再检查其子部件，即使可能得到新描述，也应判定 repeats_micro_investigation_lane=true。
- 候选可以简短提及旧线索，但旧线索不能占据主要内容；新动作必须不仅要求 GM 回应，还应当有合理机会
  改变当前局面。直接作出选择、履行条件、改变关系、移动到已开放的新镜头或应对迫近危险，都属于实质推进。
- 不判断行动是否成功，也不要因战术不佳而否决。角色明确选择暂缓、守住位置或等待时机，只要这是对当前新局面的
  有意义决定，也可以视为新行动。
- NPC在回答问题时说“不能确认是A还是B”“不知道该找谁或交给谁”，只是陈述不确定性，不是要求玩家立刻二选一。
  只有NPC或GM明确把选项、条件、最后通牒或可执行邀请交给玩家时，才存在必须立即回应的公开取舍。角色转而处理
  同一场景中更迫近的公开危险，也属于 responds_to_current_pressure_or_choice=true。
- NPC若明确说“我只记得/只知道X”，或已经逐项说明不知道原因、同行者、来路、目的地等，这会形成当前公开的
  知识边界。没有新的外部证据、感官刺激、地点变化或NPC主动恢复记忆时，再把“最后记得什么”“谁带你来”
  “原本要去哪里”等相邻问法逐项拆开，仍是在重开同一条已耗尽的问答路线，应令
  reopens_exhausted_npc_knowledge_lane=true。措辞或子问题不同不等于新路线。
- NPC若刚刚已经明确承诺“出现某个触发时会做X”、接受一项警戒或同行分工，下一名玩家又问同一NPC
  “触发时你会做X还是Y”，但公开局面没有出现新的冲突条件，这不是新战术问题，而是在重开已经确定的安排。
  应令 actionable_result_or_explicit_choice_is_already_public=true、opens_another_detail_layer=true、
  uses_public_result_or_answers_choice=false，并令 repeats_completed_action=true。只有新事实使原安排无法执行或
  两项公开指令彼此冲突时，澄清才算推进。
- NPC或GM若已把眼前行动所需的路线、参与者职责、触发信号与立即可执行步骤说明清楚，且公开信息中没有真实冲突
  或缺失项，再追问“触发后具体交给谁”“第几步由谁喊什么”等不会改变是否或如何执行的程序细节，属于拖延而非推进。
  此时令 procedural_micro_clarification_after_sufficient_plan=true，并同时令
  actionable_result_or_explicit_choice_is_already_public=true、opens_another_detail_layer=true、
  uses_public_result_or_answers_choice=false。只有答案会实质改变路线、风险、职责或行动方法时，澄清才有效。
- 合法技能或法术不等于当前场景中有意义的行动。候选若消耗精神值、物资点、物语点或其他有限资源来获得抗性、治疗、
  强化或攻击效果，必须能从公开对话中找到与其直接相关的已观察威胁、可信预警、受伤目标、敌人或明确战术计划。
  例如现场没有公开风系危险，也没有人提出相关战术时，施放风系元素幕障只是无依据消耗资源；应令
  spends_limited_resource_without_public_tactical_basis=true、materially_advances_current_situation=false、
  responds_to_current_pressure_or_choice=false。不要仅因战术不够优秀而拒绝，只有公开局面完全不存在因果联系时才拒绝。
- 新证据只会重新开放与它直接相关的窄问题。例如新出现“伊瑟娅”的登记，可以问NPC是否认识伊瑟娅；若NPC回答
  没有具体记忆，这不会自动重新开放其全部来路、同行者与目的地。只有最近公开对话里确有介入的新证据或刺激，
  且candidate直接利用它，new_public_evidence_reopens_npc_knowledge_lane才为true。
- 指定角色只能控制自己。可以邀请、呼喊或建议其他玩家角色同行，但不能直接叙述其他玩家角色已经移动、同意、
  交付或行动；只有最近公开对话中每位相关玩家已经明确同意，或GM已经宣布“队伍/众人/一行人”执行该动作时，
  party_action_authorized_by_public_consensus才为true。单个玩家说“让队伍一起撤离”不构成全桌共识。
- NPC的回答、同意、拒绝、跟随与交付由GM控制。candidate可以向NPC提出请求，但若最近公开对话尚无该NPC的明确答复，
  不得把它写成已经配合；此时controls_npc_outcome_without_public_answer=true。若只是在同一句中请求NPC跟随，而没有
  宣称其已经答应或抵达，则不是越权。
- current_scene_location是当前角色此刻实际所在的位置。GM若说“队伍”“众人”或“一行人”已经进入、抵达或离开，
  默认所有当前参演玩家角色都随队完成转场；除非GM明确说某人留在后方，否则再次跟上、踏入或抵达同一地点是重复。
- authoritative_story_items 是已经公开、由规则状态持续追踪的唯一剧情物件。若物件已有holder，只有该holder能拿取、
  放置、嵌入、交付、使用或消耗它；另一角色必须先在公开对话与结构化状态中完成转交。口头提议、站在旁边、知道物件
  在哪里，均不等于已经持有。若物件没有holder，角色也只能在自己的权威位置与物件location相符时接触它。
- actor_locations 是角色最后已经完成的权威位置。角色可以声明一次公开允许的移动，但不能在尚未完成转场时直接操作
  另一地点的门、机关或物件，也不能把“前往某处”和“抵达后操作”压成已经全部完成的事实。公开聊天中的计划、示意与
  将来动作不能覆盖结构化位置或物件持有状态。
- 若candidate包含移动，必须单独判断该移动是在当前场景内部、前往当前可见的相邻位置，还是GM最新公开内容已明确开放或邀请的
  转场。只有这些情况movement_is_authorized_by_public_context=true；不能因为目的地曾在很早以前出现就放行。
- “向NPC提出新问题或请求其兑现下一步”不等于再次交付同一份信息。只有角色确实又把已被同一NPC接收的内容重新递交、
  转告或复述时，repeats_resolved_information_delivery=true。
- prior_rejected_action_attempts来自完全相同公开上下文中已被判为无进展的候选。若candidate只是换词重做其中同一行动，
  必须令matches_prior_rejected_lane=true；不能因为措辞变了就推翻同一公开事实下已经确认的判断。

请求中的 dramatic_progress_context 是根据公开实录形成的审计摘要，只用于判断当前还缺哪类进展；它不是角色可知的
新事实，也不能要求 candidate 提及其中措辞。若该字段为空，仍按公开对话判断。

recent_public_context以及请求中的已知角色、NPC、敌人、场景实体、既定事实、合法行动、法术和技能共同构成
public/legal context；角色卡上合法的能力不必在最近一句GM回复中重复出现。
evidence 应逐字摘录 candidate 或 recent_public_context 中能支撑判断的连续短片段，不得改写；它用于审计，
不要因为 evidence 格式不完美而把语义上明确有效的新行动判为无效。
只输出 JSON：
{
  "valid_action_progress": true/false,
  "mostly_restates_known_information": true/false,
  "repeats_completed_action": true/false,
  "concrete_new_action": true/false,
  "grounded_in_public_context": true/false,
  "materially_advances_current_situation": true/false,
  "repeats_micro_investigation_lane": true/false,
  "responds_to_current_pressure_or_choice": true/false,
  "actionable_result_or_explicit_choice_is_already_public": true/false,
  "uses_public_result_or_answers_choice": true/false,
  "opens_another_detail_layer": true/false,
  "procedural_micro_clarification_after_sufficient_plan": true/false,
  "spends_limited_resource_without_public_tactical_basis": true/false,
  "resource_tactical_basis_evidence": "逐字摘录公开对话中支持该资源用途的威胁、目标或计划；没有则空字符串",
  "matches_prior_rejected_lane": true/false,
  "reopens_exhausted_npc_knowledge_lane": true/false,
  "new_public_evidence_reopens_npc_knowledge_lane": true/false,
  "npc_knowledge_boundary_evidence": "逐字摘录NPC公开知识边界；没有则空字符串",
  "controls_other_player_characters": true/false,
  "party_action_authorized_by_public_consensus": true/false,
  "controls_npc_outcome_without_public_answer": true/false,
  "npc_outcome_already_public": true/false,
  "movement_claimed": true/false,
  "movement_is_authorized_by_public_context": true/false,
  "movement_authorization_evidence": "逐字摘录最近公开对话中的许可、邀请或当前局部地点；没有则空字符串",
  "violates_story_item_custody": true/false,
  "story_item_custody_evidence": "逐字写出authoritative_story_items中冲突的物件、持有者或位置；没有则空字符串",
  "acts_outside_authoritative_actor_location": true/false,
  "actor_location_evidence": "逐字写出actor_locations中的角色位置与candidate试图操作的地点；没有则空字符串",
  "repeats_resolved_information_delivery": true/false,
  "evidence": "逐字短片段",
  "reason": "一句中文理由"
}
""".strip()


@dataclass
class SimulatedUtterance:
    text: str
    used_fallback: bool = False
    validation_errors: list[str] | None = None
    prompt_preview: str = ""
    model_attempts: list[dict[str, object]] | None = None
    fallback_kind: str = ""
    fallback_diagnostics: list[str] | None = None


class ConstrainedPlayerSimulator:
    _GM_SPEAKERS = {"时悠", "GM"}
    _KNOWN_PLAYER_SPEAKERS = {
        "阿凛",
        "南星",
        "白河",
        "时雨",
        "澄砚",
        "伊莉雅",
        "赛璃",
        "洛岚",
        "艾薇娅",
        "苍祈",
        "玩家",
    }
    _NON_ENTITY_TARGET_PATTERN = re.compile(
        r"谁|哪(?:个|位|里|种|一)|什么|怎么|如何|为何|为什么|是否|能不能|要不要|有没有|"
        r"有把握|方便(?:去|来|做|负责)?|愿意(?:去|来|做|负责)?|倾向于|你们觉得|大家觉得|"
        r"谁来|谁去|谁先|谁负责|来负责|来处理"
    )

    def __init__(
        self,
        *,
        use_llm: bool = False,
        client: OpenAICompatibleClient | None = None,
        model: str = "",
        glossary: RuleGlossary = FINAL_FABULA_GLOSSARY,
        continue_on_invalid: bool = False,
    ) -> None:
        self.glossary = glossary
        self.legal_action_layer = LegalActionLayer()
        self.client = client
        self.model = model
        self.allow_fallback = True
        self.continue_on_invalid = bool(continue_on_invalid)
        if use_llm and self.client is None:
            config = LLMConfig.from_env()
            self.allow_fallback = bool(config.allow_heuristic_fallback)
            if config.api_key:
                self.client = OpenAICompatibleClient(config)
                self.model = model or os.environ.get("FU_GM_REPLAY_PLAYER_MODEL", "").strip() or config.action_model
        self.use_llm = bool(use_llm and self.client and self.model)
        # Fixed-response unit-test clients cannot answer an additional schema.
        # Real long runs let Luna review ambiguous duplicate judgments instead
        # of allowing keyword overlap to veto a materially new action.
        self.semantic_validation_review = bool(
            self.use_llm and "fake" not in str(self.model or "").strip().lower()
        )
        self.last_action_progress_review: dict[str, object] = {}
        self.last_table_discussion_review: dict[str, object] = {}
        self._prior_rejected_action_attempts: list[dict[str, object]] = []

    def compose(
        self,
        *,
        step: ReplayStep,
        legal_context: LegalActionContext,
        last_gm_reply: str = "",
        recent_public_context: str = "",
    ) -> SimulatedUtterance:
        self._prior_rejected_action_attempts = []
        # Proactive GM beats are public even when they arrive through the
        # heartbeat endpoint. Keep the caller's newest table-facing reply
        # authoritative if an upstream context collector omitted it.
        recent_public_context = self._with_latest_gm_reply(
            recent_public_context,
            last_gm_reply,
        )
        fallback = self._fallback_utterance(
            step,
            legal_context,
            last_gm_reply=last_gm_reply,
            recent_public_context=recent_public_context,
        )
        fallback, fallback_validation_errors = self._validated_fallback_utterance(
            fallback,
            step=step,
            legal_context=legal_context,
            recent_public_context=recent_public_context,
            last_gm_reply=last_gm_reply,
        )
        followup = self._followup_to_gm_prompt(step, legal_context, last_gm_reply)
        if self._is_confirmation_step(step):
            followup = ""
        if self._has_complete_character_hint(step):
            followup = ""
        if step.message:
            if followup and not self._message_answers_gm_prompt(
                step.message,
                last_gm_reply,
                legal_context=legal_context,
            ):
                errors = self.validate(
                    followup,
                    step=step,
                    legal_context=legal_context,
                    recent_public_context=recent_public_context,
                )
                errors = self._review_candidate_semantics(
                    followup,
                    errors,
                    step=step,
                    legal_context=legal_context,
                    recent_public_context=recent_public_context,
                )
                if not errors:
                    return SimulatedUtterance(text=followup, used_fallback=False, validation_errors=[])
            errors = self.validate(
                step.message,
                step=step,
                legal_context=legal_context,
                recent_public_context=recent_public_context,
            )
            errors = self._review_candidate_semantics(
                step.message,
                errors,
                step=step,
                legal_context=legal_context,
                recent_public_context=recent_public_context,
            )
            if not errors:
                return SimulatedUtterance(text=step.message, used_fallback=False, validation_errors=[])
            fallback_validation_errors = self._review_candidate_semantics(
                fallback,
                fallback_validation_errors,
                step=step,
                legal_context=legal_context,
                recent_public_context=recent_public_context,
            )
            if fallback_validation_errors:
                if self.continue_on_invalid:
                    return self._exhaustion_safe_pass(
                        step,
                        diagnostics=[*errors, *fallback_validation_errors],
                        legal_context=legal_context,
                        recent_public_context=recent_public_context,
                        last_gm_reply=last_gm_reply,
                    )
                raise ValueError(
                    "FU-PL scripted message and fallback both failed semantic validation: "
                    f"message={errors}; fallback={fallback_validation_errors}"
                )
            return SimulatedUtterance(
                text=fallback,
                used_fallback=True,
                validation_errors=list(errors),
            )
        if followup:
            errors = self.validate(
                followup,
                step=step,
                legal_context=legal_context,
                recent_public_context=recent_public_context,
            )
            errors = self._review_candidate_semantics(
                followup,
                errors,
                step=step,
                legal_context=legal_context,
                recent_public_context=recent_public_context,
            )
            if not errors:
                return SimulatedUtterance(text=followup, used_fallback=False, validation_errors=[])
        if not self.use_llm or self.client is None:
            return SimulatedUtterance(
                text=fallback,
                used_fallback=True,
                validation_errors=fallback_validation_errors,
            )

        prompt = self._build_prompt(
            step,
            legal_context,
            last_gm_reply,
            recent_public_context=recent_public_context,
        )
        repair_errors: list[str] = []
        model_attempts: list[dict[str, object]] = []
        is_table_discussion = "正在和其他玩家短暂商量" in str(step.stage_goal or "")
        # Three candidates are enough to expose and repair one bad action
        # lane without turning one simulated player turn into an unbounded
        # chain of model generation and review requests.
        for attempt in range(3):
            attempt_prompt = prompt
            if repair_errors:
                attempt_prompt += (
                    "\n\n上一条候选没有通过玩家边界校验。请按下面的自然语言要求完全改写：\n- "
                    + "\n- ".join(self._repair_instructions(repair_errors, legal_context))
                )
                rejected = [
                    str(item.get("candidate") or "").strip()
                    for item in self._prior_rejected_action_attempts[-3:]
                    if str(item.get("candidate") or "").strip()
                ]
                if rejected:
                    attempt_prompt += (
                        "\n已经判定无效的行动方向如下；不要换词重写它们，必须改换现场对象或手段：\n- "
                        + "\n- ".join(rejected)
                    )
            try:
                raw = self.client.create_chat_completion(
                    model=self.model,
                    messages=build_cache_friendly_messages(
                        static_system_prompt=self._system_prompt(),
                        user_content=attempt_prompt,
                    ),
                    temperature=(0.35 if repair_errors else 0.45)
                    if is_table_discussion
                    else (0.55 if repair_errors else 0.85),
                )
            except Exception as exc:
                if not self.allow_fallback and not self.continue_on_invalid:
                    raise RuntimeError("FU-PL semantic generation failed and fallback is disabled.") from exc
                fallback_validation_errors = self._review_candidate_semantics(
                    fallback,
                    fallback_validation_errors,
                    step=step,
                    legal_context=legal_context,
                    recent_public_context=recent_public_context,
                )
                if fallback_validation_errors:
                    if self.continue_on_invalid:
                        return self._exhaustion_safe_pass(
                            step,
                            diagnostics=[
                                f"llm_player_error:{type(exc).__name__}",
                                *fallback_validation_errors,
                            ],
                            legal_context=legal_context,
                            recent_public_context=recent_public_context,
                            last_gm_reply=last_gm_reply,
                            prompt_preview=prompt[:1200],
                            model_attempts=model_attempts,
                        )
                    raise ValueError(
                        "FU-PL semantic generation failed and its fallback remained invalid after semantic review: "
                        f"{fallback_validation_errors}"
                    ) from exc
                return SimulatedUtterance(
                    text=fallback,
                    used_fallback=True,
                    validation_errors=[
                        f"llm_player_error:{type(exc).__name__}",
                        *fallback_validation_errors,
                    ],
                    prompt_preview=prompt[:1200],
                    model_attempts=model_attempts,
                )
            text = self._clean_llm_text(raw)
            repair_errors = self.validate(
                text,
                step=step,
                legal_context=legal_context,
                recent_public_context=recent_public_context,
            )
            repair_errors = self._review_candidate_semantics(
                text,
                repair_errors,
                step=step,
                legal_context=legal_context,
                recent_public_context=recent_public_context,
            )
            model_attempts.append(
                {
                    "attempt": attempt + 1,
                    "text": text,
                    "validation_errors": list(repair_errors),
                    "action_progress_review": dict(self.last_action_progress_review),
                    "table_discussion_review": dict(
                        self.last_table_discussion_review
                    ),
                }
            )
            if not repair_errors:
                return SimulatedUtterance(
                    text=text,
                    used_fallback=False,
                    validation_errors=[],
                    prompt_preview=prompt[:1200],
                    model_attempts=model_attempts,
                )
        if not self.allow_fallback and not self.continue_on_invalid:
            raise ValueError(f"FU-PL output remained invalid after repair: {repair_errors}")
        if is_table_discussion:
            fallback = self._table_discussion_fallback(
                step.speaker or "玩家",
                str(recent_public_context or last_gm_reply or ""),
                open_conditions=legal_context.open_npc_conditions,
            )
            fallback, fallback_validation_errors = self._validated_fallback_utterance(
                fallback,
                step=step,
                legal_context=legal_context,
                recent_public_context=recent_public_context,
                last_gm_reply=last_gm_reply,
            )
        if step.payload.get("npc_response_contract"):
            fallback = self._pending_npc_response_fallback(step)
            fallback_validation_errors = self.validate(
                fallback,
                step=step,
                legal_context=legal_context,
                recent_public_context=recent_public_context,
            )
        fallback_validation_errors = self._review_candidate_semantics(
            fallback,
            fallback_validation_errors,
            step=step,
            legal_context=legal_context,
            recent_public_context=recent_public_context,
        )
        if fallback_validation_errors:
            model_texts = [
                str(item.get("text") or "")[:500]
                for item in model_attempts[-3:]
            ]
            if self.continue_on_invalid:
                return self._exhaustion_safe_pass(
                    step,
                    diagnostics=[
                        *repair_errors,
                        *fallback_validation_errors,
                        f"model_texts:{model_texts!r}",
                        f"fallback_text:{fallback!r}",
                    ],
                    legal_context=legal_context,
                    recent_public_context=recent_public_context,
                    last_gm_reply=last_gm_reply,
                    prompt_preview=prompt[:1200],
                    model_attempts=model_attempts,
                )
            raise ValueError(
                "FU-PL model repair and validator-approved fallback both failed: "
                f"model={repair_errors}; fallback={fallback_validation_errors}; "
                f"model_texts={model_texts!r}; fallback_text={fallback!r}"
            )
        return SimulatedUtterance(
            text=fallback,
            used_fallback=True,
            # The selected fallback has passed the same validator as model
            # output. Failed model candidates remain in model_attempts and do
            # not make the utterance actually sent look unsafe in the report.
            validation_errors=[],
            prompt_preview=prompt[:1200],
            model_attempts=model_attempts,
        )

    @classmethod
    def _with_latest_gm_reply(
        cls,
        recent_public_context: str,
        last_gm_reply: str,
    ) -> str:
        """Ensure the newest table-facing GM statement cannot be omitted."""

        context = str(recent_public_context or "").strip()
        latest = " ".join(str(last_gm_reply or "").split()).strip()
        if not latest:
            return context
        recorded_latest = " ".join(cls._latest_gm_reply(context).split()).strip()
        if recorded_latest == latest:
            return context
        return f"{context}\n时悠：{latest}".strip()

    @classmethod
    def _current_gm_beat(
        cls,
        recent_public_context: str,
        last_gm_reply: str,
    ) -> str:
        """Choose the richest copy when both inputs contain the same GM beat."""

        provided = cls._latest_gm_reply(last_gm_reply) or str(last_gm_reply or "")
        recorded = cls._latest_gm_reply(recent_public_context)
        provided = " ".join(provided.split()).strip()
        recorded = " ".join(recorded.split()).strip()
        if not provided:
            return recorded
        if not recorded:
            return provided
        if provided in recorded:
            return recorded
        if recorded in provided:
            return provided
        return provided

    @classmethod
    def _current_public_exchange(
        cls,
        recent_public_context: str,
        last_gm_reply: str,
    ) -> str:
        """Keep the newest GM beat and, when aligned, its prompting player line."""

        beat = cls._current_gm_beat(recent_public_context, last_gm_reply)
        blocks = cls._dialogue_blocks(recent_public_context)
        latest_gm_index = next(
            (
                index
                for index in range(len(blocks) - 1, -1, -1)
                if blocks[index][0] in cls._GM_SPEAKERS
            ),
            -1,
        )
        if latest_gm_index < 0:
            return f"时悠：{beat}" if beat else ""
        recorded = " ".join(str(blocks[latest_gm_index][1] or "").split()).strip()
        aligned = bool(beat and recorded and (beat in recorded or recorded in beat))
        lines: list[str] = []
        if aligned and latest_gm_index > 0:
            prior_speaker, prior_text = blocks[latest_gm_index - 1]
            if prior_speaker not in cls._GM_SPEAKERS and str(prior_text or "").strip():
                lines.append(f"{prior_speaker}：{prior_text}")
        if beat:
            lines.append(f"时悠：{beat}")
        return "\n".join(lines)

    def _exhaustion_safe_pass(
        self,
        step: ReplayStep,
        *,
        diagnostics: list[str],
        legal_context: LegalActionContext | None = None,
        recent_public_context: str = "",
        last_gm_reply: str = "",
        prompt_preview: str = "",
        model_attempts: list[dict[str, object]] | None = None,
    ) -> SimulatedUtterance:
        """Try one current-scene interaction before conceding an empty turn."""

        if legal_context is not None and "这是行动槽" in str(step.stage_goal or ""):
            rescue = self._scene_interaction_rescue(
                step,
                legal_context,
                recent_public_context=recent_public_context,
                last_gm_reply=last_gm_reply,
            )
            if rescue:
                rescue_errors = self.validate(
                    rescue,
                    step=step,
                    legal_context=legal_context,
                    recent_public_context=recent_public_context,
                )
                rescue_errors = self._review_candidate_semantics(
                    rescue,
                    rescue_errors,
                    step=step,
                    legal_context=legal_context,
                    recent_public_context=recent_public_context,
                )
                if not rescue_errors:
                    return SimulatedUtterance(
                        text=rescue,
                        used_fallback=True,
                        validation_errors=[],
                        prompt_preview=prompt_preview,
                        model_attempts=list(model_attempts or []),
                        fallback_kind="scene_interaction_rescue",
                        fallback_diagnostics=list(
                            dict.fromkeys(str(item) for item in diagnostics if str(item))
                        ),
                    )
                diagnostics = [*diagnostics, *rescue_errors, f"rescue_text:{rescue!r}"]

        speaker = re.sub(
            r"[^0-9A-Za-z_\-\u4e00-\u9fff·・ ]",
            "",
            str(step.speaker or "玩家"),
        ).strip() or "玩家"
        if "正在和其他玩家短暂商量" in str(step.stage_goal or ""):
            return SimulatedUtterance(
                text=f"{speaker}: 我还没想好，先听你们的。",
                used_fallback=True,
                validation_errors=[],
                prompt_preview=prompt_preview,
                model_attempts=list(model_attempts or []),
                fallback_kind="table_discussion_safe_silence",
                fallback_diagnostics=list(
                    dict.fromkeys(str(item) for item in diagnostics if str(item))
                ),
            )
        actor = re.sub(
            r"[^0-9A-Za-z_\-\u4e00-\u9fff·・ ]",
            "",
            str(step.actor or speaker),
        ).strip() or speaker
        return SimulatedUtterance(
            text=f"{speaker}: {actor}暂时不采取行动。",
            used_fallback=True,
            validation_errors=[],
            prompt_preview=prompt_preview,
            model_attempts=list(model_attempts or []),
            fallback_kind="exhaustion_safe_pass",
            fallback_diagnostics=list(
                dict.fromkeys(str(item) for item in diagnostics if str(item))
            ),
        )

    def _scene_interaction_rescue(
        self,
        step: ReplayStep,
        legal_context: LegalActionContext,
        *,
        recent_public_context: str,
        last_gm_reply: str,
    ) -> str:
        """Build a bounded action from only the current scene's affordances."""

        latest = self._current_gm_beat(recent_public_context, last_gm_reply)
        if not latest or re.search(r"暂时没有新的变化|眼下没有新的变化|没有可见的新变化", latest):
            return ""
        speaker = re.sub(
            r"[^0-9A-Za-z_\-\u4e00-\u9fff·・ ]",
            "",
            str(step.speaker or "玩家"),
        ).strip() or "玩家"
        actor = re.sub(
            r"[^0-9A-Za-z_\-\u4e00-\u9fff·・ ]",
            "",
            str(step.actor or speaker),
        ).strip() or speaker
        current_sources = [
            latest,
            str(legal_context.scene_location or "").strip(),
            *(
                str(item or "").strip()
                for item in legal_context.visible_scene_elements
                if str(item or "").strip()
            ),
            *(
                str(item or "").strip()
                for item in legal_context.blocked_routes
                if str(item or "").strip()
            ),
        ]
        barrier = self._current_blocking_affordance(current_sources)
        if barrier:
            action = f"{actor}走到{barrier}前，用指节敲了三下，朝里面报上来意。"
            return f"{speaker}: {action}"

        latest_npc = next(
            (
                name
                for name in reversed(legal_context.known_npcs)
                if str(name or "").strip() and str(name) in latest
            ),
            "",
        )
        if latest_npc and re.search(r"(?:看向|望向|等(?:着|待)|没有回答|沉默|迟疑|犹豫)", latest):
            return f"{speaker}: {actor}转向{latest_npc}，请对方把刚才没有说完的话说清楚。"

        return ""

    @staticmethod
    def _current_blocking_affordance(sources: list[str]) -> str:
        """Return a current, publicly visible closed entrance if one exists."""

        candidates: list[str] = []
        for source in sources:
            clean = " ".join(str(source or "").split()).strip(" ，,。；;：:")
            if not clean:
                continue
            blocked = bool(
                re.search(
                    r"尚未(?:开启|打开)|没有(?:开启|打开)|仍(?:然)?(?:关着|关闭|封闭)|"
                    r"紧闭|锁着|上锁|封住|堵住|无人回应|没有回应|叫不开|进不去|不可通行|"
                    r"blocked|封闭路线|受阻路线",
                    clean,
                    re.I,
                )
            )
            if not blocked:
                continue
            for match in re.finditer(
                r"(?:尚未开启的|尚未打开的|仍然关闭的|仍关闭的|紧闭的|锁着的|上锁的|封住的)?"
                r"(?:[\u4e00-\u9fffA-Za-z0-9·]{0,12})?"
                r"(?:登记门|闸门|舱门|铁门|木门|门扇|门板|门口|舱口|入口)",
                clean,
            ):
                value = match.group(0).strip(" ，,。；;：:")
                value = re.sub(r"^(?:当前确实可见|当前可见|受阻路线|封闭路线)[：:]?", "", value)
                if value:
                    candidates.append(value)
        return candidates[-1] if candidates else ""

    def _review_candidate_semantics(
        self,
        candidate: str,
        errors: list[str],
        *,
        step: ReplayStep,
        legal_context: LegalActionContext,
        recent_public_context: str,
    ) -> list[str]:
        """Apply semantic contracts after deterministic authority checks."""

        current = list(dict.fromkeys(errors))
        current = self._review_table_discussion_contract(
            candidate,
            current,
            step=step,
            recent_public_context=recent_public_context,
        )
        current = self._review_npc_response_contract(candidate, current, step=step)
        return self._review_action_progress_contract(
            candidate,
            current,
            step=step,
            legal_context=legal_context,
            recent_public_context=recent_public_context,
        )

    def _review_table_discussion_contract(
        self,
        candidate: str,
        errors: list[str],
        *,
        step: ReplayStep,
        recent_public_context: str,
    ) -> list[str]:
        """Keep silence probes as actual player-to-player discussion.

        A semantic player model can express a committed action without using
        one of the validator's known verbs.  The runner must repair that FU-PL
        utterance rather than demand that FU-GM ignore a real in-fiction action.
        """

        current = list(dict.fromkeys(errors))
        self.last_table_discussion_review = {}
        if (
            "正在和其他玩家短暂商量" not in str(step.stage_goal or "")
            or not self.semantic_validation_review
            or self.client is None
        ):
            return current
        request = {
            "speaker": str(step.speaker or "").strip(),
            "actor": str(step.actor or "").strip(),
            "candidate": str(candidate or "").strip(),
            "recent_public_context": str(recent_public_context or "")[-3000:],
        }
        try:
            raw = self.client.create_chat_completion(
                model=self.model,
                messages=build_cache_friendly_messages(
                    static_system_prompt=PLAYER_TABLE_DISCUSSION_REVIEW_PROMPT,
                    user_content=json.dumps(request, ensure_ascii=False),
                ),
                temperature=0,
                response_format={"type": "json_object"},
                operation="fu_pl.table_discussion_contract",
            )
            payload = extract_json_object(raw)
        except Exception as exc:
            self.last_table_discussion_review = {"error": type(exc).__name__}
            # The deterministic validator remains the safe fallback when the
            # independent semantic review is temporarily unavailable.
            return current

        evidence = str(payload.get("evidence") or "").strip()
        evidence_is_verbatim = bool(evidence and evidence in str(candidate or ""))
        violation = bool(
            not payload.get("pure_table_discussion", False)
            or payload.get("commits_character_action", False)
            or payload.get("commits_party_action", False)
            or payload.get("directed_at_gm_or_npc", False)
        )
        self.last_table_discussion_review = {
            **payload,
            "evidence_is_verbatim": evidence_is_verbatim,
        }
        if not violation:
            # When the semantic reviewer has seen the full candidate and
            # confirms this is table talk, lexical verb overlap is not a
            # second authority. Keep unrelated mechanical errors, but discard
            # the local action-language hint that the semantic result
            # explicitly resolved.
            return [
                item
                for item in current
                if not item.startswith("table_discussion_declares_character_action")
            ]
        reason = " ".join(str(payload.get("reason") or "").split()).strip()
        return [
            *current,
            "table_discussion_declares_character_action:"
            + (reason or "这句话已经执行了角色或队伍行动，应改写为尚未落实的桌边讨论"),
        ]

    def _review_action_progress_contract(
        self,
        candidate: str,
        errors: list[str],
        *,
        step: ReplayStep,
        legal_context: LegalActionContext,
        recent_public_context: str,
    ) -> list[str]:
        """Reject semantic action stagnation missed by lexical heuristics."""

        current = list(dict.fromkeys(errors))
        self.last_action_progress_review = {}
        if (
            "这是行动槽" not in str(step.stage_goal or "")
            or step.payload.get("npc_response_contract")
            or not self.semantic_validation_review
            or self.client is None
        ):
            return current
        request = {
            "speaker": str(step.speaker or "").strip(),
            "actor": str(step.actor or "").strip(),
            "stage_goal": str(step.stage_goal or "").strip(),
            "candidate": str(candidate or "").strip(),
            "recent_public_context": str(recent_public_context or "")[-5000:],
            "current_scene_name": str(legal_context.scene_name or "").strip(),
            "current_scene_location": str(legal_context.scene_location or "").strip(),
            "prior_rejected_action_attempts": list(
                self._prior_rejected_action_attempts[-3:]
            ),
            "known_player_characters": list(legal_context.known_pcs or []),
            "known_npcs": list(legal_context.known_npcs or []),
            "present_npcs": list(legal_context.present_npcs or []),
            "present_pcs": list(legal_context.present_pcs or []),
            "presence_authoritative": bool(legal_context.presence_authoritative),
            "actor_locations": dict(legal_context.actor_locations or {}),
            "authoritative_story_items": list(legal_context.story_items or []),
            "known_enemies": list(legal_context.known_enemies or []),
            "visible_scene_elements": list(legal_context.visible_scene_elements or []),
            "established_scene_facts": list(
                legal_context.established_scene_facts or []
            ),
            "immediate_scene_consequence": str(
                legal_context.immediate_scene_consequence or ""
            ),
            "active_clocks": list(legal_context.active_clocks or []),
            "legal_actions": list(legal_context.legal_actions or []),
            "legal_spells": list(legal_context.legal_spells or []),
            "legal_skills": list(legal_context.legal_skills or []),
            "dramatic_progress_context": dict(
                step.payload.get("dramatic_progress_context") or {}
            ),
        }
        try:
            raw = self.client.create_chat_completion(
                model=self.model,
                messages=build_cache_friendly_messages(
                    static_system_prompt=PLAYER_ACTION_PROGRESS_REVIEW_PROMPT,
                    user_content=json.dumps(request, ensure_ascii=False),
                ),
                temperature=0,
                response_format={"type": "json_object"},
                operation="fu_pl.action_progress_contract",
            )
            payload = extract_json_object(raw)
        except Exception as exc:
            self.last_action_progress_review = {
                "error": type(exc).__name__,
            }
            return [
                *current,
                f"action_progress_review_unavailable:{type(exc).__name__}",
            ]

        evidence = str(payload.get("evidence") or "").strip()
        evidence_is_grounded = bool(
            evidence
            and (
                evidence in str(candidate or "")
                or evidence in str(recent_public_context or "")
            )
        )
        self.last_action_progress_review = {
            **payload,
            "evidence_is_verbatim": evidence_is_grounded,
        }
        materially_advances = bool(
            payload.get(
                "materially_advances_current_situation",
                payload.get("valid_action_progress"),
            )
        )
        repeats_micro_lane = bool(
            payload.get("repeats_micro_investigation_lane", False)
        )
        stalls_after_actionable_result = bool(
            payload.get(
                "actionable_result_or_explicit_choice_is_already_public", False
            )
            and payload.get("opens_another_detail_layer", False)
            and not payload.get("uses_public_result_or_answers_choice", False)
        )
        procedural_micro_clarification = bool(
            payload.get(
                "procedural_micro_clarification_after_sufficient_plan", False
            )
        )
        unsupported_resource_spend = bool(
            payload.get(
                "spends_limited_resource_without_public_tactical_basis", False
            )
        )
        matches_prior_rejected_lane = bool(
            payload.get("matches_prior_rejected_lane", False)
        )
        reopens_exhausted_npc_knowledge = bool(
            payload.get("reopens_exhausted_npc_knowledge_lane", False)
        )
        knowledge_lane_reopened_by_evidence = bool(
            payload.get("new_public_evidence_reopens_npc_knowledge_lane", False)
        )
        exhausts_npc_knowledge_lane = bool(
            reopens_exhausted_npc_knowledge
            and not knowledge_lane_reopened_by_evidence
        )
        controls_other_players = bool(
            payload.get("controls_other_player_characters", False)
        )
        party_action_authorized = bool(
            payload.get("party_action_authorized_by_public_consensus", False)
        )
        preempts_npc_decision = bool(
            payload.get("controls_npc_outcome_without_public_answer", False)
        )
        npc_outcome_already_public = bool(
            payload.get("npc_outcome_already_public", False)
        )
        player_agency_violation = bool(
            controls_other_players and not party_action_authorized
        )
        npc_agency_violation = bool(
            preempts_npc_decision and not npc_outcome_already_public
        )
        story_item_custody_violation = bool(
            payload.get("violates_story_item_custody", False)
        )
        actor_location_violation = bool(
            payload.get("acts_outside_authoritative_actor_location", False)
        )
        valid = bool(
            payload.get("valid_action_progress")
            and payload.get("concrete_new_action")
            and payload.get("grounded_in_public_context")
            and materially_advances
            and not repeats_micro_lane
            and not stalls_after_actionable_result
            and not procedural_micro_clarification
            and not unsupported_resource_spend
            and not matches_prior_rejected_lane
            and not exhausts_npc_knowledge_lane
            and not player_agency_violation
            and not npc_agency_violation
            and not story_item_custody_violation
            and not actor_location_violation
            and not payload.get("mostly_restates_known_information")
            and not payload.get("repeats_completed_action")
        )
        if valid:
            # These two lexical checks intentionally fail closed in offline
            # tests, but natural Chinese routinely defeats their word lists:
            # “侧身贴进阴影” is still an action, while “我不能确认是A还是B”
            # is an NPC admitting uncertainty rather than issuing a choice.
            # The semantic contract has the full public transcript and can
            # safely overturn only these ambiguous hints after confirming a
            # grounded, concrete and materially progressive action.
            removable = {"action_slot_contains_only_table_discussion"}
            if not bool(payload.get("repeats_completed_action", False)):
                removable.add("repeats_recent_action_lane")
            if bool(payload.get("responds_to_current_pressure_or_choice")):
                removable.add("ignores_explicit_gm_affordance")
            if (
                "repeats_resolved_information_delivery" in payload
                and not bool(payload.get("repeats_resolved_information_delivery"))
            ):
                removable.add("repeats_resolved_information_delivery")
            movement_evidence = str(
                payload.get("movement_authorization_evidence") or ""
            ).strip()
            if (
                bool(payload.get("movement_claimed"))
                and bool(payload.get("movement_is_authorized_by_public_context"))
                and movement_evidence
                and movement_evidence in str(recent_public_context or "")
            ):
                removable.add("leaves_current_scene_without_transition")
            return [item for item in current if item not in removable]
        reason = " ".join(str(payload.get("reason") or "").split()).strip()
        self._prior_rejected_action_attempts.append(
            {
                "candidate": str(candidate or "").strip(),
                "reason": reason or "候选没有形成基于当前公开局面的新行动",
                "repeats_completed_action": bool(
                    payload.get("repeats_completed_action")
                ),
                "repeats_micro_investigation_lane": repeats_micro_lane,
                "matches_prior_rejected_lane": matches_prior_rejected_lane,
                "procedural_micro_clarification_after_sufficient_plan": procedural_micro_clarification,
                "spends_limited_resource_without_public_tactical_basis": unsupported_resource_spend,
                "reopens_exhausted_npc_knowledge_lane": reopens_exhausted_npc_knowledge,
                "new_public_evidence_reopens_npc_knowledge_lane": knowledge_lane_reopened_by_evidence,
                "controls_other_player_characters": controls_other_players,
                "party_action_authorized_by_public_consensus": party_action_authorized,
                "controls_npc_outcome_without_public_answer": preempts_npc_decision,
                "npc_outcome_already_public": npc_outcome_already_public,
                "violates_story_item_custody": story_item_custody_violation,
                "acts_outside_authoritative_actor_location": actor_location_violation,
            }
        )
        flags = (
            f"valid={bool(payload.get('valid_action_progress'))},"
            f"concrete={bool(payload.get('concrete_new_action'))},"
            f"grounded={bool(payload.get('grounded_in_public_context'))},"
            f"material={materially_advances},"
            f"micro_lane={repeats_micro_lane},"
            f"stalls_after_actionable={stalls_after_actionable_result},"
            f"procedural_micro_clarification={procedural_micro_clarification},"
            f"unsupported_resource_spend={unsupported_resource_spend},"
            f"prior_lane={matches_prior_rejected_lane},"
            f"exhausted_npc_knowledge={exhausts_npc_knowledge_lane},"
            f"controls_other_players={player_agency_violation},"
            f"preempts_npc={npc_agency_violation},"
            f"story_item_custody={story_item_custody_violation},"
            f"actor_location={actor_location_violation},"
            f"restates={bool(payload.get('mostly_restates_known_information'))},"
            f"repeats={bool(payload.get('repeats_completed_action'))}"
        )
        semantic_errors = list(current)
        if procedural_micro_clarification:
            semantic_errors.append(
                "semantic_action_stalls_on_procedural_detail:"
                + (reason or "公开路线、职责与触发已经足够执行，候选仍在追问不会改变行动的程序细节")
            )
        if unsupported_resource_spend:
            basis_evidence = " ".join(
                str(payload.get("resource_tactical_basis_evidence") or "").split()
            ).strip()
            semantic_errors.append(
                "semantic_action_spends_resource_without_public_basis:"
                + (
                    reason
                    or basis_evidence
                    or "候选消耗有限资源，但公开局面没有与该效果相关的威胁、目标或战术计划"
                )
            )
        if exhausts_npc_knowledge_lane:
            semantic_errors.append(
                "semantic_action_reopens_exhausted_npc_knowledge:"
                + (reason or "NPC已经公开其当前知识边界，且没有新证据重新开放这条问答路线")
            )
        if player_agency_violation:
            semantic_errors.append(
                "semantic_action_controls_other_players:"
                + (reason or "候选替尚未公开同意的其他玩家角色执行了行动")
            )
        if npc_agency_violation:
            semantic_errors.append(
                "semantic_action_preempts_npc_decision:"
                + (reason or "候选把NPC尚未作出的决定写成了已经发生")
            )
        if story_item_custody_violation:
            custody_evidence = " ".join(
                str(payload.get("story_item_custody_evidence") or "").split()
            ).strip()
            semantic_errors.append(
                "semantic_action_violates_story_item_custody:"
                + (
                    custody_evidence
                    or reason
                    or "候选角色试图使用当前由另一角色持有、或位于别处的剧情物件"
                )
            )
        if actor_location_violation:
            location_evidence = " ".join(
                str(payload.get("actor_location_evidence") or "").split()
            ).strip()
            semantic_errors.append(
                "semantic_action_acts_outside_actor_location:"
                + (
                    location_evidence
                    or reason
                    or "候选角色尚未抵达目标地点，却已经操作了该处的环境或物件"
                )
            )
        semantic_errors.append(
            "semantic_action_without_progress:"
            + (reason or "候选没有形成基于当前公开局面的新行动")
            + f"（{flags}）"
        )
        return list(dict.fromkeys(semantic_errors))

    def _review_npc_response_contract(
        self,
        candidate: str,
        errors: list[str],
        *,
        step: ReplayStep,
    ) -> list[str]:
        contract = step.payload.get("npc_response_contract")
        if not isinstance(contract, dict) or not contract:
            return list(dict.fromkeys(errors))
        current = list(dict.fromkeys(errors))
        remaining_items = [
            {
                "item_id": str(item.get("item_id") or "").strip(),
                "prompt": str(item.get("prompt") or "").strip(),
            }
            for item in (contract.get("remaining_items") or [])
            if isinstance(item, dict)
            and str(item.get("item_id") or "").strip()
            and str(item.get("prompt") or "").strip()
        ]
        if not remaining_items:
            return current
        remaining_ids = {item["item_id"] for item in remaining_items}
        if not self.semantic_validation_review or self.client is None:
            return [*current, "npc_response_contract_requires_semantic_review"]
        request = {
            "speaker": str(step.speaker or "").strip(),
            "actor": str(step.actor or "").strip(),
            "candidate": str(candidate or "").strip(),
            "requester": str(contract.get("npc") or "未具名发问者").strip(),
            "request_summary": str(contract.get("summary") or "").strip(),
            "remaining_items": remaining_items,
            "public_request_evidence": str(contract.get("speaker_evidence") or "").strip(),
        }
        try:
            raw = self.client.create_chat_completion(
                model=self.model,
                messages=build_cache_friendly_messages(
                    static_system_prompt=PLAYER_NPC_RESPONSE_CONTRACT_PROMPT,
                    user_content=json.dumps(request, ensure_ascii=False),
                ),
                temperature=0,
                response_format={"type": "json_object"},
                operation="fu_pl.npc_response_contract",
            )
            payload = extract_json_object(raw)
        except Exception as exc:
            return [
                *current,
                f"npc_response_contract_review_unavailable:{type(exc).__name__}",
            ]
        answered_ids = {
            str(item).strip()
            for item in (payload.get("answered_item_ids") or [])
            if str(item).strip() in remaining_ids
        }
        evidence = str(payload.get("evidence") or "").strip()
        complete = bool(
            payload.get("directed_to_requester")
            and payload.get("complete")
            and all(item_id in answered_ids for item_id in remaining_ids)
            and evidence
            and evidence in str(candidate or "")
        )
        if complete:
            return current
        reason = " ".join(str(payload.get("reason") or "").split()).strip()
        return [
            *current,
            "does_not_answer_pending_npc_request:"
            + (reason or "必须直接回应发问者，并逐项回答、拒答或承认不知道"),
        ]

    @staticmethod
    def _pending_npc_response_fallback(step: ReplayStep) -> str:
        """Fail closed without inventing facts when FU-PL cannot follow the prompt."""

        contract = dict(step.payload.get("npc_response_contract") or {})
        actor = str(step.actor or step.speaker or "这位英雄").strip()
        npc = str(contract.get("npc") or "未具名发问者").strip()
        addressee = "门外发问的人" if npc == "未具名发问者" else npc
        return f"{step.speaker}: {actor}对{addressee}说：‘你刚才问的这些事，我现在拒绝回答。’"

    @classmethod
    def _table_discussion_fallback(
        cls,
        speaker: str,
        public_context: str,
        *,
        open_conditions: list[dict[str, str]] | None = None,
    ) -> str:
        """Provide safe player-to-player talk without paying out an NPC bargain.

        A conditional NPC answer is a public fact, but its promised result is
        not.  Put this check before interpreting words such as ``门开了`` in
        the transcript, because an earlier scene can otherwise make a failed
        model repair invent that the newest condition has been fulfilled.
        """

        latest = cls._latest_gm_reply(public_context)
        pending = next(
            (
                item
                for item in (open_conditions or [])
                if str(item.get("promised_result") or "").strip()
            ),
            None,
        )
        if pending:
            npc = str(pending.get("npc") or "对方").strip() or "对方"
            condition = str(pending.get("condition") or "刚才的条件").strip() or "刚才的条件"
            return f"{speaker}: {npc}还没兑现答复。我们要不要先决定怎么处理“{condition}”？"
        if re.search(r"跟我来|随我来|可以进去|门(?:已经|现在)?(?:开了|打开)", latest):
            return f"{speaker}: 入口已经开了。我们要不要现在走，谁愿意留在最后照应？"
        if re.search(r"逼近|追兵|巡逻|脚步|包围|警报|火光", latest):
            return f"{speaker}: 门外那阵动静不能再放着不管。谁守入口，谁先带同行的人撤？"
        if re.search(r"条件|只要|必须|才会|才肯|答应", latest):
            return f"{speaker}: 对方的条件已经摆出来了。我们接不接，先把这件事定下来？"
        target = cls._context_target(latest or public_context)
        if target and target not in {"现场", "当前目标"}:
            return f"{speaker}: 我还拿不准怎么处理{target}，先听听你们更担心哪一边。"
        return f"{speaker}: 我还没想好，先听你们的。"

    def _validated_fallback_utterance(
        self,
        initial: str,
        *,
        step: ReplayStep,
        legal_context: LegalActionContext,
        recent_public_context: str,
        last_gm_reply: str,
    ) -> tuple[str, list[str]]:
        """Never bypass the player-boundary validator when model repair fails."""

        candidates = [str(initial or "").strip()]
        stage_goal = str(step.stage_goal or "")
        if "这是行动槽" in stage_goal:
            speaker = step.speaker or "玩家"
            subject = step.actor or speaker
            latest = self._current_gm_beat(recent_public_context, last_gm_reply)
            current_exchange = self._current_public_exchange(
                recent_public_context,
                last_gm_reply,
            )
            visible = "；".join(
                str(item or "").strip()
                for item in legal_context.visible_scene_elements
                if str(item or "").strip()
            )
            current_bits = [
                f"当前地点：{legal_context.scene_location}" if legal_context.scene_location else "",
                current_exchange or (f"时悠：{latest}" if latest else ""),
                f"当前确实可见：{visible}" if visible else "",
                (
                    f"当前直接后果：{legal_context.immediate_scene_consequence}"
                    if legal_context.immediate_scene_consequence
                    else ""
                ),
                (
                    "当前受阻路线：" + "；".join(legal_context.blocked_routes)
                    if legal_context.blocked_routes
                    else ""
                ),
                (
                    "当前仍需遵守的约定："
                    + "；".join(
                        str(item.get("settled_terms") or "").strip()
                        for item in legal_context.settled_npc_exchanges
                        if str(item.get("settled_terms") or "").strip()
                    )
                    if legal_context.settled_npc_exchanges
                    else ""
                ),
            ]
            context = "\n".join(item for item in current_bits if item).strip()
            condition_action = self._open_condition_action_fallback(
                subject,
                legal_context.open_npc_conditions,
                public_context=context,
                known_pcs=legal_context.known_pcs,
            )
            if condition_action:
                candidates.insert(0, f"{speaker}: {condition_action}")
            focus_action = self._immediate_focus_action_fallback(
                subject,
                public_context=context,
            )
            if focus_action:
                # A fresh, explicit GM invitation is stronger grounding than
                # an object merely mentioned somewhere earlier in the log.
                # Keeping it first prevents retries from circling an already
                # exhausted guard or investigation lane.
                candidates.insert(0, f"{speaker}: {focus_action}")
            reveal_action = self._newly_revealed_detail_action_fallback(
                subject,
                public_context=context,
            )
            if reveal_action:
                # A material change in the newest GM beat is a fresh action
                # hook, even when a previous player already handled the broad
                # object.  For example, uncovering a road sign exposes the
                # nail holes and pressure marks beneath a banner; inspecting
                # those is not another attempt to read the same sign.
                candidates.insert(0, f"{speaker}: {reveal_action}")
            object_action = self._object_disposition_fallback(
                subject,
                legal_context,
                public_context=context,
            )
            if object_action:
                candidates.insert(0, f"{speaker}: {object_action}")
            clock_name = self._first_clock_name(legal_context)
            if clock_name:
                clock_method = self._clock_method(clock_name, context)
                if clock_method:
                    candidates.append(f"{speaker}: {subject}{clock_method}。")
            candidates.extend(
                f"{speaker}: {utterance}"
                for utterance in self._safe_spell_action_candidates(subject, legal_context, context)
            )
            candidates.extend(
                f"{speaker}: {utterance}"
                for _, utterance in self._contextual_action_candidates(subject, context, step)
            )
            affordance = self._affordance_response_fallback(
                subject,
                context,
                known_npcs=legal_context.known_npcs,
            )
            if affordance:
                candidates.insert(0, f"{speaker}: {affordance}")

        best = candidates[0]
        best_errors = self.validate(
            best,
            step=step,
            legal_context=legal_context,
            recent_public_context=recent_public_context,
        )
        for candidate in dict.fromkeys(item for item in candidates if item):
            errors = self.validate(
                candidate,
                step=step,
                legal_context=legal_context,
                recent_public_context=recent_public_context,
            )
            if not errors:
                return candidate, []
            if len(errors) < len(best_errors):
                best, best_errors = candidate, errors
        return best, best_errors

    @staticmethod
    def _repair_instructions(
        errors: list[str],
        legal_context: LegalActionContext,
    ) -> list[str]:
        """Turn validator codes into constraints a roleplaying model can follow."""

        instructions: list[str] = []
        for error in errors:
            if error == "repeats_saturated_npc_question_lane":
                instruction = (
                    "这一条绝对不要向任何NPC提问，也不要出现问号、询问、追问、请对方回答；"
                    "改为角色现在亲手执行的一项具体现场行动。"
                )
                if legal_context.open_npc_conditions:
                    condition = legal_context.open_npc_conditions[-1]
                    instruction += (
                        "当前NPC条件已经说清，不要再确认条件；请直接选择并完成其中一个现在可执行的分支："
                        f"【{condition.get('condition') or ''}】。"
                    )
                instructions.append(instruction)
            elif error == "repeats_settled_npc_exchange":
                instructions.append(
                    "这项NPC交涉已经结清，不要再次确认条件、重复交付或把同一个问题换个说法。"
                    "让角色依据已谈妥的结果采取一项新行动、面对新的现场压力，或提出实质不同的新议题。"
                )
            elif error == "near_duplicate_recent_player_utterance":
                instructions.append(
                    "刚才已有玩家做过近似事情；必须换一个对象、手段和目的，不能只替换角色名。"
                )
            elif error == "does_not_answer_pending_decision":
                pending = legal_context.pending_decisions[0] if legal_context.pending_decisions else {}
                options = [
                    str(item.get("effect") or item.get("label") or item.get("trait") or item.get("target") or "")
                    for item in pending.get("options", [])
                    if isinstance(item, dict)
                ]
                instructions.append(
                    "不要进行新行动，只回答GM刚交给你的待决选择"
                    + (f"；可用选项是：{'、'.join(item for item in options if item)}" if options else "")
                    + "。"
                )
            elif error.startswith("does_not_answer_pending_npc_request:"):
                instructions.append(
                    "不要对队友讨论稍后怎么答；现在直接对刚才发问的NPC或门外发问者说话，"
                    "逐项给出答案、明确拒答，或明确承认不知道。"
                )
            elif error.startswith("npc_response_contract_review_unavailable:"):
                instructions.append(
                    "模拟玩家质量检查暂时不可用；仍须直接回应刚才的发问者，并逐项回答、拒答或承认不知道。"
                )
            elif error.startswith("semantic_action_without_progress:"):
                instructions.append(
                    "不要复述、总结、记下或转告已经公开的整组线索，也不要重复已完成的调查、警戒、交付或询问。"
                    "不要沿着同一件物品刚揭示的细小部件继续套娃式调查，除非它会直接改变眼前决定。"
                    "新出现的纸角、划痕、粉末、锁槽或脚印不自动构成新路线；若已有可行动结论或NPC明确取舍，"
                    "必须利用结果或回应取舍，不能再拆一层细节。"
                    "只引用理解当前动作所必需的短语，然后让指定角色作出选择、履行或拒绝条件、回应NPC、"
                    "处理迫近危险、改变位置，或进行一项能影响下一步方案的调查。"
                )
            elif error.startswith("semantic_action_reopens_exhausted_npc_knowledge:"):
                instructions.append(
                    "这名NPC已经明确说完当前记得或知道的范围。没有新的外部证据、感官刺激或记忆变化，"
                    "不要再把来路、同行者、目的地、最后记忆等相邻问题拆开追问。改为利用已知信息行动、"
                    "处理眼前压力，或转向另一个尚未处理的人物或物件；若GM刚公开了新证据，只能询问它直接涉及的问题。"
                )
            elif error.startswith("semantic_action_controls_other_players:"):
                instructions.append(
                    "只控制本条指定角色。可以招呼或邀请队友跟上，但在其他玩家尚未逐一同意、GM也未宣布全队行动时，"
                    "不能写成队伍已经移动、同意或完成动作。把本句改为当前角色自己的行动，并把其他角色的选择留给他们。"
                )
            elif error.startswith("semantic_action_preempts_npc_decision:"):
                instructions.append(
                    "可以向NPC提出同行、交付或配合请求，但在GM尚未给出其明确答复前，不能写成NPC已经答应、跟随或抵达。"
                    "只写当前角色的提议与自己能完成的动作，把NPC结果留给GM。"
                )
            elif error.startswith("semantic_action_violates_story_item_custody:"):
                instructions.append(
                    "剧情物件的当前持有者与位置以合法行动上下文为准。不要让指定角色拿取、嵌入、交付、"
                    "使用或消耗由别人持有的物件；若需要该物件，先让角色请求持有者转交，不能在同一句里"
                    "把请求和转交结果写成已经完成。"
                )
            elif error.startswith("semantic_action_acts_outside_actor_location:"):
                instructions.append(
                    "指定角色只能操作其权威当前位置中的人物、门、机关和物件。若要去另一地点，本条只声明"
                    "公开允许的移动；等GM确认转场后，下一条才能操作目的地内容。"
                )
            elif error.startswith("action_progress_review_unavailable:"):
                instructions.append(
                    "模拟玩家行动检查暂时不可用；仍须提交一个基于最新公开变化、会要求GM给出新回应的具体行动，"
                    "不得整理旧线索充当行动。"
                )
            elif error == "table_discussion_declares_character_action":
                instructions.append("这只是玩家间商量，不得替角色声明行动；只说意见、担心或分工建议。")
            elif error == "action_slot_contains_only_table_discussion":
                instructions.append("这是角色行动槽；结尾必须明确角色此刻实际做什么，不能只问队友意见。")
            elif error == "action_slot_delegates_to_teammate":
                instructions.append(
                    "这是指定角色的行动槽；必须让该角色本人现在签、递、走、查、挡、交涉或执行其他具体行动。"
                    "不能只命令、请求或等待另一名玩家角色替他行动。"
                )
            elif error == "claims_another_players_commitment":
                instructions.append(
                    "不要把其他玩家刚做出的承诺说成自己的。可以明确引用队友已经作出的承诺，"
                    "或让当前角色现在另作一项属于自己的承诺。"
                )
            elif error == "claims_npc_controlled_outcome":
                instructions.append(
                    "只声明当前角色能控制的行动。不能替NPC、势力或机关承诺开路、放行、交钥匙、"
                    "提供许可或透露情报；若角色正在接受NPC的条件，只写角色现在如何履行，"
                    "NPC是否兑现承诺必须留给GM决定。"
                )
            elif error == "claims_unfulfilled_npc_payout":
                instructions.append(
                    "公开交易条件仍处于未兑现状态。不要写成NPC已经说出、交出、开放或完成了承诺；"
                    "可以让角色现在要求NPC兑现，或执行一项完全不依赖该承诺已经发生的行动。"
                )
            elif error == "claims_pending_npc_decision_result":
                instructions.append(
                    "NPC刚说的是‘将决定是否’，还没有宣布允许或拒绝。不要把决定当成已发生；"
                    "可以等待明确答复、讨论如何应对两种结果，或采取不依赖旧路已经开放的行动。"
                )
            elif error == "action_slot_contains_conditional_future_action":
                instructions.append(
                    "删掉‘如果需要就……’之类的未来预案；只保留角色现在真正执行的一项行动，不能让GM把备用方案提前结算。"
                )
            elif error == "action_slot_contains_deferred_future_action":
                instructions.append(
                    "不要把关键行动留到‘然后我会……’或‘之后再……’。只保留一个此刻立即完成的行动；"
                    "若角色已经决定接受或拒绝交易，就现在明确履行或拒绝，不要先重复准备。"
                )
            elif error == "action_slot_contains_multiple_actions":
                instructions.append(
                    "这一行动槽塞入了‘顺手/顺便’执行的第二项实质行动。只保留一个主要对象和一种明确做法；"
                    "说话、姿态和少量移动可以作为描述，但不能再检查、搬动、保护或操作另一个对象。"
                )
            elif error == "action_slot_preempts_gm_adjudication":
                instructions.append(
                    "只描述角色此刻如何行动及其目的；不要补‘如果需要检定’、‘请GM指定属性’或其他替GM安排裁定流程的话。"
                )
            elif error == "repeats_recent_action_lane":
                instructions.append(
                    "最近已经连续处理同一对象；不要再检查、确认、加固、监听或换一种说法重试。"
                    "必须利用已有结果作决定，回应GM刚兑现的变化，或转向一个尚未处理的具体对象。"
                    "如果队伍刚刚移动而当前角色需要跟上，只写角色离开旧位置并抵达队伍的新位置，"
                    "不要再附加此前已经做过的警戒、观察或确认。"
                )
            elif error == "rechecks_established_scene_fact":
                instructions.append(
                    "这项关系或结论已经被GM明确确认，不能再用观察、比对或核对重复证明。"
                    "直接利用它作决定；若要继续调查，必须提出一个尚未回答的新问题，例如来源、动机或后果。"
                )
            elif error == "repeats_disclosed_information":
                instructions.append(
                    "NPC已经把这项信息完整说过，不能要求他再说一遍。直接利用已公开内容作决定、完成交换、"
                    "明确拒绝，或转向一个尚未处理的对象。"
                )
            elif error == "repeats_resolved_information_delivery":
                instructions.append(
                    "同一名NPC刚刚已经明确接收或认可了这项信息，不能再向他原样交付一次。"
                    "可以指出刚才已经说过、追问对方接下来怎么做，或让角色改做一项新的现场行动。"
                )
            elif error == "ignores_explicit_gm_affordance":
                instructions.append(
                    "GM或NPC刚给出了可立即执行的邀请、通路、选择或现场指令；必须明确接受、拒绝、"
                    "选定其中一项，或落实一件被要求的可见动作。不能另起寒暄或退回此前已经完成的准备。"
                )
            elif error == "action_slot_uses_generic_scene_target":
                instructions.append("只能使用最近公开对话中已出现的具体人物或物件称呼，不能写‘对方’或‘当前目标’。")
            elif error == "action_slot_targets_mechanical_label":
                instructions.append(
                    "【优势/揭示/进展/纽带/转折】是机会效果，命刻名是局势记录，都不是角色能靠近、触摸或调查的物件。"
                    "改用当前场景确实可见的人物、道路、门、痕迹或物件，并描述角色在世界里实际做什么。"
                )
            elif error == "action_slot_targets_discussion_fragment":
                instructions.append(
                    "你把玩家讨论里的疑问或分工句截成了行动目标。不要把‘谁来做’‘谁有把握’‘要不要’之类文字放进【】；"
                    "只能选择GM已公开的具体人物、敌人、地点、痕迹或物件。"
                )
            elif error == "action_slot_targets_unestablished_entity":
                grounded = [
                    *legal_context.known_npcs,
                    *legal_context.known_enemies,
                    *legal_context.visible_scene_elements,
                ]
                examples = "、".join(str(item) for item in grounded[:6] if str(item or "").strip())
                instructions.append(
                    "这个行动目标没有在GM公开场景或合法行动上下文中出现；不要根据玩家猜测创造实体。"
                    + (f"请改用已经公开的对象，例如：{examples}。" if examples else "请改为观察当前环境或向已公开NPC行动。")
                )
            elif error == "out_of_turn_consuming_action":
                instructions.append("现在不是你的回合；只能给一句建议或说明轮到自己时的预备想法，不得结算行动。")
            elif error == "unsupported_spell_claim":
                instructions.append("不要声称施放未列在角色卡中的法术；换成普通行动或已掌握法术。")
            elif error == "player_name_used_as_fictional_character":
                instructions.append(
                    "你把桌外玩家名写进了世界。只用提示中的‘指定角色’代表该玩家在场人物；"
                    "若要指同伴，也只能使用合法上下文列出的玩家角色名。"
                )
            elif error == "spell_missing_required_parameter":
                instructions.append("已选法术要求明确选择目标及元素、异常或属性；按合法法术规则一次说全，不要让GM替你猜。")
            elif error == "spell_effect_mismatch":
                instructions.append(
                    "只声明角色卡上该法术写明的机械效果；不能把抗性、防御或治疗法术说成隐身、封门、"
                    "拖慢追兵或直接推进/擦除命刻。若想改变环境，改用普通行动、推进目标或仪式。"
                )
            elif error == "healing_spell_has_no_wounded_target":
                instructions.append(
                    "当前选中的角色生命值已满；不要浪费精神值施放治疗法术。"
                    "改为回应现场、保护同伴、调查、交涉，或使用另一项当前确有作用的能力。"
                )
            elif error == "crosses_unopened_route":
                instructions.append(
                    "这条路或入口仍未开放；角色必须留在当前这一侧，改为询问条件、观察入口附近、"
                    "满足公开门槛或寻找另一条已经公开可走的路，不能直接穿过去。"
                )
            elif error == "leaves_current_scene_without_transition":
                instructions.append(
                    "角色仍在当前场景，GM最新回复没有把镜头切到你写的地点，也没有开放通往那里的移动。"
                    "留在当前地点，改用此处已公开的人物、物件、入口或局部位置行动。"
                )
            elif error.startswith("action_slot_addresses_absent_npc:"):
                names = error.split(":", 1)[1]
                instructions.append(
                    f"【{names}】不在当前场景，不能直接向其提问、交付或要求行动。"
                    "改为与当前确实在场的NPC互动、处理眼前环境，或明确采取合法的移动/通讯行动。"
                )
            elif error == "ignores_immediate_scene_consequence":
                instructions.append(
                    "威胁刚刚兑现，不能继续刚才的普通调查。让角色立刻面对、阻挡、躲避、撤离、"
                    "保护他人或与新到场者交涉；只提交眼下的一项具体反应。"
                )
            elif error == "action_slot_leaves_adventure_for_setup_discussion":
                instructions.append(
                    "当前已经在冒险场景中，不要讨论开团基调、第零章、角色创建或‘推进剧情’。"
                    "只根据GM刚公开的人物、环境和压力，让指定角色现在采取一项具体行动或向现场NPC说一句话。"
                )
            elif error.startswith("debug_token:"):
                instructions.append("删掉所有测试、JSON、字段名和后台说明，只保留自然玩家发言。")
            else:
                instructions.append(f"修正这一问题：{error}；只使用已公开内容和角色已有能力。")
        return list(dict.fromkeys(instructions))

    def validate(
        self,
        text: str,
        *,
        step: ReplayStep,
        legal_context: LegalActionContext,
        recent_public_context: str = "",
    ) -> list[str]:
        errors: list[str] = []
        stripped = text.strip()
        if not stripped:
            return ["empty_utterance"]
        forbidden_debug = ["action_type", "target_number", "JSON", "测试目标", "合法行动上下文", "规则词汇"]
        for token in forbidden_debug:
            if token in stripped:
                errors.append(f"debug_token:{token}")
        if any(token in stripped for token in ["自动成功", "直接成功", "无需检定就成功"]):
            errors.append("declares_automatic_success")
        if re.search(r"\bd\d+\s*=\s*\d+", stripped):
            errors.append("declares_dice_result")
        if "施放" in stripped or "法术" in stripped:
            named_known_spell = any(spell and spell in stripped for spell in legal_context.legal_spells)
            if not named_known_spell and "施放已掌握法术" not in legal_context.legal_actions:
                errors.append("unsupported_spell_claim")
            if named_known_spell:
                errors.extend(
                    self._spell_rule_errors(
                        stripped,
                        legal_context,
                        actor=str(step.actor or ""),
                    )
                )
        if (
            "这是行动槽" in str(step.stage_goal or "")
            and any(alias in stripped for alias in self._GM_SPEAKERS)
        ):
            errors.append("gm_alias_used_as_fictional_target")
        if any(token in stripped for token in ["设定这里", "我设定", "新增一个事实", "这里一定有"]):
            if "消耗物语点" not in stripped and not step.kind.startswith("session_zero"):
                errors.append("world_fact_without_fabula")
        actor = step.actor
        stage_goal = str(step.stage_goal or "")
        if (
            "这是行动槽" in stage_goal
            and step.speaker
            and actor
            and step.speaker != actor
            and str(step.speaker)
            in re.sub(
                rf"^\s*{re.escape(str(step.speaker))}\s*[:：]\s*",
                "",
                stripped,
            )
        ):
            errors.append("player_name_used_as_fictional_character")
        requests_open_payout = bool(
            "这是行动槽" in stage_goal
            and self._requests_open_npc_payout(
                stripped,
                open_conditions=legal_context.open_npc_conditions,
            )
        )
        if "正在和其他玩家短暂商量" in stage_goal:
            if "机会" in stripped and any(
                cue in stripped
                for cue in (
                    "用于",
                    "用在",
                    "留着",
                    "记着",
                    "现在用",
                    "决定要不要",
                    "留到",
                    "以后用",
                    "关键的时候用",
                )
            ):
                errors.append("table_discussion_resolves_pending_opportunity")
            if actor and re.search(
                rf"{re.escape(actor)}.{{0,8}}(?:先|直接|马上|现在|去|来).{{0,12}}(?:处理|推进|压制|调查|观察|检查|确认|询问|攻击|施放|防御)",
                stripped,
            ):
                errors.append("table_discussion_declares_character_action")
            # Models sometimes omit an explicit time adverb and write a
            # completed in-fiction action directly (for example, “赛璃靠近旅人
            # 仔细检查”).  That is still an action turn, not player-to-player
            # discussion.  Keep genuine proposals such as “赛璃可以负责观察”
            # legal, because those have not committed the character yet.
            if actor and re.search(
                rf"(?:^|[，,。；;！？!?]){re.escape(actor)}"
                rf"(?!.{{0,10}}(?:可以|可否|能不能|要不要|是否|负责|最好|适合|建议|倾向))"
                rf"[^，,。；;！？!?]{{0,40}}"
                rf"(?:靠近|走向|走到|转向|跟上|进入|离开|伸手|触碰|拿起|放下|"
                rf"调查|观察|检查|查看|确认|询问|追问|安抚|保护|挡住|掩护|"
                rf"攻击|施放|防御|推进|打开)",
                stripped,
            ):
                errors.append("table_discussion_declares_character_action")
            if self._declares_action_in_table_discussion(stripped):
                errors.append("table_discussion_declares_character_action")
        if "这是行动槽" in stage_goal:
            if self._leaves_adventure_for_setup_discussion(stripped):
                errors.append("action_slot_leaves_adventure_for_setup_discussion")
            if any(
                placeholder in stripped
                for placeholder in (
                    "旁边能回答的人",
                    "能回答的人",
                    "当前目标",
                    "眼前的人物",
                    "眼前装置",
                    "伤势最明显的人",
                    "最近的遮挡",
                    "视野内仍可通行的路线",
                    "下一个遮挡点",
                    "最容易被冲开的缺口",
                    "问对方",
                )
            ):
                errors.append("action_slot_uses_generic_scene_target")
            if self._acts_on_mechanical_label(stripped, legal_context=legal_context):
                errors.append("action_slot_targets_mechanical_label")
            target_error = self._invalid_bracketed_action_target(
                stripped,
                step=step,
                legal_context=legal_context,
                recent_public_context=recent_public_context,
            )
            if target_error:
                errors.append(target_error)
            vague_objects = (
                "那件东西",
                "那块东西",
                "最能稳住局面的东西",
                "能拿出来的东西",
                "合适的担保物",
                "某种担保",
            )
            if any(token in stripped for token in vague_objects) and re.search(
                r"(?:拿出|取出|递出|交出|摆出|使用|带上|塞进|放到)",
                stripped,
            ) and not re.search(r"(?:是什么|指什么|哪一件|具体要什么)", stripped):
                errors.append("action_slot_acts_on_undefined_object")
            if self._acts_on_uninstantiated_remote_object(
                stripped,
                recent_public_context=recent_public_context,
                visible_scene_elements=legal_context.visible_scene_elements,
            ) and "action_slot_acts_on_undefined_object" not in errors:
                errors.append("action_slot_acts_on_undefined_object")
            absent_npc_mentions = [
                name
                for name in legal_context.known_npcs
                if legal_context.presence_authoritative
                and name not in legal_context.present_npcs
                and name in stripped
            ]
            if absent_npc_mentions and (
                self._looks_like_direct_npc_question(stripped)
                or re.search(
                    r"(?:询问|追问|请求|拜托|说服|威胁|递给|交给|看向|转向)",
                    stripped,
                )
            ):
                errors.append(
                    "action_slot_addresses_absent_npc:"
                    + "、".join(absent_npc_mentions)
                )
            discussion_cues = ("我建议", "我觉得", "你们觉得", "大家觉得", "谁来", "谁方便", "要不要")
            first_person_commitment = any(
                cue in stripped
                for cue in ("我先", "我来", "我去", "我继续", "我要", "我问", "我确认", "我检查", "我观察")
            )
            hypothetical_actor_action = bool(
                actor
                and re.search(
                    rf"{re.escape(actor)}.{{0,8}}(?:可以|要不要|能不能|是否).{{0,24}}(?:调查|观察|检查|确认|询问|追问|安抚|行动)",
                    stripped,
                )
            )
            if hypothetical_actor_action or (
                any(cue in stripped for cue in discussion_cues) and not first_person_commitment
            ):
                errors.append("action_slot_contains_only_table_discussion")
            if self._delegates_action_to_teammate(
                stripped,
                actor=actor,
                known_pcs=legal_context.known_pcs,
            ):
                errors.append("action_slot_delegates_to_teammate")
            if self._conditional_future_action(stripped, actor=actor):
                errors.append("action_slot_contains_conditional_future_action")
            if self._deferred_future_action(stripped, actor=actor):
                errors.append("action_slot_contains_deferred_future_action")
            if self._contains_multiple_independent_actions(stripped):
                errors.append("action_slot_contains_multiple_actions")
            if self._ignores_immediate_scene_consequence(
                stripped,
                legal_context=legal_context,
                recent_public_context=recent_public_context,
            ):
                errors.append("ignores_immediate_scene_consequence")
            if re.search(r"(?:如果|若)(?:这|此)?(?:需要|要做).{0,6}检定|请(?:由)?\s*GM\s*指定|请按合适属性处理", stripped, re.I):
                errors.append("action_slot_preempts_gm_adjudication")
            if not self._contains_world_facing_action(stripped, actor=actor):
                errors.append("action_slot_contains_only_table_discussion")
            if (
                "本行动不得继续向NPC追问" in stage_goal
                and self._looks_like_direct_npc_question(stripped)
                and not requests_open_payout
                and not self._responds_to_explicit_gm_affordance(stripped, recent_public_context)
            ):
                errors.append("repeats_saturated_npc_question_lane")
        if legal_context.conflict_active and actor and actor != legal_context.current_actor:
            consuming_words = ["攻击", "施放", "推进", "防御", "妨碍", "使用库存", "检定"]
            if any(word in stripped for word in consuming_words) and not any(
                phrase in stripped for phrase in ["等", "预备", "轮到我", "建议", "先不结算"]
            ):
                errors.append("out_of_turn_consuming_action")
        answers_pending_decision = bool(
            legal_context.pending_decisions
            and self._answers_pending_decision(
                stripped,
                legal_context.pending_decisions[0],
            )
        )
        if legal_context.pending_decisions and not answers_pending_decision:
            errors.append("does_not_answer_pending_decision")
        if (
            not answers_pending_decision
            and self._near_duplicate_player_utterance(stripped, recent_public_context)
        ):
            errors.append("near_duplicate_recent_player_utterance")
        if "这是行动槽" in stage_goal and not requests_open_payout and self._requests_repeated_disclosure(
            stripped,
            recent_public_context,
        ):
            errors.append("repeats_disclosed_information")
        if (
            "这是行动槽" in stage_goal
            and not requests_open_payout
            and self._repeats_resolved_information_delivery(stripped, recent_public_context)
        ):
            errors.append("repeats_resolved_information_delivery")
        if (
            "这是行动槽" in stage_goal
            and self._repeats_settled_npc_exchange(
                stripped,
                legal_context.settled_npc_exchanges,
            )
        ):
            errors.append("repeats_settled_npc_exchange")
        if (
            "这是行动槽" in stage_goal
            and not requests_open_payout
            and self._rechecks_established_scene_fact(
                stripped,
                legal_context.established_scene_facts,
            )
        ):
            errors.append("rechecks_established_scene_fact")
        if (
            "这是行动槽" in stage_goal
            and not requests_open_payout
            and self._repeats_recent_action_lane(stripped, recent_public_context)
        ):
            errors.append("repeats_recent_action_lane")
        if "这是行动槽" in stage_goal and self._claims_another_players_commitment(
            stripped,
            recent_public_context,
            current_speaker=step.speaker,
            actor=actor or "",
        ):
            errors.append("claims_another_players_commitment")
        if "这是行动槽" in stage_goal and self._claims_npc_controlled_outcome(
            stripped,
            actor=actor or "",
        ):
            errors.append("claims_npc_controlled_outcome")
        if self._claims_pending_npc_decision_result(stripped, recent_public_context):
            errors.append("claims_pending_npc_decision_result")
        if self._claims_unfulfilled_npc_payout(
            stripped,
            actor=actor or "",
            open_conditions=legal_context.open_npc_conditions,
        ):
            errors.append("claims_unfulfilled_npc_payout")
        if (
            "这是行动槽" in stage_goal
            and not requests_open_payout
            and not legal_context.conflict_active
            and not legal_context.pending_decisions
            and self._repeats_same_actor_target_action(
                stripped,
                recent_public_context,
                actor=actor or "",
            )
        ):
            if "repeats_recent_action_lane" not in errors:
                errors.append("repeats_recent_action_lane")
        if "这是行动槽" in stage_goal and self._ignores_explicit_gm_affordance(
            stripped,
            recent_public_context,
        ):
            errors.append("ignores_explicit_gm_affordance")
        if "这是行动槽" in stage_goal and self._crosses_unopened_route(
            stripped,
            recent_public_context,
            blocked_routes=legal_context.blocked_routes,
        ):
            errors.append("crosses_unopened_route")
        if "这是行动槽" in stage_goal and self._leaves_current_scene_without_transition(
            stripped,
            legal_context=legal_context,
            recent_public_context=recent_public_context,
        ):
            errors.append("leaves_current_scene_without_transition")
        return errors

    @staticmethod
    def _spell_rule_errors(
        text: str,
        legal_context: LegalActionContext,
        *,
        actor: str = "",
    ) -> list[str]:
        rule = next(
            (
                item
                for item in legal_context.legal_spell_rules
                if str(item.get("name") or "") and str(item.get("name")) in text
            ),
            None,
        )
        if rule is None:
            return []
        errors: list[str] = []
        selectable_groups = [
            list(rule.get("selectable_damage_types") or []),
            list(rule.get("selectable_statuses") or []),
            list(rule.get("selectable_attributes") or []),
        ]
        for group in selectable_groups:
            if group and not any(
                str(value) in text or f"{value}系" in text
                for value in group
            ):
                errors.append("spell_missing_required_parameter")
                break
        effect_type = str(rule.get("effect_type") or "")
        if effect_type == "heal" and legal_context.pc_resources:
            target_names = [
                name
                for name in legal_context.pc_resources
                if name and name != actor and name in text
            ]
            if actor and "自己" in text and actor in legal_context.pc_resources:
                target_names.append(actor)
            target_names = list(dict.fromkeys(target_names))
            relevant = (
                [legal_context.pc_resources[name] for name in target_names]
                if target_names
                else list(legal_context.pc_resources.values())
            )
            if relevant and not any(
                int(values.get("hp", 0)) < int(values.get("max_hp", 0))
                for values in relevant
            ):
                errors.append("healing_spell_has_no_wounded_target")
        if effect_type in {
            "heal",
            "defense_buff",
            "defense_floor",
            "affinity_buff",
            "status_clear",
            "status_immunity",
            "weapon_enchant",
            "attribute_buff",
            "extra_action",
            "survive_once",
        } and (
            re.search(r"(?:推进|填充|擦除|倒转|削减).{0,8}命刻", text)
            or re.search(r"(?:拖慢|延缓|阻止).{0,12}(?:巡逻|追兵|逼近|命刻)", text)
            or (
                effect_type == "affinity_buff"
                and re.search(r"(?:遮住|遮蔽|隐藏|隐蔽|封住).{0,12}(?:门|廊口|身影|道路|出口)", text)
            )
        ):
            errors.append("spell_effect_mismatch")
        return list(dict.fromkeys(errors))

    @staticmethod
    def _leaves_adventure_for_setup_discussion(text: str) -> bool:
        """Reject a synthetic player that falls out of an active scene.

        This guard belongs to FU-PL's test boundary rather than production
        routing.  An adventure action slot may contain in-character doubt, but
        it may not suddenly answer Session 0 questions or talk about moving the
        plot as a test author would.
        """

        return bool(
            re.search(
                r"开团前(?:共识|讨论)|第零章|世界创建|角色创建|"
                r"(?:往前|继续|开始|先不急着)(?:推|推进)剧情|"
                r"想玩的故事(?:味道|风格)?|这次(?:故事)?(?:更|想|要|偏).{0,8}"
                r"(?:严肃正剧|王道英雄|轻松欢乐|黑暗沉重)|"
                r"故事(?:更|想|要|偏).{0,8}(?:严肃正剧|王道英雄|轻松欢乐|黑暗沉重)",
                str(text or ""),
            )
        )

    @classmethod
    def _claims_another_players_commitment(
        cls,
        text: str,
        recent_context: str,
        *,
        current_speaker: str,
        actor: str,
    ) -> bool:
        subject = rf"(?:我|{re.escape(actor)})" if actor else r"我"
        claims_prior = bool(
            re.search(
                rf"{subject}.{{0,8}}(?:已经|刚才|刚刚|先前|之前).{{0,10}}"
                r"(?:答应|承诺|保证|应下|说过|同意承担)",
                str(text or ""),
            )
        )
        if not claims_prior:
            return False

        commitment = re.compile(
            r"(?:答应|承诺|保证|应下|同意承担)|"
            r"我来.{0,16}(?:守住|守着|护送|照看|保护|承担)"
        )
        own_commitment = False
        other_commitment = False
        for speaker, utterance in cls._dialogue_blocks(recent_context):
            if speaker in cls._GM_SPEAKERS or not commitment.search(utterance):
                continue
            if speaker == current_speaker:
                own_commitment = True
            else:
                other_commitment = True
        return other_commitment and not own_commitment

    @classmethod
    def _claims_npc_controlled_outcome(
        cls,
        text: str,
        *,
        actor: str,
    ) -> bool:
        """Keep synthetic players from paying out an NPC's promise.

        A player may fulfil a bargain by silencing a bell or presenting proof,
        but only the NPC can decide to open its road, grant permission, hand
        over its key, or reveal its information.  Requiring a future/conditional
        promise here avoids rejecting immediate, player-controlled actions such
        as using a key already in the hero's possession to open a door.
        """

        source = cls._strip_optional_speaker_prefix(text)
        subject = rf"(?:我|{re.escape(actor)})" if actor else r"我"
        promise = re.search(
            rf"(?:等|待|只要|如果|若是?|一旦).{{0,30}}[，,；;]?\s*"
            rf"(?P<outcome>{subject}.{{0,40}})",
            source,
        )
        if promise is None:
            return False
        outcome = promise.group("outcome")
        if not re.search(rf"^{subject}.{{0,4}}(?:就|会|便|可以|再)", outcome):
            return False

        controlled_object = (
            r"旧路|侧路|密道|通道|入口|出口|关口|大门|闸门|"
            r"钥匙|通行证|路引|许可|放行权|档案|卷宗|情报|答案|线索"
        )
        object_then_transfer = re.search(
            rf"(?:把)?(?:{controlled_object}).{{0,18}}"
            r"(?:给|交给|交出|提供|开放|放行|准许|允许|告诉|透露|说明)",
            outcome,
        )
        authority_then_object = re.search(
            rf"(?:开放|放行|准许|允许|交出|提供|告诉|透露|说明).{{0,18}}"
            rf"(?:{controlled_object}|你|你们|队伍|大家|旅人)",
            outcome,
        )
        return bool(object_then_transfer or authority_then_object)

    @classmethod
    def _claims_unfulfilled_npc_payout(
        cls,
        text: str,
        *,
        actor: str,
        open_conditions: list[dict[str, str]],
    ) -> bool:
        """Reject a synthetic player turning an open NPC promise into fact."""

        conditions = [
            item
            for item in (open_conditions or [])
            if str(item.get("promised_result") or "").strip()
        ]
        if not conditions:
            return False

        source = cls._strip_optional_speaker_prefix(text)
        npc_names = [
            str(item.get("npc") or "").strip()
            for item in conditions
            if str(item.get("npc") or "").strip()
        ]
        npc_subjects = ["他", "她", "对方"]
        for npc_name in npc_names:
            npc_subjects.extend(cls._npc_reference_forms(npc_name))
        subject_pattern = "(?:" + "|".join(
            sorted((re.escape(item) for item in set(npc_subjects)), key=len, reverse=True)
        ) + ")"
        completed_claims: list[str] = []
        for match in re.finditer(
            rf"{subject_pattern}.{{0,10}}(?:已经|刚才|刚刚|早已|已然|已)"
            r"(?P<bridge>[^，,。；;！？!?]{0,12}?)(?P<verb>"
            r"给出|说出|说完|讲出|讲完|告诉|透露|说明|交代|交出|交给|提供|"
            r"打开|开放|放行|准许|允许|兑现|退开|后退|撤开)",
            source,
        ):
            bridge = str(match.group("bridge") or "")
            if re.search(r"愿意|答应|同意|承诺|保证|表示|准备|打算|将要|将会|会|可以|能", bridge):
                continue
            completed_claims.append(match.group(0))

        actor_pattern = rf"(?:我|我们|{re.escape(actor)})" if actor else r"(?:我|我们)"
        completed_claims.extend(
            match.group(0)
            for match in re.finditer(
                rf"{actor_pattern}.{{0,10}}(?:已经|刚才|刚刚|早已|已然|已).{{0,10}}"
                r"(?:拿到|得到|听到|听完|获知|获准|收到|取得)",
                source,
            )
        )
        if not completed_claims:
            return False

        source_modes = cls._npc_payout_modes(source)
        source_topics = cls._npc_payout_topics(source)
        demonstrative = bool(
            re.search(
                r"(?:那|这|该)(?:一|半)?(?:段|份|条|项|个)?|承诺|答复|答案|内容|信息|结果",
                source,
            )
        )
        matching_conditions = [
            item
            for item in conditions
            if source_modes & cls._npc_payout_modes(str(item.get("promised_result") or ""))
        ]
        # When exactly one still-open promise has the same kind of payout,
        # saying that the NPC already performed it is unambiguous even when
        # the player uses a short role name ("会长") or omits the object
        # ("已经放行了").  Earlier code required a shared topic word, which
        # let a false table-talk premise slip through.
        if len(matching_conditions) == 1:
            return True
        for item in conditions:
            promised = str(item.get("promised_result") or "").strip()
            if not (source_modes & cls._npc_payout_modes(promised)):
                continue
            if source_topics & cls._npc_payout_topics(promised):
                return True
            left = cls._normalize_for_similarity(source)
            right = cls._normalize_for_similarity(promised)
            shared = cls._ngrams(left, 3) & cls._ngrams(right, 3)
            if demonstrative or any(
                gram not in {"我现在", "现在会", "会当场", "当场把", "你们有"}
                for gram in shared
            ):
                return True
        return False

    @classmethod
    def _claims_pending_npc_decision_result(
        cls,
        text: str,
        recent_public_context: str,
    ) -> bool:
        """Reject treating "I will decide whether" as an already-made choice.

        This remains useful even if an older condition was prematurely marked
        resolved by a faulty upstream response.  It only examines the latest
        GM reply, so an earlier, actually settled gate cannot poison later
        normal discussion.
        """

        latest = cls._latest_gm_reply(recent_public_context)
        if not latest:
            return False
        pending = re.search(
            r"(?:我|他|她|对方|会长|守卫|监察官)?(?:现在|接下来|随后)?(?:就|会|将)?"
            r"(?:决定|判断|裁定).{0,14}(?:是否|要不要|能否|可否|该不该|让不让|准不准|放不放)",
            latest,
        )
        if not pending:
            return False
        source = cls._strip_optional_speaker_prefix(text)
        return bool(
            re.search(
                r"(?:既然|既已|既).{0,28}(?:已经|已|当场|刚才|方才).{0,12}(?:作出|做出)?决定"
                r"|(?:你|他|她|对方|会长|守卫|监察官).{0,10}"
                r"(?:已经|已|当场|刚才|方才).{0,12}(?:作出|做出)?决定",
                source,
            )
        )

    @staticmethod
    def _npc_reference_forms(npc_name: str) -> set[str]:
        """Return the public short forms a player can naturally use for an NPC.

        The simulator only needs aliases that are already contained in the
        public NPC name.  This deliberately avoids inventing new nicknames,
        while still treating "会长已经放行" as a claim about
        "白花守望会会长".
        """

        clean = str(npc_name or "").strip()
        if not clean:
            return set()
        forms = {clean}
        role_suffixes = (
            "会长",
            "监察官",
            "代理人",
            "旅人",
            "医师",
            "守卫",
            "船长",
            "长者",
            "司教",
            "公主",
            "国王",
            "女王",
            "队长",
            "领主",
            "使者",
            "店主",
            "老板",
        )
        for suffix in role_suffixes:
            if clean.endswith(suffix):
                forms.add(suffix)
        return forms

    @classmethod
    def _requests_open_npc_payout(
        cls,
        text: str,
        *,
        open_conditions: list[dict[str, str]],
    ) -> bool:
        """Recognise a request to collect an NPC promise that is still open.

        Asking a witness to *now* say the route they promised is not the same
        action lane as asking them to rediscover or repeat an answer.  The
        promise remains authoritative state, so this exemption applies only
        when the request matches both its NPC and its promised result.
        """

        conditions = [
            item
            for item in (open_conditions or [])
            if str(item.get("promised_result") or "").strip()
        ]
        if not conditions:
            return False
        source = cls._strip_optional_speaker_prefix(text)
        request_cue = bool(
            re.search(
                r"(?:请|要求|催促|提醒).{0,30}"
                r"(?:兑现|说出|说完|讲出|讲完|告诉|透露|说明|交代|回答|给出|揭示|"
                r"打开|开放|放行|交出|交给|提供|退开|撤开|让开)|"
                r"(?:现在|这就|该你|轮到你).{0,24}"
                r"(?:兑现|说|讲|告诉|透露|说明|交代|回答|给出|打开|开放|放行|交出|提供|退开|撤开|让开)|"
                r"(?:兑现|履行|按约|依约|照约|照刚才说的)|"
                r"(?:说|讲|交|递|拿|给|打开|退开|让开)(?:出|出来|开|给我|给我们|吧)",
                source,
            )
        )
        if not request_cue:
            return False

        source_modes = cls._npc_payout_modes(source)
        source_topics = cls._npc_payout_topics(source)
        promise_reference = bool(re.search(r"承诺|答应|约定|说好|按约|依约|兑现|履行", source))
        pronoun_target = bool(re.search(r"(?:请|让|催|提醒)(?:他|她|对方)", source))
        for item in conditions:
            npc = str(item.get("npc") or "").strip()
            promised = str(item.get("promised_result") or "").strip()
            if npc and npc not in source and not (len(conditions) == 1 and pronoun_target):
                continue
            promised_modes = cls._npc_payout_modes(promised)
            promised_topics = cls._npc_payout_topics(promised)
            if not promise_reference and not (source_modes & promised_modes):
                continue
            if promise_reference or not promised_topics or source_topics & promised_topics:
                return True
        return False

    @staticmethod
    def _npc_payout_modes(text: str) -> set[str]:
        source = str(text or "")
        modes: set[str] = set()
        if re.search(
            r"说出|说完|讲出|讲完|告诉|透露|说明|交代|回答|揭示|给出|"
            r"(?:会|愿意|可以|现在|就)说(?:出|清|完|那|这)",
            source,
        ):
            modes.add("disclose")
        if re.search(r"打开|开放|放行|准许|允许|获准|通行|开门|开路", source):
            modes.add("access")
        if re.search(r"交出|交给|递交|提供|归还|拿到|得到|收到|取得", source):
            modes.add("transfer")
        if re.search(r"退开|后退|撤开|离开|移开|让开", source):
            modes.add("movement")
        return modes

    @staticmethod
    def _npc_payout_topics(text: str) -> set[str]:
        source = str(text or "")
        topics: set[str] = set()
        patterns = {
            "route": r"方向|去路|路线|路径|走法|地标|入口|出口|哪边|一小段路|半段路",
            "identity": r"名字|人名|身份|是谁|哪一位|哪个人",
            "information": r"情报|线索|真相|事实|答案|内容|原因|动机",
            "document": r"档案|卷宗|账册|名册|记录|通行证|路引",
            "key": r"钥匙|门锁|闸门",
            "position": r"退开|后退|撤开|离开|位置|门口",
        }
        for topic, pattern in patterns.items():
            if re.search(pattern, source):
                topics.add(topic)
        return topics

    @classmethod
    def _ignores_immediate_scene_consequence(
        cls,
        text: str,
        *,
        legal_context: LegalActionContext,
        recent_public_context: str,
    ) -> bool:
        consequence = str(legal_context.immediate_scene_consequence or "").strip()
        if not consequence:
            return False
        latest_gm = cls._latest_gm_reply(recent_public_context)
        if not latest_gm:
            return False
        # Only the first player action after the consequence is constrained.
        # Once the GM has answered that reaction, the latest public reply no
        # longer contains the completion announcement.
        consequence_tokens = set(re.findall(r"[\u4e00-\u9fff]{2,6}", consequence))
        completion_visible = bool(
            re.search(r"(?:\d+\s*/\s*\d+|已经|填满|包围|抵达|封住|爆发|兑现)", latest_gm)
            and any(token in latest_gm for token in consequence_tokens)
        )
        if not completion_visible:
            return False
        direct_reactions = (
            "迎上", "挡住", "拦住", "封门", "关门", "撤", "逃", "躲", "隐蔽", "掩护",
            "保护", "护住", "救", "交涉", "谈判", "投降", "攻击", "防御", "警戒", "拖延",
            "对峙", "应对", "转移", "带走", "藏好", "收起证据",
        )
        if any(token in text for token in direct_reactions):
            return False
        if any(token in text for token in consequence_tokens):
            return False
        return bool(re.search(r"(?:调查|观察|检查|比对|核对|研究|拆解|分析|继续看|继续问)", text))

    @staticmethod
    def _conditional_future_action(text: str, *, actor: str = "") -> bool:
        actor_part = rf"(?:我|{re.escape(actor)})" if actor else r"我"
        return bool(
            re.search(
                rf"(?:如果|若是|要是|必要时|到时候|万一).{{0,28}}{actor_part}.{{0,12}}"
                r"(?:也可以|可以|会|再|就|也?想(?:要)?|打算).{0,24}"
                r"(?:施放|施展|使用|启动|发动|建立|推进|攻击|防御|掩护|调查|观察|"
                r"询问|追问|改问|请求|打开|挡住|撑开|展开|隔开|拦住|收好|收起|收拾|"
                r"收拢|收一收|藏好|整理|留意|查看|听|看)",
                text,
            )
        )

    @staticmethod
    def _deferred_future_action(text: str, *, actor: str = "") -> bool:
        """Reject a second substantive move postponed behind the current one."""

        actor_part = rf"(?:我|{re.escape(actor)})" if actor else r"我"
        source = str(text or "")
        if re.search(
                rf"(?:然后|接着|随后|之后|回头再|(?:说完|听完|做完|完成|结束)(?:后|以后))[，,]?\s*{actor_part}?\s*"
                r"(?:会|要|就|再|准备|打算).{0,28}"
                r"(?:交出|递出|拿去|拿|带去|告诉|说给|送去|转向|回话|回应|打开|进入|调查|观察|检查|"
                r"攻击|施放|启动|推进|防御|掩护|安抚|追问|询问)",
                source,
            ):
            return True
        # A synthetic player must not consume an NPC response and a second
        # action in one message.  The NPC's promised answer has not happened
        # until the GM actually speaks it, so “wait for A to answer, then act
        # on B” would otherwise make the simulator invent an invisible beat.
        return bool(
            re.search(
                r"(?:等|待|先听|先等).{0,36}(?:说完|回答|答复|回应|说明|交代|告诉).{0,14}"
                r"(?:以后|之后|后|便|就|再|然后|随后|接着|[，,；;]).{0,48}"
                r"(?:转向|朝|对|拿|带|告诉|开口|回话|回应|询问|追问|交涉|攻击|施放|启动|推进|防御|掩护)",
                source,
            )
        )

    @classmethod
    def _requests_repeated_disclosure(cls, text: str, recent_context: str) -> bool:
        """Catch synthetic players asking an NPC to repeat known information."""

        source = str(text or "")
        if not re.search(
            r"(?:再|重新).{0,10}(?:说|讲|念|告诉|重复)|(?:说|讲|念|重复).{0,6}(?:一遍|一次)",
            source,
        ):
            return False
        if re.search(
            r"没听清|没有听见|被.{0,8}(?:打断|盖住)|请再说",
            cls._latest_gm_reply(recent_context),
        ):
            return False
        return any(
            speaker in cls._GM_SPEAKERS and len(utterance.strip()) >= 12
            for speaker, utterance in cls._dialogue_blocks(recent_context)
        )

    @classmethod
    def _repeats_resolved_information_delivery(cls, text: str, recent_context: str) -> bool:
        """Reject a second delivery after that same NPC already accepted it.

        Relaying information from one NPC to another is legitimate play. This
        synthetic-player guard therefore requires an explicit delivery target,
        matching subject matter, and an earlier acceptance spoken by that
        target. It does not affect messages from real players.
        """

        source = cls._strip_optional_speaker_prefix(text)
        explicit_delivery = bool(
            re.search(
                r"(?:把|将).{0,80}(?:转告|告诉|讲给|说给|递给|交给|交付|复述给)|"
                r"(?:转告|告诉|讲给|说给|递给|交给|交付|复述给)(?:你|他|她|对方|这位)",
                source,
            )
        )
        if not explicit_delivery:
            return False
        target_roles = {
            role
            for role in (
                "使者",
                "旅人",
                "会长",
                "守碑人",
                "监察官",
                "守卫",
                "商人",
                "祭司",
                "钟匠",
                "船长",
                "村长",
            )
            if re.search(
                rf"(?:对|向|回应|答复|告诉|讲给|说给)[^，,。；;！？!?\n]{{0,28}}{role}",
                source,
            )
        }
        if not target_roles:
            return False
        topics = cls._information_delivery_topics(source)
        if not topics:
            return False
        gm_replies = [
            utterance
            for speaker, utterance in cls._dialogue_blocks(recent_context)
            if speaker in cls._GM_SPEAKERS
        ]
        for reply in gm_replies[-10:]:
            overlap = topics & cls._information_delivery_topics(reply)
            if len(overlap) < min(2, len(topics)):
                continue
            for role in target_roles:
                if re.search(
                    rf"{role}.{{0,8}}(?:答道|说道|回应道|回应|答复|说)[：:]?[‘'\"“「『]?"
                    r".{0,28}(?:可以|接受|收下|同意|就按|已经收到|已经听见|已经记下|条件.{0,8}说清)",
                    reply,
                ):
                    return True
        return False

    @staticmethod
    def _information_delivery_topics(text: str) -> set[str]:
        source = str(text or "")
        patterns = {
            "route": r"去路|路线|路径|方向感|方向",
            "route_name": r"完整名字|路名|名字",
            "route_steps": r"全段走法|走法|怎么走|门槛|墙根",
            "route_end": r"终点|目的地|通向哪里",
            "terms": r"条件|交换|交付|只认一项",
            "identity": r"身份|真名|姓名",
            "evidence": r"证据|账册|记录|封蜡|印记",
        }
        return {name for name, pattern in patterns.items() if re.search(pattern, source)}

    @staticmethod
    def _contains_multiple_independent_actions(text: str) -> bool:
        """Keep a synthetic action slot to one adjudicable action."""

        source = str(text or "")
        major_action = re.compile(
            r"去看|查看|调查|观察|检查|确认|追查|听清|辨认|分析|比对|核对|"
            r"拿起|取出|拾起|摘下|摘近|搬动|移动|挪动|挪近|带走|藏好|收好|"
            r"挡住|护住|安抚|交涉|攻击|施放|防御|推进|妨碍|启动|打开|关闭|修理|拆除"
        )
        for connector in re.finditer(r"(?:同时\s*)?(?:顺手|顺便)", source):
            before = source[: connector.start()]
            after = source[connector.end() :]
            if major_action.search(before) and major_action.search(after):
                return True
        return False

    def _contains_world_facing_action(self, text: str, *, actor: str = "") -> bool:
        raw = str(text or "")
        # A colon inside an in-fiction sentence ("直接问她：……") is not a
        # chat-speaker prefix.  Check the untouched line first so stripping a
        # display prefix cannot erase the committed approach-and-question.
        if self._looks_like_direct_npc_question(raw):
            return True
        source = self._strip_optional_speaker_prefix(raw)
        if self._looks_like_direct_npc_question(source):
            return True
        actor_part = rf"(?:我|{re.escape(actor)})" if actor else r"我"
        action = (
            r"抬手|伸手|触碰|拿起|取出|放下|收起|走|跑|蹲|站到|退到|退入|俯身|侧身|转向|转身|"
            r"贴进|躲进|躲入|藏进|藏入|隐入|伏下|潜伏|等待|等候|"
            r"看向|贴近|盯住|听清|检查|观察|调查|查看|确认|询问|追问|开口|说|喊|呼喊|敲|叩|摇铃|按铃|递出|交出|"
            r"推|拉|挡|按|带|领|扶住|挪开|移开|带离|带往|带到|送到|安置|指出|标出|指明|"
            r"贴住|抵住|顶住|卡住|卡进|收稳|包住|包起|包好|藏进|护住|安抚|用|"
            r"承担|担保|承诺|宣誓|立誓|作证|见证|攻击|施放|防御|推进|妨碍|使用|启动|打开"
        )
        # Natural play often states why an old approach is being abandoned
        # before committing to a different action later in the same sentence.
        # The former 14-character window rejected lines such as “我不再碰花瓣，
        # 转而观察旧路闸门” even though the hero plainly acts in the fiction.
        subject = re.search(rf"(?:^|[，,。；;！？!?])\s*{actor_part}", source)
        return bool(subject and re.search(action, source[subject.start() :]))

    @classmethod
    def _delegates_action_to_teammate(
        cls,
        text: str,
        *,
        actor: str = "",
        known_pcs: list[str] | None = None,
    ) -> bool:
        """Reject action-slot lines that only spend another PC's agency.

        Asking a teammate to do something is valid table talk, but it is not
        the current hero's world-facing action. A line remains valid when the
        current hero also performs a concrete action of their own.
        """

        source = cls._strip_optional_speaker_prefix(text)
        pcs = [
            str(name or "").strip()
            for name in (known_pcs or cls._KNOWN_PLAYER_SPEAKERS)
            if str(name or "").strip() and str(name or "").strip() not in cls._GM_SPEAKERS
        ]
        others = [name for name in dict.fromkeys(pcs) if name != actor and name != "玩家"]

        personal_subject = rf"(?:我|{re.escape(actor)})" if actor else r"我"
        personal_action = bool(
            re.search(
                rf"(?:^|[，,。；;]){personal_subject}.{{0,16}}"
                r"(?:签(?:下|上|名)|按下|递出|交出|拿出|打开|关上|进入|离开|走到|跟上|"
                r"调查|检查|观察|追踪|挡住|护住|扶住|安抚|攻击|施放|防御|推进|妨碍|"
                r"承担|担下|担起|接下|接受|拒绝|答应|承诺|宣誓|立誓|核验|操作|修理|拆除)",
                source,
            )
        )
        if personal_action:
            return False

        if actor and re.search(
            rf"(?:^|[，,。；;]){re.escape(actor)}[，,:：].{{0,12}}"
            r"(?:(?:我|现在).{0,6})?(?:要你|请你|让你|你来|你去|你先|麻烦你|轮到你)",
            source,
        ):
            return True

        directive_verbs = (
            r"签(?:下|上|名)|担保|递|交|拿|开门|打开|关上|处理|照看|保护|挡|守|盯|"
            r"调查|检查|观察|追踪|带走|带路|跟上|进去|离开|回答|说明|交涉|攻击|施放"
        )
        for teammate in others:
            escaped = re.escape(teammate)
            if actor and re.search(
                rf"(?:^|[，,。；;]){re.escape(actor)}(?:现在|随后|接着|再)?"
                rf"{escaped}.{{0,18}}(?:{directive_verbs}|承担|承诺|答应|宣誓|立誓)",
                source,
            ):
                return True
            if re.search(
                rf"{escaped}.{{0,10}}[，,:：]?.{{0,4}}(?:请|让)(?:他|她).{{0,18}}(?:去|来|把|先|直接|{directive_verbs})",
                source,
            ):
                return True
            if re.search(
                rf"(?:请|让|叫|要|希望|催促){escaped}.{{0,18}}(?:去|来|把|先|直接|{directive_verbs})",
                source,
            ):
                return True
            if re.search(
                rf"(?:^|[，,。；;]){escaped}[，,:：]?.{{0,14}}"
                rf"(?:请你|你来|你去|你先|麻烦你|能不能|要不要|{directive_verbs})",
                source,
            ):
                return True

        return bool(
            re.search(
                rf"(?:谁|你们)(?:能|来|去|方便|愿意|负责|先).{{0,24}}(?:{directive_verbs})",
                source,
            )
        )

    @classmethod
    def _acts_on_uninstantiated_remote_object(
        cls,
        text: str,
        *,
        recent_public_context: str,
        visible_scene_elements: list[str],
    ) -> bool:
        """A lead to an archive is not a ledger already in the hero's hands."""

        handled = re.findall(
            r"(?:拿起|取出|翻开|翻看|查看|检查|比对|核对|递出|收起|带上|使用)"
            r"[^，,。；;！？!?\n]{0,12}"
            r"(?P<object>旧账册|账册|旧档|档案|登记簿|名册|卷宗|地图|钥匙|文书)",
            str(text or ""),
        )
        if not handled:
            return False
        visible = " ".join(str(item or "") for item in visible_scene_elements)
        context = str(recent_public_context or "")[-5000:]
        for obj in handled:
            if obj in visible:
                continue
            mentions = [
                match
                for match in re.finditer(re.escape(obj), context)
            ]
            if not mentions:
                return True
            physically_present = False
            only_remote = True
            for match in mentions:
                window = context[max(0, match.start() - 45) : match.end() + 45]
                if re.search(r"(?:摆在|放在|摊在|压在|手里|桌上|柜台上|眼前|递给|交给|取来|拿来|找到了)", window):
                    physically_present = True
                if not re.search(r"(?:可查|能查|可以查|去.{0,16}(?:查|找)|档案(?:室|库)|库里|以后|改日|明天)", window):
                    only_remote = False
            if not physically_present and only_remote:
                return True
        return False

    @classmethod
    def _repeats_recent_action_lane(cls, text: str, recent_context: str) -> bool:
        committed_prefix = cls._committed_action_before_last_pivot(text)
        if committed_prefix and cls._repeats_recent_action_lane(
            committed_prefix,
            recent_context,
        ):
            return True
        # NPC questions have their own saturation/affordance checks.  Treating
        # “put the old clue away, then ask the witness something new” as a
        # manipulate lane made valid follow-up dialogue look like a repeated
        # investigation merely because both sentences mentioned the witness.
        if cls._looks_like_direct_npc_question(text):
            physical_prefix = cls._physical_action_before_npc_question(text)
            if physical_prefix and cls._repeats_recent_action_lane(
                physical_prefix,
                recent_context,
            ):
                return True
            return cls._repeats_recent_npc_question(text, recent_context)
        if cls._repeats_resolved_investigation_subject(text, recent_context):
            return True
        family = cls._action_family(text)
        tokens = cls._action_lane_tokens(text)
        anchors = cls._action_lane_anchors(text)
        if not family or (len(tokens) < 3 and not anchors):
            return False
        matches = 0
        anchor_matches = 0
        low_progress_families = {"investigate", "guard", "manipulate", "move", "care"}
        for prior in cls._recent_player_utterances(recent_context)[-18:]:
            if cls._looks_like_uncommitted_table_talk(prior):
                continue
            prior_family = cls._action_family(prior)
            compatible_control = {family, prior_family}.issubset({"guard", "manipulate", "move"})
            prior_tokens = cls._action_lane_tokens(prior)
            overlap = tokens & prior_tokens
            shared_anchors = anchors & cls._action_lane_anchors(prior)
            shared_anchor = bool(shared_anchors)
            overlap_ratio = len(overlap) / max(1, min(len(tokens), len(prior_tokens)))
            # One hero tightening the same stone brace around the same signal
            # cylinder immediately after another hero built it is not a new
            # decision.  Require both concrete anchors plus the same stabilise
            # purpose so unrelated follow-up manipulation remains legal.
            same_stabilisation = bool(
                {"signal_cylinder", "stone_barrier"}.issubset(shared_anchors)
                and family in {"guard", "manipulate"}
                and prior_family in {"guard", "manipulate"}
                and re.search(r"固定|稳住|限制|卡住|止滚|加固|压紧|不再滑动|避免移动", text)
                and re.search(r"固定|稳住|限制|卡住|止滚|加固|压紧|不再滑动|避免移动", prior)
            )
            if same_stabilisation:
                return True
            # A single near-identical action is already a duplicate even though
            # a broader action lane is only saturated after two related uses.
            if prior_family == family and len(overlap) >= 8 and overlap_ratio >= 0.44:
                return True
            # Object + method + purpose form one action lane.  Once another
            # hero has already hidden at the same doorway to watch the same
            # approach signal while protecting the same person, a paraphrase
            # is duplicate play even if one sentence is labelled guard and the
            # other investigate.
            if (
                family in low_progress_families
                and prior_family in low_progress_families
                and len(shared_anchors) >= 3
            ):
                return True
            if shared_anchor and family in low_progress_families and prior_family in low_progress_families:
                anchor_matches += 1
                if anchor_matches >= 2:
                    return True
            if prior_family != family and not compatible_control:
                continue
            if (
                len(overlap) >= 3
                and overlap_ratio >= 0.25
            ) or (compatible_control and shared_anchor):
                matches += 1
                if matches >= 2:
                    return True
        return False

    @staticmethod
    def _committed_action_before_last_pivot(text: str) -> str:
        clean = ConstrainedPlayerSimulator._strip_optional_speaker_prefix(text)
        pivots = list(re.finditer(r"转向|转而|转到|改为|随后|然后|接着|继而|转身", clean))
        if not pivots:
            return ""
        prefix = clean[: pivots[-1].start()].rstrip(" ，,。；;：:")
        clauses = [item.strip() for item in re.split(r"[，,。；;！？!?]", prefix) if item.strip()]
        committed = [
            clause
            for clause in clauses
            if re.search(r"(?:现在就|立刻|马上|当场|直接|先把|开始|陪.{0,8}(?:走|退|移)|带.{0,10}(?:到|离|进)|引.{0,10}(?:到|进|向))", clause)
            and not re.search(
                r"(?:不再|没再|没有再|不去|不继续|停止|暂不|先不).{0,14}"
                r"(?:调查|观察|检查|查看|询问|追问|触碰|碰|靠近)",
                clause,
            )
        ]
        candidate = "，".join(committed)
        # “收回盯着风铃的视线，转向监察官说话” describes an abandoned
        # focus, not a second attempt to investigate the wind chime.  Keeping
        # it here made FU-PL reject legitimate changes of conversational lane
        # merely because the opening clause mentioned a recently handled
        # object.
        candidate = re.sub(
            r"(?:收回|移开|挪开|从[^，,。；;！？!?]{0,18}(?:收回|移开))"
            r"[^，,。；;！？!?]{0,24}(?:目光|视线|注意力)",
            "",
            candidate,
        ).strip(" ，,。；;")
        return candidate if ConstrainedPlayerSimulator._action_family(candidate) else ""

    @staticmethod
    def _physical_action_before_npc_question(text: str) -> str:
        clean = ConstrainedPlayerSimulator._strip_optional_speaker_prefix(text)
        marker = re.search(
            r"(?:然后|随后|接着|继而|再)?(?:我)?"
            r"(?:转向[^，,。；;：:\n]{0,30})?"
            r"(?:直接|低声|高声|轻声)?"
            r"(?:询问|追问|问(?:他|她|对方)?|开口(?:问)?|说道|说)[：:]?",
            clean,
        )
        if marker is None:
            return ""
        prefix = clean[: marker.start()].rstrip(" ，,。；;：:")
        return prefix if ConstrainedPlayerSimulator._action_family(prefix) else ""

    @classmethod
    def _repeats_recent_npc_question(cls, text: str, recent_context: str) -> bool:
        """Reject a rephrased question while allowing a genuinely new follow-up."""

        targets, concepts = cls._npc_question_profile(text)
        if not targets or not concepts:
            return False
        if "核验范围" in concepts and cls._recent_npc_reply_already_set_scope(
            targets,
            recent_context,
        ):
            return True
        normalized = cls._normalize_for_similarity(text)
        for prior in cls._recent_player_utterances(recent_context)[-18:]:
            if not cls._looks_like_direct_npc_question(prior):
                continue
            prior_targets, prior_concepts = cls._npc_question_profile(prior)
            same_target = bool(targets.intersection(prior_targets)) or (
                "当前对话NPC" in targets and bool(prior_targets)
            ) or (
                "当前对话NPC" in prior_targets and bool(targets)
            )
            if not same_target or not prior_concepts:
                continue
            union = concepts | prior_concepts
            shared = concepts & prior_concepts
            same_topic = concepts == prior_concepts
            concept_overlap = len(shared) / max(1, len(union))
            prior_normalized = cls._normalize_for_similarity(prior)
            lexical_overlap = 0.0
            left = cls._ngrams(normalized, 3)
            right = cls._ngrams(prior_normalized, 3)
            if left and right:
                lexical_overlap = len(left & right) / max(1, min(len(left), len(right)))
            if (
                same_topic
                or "交换条件" in shared
                or concept_overlap >= 0.75
                or lexical_overlap >= 0.58
            ):
                return True
        return False

    @classmethod
    def _recent_npc_reply_already_set_scope(
        cls,
        targets: set[str],
        recent_context: str,
    ) -> bool:
        """Treat an explicit public inspection boundary as settled knowledge."""

        explicit_scope = re.compile(
            r"(?:本次|这次|只)?(?:只)?(?:验|查验|核验|检验|看|核对).{0,48}"
            r"(?:不验|不碰|不带走|不查|不看|范围)"
        )
        for speaker, content in cls._dialogue_blocks(recent_context):
            if speaker not in cls._GM_SPEAKERS or not explicit_scope.search(content):
                continue
            if "当前对话NPC" in targets:
                return True
            if any(target and target in content for target in targets):
                return True
        return False

    @staticmethod
    def _npc_question_profile(text: str) -> tuple[set[str], set[str]]:
        clean = str(text or "")
        target_patterns = {
            "旅人": r"失名旅人|失忆旅人|失去名字的旅人|旅人",
            "会长": r"守望会会长|白花守望会会长|会长",
            "守门人": r"守门人|门卫",
            "巡守": r"巡守|守卫",
            "监察官": r"监察官",
            "店主": r"店主|老板|掌柜",
            "钟匠": r"钟匠",
            "向导": r"向导",
            "使者": r"财团使者|灰金短斗篷.{0,8}使者|使者",
        }
        targets = {name for name, pattern in target_patterns.items() if re.search(pattern, clean)}
        concepts = {
            name
            for name, pattern in {
                "辨识记忆": r"记得|想起|回忆|忘记|认得|认出|辨认|分辨",
                "来路地点": r"地点|地标|哪条路|哪边来|从哪|来路|去路|路口|去哪里|来自哪里",
                "文字刻记": r"那行字|字眼|文字|刻字|刻文|写着|出现过.{0,8}字",
                "声音标记": r"声响|声音|脚步|靴步|车轮|号角|听起来|熟悉的声",
                "身份归属": r"是谁|身份|标记|纹章|徽记|属于谁|哪一方",
                "身体状态": r"疼|伤口|呼吸|发烧|意识|能不能走|撑得住",
                "通路许可": r"旧路|开门|通行|借路|放行|钥匙",
                "交换条件": (
                    r"条件|代价|担保|证据|要什么|怎样才|怎么才|什么程度|说到什么程度|"
                    r"才算|谈话.{0,6}成立|交换|交易|谈妥|接受.{0,8}(?:范围|条件|交换)|"
                    r"按什么范围|范围.{0,8}(?:谈|说|听)"
                ),
                "目标动机": r"目的|目标|动机|为什么|为何|想做什么",
                "核验范围": (
                    r"(?:核验|查验|检验|验|记账|登记).{0,40}"
                    r"(?:遗物|旧阶|旧路|人|物件|东西|门板|范围)|"
                    r"(?:只看|只验|只查|只核对).{0,48}(?:还是|或是|连)|"
                    r"(?:遗物|旧阶|旧路|人|物件|东西|门板).{0,36}"
                    r"(?:还是|或是|一并).{0,24}(?:验|查|核对|记账)"
                ),
            }.items()
            if re.search(pattern, clean)
        }
        # Direct second-person questions normally continue the current NPC
        # exchange.  This marker lets the simulator reject a paraphrased
        # inspection-scope question even when neither player repeats the
        # NPC's full name in natural group-chat prose.
        if not targets and concepts and re.search(r"(?:你|你们|贵方)", clean):
            targets.add("当前对话NPC")
        return targets, concepts

    @classmethod
    def _repeats_settled_npc_exchange(
        cls,
        text: str,
        settled_exchanges: list[dict[str, str]],
    ) -> bool:
        """Reject questions that merely reopen an already settled bargain."""

        if not settled_exchanges:
            return False
        # Asking the NPC to perform an outstanding promise is a new action,
        # not another attempt to renegotiate the terms that were just settled.
        if re.search(r"(?:兑现|按约|履约|该你|现在.{0,6}(?:开门|放行|交出|给出))", text):
            return False
        is_question = cls._looks_like_direct_npc_question(text)
        is_delivery = bool(
            re.search(
                r"(?:复述|转告|转给|告诉|交给|递给|说给|交付).{0,24}(?:使者|会长|旅人|守门人|对方)|"
                r"(?:使者|会长|旅人|守门人|对方).{0,24}(?:复述|转告|告诉|交给|递给|说出)",
                text,
            )
        )
        if not is_question and not is_delivery:
            return False
        targets, concepts = cls._npc_question_profile(text)
        if not targets or not concepts:
            return False
        for exchange in settled_exchanges:
            profile_text = " ".join(
                str(exchange.get(key) or "")
                for key in ("npc", "settled_terms")
            )
            settled_targets, settled_concepts = cls._npc_question_profile(profile_text)
            shared_concepts = concepts.intersection(settled_concepts)
            same_target = bool(targets.intersection(settled_targets))
            if is_question and same_target and shared_concepts.intersection(
                {"交换条件", "通路许可"}
            ):
                return True
            if (
                is_delivery
                and same_target
                and str(exchange.get("player_performance") or "pending") == "complete"
                and shared_concepts
            ):
                return True
        return False

    @classmethod
    def _repeats_resolved_investigation_subject(cls, text: str, recent_context: str) -> bool:
        if not cls._performs_investigation(text):
            return False
        subjects = cls._investigation_subjects(text)
        if not subjects:
            return False
        latest_gm = cls._latest_gm_reply(recent_context)
        current_aspects = cls._investigation_aspects(text)
        prior_profiles: list[tuple[set[str], set[str]]] = []
        for prior in cls._recent_player_utterances(recent_context)[-18:]:
            if cls._looks_like_uncommitted_table_talk(prior):
                continue
            if not cls._performs_investigation(prior):
                continue
            prior_profiles.append(
                (cls._investigation_subjects(prior), cls._investigation_aspects(prior))
            )
        prior_subjects = set().union(*(profile[0] for profile in prior_profiles)) if prior_profiles else set()
        if not prior_subjects:
            return False
        for prior_subject_set, prior_aspects in prior_profiles:
            if subjects.intersection(prior_subject_set) and current_aspects.intersection(prior_aspects):
                return True
        fresh = subjects - prior_subjects
        # A newly revealed concrete object is a new lane even when it lies next
        # to old evidence. This lets players follow a fragment uncovered by the
        # previous check without rechecking the powder that exposed it.
        if any(subject in latest_gm for subject in fresh if subject not in {"痕迹", "方向"}):
            return False
        shared = subjects & prior_subjects
        return bool(shared) and (
            len(shared) >= 2
            or (not fresh and len(shared) / max(1, len(subjects)) >= 0.6)
        )

    @classmethod
    def _rechecks_established_scene_fact(
        cls,
        text: str,
        established_facts: list[str] | tuple[str, ...],
    ) -> bool:
        """Reject an investigation whose requested conclusion is already public."""

        if not cls._performs_investigation(text):
            return False
        subjects = cls._investigation_subjects(text)
        aspects = cls._investigation_aspects(text)
        if not subjects or not aspects:
            return False
        for raw_fact in established_facts:
            fact = str(raw_fact or "").strip()
            if not fact:
                continue
            fact_subjects = cls._investigation_subjects(fact)
            fact_aspects = cls._investigation_aspects(fact)
            if subjects.intersection(fact_subjects) and aspects.intersection(fact_aspects):
                return True
        return False

    @staticmethod
    def _performs_investigation(text: str) -> bool:
        return bool(
            re.search(
                r"调查|检查|观察|查看|看清|看得(?:更)?清楚|仔细看|仔细摸|确认|判断|分析|比对|核对|辨认|听清|追查|"
                r"(?:低头|蹲下|凑近|直接|逐段)?看(?:漆木匣|木匣|匣盖|匣身|痕迹|油渍|油痕|刻痕|纹样|门缝|地面|脚印|车辙)|"
                r"顺着[^，,。；;！？!?\n]{0,24}(?:找|看|查|摸|追)",
                str(text or ""),
            )
        )

    @staticmethod
    def _investigation_aspects(text: str) -> set[str]:
        clean = str(text or "")
        patterns = {
            "放置方式": r"怎么被|如何被|塞进去|卡进去|卡入|嵌入|放进去|压进去|塞入方式|卡入方式",
            "表面痕迹": r"表面|上面的痕迹|折痕|压痕|刻痕|刻纹|磨痕|刮痕",
            "油性残留": r"油渍|油痕|油光|油污|油迹",
            "相互对应": r"对得上|对应|一致|吻合|比对|核对|同一种|同一来源",
            "来源归属": r"来自哪里|从哪来|来源|最初是谁|谁放|属于谁|哪一方",
            "方向路径": r"方向|哪边|通向|延伸|转向|路线|去路",
            "时间新旧": r"多久|什么时候|新鲜|新旧|刚留下|先后",
        }
        return {name for name, pattern in patterns.items() if re.search(pattern, clean)}

    @staticmethod
    def _investigation_subjects(text: str) -> set[str]:
        clean = str(text or "")
        patterns = {
            "灰晶粉末": r"灰晶粉末|灰粉|粉末",
            "蜡屑": r"白色蜡屑|白蜡屑|蜡屑",
            "铃片碎边": r"铃片碎边|铃片碎片|碎边",
            "木梁缝": r"木梁缝|梁缝|木缝",
            "风铃": r"白花风铃|风铃|铃身|铃舌",
            "遗物": r"碎月遗物|遗物",
            "漆木匣": r"漆木匣|木匣|匣盖|匣身",
            "油渍": r"油渍|油痕|油光|油污|油迹",
            "痕迹": r"拖痕|擦痕|痕迹",
            "车辙": r"车辙|轮痕",
            "脚印": r"脚印|足迹",
            "刻痕": r"刻痕|刻纹",
            "接缝": r"补平接缝|接缝|缝隙",
            "灰屑": r"灰屑|细灰|灰膜",
            "水线": r"水线|湿痕",
            "纤维": r"纤维碎末|纤维|白线|线头",
            "账册": r"账册|账页|登记簿",
            "封蜡": r"封蜡|蜡封",
            "门闩": r"门闩|门轴|门缝",
            "灰晶薄片": r"灰晶薄片|灰晶片",
        }
        return {name for name, pattern in patterns.items() if re.search(pattern, clean)}

    @classmethod
    def _repeats_same_actor_target_action(
        cls,
        text: str,
        recent_context: str,
        *,
        actor: str,
    ) -> bool:
        """Reject one already-resolved noncombat probe by the same test PC."""

        family = cls._action_family(text)
        if family not in {"investigate", "manipulate", "care", "guard"}:
            return False
        if re.search(r"重掷|援用.{0,12}(?:特质|羁绊)|再试一次|重新检定", str(text or "")):
            return False
        latest_gm = cls._latest_gm_reply(recent_context)
        if re.search(r"再试一次|可以重试|重新检查|重新调查|再检定一次", latest_gm):
            return False
        anchors = cls._action_lane_anchors(text)
        tokens = cls._action_lane_tokens(text)
        if not anchors and len(tokens) < 4:
            return False
        for prior in cls._recent_player_utterances(recent_context)[-32:]:
            if actor and actor not in prior:
                continue
            if cls._action_family(prior) != family:
                continue
            prior_anchors = cls._action_lane_anchors(prior)
            if anchors and anchors & prior_anchors:
                return True
            prior_tokens = cls._action_lane_tokens(prior)
            overlap = tokens & prior_tokens
            if len(overlap) >= 4 and len(overlap) / max(1, min(len(tokens), len(prior_tokens))) >= 0.34:
                return True
        return False

    @staticmethod
    def _action_lane_anchors(text: str) -> set[str]:
        focus = ConstrainedPlayerSimulator._action_focus_text(text)
        groups = {
            "door": r"门|门缝|门板|入口|闸|通道",
            "road": r"路|旧阶|廊阶|阶梯|车辙|脚印|追踪",
            "evidence": r"证据|纸条|黑纸|粉末|粉屑|灰粉|白漆|账册|刻痕",
            "wind_chime": r"风铃|小铃|铃身|铃舌",
            "traveler": r"旅人|伤者|病人",
            "patrol": r"巡逻|追兵|财团|敌人",
            "approach_signal": r"号角|尘雾|冷白灯火|灯火|脚步|铁靴声|火光|金属回响|车轮声",
            "concealment": r"遮挡|隐蔽|藏身|压低(?:身形|视线)|檐柱|门框|麻袋|木箱",
            "ritual": r"仪式|法阵|魔力|奥灵",
            "counter": r"柜台|收据|封蜡|运单",
            "container": r"漆木匣|木匣|匣盖|匣身",
            "oil_trace": r"油渍|油痕|油光|油污|油迹",
            "signal_cylinder": r"铜筒|灰晶筒|筒盖|灰晶片",
            "stone_barrier": r"碎石|矮坎|石坎|止滚坎",
        }
        return {name for name, pattern in groups.items() if re.search(pattern, focus)}

    @staticmethod
    def _strip_optional_speaker_prefix(text: str) -> str:
        raw = str(text or "")
        match = re.match(r"^\s*([^:\n：]{1,16})\s*[:：]\s*", raw)
        if match is None:
            return raw
        label = str(match.group(1) or "").strip()
        # A transcript label is a compact nickname, not a whole narrative
        # clause.  In-fiction punctuation such as “观察旧路闸门：看看门闩”
        # must remain part of the action.
        if not label or re.search(r"[，,。；;！？!?“”\"'（）()]", label):
            return raw
        if re.search(
            r"转向|看向|走向|靠近|询问|追问|问|说|回应|答复|告诉|转告|讲给|说给|检查|观察|调查|查看|确认|"
            r"递出|交出|打开|攻击|施放|防御|推进",
            label,
        ):
            return raw
        return raw[match.end() :]

    @staticmethod
    def _action_focus_text(text: str) -> str:
        """Return the clause that carries the current action's real focus.

        Synthetic players frequently preserve continuity by naming the thing
        they are *stopping* before turning to a fresh target.  Those abandoned
        objects must not count as active lane anchors, or every action in one
        crowded scene appears to repeat the same broad ``door``/``traveler``
        lane.
        """

        clean = ConstrainedPlayerSimulator._strip_optional_speaker_prefix(text)
        pivots = list(re.finditer(r"转向|转而|转到|改为|随后|然后|接着|继而|转身", clean))
        if pivots:
            clean = clean[pivots[-1].start() :]
        clean = re.sub(
            r"(?:不再|没再|没有再|不打算再|不准备再)"
            r"[^，,。；;！？!?\n]{0,20}(?:开口|说话|追问|询问)"
            r"[，,。；;！？!?]?",
            "",
            clean,
        )
        clean = re.sub(
            r"(?:先不|暂不|不再|没有再|没再|不继续|不去|停止|避免|别再|不)"
            r"(?:再)?(?:靠近|触碰|碰|检查|观察|调查|查看|询问|追问|盯住)"
            r"[^，,。；;！？!?\n]{0,32}",
            "",
            clean,
        )
        return clean

    @classmethod
    def _ignores_explicit_gm_affordance(cls, text: str, recent_context: str) -> bool:
        latest_gm = cls._latest_gm_reply(recent_context)
        if not latest_gm:
            return False
        if not cls._has_explicit_gm_affordance(latest_gm):
            return False
        return not cls._responds_to_explicit_gm_affordance(text, recent_context)

    @staticmethod
    def _has_explicit_gm_affordance(latest_gm: str) -> bool:
        return bool(
            re.search(
                r"跟我来|随我来|带你们(?:去|进)|门(?:已经|现在)?(?:开了|打开)|"
                r"通道(?:已经|现在)?(?:开了|打开)|可以进去|现在进去|选一个|二选一|要么.{1,50}要么|"
                r"是[^。！？\n]{1,50}还是[^。！？\n]{1,50}|"
                r"(?:愿意|可以|能)(?:先|只|仅|最多)?(?:说|讲|念|公开|透露).{0,40}(?:部分|一段|方向|内容)|"
                r"最后(?:一次)?(?:警告|机会)|现在(?:交出|开门|离开|投降)|必须(?:交出|选择|决定)|"
                r"(?:开门|交出|投降|离开).{0,12}(?:否则|不然)|"
                r"(?:现在)?只要[^。！？\n]{1,80}(?:否则|不然)|"
                r"(?:做到|办到|完成).{0,72}(?:我.{0,16})?(?:放行|开门|带路|指引|告诉)",
                str(latest_gm or ""),
            )
            or ConstrainedPlayerSimulator._concrete_party_directive(latest_gm) is not None
        )

    @classmethod
    def _responds_to_affordance_text(cls, text: str, latest_gm: str = "") -> bool:
        if cls._responds_to_condition_fulfillment_offer(text, latest_gm):
            return True
        return bool(
            re.search(
                r"跟上|随(?:他|她|守门人|会长)|进去|进入|穿过|走进|接受|答应|照办|应下|接下|"
                r"选择|选定|选了|选(?:第?[一二两三四五六七八九十\d]+)(?:条|项)?|"
                r"拒绝|不去|留下|让.{0,12}先走|请.{0,12}带路|开门|不开|交出|不交|投降|迎战|撤离|决定|"
                r"请.{0,16}(?:说|讲|念|公开|透露)|把.{0,20}(?:说|讲|念)(?:完|出来)|"
                r"承担|担下|担起|担保|承诺|宣誓|立誓|见证|拿出|递上|提供.{0,12}(?:材料|证明)|"
                r"挪开|移开|带离|带往|带到|送到|扶到|安置|指出|指给|标出|指明",
                str(text or ""),
            )
        )

    @staticmethod
    def _is_condition_fulfillment_offer(latest_gm: str) -> bool:
        return bool(
            re.search(
                r"(?:做到|办到|完成).{0,72}(?:我.{0,16})?(?:放行|开门|带路|指引|告诉)|"
                r"(?:一条路线|眼前一段|这段路).{0,96}(?:要满足|需要满足|做到这些)",
                str(latest_gm or ""),
            )
        )

    @classmethod
    def _responds_to_condition_fulfillment_offer(cls, text: str, latest_gm: str) -> bool:
        """Recognize a player beginning a concrete, GM-requested safety check.

        An NPC may offer a route on the condition that the party inspect the
        immediately visible first stretch, keep a vulnerable traveller in
        sight, or state a retreat plan.  That is an invitation to *perform* a
        condition, not a demand that the player first teleport into the route
        or repeat the wording of the offer verbatim.
        """

        offer = str(latest_gm or "")
        candidate = str(text or "")
        if not offer or not candidate or not cls._is_condition_fulfillment_offer(offer):
            return False

        investigate = bool(
            re.search(r"查明|查看|检查|观察|确认|辨认|寻找|听清|看清|判断", candidate)
        )
        route_markers = ("旧路", "起段", "门槛", "闸门", "入口", "拦截", "伏击", "遮挡")
        asks_for_route_check = any(marker in offer for marker in route_markers) or "眼前一段" in offer
        performs_route_check = investigate and (
            any(marker in candidate for marker in route_markers)
            or ("眼前一段" in offer and any(marker in candidate for marker in ("脚印", "异常", "动静", "痕迹")))
        )
        if asks_for_route_check and performs_route_check:
            return True

        asks_for_traveller_safety = "旅人" in offer and bool(
            re.search(r"视线|看得见|看见|同伴之间|队伍中央|护住|留在", offer)
        )
        performs_traveller_safety = "旅人" in candidate and bool(
            re.search(r"视线|看得见|看见|同伴之间|队伍中央|护住|留在", candidate)
        )
        if asks_for_traveller_safety and performs_traveller_safety:
            return True

        asks_for_retreat = bool(re.search(r"退回|撤回|撤离", offer))
        performs_retreat = bool(re.search(r"退回|撤回|撤离", candidate))
        return asks_for_retreat and performs_retreat

    @staticmethod
    def _concrete_party_directive(latest_gm: str) -> tuple[str, str, str] | None:
        """Extract a narrow, immediate player-executable directive for FU-PL.

        This belongs solely to the test simulator. It keeps a simulated player
        from ignoring a fresh "move this person / point out this clue" request
        in favour of generic roleplay, without imposing an imperative on real
        player messages.
        """

        text = str(latest_gm or "")
        if not re.search(
            r"(?:做到了|办到|完成).{0,32}(?:我(?:现在|就)?(?:告诉|说明|带|给)|就(?:告诉|说明|带|给))",
            text,
        ):
            return None
        match = re.search(
            r"(?:把|将)(?P<object>[\u4e00-\u9fffA-Za-z0-9·]{2,20})"
            r"(?:往|到)(?P<destination>[\u4e00-\u9fffA-Za-z0-9·]{1,18})"
            r"(?P<verb>挪开|移开|带离|带往|带到|送到|扶到|安置)",
            text,
        )
        if match is None:
            return None
        target = str(match.group("object") or "").strip()
        destination = str(match.group("destination") or "").strip()
        verb = str(match.group("verb") or "").strip()
        if not target or not destination or not verb:
            return None
        return target, destination, verb

    @classmethod
    def _responds_to_explicit_gm_affordance(cls, text: str, recent_context: str) -> bool:
        latest_gm = cls._latest_gm_reply(recent_context)
        return cls._has_explicit_gm_affordance(latest_gm) and cls._responds_to_affordance_text(text, latest_gm)

    @classmethod
    def _crosses_unopened_route(
        cls,
        text: str,
        recent_context: str,
        *,
        blocked_routes: list[str] | tuple[str, ...] = (),
    ) -> bool:
        candidate = str(text or "")
        crosses = bool(
            re.search(
                r"(?:沿|顺着).{0,12}(?:旧路|侧路|通道|出口|入口).{0,12}(?:走到|走进|进入|穿过|越过)|"
                r"(?:进入|走进|穿过|越过).{0,12}(?:旧路|侧路|通道|出口|入口)|"
                r"(?:跟上|随).{0,16}(?:穿过|进入).{0,10}(?:门|入口|通道)",
                candidate,
            )
        )
        if not crosses:
            return False
        explicit_block = next(
            (route for route in blocked_routes if str(route or "").strip() and str(route) in candidate),
            "",
        )
        if explicit_block:
            return True
        context = str(recent_context or "")[-3600:]
        closed_matches = list(
            re.finditer(
                r"没(?:有)?(?:放行|开门|让开)|尚未(?:放行|开门)|还没(?:放行|开门)|"
                r"不肯放行|拒绝放行|按住门闩|没有让开|(?:条件|要求).{0,30}(?:才|之后).{0,10}(?:开门|放行)|"
                r"先.{1,40}(?:我就|才会|才能)(?:开门|放行)",
                context,
            )
        )
        if not closed_matches:
            return False
        opened_matches = list(
            re.finditer(
                r"(?:已经|现在|终于|同意|答应)(?:开门|放行|让开)|门(?:已经|现在|终于)打开|"
                r"入口(?:已经|现在|终于)?开放|可以(?:进去|通过|上路)|跟我来|随我来",
                context,
            )
        )
        last_closed = closed_matches[-1].start()
        last_opened = opened_matches[-1].start() if opened_matches else -1
        return last_closed > last_opened

    @classmethod
    def _leaves_current_scene_without_transition(
        cls,
        text: str,
        *,
        legal_context: LegalActionContext,
        recent_public_context: str,
    ) -> bool:
        """Keep synthetic PCs inside the current camera unless the GM opens a move."""

        if not (legal_context.scene_name or legal_context.scene_location):
            return False
        source = cls._strip_optional_speaker_prefix(text)
        if cls._is_adjacent_threshold_observation(source):
            return False
        destinations: list[str] = []
        patterns = (
            r"(?:沿|顺着)(?P<place>[^，,。；;！？!?\n]{1,24}?)(?:走|前进|往前|移动)",
            r"(?:走到|走向|前往|去往|来到|进入|回到|转去|赶到|抵达)"
            r"(?P<place>[^，,。；;！？!?\n]{1,24})",
        )
        for pattern in patterns:
            destinations.extend(
                str(match.group("place") or "").strip()
                for match in re.finditer(pattern, source)
                if str(match.group("place") or "").strip()
            )
        if not destinations:
            return False

        latest_gm = cls._latest_gm_reply(recent_public_context)
        allowed = "；".join(
            str(item or "").strip()
            for item in (
                legal_context.scene_name,
                legal_context.scene_location,
                *legal_context.visible_scene_elements,
                latest_gm,
            )
            if str(item or "").strip()
        )
        local_positions = re.compile(
            r"^(?:门口|入口|出口|桌边|桌旁|柜台|墙边|墙后|身边|旁边|廊下|廊柱|"
            r"角落|内侧|外侧|眼前|原地|同伴身边|旅人身边|闸门旁|闸门外侧|小室门口)$"
        )
        place_like = re.compile(
            r"(?:海岸|海边|森林|林地|山脉|山谷|城镇|城市|村庄|公国|王国|帝国|"
            r"驿站|遗迹|塔|港|岛|营地|广场|街|路|廊|小室|房间|闸门|入口|出口|门口)"
        )
        allowed_pairs = cls._han_bigrams(allowed)
        for destination in destinations:
            clean = re.sub(r"^(?:那条|这条|那个|这个|一处|前方的|外面的)", "", destination).strip()
            clean = re.sub(r"(?:那里|那边|附近|方向|转折处|尽头)$", "", clean).strip()
            if not clean or local_positions.fullmatch(clean):
                continue
            if clean in allowed or any(
                token and (token in clean or clean in token)
                for token in (legal_context.scene_name, legal_context.scene_location)
            ):
                continue
            pairs = cls._han_bigrams(clean)
            if pairs and len(pairs & allowed_pairs) >= min(2, len(pairs)):
                continue
            if place_like.search(clean):
                return True
        return False

    @staticmethod
    def _is_adjacent_threshold_observation(text: str) -> bool:
        """Allow looking through a closed threshold without treating it as travel.

        This is deliberately narrower than a generic route exception.  It
        requires an observation action, an explicit statement that the PC
        stays on the current side (or stands before an unopened threshold),
        and no affirmative crossing into the route.
        """

        source = str(text or "")
        observe = bool(re.search(r"查看|检查|观察|调查|确认|辨认|寻找|听清|看清", source))
        visible_scope = bool(
            re.search(r"可见范围|能看见|看得见|门槛|闸门|入口|门缝|这一侧|内侧|门外", source)
        )
        stays_local = bool(
            re.search(
                r"(?:不|未|没有|别|不要|不能).{0,6}(?:越过|穿过|进入|走进).{0,14}"
                r"(?:门槛|闸门|入口|旧路|通道)|"
                r"(?:留在|停在|站在).{0,20}(?:这一侧|门内|内侧|门槛边|闸门前|入口外)|"
                r"(?:尚未开启|未开放|未放行).{0,20}(?:闸门|入口|门).{0,24}(?:前|边|内侧)",
                source,
            )
        )
        if not (observe and visible_scope and stays_local):
            return False
        for match in re.finditer(r"越过|穿过|进入|走进", source):
            prefix = source[max(0, match.start() - 6) : match.start()]
            if not re.search(r"(?:不|未|没有|别|不要|不能|尚未)\s*$", prefix):
                return False
        return True

    @staticmethod
    def _han_bigrams(text: str) -> set[str]:
        clean = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(text or ""))
        return {clean[index : index + 2] for index in range(max(0, len(clean) - 1))}

    @classmethod
    def _dialogue_blocks(cls, recent_context: str) -> list[tuple[str, str]]:
        """Parse public chat without mistaking multiline GM prose for player turns."""

        blocks: list[tuple[str, str]] = []
        speaker = ""
        content: list[str] = []

        def flush() -> None:
            nonlocal content
            rendered = "\n".join(part for part in content if part).strip()
            if speaker and rendered:
                blocks.append((speaker, rendered))
            content = []

        for raw_line in str(recent_context or "").splitlines():
            line = raw_line.strip()
            match = re.match(r"^([^：:\n]{1,20})[：:]\s*(.*)$", line)
            label = str(match.group(1) if match else "").strip()
            is_known_header = bool(
                match
                and (
                    label in cls._GM_SPEAKERS
                    or label in cls._KNOWN_PLAYER_SPEAKERS
                    or re.fullmatch(r"玩家[\w\u4e00-\u9fff·-]{0,12}", label)
                )
            )
            if is_known_header:
                flush()
                speaker = label
                content = [str(match.group(2) or "").strip()]
            elif speaker:
                content.append(line)
            elif line:
                # Direct unit-level callers sometimes provide one unlabelled
                # player utterance rather than a transcript block.
                speaker = "玩家"
                content = [line]
        flush()
        return blocks

    @classmethod
    def _recent_player_utterances(cls, recent_context: str) -> list[str]:
        return [
            content
            for speaker, content in cls._dialogue_blocks(recent_context)
            if speaker not in cls._GM_SPEAKERS
        ]

    @classmethod
    def _latest_gm_reply(cls, recent_context: str) -> str:
        for speaker, content in reversed(cls._dialogue_blocks(recent_context)):
            if speaker in cls._GM_SPEAKERS:
                return content
        return ""

    @staticmethod
    def _action_family(text: str) -> str:
        focus = ConstrainedPlayerSimulator._action_focus_text(text)
        groups = (
            ("dialogue", r"询问|追问|问道|问清|问一句|想问|请问|告诉我|告诉我们|开口|说服|交涉|安抚|威胁"),
            ("investigate", r"观察|调查|检查|查看|去看|看清|看得|仔细摸|听清|听辨|留意|盯|辨认|分辨|比对|核验|校验|对照|巡夜|侦察|顺着.{0,24}(?:找|看|查|摸|追)|沿着.{0,24}(?:找|看|查|摸|追)"),
            ("care", r"扶住|包扎|照看|检查呼吸|安置|治疗"),
            ("guard", r"防御|掩护|挡住|抵住|顶住|贴住|护住|警戒|守住|守在|守着|看守"),
            ("magic", r"施放|法术|仪式|魔法"),
            ("attack", r"攻击|射击|劈|刺|砍|轰击"),
            ("manipulate", r"推开|推拢|拉开|按住|按在|压住|压稳|压紧|稳住|固定|加固|限制|止住|止滚|垒出|垒|打开|修理|拆除|递出|交出|放入|装入|操作|核验|对齐|摆好|摆到|收拢|翻到|拼成|卡住|卡进|收稳|收起|包好|藏进"),
            ("move", r"走向|走到|跑向|进入|离开|绕到|靠近|退到|移到|带到|带离|带进|引到|引进|引向|领到|领进|领向"),
        )
        return next((name for name, pattern in groups if re.search(pattern, focus)), "")

    @staticmethod
    def _looks_like_uncommitted_table_talk(text: str) -> bool:
        clean = str(text or "")
        discussion = bool(
            re.search(r"我觉得|我倾向|我建议|咱们|大家|谁来|谁方便|要不要|最好|先商量|先分工", clean)
        )
        commitment = bool(
            re.search(
                r"(?:^|[，,。；;])(?:我|[\u4e00-\u9fffA-Za-z·]{2,10})(?:先|来|现在|立刻|马上|继续|去)"
                r".{0,20}(?:调查|观察|检查|查看|询问|追问|安抚|攻击|防御|推进|带走|打开|交出)",
                clean,
            )
        )
        return discussion and not commitment

    @staticmethod
    def _action_lane_tokens(text: str) -> set[str]:
        clean = re.sub(
            r"阿凛|南星|白河|时雨|澄砚|伊莉雅|赛璃|洛岚|艾薇娅|苍祈|"
            r"观察|调查|检查|查看|确认|想要|试图|继续|先|立刻|马上|现在|自己|大家|队友|"
            r"[^一-鿿A-Za-z0-9]",
            "",
            str(text or ""),
        )
        stop = {"角色", "玩家", "现场", "周围", "眼前", "一下", "情况", "变化", "能否", "是否"}
        return {
            clean[index : index + 2]
            for index in range(max(0, len(clean) - 1))
            if clean[index : index + 2] not in stop
        }

    @classmethod
    def _near_duplicate_player_utterance(cls, text: str, recent_context: str) -> bool:
        candidate = cls._normalize_for_similarity(text)
        if len(candidate) < 14:
            return False
        for utterance in cls._recent_player_utterances(recent_context)[-16:]:
            prior = cls._normalize_for_similarity(utterance)
            if len(prior) < 14:
                continue
            left = cls._ngrams(candidate, 3)
            right = cls._ngrams(prior, 3)
            if SequenceMatcher(None, candidate, prior).ratio() >= 0.88:
                return True
            if left and right and len(left & right) / len(left | right) >= 0.68:
                return True
        return False

    @staticmethod
    def _normalize_for_similarity(text: str) -> str:
        return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(text or "")).lower()

    @staticmethod
    def _ngrams(text: str, size: int) -> set[str]:
        if len(text) <= size:
            return {text} if text else set()
        return {text[index : index + size] for index in range(len(text) - size + 1)}

    def _declares_action_in_table_discussion(self, text: str) -> bool:
        """Keep silence probes as actual table talk instead of disguised turns."""

        if self._contains_world_facing_action(text):
            return True
        if bool(
            re.search(
                r"(?:^|[，,。；;])(?:我[^，,。；;！？!?]{0,24})?"
                r"(?:想|打算|准备)(?:先|直接|马上|现在)?(?:去|来)?"
                r"(?:看(?:看|一眼)?|调查|观察|检查|查看|确认|追问|询问|安抚|保护|挡|掩护|"
                r"施放|推进|打开|触碰|拿起|放下|走到|靠近)",
                text,
            )
            or
            re.search(
                r"(?:^|[，,。；;])(?:那)?我(?:先|来|继续|跟着|转向|补位)"
                r"[^，,。；;！？!?]{0,30}"
                r"(?:调查|观察|检查|盯|看|追问|询问|安抚|保护|挡|掩护|施放|法术|屏障|推进|留住|按住|往下查|补个)",
                text,
            )
            or re.search(
                r"(?:^|[，,。；;])(?:那就|那我们(?:就)?|我们就|就)?先?"
                r"(?:把|将|让)[^，,。；;！？!?]{1,36}"
                r"(?:交给|递给|带给|放到|拿给|送到|打开|推进|交出去|带走|带离|挪开|移开|安置)",
                text,
            )
        ):
            return True

        # Collective imperatives still execute the party's movement even when
        # no individual hero is named. Genuine proposals use an explicit
        # question or uncertainty marker instead.
        for clause in re.split(r"[，,。；;！？!?\n]+", str(text or "")):
            clean = clause.strip()
            if not clean:
                continue
            if re.search(r"要不要|是否|能不能|可不可以|大家觉得|你们觉得|我(?:倾向|建议|觉得|拿不准)|不如", clean):
                continue
            if re.search(
                r"^(?:那就|那我们(?:就)?|我们(?:就)?|咱们(?:就)?)"
                r".{0,10}(?:先|现在|继续|马上|别|不要).{0,28}"
                r"(?:走|跟上|进去|进入|通过|过门|卡在|停在|留在|撤|离开|带走|带上|挪开|移开|交出|递出|"
                r"打开|推进|调查|检查|观察|追问|安抚|保护|掩护|施放|攻击)",
                clean,
            ):
                return True
            if re.search(
                r"^(?:那就|那我们(?:就)?|我们(?:就)?|咱们(?:就)?)"
                r"(?!.{0,18}(?:要不要|是否|能不能|可不可以|谁来|谁去|谁愿意|谁方便))"
                r".{0,24}(?:走|跟上|进去|进入|通过|过门|撤|离开|带走|带上|挪开|移开|"
                r"交出|递出|打开|推进|调查|检查|观察|追问|安抚|保护|掩护|施放|攻击)",
                clean,
            ):
                return True
            if re.search(
                r"^(?:那就|那我们(?:就)?|我们(?:就)?|咱们(?:就)?).{0,12}一边.{0,28}"
                r"(?:走|跟上|进去|进入|通过|撤|离开|带走|盯|观察|检查|保护|掩护)",
                clean,
            ):
                return True
        return False

    def _has_complete_character_hint(self, step: ReplayStep) -> bool:
        if not step.kind.startswith("session_zero"):
            return False
        intent = str(step.intent or step.stage_goal or "")
        hint = str(step.method_hint or step.message or "")
        if "角色" not in intent and "character" not in intent.lower():
            return False
        return "确认创建" in hint or ("职业" in hint and "属性" in hint and "技能" in hint)

    def _is_confirmation_step(self, step: ReplayStep) -> bool:
        if not step.kind.startswith("session_zero"):
            return False
        text = " ".join(str(value or "") for value in (step.intent, step.stage_goal, step.method_hint, step.message))
        return any(token in text for token in ("confirm", "确认", "同意", "赞成", "就按这个", "写入", "落档"))

    def _system_prompt(self) -> str:
        return (
            "你是《最终物语》回放测试中的玩家模拟器，只写一条玩家发言。"
            "你不是 GM，不描述行动结果，不编骰子，不输出 JSON 或测试说明。"
            "提示中的‘指定玩家’只是桌外发言账号，‘指定角色’才是世界中的人物；"
            "冒险场景里绝不能把玩家名当作角色名、NPC名、法术目标或被保护对象。"
            "不要宣布场景开场、章节收束、NPC 已经存在或线索答案；这些由 GM 决定。"
            "你只能声明角色意图、询问、回应 GM 的问题，或在明确消耗物语点时提出新事实。"
            "职业技能只能产生合法行动上下文列出的规则效果；不要从技能名称自行发明扫描、追踪、破译或其他功能。"
            "角色用随身工具辅助普通调查时，只把工具写成叙事手段，不要声称发动同名职业技能。"
            "优先接上一条 GM 回复里的场景、NPC、压力或问题，不要把剧情跳到无关方向。"
            "一旦任务是冒险行动槽，就不能讨论开团基调、第零章、角色创建或‘推进剧情’；始终留在角色此刻看见的场景里。"
            "如果当前玩家有待决窗口，先用合法选项明确回答；窗口不属于你时不要代答，但你仍可进行自己的独立行动。"
            "把最近公开对话中 NPC 已经给出的直接答复视为已确定信息；除非 NPC 明确拒答、闪避，且你更换了筹码或问题实质，"
            "否则不要换词重复询问同一件事。连续两次围绕同一 NPC 和同一问题交涉后，应落实其答复、转向别的可见要素或与同伴商量。"
            "当本条任务明确说某种行动通道已连续使用时，必须换用另一种真实手段，不能只把同一问题改个说法。"
            "如果你问过的问题没有得到GM可见回应，不要让其他玩家轮流换词复读；可以明确提醒GM刚才的问题尚未回答，或改做另一件合理行动。"
            "NPC若刚向你提出一个明确问题，应直接回答、明确拒绝或用角色立场说明为什么不能回答；不要只把同一个问题原样反问回去。"
            "尤其是NPC刚要求说明姓名、与某人的关系或是否代为答话时，必须优先正面回应；"
            "这比处理命刻压力或重复先前动作更优先。"
            "NPC若刚刚开放通路、邀请你跟上或给出明确选择，应先决定接受、拒绝或选哪一项，并让角色据此行动；"
            "不要继续重复通路开放前已经做完的守门、核验或准备。"
            "GM刚用祈使句要求队伍把某个在场人物或物件移到具体位置、指出具体线索或完成可见动作时，"
            "下一行动槽应先落实其中一件，不要另起寒暄或重复已经完成的交涉。"
            "把‘已经发生且不可倒退的现场事实’视为现实：威胁已经抵达就不能继续准备它抵达前的动作，命刻已经完成就不能假装仍可阻止它。"
            "不要把命刻当成角色知道的按钮，也不要说‘推进命刻’‘压制命刻’；应描述角色在世界里具体如何拖延威胁、完成目标或改变环境。"
            "声明行动时只说角色现在做什么以及想达到什么；不要追加‘如果需要检定’、‘请GM指定属性’或替GM安排裁定流程。"
            "每个行动槽只提交一个会改变局面的焦点行动。可以写站位、掩护或工具作为这个行动的方法，但不要把需要NPC另行决定的搀扶、"
            "阻拦或交付和另一项调查、攻击、施法并列在同一句里；必须先选当前真正要结算的一件事。"
            "行动槽里不要用‘我想/我打算’把当前行动写成尚未执行的计划；直接写角色现在去看、调查、移动或交涉。"
            "不要用‘不再盯/不再碰某个对象’当作新行动的主干；直接写角色此刻实际采取的动作，避免把已放弃的对象误当成调查目标。"
            "也不要在同一条行动后追加‘如果……我再做另一件事’的备用动作。"
            "不要要求NPC重复已经完整公开过的路线、名字或事实；直接使用那条信息。"
            "玩家可以在桌边简短整理线索，但冒险行动槽不能把复述、总结、记笔记或转告整组已知线索当成角色行动；"
            "只引用本次动作必需的一小段信息，然后执行会引发新世界回应或规则裁定的具体行动。"
            "调查刚揭示的纸角、划痕、粉末、锁槽或脚印不会自动成为新的调查路线；同一对象已经有一次深入追查后，"
            "除非GM明确说明新细节会阻挡眼前选择或构成必须处理的危险，否则下一位角色应利用结论、回应NPC、移动或处理压力。"
            "如果同一名NPC已经明确表示可以、接受、收下或按刚才的信息处理，就视为该次交付已经完成；"
            "即使后续一句话又要求重复，也不要原样再交付，可以指出刚才已经说过或改做新的行动。"
            "合法行动上下文中列出的‘NPC已经结清的交涉’同样已经结束：不要再次确认其成立条件，"
            "不要让其他玩家轮流复述同一交付；只有要求兑现尚未发生的承诺或提出实质不同的新议题才是新行动。"
            "也不要先做一件准备，再说‘然后我会’完成真正关键的行动；决定接受或拒绝交易时必须在本条当场落实。"
            "行动槽必须由指定角色本人采取行动；请求、命令或等待另一名玩家角色代为签字、开门、调查或处理局面，只算桌边商量，不算行动。"
            "同样不能替其他玩家角色宣布移动、同意或完成动作；可以招呼他们跟上，但除非最近公开对话里每位相关玩家都已明确同意，"
            "否则不要把单个角色的决定写成整个队伍已经行动。"
            "角色也不能替NPC、势力或机关兑现承诺、开放道路、放行、交出钥匙或透露情报；"
            "接受NPC条件时只声明角色正在履行的动作，NPC是否兑现由GM决定。"
            "若合法行动上下文写着‘NPC已经公开、尚未满足的有限条件’，其承诺结果尚未发生；"
            "即使只是和队友商量，也不能说NPC已经放行、开门、交出或告诉了什么。此时只能承认条件尚未满足，"
            "讨论是否接受，或在行动槽中履行条件。"
            "NPC说‘我现在决定是否……’也仍是待决状态，不等于已经允许或拒绝；"
            "必须等GM公开明确的允许、拒绝或具体后果，不能自行把这句话补成结果。"
            "场内NPC只能从当前公开人物和最近对话中选择；时悠是GM人格，不是世界里的NPC。"
            "不要把其他玩家说的疑问、建议或分工句截成场景实体；‘谁有把握’‘谁来处理’‘要不要’都不是人物或物件，"
            "即使它们出现在最近聊天或【】里也不能作为行动目标。"
            "如果NPC要求担保物、证据或交换物，却没有明确说出具体对象，先直接问清楚；"
            "不能假装拿出‘那件东西’‘合适的担保物’或其他尚未在公开对话中存在的物件。"
            "NPC已经公开了有限条件时，把它视为现场真实可选方向：若角色愿意接受，就逐项执行；若不愿接受，就明确拒绝并采取另一项具体行动。"
            "如果NPC已经拒绝拖延或拒绝替代条件，下一条必须在接受并履行、拒绝并承担后果、或立即对抗这三类方向中选一类，不能再拖一轮。"
            "不要无视这项条件后反复检查同一个物件。"
            "当NPC或GM已经给出足以执行的路线、分工、触发信号和下一步时，直接执行或明确拒绝；"
            "不要继续追问不会改变路线、风险、职责或行动方式的口令、站位和交接细枝末节。"
            "不要仅因为某个法术或技能在角色卡上合法就消耗有限资源。治疗要有受伤目标，抗性要有已公开或可信预警的对应危险，"
            "攻击与强化也要服务于眼前敌人或明确战术；公开局面没有因果联系时，改做与现场有关的行动。"
            "你只能知道GM已经公开的内容、自己的角色卡和其他玩家公开说过的话；不能看到测试大纲、暗线答案或预定解法。"
            "如果公开资料没有说明某个角色的性别，就直接重复角色名，不要擅自用‘他’或‘她’指代。"
            "合法行动上下文中的当前场景与地点就是角色此刻所在位置；除非GM最新回复明确切换镜头、带路或开放去处，"
            "不得沿旧聊天里出现过的海岸、森林、街道或其他地点自行离场。"
            "合法行动上下文中的剧情物件状态是权威事实：物件由谁持有，就只能由谁拿取、放置、嵌入、交付、使用或消耗；"
            "其他角色必须先请求并等到转交实际发生。角色也不能在尚未抵达的地点操作门、机关或物件。"
            "不要把移动和抵达后的第二个实质动作压在同一句里；先完成转场，等GM确认位置后再操作。"
            "GM一旦说‘队伍’‘众人’或‘一行人’已经进入、抵达或离开，就表示所有当前参演玩家角色都随队完成了转场；"
            "除非GM明确说你的角色留在后方，否则不要再让下一名角色跟上、踏入或抵达同一个地方，应直接处理新地点里的局面。"
            "‘已经发生且不可倒退的现场事实’是结论，不是待验证假说；不要再观察或比对来证明同一结论。"
            "如果 GM 已说某次机会‘已记录’或‘已用于某效果’，该机会已经消费，之后不能再提议保留、改用或延后使用。"
            "像真人玩家一样逐步补充，不要为了完成测试一次性填完整张角色卡；"
            "可以误解线索、犹豫、开一句不打断气氛的玩笑、问同伴意见或改变计划，但不能声明不存在的技能、职业或法术。"
        )

    def _build_prompt(
        self,
        step: ReplayStep,
        legal_context: LegalActionContext,
        last_gm_reply: str,
        *,
        recent_public_context: str = "",
    ) -> str:
        legal_block = self.legal_action_layer.as_prompt_block(legal_context)
        glossary_block = self.glossary.render_for_player_prompt(legal_actions=legal_context.legal_action_names())
        return "\n\n".join(
            [
                glossary_block,
                legal_block,
                f"指定玩家：{step.speaker or '未指定'}",
                f"指定角色：{step.actor or '未指定'}",
                f"本条发言任务：{step.stage_goal or '回应当前公开局面。'}",
                "最近公开对话（越靠后越新）：\n"
                + (recent_public_context[-5000:] if recent_public_context else "无"),
                "上一条 GM 回复节选：\n" + (last_gm_reply[-1200:] if last_gm_reply else "无"),
                "只根据以上公开内容选择一个你真正在意的反应。可以行动、提问、和队友商量或暂时保留意见；"
                "不要为了命中测试目标而猜测未公开内容。若队伍已经抵达某处，就不要再次声明抵达；"
                "若眼前路线受阻，应与当前可见的门、入口、机关或在场人物互动，而不是继续移动到同一地点或原地等候。"
                "输出一条自然中文玩家发言，不要替 GM 写结果、开场或收束。",
            ]
        )

    def _fallback_utterance(
        self,
        step: ReplayStep,
        legal_context: LegalActionContext,
        *,
        last_gm_reply: str = "",
        recent_public_context: str = "",
    ) -> str:
        speaker = step.speaker or "玩家"
        actor = step.actor or (legal_context.current_actor if legal_context.current_actor in legal_context.known_pcs else "")
        subject = actor or speaker
        public_context = str(recent_public_context or last_gm_reply or "")
        # A clock names a changing situation, not necessarily a physical thing
        # a hero can approach or inspect. Only explicit clock-oriented actions
        # may use it as their mechanical target.
        target = step.target or self._grounded_context_target(public_context, legal_context) or "现场"
        method = step.method_hint or "用谨慎的方式观察局面"
        method = re.sub(
            r"[；;，,]?\s*(?:如果|若)(?:这|此)?需要检定.*$",
            "",
            method,
        ).strip()
        intent = step.intent or step.stage_goal

        if legal_context.pending_decisions:
            return f"{speaker}: {self._decision_window_fallback(legal_context.pending_decisions[0], legal_context)}"

        if step.kind.startswith("session_zero"):
            if any(token in intent for token in ("confirm", "确认", "同意")) or any(
                token in (step.method_hint or step.message) for token in ("同意", "赞成", "就按这个", "写入", "确认")
            ):
                return f"{speaker}: {step.method_hint or step.message or '我同意，就按这个写入。'}"
            if "角色" in intent or "character" in intent.lower():
                hint = step.method_hint or "我先有个画面：一个离家出走、还不太会信任队友的年轻旅人。"
                if "确认创建" in hint or ("职业" in hint and "属性" in hint and "技能" in hint):
                    return f"{speaker}: {hint}"
                return f"{speaker}: {hint} 名字和职业我想听听大家的角色后再定，可以先这样占个方向。"
            if "安全" in intent or "界限" in intent or "帷幕" in intent or "safety" in intent.lower():
                return f"{speaker}: {step.method_hint or '我的界限是不要出现真实残酷虐待儿童的细节；帷幕是亲密场景淡出处理。'}"
            hint = step.method_hint or step.stage_goal or "这个世界的失落遗迹会留下风铃般的灵魂回声。"
            if any(token in hint for token in ("大家觉得", "你们觉得", "合适吗", "可以吗")):
                return f"{speaker}: {hint}"
            if any(
                token in hint
                for token in (
                    "魔法与科技定位",
                    "我贡献",
                    "重大历史事件",
                    "世界奥秘",
                    "世界谜团",
                    "世界威胁",
                    "先跳过",
                    "这一项跳过",
                    "暂时没想法",
                    "暂时没有",
                    "跳过",
                )
            ):
                return f"{speaker}: {hint}"
            return f"{speaker}: 我先丢一个不确定的点子：{hint}大家觉得合适吗？"
        if legal_context.conflict_active and actor and actor != legal_context.current_actor:
            return f"{speaker}: {subject}先稳住位置，等轮到我时再处理眼前局面；现在我只给当前行动者一个简短建议。"
        if "本行动不得继续向NPC追问" in str(step.stage_goal or ""):
            return f"{speaker}: {self._contextual_action_fallback(subject, public_context, step)}"
        if "物语点" in intent or "fabula" in intent.lower():
            return f"{speaker}: {subject}愿意消耗1点物语点，提出一个和当前线索有关的新事实：{method}。"
        if "仪式" in intent:
            method = re.sub(r"[；;，,]?\s*(?:如果|若)(?:这|此)?需要检定.*$", "", method).strip()
            ritual_target = step.target or self._first_clock_name(legal_context) or target
            return f"{speaker}: {subject}尝试推进仪式【{ritual_target}】，{method}。"
        if "工程" in intent:
            return f"{speaker}: {subject}启动工程【{target}】，目标是{method}，先记录材料和需要协助的人手。"
        if "命刻" in intent or "推进" in intent:
            clock_target = step.target or self._first_clock_name(legal_context) or target
            method = step.method_hint or self._clock_method(clock_target, public_context)
            method = re.sub(r"[；;，,]?\s*(?:如果|若)(?:这|此)?需要检定.*$", "", method).strip()
            if method:
                return f"{speaker}: {subject}{method}。"
            return f"{speaker}: {self._contextual_action_fallback(subject, public_context, step)}"
        if "防御" in intent:
            return f"{speaker}: {subject}进入防御姿态，先护住队伍的破绽。"
        if "攻击" in intent:
            return f"{speaker}: {subject}用已装备武器攻击眼前最有威胁的敌人。"
        if "治疗" in intent and legal_context.legal_spells:
            spell = legal_context.legal_spells[0]
            return f"{speaker}: {subject}施放已掌握法术【{spell}】，目标是{target}。"
        if "调查" in intent:
            if target in {"现场", "当前目标", "当前局面", "局面"}:
                return f"{speaker}: {subject}环顾周围，从声音、痕迹和在场者的反应判断眼下发生了什么。"
            if not str(step.method_hint or "").strip():
                return f"{speaker}: {self._investigation_fallback(subject, target, public_context)}"
            if "需要检定" in method:
                return f"{speaker}: {subject}先调查【{target}】，{method}"
            return f"{speaker}: {subject}靠近【{target}】仔细检查，{method}。"
        return f"{speaker}: {self._contextual_action_fallback(subject, public_context, step)}"

    @staticmethod
    def _investigation_fallback(subject: str, target: str, public_context: str) -> str:
        clean_target = str(target or "现场").strip("【】 ") or "现场"
        context = str(public_context or "")[-1800:]
        if re.search(r"旅人|伤员|守卫|巡守|会长|使者|书记官|掌柜|向导|人$", clean_target):
            return (
                f"{subject}先不打断{clean_target}，留意对方的呼吸、视线和听见关键名字时的反应。"
            )
        if re.search(r"车辙|脚印|泥痕|灰痕|墨迹|血迹|碎片|刻痕|暗记|封蜡", clean_target):
            return f"{subject}蹲下来比对{clean_target}的新旧边缘，再沿最清楚的一段查它从哪里来。"
        if re.search(r"闸门|门锁|门轴|机关|阀门|绳轮|旧钟|风铃|控制台|装置", clean_target):
            return f"{subject}检查{clean_target}的接缝、磨损和最近受力的位置，看看还有哪部分能活动。"
        if re.search(r"逼近|脚步|追兵|巡逻|包围", context):
            return f"{subject}留在掩体内观察{clean_target}，同时分辨外面脚步的方向和人数。"
        return f"{subject}绕着{clean_target}看一圈，从摆放位置、表面变化和周围痕迹判断最近发生过什么。"

    @classmethod
    def _contextual_action_fallback(
        cls,
        subject: str,
        public_context: str,
        step: ReplayStep,
    ) -> str:
        """Choose a concrete lane from public fiction when the model is down."""

        context = str(public_context or "")[-5000:]
        candidates = cls._contextual_action_candidates(subject, context, step)
        recent_families = cls._recent_player_families(context)
        for family, utterance in candidates:
            if family not in recent_families[-2:]:
                return utterance
        index = sum(ord(char) for char in str(step.id or subject)) % len(candidates)
        return candidates[index][1]

    @classmethod
    def _open_condition_action_fallback(
        cls,
        subject: str,
        conditions: list[dict[str, str]],
        *,
        public_context: str = "",
        known_pcs: list[str] | None = None,
    ) -> str:
        """Produce a grounded action when semantic FU-PL repair is exhausted."""

        if not conditions:
            return ""
        ready_item = next(
            (
                item
                for item in reversed(conditions)
                if cls._open_condition_ready_for_payout(item, public_context)
            ),
            None,
        )
        if ready_item is not None:
            return cls._open_condition_payout_request(subject, ready_item)
        available_conditions = [
            item
            for item in conditions
            if not cls._condition_requires_another_pc(
                str(item.get("condition") or ""),
                subject=subject,
                known_pcs=known_pcs,
            )
        ]
        if not available_conditions:
            return ""
        item = available_conditions[-1]
        npc = str(item.get("npc") or "对方").strip()
        condition = " ".join(str(item.get("condition") or "").split()).strip()
        if not condition:
            return ""
        concrete_check = cls._condition_execution_fallback(subject, condition)
        if concrete_check:
            return concrete_check
        action = re.split(r"；\s*满足标准[：:]?", condition, maxsplit=1)[0].strip()
        alternatives = [
            part.strip(" ，,。；;")
            for part in re.split(r"[，,；;]\s*(?:或者|或是|或)", action)
            if part.strip(" ，,。；;")
        ]
        if len(alternatives) > 1:
            def executable_score(value: str) -> tuple[int, int]:
                score = 0
                if any(
                    marker in value
                    for marker in (
                        "当众承诺",
                        "明确承诺",
                        "承担",
                        "担保",
                        "守望誓约",
                        "宣读",
                        "签下",
                        "签署",
                    )
                ):
                    score += 8
                if any(marker in value for marker in ("放入", "带到", "交给", "交出", "递交")):
                    score += 4
                if any(marker in value for marker in ("一项", "某个", "合适的", "能证明")):
                    score -= 4
                return score, -len(value)

            action = max(alternatives, key=executable_score)
        action = re.sub(
            r"^(?:只要|如果|若)(?:他|她|对方|[^，,]{1,16})?(?:亲眼)?(?:确认|看到|看见)?",
            "",
            action,
        ).strip(" ，,")
        action = re.sub(r"^(?:要|需要|要求)?(?:玩家|英雄们?|队伍)(?:必须|需要|应当|要)?", "", action).strip(" ，,")
        action = re.split(
            r"[，,](?:他|她|对方|这名[^，,]{0,10})(?:就|便|会|才会)?(?:视为|认为|同意|允许|兑现)",
            action,
            maxsplit=1,
        )[0].strip(" ，,。")
        action = re.sub(
            rf"^{re.escape(subject)}(?:本人|亲自)?(?:现在|当场)?",
            "",
            action,
        ).strip(" ，,")
        action = action.replace("已被放入", "放入").replace("被带到", "带到")
        action = re.sub(r"(?:、|并且|且)", "，并", action, count=1)
        if not action:
            return ""
        return f"{subject}现在{action}。"

    @staticmethod
    def _condition_requires_another_pc(
        condition: str,
        *,
        subject: str,
        known_pcs: list[str] | None,
    ) -> bool:
        """Return whether a public bargain explicitly belongs to another PC."""

        source = " ".join(str(condition or "").split())
        for pc in dict.fromkeys(
            str(item or "").strip() for item in (known_pcs or []) if str(item or "").strip()
        ):
            if pc == subject or pc == "玩家":
                continue
            if re.search(
                rf"(?:^|[。；;]){re.escape(pc)}.{{0,32}}"
                r"(?:亲自|当面|以自己的名义|由自己|本人|承担|担保|承诺|宣誓|立誓|签名|说明)",
                source,
            ):
                return True
        return False

    @staticmethod
    def _condition_execution_fallback(subject: str, condition: str) -> str:
        """Render a visible action for a route-safety condition.

        Generic condition text such as “查明旧路起段的风险” is an objective,
        not something a player can literally do in one sentence.  The fallback
        therefore chooses a bounded observation from the current side of the
        threshold and leaves its result for the GM.
        """

        source = str(condition or "")
        route_check = bool(
            re.search(r"(?:查明|检查|确认).{0,24}(?:旧路|起段|通道|入口|眼前一段|门外)", source)
        )
        if not route_check:
            return ""
        target = "旧路起段" if "旧路" in source or "起段" in source else "门外可见的一段"
        traveller = "失名旅人" if "失名旅人" in source else ("旅人" if "旅人" in source else "")
        traveller_clause = f"让{traveller}留在同伴之间，" if traveller else ""
        retreat_clause = "；一旦发现异常，就立刻退回门内" if re.search(r"退回|退路|撤回|撤离", source) else ""
        return (
            f"{subject}{traveller_clause}停在尚未开启的闸门内侧，"
            f"查看{target}可见范围内的脚印、遮挡和异常动静{retreat_clause}。"
        )

    @classmethod
    def _object_disposition_fallback(
        cls,
        subject: str,
        legal_context: LegalActionContext,
        *,
        public_context: str,
    ) -> str:
        """Act on a newly disclosed loose object instead of rechecking it."""

        latest_gm = cls._latest_gm_reply(public_context)
        if not latest_gm or not re.search(
            r"(?:该|应该|应当|得|要).{0,16}(?:放回|归还|交给|收好|保管|接管)|"
            r"不该.{0,12}(?:留|放)(?:在)?(?:外头|这里|原处)",
            latest_gm,
        ):
            return ""
        item = cls._last_public_object(
            latest_gm,
            (
                r"(银白铃舌|铃舌|钟舌|钥匙|遗物|铅签|封蜡|小布袋|信件|碎片|徽记|令牌)",
            ),
        )
        if not item:
            return ""
        npc = next(
            (
                name
                for name in reversed(legal_context.known_npcs)
                if name and name in latest_gm
            ),
            legal_context.known_npcs[-1] if legal_context.known_npcs else "",
        )
        if not npc:
            return ""
        forbidden = cls._last_public_object(
            public_context,
            (r"(主铃架|祭坛|法阵核心|封锁门|危险装置)",),
        )
        avoid = (
            f"不靠近{forbidden}，"
            if forbidden
            and re.search(
                rf"(?:别|不要|不能|避开|不再).{{0,10}}{re.escape(forbidden)}",
                public_context,
            )
            else ""
        )
        return (
            f"{subject}{avoid}用随身布巾包住{item}，"
            f"把它递到{npc}面前，请{npc}接手保管。"
        )

    @classmethod
    def _immediate_focus_action_fallback(
        cls,
        subject: str,
        *,
        public_context: str,
    ) -> str:
        """Follow a newly stated GM investigation focus with one real action.

        The simulator's long context deliberately contains prior scenes.  A
        generic object search can therefore choose a stale ledger or doorway
        even when the GM has just said "look at the moon fragment first".
        This narrow fallback only follows an explicit present-tense focus in
        the newest GM reply and leaves the outcome for the GM to adjudicate.
        """

        latest_gm = cls._latest_gm_reply(public_context)
        if not latest_gm:
            return ""
        focus_match = re.search(
            r"(?:先|优先)(?:看|查|观察|检查|研究|处理)(?:一下|一眼)?"
            r"(?P<item>[一-鿿A-Za-z0-9·]{2,20}(?:遗物|铃舌|风铃|名册|账册|刻痕|碎片|钥匙|信件|徽记|装置))",
            latest_gm,
        )
        if focus_match is None:
            return ""
        item = " ".join(str(focus_match.group("item") or "").split()).strip()
        if not item or cls._looks_like_non_entity_target(item):
            return ""
        return (
            f"{subject}与{item}保持一臂距离，观察它的表面、裂纹和周围留下的痕迹"
            "在当前异常里如何变化，判断它与眼前记忆异状的关联。"
        )

    @classmethod
    def _newly_revealed_detail_action_fallback(
        cls,
        subject: str,
        *,
        public_context: str,
    ) -> str:
        """Turn the newest material reveal into a distinct, grounded action.

        A simulator can otherwise keep treating a revealed object as the same
        broad ``road`` or ``door`` lane that another player just touched.  The
        reveal itself may expose a new physical detail: a banner's nail holes,
        a seal's torn edge, or fresh dust around a moved marker.  This helper
        deliberately asks about that new detail rather than re-reading the
        already disclosed inscription or asserting an answer for the GM.
        """

        latest_gm = cls._latest_gm_reply(public_context)
        if not latest_gm or not re.search(
            r"(?:掀开|掀起|揭开|翻开|剥开|露出|显出|显露|重新露出|浮现|掉落|脱落|打开)",
            latest_gm,
        ):
            return ""

        revealed = cls._last_public_object(
            latest_gm,
            (
                r"(旧路标|路牌|告示牌|碑文|铭牌|刻牌|封条|门牌|木板|刻痕|内侧刻字|旧刻字)",
            ),
        )
        if not revealed:
            return ""
        covering = cls._last_public_object(
            latest_gm,
            (r"(辉钢(?:收购)?旗|收购旗|旗尾|旗帜|通告|封条|遮布|盖布)",),
        )
        if covering:
            return (
                f"{subject}停在被露出的{revealed}旁，避开{covering}，"
                "检查钉孔、压痕和边缘的新旧，想判断这层遮挡是何时、从哪一侧固定上去的。"
            )
        return (
            f"{subject}停在刚露出的{revealed}旁，"
            "比对边缘碎屑、压痕和周围痕迹，想判断它最后一次被遮住或移动是在什么时候。"
        )

    @classmethod
    def _open_condition_ready_for_payout(
        cls,
        condition: dict[str, str],
        public_context: str,
    ) -> bool:
        """Use only the GM's latest public acknowledgement as payout evidence."""

        promised = str(condition.get("promised_result") or "").strip()
        latest_gm = cls._latest_gm_reply(public_context)
        if not promised or not latest_gm:
            return False
        readiness = bool(
            re.search(
                r"(?:已经.{0,24}(?:做到|带到|交出|放入|完成|满足)|"
                r"条件.{0,8}(?:已经|满足|完成|成立)|"
                r"(?:够了|这就|现在我会|现在就|该我|轮到我|按约|依约))",
                latest_gm,
            )
        )
        if not readiness:
            return False
        promised_modes = cls._npc_payout_modes(promised)
        latest_modes = cls._npc_payout_modes(latest_gm)
        promised_topics = cls._npc_payout_topics(promised)
        latest_topics = cls._npc_payout_topics(latest_gm)
        promise_reference = bool(re.search(r"承诺|答应|约定|按约|依约", latest_gm))
        return bool(
            promise_reference
            or (
                promised_modes & latest_modes
                and (not promised_topics or bool(promised_topics & latest_topics))
            )
        )

    @classmethod
    def _open_condition_payout_request(
        cls,
        subject: str,
        condition: dict[str, str],
    ) -> str:
        npc = str(condition.get("npc") or "对方").strip() or "对方"
        promised = " ".join(str(condition.get("promised_result") or "").split()).strip(" 。")
        promised = re.sub(
            r"^(?:我|他|她|对方)?(?:将会|会|可以|愿意|答应|承诺)(?:当场|立刻|马上|现在)?",
            "",
            promised,
        ).strip(" ，,")
        if "disclose" in cls._npc_payout_modes(promised):
            detail = re.split(
                r"(?:说出|讲出|告诉|透露|说明|交代|回答|揭示|给出)",
                promised,
                maxsplit=1,
            )[-1].strip(" ，,")
            detail = re.sub(r"^(?:当场|立刻|马上|现在)", "", detail).strip(" ，,")
            speech = f"你答应的{detail}，现在请说出来" if detail else "现在请把你答应的内容说出来"
        elif promised:
            speech = f"请按刚才的约定，{promised}"
        else:
            speech = "现在请兑现刚才的承诺"
        return f"{subject}转向{npc}：\u201c{speech.rstrip('。')}。\u201d"

    @classmethod
    def _contextual_action_candidates(
        cls,
        subject: str,
        public_context: str,
        step: ReplayStep,
    ) -> list[tuple[str, str]]:
        context = str(public_context or "")[-5000:]
        candidates: list[tuple[str, str]] = []
        untouched_object = cls._last_public_object(
            context,
            (r"(记忆罐|抽取车|称量器|铅签|小布袋|旧名册|名册|站务告示|告示)",),
        )
        recent_player_text = "\n".join(cls._recent_player_utterances(context)[-4:])
        if untouched_object and untouched_object not in recent_player_text:
            candidates.append(
                (
                    "investigate",
                    f"{subject}与{untouched_object}保持一臂距离，从它的摆放位置、表面变化和周围痕迹判断最近谁接触过它。",
                )
            )
        care_target = cls._last_public_object(
            context,
            (
                r"(?:受伤|流血|喘息|发抖|昏迷|虚弱|站不稳)的?(失名旅人|旅人|巡守|守门人|旁观者)",
                r"(失名旅人|旅人|巡守|守门人|旁观者).{0,10}(?:受伤|流血|喘息|发抖|昏迷|虚弱|站不稳)",
            ),
        )
        if care_target:
            shelter = cls._last_public_object(
                context,
                (r"(廊柱后|墙后|柜台后|侧廊|门侧|岩壁后|树后|车厢内)",),
            )
            care_action = (
                f"再把人扶到{shelter}坐稳"
                if shelter
                else "再扶住肩背，让人先在原地坐稳"
            )
            candidates.append(
                ("care", f"{subject}走到{care_target}身边，先检查呼吸和能否移动，{care_action}。")
            )
        entrance = cls._last_public_object(
            context,
            (r"(旧路闸门|旧闸门|门扇|门框|门轴|门口|入口|舱门|铁门|木门)",),
        )
        obstacle = cls._last_public_object(
            context,
            (r"(木柜|货架|木箱|长桌|石块|粗麻布|干草捆|门栓|木栓)",),
        )
        if re.search(r"逼近|追兵|巡逻|脚步|包围|警报|火光|敌人", context):
            if entrance and obstacle:
                candidates.append(
                    ("guard", f"{subject}贴近{entrance}听清外面的方位，再把{obstacle}卡在门板受力最重的位置。")
                )
            elif entrance:
                candidates.append(
                    ("guard", f"{subject}贴住{entrance}听外面的脚步方位，同时用肩膀抵住最先震动的那一侧。")
                )
            else:
                candidates.append(
                    ("guard", f"{subject}停下手里的调查，退到同伴外侧保持警戒，专心听清威胁接近的方向。")
                )
        mechanism = cls._last_public_object(
            context,
            (
                r"(旧路闸门|旧闸门|门轴|门栓|木栓|门锁|锁孔|机关|阀门|绳轮|吊桥|钟舌|旧钟|风铃|信号塔|控制台)",
            ),
        )
        if mechanism and mechanism not in recent_player_text:
            candidates.append(
                ("manipulate", f"{subject}俯身检查{mechanism}的接缝与磨损，伸手试一试其中仍能活动的部件。")
            )
        trace = cls._last_public_object(
            context,
            (r"(财团车辙|车辙|脚印|泥痕|灰痕|墨迹|血迹|金属碎片|金属片|刻痕|旧账册|账册|暗记)",),
        )
        if trace and trace not in recent_player_text:
            candidates.append(
                ("investigate", f"{subject}把{trace}的新旧边缘逐段比对，沿着较新的一段查到它被遮断的位置。")
            )
        latest_gm_context = cls._latest_gm_reply(context)
        route = cls._last_public_object(
            latest_gm_context,
            (r"(旧路|侧路|后门|通道|出口|栈桥|海岸|小船|林间路|钟塔|风铃廊|走廊|营地)",),
        )
        if route:
            route_action = f"{subject}沿{route}走到转折处，停下来查看这段路能否安全通过。"
            if cls._crosses_unopened_route(route_action, context):
                route_action = f"{subject}留在{route}入口这一侧，观察门闩、地面和两旁遮挡，确认有没有别的危险。"
            candidates.append(("move", route_action))
        if not candidates:
            target = cls._context_target(context)
            utterance = (
                f"{subject}站在原地环顾周围，从声音、位置和新旧痕迹里确认刚才究竟发生了什么变化。"
                if target == "现场"
                else f"{subject}靠近{target}，从声音、位置和新旧痕迹里确认刚才究竟发生了什么变化。"
            )
            candidates.append(("investigate", utterance))
        return candidates

    @classmethod
    def _safe_spell_action_candidates(
        cls,
        subject: str,
        legal_context: LegalActionContext,
        public_context: str,
    ) -> list[str]:
        """Offer only support spells whose target can be grounded publicly."""

        safe_spells = {
            "元素幕障", "元素武器", "气旋", "加速术", "预言术", "镜面反射", "黑暗武器",
            "护卫灵气", "觉醒", "屏障", "净化", "治愈术", "慈悲", "巩固", "灵魂之幕", "魂能武器",
        }
        context = str(public_context or "")
        ally_target = next(
            (
                name
                for name in legal_context.known_pcs
                if name and name in context and name != subject
            ),
            subject,
        )
        weapon_spells = {"元素武器", "黑暗武器", "魂能武器"}
        self_spells = {"气旋", "预言术", "镜面反射"}
        candidates: list[str] = []
        rules_by_name = {
            str(rule.get("name") or ""): rule
            for rule in legal_context.legal_spell_rules
            if str(rule.get("name") or "")
        }
        for spell in legal_context.legal_spells:
            clean = str(spell or "").strip()
            if clean not in safe_spells:
                continue
            if clean == "治愈术":
                wounded = [
                    name
                    for name, values in legal_context.pc_resources.items()
                    if int(values.get("hp", 0)) < int(values.get("max_hp", 0))
                ]
                if legal_context.pc_resources and not wounded:
                    continue
                if wounded:
                    ally_target = next(
                        (name for name in wounded if name != subject),
                        wounded[0],
                    )
            if clean == "净化" and legal_context.pc_resources:
                affected = [
                    name
                    for name, values in legal_context.pc_resources.items()
                    if list(values.get("statuses") or [])
                ]
                if not affected:
                    continue
                ally_target = next(
                    (name for name in affected if name != subject),
                    affected[0],
                )
            rule = rules_by_name.get(clean, {})
            if clean in weapon_spells:
                utterance = f"{subject}施放已掌握的【{clean}】，作用在自己当前装备的武器上"
            elif clean in self_spells:
                utterance = f"{subject}对自己施放已掌握的【{clean}】"
            else:
                utterance = f"{subject}对{ally_target}施放已掌握的【{clean}】"
            choices: list[str] = []
            if rule.get("selectable_damage_types"):
                choices.append(f"选择{rule['selectable_damage_types'][0]}系")
            if rule.get("selectable_statuses"):
                choices.append(f"选择{rule['selectable_statuses'][0]}")
            if rule.get("selectable_attributes"):
                choices.append(f"选择{rule['selectable_attributes'][0]}")
            if choices:
                utterance += "，" + "，".join(choices)
            candidates.append(utterance + "。")
        return candidates

    @staticmethod
    def _last_public_object(text: str, patterns: tuple[str, ...]) -> str:
        matches: list[tuple[int, str]] = []
        for pattern in patterns:
            for match in re.finditer(pattern, str(text or "")):
                value = next((group for group in match.groups() if group), match.group(0))
                clean = str(value or "").strip("【】[]，,。；;：: ")
                window = str(text or "")[max(0, match.start() - 40) : match.end() + 40]
                if clean in {"旧账册", "账册", "旧档", "档案", "卷宗", "登记簿", "名册"} and re.search(
                    r"(?:可查|能查|可以查|去.{0,16}(?:查|找)|档案(?:室|库)|库里|以后|改日|明天)",
                    window,
                ):
                    continue
                if clean:
                    matches.append((match.start(), clean))
        return max(matches, default=(-1, ""), key=lambda item: item[0])[1]

    @classmethod
    def _affordance_response_fallback(
        cls,
        subject: str,
        public_context: str,
        *,
        known_npcs: list[str] | None = None,
    ) -> str:
        latest_gm = cls._latest_gm_reply(public_context)
        if not latest_gm:
            return ""
        if cls._is_condition_fulfillment_offer(latest_gm):
            condition_action = cls._condition_execution_fallback(subject, latest_gm)
            if condition_action:
                return condition_action
        directive = cls._concrete_party_directive(latest_gm)
        if directive is not None:
            target, destination, verb = directive
            return f"{subject}扶住{target}，把{target}往{destination}{verb}一步。"
        if re.search(
            r"(?:愿意|可以|能).{0,40}(?:说|讲|念|公开|透露).{0,50}"
            r"(?:部分|一段|方向|内容|能确认|愿意公开)",
            latest_gm,
        ):
            target = cls._disclosure_target(public_context, known_npcs or [])
            if target:
                return f"{subject}留在原地，朝{target}点头，请他现在把愿意公开的那部分说完。"
        if re.search(r"跟我来|随我来|带你们(?:去|进)|可以进去|门(?:已经|现在)?(?:开了|打开)", latest_gm):
            return f"{subject}明确接受这个提议，立即跟上带路的人穿过刚打开的入口。"
        if re.search(r"要么.{1,50}要么|(?:或者|或是|，或).{1,50}(?:条件|满足)", latest_gm) and re.search(
            r"誓约|担保|承诺", latest_gm
        ):
            oath_match = re.search(
                r"(?:承担|履行|签下|立下|宣读|接受)(?:一项|这项|该项|这个)?"
                r"(?P<name>[\u4e00-\u9fff]{2,8}誓约)",
                latest_gm,
            ) or re.search(
                r"(?:一项|这项|该项|这个)(?P<name>[\u4e00-\u9fff]{2,8}誓约)",
                latest_gm,
            )
            oath_name = oath_match.group("name") if oath_match else "这项誓约"
            return (
                f"{subject}选择以自己的名义承担{oath_name}，当众说清愿意为旅人引发的旧路风险负责，"
                "并请在场守望者现在见证。"
            )
        if re.search(r"最后(?:一次)?(?:警告|机会)|现在(?:交出|开门|离开|投降)|要么.{1,30}要么", latest_gm):
            return f"{subject}明确拒绝对方的最后通牒，护住身后的人，做好承受对方下一步行动的准备。"
        if re.search(r"(?:现在)?只要[^。！？\n]{1,80}(?:否则|不然)", latest_gm):
            if "去路" in latest_gm and re.search(
                r"(?:完整名字|全段走法|完整去路|终点).{0,16}(?:不说|不会|不能|不交)",
                public_context,
            ):
                return (
                    f"{subject}当场拒绝再交出更多去路，不去碰对方留下的东西，"
                    "留在门内护住旅人，准备承受对方刚宣布的后果。"
                )
            return (
                f"{subject}当场拒绝这项要求，不去碰对方留下的东西，"
                "留在原位准备承受对方刚宣布的后果。"
            )
        return ""

    @classmethod
    def _disclosure_target(cls, public_context: str, known_npcs: list[str]) -> str:
        recent_players = cls._recent_player_utterances(public_context)
        latest_player = recent_players[-1] if recent_players else ""
        names = [str(name or "").strip() for name in known_npcs if str(name or "").strip()]
        for name in names:
            if re.search(
                rf"(?:问|向|对|朝).{{0,5}}{re.escape(name)}(?:一句|开口|询问|追问|说|[，,：:])?",
                latest_player,
            ):
                return name
        latest_gm = cls._latest_gm_reply(public_context)
        present = [name for name in names if name in latest_gm]
        if present:
            return present[-1]
        return ""

    @classmethod
    def _recent_player_families(cls, public_context: str) -> list[str]:
        families: list[str] = []
        for utterance in cls._recent_player_utterances(public_context)[-14:]:
            if cls._looks_like_uncommitted_table_talk(utterance):
                continue
            family = cls._action_family(utterance)
            if family:
                families.append(family)
        return families

    @staticmethod
    def _context_target(public_context: str) -> str:
        text = str(public_context or "")[-3000:]
        bracketed = re.findall(r"【([^】]{2,28})】", text)
        mechanical_labels = {"揭示", "进展", "纽带", "优势", "转折", "机会", "大成功", "大失败"}
        for candidate in reversed(bracketed):
            clean = str(candidate or "").strip()
            if (
                clean
                and clean not in mechanical_labels
                and not re.search(r"(?:命刻|机会)(?:效果)?$", clean)
                and not ConstrainedPlayerSimulator._looks_like_non_entity_target(clean)
            ):
                return clean
        named = re.findall(
            r"([\u4e00-\u9fffA-Za-z·]{2,18}(?:会长|监察官|代理人|旅人|医师|守卫|船长|长者|司教|公主|王|少女|老人))",
            text,
        )
        for candidate in reversed(named):
            if not ConstrainedPlayerSimulator._looks_like_non_entity_target(candidate):
                return candidate
        for noun in ("门轴", "旧路", "风铃", "车辙", "账册", "钟塔", "水道", "海图", "路标", "营火", "石碑"):
            if noun in text:
                return noun
        return "现场"

    @classmethod
    def _looks_like_non_entity_target(cls, value: str) -> bool:
        clean = str(value or "").strip("【】[]，,。；;：:！？!? ")
        if not clean:
            return True
        if cls._NON_ENTITY_TARGET_PATTERN.search(clean):
            return True
        return bool(
            re.match(r"^(?:向|对|跟|和|把|让|请)", clean)
            and re.search(r"(?:会长|旅人|守卫|敌人|同伴|队友|大家)$", clean)
        )

    @classmethod
    def _invalid_bracketed_action_target(
        cls,
        text: str,
        *,
        step: ReplayStep,
        legal_context: LegalActionContext,
        recent_public_context: str,
    ) -> str:
        """Reject synthetic targets copied from table talk or invented by FU-PL.

        Brackets make a model-generated target explicit.  A real scene entity
        must come from runtime state, the scripted target, or something the GM
        has publicly established.  Player speculation alone is not enough.
        """

        source = str(text or "")
        candidates = [
            str(item or "").strip()
            for item in re.findall(r"【([^】]{1,40})】", source)
            if str(item or "").strip()
        ]
        if not candidates:
            return ""

        ability_names = {
            str(item or "").strip()
            for item in [*legal_context.legal_spells, *legal_context.legal_skills]
            if str(item or "").strip()
        }
        mechanical = {"揭示", "进展", "纽带", "优势", "转折", "机会", "大成功", "大失败"}
        clock_names: set[str] = set()
        for raw in legal_context.active_clocks:
            match = re.search(r"[【\[]([^】\]]+)[】\]]", str(raw or ""))
            name = match.group(1).strip() if match else str(raw or "").split()[0].strip()
            if name:
                clock_names.add(name)

        explicit_entities = [
            str(item or "").strip()
            for item in [
                step.target,
                *legal_context.known_pcs,
                *legal_context.known_enemies,
                *legal_context.known_npcs,
                *legal_context.visible_scene_elements,
                *legal_context.established_scene_facts,
                *(item.get("npc", "") for item in legal_context.open_npc_conditions),
            ]
            if str(item or "").strip()
        ]
        gm_public_text = "\n".join(
            content
            for speaker, content in cls._dialogue_blocks(recent_public_context)
            if speaker in cls._GM_SPEAKERS
        )

        for candidate in candidates:
            if candidate in ability_names or candidate in mechanical or candidate in clock_names:
                continue
            if cls._looks_like_non_entity_target(candidate):
                return "action_slot_targets_discussion_fragment"

            escaped = re.escape(candidate)
            used_as_target = bool(
                re.search(
                    rf"(?:靠近|走向|走到|贴近|来到|检查|观察|调查|触摸|拿起|翻看|询问|问)"
                    rf"[^，,。；;！？!?\n]{{0,10}}【{escaped}】|"
                    rf"【{escaped}】[^，,。；;！？!?\n]{{0,10}}(?:旁|前|边|附近|仔细检查|作为目标)",
                    source,
                )
            )
            if not used_as_target:
                continue
            grounded = any(
                candidate == entity or candidate in entity or entity in candidate
                for entity in explicit_entities
            )
            if not grounded and candidate not in gm_public_text:
                return "action_slot_targets_unestablished_entity"
        return ""

    @classmethod
    def _grounded_context_target(
        cls,
        public_context: str,
        legal_context: LegalActionContext,
    ) -> str:
        """Prefer runtime-established entities over bracketed rule labels."""

        text = str(public_context or "")[-5000:]
        candidates = [
            str(item or "").strip()
            for item in [*legal_context.visible_scene_elements, *legal_context.known_npcs]
            if str(item or "").strip()
            and not cls._mechanical_context_entry(str(item or ""))
        ]
        mentioned = [
            (text.rfind(candidate), candidate)
            for candidate in dict.fromkeys(candidates)
            if candidate in text
        ]
        if mentioned:
            return max(mentioned, key=lambda item: item[0])[1]
        return cls._context_target(text)

    @staticmethod
    def _mechanical_context_entry(value: str) -> bool:
        text = " ".join(str(value or "").split()).strip()
        return bool(
            not text
            or "命刻" in text
            or re.search(r"【(?:揭示|进展|纽带|优势|转折|机会|大成功|大失败)】", text)
            or re.search(r"(?:自动推进|焦点|赌注|进度未变化|还剩\s*\d+\s*格)", text)
        )

    @classmethod
    def _acts_on_mechanical_label(
        cls,
        text: str,
        *,
        legal_context: LegalActionContext,
    ) -> bool:
        bracketed = {
            str(item or "").strip()
            for item in re.findall(r"【([^】]{1,40})】", str(text or ""))
            if str(item or "").strip()
        }
        if not bracketed:
            return False
        mechanical = {"揭示", "进展", "纽带", "优势", "转折", "机会", "大成功", "大失败"}
        clock_names: set[str] = set()
        for raw in legal_context.active_clocks:
            match = re.search(r"[【\[]([^】\]]+)[】\]]", str(raw or ""))
            name = match.group(1).strip() if match else str(raw or "").split()[0].strip()
            if name:
                clock_names.add(name)
        physical_action = bool(
            re.search(
                r"靠近|走到|贴近|拿起|触摸|打开|检查|观察|调查|翻看|搬动|按住|推开",
                str(text or ""),
            )
        )
        return bool(bracketed & mechanical) or bool(physical_action and bracketed & clock_names)

    @classmethod
    def _clock_method(cls, target: str, public_context: str = "") -> str:
        """Return a clock-facing action only when its means exist in public fiction.

        A clock name says what is at stake, not which road, lever or barricade is
        physically present.  Older fallbacks invented a fork and a road sign as
        soon as a pursuit clock existed; synthetic players then dragged those
        inventions into the campaign as if the GM had established them.
        """

        clean = str(target or "当前局面")
        context = str(public_context or "")[-6000:]
        if any(token in clean for token in ("巡逻", "追兵", "逼近", "包围")):
            trace = cls._last_public_object(
                context,
                (r"(财团车辙|车辙|脚印|泥痕|足迹|追踪标记|路标|暗记)",),
            )
            if trace:
                return f"走到{trace}旁，抹乱其中最新的一段，让追踪者更难沿原路定位现场"
            entrance = cls._last_public_object(
                context,
                (r"(旧路闸门|旧闸门|门扇|门框|门口|入口|舱门|铁门|木门)",),
            )
            obstacle = cls._last_public_object(
                context,
                (r"(木柜|货架|木箱|长桌|石块|粗麻布|干草捆|门栓|木栓)",),
            )
            if entrance and obstacle:
                return f"把{obstacle}移到{entrance}受力最重的位置，争取拖慢外面的追兵"
            return ""
        if any(token in clean for token in ("潮", "水位", "淹没", "洪水")):
            barrier = cls._last_public_object(
                context,
                (r"(水闸|闸门|舱门|堤坝|泄水口|排水沟|阀门)",),
            )
            return f"走到{barrier}旁检查支撑点，尝试把上涨的水势暂时挡住" if barrier else ""
        if any(token in clean for token in ("警报", "警戒", "增援")):
            device = cls._last_public_object(
                context,
                (r"(传讯装置|警报器|信号塔|警铃|控制台|通讯器)",),
            )
            return f"走到{device}旁切断它与外界的联系，阻止警报继续扩散" if device else ""
        if any(token in clean for token in ("仪式", "蓄力", "魔法")):
            node = cls._last_public_object(
                context,
                (r"(法阵|符文|水晶|祭坛|线圈|魔法节点|仪式节点)",),
            )
            return f"靠近{node}，尝试扰乱其中一处能量汇聚点" if node else ""
        if any(token in clean for token in ("开启", "修复", "信任", "说服", "争取")):
            evidence = cls._last_public_object(
                context,
                (r"(铅签|小布袋|旧名册|名册|告示|票联|收据|纸条|木牌|遗物|钥匙)",),
            )
            return f"把{evidence}放到众人都能核验的位置，说明自己愿意承担的代价" if evidence else ""
        return ""

    @staticmethod
    def _looks_like_direct_npc_question(text: str) -> bool:
        clean = ConstrainedPlayerSimulator._without_disavowed_npc_questions(text)
        return bool(
            re.search(r"[？?]", clean)
            or re.search(
                r"(?:问|询问|追问|请问|告诉我|告诉我们|回答|答复)[^。！？\n]{0,100}",
                clean,
            )
            or re.search(
                r"(?:你|你们|贵方)[^。！？\n]{0,56}(?:说清楚|说明|告诉|交代|讲清楚|公开)",
                clean,
            )
        )

    @staticmethod
    def _without_disavowed_npc_questions(text: str) -> str:
        """Drop clauses that explicitly say the hero is *not* asking again.

        The remaining clause is still checked normally, so “不再问白穗，改问
        岑铅” remains a question while “不再问白穗，改查布袋” does not.
        """

        clauses = [
            clause.strip()
            for clause in re.split(r"[，,。；;！？!?\n]+", str(text or ""))
            if clause.strip()
        ]
        kept: list[str] = []
        for clause in clauses:
            question = re.search(r"(?:追问|询问|请问|发问|问)", clause)
            disavowal = re.search(r"(?:不再|不继续|先不|暂不|没有再|停止)(?:向|对)?", clause)
            if question and disavowal and disavowal.start() <= question.start():
                continue
            kept.append(clause)
        return "。".join(kept)

    @staticmethod
    def _answers_pending_decision(text: str, window: dict[str, object]) -> bool:
        kind = str(window.get("kind") or "")
        if kind == "zero_hp":
            return any(token in text for token in ("牺牲", "放弃抵抗", "不牺牲", "活下去", "投降"))
        if kind == "opportunity_parameter":
            return any(token in text for token in ("目标", "选择", "对", "会长", "旅人", "敌"))
        if kind == "spell_parameter":
            groups: dict[str, list[str]] = {}
            for option in window.get("options", []):
                if not isinstance(option, dict):
                    continue
                parameter = str(option.get("parameter") or "")
                values = groups.setdefault(parameter, [])
                for key in ("label", "value"):
                    value = str(option.get(key) or "")
                    if value:
                        values.append(value)
            return bool(groups) and all(any(value in text for value in values) for values in groups.values())
        if kind == "critical_opportunity":
            selected = next(
                (
                    option
                    for option in window.get("options", [])
                    if isinstance(option, dict)
                    and str(option.get("effect") or "")
                    and str(option.get("effect") or "") in text
                ),
                None,
            )
            if selected is None:
                return False
            required = {
                str(value or "").strip()
                for value in (selected.get("requires") or [])
                if str(value or "").strip()
            }
            if "target" in required:
                owner = str(window.get("owner") or "").strip()
                if owner and owner not in text:
                    return False
                if not owner and not any(token in text for token in ("目标", "给", "对")):
                    return False
            if "clock_name" in required and "命刻" not in text:
                return False
            if "emotion" in required and not any(
                token in text for token in ("钦佩", "信赖", "喜爱", "忠诚", "憎恨", "猜忌")
            ):
                return False
            if "subject" in required and not any(token in text for token in ("出现", "来到", "现身")):
                return False
            if "status_effect" in required and not any(
                token in text for token in ("眩晕", "动摇", "迟缓", "虚弱")
            ):
                return False
            if "description" in required and len(text) < 12:
                return False
            return True
        if kind == "fumble_opportunity":
            return False
        if kind == "trait_invocation":
            return any(token in text for token in ("接受结果", "接受这次结果", "不重掷")) or (
                "援用" in text
                and any(
                    str(option.get("trait") or "") in text
                    for option in window.get("options", [])
                    if isinstance(option, dict) and str(option.get("trait") or "")
                )
            )
        if kind == "bond_invocation":
            return any(token in text for token in ("接受结果", "接受这次结果", "不重掷")) or (
                "羁绊" in text
                and any(
                    str(option.get("target") or "") in text
                    for option in window.get("options", [])
                    if isinstance(option, dict) and str(option.get("target") or "")
                )
            )
        if kind in {"skill_parameter", "skill_judgement"}:
            values = [
                str(value)
                for option in window.get("options", [])
                if isinstance(option, dict)
                for value in option.values()
                if isinstance(value, (str, int)) and str(value)
            ]
            return any(value in text for value in values) or any(token in text for token in ("发动", "使用", "不用", "放弃"))
        return any(token in text for token in ("接受这次失败", "接受结果", "不重掷", "选择", "使用"))

    @staticmethod
    def _decision_window_fallback(
        window: dict[str, object],
        legal_context: LegalActionContext,
    ) -> str:
        kind = str(window.get("kind") or "")
        options = [item for item in window.get("options", []) if isinstance(item, dict)]
        if kind == "zero_hp":
            return "我选择放弃抵抗并承受后果。"
        if kind == "opportunity_parameter":
            target = next(iter(legal_context.known_npcs or legal_context.known_enemies), "")
            return f"我把【揭示】用于【{target}】，想知道其目标或动机。" if target else "我先不提交这个机会，请GM再明确可选目标。"
        if kind == "spell_parameter":
            grouped: dict[str, list[dict[str, object]]] = {}
            for option in options:
                grouped.setdefault(str(option.get("parameter") or ""), []).append(option)
            pieces: list[str] = []
            target = next(iter(grouped.get("target", [])), None)
            if target is not None:
                pieces.append(f"目标选【{target.get('label') or target.get('value')}】")
            for parameter, label in (
                ("chosen_damage_type", "元素"),
                ("chosen_status", "异常"),
                ("chosen_attribute", "属性"),
            ):
                option = next(iter(grouped.get(parameter, [])), None)
                if option is not None:
                    pieces.append(f"{label}选【{option.get('label') or option.get('value')}】")
            return "，".join(pieces) + "。" if pieces else "请再说明这个法术缺少哪项选择。"
        if kind == "critical_opportunity":
            effects = [str(option.get("effect") or "") for option in options]
            if "优势" in effects:
                target = (
                    str(window.get("owner") or "").strip()
                    or str(legal_context.current_actor or "").strip()
                    or next(iter(legal_context.known_pcs), "")
                )
                if target:
                    return f"我把这次机会用于【优势】，目标是【{target}】。"
            effect = next((item for item in effects if item and item != "揭示"), effects[0] if effects else "")
            return f"我把这次机会用于【{effect}】。" if effect else "我接受当前结果，不再追加机会效果。"
        if kind == "fumble_opportunity":
            return "这次大失败的机会由GM处理。"
        if kind == "trait_invocation":
            if window.get("roll_success") is True:
                return "我接受这次结果，不重掷。"
            trait = next((str(option.get("trait") or "") for option in options if option.get("trait")), "")
            return f"我花 1 点物语点，援用【{trait}】重掷两枚骰。" if trait else "我接受这次失败，不重掷。"
        if kind == "bond_invocation":
            if window.get("roll_success") is True:
                return "我接受这次结果，不重掷。"
            target = next((str(option.get("target") or "") for option in options if option.get("target")), "")
            return f"我花 1 点物语点，援用与【{target}】的羁绊。" if target else "我接受这次失败，不重掷。"
        if kind in {"skill_parameter", "skill_judgement"} and options:
            first = next(
                (
                    option
                    for option in options
                    if str(option.get("choice") or "").strip() != "decline"
                ),
                options[0],
            )
            choice = str(first.get("choice") or "").strip()
            skill = str(window.get("label") or window.get("skill") or "技能").strip()
            if choice == "assist_trait":
                trait = str(first.get("trait") or "").strip()
                target = str(first.get("target") or "").strip()
                if trait and target:
                    return f"我发动【{skill}】，援用【{target}】的特质【{trait}】帮助其改写检定。"
            if choice == "assist_bond":
                bond_target = str(first.get("bond_target") or "").strip()
                target = str(first.get("target") or "").strip()
                if bond_target and target:
                    return f"我发动【{skill}】，援用【{target}】与【{bond_target}】的羁绊帮助其改写检定。"
            if choice == "decline":
                return f"我不发动【{skill}】。"
            details = [
                f"{key}=【{value}】"
                for key, value in first.items()
                if key != "choice" and isinstance(value, (str, int)) and str(value).strip()
            ]
            suffix = f"，{'，'.join(details)}" if details else ""
            if choice:
                return f"我为【{skill}】选择【{choice}】{suffix}。"
        if options:
            first = options[0]
            label = str(
                first.get("effect")
                or first.get("label")
                or first.get("choice")
                or first.get("target")
                or ""
            )
            if label:
                return f"我选择【{label}】。"
        return "我接受这次结果，不再追加处理。"

    def _followup_to_gm_prompt(
        self,
        step: ReplayStep,
        legal_context: LegalActionContext,
        last_gm_reply: str,
    ) -> str:
        reply = str(last_gm_reply or "")
        if not reply:
            return ""
        if legal_context.pending_decisions:
            # The semantic simulator must choose from the persisted window and
            # any required target; generic clock/theme follow-ups would answer
            # the wrong question here.
            return ""
        if "正在和其他玩家短暂商量" in str(step.stage_goal or ""):
            return ""
        speaker = step.speaker or "玩家"
        actor = step.actor or self._hero_name_from_reply(reply) or speaker
        # A direct NPC identity/agency demand is an in-fiction interaction,
        # even when it is phrased as an order ("说清楚") rather than a
        # literal question.  Check it before the generic prompt-marker gate.
        npc_identity_followup = self._explicit_npc_identity_followup(
            speaker=speaker,
            actor=actor,
            step=step,
            legal_context=legal_context,
            reply=reply,
        )
        if npc_identity_followup:
            return npc_identity_followup
        prompt_markers = (
            "？",
            "?",
            "请",
            "还缺",
            "还差",
            "先说",
            "想听",
            "想让",
            "哪一个",
            "哪些",
            "什么",
            "是否",
            "如何",
            "怎么",
        )
        if not any(marker in reply for marker in prompt_markers):
            return ""
        theme_match = re.search(r"主题[“「『【]?([^”」』】。；;\n]{1,12})[”」』】]?", reply)
        asks_theme_drive = any(token in reply for token in ("推着", "支配行动", "推动角色", "推动这个英雄", "底线", "拒绝退让"))
        if asks_theme_drive:
            theme = theme_match.group(1).strip() if theme_match else "这个主题"
            return (
                f"{speaker}: 对{actor}来说，“{theme}”会在有人试图把活人的选择变成筹码时推着他行动；"
                "他的底线是可以妥协方法和代价，但不会同意牺牲无辜者的记忆或名字。"
            )

        if any(token in reply for token in ("还没听到", "英雄概念", "怎么称呼", "名字", "身份", "故乡", "5 级", "属性", "技能", "出门时带着", "装备")) and step.kind.startswith("session_zero"):
            if "职业技能" in reply or "技能" in reply and any(token in reply for token in ("还差", "还需", "先选一项")):
                skill = self._skill_from_hint(step.method_hint) or self._skill_from_hint(step.message) or "保镖"
                return f"{speaker}: {actor}这次先选职业技能【{skill}】；剩下的我等下一轮再补。"
            if any(token in reply for token in ("名字", "怎么称呼")):
                return f"{speaker}: 先叫{actor}吧，名字如果之后和世界设定不合，我再改。"
            if "身份" in reply:
                return f"{speaker}: {actor}的身份先定成“追寻失落名字的旅人”，不是很强势，更像被卷进来的那种。"
            if "主题" in reply:
                return f"{speaker}: 主题我先选【希望】。他会相信名字和记忆能被找回来，但这个理解后面可以再长出来。"
            if "故乡" in reply:
                return f"{speaker}: 故乡我想接到刚才共创的边境地点，像白花碑驿站附近的小聚落。"
            if "职业" in reply or "5 级" in reply:
                return f"{speaker}: 职业我先倾向御魂使3级、旅人2级；如果队里太缺前排，我再把一部分换成守护者。"
            if "属性" in reply:
                return f"{speaker}: 属性我先用均衡分配：洞察d10、意志d8、力量d8、敏捷d6，比较像会观察但不太灵活。"
            if "装备" in reply or "出门时带着" in reply:
                return f"{speaker}: 装备我先想带法杖和旅行装束，具体花费我等确认职业后再核。"
            return f"{speaker}: 我先说一个画面：{actor}总是把一枚旧风铃握在手心，像怕忘记什么人。其他部分我慢慢补。"

        if legal_context.active_clocks and any(token in reply for token in ("命刻", "还剩", "赌注", "倒计时")):
            clock_name = self._first_clock_name(legal_context)
            if legal_context.conflict_active and legal_context.current_actor and step.actor == legal_context.current_actor:
                return f"{speaker}: {actor}把注意力转回【{clock_name}】，用当前最合理的方法推进或压制它，请 GM 指定检定。"
            if legal_context.conflict_active:
                return ""
            return f"{speaker}: {actor}把注意力转回【{clock_name}】，先确认它此刻对现场造成了什么变化。"
        return ""

    def _message_answers_gm_prompt(
        self,
        message: str,
        last_gm_reply: str,
        *,
        legal_context: LegalActionContext | None = None,
    ) -> bool:
        if not last_gm_reply:
            return True
        identity_question = self._explicit_npc_identity_question(
            last_gm_reply,
            known_npcs=(legal_context.known_npcs if legal_context else []),
        )
        if identity_question is not None:
            return self._answers_explicit_npc_identity_question(message, identity_question)
        if "职业技能" in last_gm_reply and any(token in last_gm_reply for token in ("还差", "还需", "先选一项")):
            return "技能" in message or any(token in message for token in ("选", "学习", "学会"))
        if any(token in last_gm_reply for token in ("推着", "支配行动", "推动角色", "底线", "拒绝退让")):
            return any(token in message for token in ("会在", "不会", "底线", "推着", "选择", "妥协", "拒绝"))
        if "命刻" in last_gm_reply and any(token in last_gm_reply for token in ("还剩", "赌注", "倒计时")):
            return "命刻" in message or "推进" in message or "压制" in message or "阻止" in message
        return True

    def _explicit_npc_identity_followup(
        self,
        *,
        speaker: str,
        actor: str,
        step: ReplayStep,
        legal_context: LegalActionContext,
        reply: str,
    ) -> str:
        """Answer a narrow, immediate NPC identity check before generic play.

        FU-PL has a deliberately broad set of clock and affordance fallbacks.
        Without this guard, a fresh NPC demand such as "state your name and
        whether you speak for the traveler" can be buried by an old pursuit
        clock.  The reply below only uses the PC's own name and a cautious
        statement of agency; it never invents the companion's history.
        """

        if step.kind.startswith("session_zero"):
            return ""
        if (
            legal_context.conflict_active
            and legal_context.current_actor
            and step.actor
            and step.actor != legal_context.current_actor
        ):
            return ""
        question = self._explicit_npc_identity_question(
            reply,
            known_npcs=legal_context.known_npcs,
        )
        if question is None:
            return ""
        npc = str(question["npc"])
        relation_target = str(question.get("relation_target") or "那位同行者")
        parts: list[str] = []
        if bool(question.get("asks_name")):
            parts.append(f"我是{actor}")
        if bool(question.get("asks_relationship")):
            parts.append(f"我和{relation_target}同行")
        if bool(question.get("asks_representation")):
            parts.append(f"我只代表自己答话，不替{relation_target}作主")
        elif bool(question.get("asks_relationship")):
            parts.append(f"{relation_target}自己的话，由本人决定是否说")
        if not parts:
            return ""
        return f"{speaker}: {actor}看向{npc}：“{'。'.join(parts)}。”"

    @classmethod
    def _explicit_npc_identity_question(
        cls,
        reply: str,
        *,
        known_npcs: list[str],
    ) -> dict[str, object] | None:
        """Recognise a direct, answerable NPC identity/agency demand.

        This is intentionally narrower than general NPC dialogue.  It is a
        deterministic safety net for replay players, not a substitute for the
        language model deciding how to answer a broad question.
        """

        source = str(reply or "")
        if not source or not known_npcs:
            return None
        mentions = [
            (source.rfind(name), name)
            for raw_name in known_npcs
            if (name := str(raw_name or "").strip()) and source.rfind(name) >= 0
        ]
        if not mentions:
            return None
        _, npc = max(mentions, key=lambda item: item[0])
        segment = source[source.rfind(npc) :]
        # A name in background narration is not enough.  The NPC must address
        # the heroes or issue a concrete demand about who can speak.
        if not re.search(r"(?:你|你们|报上|说清|交代)", segment):
            return None
        asks_name = bool(
            re.search(
                r"(?:你|你们)(?:现在|先|只要)?(?:把|将|说出|交代|报上)?"
                r"(?:你自己的|你们自己的|自己的|你的|你们的)?(?:姓名|名字|身份)"
                r"|你(?:叫|是)什么(?:名字|人)?|报上(?:你的|你们的)?(?:姓名|名字|身份)",
                segment,
            )
        )
        asks_relationship = bool(
            re.search(
                r"(?:你(?:和|与)[^，,。；;！？?]{1,24}?(?:的)?关系|你们[^，,。；;！？?]{0,16}关系)",
                segment,
            )
        )
        asks_representation = bool(
            re.search(
                r"(?:是否|是不是|能否|可否|要不要)[^，,。；;！？?]{0,24}?(?:代表|替)[^，,。；;！？?]{0,16}?(?:答话|作答)|"
                r"(?:代表|替)[^，,。；;！？?]{0,14}?(?:答话|作答)",
                segment,
            )
        )
        if not (asks_name or asks_relationship or asks_representation):
            return None
        relation_match = re.search(
            r"你(?:和|与)(?P<target>[^，,。；;！？?]{1,24}?)(?:的)?关系",
            segment,
        )
        relation_target = ""
        if relation_match:
            relation_target = str(relation_match.group("target") or "").strip(" “\"”")
        if not relation_target:
            relation_target = "柱影里那位" if "柱影里那位" in segment else "那位同行者"
        return {
            "npc": npc,
            "asks_name": asks_name,
            "asks_relationship": asks_relationship,
            "asks_representation": asks_representation,
            "relation_target": relation_target,
        }

    @staticmethod
    def _answers_explicit_npc_identity_question(
        message: str,
        question: dict[str, object],
    ) -> bool:
        """Tell whether a scripted line really answers the NPC's demand."""

        source = ConstrainedPlayerSimulator._strip_optional_speaker_prefix(message)
        if bool(question.get("asks_name")) and not re.search(r"(?:我是|我叫|姓名|名字)", source):
            return False
        if bool(question.get("asks_relationship")) and not re.search(
            r"(?:同行|同来|同路|关系|同伴|同一边|我和|我与|保护)",
            source,
        ):
            return False
        if bool(question.get("asks_representation")) and not re.search(
            r"(?:只代表自己|不替[^，,。；;！？?]{0,16}(?:答话|作答|作主)|不代[^，,。；;！？?]{0,16}(?:答话|作答|作主)|代表[^，,。；;！？?]{0,16}(?:自己|答话|作答))",
            source,
        ):
            return False
        return True

    def _hero_name_from_reply(self, reply: str) -> str:
        match = re.search(r"【([^】]+)】", reply)
        if match:
            return match.group(1).strip()
        match = re.search(r"([一-龥A-Za-z·]{2,8})的主题", reply)
        return match.group(1).strip() if match else ""

    def _skill_from_hint(self, text: str) -> str:
        if not text:
            return ""
        known = (
            "保镖",
            "防御精通",
            "挺身守护",
            "元素魔法",
            "元素系仪式",
            "灵魂魔法",
            "御魂系仪式",
            "治愈之力",
            "鼓舞",
            "予以信任",
            "便携装置",
            "秘密配方",
            "先见之明",
            "碎骨",
            "破防打击",
            "谴责",
            "熵系魔法",
            "熵系仪式",
            "见多识广",
            "充足补给",
            "契约与召唤",
            "奥灵系仪式",
            "野性之语",
            "拟兽系仪式",
            "暗影击",
        )
        for name in known:
            if name in text:
                return name
        return ""

    def _first_clock_name(self, legal_context: LegalActionContext) -> str:
        if not legal_context.active_clocks:
            return ""
        raw = legal_context.active_clocks[0]
        match = re.search(r"\[([^\]]+)\]", raw)
        return match.group(1) if match else raw.split()[0]

    def _clean_llm_text(self, raw: str) -> str:
        text = raw.strip()
        text = re.sub(r"^```(?:text|markdown)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
        return text.splitlines()[0].strip() if "\n" in text else text
