from __future__ import annotations

import os
import re
from dataclasses import dataclass

from fu_gm.config import LLMConfig
from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.prompt_cache import build_cache_friendly_messages
from fu_gm.testing.legal_actions import LegalActionLayer
from fu_gm.testing.replay_models import LegalActionContext, ReplayStep
from fu_gm.testing.rule_glossary import FINAL_FABULA_GLOSSARY, RuleGlossary


@dataclass
class SimulatedUtterance:
    text: str
    used_fallback: bool = False
    validation_errors: list[str] | None = None
    prompt_preview: str = ""


class ConstrainedPlayerSimulator:
    def __init__(
        self,
        *,
        use_llm: bool = False,
        client: OpenAICompatibleClient | None = None,
        model: str = "",
        glossary: RuleGlossary = FINAL_FABULA_GLOSSARY,
    ) -> None:
        self.glossary = glossary
        self.legal_action_layer = LegalActionLayer()
        self.client = client
        self.model = model
        if use_llm and self.client is None:
            config = LLMConfig.from_env()
            if config.api_key:
                self.client = OpenAICompatibleClient(config)
                self.model = model or os.environ.get("FU_GM_REPLAY_PLAYER_MODEL", "").strip() or config.action_model
        self.use_llm = bool(use_llm and self.client and self.model)

    def compose(
        self,
        *,
        step: ReplayStep,
        legal_context: LegalActionContext,
        last_gm_reply: str = "",
    ) -> SimulatedUtterance:
        fallback = self._fallback_utterance(step, legal_context)
        if step.message:
            errors = self.validate(step.message, step=step, legal_context=legal_context)
            if not errors:
                return SimulatedUtterance(text=step.message, used_fallback=False, validation_errors=[])
            return SimulatedUtterance(text=fallback, used_fallback=True, validation_errors=errors)
        if not self.use_llm or self.client is None:
            return SimulatedUtterance(text=fallback, used_fallback=True, validation_errors=[])

        prompt = self._build_prompt(step, legal_context, last_gm_reply)
        try:
            raw = self.client.create_chat_completion(
                model=self.model,
                messages=build_cache_friendly_messages(
                    static_system_prompt=self._system_prompt(),
                    user_content=prompt,
                ),
                temperature=0.85,
            )
        except Exception as exc:
            return SimulatedUtterance(
                text=fallback,
                used_fallback=True,
                validation_errors=[f"llm_player_error:{type(exc).__name__}"],
                prompt_preview=prompt[:1200],
            )
        text = self._clean_llm_text(raw)
        errors = self.validate(text, step=step, legal_context=legal_context)
        if errors:
            return SimulatedUtterance(
                text=fallback,
                used_fallback=True,
                validation_errors=errors,
                prompt_preview=prompt[:1200],
            )
        return SimulatedUtterance(text=text, used_fallback=False, validation_errors=[], prompt_preview=prompt[:1200])

    def validate(self, text: str, *, step: ReplayStep, legal_context: LegalActionContext) -> list[str]:
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
        if any(token in stripped for token in ["设定这里", "我设定", "新增一个事实", "这里一定有"]):
            if "消耗物语点" not in stripped and not step.kind.startswith("session_zero"):
                errors.append("world_fact_without_fabula")
        actor = step.actor
        if legal_context.conflict_active and actor and actor != legal_context.current_actor:
            consuming_words = ["攻击", "施放", "推进", "防御", "妨碍", "使用库存", "检定"]
            if any(word in stripped for word in consuming_words) and not any(
                phrase in stripped for phrase in ["等", "预备", "轮到我", "建议", "先不结算"]
            ):
                errors.append("out_of_turn_consuming_action")
        return errors

    def _system_prompt(self) -> str:
        return (
            "你是《最终物语》回放测试中的玩家模拟器，只写一条玩家发言。"
            "你不是 GM，不描述行动结果，不编骰子，不输出 JSON 或测试说明。"
        )

    def _build_prompt(self, step: ReplayStep, legal_context: LegalActionContext, last_gm_reply: str) -> str:
        legal_block = self.legal_action_layer.as_prompt_block(legal_context)
        glossary_block = self.glossary.render_for_player_prompt(legal_actions=legal_context.legal_action_names())
        return "\n\n".join(
            [
                glossary_block,
                legal_block,
                f"本步测试目标：{step.stage_goal or step.intent}",
                f"指定玩家：{step.speaker or '未指定'}",
                f"指定角色：{step.actor or '未指定'}",
                f"目标对象：{step.target or '未指定'}",
                f"方法提示：{step.method_hint or '自由选择合法表达'}",
                "额外约束：" + ("；".join(step.constraints) if step.constraints else "无"),
                "上一条 GM 回复节选：\n" + (last_gm_reply[-1200:] if last_gm_reply else "无"),
                "请输出一条自然中文玩家发言，格式建议为“玩家名：角色做什么”。",
            ]
        )

    def _fallback_utterance(self, step: ReplayStep, legal_context: LegalActionContext) -> str:
        speaker = step.speaker or "玩家"
        actor = step.actor or (legal_context.current_actor if legal_context.current_actor in legal_context.known_pcs else "")
        subject = actor or speaker
        target = step.target or self._first_clock_name(legal_context) or "当前目标"
        method = step.method_hint or "用谨慎的方式观察局面"
        intent = step.intent or step.stage_goal

        if step.kind.startswith("session_zero"):
            if "角色" in intent or "character" in intent.lower():
                return f"{speaker}: {step.method_hint or '我开始创建角色，先给出身份、主题、故乡、职业、属性和技能。'}"
            if "安全" in intent or "界限" in intent or "帷幕" in intent:
                return f"{speaker}: {step.method_hint or '我的界限是不要出现真实残酷虐待儿童的细节；帷幕是亲密场景淡出处理。'}"
            return f"{speaker}: 我补充一个共创细节：{step.method_hint or step.stage_goal or '这个世界的失落遗迹会留下风铃般的灵魂回声。'}"
        if legal_context.conflict_active and actor and actor != legal_context.current_actor:
            return f"{speaker}: {subject}先稳住位置，等轮到我时再处理【{target}】；现在我只给当前行动者一个简短建议。"
        if "物语点" in intent or "fabula" in intent.lower():
            return f"{speaker}: {subject}愿意消耗1点物语点，提出一个和当前线索有关的新事实：{method}。"
        if "仪式" in intent:
            return f"{speaker}: {subject}想尝试推进仪式【{target}】，{method}；如果需要检定，请按合适属性处理。"
        if "工程" in intent:
            return f"{speaker}: {subject}启动工程【{target}】，目标是{method}，先记录材料和需要协助的人手。"
        if "命刻" in intent or "推进" in intent:
            return f"{speaker}: {subject}尝试推进【{target}】，{method}；如果需要检定，我接受 GM 指定属性。"
        if "防御" in intent:
            return f"{speaker}: {subject}进入防御姿态，先护住队伍的破绽。"
        if "攻击" in intent:
            return f"{speaker}: {subject}用已装备武器攻击眼前最有威胁的敌人。"
        if "治疗" in intent and legal_context.legal_spells:
            spell = legal_context.legal_spells[0]
            return f"{speaker}: {subject}施放已掌握法术【{spell}】，目标是{target}。"
        if "调查" in intent:
            return f"{speaker}: {subject}先调查【{target}】，{method}；如果需要检定，我想用合适的观察或交涉方式。"
        return f"{speaker}: {subject}{method}，先看看局面会怎样变化。"

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
