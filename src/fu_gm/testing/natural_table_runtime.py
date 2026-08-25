from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from typing import Callable, Mapping

from fu_gm.testing.luna_player_agent import (
    DEFAULT_LONGRUN_PERSONAS,
    LunaPlayerAgent,
    PlayerPersona,
)
from fu_gm.testing.player_simulator import SimulatedUtterance
from fu_gm.testing.replay_models import LegalActionContext, ReplayStep


@dataclass(frozen=True)
class PublicTableEvent:
    """One player-visible message delivered to every simulated player."""

    event_id: int
    speaker: str
    text: str
    role: str = "gm"
    reply_to_event_id: int | None = None
    action_bar: dict[str, object] = field(default_factory=dict)

    def prompt_payload(self) -> dict[str, object]:
        return {
            "event_id": int(self.event_id),
            "speaker": self.speaker,
            "role": self.role,
            "text": self.text,
            "reply_to_event_id": self.reply_to_event_id,
            "action_bar": dict(self.action_bar),
        }


@dataclass
class PlayerMind:
    """Small private state that survives between public table messages."""

    player_name: str
    hero_name: str
    focus: str = ""
    belief: str = ""
    commitment: str = ""
    mood: str = ""
    private_brief: str = ""
    last_seen_event_id: int = 0
    last_spoke_event_id: int = 0
    silence_streak: int = 0

    def prompt_payload(self) -> dict[str, object]:
        return asdict(self)

    def apply_update(
        self,
        update: Mapping[str, object] | None,
        *,
        include_commitment: bool = True,
    ) -> None:
        if not update:
            return
        keys = ["focus", "belief", "mood"]
        if include_commitment:
            keys.append("commitment")
        for key in keys:
            value = " ".join(str(update.get(key) or "").split()).strip()
            if value:
                setattr(self, key, value[:240])


@dataclass(frozen=True)
class NaturalTableCandidate:
    player_name: str
    hero_name: str
    based_on_event_id: int
    utterance: SimulatedUtterance
    generation_order: int

    @property
    def text(self) -> str:
        return str(self.utterance.text or "").strip()

    @property
    def expects_gm_reply(self) -> bool:
        return bool(
            self.utterance.audience in {"gm", "npc"}
            or self.utterance.utterance_kind in {"action", "rules_question"}
        )


@dataclass(frozen=True)
class NaturalTableWave:
    event: PublicTableEvent
    candidates: tuple[NaturalTableCandidate, ...]
    reactions: tuple[NaturalTableCandidate, ...]
    waiting_players: tuple[str, ...]
    heartbeat_due: bool

    @property
    def all_wait(self) -> bool:
        return not self.candidates


ContextFactory = Callable[[str, str], LegalActionContext]
AgentFactory = Callable[[PlayerPersona], LunaPlayerAgent]


class NaturalTableRuntime:
    """Broadcast-driven FU-PL table with no framework-selected speaker.

    Every public event is observed by every player mind. Each player's own
    model independently chooses ``speak`` or ``wait`` and supplies a reaction
    delay. The runtime only serializes candidates by that delay, just as a chat
    transport would serialize messages that users independently decided to
    send.
    """

    engine_name = "natural_v1"

    def __init__(
        self,
        *,
        client: object | None = None,
        model: str = "",
        personas: Mapping[str, PlayerPersona] | None = None,
        agent_factory: AgentFactory | None = None,
        player_briefs: Mapping[str, str] | None = None,
        heartbeat_after_quiet_waves: int = 1,
    ) -> None:
        resolved_personas = (
            dict(personas)
            if personas is not None
            else dict(DEFAULT_LONGRUN_PERSONAS)
        )
        self.personas = resolved_personas
        self._event_counter = 0
        self._quiet_waves = 0
        self.last_action_progress_review: dict[str, object] = {}
        self.last_table_discussion_review: dict[str, object] = {}
        self.heartbeat_after_quiet_waves = max(
            1, int(heartbeat_after_quiet_waves)
        )
        briefs = dict(player_briefs or {})
        self.minds = {
            name: PlayerMind(
                name,
                persona.hero_name,
                private_brief=str(briefs.get(name) or "").strip()[:4000],
            )
            for name, persona in self.personas.items()
        }
        if agent_factory is not None:
            self.agents = {
                name: agent_factory(persona)
                for name, persona in self.personas.items()
            }
        else:
            self.agents: dict[str, LunaPlayerAgent] = {}
            shared_client = client
            shared_model = model
            for name, persona in self.personas.items():
                agent = LunaPlayerAgent(
                    use_llm=True,
                    client=shared_client,
                    model=shared_model,
                    personas={persona.player_name: persona},
                    continue_on_invalid=True,
                )
                if shared_client is None:
                    shared_client = agent.client
                    shared_model = agent.model
                self.agents[name] = agent

    @property
    def model(self) -> str:
        first = next(iter(self.agents.values()), None)
        return str(getattr(first, "model", "") or "")

    @property
    def use_llm(self) -> bool:
        return any(bool(getattr(agent, "use_llm", False)) for agent in self.agents.values())

    @property
    def client(self) -> object | None:
        first = next(iter(self.agents.values()), None)
        return getattr(first, "client", None)

    def new_event(
        self,
        *,
        speaker: str,
        text: str,
        role: str = "gm",
        reply_to_event_id: int | None = None,
        action_bar: Mapping[str, object] | None = None,
    ) -> PublicTableEvent:
        self._event_counter += 1
        return PublicTableEvent(
            event_id=self._event_counter,
            speaker=str(speaker or "").strip(),
            text=str(text or "").strip(),
            role=str(role or "gm").strip().lower(),
            reply_to_event_id=reply_to_event_id,
            action_bar=dict(action_bar or {}),
        )

    def react(
        self,
        event: PublicTableEvent,
        *,
        context_factory: ContextFactory,
        recent_public_context: str,
        last_gm_reply: str = "",
        stale_drafts: Mapping[str, NaturalTableCandidate] | None = None,
    ) -> NaturalTableWave:
        """Ask every independent player mind whether this event merits speech."""

        stale_drafts = dict(stale_drafts or {})
        candidates: list[NaturalTableCandidate] = []
        reactions_by_order: dict[int, NaturalTableCandidate] = {}
        waiting: list[str] = []
        blocking_decision_exists = False
        prepared: list[
            tuple[
                int,
                str,
                PlayerPersona,
                PlayerMind,
                LegalActionContext,
                ReplayStep,
                dict[str, object],
            ]
        ] = []

        for generation_order, (player_name, persona) in enumerate(
            self.personas.items()
        ):
            mind = self.minds[player_name]
            mind.last_seen_event_id = max(mind.last_seen_event_id, event.event_id)

            # The author still observes their own message, but does not answer
            # it before anyone else has had a chance to react.
            if event.role == "player" and event.speaker == player_name:
                mind.silence_streak += 1
                waiting.append(player_name)
                reactions_by_order[generation_order] = NaturalTableCandidate(
                        player_name=player_name,
                        hero_name=persona.hero_name,
                        based_on_event_id=event.event_id,
                        utterance=SimulatedUtterance(
                            text="",
                            decision="wait",
                            utterance_kind="wait",
                            audience="table",
                        ),
                        generation_order=generation_order,
                )
                continue

            context = context_factory(player_name, persona.hero_name)
            owned, foreign = self._partition_pending_decisions(
                context.pending_decisions,
                player_name=player_name,
                hero_name=persona.hero_name,
            )
            blocking_decision_exists = blocking_decision_exists or bool(owned)
            context = replace(context, pending_decisions=owned)
            action_bar = self._action_bar_for_player(
                context,
                player_name=player_name,
                hero_name=persona.hero_name,
                foreign_pending=foreign,
                shared=event.action_bar,
            )
            stale = stale_drafts.get(player_name)
            event_payload = event.prompt_payload()
            event_payload["action_bar"] = action_bar
            if stale is not None:
                event_payload["stale_draft"] = {
                    "based_on_event_id": stale.based_on_event_id,
                    "text": stale.text,
                    "kind": stale.utterance.utterance_kind,
                    "audience": stale.utterance.audience,
                }

            step = ReplayStep(
                id=f"natural-event-{event.event_id}-{player_name}",
                kind=(
                    "session_zero_message"
                    if str(action_bar.get("phase") or "") == "session_zero"
                    else "player_message"
                ),
                speaker=player_name,
                actor=persona.hero_name,
                payload={"natural_broadcast": True},
            )
            prepared.append(
                (
                    generation_order,
                    player_name,
                    persona,
                    mind,
                    context,
                    step,
                    event_payload,
                )
            )

        def compose_player(
            item: tuple[
                int,
                str,
                PlayerPersona,
                PlayerMind,
                LegalActionContext,
                ReplayStep,
                dict[str, object],
            ],
        ) -> tuple[int, str, PlayerPersona, PlayerMind, SimulatedUtterance]:
            (
                generation_order,
                player_name,
                persona,
                mind,
                context,
                step,
                event_payload,
            ) = item
            utterance = self.agents[player_name].compose(
                step=step,
                legal_context=context,
                last_gm_reply=last_gm_reply,
                recent_public_context=recent_public_context,
                player_mind=mind.prompt_payload(),
                natural_table_event=event_payload,
                record_public_history=False,
            )
            return generation_order, player_name, persona, mind, utterance

        if len(prepared) > 1:
            with ThreadPoolExecutor(
                max_workers=len(prepared),
                thread_name_prefix="fu-pl",
            ) as executor:
                composed = list(executor.map(compose_player, prepared))
        else:
            composed = [compose_player(item) for item in prepared]

        for generation_order, player_name, persona, mind, utterance in composed:
            reaction = NaturalTableCandidate(
                player_name=player_name,
                hero_name=persona.hero_name,
                based_on_event_id=event.event_id,
                utterance=utterance,
                generation_order=generation_order,
            )
            reactions_by_order[generation_order] = reaction
            if utterance.decision == "speak" and str(utterance.text or "").strip():
                candidates.append(reaction)
            else:
                mind.apply_update(utterance.private_mind_update)
                mind.silence_streak += 1
                waiting.append(player_name)

        candidates.sort(
            key=lambda item: (
                int(item.utterance.speak_after_ms),
                self._transport_tie_break(event.event_id, item.player_name),
            )
        )
        if candidates:
            self._quiet_waves = 0
        else:
            self._quiet_waves += 1
        heartbeat_due = bool(
            not candidates
            and not blocking_decision_exists
            and self._quiet_waves >= self.heartbeat_after_quiet_waves
        )
        return NaturalTableWave(
            event=event,
            candidates=tuple(candidates),
            reactions=tuple(
                reactions_by_order[index]
                for index in sorted(reactions_by_order)
            ),
            waiting_players=tuple(waiting),
            heartbeat_due=heartbeat_due,
        )

    def commit_candidate(self, candidate: NaturalTableCandidate) -> None:
        """Commit public-memory effects only after transport delivers a draft."""

        mind = self.minds.get(candidate.player_name)
        if mind is None:
            return
        mind.last_spoke_event_id = max(
            mind.last_spoke_event_id,
            candidate.based_on_event_id,
        )
        mind.silence_streak = 0
        mind.apply_update(candidate.utterance.private_mind_update)
        agent = self.agents.get(candidate.player_name)
        recorder = getattr(agent, "record_delivered", None)
        if callable(recorder):
            recorder(candidate.player_name, candidate.text)

    @staticmethod
    def _transport_tie_break(event_id: int, player_name: str) -> int:
        """Resolve equal model delays without privileging persona list order."""

        digest = hashlib.sha256(
            f"{int(event_id)}:{str(player_name or '').strip()}".encode("utf-8")
        ).digest()
        return int.from_bytes(digest[:8], "big")

    def compose(
        self,
        *,
        step: ReplayStep,
        legal_context: LegalActionContext,
        last_gm_reply: str = "",
        recent_public_context: str = "",
        **_unused: object,
    ) -> SimulatedUtterance:
        """Compatibility path for a decision window owned by one player.

        Natural broadcasts use :meth:`react`. A durable rules choice or a GM
        clarification already has one authoritative respondent, so it should
        return to that player's persistent mind rather than wake the full table.
        """

        player_name = str(step.speaker or "").strip()
        agent = self.agents.get(player_name)
        mind = self.minds.get(player_name)
        if agent is None or mind is None:
            return SimulatedUtterance(
                text="",
                used_fallback=True,
                validation_errors=["natural_player_not_registered"],
                decision="wait",
                utterance_kind="wait",
            )
        utterance = agent.compose(
            step=step,
            legal_context=legal_context,
            last_gm_reply=last_gm_reply,
            recent_public_context=recent_public_context,
            player_mind=mind.prompt_payload(),
        )
        mind.apply_update(utterance.private_mind_update)
        if str(utterance.text or "").strip():
            mind.silence_streak = 0
        else:
            mind.silence_streak += 1
        self.last_action_progress_review = dict(
            getattr(agent, "last_action_progress_review", {}) or {}
        )
        self.last_table_discussion_review = dict(
            getattr(agent, "last_table_discussion_review", {}) or {}
        )
        return utterance

    def snapshot(self) -> dict[str, object]:
        return {
            "version": 2,
            "event_counter": self._event_counter,
            "quiet_waves": self._quiet_waves,
            "minds": {
                name: mind.prompt_payload() for name, mind in self.minds.items()
            },
        }

    def restore(self, snapshot: Mapping[str, object] | None) -> None:
        payload = dict(snapshot or {})
        self._event_counter = max(0, int(payload.get("event_counter") or 0))
        self._quiet_waves = max(0, int(payload.get("quiet_waves") or 0))
        raw_minds = payload.get("minds")
        if not isinstance(raw_minds, Mapping):
            return
        for player_name, raw in raw_minds.items():
            if player_name not in self.minds or not isinstance(raw, Mapping):
                continue
            mind = self.minds[player_name]
            for key in ("focus", "belief", "commitment", "mood"):
                setattr(mind, key, str(raw.get(key) or "")[:240])
            if "private_brief" in raw:
                mind.private_brief = str(raw.get("private_brief") or "")[:4000]
            mind.last_seen_event_id = max(
                0, int(raw.get("last_seen_event_id") or 0)
            )
            mind.last_spoke_event_id = max(
                0, int(raw.get("last_spoke_event_id") or 0)
            )
            mind.silence_streak = max(0, int(raw.get("silence_streak") or 0))

    def telemetry_payload(self) -> dict[str, object]:
        clients: dict[int, object] = {}
        for agent in self.agents.values():
            client = getattr(agent, "client", None)
            if client is not None:
                clients[id(client)] = client
        if len(clients) == 1:
            client = next(iter(clients.values()))
            getter = getattr(client, "telemetry_payload", None)
            if callable(getter):
                return dict(getter() or {})
        return {
            "players": {
                name: agent.telemetry_payload()
                for name, agent in self.agents.items()
            }
        }

    @staticmethod
    def _partition_pending_decisions(
        decisions: list[dict[str, object]],
        *,
        player_name: str,
        hero_name: str,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        owned: list[dict[str, object]] = []
        foreign: list[dict[str, object]] = []
        aliases = {player_name, hero_name}
        for decision in decisions:
            owner = str(
                decision.get("owner")
                or decision.get("player_name")
                or decision.get("actor")
                or ""
            ).strip()
            if not owner or owner in aliases:
                owned.append(dict(decision))
            else:
                foreign.append(dict(decision))
        return owned, foreign

    @staticmethod
    def _action_bar_for_player(
        context: LegalActionContext,
        *,
        player_name: str,
        hero_name: str,
        foreign_pending: list[dict[str, object]],
        shared: Mapping[str, object],
    ) -> dict[str, object]:
        current_actor = str(context.current_actor or "").strip()
        is_current = bool(not context.conflict_active or current_actor == hero_name)
        shared_pending = [
            item
            for item in shared.get("pending_decisions") or []
            if isinstance(item, Mapping)
        ]
        aliases = {player_name, hero_name}

        def pending_owners(item: Mapping[str, object]) -> set[str]:
            owners = {
                str(item.get("owner") or "").strip(),
                str(item.get("player_name") or "").strip(),
                str(item.get("actor") or "").strip(),
            }
            allowed = item.get("allowed_speakers")
            if isinstance(allowed, (list, tuple, set)):
                owners.update(str(value or "").strip() for value in allowed)
            return {value for value in owners if value}

        shared_for_you = any(
            not pending_owners(item) or not aliases.isdisjoint(pending_owners(item))
            for item in shared_pending
        )
        shared_for_another = any(
            bool(pending_owners(item)) and aliases.isdisjoint(pending_owners(item))
            for item in shared_pending
        )
        missing_by_player = shared.get("session_zero_missing_by_player")
        hero_missing_by_player = shared.get("hero_missing_by_player")
        your_session_zero_missing = (
            list(missing_by_player.get(player_name) or [])
            if isinstance(missing_by_player, Mapping)
            else []
        )
        your_hero_missing = (
            list(hero_missing_by_player.get(player_name) or [])
            if isinstance(hero_missing_by_player, Mapping)
            else []
        )
        public_shared = {
            key: value
            for key, value in dict(shared).items()
            if key
            not in {
                "session_zero_missing_by_player",
                "hero_missing_by_player",
            }
        }
        return {
            **public_shared,
            "player": player_name,
            "hero": hero_name,
            "conflict_active": bool(context.conflict_active),
            "current_actor": current_actor,
            "you_are_current_actor": is_current,
            "may_take_rules_action": is_current,
            "may_chat": True,
            "pending_decision_for_you": bool(context.pending_decisions)
            or shared_for_you,
            "another_player_has_pending_decision": bool(foreign_pending)
            or shared_for_another,
            "your_session_zero_missing": your_session_zero_missing,
            "your_hero_missing": your_hero_missing,
        }
