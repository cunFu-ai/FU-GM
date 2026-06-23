from __future__ import annotations

import json
import re
from dataclasses import asdict
from enum import Enum
from typing import Protocol

from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.models import ActionResolution, ActionType, Affinity, RollOutcome
from fu_gm.prompt_cache import build_cache_friendly_messages
from fu_gm.prompts import EXPRESSOR_SYSTEM_PROMPT


class Expressor:
    """把已经过规则验证的结果转成面向玩家的 JRPG 叙事文本。"""

    def render(self, resolution: ActionResolution) -> str:
        action = resolution.action
        mood = action.parameters.get("in_mind_reply", "")
        if resolution.payload.get("out_of_turn"):
            return f"【回合提示】{resolution.rules_text}\n{mood}".strip()
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
            return f"【防御】{resolution.rules_text}\n{mood}".strip()
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
        if action.action_type == ActionType.ACCEPT_STORY_CHANGE:
            fact = resolution.payload["fact"]
            return f"【物语改写】{resolution.rules_text}\n{mood}\n新的事实：{fact}"
        return f"【叙事】{action.parameters.get('summary', resolution.rules_text)}\n{mood}".strip()

    def _render_roll(self, resolution: ActionResolution, mood: str) -> str:
        roll: RollOutcome = resolution.payload["roll"]
        result_text = "成功" if roll.success else "失败"

        special = ""
        if roll.critical_success:
            special = " 大成功！"
        elif roll.fumble:
            special = " 大失败！"

        body = [
            (
                f"【战斗结算】{roll.actor} 对 {roll.target} 的检定："
                f"{self._roll_process_text(roll)}，{result_text}！{special}"
            ).strip(),
        ]

        if roll.success:
            affinity_text = self._affinity_text(roll.applied_affinity)
            body.append(
                f"对 {roll.target} 造成 {roll.damage} 点{self._damage_type_text(roll.damage_type)}伤害。"
                f"{affinity_text} 目标剩余 HP：{roll.hp_after}。"
            )
        if roll.opportunity_count:
            body.append(f"本次结算产生 {roll.opportunity_count} 次机会。")
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
            body.append(
                f"命刻【{clock_change.clock_name}】变化："
                f"{clock_change.before}/{clock_change.max_segments} -> {clock_change.after}/{clock_change.max_segments}。"
            )
        if "conflict_event" in resolution.payload:
            conflict_event = resolution.payload["conflict_event"]
            body.append(conflict_event.summary)
            if conflict_event.fabula_awarded:
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

        if mood:
            body.append(mood)
        return "\n".join(body)

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
            for change in getattr(result, "resource_changes", []):
                body.append(f"{change.target} 的 {change.resource.upper()}：{change.before} -> {change.after}。")
            for damage in getattr(result, "damage_results", []):
                if damage.get("healing"):
                    body.append(f"{damage['target']} 吸收效果，HP：{damage['hp_before']} -> {damage['hp_after']}。")
                else:
                    body.append(f"{damage['target']} 受到 {damage['damage']} 点{self._damage_type_text(damage['damage_type'])}伤害，HP：{damage['hp_before']} -> {damage['hp_after']}。")
            body.extend(getattr(result, "status_changes", []))
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
            for change in getattr(result, "resource_changes", []):
                body.append(f"{change.target} 的 {change.resource.upper()}：{change.before} -> {change.after}。")
            for damage in getattr(result, "damage_results", []):
                body.append(f"{damage['target']} 受到 {damage['damage']} 点{self._damage_type_text(damage['damage_type'])}伤害，HP：{damage['hp_before']} -> {damage['hp_after']}。")
            body.extend(getattr(result, "status_changes", []))
        nested = resolution.payload.get("nested_resolution")
        if nested is not None:
            body.append(f"联动结算：{nested.rules_text}")
        if mood:
            body.append(mood)
        return "\n".join(body)

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
        body = [
            (
                f"【调查】{roll.actor} 对 {roll.target} 的检定："
                f"{self._roll_process_text(roll)}，{result_text}！{special}"
            ).strip()
        ]
        if roll.opportunity_count:
            body.append(f"本次结算产生 {roll.opportunity_count} 次机会。")
        for info in resolution.payload.get("information", []):
            body.append(info)
        self._append_auto_turn_advance(body, resolution)
        if mood:
            body.append(mood)
        return "\n".join(body)

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
            body = [
                (
                    f"【{title}】{roll.actor}：{self._roll_process_text(roll)}，"
                    f"{result_text}！{special}"
                ).strip()
            ]
            if roll.opportunity_count:
                body.append(f"本次结算产生 {roll.opportunity_count} 次机会。")
            teamwork = resolution.payload.get("conflict_teamwork") or {}
            if teamwork:
                body.append(f"团队合作提供 +{teamwork.get('total_bonus', 0)} 修正。")
            if resolution.payload.get("clock_direction_corrected"):
                body.append("本次成功用于压制威胁命刻，因此按规则擦除威胁进度，而不是推进威胁。")
        else:
            body = [f"【{title}】{resolution.rules_text}"]
        if "clock_change" in resolution.payload:
            clock_change = resolution.payload["clock_change"]
            body.append(
                f"命刻【{clock_change.clock_name}】变化："
                f"{clock_change.before}/{clock_change.max_segments} -> {clock_change.after}/{clock_change.max_segments}。"
            )
        self._append_auto_turn_advance(body, resolution)
        if mood:
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
            body.append(f"持续时机：{self._effect_timing_text(spell_effect.expires_on)}。")
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
        body = [f"【仪式设计】{resolution.rules_text}"]
        body.append(
            f"学科：{self._ritual_discipline_text(plan.discipline)}；"
            f"检定：【{'+'.join(plan.attributes)}】；MP 消耗：{plan.mp_cost}；DL：{plan.target_number}。"
        )
        if plan.rare_material:
            body.append(f"稀有媒介：{plan.rare_material}，MP 消耗已减半。")
        if "clock_change" in resolution.payload:
            clock_change = resolution.payload["clock_change"]
            body.append(
                f"仪式命刻【{clock_change.clock_name}】已建立："
                f"{clock_change.after}/{clock_change.max_segments}。"
            )
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
                f"命刻【{clock_change.clock_name}】变化："
                f"{clock_change.before}/{clock_change.max_segments} -> "
                f"{clock_change.after}/{clock_change.max_segments}。"
            ),
        ]
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
        body.append(
            f"用途：{self._project_use_text(project.use)}；效力：{self._ritual_potency_text(project.potency)}；"
            f"范围：{self._ritual_scope_text(project.scope)}。"
        )
        body.append(
            f"材料成本：{project.material_cost}Z；所需进度："
            f"{project.current_progress}/{project.required_progress}。"
        )
        body.append(f"完成后写入：{self._persistent_change_type_text(project.output_type)}。")
        if project.owner:
            body.append(f"预定持有者：{project.owner}。")
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
        attr_text = "+".join(roll.attributes) if roll.attributes else "未指定属性"
        dice_text = " + ".join(f"d{size}={value}" for size, value in roll.dice)
        dice_subtotal = sum(value for _, value in roll.dice)
        modifier_text = f"{roll.modifier:+d}"
        return (
            f"属性【{attr_text}】；掷骰 {dice_text} = {dice_subtotal}；"
            f"修正值 {modifier_text}；结算值 {roll.total} vs DL {roll.target_number}"
        )

    def _append_auto_turn_advance(self, body: list[str], resolution: ActionResolution) -> None:
        if not resolution.payload.get("turn_auto_advanced"):
            return
        next_actor = resolution.payload.get("next_actor")
        if next_actor:
            body.append(f"下一位行动者：{next_actor}。")

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
            "arcane": "奥术",
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
            "arcanism": "奥术",
            "chimerism": "嵌合",
            "elementalism": "元素",
            "entropism": "熵系",
            "ritualism": "仪式",
            "spiritism": "灵魂",
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
    """调用真实 LLM 生成最终叙事，失败时回退到规则表达器。"""

    def __init__(
        self,
        client: OpenAICompatibleClient,
        model: str,
        fallback: Narrator | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.fallback = fallback or Expressor()
        self.last_raw_content = ""
        self.last_error = ""
        self.last_used_fallback = False

    def render(self, resolution: ActionResolution) -> str:
        canonical_text = self.fallback.render(resolution).strip()
        try:
            self.last_used_fallback = False
            self.last_error = ""
            content = self.client.create_chat_completion(
                model=self.model,
                messages=build_cache_friendly_messages(
                    static_system_prompt=EXPRESSOR_SYSTEM_PROMPT,
                    user_content=(
                        "下面的【规则面板】由系统代码生成，是必须原样保留的权威结算。\n"
                        "你只可以额外写 1 到 2 句纯叙事画面，不能写任何骰子、数字公式、HP/MP、伤害、恢复、命刻、修正值或规则解释。\n"
                        "如果无法确定叙事补充，就只返回空字符串。\n"
                        f"【规则面板】\n{canonical_text}\n\n"
                        "【结构化结算数据，仅供理解，不得重写数值】\n"
                        f"{json.dumps(_serialize_resolution(resolution), ensure_ascii=False, indent=2)}"
                    ),
                ),
                temperature=0.7,
            )
            self.last_raw_content = content
            narrative = self._sanitize_narrative(content)
            if not narrative:
                return canonical_text
            narrative = self._dedupe_narrative(canonical_text, narrative)
            if not narrative:
                return canonical_text
            return f"{canonical_text}\n{narrative}"
        except Exception as exc:
            self.last_used_fallback = True
            self.last_error = str(exc)
            return canonical_text

    def _sanitize_narrative(self, content: str) -> str:
        text = str(content or "").strip()
        if not text:
            return ""
        lines = []
        forbidden_patterns = [
            r"骰",
            r"结算",
            r"目标值|目标数|DL|防御值|物防|魔防",
            r"修正",
            r"HP|MP|生命值|精神值",
            r"伤害|恢复|命刻|物语点|终结点",
            r"大成功|大失败|成功|失败",
            r"规则|payload|JSON|参数|实际|这里|不，这里|可能|应该|？不",
            r"\d+\s*[+＋\-x×*/=]\s*\d+",
            r"\b\d+\b",
        ]
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
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
        "parameters": _json_safe(resolution.action.parameters),
        "rules_text": resolution.rules_text,
        "canonical_rules_panel": Expressor().render(resolution),
        "payload": payload,
    }


def _json_safe(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value

