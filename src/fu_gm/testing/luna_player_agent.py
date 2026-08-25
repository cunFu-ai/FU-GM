from __future__ import annotations

import json
import os
import re
from difflib import SequenceMatcher
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from typing import Any

from fu_gm.config import (
    DEFAULT_LLM_MODEL,
    LLMConfig,
    resolve_model_api_key,
    uses_high_latency_model,
)
from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.llm_utils import extract_json_object
from fu_gm.prompt_cache import build_cache_friendly_messages
from fu_gm.testing.player_simulator import SimulatedUtterance
from fu_gm.testing.replay_models import LegalActionContext, ReplayStep


LUNA_PLAYER_SYSTEM_PROMPT = """
你是多人文字跑团中的一名普通玩家，不是主持人、旁白、规则引擎或测试执行器。你的工作只有一个：根据自己
能看见的公开聊天、自己的角色卡与当前桌边时机，决定这一刻是否说话；若说话，只写一条像真人会发在群里的
中文消息。

【视角边界】
你只知道输入中明确列出的公开事实、公开人物、自己的资源与能力，以及最近群聊里已经说出口的内容。你看不到
主持人的暗线、NPC隐藏动机、场景规划、预定答案、测试目标或其他玩家的内心。不要为了让故事顺利而猜中真相，
也不要把可疑迹象直接写成结论。角色可以误会、犹豫、不同意、改变主意或问一个朴素问题。

【玩家权限】
你只能控制指定角色。你可以邀请、建议、呼喊或请求其他玩家角色，但不能宣布他们已经同意、移动、接住物件、
施法或行动。你可以向NPC提出请求，但NPC是否答应、开门、交出东西、透露情报或跟随，必须等主持人回答。
你可以描述自己角色的动作与台词，不能描述检定结果、发现内容、伤害结果、环境反应或场景收束。

【桌边节奏】
若当前是自由讨论，可以只表达倾向、问队友意见、开一句不打断气氛的玩笑，或者选择等待；不要为了推动测试而
强行声明行动。自由讨论中若说话，audience只能是player或table；想直接询问NPC或主持人时，把这件事留到自己的
行动时机。若当前是你的行动时机，选择一件具体、当前可执行的事，直接说角色现在怎么做和想达到什么，不要
追加“如果成功就……”或第二个备用动作。若NPC或主持人刚明确问你问题，应先回答、拒答或承认不知道。若有属于
你的待决选择，应先处理该选择，而且一条消息只处理当前一个窗口：援用特质时只选一项并说明它为何有助于本次
检定，不要在同一句里再追加羁绊、第二项特质或另一个机会效果。冲突中若还没轮到你，可以讨论战术，但不能抢先
结算行动。

若输入中的 turn_requirement 是 must_submit_action_or_question，表示这个槽位必须落实一次行动：声明角色现在执行的
具体动作，或直接向现场NPC/主持人提出一个需要回应的问题。不能只向另一名玩家提问、只说台词、含糊地表示
“我准备着”、继续讨论或等待。若它是 must_consume_rules_action，则角色已经在冲突回合中用过自由台词；这次必须
声明会消耗本回合的合法行动，不能继续追问NPC。

【规则边界】
只能使用输入列出的技能、法术、装备和剧情物件；不知道规则时可以自然询问主持人。不要把命刻当作角色能看见的
按钮，应描述角色在世界中如何拖延威胁或完成目标。不要说“如果需要检定”“请指定属性”“我成功调查到”等替
主持人安排裁定的话。known_skill_rules 中 can_declare_as_action=false 的条目是被动、仪式权限或授法技能，不能把
它当成一次主动技能行动；授法技能只代表角色学会了 known_spells 中的具体法术。施法时必须选择 known_spells 中
的标准法术名，并留意 known_spell_rules 所列精神值消耗与目标。有限资源应服务于眼前已经公开的危险、伤势或计划。

【说话风格】
像群友聊天，不写舞台说明、测试标签、行动分析、教学提醒或第三人称总结。通常一到三句，一次只保留一个主要
意图。可以简短引用必要线索，但不要把主持人或上一名玩家的话换词复述一遍。不同玩家应保持各自的人格、语气和
风险偏好，不要都说成同一种谨慎而工整的助理口吻。

【自然群聊事件】
当输入包含natural_table_event时，这条公开消息会同时送达桌上的每名玩家。你不是被脚本点名的唯一发言者；请依据
角色关注点、消息受众、自己是否能增加新内容以及当前行动条，自主决定speak或wait。没有新内容、别人更适合回答、
刚说过相同意见或只是想看后续时，优先wait。即使决定speak，也不一定采取行动：可以回应另一名玩家、表达不同意见、
开一句短玩笑、向NPC或GM提问，或在确实轮到自己时声明行动。若stale_draft存在，表示你原先准备的话尚未发出但桌面
已经出现新消息；重新判断旧话是否仍合时宜，可以修改或直接wait，不要机械补发。

speak_after_ms表示你在真实群聊里理解消息、组织语言和打字所需的时间，不是固定填0：简单附和或短回应通常300到1500，
普通讨论或提问通常1200到4000，新提案或较长行动通常2500到7000。只有必须立刻回答的待决选择或极短警告才接近0。
它只决定多个已经自主选择speak的玩家谁先发出，不影响你是否应当说话。

第零章时，player_mind.private_brief是你开团前记下的个人灵感，不是已经成立的世界事实，也不是必须照抄的答案。
只在当前话题相关时自然提出其中一小部分，先听其他玩家怎么想；可以赞成、调整或放弃原想法。不要一次性倾倒整张
角色卡或所有世界贡献，也不要把尚未获得其他玩家确认的提案说成全桌共识。

只输出一个JSON对象：
{"decision":"speak或wait","kind":"action、in_character、out_of_character、table_discussion、backchannel、rules_question或wait",
 "audience":"gm、npc、player或table","text":"要发送的群聊消息","reply_to_event_id":整数或null,
 "speak_after_ms":0到12000之间的整数,"mind_update":{"focus":"当前关注点","belief":"新增的个人判断或空字符串",
 "commitment":"自己刚作出的承诺或空字符串","mood":"简短心境"},"reason":"一句后台理由"}
decision为wait时kind必须为wait且text必须为空。待决选择仍不得wait。不要输出Markdown代码块。
""".strip()


@dataclass(frozen=True)
class PlayerPersona:
    player_name: str
    hero_name: str
    table_style: str
    character_voice: str
    priorities: tuple[str, ...] = ()
    risk_style: str = "会权衡风险，但不会总选最稳妥的方案"
    rules_fluency: str = "理解基础规则，复杂细节会向主持人确认"

    def prompt_payload(self) -> dict[str, object]:
        return {
            "player_name": self.player_name,
            "hero_name": self.hero_name,
            "table_style": self.table_style,
            "character_voice": self.character_voice,
            "priorities": list(self.priorities),
            "risk_style": self.risk_style,
            "rules_fluency": self.rules_fluency,
        }


DEFAULT_LONGRUN_PERSONAS: dict[str, PlayerPersona] = {
    "阿凛": PlayerPersona(
        player_name="阿凛",
        hero_name="伊莉雅",
        table_style="愿意先做决定，也会停下来确认同伴是否跟得上",
        character_voice="说话直接，保护别人时会变得很坚定",
        priorities=("保护脆弱者", "把线索变成可以执行的方案"),
        risk_style="面对迫近危险时愿意先承担风险",
    ),
    "南星": PlayerPersona(
        player_name="南星",
        hero_name="赛璃",
        table_style="喜欢先听清条件，再提出简短而实际的问题",
        character_voice="克制、温和，偶尔会用很淡的玩笑缓和气氛",
        priorities=("避免无谓冲突", "照顾队伍资源"),
        risk_style="通常谨慎，但不会在已经明确的选择前反复确认",
    ),
    "白河": PlayerPersona(
        player_name="白河",
        hero_name="洛岚",
        table_style="对机关、路线和可利用的现场细节比较敏感",
        character_voice="务实，想到办法时会很快说出来",
        priorities=("寻找可操作的突破口", "让发现帮助全队做决定"),
        risk_style="愿意为了新路线尝试不完全可靠的办法",
    ),
    "时雨": PlayerPersona(
        player_name="时雨",
        hero_name="艾薇娅",
        table_style="在意人物关系与话语背后的情绪，不急着抢行动",
        character_voice="观察细致，表达立场时不绕弯",
        priorities=("弄清人物真正害怕什么", "维护角色自己的底线"),
        risk_style="社交上敢于冒险，面对未知机关会更谨慎",
    ),
    "澄砚": PlayerPersona(
        player_name="澄砚",
        hero_name="苍祈",
        table_style="容易被奇怪生物和魔法现象吸引，但会听取队友提醒",
        character_voice="好奇、灵活，有时会先抛一个不成熟的想法",
        priorities=("理解异常生命与魔法", "给队伍创造意外选择"),
        risk_style="愿意试验，但不会凭空声明未知能力",
    ),
}


class PlayerBoundaryGuard:
    """只阻止模拟器作弊；普通玩家的误判交给GM在桌面上处理。"""

    _ACTION_PATTERN = re.compile(
        r"调查|观察|检查|细看|看清|侧耳|倾听|听清|攻击|防御|施放|使用|推进|阻挡|"
        r"撬|打开|移动|走向|走近|靠近|进入|拾起|捡起|交给|递给|投掷|抛|拿出|"
        r"启动|拆除|说服|威胁|安抚|询问"
    )
    _CONTROL_PATTERN = re.compile(
        r"(?:已经|立刻|随即|于是)?(?:同意|跟上|接住|接过|走进|离开|施放|攻击|打开|交出|告诉)"
    )
    _REQUEST_PATTERN = re.compile(r"请|能不能|可不可以|要不要|愿不愿意|希望|建议|问|等.*回应")
    _RESULT_PATTERN = re.compile(r"(?:我|我们|[\u4e00-\u9fff]{2,8})(?:成功|已经)(?:说服|发现|击中|打开|破解|完成)")
    _TOOL_NAME_PATTERN = re.compile(r"(?:施放|使用)(?:法术|技能)?【([^】]+)】")
    _DISCUSSION_ACTION_VERBS = (
        "调查|观察|检查|弄清|确认|攻击|防御|施放|使用|推进|阻挡|撬|打开|"
        "移动|走向|进入|拾起|捡起|交给|递给|投掷|抛|启动|拆除|说服|威胁|安抚|询问"
    )
    _PLAYER_CHARACTER_ALIASES = {
        "阿凛": "伊莉雅",
        "南星": "赛璃",
        "白河": "洛岚",
        "时雨": "艾薇娅",
        "澄砚": "苍祈",
    }

    def __init__(self, player_character_aliases: dict[str, str] | None = None) -> None:
        self.player_character_aliases = dict(
            player_character_aliases
            if player_character_aliases is not None
            else self._PLAYER_CHARACTER_ALIASES
        )

    def validate(
        self,
        text: str,
        *,
        step: ReplayStep,
        legal_context: LegalActionContext,
        mode: str,
        audience: str = "",
        utterance_kind: str = "",
    ) -> list[str]:
        clean = " ".join(str(text or "").split()).strip()
        errors: list[str] = []
        if not clean:
            if mode not in {"discussion", "out_of_turn", "natural"}:
                errors.append("当前时机需要玩家回应，不能保持沉默")
            return errors
        if len(clean) > 420:
            errors.append("玩家消息过长，应只保留一个主要意图")
        if any(token in clean for token in ("测试目标", "stage_goal", "脚本要求", "覆盖率")):
            errors.append("泄露了测试控制信息")
        if self._RESULT_PATTERN.search(clean):
            errors.append("替主持人宣布了行动结果")
        if mode == "discussion" and self._looks_like_immediate_discussion_action(
            clean,
            actor=str(step.actor or "").strip(),
        ):
            errors.append(
                "自由讨论用了会被理解为立即行动的第一人称措辞；"
                "改成明确的建议、向队友提问，或等到自己的行动时机再执行"
            )
        if (
            mode == "natural"
            and str(utterance_kind or "").strip().lower()
            in {"in_character", "out_of_character", "table_discussion", "backchannel"}
            and self._looks_like_immediate_discussion_action(
                clean,
                actor=str(step.actor or "").strip(),
            )
        ):
            errors.append(
                "候选把立即角色行动标成了聊天；若角色正在执行动作，请使用kind=action，"
                "否则改成尚未落实的玩家交流"
            )
        if (
            mode == "natural"
            and legal_context.conflict_active
            and str(legal_context.current_actor or "").strip()
            and str(legal_context.current_actor or "").strip()
            != str(step.actor or step.speaker or "").strip()
            and str(utterance_kind or "").strip().lower() == "action"
        ):
            errors.append(
                f"冲突中当前轮到【{legal_context.current_actor}】，本角色只能聊天或等待，不能抢先行动"
            )
        if mode not in {"discussion", "out_of_turn", "session_zero"} and not (
            mode == "natural" and audience in {"player", "table"}
        ):
            for player_name, hero_name in self.player_character_aliases.items():
                if player_name not in clean:
                    continue
                errors.append(
                    f"角色行动把桌外玩家名【{player_name}】当成世界内人物；"
                    f"应改用该玩家的角色名【{hero_name}】"
                )
                break

        actor = str(step.actor or "").strip()
        if (
            legal_context.conflict_active
            and actor
            and legal_context.current_actor
            and actor != legal_context.current_actor
            and self._ACTION_PATTERN.search(clean)
            and not self._looks_like_advice(clean)
        ):
            errors.append(f"冲突中当前行动者是{legal_context.current_actor}，不能替{actor}抢先行动")

        allowed_named_rules = {
            *[str(item) for item in legal_context.legal_spells],
            *[str(item) for item in legal_context.legal_skills],
            *[
                str(item.get("name") or "")
                for item in legal_context.story_items
                if str(item.get("name") or "").strip()
            ],
        }
        for name in self._TOOL_NAME_PATTERN.findall(clean):
            if name not in allowed_named_rules:
                errors.append(f"声明了当前角色未拥有或未公开的能力【{name}】")

        actor_resources = legal_context.pc_resources.get(actor, {}) if actor else {}
        current_mp = actor_resources.get("mp")
        if isinstance(current_mp, (int, float)):
            for rule in legal_context.legal_spell_rules:
                name = str(rule.get("name") or "").strip()
                if not name or name not in clean:
                    continue
                if not re.search(rf"(?:施放|施展|使用)\s*【?{re.escape(name)}】?", clean):
                    continue
                minimum_cost = int(rule.get("mp_cost") or 0)
                if (
                    minimum_cost > int(current_mp)
                    and not bool(rule.get("can_pay_with_hp"))
                ):
                    errors.append(
                        f"当前精神值{int(current_mp)}不足以施放【{name}】"
                        f"（至少需要{minimum_cost}点）"
                    )
                    break
                max_targets = int(rule.get("max_affordable_targets") or 0)
                if max_targets <= 0:
                    continue
                mentioned_targets = {
                    entity
                    for entity in [
                        *legal_context.known_pcs,
                        *legal_context.known_enemies,
                        *legal_context.present_npcs,
                    ]
                    if entity and entity != actor and entity in clean
                }
                if "自己" in clean:
                    mentioned_targets.add(actor or "自己")
                target_count = max(1, len(mentioned_targets))
                if "所有" in clean or "全体" in clean:
                    target_count = max(target_count, 3)
                if target_count > max_targets:
                    errors.append(
                        f"当前资源至多支持【{name}】影响{max_targets}个目标，"
                        f"不能声明{target_count}个目标"
                    )
                    break

        if mode not in {"discussion", "out_of_turn", "session_zero", "decision"}:
            for rule in legal_context.legal_skill_rules:
                name = str(rule.get("name") or "").strip()
                if (
                    not name
                    or rule.get("can_declare_as_action") is not False
                    or name not in clean
                ):
                    continue
                if re.search(
                    rf"(?:以|用|使用|发动|施放|施展|运用|借助)\s*(?:技能)?【?{re.escape(name)}】?",
                    clean,
                ):
                    errors.append(f"【{name}】不能单独声明为一次主动行动")
                    break
        must_consume_rules_action = bool(
            mode == "action" and step.payload.get("must_consume_turn") is True
        )
        must_submit_action_slot = bool(
            mode == "action" and step.payload.get("must_submit_action_slot") is True
        )
        has_rules_action = bool(self._ACTION_PATTERN.search(clean))
        has_direct_npc_or_gm_question = bool(
            str(audience or "").strip().lower() in {"gm", "npc"}
            and self._REQUEST_PATTERN.search(clean)
        )
        if must_consume_rules_action and not has_rules_action:
            errors.append(
                "这个回合已经进行过自由交谈，现在必须声明一项会消耗回合的行动"
            )
        elif (
            must_submit_action_slot
            and not has_rules_action
            and not has_direct_npc_or_gm_question
        ):
            errors.append(
                "当前行动槽必须由本角色执行一个具体动作，或直接向现场NPC/主持人提问；"
                "不能只向另一名玩家提问或含糊表示准备"
            )

        for other in legal_context.known_pcs:
            if not other or other == actor or other not in clean:
                continue
            prefix, tail = clean.split(other, 1)
            tail = tail[:28]
            pc_is_target = bool(
                re.search(r"(?:对|向|朝|给|为|替)[^。；！？]{0,40}$", prefix)
            )
            if self._CONTROL_PATTERN.search(tail) and not self._REQUEST_PATTERN.search(clean):
                if pc_is_target:
                    continue
                errors.append(f"替其他玩家角色【{other}】决定了行动")
                break

        for npc in legal_context.present_npcs:
            if not npc or npc not in clean:
                continue
            prefix, tail = clean.split(npc, 1)
            tail = tail[:32]
            # “对旅人施放屏障”里的旅人是目标，不是被玩家代为控制的主体。
            npc_is_target = bool(
                re.search(r"(?:对|向|朝|给|为|替)[^。；！？]{0,40}$", prefix)
            )
            if self._CONTROL_PATTERN.search(tail) and not self._REQUEST_PATTERN.search(clean):
                if npc_is_target:
                    continue
                errors.append(f"替NPC【{npc}】宣布了回应或结果")
                break

        for item in legal_context.story_items:
            name = str(item.get("name") or "").strip()
            holder = str(item.get("holder") or "").strip()
            if (
                not name
                or not self._mentions_story_item(clean, name)
                or not holder
                or holder == actor
            ):
                continue
            if re.search(r"拾起|捡起|拿起|使用|交给|递给|投掷|抛|放下|嵌入", clean):
                if not self._REQUEST_PATTERN.search(clean):
                    errors.append(f"剧情物件【{name}】当前由【{holder}】持有")
        return list(dict.fromkeys(errors))

    @staticmethod
    def _mentions_story_item(text: str, name: str) -> bool:
        """允许玩家用已公开物件名的自然简称，但不做开放式意图猜测。"""

        compact = "".join(str(name or "").split())
        aliases = {compact}
        if len(compact) >= 3:
            aliases.add(compact[-2:])
        if len(compact) >= 4:
            aliases.add(compact[-3:])
        return any(alias and alias in text for alias in aliases)

    @classmethod
    def _looks_like_advice(cls, text: str) -> bool:
        return bool(
            re.search(r"建议|可以|要不要|不如|等轮到|先别|你来|谁来|提醒", text)
            and not re.search(r"我(?:现在|立刻|马上)?(?:调查|攻击|施放|移动|打开|拾起|捡起)", text)
        )

    @classmethod
    def _looks_like_immediate_discussion_action(
        cls,
        text: str,
        *,
        actor: str,
    ) -> bool:
        subjects = ["我"]
        if actor:
            subjects.append(re.escape(actor))
        subject_pattern = "(?:" + "|".join(subjects) + ")"
        return bool(
            re.search(
                subject_pattern
                + r"(?:想|准备|打算)?(?:先|现在|马上|这就|来|要去)?(?:"
                + cls._DISCUSSION_ACTION_VERBS
                + r")",
                text,
            )
        )


class LunaPlayerAgent:
    """公开视角、无工具权限、缓存友好的FU-PL V2。"""

    engine_name = "luna_v2"

    def __init__(
        self,
        *,
        use_llm: bool = True,
        client: OpenAICompatibleClient | Any | None = None,
        model: str = "",
        personas: dict[str, PlayerPersona] | None = None,
        continue_on_invalid: bool = True,
        max_attempts: int = 2,
    ) -> None:
        self.model = (
            str(model or "").strip()
            or os.environ.get("FU_GM_REPLAY_PLAYER_MODEL", "").strip()
            or DEFAULT_LLM_MODEL
        )
        config = (
            LLMConfig.for_test_client(self.model)
            if bool(getattr(client, "test_only", False))
            else LLMConfig.from_env()
        )
        if use_llm and client is None and config.api_key:
            player_config = replace(
                config,
                api_key=resolve_model_api_key(self.model, config.api_key),
                action_model=self.model,
                timeout_seconds=max(
                    config.timeout_seconds,
                    180.0 if uses_high_latency_model(self.model) else 90.0,
                ),
            )
            client = OpenAICompatibleClient(player_config)
        self.client = client
        self.use_llm = bool(use_llm and self.client is not None and self.model)
        self.continue_on_invalid = bool(continue_on_invalid)
        self.max_attempts = max(1, min(2, int(max_attempts)))
        self.personas = (
            dict(personas)
            if personas is not None
            else dict(DEFAULT_LONGRUN_PERSONAS)
        )
        self.guard = PlayerBoundaryGuard(
            {
                player_name: persona.hero_name
                for player_name, persona in self.personas.items()
            }
        )
        self.last_action_progress_review: dict[str, object] = {}
        self.last_table_discussion_review: dict[str, object] = {}
        self._player_history: dict[str, deque[str]] = defaultdict(lambda: deque(maxlen=4))

    def compose(
        self,
        *,
        step: ReplayStep,
        legal_context: LegalActionContext,
        last_gm_reply: str = "",
        recent_public_context: str = "",
        player_mind: dict[str, object] | None = None,
        natural_table_event: dict[str, object] | None = None,
        record_public_history: bool = True,
    ) -> SimulatedUtterance:
        mode = self._speaking_mode(step, legal_context)
        if not self.use_llm or self.client is None:
            fallback = self._safe_fallback(step, legal_context, mode=mode)
            return SimulatedUtterance(
                text=fallback,
                used_fallback=True,
                validation_errors=["luna_player_unavailable"],
                fallback_kind="luna_v2_unavailable",
            )

        persona = self._persona(step)
        persona_block = self._persona_block(persona)
        perspective = self._perspective_payload(
            step,
            legal_context,
            mode=mode,
            last_gm_reply=last_gm_reply,
            recent_public_context=recent_public_context,
            player_mind=player_mind,
            natural_table_event=natural_table_event,
        )
        base_dynamic = "当前公开玩家视角：\n" + json.dumps(
            perspective,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        base_user = f"{persona_block}\n\n{base_dynamic}"
        attempts: list[dict[str, object]] = []
        repair_errors: list[str] = []

        for attempt in range(1, self.max_attempts + 1):
            user_content = base_user
            if repair_errors:
                user_content += (
                    "\n\n上一条候选越过了玩家权限，请只修正这些问题，不要改变角色人格：\n- "
                    + "\n- ".join(repair_errors)
                )
            messages = build_cache_friendly_messages(
                static_system_prompt=LUNA_PLAYER_SYSTEM_PROMPT,
                user_content=user_content,
                cache_family="fu-pl-v2",
                user_cache_breakpoint_offsets=(
                    len(persona_block),
                    len(base_user),
                ),
            )
            try:
                operation = "fu_pl.generate"
                if str(getattr(self.client, "provider_name", "")) == "codex_subagent":
                    operation = f"fu_pl.generate.{persona.player_name}"
                raw = self.client.create_chat_completion(
                    model=self.model,
                    messages=messages,
                    temperature=0.85 if attempt == 1 else 0.45,
                    response_format={"type": "json_object"},
                    max_tokens=320,
                    operation=operation,
                )
                decision = extract_json_object(raw)
                candidate = self._clean_candidate(
                    decision.get("text"),
                    speaker=step.speaker,
                )
                action = str(decision.get("decision") or "speak").strip().lower()
                audience = str(decision.get("audience") or "").strip().lower()
                utterance_kind = str(
                    decision.get("kind")
                    or ("wait" if action == "wait" else "action")
                ).strip().lower()
                if action == "wait":
                    candidate = ""
                    utterance_kind = "wait"
                repair_errors = self.guard.validate(
                    candidate,
                    step=step,
                    legal_context=legal_context,
                    mode=mode,
                    audience=audience,
                    utterance_kind=utterance_kind,
                )
                if candidate and self._near_duplicate_of_public_history(
                    step.speaker,
                    candidate,
                ):
                    repair_errors.append(
                        "这句话与自己刚刚已经发到群里的消息近似重复；"
                        "若没有新增内容请wait，否则只说真正新增的一点"
                    )
                if action not in {"speak", "wait"}:
                    repair_errors.append("decision必须是speak或wait")
                if action == "wait" and mode not in {
                    "discussion",
                    "out_of_turn",
                    "natural",
                }:
                    repair_errors.append("当前时机需要实际回应，不能选择wait")
                if (
                    action == "speak"
                    and mode in {"discussion", "out_of_turn"}
                    and audience not in {"player", "table"}
                ):
                    repair_errors.append("自由讨论只能面向其他玩家或全桌，不能直接询问NPC或主持人")
                allowed_kinds = {
                    "action",
                    "in_character",
                    "out_of_character",
                    "table_discussion",
                    "backchannel",
                    "rules_question",
                    "wait",
                }
                if utterance_kind not in allowed_kinds:
                    repair_errors.append("kind不是允许的玩家消息类型")
                if action == "wait" and utterance_kind != "wait":
                    repair_errors.append("decision=wait时kind必须为wait")
                if action == "speak" and utterance_kind == "wait":
                    repair_errors.append("decision=speak时kind不能为wait")
                reply_to_event_id = self._optional_int(
                    decision.get("reply_to_event_id")
                )
                if audience not in {"gm", "npc", "player", "table"}:
                    repair_errors.append("audience必须是gm、npc、player或table")
                expected_event_id = self._optional_int(
                    (natural_table_event or {}).get("event_id")
                )
                if (
                    natural_table_event
                    and reply_to_event_id is not None
                    and expected_event_id is not None
                    and reply_to_event_id != expected_event_id
                ):
                    repair_errors.append(
                        "reply_to_event_id必须指向当前收到的公开消息；旧草稿不能直接补发"
                    )
                speak_after_ms = self._bounded_delay_ms(
                    decision.get("speak_after_ms")
                )
                mind_update = (
                    dict(decision.get("mind_update") or {})
                    if isinstance(decision.get("mind_update"), dict)
                    else {}
                )
                attempts.append(
                    {
                        "attempt": attempt,
                        "decision": action,
                        "audience": audience,
                        "kind": utterance_kind,
                        "reply_to_event_id": reply_to_event_id,
                        "speak_after_ms": speak_after_ms,
                        "text": candidate,
                        "validation_errors": list(repair_errors),
                        "raw": str(raw or "")[:1200],
                    }
                )
                if not repair_errors:
                    if candidate and record_public_history:
                        self._player_history[step.speaker].append(candidate)
                    self._update_review_state(mode, decision, candidate)
                    return SimulatedUtterance(
                        text=candidate,
                        used_fallback=False,
                        validation_errors=[],
                        prompt_preview=base_user[:1600],
                        model_attempts=attempts,
                        decision=action,
                        audience=audience,
                        utterance_kind=utterance_kind,
                        reply_to_event_id=reply_to_event_id,
                        speak_after_ms=speak_after_ms,
                        private_mind_update=mind_update,
                    )
            except Exception as exc:
                repair_errors = [f"luna_player_error:{type(exc).__name__}"]
                attempts.append(
                    {
                        "attempt": attempt,
                        "text": "",
                        "validation_errors": list(repair_errors),
                        "error": str(exc)[:500],
                    }
                )

        fallback = self._safe_fallback(step, legal_context, mode=mode)
        fallback_errors = self.guard.validate(
            fallback,
            step=step,
            legal_context=legal_context,
            mode=mode,
        )
        if fallback_errors and not self.continue_on_invalid:
            raise ValueError(f"Luna FU-PL输出与安全回退均无效：{repair_errors + fallback_errors}")
        self._update_review_state(mode, {}, fallback)
        return SimulatedUtterance(
            text=fallback,
            used_fallback=True,
            validation_errors=list(repair_errors or fallback_errors),
            prompt_preview=base_user[:1600],
            model_attempts=attempts,
            fallback_kind="luna_v2_guarded_fallback",
            fallback_diagnostics=list(repair_errors or fallback_errors),
            decision="speak" if fallback else "wait",
            utterance_kind="action" if fallback else "wait",
        )

    def record_delivered(self, speaker: str, text: str) -> None:
        """Record only a message that actually reached the public table."""

        clean = " ".join(str(text or "").split()).strip()
        if clean:
            self._player_history[str(speaker or "").strip()].append(clean)

    def _near_duplicate_of_public_history(self, speaker: str, text: str) -> bool:
        """Reject a player's own near-verbatim resend, not a changed opinion."""

        candidate = self._comparison_text(text)
        if len(candidate) < 12:
            return False
        for previous in self._player_history.get(str(speaker or "").strip(), ()):
            old = self._comparison_text(previous)
            if not old:
                continue
            if candidate == old:
                return True
            if min(len(candidate), len(old)) < 18:
                continue
            if SequenceMatcher(None, candidate, old).ratio() >= 0.84:
                return True
        return False

    @staticmethod
    def _comparison_text(value: object) -> str:
        return re.sub(r"[\s，。！？、；：,.!?;:\-—…'\"“”‘’（）()【】]", "", str(value or ""))

    def telemetry_payload(self) -> dict[str, object]:
        if self.client is None or not hasattr(self.client, "telemetry_payload"):
            return {}
        return dict(self.client.telemetry_payload() or {})

    def _persona(self, step: ReplayStep) -> PlayerPersona:
        existing = self.personas.get(step.speaker)
        if existing is not None:
            if step.actor and existing.hero_name != step.actor:
                return replace(existing, hero_name=step.actor)
            return existing
        return PlayerPersona(
            player_name=step.speaker or "玩家",
            hero_name=step.actor or step.speaker or "角色",
            table_style="会先听清公开局面，再按角色当下最在意的事回应",
            character_voice="自然、简短，不替主持人宣布结果",
        )

    @staticmethod
    def _persona_block(persona: PlayerPersona) -> str:
        return "固定玩家人格：\n" + json.dumps(
            persona.prompt_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _perspective_payload(
        self,
        step: ReplayStep,
        context: LegalActionContext,
        *,
        mode: str,
        last_gm_reply: str,
        recent_public_context: str,
        player_mind: dict[str, object] | None = None,
        natural_table_event: dict[str, object] | None = None,
    ) -> dict[str, object]:
        actor = str(step.actor or step.speaker or "").strip()
        public_context = str(recent_public_context or "").strip()[-5000:]
        latest = str(last_gm_reply or "").strip()[-1200:]
        if latest and latest in public_context:
            latest = ""
        natural_event = dict(natural_table_event or {})
        action_bar = dict(natural_event.get("action_bar") or {})
        legal_actions = list(context.legal_actions)
        if natural_event and not (
            bool(action_bar.get("you_are_current_actor"))
            or bool(context.pending_decisions)
        ):
            # Natural players know their sheet, but do not receive a menu that
            # nudges every listener toward taking an immediate rules action.
            legal_actions = []
        return {
            "speaking_mode": mode,
            "speaker": step.speaker,
            "actor": actor,
            "mode_instruction": self._mode_instruction(mode, context),
            "turn_requirement": (
                "must_consume_rules_action"
                if step.payload.get("must_consume_turn") is True
                else (
                    "must_submit_action_or_question"
                    if step.payload.get("must_submit_action_slot") is True
                    else "free_speech_or_action"
                )
            ),
            "scene": {
                "name": context.scene_name,
                "location": context.scene_location,
                "conflict_active": context.conflict_active,
                "current_actor": context.current_actor,
            },
            "your_public_status": dict(context.pc_resources.get(actor) or {}),
            "your_location": context.actor_locations.get(actor, ""),
            "legal_actions": legal_actions,
            "known_spells": list(context.legal_spells),
            "known_spell_rules": list(context.legal_spell_rules),
            "known_skills": list(context.legal_skills),
            "known_skill_rules": list(context.legal_skill_rules),
            "present_player_characters": list(context.present_pcs),
            "present_npcs": list(context.present_npcs),
            "visible_enemies": list(context.known_enemies),
            "visible_scene_elements": list(context.visible_scene_elements),
            "established_scene_facts": list(context.established_scene_facts),
            "immediate_scene_consequence": context.immediate_scene_consequence,
            "blocked_routes": list(context.blocked_routes),
            "public_story_items": list(context.story_items),
            "public_clocks": list(context.active_clocks),
            "public_npc_conditions": list(context.open_npc_conditions),
            "pending_decisions_for_you": list(context.pending_decisions),
            "public_rules_notes": list(context.notes),
            "latest_gm_message": latest,
            "recent_public_chat": public_context,
            "your_recent_messages": list(self._player_history.get(step.speaker, ())),
            "player_mind": dict(player_mind or {}),
            "natural_table_event": natural_event,
        }

    @staticmethod
    def _speaking_mode(step: ReplayStep, context: LegalActionContext) -> str:
        if context.pending_decisions:
            return "decision"
        if step.payload.get("natural_broadcast") is True:
            return "natural"
        if step.kind.startswith("session_zero"):
            return "session_zero"
        stage = str(step.stage_goal or "")
        if "正在和其他玩家短暂商量" in stage:
            return "discussion"
        if (
            context.conflict_active
            and step.actor
            and context.current_actor
            and step.actor != context.current_actor
        ):
            return "out_of_turn"
        return "action"

    @staticmethod
    def _mode_instruction(mode: str, context: LegalActionContext) -> str:
        if mode == "decision":
            if any(
                str(item.get("kind") or "").strip() == "zero_hp"
                for item in context.pending_decisions
                if isinstance(item, dict)
            ):
                return (
                    "主持人正在等你处理生命值归零选择；只回答当前窗口。"
                    "普通遭遇默认选择放弃抵抗并承受一种后果，以便继续扮演角色。"
                    "只有近期公开剧情已经明确把永久退场塑造成角色唯一愿望，"
                    "且玩家人格也明确支持这个决定时，才选择牺牲；不确定就活下来。"
                    "不要追加新的行动或替主持人描述牺牲、失败后果。"
                )
            return (
                "主持人正在等你的明确选择；只处理当前一个待决窗口。"
                "若援用身份、主题或故乡，只选一项并亲自说明它为何有助于这次检定；"
                "不要同时追加羁绊或第二个选择。"
            )
        if mode == "session_zero":
            return "这是共同创作时间；一次只贡献、确认或询问一个角色或世界点子。"
        if mode == "discussion":
            return "这是玩家自由讨论，不替任何角色或全队执行行动；可以选择暂时不说。"
        if mode == "out_of_turn":
            return f"冲突中当前轮到{context.current_actor}；只能简短讨论，不能抢先行动。"
        if mode == "natural":
            return (
                "你与其他玩家同时收到了这条公开消息。自行决定是否回应；没有新内容时保持wait。"
                "若当前行动条显示轮到你，可以先交流，也可以直接声明行动；若没轮到你，"
                "只能聊天、提醒或等待，不能消费回合。"
            )
        return "现在轮到你回应公开局面；选择一项当前可执行的角色行动或一个需要现场人物回答的问题。"

    @staticmethod
    def _optional_int(value: object) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _bounded_delay_ms(value: object) -> int:
        try:
            delay = int(value or 0)
        except (TypeError, ValueError):
            delay = 0
        return max(0, min(12000, delay))

    @staticmethod
    def _clean_candidate(value: object, *, speaker: str) -> str:
        text = str(value or "").strip().strip("`")
        for prefix in (f"{speaker}:", f"{speaker}："):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
        return text

    @staticmethod
    def _safe_fallback(
        step: ReplayStep,
        context: LegalActionContext,
        *,
        mode: str,
    ) -> str:
        actor = str(step.actor or step.speaker or "角色")
        if mode == "decision" and step.message:
            return str(step.message).strip()
        if mode == "natural":
            # A failed independent player model must not be converted into a
            # framework-authored action. Waiting is safer and more human than
            # stealing the player's agency to keep a soak test moving.
            return ""
        if mode in {"discussion", "out_of_turn"}:
            return "我先听听你们怎么想。"
        if mode == "session_zero":
            return str(step.message or "这个点子我还没想定，想先听听大家的。")
        if context.conflict_active and context.current_actor == actor:
            return f"{actor}先采取防御，留意眼前最迫近的威胁。"
        if "调查" in context.legal_actions:
            return f"{actor}观察眼前最明显的异常，想确认它会不会影响我们现在的选择。"
        return f"{actor}先稳住位置，留意现场接下来发生的变化。"

    def _update_review_state(
        self,
        mode: str,
        decision: dict[str, object],
        text: str,
    ) -> None:
        self.last_action_progress_review = {
            "engine": self.engine_name,
            "mode": mode,
            "generated": bool(text),
        }
        self.last_table_discussion_review = {
            "engine": self.engine_name,
            "mode": mode,
            "audience": str(decision.get("audience") or "").strip(),
            "pure_table_discussion": mode in {"discussion", "out_of_turn"},
        }


__all__ = [
    "DEFAULT_LONGRUN_PERSONAS",
    "LUNA_PLAYER_SYSTEM_PROMPT",
    "LunaPlayerAgent",
    "PlayerBoundaryGuard",
    "PlayerPersona",
]
