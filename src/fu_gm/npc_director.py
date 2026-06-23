from __future__ import annotations

import json
import re
from typing import Protocol

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.world_state import WorldState
from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.llm_utils import extract_json_object
from fu_gm.models import Action, ActionType, GamePanel
from fu_gm.prompt_cache import build_cache_friendly_messages, system_reminder
from fu_gm.prompts import NPC_ACT_SYSTEM_PROMPT


class NPCDirector(Protocol):
    def decide(self, panel: GamePanel, actor_name: str) -> Action:
        ...


class HeuristicNPCDirector:
    """不依赖 LLM 的 NPC 行动选择器，用于兜底与测试。"""

    _CLOCK_PATTERN = re.compile(r"^\[(?P<name>.+?)\]\s+(?P<current>\d+)/(?P<max>\d+)$")

    def __init__(
        self,
        character_manager: CharacterManager,
        conflict_manager: ConflictManager,
        world_state: WorldState,
    ) -> None:
        self.character_manager = character_manager
        self.conflict_manager = conflict_manager
        self.world_state = world_state

    def build_tactical_snapshot(self, panel: GamePanel, actor_name: str) -> dict[str, object]:
        actor = self.character_manager.get(actor_name)
        enemies = [character for character in self.character_manager.all() if "pc" in character.traits and character.hp > 0]
        target = self._pick_target(enemies) if enemies else None
        preferred_clock = self._pick_preferred_clock(panel.active_clocks)
        stage = self.conflict_manager.current_stage(actor_name)
        guarded_targets = [
            enemy.name for enemy in enemies if self.character_manager.guardian_for(enemy.name) is not None
        ]
        return {
            "actor_in_crisis": actor.in_crisis,
            "actor_statuses": [status.value for status in actor.statuses],
            "ultima_points": self.conflict_manager.state.ultima_points.get(actor_name, 0),
            "current_stage": stage.name if stage is not None else "",
            "stage_public_cue": stage.public_cue if stage is not None else "",
            "stage_preferred_actions": list(stage.preferred_actions) if stage is not None else [],
            "stage_tactic_hints": list(stage.tactic_hints) if stage is not None else [],
            "stage_hint_policy": "soft_suggestions_not_forced",
            "stage_affinity_changes": {
                damage_type: affinity.value for damage_type, affinity in (stage.affinity_changes.items() if stage is not None else [])
            },
            "can_escalate": self.conflict_manager.can_escalate(actor_name),
            "is_exalted": actor_name in self.conflict_manager.state.exalted_enemies,
            "action_count_per_round": self.conflict_manager.state.enemy_action_counts.get(actor_name, 1),
            "queued_extra_actions": [
                queued_actor for queued_actor in self.conflict_manager.state.queued_turns if queued_actor == actor_name
            ],
            "preferred_target": target.name if target is not None else "",
            "preferred_clock": preferred_clock["name"] if preferred_clock is not None else "",
            "preferred_clock_progress": (
                f"{preferred_clock['current']}/{preferred_clock['max_segments']}" if preferred_clock is not None else ""
            ),
            "guarded_targets": guarded_targets,
            "crisis_targets": [enemy.name for enemy in enemies if enemy.in_crisis],
        }

    def decide(self, panel: GamePanel, actor_name: str) -> Action:
        actor = self.character_manager.get(actor_name)
        self._ensure_persona(actor_name, panel)
        enemies = [character for character in self.character_manager.all() if "pc" in character.traits and character.hp > 0]
        if not enemies:
            return Action(
                action_type=ActionType.NPCACT,
                parameters={
                    "actor": actor_name,
                    "npc_action_type": "Narrate",
                    "summary": f"{actor_name} 暂时找不到有效目标，正在观察战场。",
                    "in_mind_reply": f"{actor_name} 没有贸然行动，而是在寻找新的破绽。",
                },
            )

        snapshot = self.build_tactical_snapshot(panel, actor_name)
        target = self._pick_target(enemies)
        ultima_points = int(snapshot["ultima_points"])
        if actor.statuses and ultima_points > 0:
            return Action(
                action_type=ActionType.NPCACT,
                parameters={
                    "actor": actor_name,
                    "npc_action_type": "UltimaRecover",
                    "in_mind_reply": f"{actor_name} 以终结点扭转劣势，强行镇压体内失衡的力量。",
                },
            )

        preferred_clock = self._pick_preferred_clock(panel.active_clocks)
        # 阶段偏好是软提示：它帮助兜底 AI 选出更像 Boss 阶段的动作，但不覆盖保命、
        # 终结点恢复、命刻压迫等更贴近当前局势的选择。
        stage_actions = [str(item) for item in snapshot.get("stage_preferred_actions", [])]
        if preferred_clock is not None and self._should_push_objective(actor_name, actor, target, preferred_clock):
            return Action(
                action_type=ActionType.NPCACT,
                parameters={
                    "actor": actor_name,
                    "npc_action_type": "Objective",
                    "target": preferred_clock["name"],
                    "clock_name": preferred_clock["name"],
                    "attributes": self._objective_attributes(actor),
                    "target_number": 10,
                    "reasoning": "战场命刻已经接近关键节点，当前回合优先推进计划或威胁。",
                    "in_mind_reply": (
                        f"{actor_name} 没有把全部注意力放在眼前的对手身上，而是强行推动"
                        f"【{preferred_clock['name']}】进入下一阶段。"
                    ),
                },
            )

        if "Spell" in stage_actions and self._should_cast_spell(actor, target):
            spell_name = actor.spells[0] if actor.spells else None
            return Action(
                action_type=ActionType.NPCACT,
                parameters={
                    "actor": actor_name,
                    "npc_action_type": "Spell",
                    "target": target.name,
                    "spell_name": spell_name,
                    "attributes": ["INS", "WLP"],
                    "mp_cost": 5,
                    "fixed_damage": max(5, actor.weapon_damage - 1),
                    "damage_type": self._infer_spell_damage_type(actor),
                    "reasoning": "当前 Boss 阶段偏好施法压制，优先用魔法制造相性或异常压力。",
                    "in_mind_reply": f"{actor_name} 顺着阶段变化释放魔力，让战场节奏开始偏向自己。",
                },
            )

        if "Hinder" in stage_actions:
            hinder_status = self._pick_hinder_status(target)
            if hinder_status is not None:
                return Action(
                    action_type=ActionType.NPCACT,
                    parameters={
                        "actor": actor_name,
                        "npc_action_type": "Hinder",
                        "target": target.name,
                        "attributes": ["INS", "WLP"],
                        "target_number": 10,
                        "status_effect": hinder_status,
                        "reasoning": "当前 Boss 阶段强调控制，优先用异常状态拆掉英雄节奏。",
                        "in_mind_reply": f"{actor_name} 不急着击倒 {target.name}，而是先锁住其行动节拍。",
                    },
                )

        defensive_spell = self._pick_defensive_spell(actor)
        if defensive_spell is not None and actor.in_crisis and ultima_points == 0 and not self.conflict_manager.can_escalate(actor_name):
            return Action(
                action_type=ActionType.NPCACT,
                parameters={
                    "actor": actor_name,
                    "npc_action_type": "Spell",
                    "target": actor_name,
                    "spell_name": defensive_spell,
                    "reasoning": "当前已陷入危机，优先用持续型防护法术争取回合。",
                    "in_mind_reply": f"{actor_name} 强行升起最后的防护结界，为自己赢得喘息。",
                },
            )

        if actor.in_crisis and ultima_points == 0 and not self.conflict_manager.can_escalate(actor_name):
            guard_target = self._pick_guard_target(enemies)
            return Action(
                action_type=ActionType.NPCACT,
                parameters={
                    "actor": actor_name,
                    "npc_action_type": "Guard",
                    "guarded_target": guard_target.name if guard_target is not None else None,
                    "reasoning": "当前已陷入危机且缺乏终结点，优先防御并维持战线。",
                    "in_mind_reply": f"{actor_name} 收拢姿态，准备顶住英雄们下一波猛攻。",
                },
            )

        if self._should_cast_spell(actor, target):
            spell_name = actor.spells[0] if actor.spells else None
            return Action(
                action_type=ActionType.NPCACT,
                parameters={
                    "actor": actor_name,
                    "npc_action_type": "Spell",
                    "target": target.name,
                    "spell_name": spell_name,
                    "attributes": ["INS", "WLP"],
                    "mp_cost": 5,
                    "fixed_damage": max(5, actor.weapon_damage - 1),
                    "damage_type": self._infer_spell_damage_type(actor),
                    "reasoning": "当前法术命中面更优，或可以绕开近战掩护，适合用魔导火力施压。",
                    "in_mind_reply": f"{actor_name} 的核心回路骤然升温，准备以魔导火力压垮 {target.name}。",
                },
            )

        if target.in_crisis and not actor.guarding:
            return Action(
                action_type=ActionType.NPCACT,
                parameters={
                    "actor": actor_name,
                    "npc_action_type": "Attack",
                    "target": target.name,
                    "attributes": ["DEX", "MIG"],
                    "damage_type": actor.weapon_type,
                    "reasoning": "敌方角色已进入危机状态，优先追击压制。",
                    "in_mind_reply": f"{actor_name} 察觉到 {target.name} 已经摇摇欲坠，立刻追击。",
                },
            )

        hinder_status = self._pick_hinder_status(target)
        if hinder_status is not None:
            return Action(
                action_type=ActionType.NPCACT,
                parameters={
                    "actor": actor_name,
                    "npc_action_type": "Hinder",
                    "target": target.name,
                    "attributes": ["INS", "WLP"],
                    "target_number": 10,
                    "status_effect": hinder_status,
                    "reasoning": "先用异常状态拆掉对方最强的能力，再组织后续压制。",
                    "in_mind_reply": f"{actor_name} 试图抓住 {target.name} 的节奏破绽，先将其压入失衡。",
                },
            )

        return Action(
            action_type=ActionType.NPCACT,
            parameters={
                "actor": actor_name,
                "npc_action_type": "Attack",
                "target": target.name,
                "attributes": ["DEX", "MIG"],
                "damage_type": actor.weapon_type,
                "reasoning": "执行默认进攻动作，持续施压。",
                "in_mind_reply": f"{actor_name} 发动稳健而凶狠的攻势，逼迫 {target.name} 犯错。",
                },
            )

    def _pick_target(self, enemies):
        crisis_targets = [enemy for enemy in enemies if enemy.in_crisis and self.character_manager.guardian_for(enemy.name) is None]
        if crisis_targets:
            return sorted(crisis_targets, key=lambda character: character.hp)[0]
        unguarded = [enemy for enemy in enemies if self.character_manager.guardian_for(enemy.name) is None]
        candidates = unguarded or enemies
        return sorted(candidates, key=lambda character: character.hp)[0]

    def _pick_guard_target(self, enemies):
        if not enemies:
            return None
        priority = [enemy for enemy in enemies if not enemy.in_crisis]
        candidates = priority or enemies
        return sorted(candidates, key=lambda character: character.hp, reverse=True)[0]

    def _pick_hinder_status(self, target) -> str | None:
        strongest = max(target.attributes.items(), key=lambda item: item[1])[0]
        priority_by_attribute = {
            "DEX": ["slow", "enraged"],
            "INS": ["dazed", "enraged"],
            "MIG": ["weakened", "poisoned"],
            "WLP": ["shaken", "poisoned"],
        }
        current = {status.value for status in target.statuses}
        for candidate in priority_by_attribute.get(strongest, ["shaken"]):
            if candidate not in current:
                return candidate
        return None

    def _infer_spell_damage_type(self, actor) -> str:
        combined = " ".join(actor.spells + actor.abilities + [actor.theme, actor.identity])
        mapping = {
            "lightning": ["雷", "电", "霆"],
            "fire": ["火", "炎", "熔", "爆"],
            "ice": ["冰", "霜", "雪"],
            "wind": ["风", "岚", "暴"],
            "earth": ["土", "岩", "地"],
            "light": ["光", "圣"],
            "dark": ["暗", "影", "冥"],
        }
        for damage_type, keywords in mapping.items():
            if any(keyword in combined for keyword in keywords):
                return damage_type
        return "arcane"

    def _should_cast_spell(self, actor, target) -> bool:
        if not actor.spells or actor.mp < 5:
            return False
        if self.character_manager.guardian_for(target.name) is not None:
            return True
        return target.defenses["magic"] <= target.defenses["physical"]

    def _pick_defensive_spell(self, actor) -> str | None:
        if "魔导屏障" in actor.spells and actor.mp >= 8:
            return "魔导屏障"
        if "守护咏唱" in actor.spells and actor.mp >= 6:
            return "守护咏唱"
        return None

    def _pick_preferred_clock(self, active_clocks: list[str]) -> dict[str, int] | None:
        parsed = []
        for raw in active_clocks:
            match = self._CLOCK_PATTERN.match(raw)
            if not match:
                continue
            parsed.append(
                {
                    "name": match.group("name"),
                    "current": int(match.group("current")),
                    "max_segments": int(match.group("max")),
                }
            )
        incomplete = [clock for clock in parsed if clock["current"] < clock["max_segments"]]
        if not incomplete:
            return None
        return max(incomplete, key=lambda clock: (clock["current"] / clock["max_segments"], clock["current"]))

    def _should_push_objective(self, actor_name: str, actor, target, clock: dict[str, int]) -> bool:
        ratio = clock["current"] / clock["max_segments"]
        if target.in_crisis and self.character_manager.guardian_for(target.name) is None:
            return False
        if actor_name in self.conflict_manager.state.exalted_enemies and ratio >= 0.5:
            return True
        return ratio >= 0.75 and not actor.in_crisis

    def _objective_attributes(self, actor) -> list[str]:
        if actor.attributes.get("INS", 6) >= actor.attributes.get("DEX", 6):
            return ["INS", "WLP"]
        return ["DEX", "INS"]

    def _ensure_persona(self, actor_name: str, panel: GamePanel) -> None:
        actor = self.character_manager.get(actor_name)
        role = "最终反派或重要敌人" if self.conflict_manager.is_villain(actor_name) else "敌对 NPC"
        goals = ["击溃英雄或完成自己的计划"]
        combat_style = "高压进攻" if actor.weapon_damage >= 6 else "干扰与试探"
        drive = actor.theme or "贯彻自己的信念，不惜击倒阻碍者"
        manner = "冷酷而果断" if "villain" in actor.traits else "敌意明确"
        current_stage = self.conflict_manager.current_stage(actor_name)
        if current_stage is not None:
            goals.append(f"以【{current_stage.name}】姿态压倒英雄")
        self.world_state.ensure_npc_persona(
            actor_name,
            public_identity=actor.identity or actor_name,
            role_in_story=role,
            core_drive=drive,
            manner=manner,
            speech_style="压迫感强、用词锐利",
            combat_style=combat_style,
            first_scene=panel.game_phase,
            goals=goals,
        )


class LLMNPCDirector:
    """根据 NPC 人设档案与当前场景，用真实 LLM 决定 NPC 行动。"""

    def __init__(
        self,
        client: OpenAICompatibleClient,
        model: str,
        character_manager: CharacterManager,
        conflict_manager: ConflictManager,
        world_state: WorldState,
        fallback: NPCDirector | None = None,
        allow_fallback: bool = False,
    ) -> None:
        self.client = client
        self.model = model
        self.character_manager = character_manager
        self.conflict_manager = conflict_manager
        self.world_state = world_state
        self.fallback = fallback or HeuristicNPCDirector(character_manager, conflict_manager, world_state)
        self.allow_fallback = allow_fallback
        self.last_raw_content = ""
        self.last_error = ""
        self.last_used_fallback = False

    def decide(self, panel: GamePanel, actor_name: str) -> Action:
        self._ensure_persona(actor_name, panel)
        try:
            self.last_used_fallback = False
            self.last_error = ""
            actor = self.character_manager.get(actor_name)
            persona_prompt = self.world_state.render_npc_prompt(actor_name)
            current_status = self.character_manager.format_status(actor)
            ultima_points = self.conflict_manager.state.ultima_points.get(actor_name, 0)
            tactical_snapshot = {}
            if isinstance(self.fallback, HeuristicNPCDirector):
                tactical_snapshot = self.fallback.build_tactical_snapshot(panel, actor_name)
            content = self.client.create_chat_completion(
                model=self.model,
                messages=build_cache_friendly_messages(
                    static_system_prompt=NPC_ACT_SYSTEM_PROMPT,
                    user_content=(
                        "请根据当前局势，输出这个 NPC 这一回合的行动 JSON。\n"
                        "返回格式示例："
                        '{"action_type":"NPCAct","parameters":{"actor":"帝国机甲","npc_action_type":"Attack",'
                        '"target":"瓦莉亚","attributes":["DEX","MIG"],"damage_type":"physical",'
                        '"reasoning":"为什么采取该动作","in_mind_reply":"对外表现的气势"}}\n'
                        f"{system_reminder('当前 NPC 人设档案', persona_prompt)}\n"
                        f"当前行动者：{actor_name}\n"
                        f"当前状态：{current_status}\n"
                        f"当前终结点：{ultima_points}\n"
                        f"硬规则战术摘要：\n{json.dumps(tactical_snapshot, ensure_ascii=False, indent=2)}\n"
                        f"游戏面板：\n{json.dumps(panel.__dict__, ensure_ascii=False, indent=2)}"
                    ),
                ),
                temperature=0.3,
                response_format={"type": "json_object"},
            )
            self.last_raw_content = content
            data = extract_json_object(content)
            return Action(
                action_type=ActionType(data["action_type"]),
                parameters=data["parameters"],
            )
        except Exception as exc:
            self.last_error = str(exc)
            if self.allow_fallback:
                self.last_used_fallback = True
                return self.fallback.decide(panel, actor_name)
            self.last_used_fallback = False
            raise RuntimeError("LLMNPCDirector failed and heuristic fallback is disabled.") from exc

    def _ensure_persona(self, actor_name: str, panel: GamePanel) -> None:
        actor = self.character_manager.get(actor_name)
        role = "最终反派或重要敌人" if self.conflict_manager.is_villain(actor_name) else "敌对 NPC"
        goals = ["击败英雄", "推进自己的计划"]
        if actor_name in self.conflict_manager.state.exalted_enemies:
            goals.append("在升格后证明自己的意志高于英雄")
        self.world_state.ensure_npc_persona(
            actor_name,
            public_identity=actor.identity or actor_name,
            role_in_story=role,
            core_drive=actor.theme or "不惜代价贯彻自身目标",
            manner="戏剧化、强势、有压迫感" if "villain" in actor.traits else "警惕、敌对",
            speech_style="带有 JRPG 反派或强敌风格的压迫性表达",
            combat_style="善于施压、根据局势切换攻击与控制",
            first_scene=panel.game_phase,
            goals=goals,
        )
