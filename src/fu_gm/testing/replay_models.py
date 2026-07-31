from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ReplayStep:
    id: str
    kind: str
    label: str = ""
    speaker: str = ""
    endpoint: str = ""
    method: str = "POST"
    message: str = ""
    stage_goal: str = ""
    intent: str = ""
    actor: str = ""
    target: str = ""
    method_hint: str = ""
    allowed_terms: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    expected: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ReplayStep":
        return cls(
            id=str(raw.get("id") or raw.get("label") or ""),
            kind=str(raw.get("kind") or "player_message"),
            label=str(raw.get("label") or raw.get("id") or ""),
            speaker=str(raw.get("speaker") or ""),
            endpoint=str(raw.get("endpoint") or ""),
            method=str(raw.get("method") or "POST").upper(),
            message=str(raw.get("message") or ""),
            stage_goal=str(raw.get("stage_goal") or ""),
            intent=str(raw.get("intent") or ""),
            actor=str(raw.get("actor") or ""),
            target=str(raw.get("target") or ""),
            method_hint=str(raw.get("method_hint") or ""),
            allowed_terms=[str(item) for item in raw.get("allowed_terms", [])],
            constraints=[str(item) for item in raw.get("constraints", [])],
            expected=[str(item) for item in raw.get("expected", [])],
            payload=dict(raw.get("payload") or {}),
        )


@dataclass(frozen=True)
class ReplayScenario:
    name: str
    campaign_id: str
    session_id: str
    channel_id: str
    participants: list[str]
    steps: list[ReplayStep]
    description: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ReplayScenario":
        return cls(
            name=str(raw.get("name") or "未命名回放"),
            campaign_id=str(raw.get("campaign_id") or "replay-campaign"),
            session_id=str(raw.get("session_id") or "replay-session"),
            channel_id=str(raw.get("channel_id") or "replay-channel"),
            participants=[str(item) for item in raw.get("participants", [])],
            steps=[ReplayStep.from_dict(item) for item in raw.get("steps", [])],
            description=str(raw.get("description") or ""),
        )

    @classmethod
    def load(cls, path: str | Path) -> "ReplayScenario":
        scenario_path = Path(path)
        data = json.loads(scenario_path.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @property
    def common_payload(self) -> dict[str, str]:
        return {
            "campaign_id": self.campaign_id,
            "session_id": self.session_id,
            "channel_id": self.channel_id,
        }


@dataclass
class LegalActionContext:
    stage_goal: str
    scene_name: str = ""
    scene_location: str = ""
    current_actor: str = ""
    conflict_active: bool = False
    known_pcs: list[str] = field(default_factory=list)
    pc_resources: dict[str, dict[str, Any]] = field(default_factory=dict)
    known_enemies: list[str] = field(default_factory=list)
    known_npcs: list[str] = field(default_factory=list)
    present_npcs: list[str] = field(default_factory=list)
    present_pcs: list[str] = field(default_factory=list)
    presence_authoritative: bool = False
    actor_locations: dict[str, str] = field(default_factory=dict)
    story_items: list[dict[str, Any]] = field(default_factory=list)
    visible_scene_elements: list[str] = field(default_factory=list)
    established_scene_facts: list[str] = field(default_factory=list)
    immediate_scene_consequence: str = ""
    blocked_routes: list[str] = field(default_factory=list)
    active_clocks: list[str] = field(default_factory=list)
    open_npc_conditions: list[dict[str, str]] = field(default_factory=list)
    settled_npc_exchanges: list[dict[str, str]] = field(default_factory=list)
    legal_actions: list[str] = field(default_factory=list)
    legal_spells: list[str] = field(default_factory=list)
    legal_spell_rules: list[dict[str, Any]] = field(default_factory=list)
    legal_skills: list[str] = field(default_factory=list)
    legal_skill_rules: list[dict[str, Any]] = field(default_factory=list)
    pending_decisions: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def legal_action_names(self) -> list[str]:
        return list(self.legal_actions)


@dataclass
class ReplayCallRecord:
    index: int
    step_id: str
    label: str
    method: str
    endpoint: str
    speaker: str
    message: str
    status: int
    elapsed_ms: int
    ok: bool
    reply: str
    body: dict[str, Any]
    legal_context: dict[str, Any] = field(default_factory=dict)
    validation_errors: list[str] = field(default_factory=list)
