from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class AllyNPCAbility:
    name: str
    timing: str
    description: str
    mechanical_hint: str = ""
    uses_remaining: int = -1
    public_cue: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class AllyNPCState:
    name: str
    role: str = ""
    disposition: str = "friendly"
    scene: str = ""
    abilities: list[AllyNPCAbility] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class AllyNPCTriggerResult:
    ally_name: str
    ability_name: str
    timing: str
    summary: str
    mechanical_hint: str = ""
    created_at: str = ""


class AllyNPCManager:
    """Simple trigger-window model for allied NPCs.

    Allies should support the table without becoming full extra PCs. Their
    abilities fire at clear windows such as round end or when a PC would fall.
    """

    PRESET_ABILITIES: dict[str, dict[str, object]] = {
        "屠龙者": {
            "timing": "pc_turn_end",
            "description": "盟友协助英雄压制一名强敌，可叙事为重击、牵制或迫使飞行目标落地。",
            "mechanical_hint": "可裁定为对一个敌人造成少量到中等伤害，或使一个飞行目标落地至当前轮结束。",
            "public_cue": "盟友抓住英雄制造的破绽",
            "tags": ["damage", "boss", "support"],
        },
        "元素附魔": {
            "timing": "round_end",
            "description": "盟友短暂为一名英雄的武器或招式附上元素力量。",
            "mechanical_hint": "选择风、雷、冰、火、土、暗、光、毒之一；目标下次造成的伤害可改为该类型。",
            "public_cue": "盟友把一缕元素光压进英雄的武器",
            "tags": ["element", "buff"],
        },
        "元素防护": {
            "timing": "round_end",
            "description": "盟友展开短暂防护，让英雄们能看见并利用即将到来的元素威胁。",
            "mechanical_hint": "可令至多三名盟友对一种伤害类型获得抵抗，持续到下一轮结束或一次明确威胁结算后。",
            "public_cue": "盟友展开一层薄而清晰的护光",
            "tags": ["element", "defense"],
        },
        "能量波": {
            "timing": "round_end",
            "description": "盟友释放一阵不抢戏的能量波，清理杂兵或打断环境压力。",
            "mechanical_hint": "可对一组弱小敌人造成少量伤害，或在一个环境/威胁命刻上擦除 1 格。",
            "public_cue": "盟友把积蓄的灵魂能量推出去",
            "tags": ["area", "clock", "support"],
        },
        "完全愈合": {
            "timing": "pc_turn_end",
            "description": "盟友抓住喘息窗口，为一名英雄稳定伤势。",
            "mechanical_hint": "可恢复一名 PC 少量到中等 HP；通常每场景一次。",
            "uses_remaining": 1,
            "public_cue": "盟友把治疗力量按在伤口上",
            "tags": ["heal", "limited"],
        },
        "第二次机会": {
            "timing": "pc_zero_hp",
            "description": "当英雄即将倒下时，盟友用承诺、护符或治疗把他从边缘拉回来。",
            "mechanical_hint": "可在一名 PC HP 将归零时改为保留 1 HP；通常每章节或每场景一次。",
            "uses_remaining": 1,
            "public_cue": "盟友冲进危险中拽住英雄",
            "tags": ["survive", "limited"],
        },
    }

    def __init__(self) -> None:
        self.allies: dict[str, AllyNPCState] = {}
        self.trigger_log: list[AllyNPCTriggerResult] = []

    def register_ally(
        self,
        name: str,
        *,
        role: str = "",
        disposition: str = "friendly",
        scene: str = "",
        abilities: list[AllyNPCAbility] | None = None,
        notes: list[str] | None = None,
    ) -> AllyNPCState:
        state = AllyNPCState(
            name=name,
            role=role,
            disposition=disposition,
            scene=scene,
            abilities=list(abilities or []),
            notes=list(notes or []),
        )
        self.allies[name] = state
        return state

    def add_ability(
        self,
        ally_name: str,
        *,
        name: str,
        timing: str,
        description: str,
        mechanical_hint: str = "",
        uses_remaining: int = -1,
        public_cue: str = "",
        tags: list[str] | None = None,
    ) -> AllyNPCAbility:
        ally = self.allies.setdefault(ally_name, AllyNPCState(name=ally_name))
        ability = AllyNPCAbility(
            name=name,
            timing=timing,
            description=description,
            mechanical_hint=mechanical_hint,
            uses_remaining=uses_remaining,
            public_cue=public_cue,
            tags=list(tags or []),
        )
        ally.abilities.append(ability)
        return ability

    def add_preset_ability(
        self,
        ally_name: str,
        preset_name: str,
        *,
        timing: str = "",
        uses_remaining: int | None = None,
        public_cue: str = "",
        tags: list[str] | None = None,
    ) -> AllyNPCAbility:
        preset = self.PRESET_ABILITIES.get(preset_name)
        if preset is None:
            known = "、".join(sorted(self.PRESET_ABILITIES))
            raise ValueError(f"未知盟友 NPC 预设能力【{preset_name}】；可用预设：{known}。")
        merged_tags = [str(item) for item in preset.get("tags", [])]
        for item in tags or []:
            if item not in merged_tags:
                merged_tags.append(item)
        return self.add_ability(
            ally_name,
            name=preset_name,
            timing=timing or str(preset.get("timing") or "round_end"),
            description=str(preset.get("description") or ""),
            mechanical_hint=str(preset.get("mechanical_hint") or ""),
            uses_remaining=uses_remaining if uses_remaining is not None else int(preset.get("uses_remaining", -1)),
            public_cue=public_cue or str(preset.get("public_cue") or ""),
            tags=merged_tags,
        )

    def trigger(self, timing: str, *, scene: str = "", context: str = "") -> list[AllyNPCTriggerResult]:
        results: list[AllyNPCTriggerResult] = []
        for ally in self.allies.values():
            if scene and ally.scene and ally.scene != scene:
                continue
            for ability in ally.abilities:
                if ability.timing != timing or ability.uses_remaining == 0:
                    continue
                if ability.uses_remaining > 0:
                    ability.uses_remaining -= 1
                summary = self._summary(ally, ability, context=context)
                result = AllyNPCTriggerResult(
                    ally_name=ally.name,
                    ability_name=ability.name,
                    timing=timing,
                    summary=summary,
                    mechanical_hint=ability.mechanical_hint,
                    created_at=self._now(),
                )
                self.trigger_log.append(result)
                results.append(result)
        return results

    def audit_payload(self, *, limit: int = 20) -> dict[str, Any]:
        return {
            "allies": [asdict(ally) for ally in self.allies.values()],
            "recent_triggers": [asdict(result) for result in self.trigger_log[-limit:]],
            "usage_note": "盟友 NPC 使用触发窗口支援玩家，不应占用完整 PC 回合；具体叙事交给 GM/LLM 表达。",
        }

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "allies": [asdict(ally) for ally in self.allies.values()],
            "trigger_log": [asdict(result) for result in self.trigger_log],
        }

    def apply_snapshot(self, snapshot: dict[str, Any] | None) -> None:
        snapshot = snapshot or {}
        self.allies = {}
        for ally_data in snapshot.get("allies", []):
            abilities = [AllyNPCAbility(**item) for item in ally_data.get("abilities", [])]
            ally = AllyNPCState(
                name=ally_data.get("name", ""),
                role=ally_data.get("role", ""),
                disposition=ally_data.get("disposition", "friendly"),
                scene=ally_data.get("scene", ""),
                abilities=abilities,
                notes=list(ally_data.get("notes", [])),
            )
            if ally.name:
                self.allies[ally.name] = ally
        self.trigger_log = [AllyNPCTriggerResult(**item) for item in snapshot.get("trigger_log", [])]

    def _summary(self, ally: AllyNPCState, ability: AllyNPCAbility, *, context: str) -> str:
        cue = f"{ability.public_cue}。" if ability.public_cue else ""
        context_text = f" 当前上下文：{context}" if context else ""
        return f"{cue}{ally.name} 触发【{ability.name}】：{ability.description}{context_text}".strip()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
