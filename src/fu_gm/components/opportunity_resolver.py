from __future__ import annotations

from typing import Callable

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.combat_trait_manager import CombatTraitManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.economy_manager import EconomyManager
from fu_gm.components.post_check_state_journal import PostCheckStateJournal
from fu_gm.components.world_state import WorldState
from fu_gm.models import Action, ActionResolution, Affinity, ClockChange, StatusEffect


class OpportunityResolver:
    """Resolve Fabula Ultima opportunity effects through one rule authority."""

    _TOOL_PARAMETER_GUIDE = (
        "机会参数按核心规则提交：揭示=target；进展=clock_name，可选delta(0至2)、erase；"
        "纽带=target及emotion/emotions，情感使用赞赏/自卑、忠诚/不信任、喜爱/憎恨；"
        "情报=information（玩家未自定时由GM依据真实暗线给出）；青睐=target，可选description，"
        "但GM机会必须明确description；"
        "审视=target，可选scan_type(弱点/特质)，不得编造目标没有的数据；"
        "失态=target及statement，言论由该生物的操控者决定；"
        "失物若影响角色物品用target及item_name，若影响现场已有的门、钥匙、装置等物件，"
        "用scene_object及description；受苦=target及status_effect(dazed/shaken/slow/weakened)；"
        "优势=target；转折=subject，可选description；自定义=description。"
    )

    _ALIASES = {
        "揭示": "reveal",
        "进展": "progress",
        "纽带": "bond",
        "情报": "information",
        "青睐": "favor",
        "审视": "scan",
        "失态": "misstep",
        "失物": "lost_item",
        "受苦": "suffer",
        "优势": "advantage",
        "转折": "twist",
        "转折!": "twist",
        "转折！": "twist",
        "自定义": "custom",
        "法术附加效果": "spell_effect",
    }
    _VALID_EFFECTS = {
        "reveal",
        "progress",
        "bond",
        "information",
        "favor",
        "scan",
        "misstep",
        "lost_item",
        "suffer",
        "advantage",
        "twist",
        "custom",
        "spell_effect",
    }
    _SUFFER_STATUSES = {
        StatusEffect.DAZED,
        StatusEffect.SHAKEN,
        StatusEffect.SLOW,
        StatusEffect.WEAKENED,
    }
    _DAMAGE_TYPE_NAMES = {
        "physical": "物理",
        "air": "风",
        "bolt": "雷",
        "dark": "暗",
        "earth": "土",
        "fire": "火",
        "ice": "冰",
        "light": "光",
        "poison": "毒",
    }

    def __init__(
        self,
        *,
        characters: CharacterManager,
        clocks: ClockManager,
        conflict: ConflictManager,
        world: WorldState,
        post_check_state: PostCheckStateJournal,
        economy: EconomyManager,
        ensure_clock_exists: Callable[..., None],
        status_effect: Callable[[object], StatusEffect],
        status_name: Callable[[StatusEffect], str],
        reveal_motivation: Callable[[str], str] | None = None,
    ) -> None:
        self.characters = characters
        self.clocks = clocks
        self.conflict = conflict
        self.world = world
        self.post_check_state = post_check_state
        self.economy = economy
        self.ensure_clock_exists = ensure_clock_exists
        self.status_effect = status_effect
        self.status_name = status_name
        self.reveal_motivation_provider = reveal_motivation
        self.combat_traits = CombatTraitManager()

    def resolve(self, action: Action) -> ActionResolution:
        effect = str(action.parameters.get("effect") or action.parameters.get("opportunity") or "").strip()
        normalized = self.normalize_effect(effect)
        actor = str(action.parameters.get("actor") or "system")
        payload: dict[str, object] = {"effect": normalized}
        if normalized not in self._VALID_EFFECTS:
            raise ValueError(f"未知机会效果【{effect or normalized}】。")

        if normalized == "spell_effect":
            metadata = action.parameters.get("_spell_opportunity")
            if not isinstance(metadata, dict):
                raise ValueError("法术专属机会缺少可信的原始法术效果。")
            targets = [
                str(name)
                for name in metadata.get("targets", [])
                if str(name) and self.characters.exists(str(name))
            ]
            applied_statuses: dict[str, list[str]] = {}
            penalized: list[str] = []
            grounded: list[str] = []
            for target_name in targets:
                statuses = []
                for raw_status in metadata.get("statuses", []):
                    status = self.status_effect(raw_status)
                    if self.conflict.apply_status(target_name, status):
                        statuses.append(status.value)
                if statuses:
                    applied_statuses[target_name] = statuses
                turn_penalty = int(metadata.get("turn_penalty", 0) or 0)
                if turn_penalty > 0:
                    self.conflict.penalize_next_turn(
                        target_name,
                        turn_penalty,
                    )
                    penalized.append(target_name)
                if metadata.get("ground_flying"):
                    event = self.combat_traits.suppress_flight_by_opportunity(
                        self.characters.get(target_name)
                    )
                    if event is not None:
                        if event.effect is not None:
                            self.conflict.register_effect(event.effect)
                        grounded.append(target_name)
            payload.update(
                {
                    "spell_name": str(metadata.get("spell_name") or ""),
                    "targets": targets,
                    "status_applied_by_target": applied_statuses,
                    "turn_penalty_targets": penalized,
                    "grounded_targets": grounded,
                }
            )
            effects: list[str] = []
            if applied_statuses:
                effects.append(
                    "施加异常："
                    + "、".join(applied_statuses)
                )
            if penalized:
                effects.append("下回合少一次行动：" + "、".join(penalized))
            if grounded:
                effects.append("迫使落地：" + "、".join(grounded))
            return ActionResolution(
                action,
                "机会【法术附加效果】："
                + ("；".join(effects) if effects else "没有目标受到新增效果")
                + "。",
                payload,
            )

        if normalized == "reveal":
            return self._resolve_reveal(action, actor=actor, effect=effect, payload=payload)
        if normalized == "progress":
            clock_name = str(action.parameters.get("clock_name") or "").strip()
            if not clock_name:
                return self._parameter_required(
                    action,
                    actor=actor,
                    effect=normalized,
                    required_parameter="clock_name",
                    prompt="你想让【进展】影响哪一个现有命刻？",
                    payload=payload,
                )
            if not self.clocks.exists(clock_name):
                raise ValueError("机会【进展】只能影响一个已经存在的命刻。")
            direction = -1 if action.parameters.get("erase") or action.parameters.get("clock_direction") == -1 else 1
            amount = self._int_parameter(action.parameters.get("delta"), default=2, minimum=0)
            if amount > 2:
                raise ValueError("机会【进展】至多填充或擦除命刻2格。")
            delta = amount * direction
            before, after = self.clocks.advance(clock_name, delta)
            clock = self.clocks.get(clock_name)
            change = ClockChange(
                clock_name=clock.name,
                before=before,
                after=after,
                delta=delta,
                max_segments=clock.max_segments,
                reason="机会效果：进展。",
                clock_type=clock.clock_type,
                stakes=clock.stakes,
                completion_consequence=clock.completion_consequence,
            )
            payload["clock_change"] = change
            verb = "推进" if delta >= 0 else "擦除"
            return ActionResolution(action, f"机会【进展】：命刻 [{clock.name}] {verb} {abs(delta)} 格。", payload)
        if normalized == "bond":
            bond_owner = str(action.parameters.get("bond_owner") or actor).strip()
            target = str(action.parameters.get("target") or "").strip()
            if not self.characters.exists(bond_owner):
                return self._parameter_required(
                    action,
                    actor=actor,
                    effect=normalized,
                    required_parameter="bond_owner",
                    prompt="这段羁绊属于哪一个生物？",
                    payload=payload,
                )
            if not target:
                return self._parameter_required(
                    action,
                    actor=actor,
                    effect=normalized,
                    required_parameter="target",
                    prompt="你想与谁或什么建立、强化这段羁绊？",
                    payload=payload,
                )
            emotions = self._string_sequence(action.parameters.get("emotions"))
            if not emotions:
                emotion = str(action.parameters.get("emotion") or "").strip()
                if not emotion:
                    return self._parameter_required(
                        action,
                        actor=actor,
                        effect=normalized,
                        required_parameter="emotion",
                        prompt="这段羁绊新增哪一种情感？",
                        payload=payload,
                    )
                emotions = [emotion]
            bond = self.characters.manage_bond(
                bond_owner,
                target,
                emotions,
                mode="upsert",
            )
            payload.update({"bond": bond, "bond_owner": bond_owner})
            return ActionResolution(
                action,
                f"机会【纽带】：{bond_owner} 对【{bond.target}】的羁绊现在为强度 {bond.strength}。",
                payload,
            )
        if normalized == "information":
            information = str(
                action.parameters.get("information")
                or action.parameters.get("fact")
                or action.parameters.get("description")
                or ""
            ).strip()
            if not information:
                return self._parameter_required(
                    action,
                    actor=actor,
                    effect=normalized,
                    required_parameter="information",
                    prompt="这次机会让你发现了哪条有用的线索或情报？",
                    payload=payload,
                )
            self.world.add_memory(f"机会【情报】：{information}")
            subject = str(action.parameters.get("subject") or "").strip()
            if subject:
                self.world.remember_subject_fact(subject, information)
            payload.update({"information": information, "subject": subject})
            return ActionResolution(action, f"机会【情报】：{information}", payload)
        if normalized == "favor":
            target = str(action.parameters.get("target") or "").strip()
            if not target:
                return self._parameter_required(
                    action,
                    actor=actor,
                    effect=normalized,
                    required_parameter="target",
                    prompt="你的行动赢得了谁的支持或赞赏？",
                    payload=payload,
                )
            description = str(
                action.parameters.get("description")
                or action.parameters.get("support")
                or ""
            ).strip()
            if not description and actor == "__gm__":
                return self._parameter_required(
                    action,
                    actor=actor,
                    effect=normalized,
                    required_parameter="description",
                    prompt="这份支持或赞赏具体给了谁，又会怎样表现出来？",
                    payload=payload,
                )
            if not description:
                description = f"{target}愿意为{actor}提供力所能及的支持"
            self.world.remember_subject_fact(target, f"青睐：{description}")
            payload.update({"target": target, "description": description})
            return ActionResolution(action, f"机会【青睐】：{description}。", payload)
        if normalized == "scan":
            return self._resolve_scan(action, actor=actor, payload=payload)
        if normalized == "misstep":
            target = str(action.parameters.get("target") or "").strip()
            if not target:
                return self._parameter_required(
                    action,
                    actor=actor,
                    effect=normalized,
                    required_parameter="target",
                    prompt="你要让场景中的哪一个生物失态？",
                    payload=payload,
                )
            statement = str(
                action.parameters.get("statement")
                or action.parameters.get("compromising_statement")
                or ""
            ).strip()
            if not statement:
                return self._parameter_required(
                    action,
                    actor=actor,
                    effect=normalized,
                    required_parameter="statement",
                    prompt=f"【{target}】会说出怎样一句妥协性言论？这句话由其操控者决定。",
                    payload=payload,
                )
            self.world.remember_subject_fact(target, f"失态时说过：{statement}")
            payload.update({"target": target, "statement": statement})
            return ActionResolution(action, f"机会【失态】：【{target}】说：“{statement}”", payload)
        if normalized == "suffer":
            target_name = str(action.parameters.get("target") or "").strip()
            if not target_name:
                return self._parameter_required(
                    action,
                    actor=actor,
                    effect=normalized,
                    required_parameter="target",
                    prompt="你想让哪一个生物承受异常状态？",
                    payload=payload,
                )
            raw_status = action.parameters.get("status_effect")
            if not str(raw_status or "").strip():
                return self._parameter_required(
                    action,
                    actor=actor,
                    effect=normalized,
                    required_parameter="status_effect",
                    prompt="你想施加眩晕、动摇、迟缓还是虚弱？",
                    payload=payload,
                )
            if not self.characters.exists(target_name):
                raise ValueError(f"没有找到可承受机会【受苦】的生物【{target_name}】。")
            status = self.status_effect(raw_status)
            if status not in self._SUFFER_STATUSES:
                raise ValueError("机会【受苦】只能施加眩晕、动摇、迟缓或虚弱。")
            applied = self.conflict.apply_status(target_name, status)
            payload.update({"status_applied": applied, "status": status})
            return ActionResolution(
                action,
                f"机会【受苦】：{target_name} 被施加 {self.status_name(status)}。",
                payload,
            )
        if normalized == "advantage":
            target_name = str(action.parameters.get("target") or action.parameters.get("advantage_target") or actor)
            if not self.characters.exists(target_name):
                raise ValueError(f"没有找到可获得机会【优势】的生物【{target_name}】。")
            self.post_check_state.grant_advantage(target_name, 4)
            payload.update({"target": target_name, "advantage_bonus": 4})
            return ActionResolution(
                action,
                f"机会【优势】：{target_name} 的下一次检定获得 +4 修正。",
                payload,
            )
        if normalized == "lost_item":
            return self._resolve_lost_item(action, actor=actor, payload=payload)
        if normalized == "twist":
            subject = str(
                action.parameters.get("subject")
                or action.parameters.get("target")
                or ""
            ).strip()
            if not subject:
                return self._parameter_required(
                    action,
                    actor=actor,
                    effect=normalized,
                    required_parameter="subject",
                    prompt="你选择的谁或什么会突然出现在场景中？",
                    payload=payload,
                )
            description = str(
                action.parameters.get("description")
                or f"{subject}突然出现在场景中"
            ).strip()
            self.world.add_memory(f"机会【转折】：{description}")
            payload.update({"subject": subject, "text": description})
            return ActionResolution(action, f"机会【转折】：{self._sentence(description)}", payload)
        if normalized == "custom":
            description = str(action.parameters.get("description") or "").strip()
            if not description:
                return self._parameter_required(
                    action,
                    actor=actor,
                    effect=normalized,
                    required_parameter="description",
                    prompt="请提出一个符合当前场景、且不会推翻已公开事实的机会转折。",
                    payload=payload,
                )
            self.world.add_memory(f"自定义机会：{description}")
            payload["text"] = description
            return ActionResolution(action, f"机会：{description}", payload)

        raise ValueError(f"机会效果【{effect or normalized}】尚未实现。")

    def reveal_motivation(self, action: Action, target: str) -> tuple[str, bool]:
        supplied = str(
            action.parameters.get("revealed_motivation")
            or action.parameters.get("motivation")
            or action.parameters.get("goal")
            or ""
        ).strip()
        if supplied:
            return supplied, False

        normalized_target = target.strip(" 【】[]")
        if callable(self.reveal_motivation_provider):
            provided = str(self.reveal_motivation_provider(normalized_target) or "").strip()
            if provided:
                return provided, False

        role_fallback = ""
        for name, persona in self.world.npc_personas.items():
            labels = {name, str(persona.public_identity or "").strip()}
            if normalized_target not in labels and not any(
                label and (normalized_target in label or label in normalized_target)
                for label in labels
            ):
                continue
            active_goal = str(persona.active_goal or "").strip()
            if active_goal:
                return active_goal, False
            meaningful_goals = [goal for goal in persona.goals if str(goal).strip()]
            if meaningful_goals:
                return str(meaningful_goals[0]).strip(), False
            drive = str(persona.core_drive or "").strip()
            if drive and drive not in {"根据当前故事目标行动", "未定义", "尚未明确记录"}:
                return drive, False
            role = str(persona.role_in_story or persona.public_identity or name).strip()
            if role and role not in {"当前场景中的非玩家角色", "当前局面的对立或把关者"}:
                role_fallback = f"先守住{role}所承担的人与事，再决定是否接受眼前的要求"

        if self.characters.exists(normalized_target):
            character = self.characters.get(normalized_target)
            if character.theme:
                return f"行动受主题“{character.theme}”驱动", False
            if character.identity:
                return f"维护自己作为“{character.identity}”的信念与责任", False

        for fact in reversed(self.world.subject_facts.get(normalized_target, [])):
            if any(marker in fact for marker in ("目标", "动机", "想要", "希望", "试图", "保护", "阻止")):
                return str(fact).strip(), False

        if role_fallback:
            return "", True
        return "", True

    @classmethod
    def normalize_effect(cls, effect: str) -> str:
        return cls._ALIASES.get(effect, effect.lower() or "information")

    @classmethod
    def tool_parameter_guide(cls) -> str:
        """Return the single model-facing contract for all core opportunities."""

        return cls._TOOL_PARAMETER_GUIDE

    def _resolve_reveal(
        self,
        action: Action,
        *,
        actor: str,
        effect: str,
        payload: dict[str, object],
    ) -> ActionResolution:
        target = str(action.parameters.get("target") or "").strip()
        target_explicit = bool(
            action.parameters.get("target_explicit")
            or (target and target != actor and target not in {"当前目标", "当前对象", "当前生物"})
        )
        if not target_explicit:
            payload.update(
                {
                    "pending_opportunity": {
                        "actor": actor,
                        "effect": "reveal",
                        "label": effect or "揭示",
                    },
                    "opportunity_parameter_required": True,
                    "required_parameter": "target",
                }
            )
            return ActionResolution(action, "你想对哪一个生物使用【揭示】？", payload)

        canonical_target = self.world.resolve_npc_name(target)
        if canonical_target:
            target = canonical_target

        motivation, inferred = self.reveal_motivation(action, target)
        if not motivation:
            return self._parameter_required(
                action,
                actor=actor,
                effect="reveal",
                required_parameter="revealed_motivation",
                prompt=f"【{target}】当前真实的目标或动机还未记录，请由其操控者明确。",
                payload=payload,
            )
        payload.update(
            {
                "target": target,
                "revealed_motivation": motivation,
                "revealed_motivation_inferred": inferred,
            }
        )
        self.world.add_memory(f"通过机会【揭示】得知：{target}的目标或动机是“{motivation}”")
        self.world.remember_subject_fact(target, f"目标或动机：{motivation}")
        return ActionResolution(
            action,
            f"机会【揭示】：你得知【{target}】的目标或动机是：{motivation}。",
            payload,
        )

    def _resolve_scan(
        self,
        action: Action,
        *,
        actor: str,
        payload: dict[str, object],
    ) -> ActionResolution:
        target = str(action.parameters.get("target") or "").strip()
        if not target:
            return self._parameter_required(
                action,
                actor=actor,
                effect="scan",
                required_parameter="target",
                prompt="你想审视哪一个能看见的生物？",
                payload=payload,
            )
        detail = str(action.parameters.get("revealed_detail") or "").strip()
        detail_type = str(action.parameters.get("scan_type") or "").strip()
        canonical_target = self.world.resolve_npc_name(target)
        if canonical_target:
            target = canonical_target
        if self.characters.exists(target):
            character = self.characters.get(target)
            weaknesses = [
                self._DAMAGE_TYPE_NAMES.get(damage_type, damage_type)
                for damage_type, affinity in character.affinities.items()
                if affinity == Affinity.WEAK
            ]
            traits = [
                str(item).strip()
                for item in (
                    character.identity,
                    character.theme,
                    character.origin,
                    *character.traits,
                )
                if str(item).strip() and str(item).strip() not in {"pc", "npc", "enemy"}
            ]
            if detail:
                legal = set(weaknesses + traits)
                if detail not in legal:
                    raise ValueError("机会【审视】只能揭示目标实际拥有的一项弱点或特质。")
            elif detail_type in {"weakness", "弱点"} and weaknesses:
                detail = weaknesses[0]
                detail_type = "弱点"
            elif detail_type in {"trait", "特质"} and traits:
                detail = traits[0]
                detail_type = "特质"
            elif weaknesses:
                detail = weaknesses[0]
                detail_type = "弱点"
            elif traits:
                detail = traits[0]
                detail_type = "特质"
        elif target in self.world.npc_personas:
            persona = self.world.npc_personas[target]
            traits = [str(item).strip() for item in persona.traits if str(item).strip()]
            if detail:
                if detail not in traits:
                    raise ValueError("机会【审视】只能揭示目标实际拥有的一项弱点或特质。")
                detail_type = "特质"
            elif traits:
                detail = traits[0]
                detail_type = "特质"
        if not detail:
            return self._parameter_required(
                action,
                actor=actor,
                effect="scan",
                required_parameter="revealed_detail",
                prompt=f"【{target}】尚未记录可揭示的弱点或特质，请由GM补充其真实数据。",
                payload=payload,
            )
        label = "弱点" if detail_type in {"weakness", "弱点"} else "特质"
        self.world.remember_subject_fact(target, f"{label}：{detail}")
        payload.update({"target": target, "scan_type": label, "revealed_detail": detail})
        return ActionResolution(action, f"机会【审视】：你发现【{target}】的一项{label}是【{detail}】。", payload)

    def _resolve_lost_item(
        self,
        action: Action,
        *,
        actor: str,
        payload: dict[str, object],
    ) -> ActionResolution:
        target_name = str(action.parameters.get("target") or "").strip()
        item_name = str(action.parameters.get("item_name") or "").strip()
        scene_object = str(
            action.parameters.get("scene_object")
            or action.parameters.get("object_name")
            or action.parameters.get("item")
            or ""
        ).strip()
        description = str(
            action.parameters.get("description")
            or action.parameters.get("outcome")
            or ""
        ).strip()

        # “失物”并不只处理角色背包：规则允许一件物品损坏、遗失、
        # 失窃或被丢弃，现场已经存在的门、钥匙或装置同样可能成为对象。
        # 角色物品仍使用 item_name，以便硬规则安全地同步装备清单；
        # 场景物件则记录为已公开事实，不伪造角色的装备变动。
        if scene_object or (description and not item_name):
            if not description:
                description = f"【{scene_object}】损坏、遗失、失窃或被丢弃"
            fact = description.rstrip("。！？!?；;") + "。"
            self.world.add_memory(f"机会【失物】：{fact}")
            if scene_object:
                self.world.remember_subject_fact(scene_object, fact)
            committed_facts = [
                str(item).strip()
                for item in list(
                    action.parameters.get("committed_public_facts") or []
                )
                if str(item).strip()
            ]
            if fact not in committed_facts:
                committed_facts.append(fact)
            action.parameters["committed_public_facts"] = committed_facts
            payload.update(
                {
                    "scene_object": scene_object,
                    "scene_fact": fact,
                    "lost_item_scope": "scene",
                }
            )
            return ActionResolution(action, f"机会【失物】：{fact}", payload)

        if not target_name and not item_name:
            return self._parameter_required(
                action,
                actor=actor,
                effect="lost_item",
                required_parameter="item_or_scene_object",
                prompt="你想让哪件角色物品或现场物件损坏、遗失、失窃或被丢弃？",
                payload=payload,
            )
        if not target_name or not self.characters.exists(target_name):
            raise ValueError("机会【失物】需要选择一个存在的生物，或说明受影响的现场物件。")
        try:
            resolved_item = self.economy.resolve_owned_equipment_name(
                target_name,
                item_name,
            )
        except ValueError as exc:
            raise ValueError(
                f"【{target_name}】没有可失去的物品【{item_name or '未指定'}】。"
            ) from exc
        access = self.economy.set_equipment_access(
            target_name,
            [resolved_item],
            available=False,
            reason=description or "机会【失物】",
            location="当前场景",
        )
        fact = self._sentence(
            description or f"{target_name}失去了【{resolved_item}】"
        )
        self.world.add_memory(f"机会【失物】：{fact}")
        self.world.remember_subject_fact(target_name, fact)
        payload.update(
            {
                "target": target_name,
                "lost_item": resolved_item,
                "lost_item_scope": "character",
                "equipment_access": access,
            }
        )
        return ActionResolution(action, f"机会【失物】：{fact}", payload)

    @staticmethod
    def _parameter_required(
        action: Action,
        *,
        actor: str,
        effect: str,
        required_parameter: str,
        prompt: str,
        payload: dict[str, object],
    ) -> ActionResolution:
        provided_parameters = {
            key: value
            for key, value in action.parameters.items()
            if key
            in {
                "bond_owner",
                "target",
                "emotion",
                "emotions",
                "clock_name",
                "delta",
                "erase",
                "clock_direction",
                "information",
                "fact",
                "description",
                "subject",
                "support",
                "scan_type",
                "revealed_detail",
                "statement",
                "compromising_statement",
                "item_name",
                "scene_object",
                "object_name",
                "item",
                "outcome",
                "status_effect",
                "advantage_target",
            }
            and value not in (None, "", [], {})
        }
        payload.update(
            {
                "pending_opportunity": {
                    "actor": actor,
                    "effect": effect,
                },
                "opportunity_parameter_required": True,
                "required_parameter": required_parameter,
                "provided_parameters": provided_parameters,
            }
        )
        return ActionResolution(action, prompt, payload)

    @staticmethod
    def _string_sequence(value: object) -> list[str]:
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        if value is None:
            return []
        text = str(value).strip()
        return [text] if text else []

    @staticmethod
    def _sentence(text: str) -> str:
        clean = str(text or "").strip()
        if not clean:
            return ""
        return clean if clean[-1] in "。！？!?" else clean + "。"

    @staticmethod
    def _int_parameter(value: object, *, default: int, minimum: int) -> int:
        try:
            parsed = int(value if value is not None else default)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, parsed)
