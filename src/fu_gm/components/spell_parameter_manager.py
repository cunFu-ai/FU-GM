from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.decision_window_manager import DecisionWindowManager
from fu_gm.components.scene_manager import SceneManager
from fu_gm.models import Action, ActionType, DecisionWindow, SpellDefinition, SpellTarget, StatusEffect


DAMAGE_TYPE_LABELS = {
    "wind": "风",
    "lightning": "雷",
    "ice": "冰",
    "fire": "火",
    "earth": "土",
    "dark": "暗",
    "light": "光",
    "poison": "毒",
    "physical": "物理",
}

STATUS_LABELS = {
    StatusEffect.SLOW.value: "迟缓",
    StatusEffect.SHAKEN.value: "动摇",
    StatusEffect.WEAKENED.value: "虚弱",
    StatusEffect.DAZED.value: "眩晕",
    StatusEffect.ENRAGED.value: "激怒",
    StatusEffect.POISONED.value: "中毒",
}

ATTRIBUTE_LABELS = {
    "DEX": "敏捷",
    "INS": "洞察",
    "MIG": "力量",
    "WLP": "意志",
}

_DAMAGE_ALIASES = {
    **{key: key for key in DAMAGE_TYPE_LABELS},
    **{label: key for key, label in DAMAGE_TYPE_LABELS.items()},
    **{f"{label}系": key for key, label in DAMAGE_TYPE_LABELS.items()},
    **{f"{label}元素": key for key, label in DAMAGE_TYPE_LABELS.items()},
    **{f"{label}系元素": key for key, label in DAMAGE_TYPE_LABELS.items()},
    **{f"{label}属性": key for key, label in DAMAGE_TYPE_LABELS.items()},
    **{f"{label}伤害": key for key, label in DAMAGE_TYPE_LABELS.items()},
    **{f"{label}系伤害": key for key, label in DAMAGE_TYPE_LABELS.items()},
    **{f"{label}元素伤害": key for key, label in DAMAGE_TYPE_LABELS.items()},
    **{f"{key} element": key for key in DAMAGE_TYPE_LABELS},
    **{f"{key}_element": key for key in DAMAGE_TYPE_LABELS},
    **{f"{key} damage": key for key in DAMAGE_TYPE_LABELS},
    **{f"{key}_damage": key for key in DAMAGE_TYPE_LABELS},
    "电": "lightning",
    "雷电": "lightning",
}

_STATUS_ALIASES = {
    **{key: key for key in STATUS_LABELS},
    **{label: key for key, label in STATUS_LABELS.items()},
    "缓慢": StatusEffect.SLOW.value,
    "晕眩": StatusEffect.DAZED.value,
    "颤抖": StatusEffect.SHAKEN.value,
}

_ATTRIBUTE_ALIASES = {
    **{key: key for key in ATTRIBUTE_LABELS},
    **{label: key for key, label in ATTRIBUTE_LABELS.items()},
}


def normalize_spell_damage_type(value: object) -> str:
    return _DAMAGE_ALIASES.get(str(value or "").strip().lower(), "")


def normalize_spell_status(value: object) -> str:
    return _STATUS_ALIASES.get(str(value or "").strip().lower(), "")


def normalize_spell_statuses(value: object) -> list[str]:
    if isinstance(value, str):
        raw_items = [item for item in re.split(r"[、,，/；;\s]+", value) if item]
    elif isinstance(value, (list, tuple, set)):
        raw_items = [item for item in value if item not in (None, "")]
    elif value in (None, ""):
        raw_items = []
    else:
        raw_items = [value]
    normalized = [normalize_spell_status(item) for item in raw_items]
    return list(dict.fromkeys(item for item in normalized if item))


def normalize_spell_attribute(value: object) -> str:
    raw = str(value or "").strip()
    return _ATTRIBUTE_ALIASES.get(raw, _ATTRIBUTE_ALIASES.get(raw.upper(), ""))


@dataclass(frozen=True)
class SpellParameterRequirement:
    missing_fields: tuple[str, ...]
    target_names: tuple[str, ...]
    target_candidates: tuple[str, ...]
    invalid_targets: tuple[str, ...] = ()


class SpellParameterManager:
    """Validate canonical spell choices before any rule transaction commits.

    A canonical spell never guesses a required target, element, status, or
    attribute.  Missing choices become one persisted ``DecisionWindow`` so the
    same declaration can resume without spending MP or advancing fictional
    time twice.
    """

    def __init__(
        self,
        characters: CharacterManager,
        decisions: DecisionWindowManager,
        scene_manager: SceneManager | None = None,
    ) -> None:
        self.characters = characters
        self.decisions = decisions
        self.scene_manager = scene_manager

    def inspect(
        self,
        action: Action,
        definition: SpellDefinition,
        actor_name: str,
    ) -> SpellParameterRequirement | None:
        target_names, invalid_targets = self._validated_targets(action, definition, actor_name)
        missing: list[str] = []
        if definition.target not in {SpellTarget.SELF, SpellTarget.ALL_ENEMIES} and (
            not target_names or invalid_targets or not self._valid_target_count(definition, target_names)
        ):
            missing.append("targets")

        if definition.selectable_damage_types:
            selected = normalize_spell_damage_type(action.parameters.get("chosen_damage_type"))
            if selected not in definition.selectable_damage_types:
                missing.append("chosen_damage_type")
        if definition.selectable_statuses:
            selected = normalize_spell_statuses(
                action.parameters.get("chosen_statuses")
                or action.parameters.get("chosen_status")
                or action.parameters.get("status_effect")
            )
            allowed = {status.value for status in definition.selectable_statuses}
            required_count = max(1, int(definition.selectable_status_count or 1))
            if len(selected) != required_count or any(item not in allowed for item in selected):
                missing.append("chosen_status")
        if definition.selectable_attributes:
            selected = normalize_spell_attribute(action.parameters.get("chosen_attribute"))
            if selected not in definition.selectable_attributes:
                missing.append("chosen_attribute")

        if not missing:
            return None
        return SpellParameterRequirement(
            missing_fields=tuple(dict.fromkeys(missing)),
            target_names=tuple(target_names),
            target_candidates=tuple(self._target_candidates(definition, actor_name)),
            invalid_targets=tuple(invalid_targets),
        )

    def target_candidates(
        self,
        definition: SpellDefinition,
        actor_name: str,
    ) -> list[str]:
        """Return scene-scoped legal candidates for one canonical spell."""

        return self._target_candidates(definition, actor_name)

    def bind_explicit_choices(
        self,
        action: Action,
        definition: SpellDefinition,
        text: str,
    ) -> Action:
        """Bind choices stated in the declaration before opening a window.

        Parsing and validation intentionally share this component so a scene
        NPC recognized here cannot disappear when ``inspect`` runs next.
        """

        if action.action_type != ActionType.SPELL:
            return action
        parameters = dict(action.parameters)
        actor = str(parameters.get("actor") or "").strip()
        clean_text = str(text or "")
        candidate_targets = [
            name
            for name in self._target_candidates(definition, actor)
            if name and name != actor
        ]
        target_text = self._declared_target_text(clean_text, definition.name)
        named_targets = self.named_targets_from_text(candidate_targets, target_text)
        self_target_explicit = bool(
            actor
            and re.search(
                r"(?:对|给)自己|目标(?:是|为)?(?:我|自己|自身)|"
                r"(?:^|[、和与及])(?:我|自己|自身)(?:$|[、和与及])",
                target_text,
            )
        )
        if definition.target == SpellTarget.SELF:
            named_targets = [actor] if actor else []
        elif definition.target == SpellTarget.ALL_ENEMIES:
            named_targets = self._target_candidates(definition, actor)
        elif self_target_explicit:
            named_targets.append(actor)
        elif (
            actor
            and str(parameters.get("target") or "").strip() == actor
            and definition.target != SpellTarget.SELF
            and not parameters.get("target_explicit")
        ):
            # A model may repeat the grammatical subject as ``target``.  That
            # is not a declared self-target for a non-self spell.
            parameters.pop("target", None)
            parameters.pop("targets", None)

        named_targets = list(dict.fromkeys(named_targets))
        if named_targets:
            max_targets = self._max_targets(definition, len(candidate_targets))
            selected_targets = named_targets if max_targets <= 0 else named_targets[:max_targets]
            parameters["target"] = selected_targets[0]
            if len(selected_targets) > 1 or definition.target in {
                SpellTarget.UP_TO_THREE_CREATURES,
                SpellTarget.ANY_VISIBLE_CREATURES,
                SpellTarget.ALL_ENEMIES,
            }:
                parameters["targets"] = selected_targets
            parameters["target_explicit"] = True

        if definition.selectable_damage_types:
            selected = self._parameter_from_text(
                clean_text,
                definition.selectable_damage_types,
                DAMAGE_TYPE_LABELS,
            )
            if selected:
                parameters["chosen_damage_type"] = selected
        if definition.selectable_statuses:
            allowed = tuple(status.value for status in definition.selectable_statuses)
            selected = self._parameters_from_text(clean_text, allowed, STATUS_LABELS)
            required_count = max(1, int(definition.selectable_status_count or 1))
            if len(selected) >= required_count:
                chosen = selected[:required_count]
                parameters["chosen_status"] = chosen[0]
                if required_count > 1:
                    parameters["chosen_statuses"] = chosen
        if definition.selectable_attributes:
            selected = self._parameter_from_text(
                clean_text,
                definition.selectable_attributes,
                ATTRIBUTE_LABELS,
            )
            if selected:
                parameters["chosen_attribute"] = selected
        return Action(ActionType.SPELL, parameters)

    @staticmethod
    def _declared_target_text(text: str, spell_name: str) -> str:
        """Limit target binding to the spell's grammatical target clause.

        A declaration often mentions bystanders before the actual cast, for
        example ``在守门人视线内施放屏障护住旅人与自己``.  Scanning the
        entire sentence made every named bystander a spell target.  Prefer the
        explicit ``对X施放`` clause and the target phrase following an effect
        verb; fall back to the whole declaration only when neither exists.
        """

        source = str(text or "").strip()
        canonical = str(spell_name or "").strip()
        if not source or not canonical:
            return source
        spell_pattern = rf"[【\[「『“\"]?\s*{re.escape(canonical)}\s*[】\]」』”\"]?"
        clauses: list[str] = []
        before = re.search(
            rf"(?:对(?:着)?|给)\s*(?P<targets>[^，。；！？!?]{{1,80}}?)\s*"
            rf"(?:施放|施展|释放|发动|吟唱|使用|施术|念出)\s*(?:法术)?\s*{spell_pattern}",
            source,
        )
        if before:
            clauses.append(before.group("targets").strip())
        after = re.search(
            rf"{spell_pattern}[^，。；！？!?]{{0,48}}?"
            rf"(?:护住|保护|覆盖|治疗|治愈|强化|影响|作用于|施加给|施予)\s*"
            rf"(?P<targets>[^，。；！？!?]{{1,80}})",
            source,
        )
        if after:
            clauses.append(after.group("targets").strip())
        enclosed = re.search(
            rf"{spell_pattern}\s*[，,：:]?\s*[^，。；！？!?]{{0,24}}?(?:将|把)\s*"
            rf"(?P<targets>[^，。；！？!?]{{1,80}}?)\s*"
            rf"(?:纳入|置于|包裹在)(?:这道|该|其)?(?:护持|保护|屏障|结界|效果)"
            rf"(?:之中|中|内|范围内|范围)?",
            source,
        )
        if enclosed:
            clauses.append(enclosed.group("targets").strip())
        return "、".join(item for item in clauses if item) or source

    @classmethod
    def named_targets_from_text(cls, candidates: list[str], text: str) -> list[str]:
        """Resolve full names and unambiguous table-facing short names.

        Scene NPCs often have a stable archival name such as
        ``白花守望会的守碑人`` while players naturally say ``守碑人``.  A short
        suffix is accepted only when it identifies exactly one current target;
        ambiguous labels such as ``旅人`` are left for a decision window.
        """

        ordered = sorted(dict.fromkeys(candidates), key=len, reverse=True)
        matched = [name for name in ordered if name in text]
        matched_set = set(matched)

        alias_owners: dict[str, set[str]] = {}
        aliases_by_name: dict[str, list[str]] = {}
        for name in ordered:
            aliases = cls._target_suffix_aliases(name)
            aliases_by_name[name] = aliases
            for alias in aliases:
                alias_owners.setdefault(alias, set()).add(name)

        for name in ordered:
            if name in matched_set:
                continue
            aliases = sorted(aliases_by_name[name], key=len, reverse=True)
            if any(
                alias in text and alias_owners.get(alias) == {name}
                for alias in aliases
            ):
                matched.append(name)
                matched_set.add(name)
        return matched

    @staticmethod
    def _target_suffix_aliases(name: str) -> list[str]:
        clean = str(name or "").strip()
        if not clean:
            return []
        aliases: set[str] = set()
        tail_parts = [
            part.strip()
            for part in re.split(r"[的·・—\-（）()：:]+", clean)
            if part.strip()
        ]
        if len(tail_parts) > 1 and len(tail_parts[-1]) >= 2:
            aliases.add(tail_parts[-1])
        compact = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", clean)
        for length in range(2, min(6, len(compact)) + 1):
            aliases.add(compact[-length:])
        aliases.discard(clean)
        return list(aliases)

    def open_window(
        self,
        action: Action,
        definition: SpellDefinition,
        actor_name: str,
        requirement: SpellParameterRequirement,
        *,
        scope_kind: str,
        scope_id: str,
    ) -> DecisionWindow:
        options: list[dict[str, object]] = []
        if "targets" in requirement.missing_fields:
            options.extend(
                {"parameter": "target", "value": name, "label": name}
                for name in requirement.target_candidates
            )
        if "chosen_damage_type" in requirement.missing_fields:
            options.extend(
                {
                    "parameter": "chosen_damage_type",
                    "value": damage_type,
                    "label": DAMAGE_TYPE_LABELS.get(damage_type, damage_type),
                }
                for damage_type in definition.selectable_damage_types
            )
        if "chosen_status" in requirement.missing_fields:
            options.extend(
                {
                    "parameter": "chosen_status",
                    "value": status.value,
                    "label": STATUS_LABELS.get(status.value, status.value),
                }
                for status in definition.selectable_statuses
            )
        if "chosen_attribute" in requirement.missing_fields:
            options.extend(
                {
                    "parameter": "chosen_attribute",
                    "value": attribute,
                    "label": ATTRIBUTE_LABELS.get(attribute, attribute),
                }
                for attribute in definition.selectable_attributes
            )

        prompt = self._prompt(definition, requirement)
        return self.decisions.create(
            kind="spell_parameter",
            owner=actor_name,
            prompt=prompt,
            options=options,
            scope_kind=scope_kind,
            scope_id=scope_id,
            blocking=True,
            action_type=ActionType.SPELL.value,
            payload={
                "spell_name": definition.name,
                "required_fields": list(requirement.missing_fields),
                "existing_targets": list(requirement.target_names),
                "target_candidates": list(requirement.target_candidates),
                "invalid_targets": list(requirement.invalid_targets),
                "max_targets": self._max_targets(
                    definition,
                    len(requirement.target_candidates),
                ),
                "selectable_damage_types": list(definition.selectable_damage_types),
                "selectable_statuses": [status.value for status in definition.selectable_statuses],
                "selectable_status_count": max(
                    1,
                    int(definition.selectable_status_count or 1),
                ),
                "selectable_attributes": list(definition.selectable_attributes),
                "original_action": {
                    "action_type": action.action_type.value,
                    "parameters": deepcopy(action.parameters),
                },
            },
            dedupe_key=f"spell_parameter:{scope_kind}:{scope_id}:{actor_name}:{definition.name}",
        )

    def resume_action(
        self,
        window: DecisionWindow,
        selection: dict[str, object],
        definition: SpellDefinition,
    ) -> Action:
        original = window.payload.get("original_action")
        if not isinstance(original, dict) or not isinstance(original.get("parameters"), dict):
            raise ValueError("原施法动作已经丢失，无法继续结算。")
        parameters = deepcopy(original["parameters"])
        actor_name = str(parameters.get("actor") or window.owner or "").strip()
        required = {str(item) for item in window.payload.get("required_fields", [])}

        raw_targets = selection.get("targets")
        if "targets" in required or raw_targets not in (None, "", []):
            targets = self._split_targets(raw_targets)
            candidates = {str(item) for item in window.payload.get("target_candidates", [])}
            max_targets = int(window.payload.get("max_targets", 1) or 1)
            if (
                not targets
                or (max_targets > 0 and len(targets) > max_targets)
                or any(target not in candidates for target in targets)
            ):
                raise ValueError("请选择待决窗口列出的合法法术目标。")
            for key in ("target", "targets", "target_names", "subject", "scene_object"):
                parameters.pop(key, None)
            parameters["target"] = targets[0]
            if len(targets) > 1 or max_targets != 1:
                parameters["targets"] = targets

        if "chosen_damage_type" in required:
            value = normalize_spell_damage_type(selection.get("chosen_damage_type"))
            if value not in definition.selectable_damage_types:
                raise ValueError("请选择这个法术允许的伤害类型。")
            parameters["chosen_damage_type"] = value
        if "chosen_status" in required:
            values = normalize_spell_statuses(
                selection.get("chosen_statuses") or selection.get("chosen_status")
            )
            allowed = {status.value for status in definition.selectable_statuses}
            required_count = max(1, int(definition.selectable_status_count or 1))
            if len(values) != required_count or any(value not in allowed for value in values):
                raise ValueError(f"请选择这个法术允许的 {required_count} 种不同异常状态。")
            parameters["chosen_status"] = values[0]
            if required_count > 1:
                parameters["chosen_statuses"] = values
        if "chosen_attribute" in required:
            value = normalize_spell_attribute(selection.get("chosen_attribute"))
            if value not in definition.selectable_attributes:
                raise ValueError("请选择这个法术允许的属性。")
            parameters["chosen_attribute"] = value

        # Canonical combat spells never manipulate a clock merely because a
        # player described the hoped-for fictional outcome. Such an effect
        # needs its own Objective/Ritual adjudication.
        for key in (
            "clock_name",
            "clock_target",
            "clock_intent",
            "clock_direction",
            "clock_segments",
            "advance_clock",
            "erase_clock",
        ):
            parameters.pop(key, None)
        parameters["actor"] = actor_name
        parameters["spell_name"] = definition.name
        parameters["_spell_parameters_confirmed"] = True
        resumed = Action(ActionType.SPELL, parameters)
        remaining = self.inspect(resumed, definition, actor_name)
        if remaining is not None:
            raise ValueError("法术仍缺少必要的目标或效果选择。")
        return resumed

    def prepare_action(self, action: Action, definition: SpellDefinition) -> None:
        """Canonicalize validated choices and remove unsupported side effects."""

        if definition.selectable_damage_types:
            selected = normalize_spell_damage_type(action.parameters.get("chosen_damage_type"))
            if selected:
                action.parameters["chosen_damage_type"] = selected
        if definition.selectable_statuses:
            selected = normalize_spell_statuses(
                action.parameters.get("chosen_statuses")
                or action.parameters.get("chosen_status")
                or action.parameters.get("status_effect")
            )
            if selected:
                action.parameters["chosen_status"] = selected[0]
                if len(selected) > 1:
                    action.parameters["chosen_statuses"] = selected
        if definition.selectable_attributes:
            selected = normalize_spell_attribute(action.parameters.get("chosen_attribute"))
            if selected:
                action.parameters["chosen_attribute"] = selected
        for key in (
            "clock_name",
            "clock_target",
            "clock_intent",
            "clock_direction",
            "clock_segments",
            "advance_clock",
            "erase_clock",
        ):
            action.parameters.pop(key, None)

    def _validated_targets(
        self,
        action: Action,
        definition: SpellDefinition,
        actor_name: str,
    ) -> tuple[list[str], list[str]]:
        if definition.target == SpellTarget.SELF:
            return [actor_name], []
        if definition.target == SpellTarget.ALL_ENEMIES:
            return self._target_candidates(definition, actor_name), []
        raw = next(
            (
                action.parameters.get(key)
                for key in ("targets", "target_names", "target", "subject")
                if action.parameters.get(key) not in (None, "", [])
            ),
            None,
        )
        names = self._split_targets(raw)
        valid = [name for name in names if self._target_exists(name)]
        invalid = [name for name in names if not self._target_exists(name)]
        return list(dict.fromkeys(valid)), list(dict.fromkeys(invalid))

    def _target_exists(self, name: str) -> bool:
        if self.scene_manager is not None and self.scene_manager.current_scene is not None:
            return self.scene_manager.is_participant(name)
        return self.characters.exists(name)

    @staticmethod
    def _split_targets(value: object) -> list[str]:
        if isinstance(value, str):
            return [item.strip() for item in re.split(r"[、,，/]+", value) if item.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()] if value not in (None, "") and str(value).strip() else []

    @staticmethod
    def _parameter_from_text(
        text: str,
        allowed: tuple[str, ...],
        labels: dict[str, str],
    ) -> str:
        for value in allowed:
            label = labels.get(value, value)
            tokens = {value, label, f"{label}系"}
            if value == "lightning":
                tokens.update({"电", "雷电"})
            if any(token and token in text for token in sorted(tokens, key=len, reverse=True)):
                return value
        return ""

    @staticmethod
    def _parameters_from_text(
        text: str,
        allowed: tuple[str, ...],
        labels: dict[str, str],
    ) -> list[str]:
        selected: list[str] = []
        for value in allowed:
            label = labels.get(value, value)
            tokens = {value, label, f"{label}系"}
            if any(token and token in text for token in sorted(tokens, key=len, reverse=True)):
                selected.append(value)
        return selected

    @staticmethod
    def _valid_target_count(definition: SpellDefinition, targets: list[str]) -> bool:
        if definition.target == SpellTarget.UP_TO_THREE_CREATURES:
            return 1 <= len(targets) <= 3
        if definition.target in {
            SpellTarget.ANY_VISIBLE_CREATURES,
            SpellTarget.ALL_ENEMIES,
        }:
            return len(targets) >= 1
        return len(targets) == 1

    def _target_candidates(self, definition: SpellDefinition, actor_name: str) -> list[str]:
        actor = self.characters.get(actor_name)
        all_characters = list(self.characters.all())
        if self.scene_manager is not None and self.scene_manager.current_scene is not None:
            visible_names = set(self.scene_manager.current_scene.participants)
            all_characters = [
                item for item in all_characters if item.name in visible_names
            ]
        if definition.target in {SpellTarget.ONE_ENEMY, SpellTarget.ALL_ENEMIES}:
            actor_is_pc = "pc" in actor.traits
            candidates = [item.name for item in all_characters if ("pc" in item.traits) != actor_is_pc]
        elif definition.target == SpellTarget.ONE_ALLY:
            actor_is_pc = "pc" in actor.traits
            candidates = [item.name for item in all_characters if ("pc" in item.traits) == actor_is_pc]
        else:
            candidates = [item.name for item in all_characters]
        if self.scene_manager is not None and self.scene_manager.current_scene is not None:
            narrative = [
                name
                for name in self.scene_manager.current_scene.participants
                if name and not self.characters.exists(name)
            ]
            # Unknown-stat scene entities are valid for generic creature and
            # ally-facing choices, but never guessed as hostile targets.
            if definition.target not in {SpellTarget.ONE_ENEMY, SpellTarget.ALL_ENEMIES}:
                candidates.extend(narrative)
        return list(dict.fromkeys(name for name in candidates if name))

    def _prompt(
        self,
        definition: SpellDefinition,
        requirement: SpellParameterRequirement,
    ) -> str:
        parts = [f"【{definition.name}】还缺少结算所需的选择。"]
        if "targets" in requirement.missing_fields:
            if definition.target == SpellTarget.UP_TO_THREE_CREATURES:
                count = "一至三个生物"
            elif definition.target == SpellTarget.ANY_VISIBLE_CREATURES:
                count = "任意数量的可见生物"
            else:
                count = "一个生物"
            candidates = "、".join(requirement.target_candidates) or "当前在场的合法生物"
            parts.append(f"请选择{count}作为目标：{candidates}。")
        if "chosen_damage_type" in requirement.missing_fields:
            labels = "、".join(DAMAGE_TYPE_LABELS.get(item, item) for item in definition.selectable_damage_types)
            parts.append(f"请选择伤害类型：{labels}。")
        if "chosen_status" in requirement.missing_fields:
            labels = "、".join(STATUS_LABELS.get(item.value, item.value) for item in definition.selectable_statuses)
            count = max(1, int(definition.selectable_status_count or 1))
            if count > 1:
                parts.append(f"请选择 {count} 种不同异常状态：{labels}。")
            else:
                parts.append(f"请选择异常状态：{labels}。")
        if "chosen_attribute" in requirement.missing_fields:
            labels = "、".join(ATTRIBUTE_LABELS.get(item, item) for item in definition.selectable_attributes)
            parts.append(f"请选择属性：{labels}。")
        return "".join(parts)

    @staticmethod
    def _max_targets(definition: SpellDefinition, candidate_count: int) -> int:
        if definition.target == SpellTarget.UP_TO_THREE_CREATURES:
            return 3
        if definition.target in {
            SpellTarget.ANY_VISIBLE_CREATURES,
            SpellTarget.ALL_ENEMIES,
        }:
            return max(0, candidate_count)
        return 1
