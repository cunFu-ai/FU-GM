from __future__ import annotations

import json
import re
from dataclasses import asdict
from enum import Enum
from typing import Protocol

from fu_gm.components.clock_narrative_boundary import ClockNarrativeBoundary
from fu_gm.components.npc_continuity_policy import NPCCommitmentBoundary
from fu_gm.components.npc_statement_boundary import NPCStatementBoundary
from fu_gm.components.scene_moment_policy import SceneMomentPolicy
from fu_gm.components.speech_intent_boundary import SpeechIntentBoundary
from fu_gm.gm_persona import GMPersonaProfile
from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.llm_utils import extract_json_object
from fu_gm.models import Action, ActionResolution, ActionType, Affinity, RollOutcome
from fu_gm.prompt_cache import build_cache_friendly_messages
from fu_gm.prompts import EXPRESSOR_SYSTEM_PROMPT


class Expressor:
    """把已经过规则验证的结果转成面向玩家的 JRPG 叙事文本。"""

    def render(self, resolution: ActionResolution) -> str:
        return self._sanitize_player_text(self._render(resolution))

    def render_scene_moment(
        self,
        scene_packet: dict[str, object],
        *,
        instruction: str = "",
        beat: bool = False,
    ) -> str:
        """Fallback prose for scene openings/beats without routing a fake player turn."""

        location = str(scene_packet.get("location") or scene_packet.get("scene_name") or "当前地点")
        visible = [str(item) for item in scene_packet.get("visible_elements", []) if str(item)]
        npc_functions = [str(item) for item in scene_packet.get("npc_functions", []) if str(item)]
        pressure = str(scene_packet.get("current_pressure") or "")
        premise = str(scene_packet.get("premise") or "")
        if not SceneMomentPolicy.is_player_facing_fact(pressure):
            pressure = ""
        if not SceneMomentPolicy.is_player_facing_fact(premise):
            premise = ""
        prepared_npcs = [
            dict(item)
            for item in (scene_packet.get("prepared_npcs") or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        scene_details = [
            self._scene_detail_text(item)
            for item in visible
            if not item.startswith(("地点：", "在场英雄："))
            and SceneMomentPolicy.is_player_facing_fact(item)
        ]
        scene_details = [item for item in scene_details if item]
        if beat:
            # A proactive beat must add an enacted change. Raw scene fields are
            # planning notes, not player-facing prose; if the expression model
            # cannot produce a valid beat, silence is safer than reciting them.
            return ""
        else:
            parts = []
            if scene_details:
                parts.append(f"{location}里，{scene_details[0]}")
                parts.extend(scene_details[1:2])
            else:
                parts.append(f"{location}里，{premise}" if premise else f"镜头落在{location}")
            if prepared_npcs:
                first = prepared_npcs[0]
                name = str(first.get("name") or "").strip()
                role = str(first.get("public_role") or "").strip()
                parts.append(f"{name}就在现场" + (f"，负责{role}" if role else ""))
            parts.append(pressure)
        clean_parts = [part.strip().rstrip("。！？；;") for part in parts if part.strip()]
        rendered = "。".join(clean_parts) + ("。" if clean_parts else "")
        return re.sub(r"([。！？])([’”」』])。", r"\1\2", rendered)

    def _scene_detail_text(self, item: str) -> str:
        text = str(item or "").strip()
        if "：" in text:
            text = text.split("：", 1)[1].strip()
        return text

    def _scene_npc_beat(self, npc_functions: list[str], instruction: str) -> str:
        if not npc_functions:
            return ""
        selected = ""
        instruction_text = str(instruction or "")
        for item in npc_functions:
            role = item.split("：", 1)[0]
            if role and role in instruction_text:
                selected = item
                break
            if "守门" in instruction_text and "守门" in item:
                selected = item
                break
        if not selected:
            return ""
        role, _, detail = selected.partition("：")
        subject = re.split(r"[，,；;]", detail, maxsplit=1)[0].strip() or role
        if "守门" in role:
            return "守门人把压在掌下的旧路钥匙推到桌沿：‘目的地说清楚，旅人由你们亲自护送，我就带你们开门。’"
        if "受压" in role:
            return "失忆旅人的呼吸忽然乱了一拍。他盯着北侧旧路，低声说：‘第二遍铃声响起前，带我离开这里。’"
        if "对立" in role:
            return f"{subject}不再旁观，开始把自己的意图付诸行动"
        return f"{subject}终于开口，让现场出现了一个必须回应的新条件"

    def _render(self, resolution: ActionResolution) -> str:
        action = resolution.action
        if resolution.payload.get("check_transaction_replayed") or resolution.payload.get(
            "check_transaction_accepted"
        ):
            return self._render_committed_check_transaction(resolution)
        if resolution.payload.get("check_result_provisional"):
            roll = resolution.payload.get("roll")
            if isinstance(roll, RollOutcome):
                result_text = "成功" if roll.success else "失败"
                special = "，大成功" if roll.critical_success else ("，大失败" if roll.fumble else "")
                return f"{roll.actor}：{self._roll_process_text(roll)}，{result_text}{special}！"
        # in_mind_reply comes from the action-routing pass before dice are rolled.
        # Short table-side comments belong to the final expression pass, where
        # the true success/failure and clock/resource changes are already known.
        mood = ""
        if resolution.payload.get("team_assist_registered"):
            return f"【团队协作】{resolution.rules_text}\n{mood}".strip()
        if resolution.payload.get("out_of_turn"):
            return f"【回合意图】{resolution.rules_text}\n{mood}".strip()
        if action.action_type == ActionType.NPCACT:
            npc_action_type = action.parameters.get("npc_action_type")
            if npc_action_type == "Spell" and resolution.payload.get("spell_failed"):
                return f"【敌方施法失败】{resolution.rules_text}\n{mood}".strip()
            if npc_action_type == "Spell" and "spell_effect" in resolution.payload:
                return self._render_spell_effect(resolution, mood, enemy_cast=True)
            if npc_action_type in {"Attack", "Spell", "Hinder"} and "roll" in resolution.payload:
                return self._render_roll(resolution, mood)
            if npc_action_type == "Guard":
                return f"【敌方防御】{resolution.rules_text}\n{mood}".strip()
            if npc_action_type == "Investigate":
                return self._render_investigation(resolution, mood)
            if npc_action_type == "Objective":
                return self._render_objective(resolution, mood)
            if npc_action_type == "UltimaRecover":
                body = [f"【终结点恢复】{resolution.rules_text}"]
                conflict_event = resolution.payload.get("conflict_event")
                if conflict_event is not None and conflict_event.mp_after is not None:
                    body.append(f"{conflict_event.target} 当前 MP：{conflict_event.mp_after}。")
                if mood:
                    body.append(mood)
                return "\n".join(body)
            return f"【敌方行动】{resolution.rules_text}\n{mood}".strip()

        if action.action_type == ActionType.SPELL and resolution.payload.get("spell_parameter_required"):
            return str(resolution.rules_text or "请先补充法术所需的目标或效果选择。").strip()
        if action.action_type == ActionType.SPELL and resolution.payload.get("spell_failed"):
            return f"【施法失败】{resolution.rules_text}\n{mood}".strip()
        if action.action_type == ActionType.SPELL and "spell_effect" in resolution.payload:
            return self._render_spell_effect(resolution, mood)
        if action.action_type == ActionType.SPELL and (
            "healing_change" in resolution.payload
            or "healing_changes" in resolution.payload
            or "statuses_cleared" in resolution.payload
            or "statuses_cleared_targets" in resolution.payload
            or "fixed_hp_loss" in resolution.payload
            or "action_penalty_targets" in resolution.payload
            or "narrative_spell" in resolution.payload
            or "dispelled_effects" in resolution.payload
        ):
            return self._render_static_spell_resolution(resolution, mood)
        if action.action_type == ActionType.ATTACK and resolution.payload.get("multi_target"):
            return self._render_multi_attack(resolution, mood)
        if action.action_type == ActionType.SKILL:
            return f"【技能】{resolution.rules_text}\n{mood}".strip()
        if action.action_type == ActionType.USE_INVENTORY:
            return self._render_inventory_use(resolution, mood)
        if action.action_type == ActionType.TINKERER_GADGET:
            return self._render_tinkerer_gadget(resolution, mood)
        if action.action_type == ActionType.SHOP:
            return self._render_shop(resolution, mood)
        if action.action_type == ActionType.REST:
            return str(resolution.rules_text or "队伍完成了休息。").strip()
        if action.action_type == ActionType.OPEN_CHEST:
            return self._render_chest(resolution, mood)
        if action.action_type == ActionType.AWARD_REWARD:
            return self._render_reward(resolution, mood)
        if action.action_type == ActionType.EXPLORE_DUNGEON:
            return self._render_dungeon_exploration(resolution, mood)
        if action.action_type == ActionType.NEXT_TURN:
            return self._render_next_turn(resolution, mood)
        if action.action_type in {ActionType.ATTACK, ActionType.SPELL, ActionType.REQUEST_ROLL, ActionType.HINDER}:
            return self._render_roll(resolution, mood)
        if action.action_type == ActionType.GUARD:
            body = [f"【防御】{resolution.rules_text}"]
            self._append_auto_turn_advance(body, resolution)
            if mood:
                body.append(mood)
            return "\n".join(body)
        if action.action_type == ActionType.INVESTIGATE:
            return self._render_investigation(resolution, mood)
        if action.action_type == ActionType.OBJECTIVE:
            return self._render_objective(resolution, mood)
        if action.action_type == ActionType.PLAN_RITUAL:
            return self._render_ritual_plan(resolution, mood)
        if action.action_type == ActionType.CONTRIBUTE_RITUAL:
            return self._render_ritual_contribution(resolution, mood)
        if action.action_type == ActionType.CAST_RITUAL:
            return self._render_ritual_cast(resolution, mood)
        if action.action_type == ActionType.START_PROJECT:
            return self._render_project_start(resolution, mood)
        if action.action_type == ActionType.HIRE_PROJECT_HELPERS:
            return self._render_project_helpers(resolution, mood)
        if action.action_type == ActionType.WORK_PROJECT:
            return self._render_project_progress(resolution, mood)
        if action.action_type == ActionType.MODIFY_RESOURCE:
            return f"【资源变化】{resolution.rules_text}\n{mood}".strip()
        if action.action_type == ActionType.ADVANCE_CLOCK:
            return f"【命刻推进】{resolution.rules_text}\n{mood}".strip()
        if action.action_type in {
            ActionType.INVOKE_TRAIT,
            ActionType.INVOKE_BOND,
            ActionType.TRIGGER_OPPORTUNITY,
            ActionType.RESOLVE_DECISION,
            ActionType.RESOLVE_ZERO_HP,
        }:
            return str(resolution.rules_text or "").strip()
        if action.action_type == ActionType.ACCEPT_STORY_CHANGE:
            fact = resolution.payload["fact"]
            body = [str(fact).strip()]
            followup_intent = str(resolution.payload.get("followup_intent") or "").strip()
            if followup_intent:
                body.append(f"{followup_intent}，会成为下一步镜头焦点。")
            if mood:
                body.append(mood)
            return "\n".join(line for line in body if line)
        if action.action_type == ActionType.NARRATE:
            body = [str(action.parameters.get("summary") or resolution.rules_text or "场景继续推进。").strip()]
            self._append_auto_turn_advance(body, resolution)
            return "\n".join(line for line in body if line)
        return f"【叙事】{action.parameters.get('summary', resolution.rules_text)}\n{mood}".strip()

    def _render_committed_check_transaction(self, resolution: ActionResolution) -> str:
        source_action = resolution.payload.get("committed_source_action")
        if not isinstance(source_action, Action):
            return str(resolution.rules_text or "").strip()
        payload = dict(resolution.payload)
        for key in (
            "check_transaction_replayed",
            "check_transaction_accepted",
            "check_transaction_invocation_text",
            "check_transaction_acceptance_text",
        ):
            payload.pop(key, None)
        source_resolution = ActionResolution(
            action=source_action,
            rules_text=str(resolution.rules_text or ""),
            payload=payload,
        )
        rendered = self._render(source_resolution).strip()
        prefix = str(
            resolution.payload.get("check_transaction_invocation_text")
            or resolution.payload.get("check_transaction_acceptance_text")
            or ""
        ).strip()
        # Rollback/replay is an implementation detail. At the table, only say
        # what was invoked and what the final authoritative result is.
        prefix = prefix.replace("；旧结果已回滚并重新提交。", "。").replace(
            "；旧结果已回滚并重新提交", ""
        )
        return "\n".join(item for item in (prefix, rendered) if item)

    def _sanitize_player_text(self, text: str) -> str:
        text = self._humanize_internal_terms(str(text or ""))
        text = re.sub(
            r"(?m)^\s*【("
            r"团队协作|回合意图|敌方施法失败|敌方防御|终结点恢复|敌方行动|"
            r"检定|战斗结算|"
            r"施法失败|技能|防御|资源变化|命刻推进|叙事|调查|目标行动|协同推进|"
            r"敌方法术|法术|多目标攻击|库存道具|造物使便携装置|商店|宝箱|"
            r"阶段奖励|地下城探索|回合推进|仪式启动|仪式设计|仪式推进|"
            r"仪式等待|仪式结算|项目启动|项目帮手|项目推进"
            r")】",
            "",
            text,
        )
        cleaned_lines: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if self._is_meta_line(stripped):
                continue
            cleaned_lines.append(line.rstrip())
        return "\n".join(cleaned_lines).strip()

    def _humanize_internal_terms(self, text: str) -> str:
        replacements = {
            "伊莉莉雅": "伊莉雅",
            "伊莉莉娅": "伊莉雅",
            "旅旅人": "旅人",
            "SellItem": "出售物品",
            "硬状态": "已确认状态",
            "公开硬状态": "当前已确认状态",
            "保持冲突继续": "冲突还在继续",
            "GM应回应": "时悠会回应",
            "GM 应回应": "时悠会回应",
            "GM应接住": "先接住",
            "GM 应接住": "先接住",
            "硬成本": "实际花费",
            "硬数值": "数值",
            "硬结算": "规则结算",
            "暂时没有执行明确动作": "没有立刻采取会改变局势的动作",
            "硬规则": "规则",
            "规则层": "规则",
        }
        for source, target in replacements.items():
            text = text.replace(source, target)
        text = re.sub(r"[，,；;]?\s*这一步的(?:重点|目的|意义)[^。！？\n]*(?:[。！？]|$)", "。", text)
        text = re.sub(r"[，,；;]?\s*这(?:个|次)动作的(?:重点|目的|意义)[^。！？\n]*(?:[。！？]|$)", "。", text)
        text = re.sub(r"(?:他|她|他们|她们|[^\s，。；;]{1,12})?没有急着替任何人做决定[，,；;]?\s*", "", text)
        text = re.sub(r"GM\s*应", "接下来要", text)
        text = re.sub(r"当前已确认状态显示", "现在已经确认", text)
        text = re.sub(r"当前公开已确认状态显示", "现在已经确认", text)
        text = re.sub(r"当前已确认状态", "当前状态", text)
        return text

    def _is_meta_line(self, line: str) -> bool:
        if re.search(r"像一[枚颗粒][^。\n]*钉子|钉住[^。\n]*(?:选择|设定|世界|创作)", line):
            return True
        meta_markers = (
            "新的事实：",
            "规则面板",
            "结构化结算数据",
            "LLM",
            "后台",
            "后端",
            "系统代码",
            "未执行任何硬数值结算",
            "已记录这条机会偏好",
            "玩家的共同创作",
            "共同创作固定",
            "这条新设定像",
            "一枚沉静的钉子",
            "场景物件或线索目标",
            "不是已建档敌人",
            "不是已建档角色",
            "可用调查、目标行动",
            "世界设定已更新",
            "物语改写",
            "ActionType",
            "npc_action_type",
        )
        return any(marker in line for marker in meta_markers)

    def _render_roll(self, resolution: ActionResolution, mood: str) -> str:
        roll: RollOutcome = resolution.payload["roll"]
        result_text = "成功" if roll.success else "失败"

        special = ""
        if roll.critical_success:
            special = " 大成功！"
        elif roll.fumble:
            special = " 大失败！"

        body: list[str] = []
        if resolution.payload.get("acceleration_benefit_used"):
            body.append("【加速术】触发。")
        npc_intent = self._npc_action_intent_text(resolution, roll)
        if npc_intent:
            body.append(npc_intent)
        target_text = self._roll_target_text(resolution, roll)
        body.append(
            (
                f"【{self._roll_panel_label(resolution, roll)}】{roll.actor}{target_text}："
                f"{self._roll_process_text(roll)}，{result_text}！{special}"
            ).strip(),
        )

        if not roll.success:
            consequence = str(
                resolution.action.parameters.get("failure_consequence")
                or resolution.action.parameters.get("failure_stakes")
                or ""
            ).strip()
            if consequence:
                body.append(f"{consequence.rstrip('。')}。")

        if roll.success and resolution.action.parameters.get("scene_check_planned"):
            success_observation = str(
                resolution.action.parameters.get("success_observation")
                or resolution.action.parameters.get("success_answer")
                or ""
            ).strip()
            if success_observation:
                body.append(f"{success_observation.rstrip('。')}。")

        if roll.success and roll.hp_after is not None:
            affinity_text = self._affinity_text(roll.applied_affinity)
            body.append(
                f"对 {roll.target} 造成 {roll.damage} 点{self._damage_type_text(roll.damage_type)}伤害。"
                f"{affinity_text} 目标剩余 HP：{roll.hp_after}。"
            )
        if roll.opportunity_count:
            body.append(f"你获得 {roll.opportunity_count} 次机会。")
        if "fabula_gain" in resolution.payload:
            fabula_gain = resolution.payload["fabula_gain"]
            body.append(
                f"{fabula_gain.target} 获得 {fabula_gain.amount} 点物语点，"
                f"当前为 {fabula_gain.after} 点。"
            )
        if "resource_change" in resolution.payload:
            resource_change = resolution.payload["resource_change"]
            if resource_change.resource == "mp" and resource_change.amount < 0:
                body.append(
                    f"{resource_change.target} 消耗 {abs(resource_change.amount)} 点 MP，"
                    f"消耗前 {resource_change.before}，剩余 {resource_change.after}。"
                )
        if "clock_change" in resolution.payload:
            clock_change = resolution.payload["clock_change"]
            body.append(self._clock_change_text(clock_change))
            explanation = self._clock_delta_explanation(roll, clock_change)
            if explanation:
                body.append(explanation)
        if "conflict_event" in resolution.payload:
            conflict_event = resolution.payload["conflict_event"]
            body.append(conflict_event.summary)
            if (
                conflict_event.fabula_awarded
                and conflict_event.event_type == "pc_give_up_resistance"
            ):
                body.append(
                    f"{conflict_event.target} 获得 {conflict_event.fabula_awarded} 点物语点。"
                )
            if conflict_event.stage_name:
                body.append(f"{conflict_event.target} 进入新阶段：{conflict_event.stage_name}。")
            if conflict_event.consequence:
                body.append(f"代价：{conflict_event.consequence}。")
        if "hinder_status" in resolution.payload and resolution.payload["roll"].success:
            hinder_status = resolution.payload["hinder_status"]
            if resolution.payload.get("status_applied", False):
                body.append(f"{resolution.payload['roll'].target} 被施加了{self._status_name(hinder_status)}。")
        for target, status in resolution.payload.get("on_hit_statuses", {}).items():
            if resolution.payload.get("status_applied_on_hit", {}).get(target):
                body.append(f"{target} 被追加施加了{self._status_name(status)}。")
        for reaction in resolution.payload.get("reaction_events", []):
            if reaction.get("rules_text"):
                body.append(reaction["rules_text"])
        self._append_auto_turn_advance(body, resolution)

        npc_failure = self._npc_failed_action_text(resolution, roll)
        if npc_failure:
            body.append(npc_failure)
        elif mood:
            body.append(mood)
        return "\n".join(body)

    def _roll_target_text(self, resolution: ActionResolution, roll: RollOutcome) -> str:
        label = str(
            resolution.action.parameters.get("scene_investigation_label") or ""
        ).strip()
        if resolution.action.parameters.get("scene_check_planned") and label:
            return f"进行{label}检定"
        scope = str(
            resolution.action.parameters.get("scene_investigation_scope")
            or resolution.action.parameters.get("investigation_scope")
            or ""
        ).strip()
        generic_targets = {"", "当前目标", "当前线索", "当前对象", "周边环境", "旧路周边环境", "夜间周边环境"}
        if scope == "environment" or str(roll.target or "") in generic_targets:
            label = str(
                resolution.action.parameters.get("scene_investigation_label")
                or resolution.action.parameters.get("reason")
                or "观察周边环境"
            ).strip()
            return f"进行{label}检定"
        return f" 对 {roll.target} 的检定"

    def _roll_panel_label(self, resolution: ActionResolution, roll: RollOutcome) -> str:
        action_type = resolution.action.action_type
        if action_type == ActionType.HINDER:
            return "妨碍"
        if action_type in {ActionType.ATTACK, ActionType.SPELL}:
            return "战斗结算"
        if roll.hp_after is not None:
            return "战斗结算"
        return "检定"

    def _render_multi_attack(self, resolution: ActionResolution, mood: str) -> str:
        body = [f"【多目标攻击】{resolution.rules_text}"]
        for roll in resolution.payload.get("rolls", []):
            result = "命中" if roll.success else "未命中"
            line = f"{roll.actor} -> {roll.target}: {self._roll_process_text(roll)}，{result}"
            if roll.success:
                line += f"，造成 {roll.damage} 点{self._damage_type_text(roll.damage_type)}伤害，目标剩余 HP：{roll.hp_after}"
            body.append(line + "。")
        for reaction in resolution.payload.get("reaction_events", []):
            if reaction.get("rules_text"):
                body.append(reaction["rules_text"])
        if "fabula_gain" in resolution.payload:
            fabula_gain = resolution.payload["fabula_gain"]
            body.append(f"{fabula_gain.target} 获得 {fabula_gain.amount} 点物语点，当前为 {fabula_gain.after} 点。")
        for conflict_event in resolution.payload.get("conflict_events", []):
            body.append(conflict_event.summary)
        for target, status in resolution.payload.get("on_hit_statuses", {}).items():
            if resolution.payload.get("status_applied_on_hit", {}).get(target):
                body.append(f"{target} 被追加施加了{self._status_name(status)}。")
        self._append_auto_turn_advance(body, resolution)
        if mood:
            body.append(mood)
        return "\n".join(body)

    def _render_inventory_use(self, resolution: ActionResolution, mood: str) -> str:
        body = [f"【库存道具】{resolution.rules_text}"]
        result = resolution.payload.get("inventory_result")
        if result is not None:
            self._append_resource_and_damage_results(body, result, healing_aware=True)
        if mood:
            body.append(mood)
        return "\n".join(body)

    def _render_tinkerer_gadget(self, resolution: ActionResolution, mood: str) -> str:
        body = [f"【造物使便携装置】{resolution.rules_text}"]
        result = resolution.payload.get("gadget_result")
        if result is not None:
            if getattr(result, "ip_change", None) is not None:
                change = result.ip_change
                body.append(f"{change.target} 的 IP：{change.before} -> {change.after}。")
            self._append_resource_and_damage_results(body, result)
        nested = resolution.payload.get("nested_resolution")
        if nested is not None:
            body.append(f"联动结算：{nested.rules_text}")
        if mood:
            body.append(mood)
        return "\n".join(body)

    def _append_resource_and_damage_results(self, body: list[str], result, *, healing_aware: bool = False) -> None:
        for change in getattr(result, "resource_changes", []):
            body.append(f"{change.target} 的 {change.resource.upper()}：{change.before} -> {change.after}。")
        for damage in getattr(result, "damage_results", []):
            if healing_aware and damage.get("healing"):
                body.append(f"{damage['target']} 吸收效果，HP：{damage['hp_before']} -> {damage['hp_after']}。")
                continue
            body.append(
                f"{damage['target']} 受到 {damage['damage']} 点"
                f"{self._damage_type_text(damage['damage_type'])}伤害，"
                f"HP：{damage['hp_before']} -> {damage['hp_after']}。"
            )
        body.extend(getattr(result, "status_changes", []))

    def _render_shop(self, resolution: ActionResolution, mood: str) -> str:
        transaction = resolution.payload.get("shop_transaction")
        body = [f"【商店】{resolution.rules_text}"]
        if transaction is not None:
            actor = getattr(transaction, "actor", None) or getattr(transaction, "payer", None) or getattr(transaction, "buyer", "")
            body.append(f"{actor} 的资金：{transaction.zenit_before}Z -> {transaction.zenit_after}Z。")
            item_name = (
                getattr(transaction, "item_name", None)
                or getattr(transaction, "service_name", None)
                or getattr(transaction, "transport_name", "")
            )
            if item_name == "库存点补充":
                body.append(f"库存点：{transaction.ip_before} -> {transaction.ip_after}。")
        if mood:
            body.append(mood)
        return "\n".join(body)

    def _render_chest(self, resolution: ActionResolution, mood: str) -> str:
        reward = resolution.payload.get("chest_reward")
        body = [f"【宝箱】{resolution.rules_text}"]
        if reward is not None and reward.rare_items:
            body.append(f"稀有物品：{'、'.join(reward.rare_items)}。")
        if mood:
            body.append(mood)
        return "\n".join(body)

    def _render_reward(self, resolution: ActionResolution, mood: str) -> str:
        reward = resolution.payload.get("session_reward")
        body = [f"【阶段奖励】{resolution.rules_text}"]
        if reward is not None and reward.rare_items:
            body.append(f"稀有奖励：{'、'.join(reward.rare_items)}。")
        if mood:
            body.append(mood)
        return "\n".join(body)

    def _render_dungeon_exploration(self, resolution: ActionResolution, mood: str) -> str:
        result = resolution.payload.get("dungeon_exploration")
        body = [f"【地下城探索】{resolution.rules_text}"]
        if result is not None:
            if getattr(result, "danger_change", None) is not None:
                change = result.danger_change
                body.append(
                    f"危险命刻【{change.clock_name}】：{change.before}/{change.max_segments} -> "
                    f"{change.after}/{change.max_segments}。"
                )
            if getattr(result, "exits", None):
                body.append(f"可前往：{'、'.join(result.exits)}。")
            if getattr(result, "boss_revealed", False):
                body.append("Boss 房已揭示，可以切入首领战或最终目标命刻。")
        reward = resolution.payload.get("chest_reward")
        if reward is not None:
            body.append(f"宝箱奖励：{reward.zenit}Z。")
            if reward.items:
                body.append(f"获得物品：{'、'.join(reward.items)}。")
            if reward.rare_items:
                body.append(f"获得稀有物品：{'、'.join(reward.rare_items)}。")
        if mood:
            body.append(mood)
        return "\n".join(body)

    def _render_next_turn(self, resolution: ActionResolution, mood: str) -> str:
        body = [f"【回合推进】{resolution.rules_text}"]
        turn_board = resolution.payload.get("turn_board") or {}
        if turn_board:
            waiting = turn_board.get("waiting") or []
            acted = turn_board.get("acted") or []
            if waiting:
                body.append(f"待行动：{'、'.join(waiting)}。")
            if acted:
                body.append(f"本轮已行动：{'、'.join(acted)}。")
            acted_this_round = turn_board.get("acted_this_round") or []
            if acted_this_round:
                body.append(f"本轮已消耗行动：{'、'.join(acted_this_round)}。")
            pending_assists = turn_board.get("pending_assists") or {}
            if pending_assists:
                assist_text = [
                    f"{leader} <= {', '.join(helpers)}"
                    for leader, helpers in pending_assists.items()
                    if helpers
                ]
                if assist_text:
                    body.append("待结算协助：" + "；".join(assist_text) + "。")
            held_actions = turn_board.get("held_actions") or []
            if held_actions:
                body.append(f"暂缓动作：{len(held_actions)} 条。")
        if resolution.payload.get("bonus_turn"):
            body.append("当前是奖励/额外行动窗口。")
        if resolution.payload.get("queued_turns"):
            body.append(f"待处理额外行动：{'、'.join(resolution.payload['queued_turns'])}。")
        if resolution.payload.get("combat_log"):
            body.append("最近战斗日志：" + " / ".join(resolution.payload["combat_log"][-3:]))
        if mood:
            body.append(mood)
        return "\n".join(body)

    def _render_investigation(self, resolution: ActionResolution, mood: str) -> str:
        roll: RollOutcome = resolution.payload["roll"]
        result_text = "成功" if roll.success else "失败"
        special = ""
        if roll.critical_success:
            special = " 大成功！"
        elif roll.fumble:
            special = " 大失败！"
        intro = self._investigation_intro_text(resolution, roll)
        body: list[str] = []
        body.append((f"{intro}{self._roll_process_text(roll)}，{result_text}！{special}").strip())
        if not roll.success:
            consequence = str(
                resolution.action.parameters.get("failure_consequence")
                or resolution.action.parameters.get("failure_stakes")
                or ""
            ).strip()
            if consequence:
                body.append(f"{consequence.rstrip('。')}。")
        for info in resolution.payload.get("information", []):
            body.append(info)
        if roll.opportunity_count:
            body.append(f"你获得 {roll.opportunity_count} 次机会。")
        if "clock_change" in resolution.payload:
            clock_change = resolution.payload["clock_change"]
            auto_changed_names = {
                str(getattr(change, "clock_name", "") or "")
                for change in resolution.payload.get("auto_clock_changes") or []
            }
            if str(getattr(clock_change, "clock_name", "") or "") not in auto_changed_names:
                body.append(self._clock_change_text(clock_change))
            explanation = self._clock_delta_explanation(roll, clock_change)
            if explanation:
                body.append(explanation)
        self._append_auto_turn_advance(body, resolution)
        if mood:
            body.append(mood)
        return "\n".join(body)

    def _investigation_intro_text(self, resolution: ActionResolution, roll: RollOutcome) -> str:
        label = str(
            resolution.action.parameters.get("scene_investigation_label") or ""
        ).strip()
        if resolution.action.parameters.get("scene_check_planned") and label:
            return f"【调查】{roll.actor}{label}："
        scope = str(resolution.action.parameters.get("scene_investigation_scope") or "").strip()
        if scope == "environment":
            label = str(resolution.action.parameters.get("scene_investigation_label") or "观察周边环境").strip()
            return f"【调查】{roll.actor}{label}："
        scene_object = str(resolution.payload.get("scene_object") or "").strip()
        if scene_object in {"周边环境", "旧路周边环境", "夜间周边环境"}:
            return f"【调查】{roll.actor}观察周边环境："
        return f"【调查】{roll.actor} 对 {roll.target} 的检定："

    def _render_objective(self, resolution: ActionResolution, mood: str) -> str:
        roll: RollOutcome | None = resolution.payload.get("roll")
        title = "协同推进" if resolution.payload.get("cooperative_progress") or resolution.action.parameters.get("cooperative_progress") else "目标行动"
        if roll is not None:
            result_text = "成功" if roll.success else "失败"
            special = ""
            if roll.critical_success:
                special = " 大成功！"
            elif roll.fumble:
                special = " 大失败！"
            body: list[str] = []
            npc_intent = self._npc_action_intent_text(resolution, roll)
            if npc_intent:
                body.append(npc_intent)
            check_label = str(
                resolution.action.parameters.get("scene_investigation_label") or ""
            ).strip()
            actor_and_action = f"{roll.actor}{check_label}" if check_label else roll.actor
            body.append(
                (
                    f"【{title}】{actor_and_action}：{self._roll_process_text(roll)}，"
                    f"{result_text}！{special}"
                ).strip()
            )
            if roll.opportunity_count:
                body.append(f"你获得 {roll.opportunity_count} 次机会。")
            teamwork = resolution.payload.get("conflict_teamwork") or {}
            if teamwork:
                body.append(f"团队合作提供 +{teamwork.get('total_bonus', 0)} 修正。")
            if resolution.payload.get("clock_direction_corrected"):
                body.append("本次成功用于压制威胁命刻，因此按规则擦除威胁进度，而不是推进威胁。")
            if not roll.success:
                failure = str(
                    resolution.action.parameters.get("failure_consequence")
                    or resolution.action.parameters.get("failure_stakes")
                    or ""
                ).strip()
                if failure:
                    body.append(f"{failure.rstrip('。')}。")
            elif resolution.action.parameters.get("scene_check_planned"):
                success = str(
                    resolution.action.parameters.get("success_observation")
                    or resolution.action.parameters.get("success_answer")
                    or ""
                ).strip()
                if success:
                    body.append(f"{success.rstrip('。')}。")
        else:
            body = [f"【{title}】{resolution.rules_text}"]
        if "clock_change" in resolution.payload:
            clock_change = resolution.payload["clock_change"]
            body.append(self._clock_change_text(clock_change))
            explanation = self._clock_delta_explanation(roll, clock_change)
            if explanation:
                body.append(explanation)
        elif "clock_state" in resolution.payload:
            state_text = self._clock_state_text(resolution.payload["clock_state"])
            if state_text:
                body.append(state_text)
        self._append_auto_turn_advance(body, resolution)
        npc_failure = self._npc_failed_action_text(resolution, roll)
        if npc_failure:
            body.append(npc_failure)
        elif mood:
            body.append(mood)
        return "\n".join(body)

    def _render_spell_effect(self, resolution: ActionResolution, mood: str, enemy_cast: bool = False) -> str:
        title = "【敌方法术】" if enemy_cast else "【法术】"
        body = [f"{title}{resolution.rules_text}"]
        resource_change = resolution.payload.get("resource_change")
        if resource_change is not None and resource_change.amount < 0:
            body.append(
                f"{resource_change.target} 消耗 {abs(resource_change.amount)} 点 MP，"
                f"消耗前 {resource_change.before}，剩余 {resource_change.after}。"
            )
        spell_effect = resolution.payload.get("spell_effect")
        if spell_effect is not None:
            effects = spell_effect if isinstance(spell_effect, list) else [spell_effect]
            timings = list(
                dict.fromkeys(
                    self._effect_timing_text(effect.expires_on)
                    for effect in effects
                    if getattr(effect, "expires_on", None) is not None
                )
            )
            if timings:
                body.append(f"持续时机：{'、'.join(timings)}。")
        if mood:
            body.append(mood)
        return "\n".join(body)

    def _render_static_spell_resolution(self, resolution: ActionResolution, mood: str) -> str:
        body = [f"【法术】{resolution.rules_text}"]
        resource_change = resolution.payload.get("resource_change")
        if resource_change is not None and resource_change.amount < 0:
            body.append(
                f"{resource_change.target} 消耗 {abs(resource_change.amount)} 点 MP，"
                f"消耗前 {resource_change.before}，剩余 {resource_change.after}。"
            )

        fixed_effect = resolution.payload.get("spell_fixed_effect") or {}
        if "healing_change" in resolution.payload:
            change = resolution.payload["healing_change"]
            base_amount = fixed_effect.get("base_amount", change.amount)
            body.append(
                f"{change.target} HP：{change.before} -> {change.after}；"
                f"规则恢复量 {base_amount}，实际恢复 {change.amount}。"
            )
        for change in resolution.payload.get("healing_changes", []):
            base_amount = fixed_effect.get("base_amount", change.amount)
            body.append(
                f"{change.target} HP：{change.before} -> {change.after}；"
                f"规则恢复量 {base_amount}，实际恢复 {change.amount}。"
            )
        fixed_hp_loss = resolution.payload.get("fixed_hp_loss")
        if fixed_hp_loss is not None:
            body.append(f"{fixed_hp_loss.target} HP：{fixed_hp_loss.before} -> {fixed_hp_loss.after}。")
        if resolution.payload.get("statuses_cleared") is not None:
            body.append("异常状态清除结算已按硬规则处理。")
        if resolution.payload.get("statuses_cleared_targets") is not None:
            targets = resolution.payload.get("statuses_cleared_targets") or []
            body.append(f"已清除异常的目标：{'、'.join(targets) if targets else '无'}。")
        if resolution.payload.get("dispelled_effects"):
            body.append(f"驱散效果：{'、'.join(resolution.payload['dispelled_effects'])}。")
        if resolution.payload.get("action_penalty_targets"):
            body.append(f"行动惩罚目标：{'、'.join(resolution.payload['action_penalty_targets'])}。")
        if mood:
            body.append(mood)
        return "\n".join(body)

    def _render_ritual_plan(self, resolution: ActionResolution, mood: str) -> str:
        plan = resolution.payload["ritual_plan"]
        roll: RollOutcome | None = resolution.payload.get("roll")
        if roll is not None:
            result_text = "成功" if roll.success else "失败"
            special = ""
            if roll.critical_success:
                special = " 大成功！"
            elif roll.fumble:
                special = " 大失败！"
            body = [
                (
                    f"【仪式启动】{roll.actor}：{self._roll_process_text(roll)}，{result_text}。"
                    f"{special}"
                ).strip(),
                resolution.rules_text,
            ]
        else:
            body = [f"【仪式设计】{resolution.rules_text}"]
        body.append(
            f"学科：{self._ritual_discipline_text(plan.discipline)}；"
            f"检定：【{self._attributes_text(plan.attributes)}】；MP 消耗：{plan.mp_cost}；难度等级：{plan.target_number}。"
        )
        if plan.rare_material:
            body.append(f"稀有媒介：{plan.rare_material}，MP 消耗已减半。")
        if "clock_change" in resolution.payload:
            clock_change = resolution.payload["clock_change"]
            body.append(self._clock_change_text(clock_change))
            explanation = self._clock_delta_explanation(roll, clock_change)
            if explanation:
                body.append(explanation)
        self._append_auto_turn_advance(body, resolution)
        if mood:
            body.append(mood)
        return "\n".join(body)

    def _render_ritual_contribution(self, resolution: ActionResolution, mood: str) -> str:
        roll: RollOutcome = resolution.payload["roll"]
        clock_change = resolution.payload["clock_change"]
        result_text = "成功" if roll.success else "失败"
        special = ""
        if roll.critical_success:
            special = " 大成功！"
        elif roll.fumble:
            special = " 大失败！"
        body = [
            (
                f"【仪式推进】{roll.actor}：{self._roll_process_text(roll)}，{result_text}。"
                f"{special}"
            ).strip(),
            (
                self._clock_change_text(clock_change)
            ),
        ]
        explanation = self._clock_delta_explanation(roll, clock_change)
        if explanation:
            body.append(explanation)
        if roll.opportunity_count:
            if roll.critical_success:
                body.append(f"本次仪式推进产生 {roll.opportunity_count} 次机会；这是明显的叙事高光，请把它当成意外转折来描写。")
            else:
                body.append(f"本次仪式推进产生 {roll.opportunity_count} 次机会。")
        if mood:
            body.append(mood)
        return "\n".join(body)

    def _render_ritual_cast(self, resolution: ActionResolution, mood: str) -> str:
        if resolution.payload.get("ritual_waiting"):
            body = [f"【仪式等待】{resolution.rules_text}"]
            if mood:
                body.append(mood)
            return "\n".join(body)
        result = resolution.payload["ritual_result"]
        plan = result.plan
        body = [f"【仪式结算】{resolution.rules_text}"]
        if result.mp_change is not None:
            body.append(
                f"{result.mp_change.target} 消耗 {abs(result.mp_change.amount)} 点 MP，"
                f"消耗前 {result.mp_change.before}，剩余 {result.mp_change.after}。"
            )
        if result.roll is not None:
            special = ""
            if result.roll.critical_success:
                special = " 大成功！"
            elif result.roll.fumble:
                special = " 大失败！"
            body.append(
                f"仪式检定：{self._roll_process_text(result.roll)}。{special}".strip()
            )
        if result.success:
            body.append(f"仪式效果：{plan.effect}")
            if "persistence" in resolution.payload:
                body.append(f"长期变化：{self._persistent_change_text(resolution.payload['persistence'])}")
        elif result.catastrophe:
            body.append(f"灾变后果：{result.catastrophe}")
        if mood:
            body.append(mood)
        return "\n".join(body)

    def _render_project_start(self, resolution: ActionResolution, mood: str) -> str:
        project = resolution.payload["project"]
        body = [f"【项目启动】{resolution.rules_text}"]
        body.append(f"按{self._ritual_potency_text(project.potency)}效力、{self._ritual_scope_text(project.scope)}范围处理。")
        body.append(f"材料需要 {project.material_cost}Z，当前进度 {project.current_progress}/{project.required_progress}。")
        body.append(f"完成后会成为{self._persistent_change_type_text(project.output_type)}。")
        change_type = getattr(getattr(project, "output_type", ""), "value", getattr(project, "output_type", ""))
        if project.owner and change_type in {"equipment", "consumable"}:
            body.append(f"完成后先交给 {project.owner} 保管。")
        if project.location:
            body.append(f"预定地点：{project.location}。")
        if project.flaw:
            body.append(f"可怕缺陷：{project.flaw}。")
        if project.special_materials:
            body.append(f"特殊材料：{', '.join(project.special_materials)}。")
        if mood:
            body.append(mood)
        return "\n".join(body)

    def _render_project_helpers(self, resolution: ActionResolution, mood: str) -> str:
        project = resolution.payload["project"]
        resource_change = resolution.payload["resource_change"]
        body = [f"【项目帮手】{resolution.rules_text}"]
        body.append(
            f"{resource_change.target} 支付 {abs(resource_change.amount)}Z，"
            f"剩余 {resource_change.after}Z；项目当前帮手：{project.helpers} 名。"
        )
        if mood:
            body.append(mood)
        return "\n".join(body)

    def _render_project_progress(self, resolution: ActionResolution, mood: str) -> str:
        progress = resolution.payload["project_progress"]
        project = progress.project
        body = [f"【项目推进】{resolution.rules_text}"]
        body.append(
            f"参与者：{', '.join(progress.workers) if progress.workers else '无'}；"
            f"新增进度：{progress.progress_added}；"
            f"{progress.before}/{project.required_progress} -> {progress.after}/{project.required_progress}。"
        )
        if progress.completed:
            body.append(f"完成效果：{project.effect}")
            if "persistence" in resolution.payload:
                body.append(f"已写入长期状态：{self._persistent_change_text(resolution.payload['persistence'])}")
        if mood:
            body.append(mood)
        return "\n".join(body)

    def _affinity_text(self, affinity: Affinity) -> str:
        mapping = {
            Affinity.NORMAL: "相性正常。",
            Affinity.WEAK: "命中弱点！",
            Affinity.RESIST: "敌人对此有抗性。",
            Affinity.IMMUNE: "敌人完全免疫。",
            Affinity.ABSORB: "敌人反而吸收了这股力量。",
        }
        return mapping[affinity]

    def _roll_process_text(self, roll: RollOutcome) -> str:
        attr_text = self._attributes_text(roll.attributes) if roll.attributes else "未指定属性"
        dice_text = " + ".join(f"d{size}={value}" for size, value in roll.dice)
        dice_subtotal = sum(value for _, value in roll.dice)
        modifier_text = f"{roll.modifier:+d}"
        return (
            f"属性【{attr_text}】；掷骰 {dice_text} = {dice_subtotal}；"
            f"修正值 {modifier_text}；结算值 {roll.total} 对抗难度等级 {roll.target_number}"
        )

    def _attributes_text(self, attributes: list[str] | tuple[str, ...]) -> str:
        mapping = {"DEX": "敏捷", "INS": "洞察", "MIG": "力量", "WLP": "意志"}
        return "+".join(mapping.get(str(attribute), str(attribute)) for attribute in attributes)

    def _clock_delta_explanation(self, roll: RollOutcome | None, clock_change) -> str:
        if roll is None or clock_change is None:
            return ""
        delta = int(getattr(clock_change, "delta", 0) or 0)
        if abs(delta) <= 1:
            return ""
        margin = int(getattr(roll, "margin", 0) or 0)
        verb = "填充" if delta > 0 else "擦除"
        if roll.success:
            if margin >= 6:
                return f"结算值高出难度等级 {margin} 点，本次按命刻规则{verb} {abs(delta)} 格。"
            if margin >= 3:
                return f"结算值高出难度等级 {margin} 点，本次按命刻规则{verb} {abs(delta)} 格。"
            return f"本次规则修正后，命刻合计{verb} {abs(delta)} 格。"
        deficit = abs(margin)
        if deficit >= 6:
            return f"结算值低于难度等级 {deficit} 点，本次按命刻规则推进威胁 {abs(delta)} 格。"
        if deficit >= 3:
            return f"结算值低于难度等级 {deficit} 点，本次按命刻规则推进威胁 {abs(delta)} 格。"
        return f"本次失败触发威胁进展，命刻合计{verb} {abs(delta)} 格。"

    def _npc_action_intent_text(self, resolution: ActionResolution, roll: RollOutcome) -> str:
        if resolution.action.action_type != ActionType.NPCACT:
            return ""
        params = resolution.action.parameters
        npc_action_type = str(params.get("npc_action_type") or "")
        attributes = self._attributes_text(roll.attributes) if roll.attributes else "未指定属性"
        if npc_action_type == "Objective":
            clock_name = self._clean_clock_name(str(params.get("clock_name") or params.get("target") or roll.target or "当前命刻"))
            direction = self._npc_clock_direction(resolution)
            verb = "加速" if direction >= 0 else "阻止"
            return f"{roll.actor}试图{verb}命刻【{clock_name}】，使用属性【{attributes}】。"
        if npc_action_type == "Hinder":
            return f"{roll.actor}试图妨碍{roll.target}，使用属性【{attributes}】。"
        if npc_action_type in {"Attack", "Spell"}:
            return f"{roll.actor}对{roll.target}发起行动，使用属性【{attributes}】。"
        return ""

    def _npc_failed_action_text(self, resolution: ActionResolution, roll: RollOutcome | None) -> str:
        if roll is None or roll.success or resolution.action.action_type != ActionType.NPCACT:
            return ""
        params = resolution.action.parameters
        npc_action_type = str(params.get("npc_action_type") or "")
        if npc_action_type == "Objective":
            if self._npc_clock_direction(resolution) < 0:
                return f"{roll.actor}没能压住这个目标，没有造成额外擦除。"
            return f"{roll.actor}的施压没有立刻得手，没有造成额外推进。"
        if npc_action_type == "Hinder":
            return f"{roll.actor}试图制造破绽，但{roll.target}没有被动摇。"
        if npc_action_type in {"Attack", "Spell"}:
            return f"{roll.actor}的攻势落空，局势短暂露出反击的缝隙。"
        return ""

    def _npc_clock_direction(self, resolution: ActionResolution) -> int:
        clock_change = resolution.payload.get("clock_change")
        if clock_change is not None:
            delta = int(getattr(clock_change, "delta", 0) or 0)
            if delta < 0:
                return -1
            if delta > 0:
                return 1
        try:
            direction = int(resolution.action.parameters.get("clock_direction", 1))
        except (TypeError, ValueError):
            direction = 1
        return -1 if direction < 0 else 1

    def _clean_clock_name(self, value: str) -> str:
        text = str(value or "").strip()
        for left, right in (("[", "]"), ("【", "】")):
            if text.startswith(left) and right in text:
                return text[len(left) : text.index(right)].strip()
        return text

    def _clock_change_text(self, clock_change, *, prefix: str = "命刻") -> str:
        before = int(getattr(clock_change, "before", 0) or 0)
        after = int(getattr(clock_change, "after", 0) or 0)
        max_segments = int(getattr(clock_change, "max_segments", 0) or 0)
        if before == after:
            if before == 0 and after == 0 and max_segments > 0:
                return f"【{clock_change.clock_name}】0/{max_segments}。"
            return ""
        text = f"【{clock_change.clock_name}】{after}/{max_segments}。"
        completion = self._clock_completion_text(clock_change)
        if completion:
            text += f"\n{completion}"
        return text

    def _clock_state_text(self, clock_state: object) -> str:
        if not isinstance(clock_state, dict):
            return ""
        name = str(clock_state.get("clock_name") or clock_state.get("name") or "命刻").strip()
        current = int(clock_state.get("current") or 0)
        max_segments = int(clock_state.get("max_segments") or clock_state.get("max") or 0)
        if not name or max_segments <= 0:
            return ""
        return ""

    def _clock_completion_text(self, clock_change) -> str:
        before = int(getattr(clock_change, "before", 0) or 0)
        after = int(getattr(clock_change, "after", 0) or 0)
        max_segments = int(getattr(clock_change, "max_segments", 0) or 0)
        if max_segments <= 0 or before >= max_segments or after < max_segments:
            return ""
        clock_type = str(getattr(clock_change, "clock_type", "") or "").strip()
        if clock_type in {"threat", "villain", "dungeon", "boss"}:
            consequence = str(
                getattr(clock_change, "completion_consequence", "")
                or getattr(clock_change, "stakes", "")
                or ""
            ).strip()
            consequence = re.sub(r"^(?:若|当)?(?:命刻)?填满(?:后|时)?[，,:：]?\s*", "", consequence)
            if consequence:
                return consequence.rstrip("。") + "。"
            return "威胁已经兑现。"
        if clock_type == "ritual":
            return "仪式准备完成。"
        if clock_type == "objective":
            return "目标已经达成。"
        return "命刻已经填满。"

    def _append_auto_turn_advance(self, body: list[str], resolution: ActionResolution) -> None:
        if not (
            resolution.payload.get("turn_auto_advanced")
            or resolution.payload.get("clock_status_refresh")
        ):
            if resolution.payload.get("held_action_notice"):
                body.append(str(resolution.payload["held_action_notice"]))
            return
        state_pattern = re.compile(r"【([^】]+)】\s*\d+\s*/\s*\d+")
        seen_clock_names = {
            name
            for line in body
            for name in state_pattern.findall(str(line or ""))
        }
        for change in resolution.payload.get("auto_clock_changes") or []:
            name = str(getattr(change, "clock_name", "") or "").strip()
            if name and name in seen_clock_names:
                continue
            rendered = self._clock_change_text(change)
            if rendered:
                body.append(rendered)
                seen_clock_names.update(state_pattern.findall(rendered))
        clock_progress = resolution.payload.get("clock_progress") or []
        if clock_progress:
            for item in clock_progress:
                rendered = str(item)
                item_clock_names = set(state_pattern.findall(rendered))
                if item_clock_names and item_clock_names.issubset(seen_clock_names):
                    continue
                body.append(rendered)
                seen_clock_names.update(item_clock_names)
        if resolution.payload.get("held_action_notice"):
            body.append(str(resolution.payload["held_action_notice"]))

    def _damage_type_text(self, damage_type: str) -> str:
        mapping = {
            "physical": "物理",
            "fire": "火焰",
            "ice": "冰霜",
            "lightning": "雷电",
            "wind": "风",
            "earth": "大地",
            "dark": "黑暗",
            "light": "光明",
            "poison": "毒",
            "arcane": "奥灵",
            "none": "无属性",
        }
        return mapping.get(damage_type, damage_type)

    def _status_name(self, status) -> str:
        mapping = {
            "slow": "迟缓",
            "dazed": "眩晕",
            "weakened": "虚弱",
            "shaken": "动摇",
            "enraged": "激怒",
            "poisoned": "中毒",
        }
        value = getattr(status, "value", status)
        return mapping.get(value, value)

    def _effect_timing_text(self, timing) -> str:
        mapping = {
            "owner_turn_start": "直到施法者下回合开始",
            "owner_turn_end": "直到施法者本回合结束",
            "round_end": "直到本轮结束",
            "scene_end": "直到场景结束",
        }
        value = getattr(timing, "value", timing)
        return mapping.get(value, value)

    def _ritual_discipline_text(self, discipline) -> str:
        mapping = {
            "arcanism": "奥灵",
            "chimerism": "拟兽",
            "elementalism": "元素",
            "entropism": "熵系",
            "ritualism": "仪式",
            "spiritism": "御魂",
        }
        value = getattr(discipline, "value", discipline)
        return mapping.get(value, value)

    def _ritual_potency_text(self, potency) -> str:
        mapping = {
            "minor": "轻微",
            "moderate": "中等",
            "major": "强大",
            "extreme": "极强",
        }
        value = getattr(potency, "value", potency)
        return mapping.get(value, value)

    def _ritual_scope_text(self, scope) -> str:
        mapping = {
            "individual": "个体",
            "small": "小型",
            "large": "大型",
            "huge": "巨大",
        }
        value = getattr(scope, "value", scope)
        return mapping.get(value, value)

    def _project_use_text(self, use) -> str:
        mapping = {
            "consumable": "消耗品",
            "permanent": "永久发明",
        }
        value = getattr(use, "value", use)
        return mapping.get(value, value)

    def _persistent_change_type_text(self, change_type) -> str:
        mapping = {
            "world_fact": "世界事实",
            "facility": "地点设施",
            "equipment": "角色装备",
            "consumable": "一次性道具",
        }
        value = getattr(change_type, "value", change_type)
        return mapping.get(value, value)

    def _persistent_change_text(self, change) -> str:
        change_type = getattr(change, "change_type", "")
        value = getattr(change_type, "value", change_type)
        name = getattr(change, "name", "")
        description = getattr(change, "description", "")
        owner = getattr(change, "owner", "")
        location = getattr(change, "location", "")
        if value == "equipment":
            return f"{owner or '未指定持有者'} 获得装备【{name}】：{description}"
        if value == "consumable":
            return f"{owner or '未指定持有者'} 获得一次性道具【{name}】：{description}"
        if value == "facility":
            return f"{location or '未指定地点'} 出现设施【{name}】：{description}"
        location_text = f"（{location}）" if location else ""
        return f"{name}{location_text}：{description}"


class Narrator(Protocol):
    def render(self, resolution: ActionResolution) -> str:
        ...


class LLMExpressor:
    supports_scene_moment_deadline = True
    supports_scene_moment_attempt_limit = True

    """调用真实 LLM 生成最终叙事，失败时回退到规则表达器。"""

    def __init__(
        self,
        client: OpenAICompatibleClient,
        model: str,
        fallback: Narrator | None = None,
        *,
        allow_fallback: bool = True,
        gm_personality_prompt: str = "",
    ) -> None:
        self.client = client
        self.model = model
        self.fallback = fallback or Expressor()
        self.allow_fallback = bool(allow_fallback)
        self.gm_personality_prompt = str(gm_personality_prompt or "").strip()
        self.gm_persona = GMPersonaProfile.from_markdown(
            self.gm_personality_prompt,
            source="expressor",
        )
        self.last_raw_content = ""
        self.last_scene_candidates: list[str] = []
        self.last_scene_candidate_diagnostics: list[dict[str, object]] = []
        self.last_scene_moment_metadata: dict[str, object] = {}
        self.last_error = ""
        self.last_used_fallback = False

    def render(self, resolution: ActionResolution) -> str:
        self.last_error = ""
        self.last_used_fallback = False
        canonical_text = self.fallback.render(resolution).strip()
        if resolution.payload.get("check_result_provisional"):
            # A trait, bond, or skill may still replace this roll.  The
            # structured success/failure fields describe a transaction that
            # has not happened yet, so an expression model must not see them
            # and turn them into public fiction.  The deterministic panel is
            # the complete player-facing result until the window is resolved.
            return canonical_text
        # Long narration and direct answers are already authored by the
        # semantic GM pass. A second prose pass mostly paraphrases the same beat.
        if resolution.action.action_type == ActionType.NARRATE and (
            resolution.action.parameters.get("scene_clarification")
            or resolution.action.parameters.get("npc_answer_generated")
            or resolution.action.parameters.get("scene_object_response")
            or len(canonical_text) >= 80
        ):
            return canonical_text
        if resolution.action.action_type == ActionType.TRIGGER_OPPORTUNITY:
            return canonical_text
        if (
            resolution.action.action_type == ActionType.INVESTIGATE
            and not resolution.payload.get("world_consequence_required")
            and len(
            [item for item in resolution.payload.get("information", []) if str(item).strip()]
            ) >= 1
        ):
            # Once the authoritative panel contains a concrete fictional fact,
            # another prose pass usually paraphrases it. Save GM colour for an
            # NPC reaction or the next scene beat where something can change.
            return canonical_text
        roll = resolution.payload.get("roll")
        if (
            roll is not None
            and not bool(getattr(roll, "success", False))
            and str(
                resolution.action.parameters.get("failure_consequence")
                or resolution.action.parameters.get("failure_stakes")
                or ""
            ).strip()
        ):
            # The canonical panel already describes how the attempt was blocked.
            # A second prose pass consistently paraphrased that same failure and
            # made the GM sound like it was saying every result twice.
            return canonical_text
        speech_intent = resolution.payload.get("speech_intent")
        intent_text = (
            json.dumps(speech_intent, ensure_ascii=False, indent=2)
            if isinstance(speech_intent, dict)
            else "未提供；按默认的简洁结算表达处理。"
        )
        zero_heal_targets = self._zero_heal_targets(resolution)
        zero_heal_constraint = ""
        if zero_heal_targets:
            zero_heal_constraint = (
                "硬事实：本次法术对"
                + "、".join(zero_heal_targets)
                + "的实际恢复量为0。不得暗示其原本受伤、脸色未回暖、治疗失败或伤势没有改善；"
                "只有明确写出目标没有需要修补的伤势才可补充，否则必须留空。\n"
            )
        try:
            content = self.client.create_chat_completion(
                model=self.model,
                messages=build_cache_friendly_messages(
                    static_system_prompt=self._expression_system_prompt(
                        self._persona_mode_for_resolution(resolution)
                    ),
                    user_content=(
                        "下面的【规则面板】由系统代码生成，是必须原样保留的权威结算。\n"
                        "你只可以额外写 1 到 2 句纯叙事画面或真人桌边短评，不能写任何骰子、数字公式、HP/MP、伤害、恢复、命刻、修正值或规则解释。\n"
                        "如果规则面板显示失败、未命中、没有推进或被阻止，你的补充必须呈现阻力、代价、错失或 NPC/环境如何挡住行动；绝不能写成顺利推进、姿态很稳或局势打开。\n"
                        "不要把玩家刚刚声明的动作、台词或计划换一种说法复述一遍；补充必须是世界/NPC/环境对这件事的回应，或一句很短的桌边短评。\n"
                        "不得引入规则面板中没有出现的新人物、势力、线索或因果关系。若规则面板已经给出具体可见结果，不要换词复述；没有真正的新反应时应留空。\n"
                        "若规则面板已经含有‘大成功线索’、‘进一步线索’或两条以上具体调查事实，必须留空，不再追加同义叙述。\n"
                        f"{zero_heal_constraint}"
                        "若结构化数据里有本次刚刚填满的威胁命刻，必须把它的 stakes 或 completion_consequence 转成一句立即可见的现场后果；"
                        "不要照抄后台字段，不要说‘赌注’、‘命刻类型’或‘系统触发’。\n"
                        "如果无法确定叙事补充，就完全留空；不要写“空字符串”这几个字。\n"
                        "【表达意图】只约束你如何补充叙事，不得覆盖规则面板。\n"
                        f"{intent_text}\n\n"
                        f"【规则面板】\n{canonical_text}\n\n"
                        "【结构化结算数据，仅供理解，不得重写数值】\n"
                        f"{json.dumps(_serialize_resolution(resolution), ensure_ascii=False, indent=2)}"
                    ),
                ),
                temperature=0.7,
                allow_empty=True,
            )
            self.last_raw_content = content
            narrative = self._sanitize_narrative(content)
            if not narrative:
                return canonical_text
            if zero_heal_targets and not self._zero_heal_narrative_is_safe(narrative):
                return canonical_text
            narrative = self._dedupe_narrative(canonical_text, narrative)
            if not narrative:
                return canonical_text
            return f"{canonical_text}\n{narrative}"
        except Exception as exc:
            self.last_error = str(exc)
            if not self.allow_fallback:
                self.last_used_fallback = False
                raise RuntimeError("LLMExpressor failed and fallback is disabled.") from exc
            self.last_used_fallback = True
            return canonical_text

    @staticmethod
    def _zero_heal_targets(resolution: ActionResolution) -> list[str]:
        changes = []
        if "healing_change" in resolution.payload:
            changes.append(resolution.payload["healing_change"])
        changes.extend(resolution.payload.get("healing_changes") or [])
        return list(
            dict.fromkeys(
                str(change.target)
                for change in changes
                if int(getattr(change, "amount", 0) or 0) == 0
                and str(getattr(change, "target", "") or "").strip()
            )
        )

    @staticmethod
    def _zero_heal_narrative_is_safe(narrative: str) -> bool:
        compact = re.sub(r"\s+", "", str(narrative or ""))
        return any(
            cue in compact
            for cue in (
                "没有需要治疗",
                "没有需要修补",
                "并未受伤",
                "本就没有伤势",
                "没有伤口",
                "生命力本就充盈",
                "状态本就完好",
                "无需疗愈",
            )
        )

    def render_scene_moment(
        self,
        scene_packet: dict[str, object],
        *,
        instruction: str = "",
        beat: bool = False,
        deadline: float | None = None,
        max_attempts: int | None = None,
    ) -> str:
        scene_packet = self._scene_packet_for_moment(scene_packet, beat=beat)
        self.last_error = ""
        self.last_used_fallback = False
        self.last_scene_candidates = []
        self.last_scene_candidate_diagnostics = []
        self.last_scene_moment_metadata = {}
        mode = "场景中的下一拍" if beat else "场景开场"
        default_attempts = 2 if beat else 3
        attempt_limit = (
            default_attempts
            if max_attempts is None
            else max(1, min(default_attempts, int(max_attempts)))
        )
        fallback = self.fallback.render_scene_moment(scene_packet, instruction=instruction, beat=beat)
        try:
            static_prompt = (
                "你是《最终物语》游戏主持人时悠。你像真人GM一样说话：具体、自然、简洁，"
                "关注角色眼前能感知到的环境与NPC反应。你准备局势而不是预写剧情，绝不替玩家角色行动或决定。"
                "按用户消息末尾的JSON契约封装结果；只有reply字段会发给玩家，其余字段只记录reply已经公开的事实。"
            )
            persona = self.gm_persona.prompt_block(
                "scene",
                overlays=("heartbeat",) if beat else (),
                include_examples=not beat,
            )
            if persona:
                static_prompt += "\n\n" + persona
            selected_situation = str(
                scene_packet.get("selected_scene_situation") or ""
            ).strip()
            scene_focus = (
                "本场新局面的核心现场（必须在前两句中真正演出，而非只复述附近物件）："
                f"{selected_situation}\n"
                if selected_situation and not beat
                else ""
            )
            structured_output_contract = (
                "\n只输出一个JSON对象，不要使用Markdown代码块。格式：\n"
                '{"reply":"直接发给玩家的文本",'
                '"npc_conditions":[{"npc":"明确说话人","speaker_evidence":"reply中的逐字短句",'
                '"condition":"玩家需完成的具体事项","promised_result":"NPC随后明确承诺的结果"}],'
                '"npc_speakers":[{"npc":"稳定名字或场内唯一称呼",'
                '"speaker_evidence":"reply中含说话人归属的逐字短句",'
                '"public_statement":"该NPC在reply中的逐字台词",'
                '"supersedes_prior_terms":false}],'
                '"deferred_commitment_updates":[{"commitment_id":"pending_npc_commitments中的ID",'
                '"npc":"同一NPC","outcome":"fulfilled|cancelled",'
                '"evidence":"reply中证明兑现或取消的连续逐字片段"}],'
                '"settlement_conflicts":[{"exchange_id":"已结清交涉ID",'
                '"reason":"reply如何重新索要或推翻已谈妥事项"}],'
                '"state_change":{"material_change":false,"public_fact":"","opposition_move":"",'
                '"reveal":"","reversal":false,"local_question_changed":false,'
                '"local_question_resolved":false,"deliberate_cliffhanger":false,'
                '"signature_image_evolved":false,'
                '"commitment_level":"atmosphere|telegraph|action|consequence",'
                '"irreversible_change":false},'
                '"quality":{"adds_new_change":true,"honors_consequences":true,'
                '"fulfills_requested_change":true,"requested_change_already_public":false}}\n'
                "npc_conditions只能逐字提取reply中由明确说话人公开提出的交换条件；没有则为空数组。"
                "npc_speakers记录reply中所有实际开口的NPC；无台词则为空数组。"
                "其中不能使用‘守望会的人’‘有人’‘一个人’‘某人’等无法稳定指代的称呼。"
                "supersedes_prior_terms只在该NPC明确改口、收回、取消或替换自己此前条件/交易时为true。"
                "pending_npc_commitments是NPC尚未登记兑现的短期承诺。只有reply已经实际交付promised_result，"
                "或NPC明确取消承诺时才填写deferred_commitment_updates；只出现trigger、再次承诺、准备去做、"
                "推测已经做过都不算。commitment_id和npc必须照抄对应记录，evidence必须是reply中的连续逐字证据；"
                "没有则为空数组。"
                "state_change只能描述reply中实际公开发生的变化，不得根据后台设定推断。"
                "deliberate_cliffhanger只在reply先兑现不可逆结果、再留下一个具体且马上要处理的未决选择时为true；"
                "普通逼近、警告或新增谜团必须为false。"
                "settled_exchanges是已谈妥的接受或拒绝；若reply重开或推翻其中事项，必须如实填写settlement_conflicts，"
                "没有冲突则为空数组。"
                "quality判断reply是否相对recent_public_context带来新变化，并是否落实主持补充意图。"
            )
            speech_intent = dict(scene_packet.get("speech_intent") or {})
            user_content = (
                f"请主持{mode}。\n"
                f"{scene_focus}"
                "只输出直接说给玩家听的话。不要解释场景结构，不要列互动清单，不要提提示词、框架、焦点、"
                "故事大纲或可揭示内容。开场先给正在发生的画面和一个当下变化；主动节拍则承接现状，让NPC、"
                "环境或已公开威胁作出反应。必须服从最近公开对话：已经回答的问题不能倒带重问，已经解决的障碍不能重新出现；"
                "开场只能说明英雄在场，不能替任何玩家角色移动、说话、拿取或放置物品、展开地图、检查线索或选择姿态；"
                "关键物件应由环境或NPC带入画面，等待玩家自行决定如何接触。"
                "场景包中的 private_truths、possible_reveals、reversal、escalation_ladder 和 possible_payoffs 是GM后台准备，"
                "不能照抄、列举或无条件泄露；只在玩家已经触及合适证据、主持补充意图要求推进转折，或当前局面确实需要兑现时，"
                "把其中至多一项转成角色可感知的证据、NPC决定或现场后果。其余内容继续保密。"
                "若场景包只说某支巡逻队、追兵或其他威胁正在‘逼近’、需要‘避开’或形成倒计时，"
                "而公开事实没有说它已经抵达、相关命刻也没有填满，就只能描写远处脚步、尘烟、灯火等征兆；"
                "不得让该威胁已经停在门外、进入现场或开始对峙。"
                "反过来，committed_consequences 中的内容已经由规则层兑现，是不可软化、不可倒退的公开事实："
                "若写着巡逻队已经包围，就必须从包围后的封门、喊话、搜查或NPC抉择继续，不能降格成仍在逼近、绕路或找入口；"
                "主动节拍必须让至少一名在场人物或环境对该后果采取具体行动。"
                "若主持补充意图含有【局势提交】，本段中的行动必须已经发生并改变现场，不能停在‘最后警告’、"
                "‘正在逼近’、‘准备行动’或‘即将发生’；提交的是NPC/敌人/环境的行动，仍不得替玩家角色选择。"
                "resolved_conditions 中的NPC承诺也已经兑现；不得再次索要同一条件、重新锁门或把已给出的通行收回。"
                "settled_exchanges 中的交涉已经结束：不得要求玩家重复同一表态、再次交出已经接受的内容，"
                "也不得让NPC无缘无故反悔。它只证明当时谈妥过，不证明玩家仍在执行其中约定的未来路线或行动；"
                "若recent_public_context、location或转场锚点显示玩家后来选择了另一地点或互斥方案，必须服从较新的玩家选择。"
                "NPC可以保留旧许可或让旧向导待命，但不得把它写成玩家已经出发、向导正在带队或旧方案已经完成。"
                "可以让NPC执行不替玩家作决定的后续动作、改变策略或处理新的外部压力。"
                "场景包的 location 是当前物理地点：所有现场人物、建筑和动作都必须属于这个地点；"
                "除非最近公开对话明确转场，否则绝不能把上一场地点、守门人或障碍搬回当前画面。"
                "凡NPC在本段开口、提出条件或作出决定，都要使用一个稳定且可再次指代的名字或角色称呼；"
                "若尚未命名，可用‘白花守门人’‘驿站掌柜’这类场内唯一称呼，不能写‘守望会的人’‘有个人’后又让其连续发言。"
                "已经登场的NPC必须沿用原称呼，不能把另一个NPC或上一位说话人的身份套过来。"
                "若要首次说出一名此前已用描述性称呼开口的NPC名字，必须在同一句自然说明两者是同一人，"
                "例如‘门外那位财团使者、那位监察官……’；不得把新名字写成仿佛另一位突然出现的人。"
                "npc_statement_ledger只含已经公开的NPC台词、条件和承诺；它用于区分说话归属。"
                "NPC可以根据自身目标说新话，但绝不能把账本中属于另一人的独特限制、承诺或揭示改成自己说过的话。"
                "不要向玩家列出或解释这份账本。"
                "npc_due_commitments是已经公开、但尚可能在本段兑现的NPC承诺。若本段让其中的触发条件已经完成，"
                "必须把同一NPC承诺的后续动作实际写进画面；例如他说‘验完就退开’，便不能写成验完后还留在门边施压。"
                "这只是连续性约束，绝不能把该字段或其清单直接说给玩家。"
                "不要复述最近一条玩家或GM消息，也不要凭空添加新的阻拦条件。开场必须自然带出场景包中 mission_anchor 已确认的"
                "护送对象、关键物件或目的地，并让至少一项相关可见元素真正出现在画面里，不能只写地点气氛和守门障碍。"
                "场景开场仅在required_opening_image非空时，才必须在前两句中让它描述的同一具体物件或景象直接入镜；"
                "只描写其可感知外观与当下变化，不泄露后台含义。signature_image_reference只是后续场景的可选连续性意象："
                "只有它与当前地点和局面直接相关、并且因玩家选择或现场后果发生了可见变化时，才自然带回其中一个元素；"
                "不得为了呼应而重复整幅旧画面，也不得让它挤掉selected_scene_situation。主动节拍不必机械重复它。"
                "若场景包提供required_opening_elements，开场必须让每一项作为玩家当下可感知的物件、生物或现象实际出现，"
                "不能只暗示它可能在别处。required_opening_npc_names是本次开场的演员名单；"
                "prepared_npcs只是这些指定人物的资料。开场必须让每一名指定NPC清楚登场，"
                "但不得把未列入该名单、且最近公开内容也未说明已在场的后台NPC提前带入现场。"
                "首次介绍可以使用public_role加姓名，身份已经由上下文建立后可自然使用prepared_npcs中能唯一对应的短名；"
                "不得用临时造出的‘守门人’‘会长’替代。其goal_now、concrete_demand、"
                "acceptance_rule与private_secret仍是后台内容，除非NPC在当前对话里亲口说明，否则不得整表公开。"
                "selected_scene_title、selected_scene_role、selected_scene_purpose与selected_scene_situation共同限定本次开场的当前局面。"
                "本段只建立并推进这个局面，不得提前搬入其他备选场景的人物、对峙、高潮或结局；这些字段仍是后台约束，"
                "绝不能向玩家解释、列举或照抄。"
                "通常二至五句，最后停在玩家可以自由回应的位置。\n"
                "表达意图只约束说话方式，不改变场景事实；其中avoid列出的内容不能出现在reply里。\n"
                f"表达意图：{json.dumps(speech_intent, ensure_ascii=False)}\n"
                f"主持补充意图：{instruction.strip() or '无'}\n"
                f"场景包：{json.dumps(scene_packet, ensure_ascii=False, indent=2)}"
                f"{structured_output_contract}"
            )

            def request_scene(candidate_prompt: str) -> str:
                return self.client.create_chat_completion(
                    model=self.model,
                    messages=build_cache_friendly_messages(
                        static_system_prompt=static_prompt,
                        user_content=candidate_prompt,
                    ),
                    temperature=0.8,
                    response_format={"type": "json_object"},
                    deadline=deadline,
                    operation="scene_moment.beat" if beat else "scene_moment.opening",
                )

            def decode_scene(content: str) -> str:
                self.last_scene_moment_metadata = {}
                try:
                    payload = extract_json_object(content)
                except Exception:
                    return self._sanitize_scene_moment(content)
                reply = self._sanitize_scene_moment(str(payload.get("reply") or ""))
                if not reply:
                    return self._sanitize_scene_moment(content)
                payload = dict(payload)
                payload["reply"] = reply
                self.last_scene_moment_metadata = payload
                return reply

            content = request_scene(user_content)
            text = decode_scene(content)
            self.last_scene_candidates.append(text)
            recent_context = str(scene_packet.get("recent_public_context") or "").strip()
            planned_transition = (
                ""
                if beat
                else str(scene_packet.get("selected_scene_situation") or "").strip()
            )
            continuity_change = instruction if beat else planned_transition
            boundary_violation = self._scene_boundary_violation(text, scene_packet)
            usable = self._scene_moment_is_usable(text, scene_packet)
            if boundary_violation:
                usable = False
            required_npc_missing = bool(
                not beat
                and usable
                and not self._opening_names_prepared_npc(text, scene_packet)
            )
            required_element_missing = bool(
                not beat
                and usable
                and not self._opening_contains_required_elements(text, scene_packet)
            )
            if required_npc_missing or required_element_missing:
                usable = False
            repeated = bool(
                recent_context
                and usable
                and not self._scene_moment_adds_new_change_with_metadata(
                        recent_context,
                        text,
                        committed_consequences=list(scene_packet.get("committed_consequences") or []),
                        requested_change=continuity_change,
                        planned_change_is_new=bool(planned_transition),
                    )
                )
            self.last_scene_candidate_diagnostics.append(
                {
                    "attempt": 1,
                    "usable": bool(usable),
                    "repeated": bool(repeated),
                    "boundary_violation": str(boundary_violation or ""),
                    "missing_requirements": (
                        self._missing_opening_requirements(text, scene_packet)
                        if not beat
                        else []
                    ),
                }
            )
            if (not usable or repeated) and attempt_limit >= 2:
                missing_opening = (
                    self._missing_opening_requirements(text, scene_packet)
                    if not beat
                    else []
                )
                reason = boundary_violation or (
                    "上一候选遗漏了这些必须在开场直接出现的内容："
                    + "、".join(missing_opening)
                    + "。重写时请明确呈现这些对象并让它们真正进入同一画面；可以使用自然的同义称呼，"
                    "但不能只提与对象共享的地点词或势力词。"
                    if missing_opening
                    else
                    "上一候选只是把最近已经公开的赤羽纹样、风铃反应、人物姿态或旧线索换词重述，"
                    "没有让局势向前走。"
                    if repeated
                    else "上一候选过短、像标题或不是可直接说给玩家听的场景。"
                )
                if beat:
                    correction = (
                        "请完全重写。必须承接已公开事实，并加入一项此前没有公开的、由NPC决定、环境变化、"
                        "威胁后果或新抵达事件造成的具体变化；不要再描写同一物件被看见、同一个人再次偏头、"
                        "同一线索变得更清楚。不得推翻玩家已经知道的事实。"
                    )
                else:
                    correction = (
                        "请重写同一个开场镜头，只补齐缺失对象、增加本场当前正在发生的新局面或修正越界内容。"
                        "可以让旧意象短暂入镜，但不得把上一场已经公开的线索当成新发现重新讲一遍。"
                        "不要为了显得有变化而让威胁提前抵达、"
                        "让命刻提前完成、揭示幕后真相或搬入其他备选场景；保持场景包给出的当前压力阶段。"
                    )
                retry_prompt = f"{user_content}\n\n{reason}{correction}\n上一候选：{text}"
                content = request_scene(retry_prompt)
                text = decode_scene(content)
                self.last_scene_candidates.append(text)
                boundary_violation = self._scene_boundary_violation(text, scene_packet)
                usable = self._scene_moment_is_usable(text, scene_packet)
                if boundary_violation:
                    usable = False
                if usable and not beat:
                    usable = self._opening_names_prepared_npc(
                        text, scene_packet
                    ) and self._opening_contains_required_elements(text, scene_packet)
                if usable and recent_context:
                    usable = self._scene_moment_adds_new_change_with_metadata(
                        recent_context,
                        text,
                        committed_consequences=list(scene_packet.get("committed_consequences") or []),
                        requested_change=continuity_change,
                        planned_change_is_new=bool(planned_transition),
                    )
                self.last_scene_candidate_diagnostics.append(
                    {
                        "attempt": 2,
                        "usable": bool(usable),
                        "repeated": bool(recent_context and not usable and not boundary_violation),
                        "boundary_violation": str(boundary_violation or ""),
                        "missing_requirements": (
                            self._missing_opening_requirements(text, scene_packet)
                            if not beat
                            else []
                        ),
                    }
                )
                if not usable and not beat:
                    second_missing = self._missing_opening_requirements(text, scene_packet)
                    second_boundary = self._scene_boundary_violation(text, scene_packet)
                if not usable and not beat and attempt_limit >= 3:
                    second_reason = second_boundary or (
                        "第二候选仍遗漏：" + "、".join(second_missing) + "。"
                        if second_missing
                        else "第二候选仍未形成承接前情的新场景，或不是可直接说给玩家听的完整场景。"
                    )
                    final_retry_prompt = (
                        f"{user_content}\n\n{second_reason}请最后完整重写一次同一开场镜头。"
                        "必须呈现所需人物与物件，但不得让未完成命刻的后果已经发生，"
                        "不得把尚在远处的敌人写成抵达、封锁、立停或进入现场。\n"
                        f"第二候选：{text}"
                    )
                    content = request_scene(final_retry_prompt)
                    text = decode_scene(content)
                    self.last_scene_candidates.append(text)
                    boundary_violation = self._scene_boundary_violation(text, scene_packet)
                    usable = self._scene_moment_is_usable(text, scene_packet)
                    if boundary_violation:
                        usable = False
                    if usable:
                        usable = self._opening_names_prepared_npc(
                            text, scene_packet
                        ) and self._opening_contains_required_elements(text, scene_packet)
                    if usable and recent_context:
                        usable = self._scene_moment_adds_new_change_with_metadata(
                            recent_context,
                            text,
                            committed_consequences=list(scene_packet.get("committed_consequences") or []),
                            requested_change=continuity_change,
                            planned_change_is_new=bool(planned_transition),
                        )
                    self.last_scene_candidate_diagnostics.append(
                        {
                            "attempt": 3,
                            "usable": bool(usable),
                            "repeated": bool(recent_context and not usable and not boundary_violation),
                            "boundary_violation": str(boundary_violation or ""),
                            "missing_requirements": (
                                self._missing_opening_requirements(text, scene_packet)
                                if not beat
                                else []
                            ),
                        }
                    )
            self.last_raw_content = content
            if not usable:
                final_missing = (
                    self._missing_opening_requirements(text, scene_packet)
                    if not beat
                    else []
                )
                self.last_error = (
                    "场景开场重写后仍缺少：" + "、".join(final_missing)
                    if final_missing
                    else "表达模型重写后仍返回过短或后台化的场景文本"
                )
                if beat:
                    # A proactive beat is optional. A human GM with nothing
                    # useful to add stays quiet rather than crashing play or
                    # repeating the previous description.
                    self.last_error = ""
                    self.last_scene_moment_metadata = {}
                    return ""
                if not self.allow_fallback:
                    self.last_used_fallback = False
                    raise RuntimeError(self.last_error)
                self.last_used_fallback = True
                self.last_scene_moment_metadata = {}
                return fallback
            self.last_error = ""
            return text
        except Exception as exc:
            # Preserve the specific validator reason set above. The public HTTP
            # error stays generic; private diagnostics can still explain which
            # requirement rejected each candidate.
            self.last_error = self.last_error or str(exc)
            self.last_scene_moment_metadata = {}
            if not self.allow_fallback:
                self.last_used_fallback = False
                raise RuntimeError("LLMExpressor scene rendering failed and fallback is disabled.") from exc
            self.last_used_fallback = True
            return fallback

    def _scene_boundary_violation(
        self,
        text: str,
        scene_packet: dict[str, object],
    ) -> str:
        clock_violation = ClockNarrativeBoundary.violation(
            text,
            list(scene_packet.get("clock_boundaries") or []),
        )
        if clock_violation:
            return clock_violation
        commitment_violation = NPCCommitmentBoundary.violation(
            self.last_scene_moment_metadata,
            list(scene_packet.get("npc_due_commitments") or []),
        )
        if commitment_violation:
            return commitment_violation
        npc_violation = NPCStatementBoundary.violation(
            self.last_scene_moment_metadata,
            list(scene_packet.get("npc_statement_ledger") or []),
        )
        if npc_violation:
            return npc_violation
        return SpeechIntentBoundary.violation(
            text,
            dict(scene_packet.get("speech_intent") or {}),
        )

    @classmethod
    def _scene_packet_for_moment(
        cls,
        scene_packet: dict[str, object],
        *,
        beat: bool,
    ) -> dict[str, object]:
        """Give an opening only the private material needed for its shot."""

        packet = dict(scene_packet)
        if beat:
            return packet

        required_names = [
            str(item or "").strip()
            for item in (packet.get("required_opening_npc_names") or [])
            if str(item or "").strip()
        ]
        if "opening_prepared_npcs" in packet:
            opening_records = [
                dict(item)
                for item in (packet.get("opening_prepared_npcs") or [])
                if isinstance(item, dict)
            ]
        else:
            opening_records = [
                dict(item)
                for item in (packet.get("prepared_npcs") or [])
                if isinstance(item, dict)
                and any(
                    cls._opening_record_matches_required(item, required)
                    for required in required_names
                )
            ]
        packet["prepared_npcs"] = opening_records

        allowed_labels = [*required_names]
        allowed_labels.extend(
            str(item.get("name") or "").strip()
            for item in opening_records
            if str(item.get("name") or "").strip()
        )
        packet["npc_functions"] = [
            str(entry)
            for entry in (packet.get("npc_functions") or [])
            if cls._opening_function_matches_cast(str(entry), allowed_labels)
        ]

        # These remain in the GM scene frame for later decisions and beats.
        # The opening needs only the selected situation, public continuity and
        # its declared cast; extra arc prep encourages premature reveals.
        for key in (
            "private_truths",
            "possible_reveals",
            "session_title",
            "dramatic_question",
            "signature_image",
            "selected_scene_title",
            "selected_scene_role",
            "selected_scene_purpose",
            "opposition_goal",
            "dilemma",
            "reversal",
            "climax_type",
            "closure_requirement",
            "irreversible_change",
            "ending_echo",
            "escalation_ladder",
            "possible_payoffs",
            "opening_prepared_npcs",
        ):
            packet.pop(key, None)
        return packet

    @staticmethod
    def _opening_record_matches_required(record: dict[str, object], required: str) -> bool:
        def normalize(value: object) -> str:
            return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(value or ""))

        required_name = normalize(required)
        record_name = normalize(record.get("name"))
        if required_name and record_name and required_name == record_name:
            return True
        aliases = [normalize(value) for value in (record.get("aliases") or [])]
        return bool(required_name and required_name in aliases)

    @staticmethod
    def _opening_function_matches_cast(entry: str, allowed_labels: list[str]) -> bool:
        label = re.split(r"[：:]", str(entry or ""), maxsplit=1)[0]
        compact_label = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", label)
        for allowed in allowed_labels:
            compact_allowed = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(allowed or ""))
            if not compact_label or not compact_allowed:
                continue
            if compact_label == compact_allowed:
                return True
            if min(len(compact_label), len(compact_allowed)) >= 3 and (
                compact_label in compact_allowed or compact_allowed in compact_label
            ):
                return True
        return False

    def _expression_system_prompt(self, mode: str = "scene") -> str:
        persona = self.gm_persona.prompt_block(
            mode,
            include_examples=False,
        )
        if not persona:
            return EXPRESSOR_SYSTEM_PROMPT
        return (
            EXPRESSOR_SYSTEM_PROMPT
            + "\n\n"
            + persona
        )

    @staticmethod
    def _persona_mode_for_resolution(resolution: ActionResolution) -> str:
        if resolution.action.action_type in {
            ActionType.ATTACK,
            ActionType.SPELL,
            ActionType.GUARD,
            ActionType.EQUIP,
            ActionType.HINDER,
            ActionType.OBJECTIVE,
            ActionType.SKILL,
            ActionType.NEXT_TURN,
            ActionType.NPCACT,
            ActionType.START_CONFLICT,
            ActionType.RESOLVE_ZERO_HP,
            ActionType.RESOLVE_DECISION,
        }:
            return "conflict"
        return "scene"

    def _scene_moment_adds_new_change_with_metadata(
        self,
        recent_context: str,
        candidate: str,
        *,
        committed_consequences: list[object] | None = None,
        requested_change: str = "",
        planned_change_is_new: bool = False,
    ) -> bool:
        """Use co-generated quality metadata before requesting another audit.

        Older providers and test doubles may still return plain text. They
        retain the previous semantic review path; JSON-capable providers can
        settle expression, continuity quality and public state in one call.
        """

        quality = self.last_scene_moment_metadata.get("quality")
        if isinstance(quality, dict):
            required = {
                "adds_new_change",
                "honors_consequences",
                "fulfills_requested_change",
                "requested_change_already_public",
            }
            if required.issubset(quality):
                if not bool(quality.get("adds_new_change")):
                    return False
                if committed_consequences and not bool(quality.get("honors_consequences")):
                    return False
                judgeable_change = self._judgeable_requested_change(requested_change)
                if judgeable_change and not bool(quality.get("fulfills_requested_change")):
                    return False
                if planned_change_is_new and bool(quality.get("requested_change_already_public")):
                    return False
                return True
        return self._scene_moment_adds_new_change(
            recent_context,
            candidate,
            committed_consequences=committed_consequences,
            requested_change=requested_change,
            planned_change_is_new=planned_change_is_new,
        )

    def _scene_moment_adds_new_change(
        self,
        recent_context: str,
        candidate: str,
        *,
        committed_consequences: list[object] | None = None,
        requested_change: str = "",
        planned_change_is_new: bool = False,
    ) -> bool:
        """Ask the expression model whether a transition actually moves forward."""

        requested_change = self._judgeable_requested_change(requested_change)
        try:
            content = self.client.create_chat_completion(
                model=self.model,
                messages=build_cache_friendly_messages(
                    static_system_prompt=(
                        "你是跑团连续性审校器，只判断候选场景是否在最近公开内容之后增加了至少一项具体新变化。"
                        "角色、物件和气氛换词重述不算新变化；NPC新决定、环境新事件、威胁兑现的新后果、"
                        "新抵达者或已知行动造成的新结果才算。已兑现后果是不可逆公开事实：候选若把“已经发生”"
                        "降格成“尚在逼近、寻找或等待”，honors_consequences必须为false。"
                        "若提供requested_change，候选必须实际演出这项指定变化；只做无关动作或气氛描写时，"
                        "fulfills_requested_change必须为false。若planned_change_is_new为true，requested_change来自"
                        "新场景的策划局面：只要最近公开内容尚未演出这件事，而候选已把它变成现场正在发生的动作或变化，"
                        "即使沿用上一场的地点、人物或标志物，也应视为具体新变化；不要因为共享名词而误判成重述。"
                        "如果最近公开内容已经演出同一件事，requested_change_already_public必须为true。只输出JSON。"
                    ),
                    user_content=json.dumps(
                        {
                            "recent_public_context": recent_context[-2400:],
                            "committed_consequences": [
                                str(item) for item in (committed_consequences or []) if str(item).strip()
                            ],
                            "requested_change": str(requested_change or "").strip(),
                            "planned_change_is_new": bool(planned_change_is_new),
                            "candidate": candidate,
                            "output_schema": {
                                "adds_new_change": True,
                                "honors_consequences": True,
                                "fulfills_requested_change": True,
                                "requested_change_already_public": False,
                                "reason": "一句话理由",
                            },
                        },
                        ensure_ascii=False,
                    ),
                ),
                temperature=0,
                response_format={"type": "json_object"},
            )
            data = extract_json_object(content)
            fulfills_requested = bool(
                data.get("fulfills_requested_change", not bool(str(requested_change or "").strip()))
            )
            if fulfills_requested and requested_change:
                fulfills_requested = self._requested_relation_is_complete(
                    requested_change,
                    candidate,
                )
            adds_new_change = bool(data.get("adds_new_change", False))
            if (
                planned_change_is_new
                and requested_change
                and fulfills_requested
                and not bool(data.get("requested_change_already_public", False))
            ):
                adds_new_change = True
            return adds_new_change and bool(
                data.get("honors_consequences", True)
            ) and fulfills_requested
        except Exception:
            # The quality pass must not turn a valid scene opening into an API failure.
            return True

    @staticmethod
    def _judgeable_requested_change(instruction: str) -> str:
        """Keep quality review focused on the requested fiction, not meta prose.

        Heartbeat instructions often contain both a concise change and several
        routing/negative constraints. Asking the reviewer to "fulfil" that
        whole paragraph creates false negatives even when the scene advances.
        """

        clean = " ".join(str(instruction or "").split()).strip()
        if not clean:
            return ""
        marker = "只落实这一项尚未发生的变化："
        if marker in clean:
            clean = clean.split(marker, 1)[1]
            clean = re.split(r"[。；;]", clean, maxsplit=1)[0].strip()
            return clean[:180]
        meta_markers = (
            "判断是否需要",
            "若需要发言",
            "不得",
            "不要复述",
            "不替英雄决定",
            "桌面自然停顿",
        )
        if len(clean) > 100 or any(marker in clean for marker in meta_markers):
            return ""
        return clean[:180]

    @staticmethod
    def _requested_relation_is_complete(requested_change: str, candidate: str) -> bool:
        """Require both halves of an explicit contrastive reveal.

        A semantic reviewer often accepts “not X” as fulfillment of “not X,
        but Y”. This guard checks only the relation shape and named anchors; it
        does not decide what Y should be.
        """

        requested = " ".join(str(requested_change or "").split()).strip()
        text = " ".join(str(candidate or "").split()).strip()
        match = re.search(
            r"(?P<subject>[^。；;]{1,60}?)(?:并非|不是)(?P<negative>[^，,。；;]{1,36})"
            r"[，,；;](?:而)?(?:是|属于|来自|指向)(?P<positive>[^。；;]{1,60})",
            requested,
        )
        if not match:
            return True

        negative = re.sub(r"(?:的|那个|这个|一名|一个|某个|相关)$", "", match.group("negative").strip())
        positive = match.group("positive").strip()
        negative_tokens = [
            token
            for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", negative)
            if token not in {"并非", "不是"}
        ]
        positive_tokens = [
            token
            for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", positive)
            if token not in {"本应", "应该", "记得", "属于", "一个", "那名", "这个", "那个"}
        ]
        named_anchor = re.match(
            r"(?P<name>[\u4e00-\u9fffA-Za-z0-9·]{2,12}?)(?:本应|应该|曾经|已经|所|认识|记得)",
            positive,
        )
        if named_anchor:
            positive_tokens.insert(0, named_anchor.group("name"))

        negative_present = bool(
            any(token in text for token in negative_tokens)
            and re.search(r"并非|不是|不属于|并不", text)
        )
        positive_present = bool(
            any(token in text for token in positive_tokens)
            and re.search(r"属于|其实是|而是|原来是|刻着|指向|来自", text)
        )
        return negative_present and positive_present

    @classmethod
    def _scene_moment_is_usable(cls, text: str, scene_packet: dict[str, object]) -> bool:
        compact = re.sub(r"\s+", "", str(text or ""))
        if len(compact) < 18:
            return False
        location = re.sub(r"\s+", "", str(scene_packet.get("location") or "")).strip("。！？")
        if compact.strip("。！？") == location:
            return False
        if re.search(r"第[一二三四五六七八九十0-9]+幕", compact):
            return False
        if cls._contains_vague_speaking_npc(text):
            return False
        if ClockNarrativeBoundary.violation(
            text,
            list(scene_packet.get("clock_boundaries") or []),
        ):
            return False
        return True

    @staticmethod
    def _contains_vague_speaking_npc(text: str) -> bool:
        """Reject anonymous speakers that cannot support later continuity."""

        clean = " ".join(str(text or "").split()).strip()
        vague = r"(?:白花)?守望会的人|有人|有个人|一个人|某人|一名(?:守望会成员|巡守|卫兵|侍从)"
        return bool(
            re.search(
                rf"(?:{vague})[^。！？!?\n]{{0,72}}"
                r"(?:说|说道|答|答道|回答|回应|表示|开口|低声说|喊道|[：:]\s*[‘'\"“「『]|[‘\"“「『])",
                clean,
            )
        )

    @classmethod
    def _opening_names_prepared_npc(cls, text: str, scene_packet: dict[str, object]) -> bool:
        required = [
            str(item or "").strip()
            for item in (scene_packet.get("required_opening_npc_names") or [])
            if str(item or "").strip()
        ]
        compact = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(text or ""))
        return not required or all(
            cls._opening_npc_name_present(name, compact=compact, scene_packet=scene_packet)
            for name in required
        )

    @staticmethod
    def _opening_npc_name_present(
        required_name: str,
        *,
        compact: str,
        scene_packet: dict[str, object],
    ) -> bool:
        """Accept a prepared NPC's unambiguous short name in natural prose."""

        normalized = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(required_name or ""))
        if not normalized or normalized in compact:
            return True
        records = [
            item
            for item in (scene_packet.get("prepared_npcs") or [])
            if isinstance(item, dict)
        ]
        record = next(
            (
                item
                for item in records
                if re.sub(
                    r"[^\u4e00-\u9fffA-Za-z0-9]",
                    "",
                    str(item.get("name") or ""),
                )
                == normalized
            ),
            None,
        )
        if record is None:
            return False
        aliases = [
            re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(value or ""))
            for value in (record.get("aliases") or [])
            if str(value or "").strip()
        ]
        role = re.sub(
            r"[^\u4e00-\u9fffA-Za-z0-9]",
            "",
            str(record.get("public_role") or ""),
        )
        if role and normalized.startswith(role):
            aliases.append(normalized[len(role) :])
        elif role and normalized.endswith(role):
            aliases.append(normalized[: -len(role)])
        # Prepared names may put a short office after the personal name
        # (``白栎会长``), while natural prose introduces the same person as
        # ``白花守望会会长白栎``.  The exact-name and full-role checks above do
        # not cover that harmless ordering change.  Since this is already the
        # uniquely matched prepared record, its two-character-or-longer core is
        # a safe alias; an unregistered short name is still rejected.
        role_terms = (
            "守望会会长",
            "失忆旅人",
            "巡逻队长",
            "书记官",
            "监察官",
            "巡守长",
            "守门人",
            "代理人",
            "负责人",
            "会长",
            "队长",
            "领主",
            "祭司",
            "钟匠",
            "掌柜",
            "巡守",
        )
        for term in role_terms:
            if normalized.startswith(term):
                aliases.append(normalized[len(term) :])
            if normalized.endswith(term):
                aliases.append(normalized[: -len(term)])
        return any(len(alias) >= 2 and alias in compact for alias in aliases)

    @staticmethod
    def _opening_contains_required_elements(
        text: str,
        scene_packet: dict[str, object],
    ) -> bool:
        compact = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(text or ""))
        location = re.sub(
            r"[^\u4e00-\u9fffA-Za-z0-9]",
            "",
            str(scene_packet.get("location") or ""),
        )
        required = [
            str(item or "").strip()
            for item in (scene_packet.get("required_opening_elements") or [])
            if str(item or "").strip()
        ]
        return not required or all(
            LLMExpressor._required_element_present(
                label,
                compact=compact,
                location=location,
            )
            for label in required
        )

    @classmethod
    def _missing_opening_requirements(
        cls,
        text: str,
        scene_packet: dict[str, object],
    ) -> list[str]:
        source = str(text or "")
        compact = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", source)
        missing = [
            f"NPC【{name}】"
            for name in (
                str(item or "").strip()
                for item in (scene_packet.get("required_opening_npc_names") or [])
            )
            if name
            and not cls._opening_npc_name_present(
                name,
                compact=compact,
                scene_packet=scene_packet,
            )
        ]
        for item in (scene_packet.get("required_opening_elements") or []):
            label = str(item or "").strip()
            location = re.sub(
                r"[^\u4e00-\u9fffA-Za-z0-9]",
                "",
                str(scene_packet.get("location") or ""),
            )
            if label and not cls._required_element_present(
                label,
                compact=compact,
                location=location,
            ):
                missing.append(f"场景要素【{label}】")
        return missing

    @classmethod
    def _required_element_present(cls, label: str, *, compact: str, location: str) -> bool:
        normalized = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(label or ""))
        if not normalized or normalized in compact:
            return True
        # Chinese prose naturally inserts possessive particles inside a
        # prepared compound name (for example ``辉钢财团的巡逻印记`` for the
        # canonical ``辉钢财团巡逻印记``).  They do not change the fictional
        # identity, so compare one particle-free form before considering
        # broader aliases.  Keeping every other character intact prevents a
        # generic mention of ``巡逻`` or ``印记`` from satisfying the check.
        particle_free_label = re.sub(r"[的之]", "", normalized)
        particle_free_text = re.sub(r"[的之]", "", compact)
        if particle_free_label and particle_free_label in particle_free_text:
            return True
        # Prepared elements often use a fully-qualified map label such as
        # “白花碑驿站旧路”. Inside that location a human GM naturally says
        # “旧路闸门”; requiring the map prefix verbatim creates stilted prose.
        if location and normalized.startswith(location):
            local_name = normalized[len(location) :].lstrip("的")
            if len(local_name) >= 2 and local_name in compact:
                return True
            if local_name:
                normalized = local_name
        # A prepared object is a fictional identity, not a mandatory display
        # string. Preserve its distinguishing modifier while accepting ordinary
        # Chinese variants for the same object class, such as 白花风铃/白花铜铃
        # or 旧路闸门/旧路门闸. This remains stricter than token overlap: merely
        # mentioning 白花碑驿站 cannot satisfy a required 白花风铃.
        alias_groups = (
            ("风铃", "铜铃", "挂铃", "铃铛"),
            ("闸门", "门闸"),
            ("旅人", "旅客"),
            ("巡逻队", "巡逻兵", "巡逻小队"),
        )
        for aliases in alias_groups:
            source = next((item for item in aliases if item in normalized), "")
            if not source:
                continue
            modifier = normalized.replace(source, "", 1)
            for alias in aliases:
                candidate = f"{modifier}{alias}"
                if len(candidate) >= 2 and candidate in compact:
                    return True
        return False

    def _sanitize_scene_moment(self, content: str) -> str:
        lines: list[str] = []
        backstage = ("场景框架", "场景包", "互动焦点", "可揭示内容", "故事大纲", "提示词", "后台")
        for raw_line in str(content or "").splitlines():
            line = raw_line.strip()
            if not line or any(term in line for term in backstage):
                continue
            line = re.sub(r"^(?:场景开场|主动节拍|叙事)[：:]\s*", "", line)
            if line:
                lines.append(line)
        return self._dedupe_scene_paragraphs("\n".join(lines[:5]).strip())

    @staticmethod
    def _dedupe_scene_paragraphs(text: str) -> str:
        paragraphs = [part.strip() for part in str(text or "").splitlines() if part.strip()]
        kept: list[str] = []
        for paragraph in paragraphs:
            normalized = re.sub(r"\s+", "", paragraph)
            if kept:
                previous = re.sub(r"\s+", "", kept[-1])
                if normalized == previous or (len(normalized) >= 28 and normalized in previous):
                    continue
            kept.append(paragraph)
        return "\n".join(kept)

    def _sanitize_narrative(self, content: str) -> str:
        text = str(content or "").strip()
        if not text:
            return ""
        lines = []
        forbidden_patterns = [
            r"骰",
            r"结算",
            r"目标值|目标数|DL|难度等级|防御值|物防|魔防",
            r"修正",
            r"HP|MP|生命值|精神值",
            r"伤害|恢复|命刻|物语点|终结点",
            r"大成功|大失败|成功|失败",
            r"规则|payload|JSON|参数|实际|这里|不，这里|可能|应该|？不",
            r"共同创作固定|系统已记录|世界设定已更新|规则层会处理|场景物件|已建档",
            r"这一步的(?:重点|目的|意义)|这(?:个|次)动作的(?:重点|目的|意义)|没有急着替任何人做决定",
            r"像一[枚颗粒][^。\n]*钉子|钉住[^。\n]*(?:选择|设定|世界|创作)",
            r"硬状态|SellItem|ActionType|GM应回应|保持冲突继续",
            r"\d+\s*[+＋\-x×*/=]\s*\d+",
            r"\b\d+\b",
        ]
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line in {"空字符串", "空白", "留空", '""', "''"}:
                continue
            if any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in forbidden_patterns):
                continue
            lines.append(line)
        return "\n".join(lines[:2]).strip()

    def _dedupe_narrative(self, canonical_text: str, narrative: str) -> str:
        existing = {self._normalize_line_for_dedupe(line) for line in str(canonical_text or "").splitlines()}
        kept: list[str] = []
        for raw_line in str(narrative or "").splitlines():
            line = raw_line.strip()
            key = self._normalize_line_for_dedupe(line)
            if not line or not key or key in existing:
                continue
            existing.add(key)
            kept.append(line)
        return "\n".join(kept[:2]).strip()

    def _normalize_line_for_dedupe(self, line: str) -> str:
        text = re.sub(r"\s+", "", str(line or ""))
        return text.strip("。！？!?,，；;：:")


def _serialize_resolution(resolution: ActionResolution) -> dict:
    payload: dict[str, object] = {}
    for key, value in resolution.payload.items():
        if hasattr(value, "__dataclass_fields__"):
            payload[key] = _json_safe(asdict(value))
        else:
            payload[key] = _json_safe(value)
    return {
        "action_type": resolution.action.action_type.value,
        "parameters": _json_safe(_public_action_parameters(resolution.action.parameters)),
        "rules_text": resolution.rules_text,
        "canonical_rules_panel": Expressor().render(resolution),
        "payload": payload,
    }


def _public_action_parameters(parameters: dict) -> dict:
    return {
        key: value
        for key, value in dict(parameters or {}).items()
        if key not in {"in_mind_reply", "reasoning", "gm_private_notes", "private_notes"}
    }


def _json_safe(value):
    if hasattr(value, "__dataclass_fields__"):
        return _json_safe(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value

