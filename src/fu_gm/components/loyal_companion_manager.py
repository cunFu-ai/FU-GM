from __future__ import annotations

from collections import Counter
from copy import deepcopy
from typing import Any

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.encounter_manager import EncounterManager
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.world_state import WorldState
from fu_gm.models import Character, EnemyRank, SceneRecord, StatusEffect
from fu_gm.npc_design_library import normalize_damage_type, normalize_species, normalize_status
from fu_gm.skill_library import normalize_skill_reference_name, skill_rank


class LoyalCompanionManager:
    """Own the complete runtime lifecycle of the Wayfarer's loyal companion."""

    OWNER_STATE_KEY = "忠诚伙伴"
    COMPANION_STATE_KEY = "loyal_companion"
    ALLOWED_SPECIES = {"beast", "construct", "elemental", "plant"}
    ATTRIBUTE_ALIASES = {
        "敏捷": "DEX",
        "洞察": "INS",
        "力量": "MIG",
        "意志": "WLP",
        "DEX": "DEX",
        "INS": "INS",
        "MIG": "MIG",
        "WLP": "WLP",
    }

    def __init__(
        self,
        characters: CharacterManager,
        conflict: ConflictManager,
        scenes: SceneManager,
        world: WorldState,
    ) -> None:
        self.characters = characters
        self.conflict = conflict
        self.scenes = scenes
        self.world = world

    def create(
        self,
        owner_name: str,
        companion_name: str,
        *,
        species: str,
        traits: list[str],
        attribute_spread: str,
        attribute_order: list[str],
        selected_skills: list[str],
        skill_options: dict[str, object] | None,
        attacks: list[dict[str, object]],
        spell_attributes: dict[str, list[str]] | None = None,
        profile: dict[str, object] | None = None,
    ) -> Character:
        owner = self._owner(owner_name)
        rank = skill_rank(owner.skills, self.OWNER_STATE_KEY)
        if rank <= 0:
            raise ValueError(f"【{owner_name}】没有技能【忠诚伙伴】。")
        clean_name = str(companion_name or "").strip()
        if not clean_name:
            raise ValueError("忠诚伙伴需要一个稳定名称。")
        if self.characters.exists(clean_name) or self.world.resolve_npc_name(clean_name):
            raise ValueError(f"【{clean_name}】已经是现有角色或NPC，不能覆盖成忠诚伙伴。")
        existing = self.state_for_owner(owner_name)
        if existing and str(existing.get("name") or "").strip():
            raise ValueError(
                f"【{owner_name}】已经拥有忠诚伙伴【{existing['name']}】，不能再创建第二名。"
            )

        species_rule = normalize_species(str(species or ""))
        if species_rule.slug not in self.ALLOWED_SPECIES:
            raise ValueError("忠诚伙伴的物种只能是野兽、构装体、元素或植物。")
        clean_traits = list(
            dict.fromkeys(str(item or "").strip() for item in traits if str(item or "").strip())
        )
        if len(clean_traits) != 4:
            raise ValueError("忠诚伙伴作为5级NPC，需要恰好四个不同特质。")
        normalized_order = [
            self.ATTRIBUTE_ALIASES.get(str(item or "").strip(), "")
            for item in attribute_order
        ]
        if sorted(normalized_order) != ["DEX", "INS", "MIG", "WLP"]:
            raise ValueError("属性顺序必须让敏捷、洞察、力量、意志各出现一次。")
        clean_skills = [
            normalize_skill_reference_name(str(item or "").strip())
            for item in selected_skills
            if str(item or "").strip()
        ]
        if "强化先攻" in clean_skills:
            raise ValueError(
                "忠诚伙伴没有先攻值且不获得独立回合，不能选择【强化先攻】。"
            )
        options = deepcopy(skill_options or {})
        draft = EncounterManager(self.characters, self.conflict).design_npc(
            clean_name,
            level=5,
            species=species_rule.slug,
            traits=clean_traits,
            attribute_spread=str(attribute_spread or "versatile"),
            attribute_order=tuple(normalized_order),
            rank=EnemyRank.SOLDIER,
            selected_skill_names=clean_skills,
            skill_options=options,
        )
        if len(clean_skills) != draft.skill_budget:
            raise ValueError(
                f"【{clean_name}】作为{species_rule.name}应选择{draft.skill_budget}项NPC技能，"
                f"当前提交{len(clean_skills)}项。"
            )
        attack_profiles = self._validate_attacks(
            attacks,
            selected_skills=clean_skills,
            skill_options=options,
            base_damage_bonus=draft.extra_damage,
            check_bonus=draft.check_bonus + rank,
        )
        normalized_spell_attributes = self._validate_spell_attributes(
            spell_attributes or {},
            known_spells=draft.known_spells,
        )

        enhanced_hp = int(
            (
                draft.skill_effects.get("强化生命", {})
                if isinstance(draft.skill_effects.get("强化生命"), dict)
                else {}
            ).get("max_hp", 0)
            or 0
        )
        companion_max_hp = (
            rank * int(draft.attributes["MIG"])
            + owner.level // 2
            + enhanced_hp
        )
        primary_attack = attack_profiles[0]
        companion_state = {
            "owner": owner.name,
            "skill_rank": rank,
            "can_level": False,
            "awaiting_rejoin": False,
            "retreated_scene_id": "",
            "retreated_scene_name": "",
            "last_command_turn_serial": 0,
            "attacks": deepcopy(attack_profiles),
        }
        companion = Character(
            name=clean_name,
            attributes=dict(draft.attributes),
            max_hp=companion_max_hp,
            hp=companion_max_hp,
            max_mp=draft.max_mp,
            mp=draft.max_mp,
            level=5,
            crisis_threshold=companion_max_hp // 2,
            defenses=dict(draft.defenses),
            affinities=dict(draft.affinities),
            traits=list(
                dict.fromkeys(
                    [
                        self.COMPANION_STATE_KEY,
                        species_rule.slug,
                        *clean_traits,
                    ]
                )
            ),
            weapon_damage=int(primary_attack["weapon_damage"]),
            weapon_type=str(primary_attack["damage_type"]),
            weapon_accuracy_attributes=list(primary_attack["attributes"]),
            weapon_accuracy_modifier=int(primary_attack["accuracy_modifier"]),
            weapon_range=str(primary_attack["range"]),
            initiative=0,
            abilities=list(dict.fromkeys(clean_skills)),
            spells=list(draft.known_spells),
            skills=dict(Counter(clean_skills)),
            skill_options={
                str(key): (
                    [str(item) for item in value]
                    if isinstance(value, list)
                    else [str(value)]
                )
                for key, value in options.items()
            },
            npc_specialty_bonuses=dict(draft.specialty_bonuses),
            npc_skill_effects={
                **deepcopy(draft.skill_effects),
                self.COMPANION_STATE_KEY: companion_state,
            },
            npc_spell_check_bonus=(
                draft.check_bonus
                + rank
                + int(draft.specialty_bonuses.get("施法检定", 0))
            ),
            npc_spell_damage_bonus=draft.extra_damage,
            npc_spell_attributes=normalized_spell_attributes,
            permanent_status_immunities=set(draft.status_immunities),
            equipped_main_hand=str(primary_attack["name"]),
            equipment_attack_targets_magic_defense=bool(
                primary_attack["targets_magic_defense"]
            ),
            equipment_multi_attack=int(primary_attack["multi_attack"]),
            equipment_on_hit_status=primary_attack["status_effect_on_hit"],
            equipment_notes=list(primary_attack["notes"]),
        )
        self.characters.add(companion)
        owner.npc_skill_effects[self.OWNER_STATE_KEY] = {
            "name": clean_name,
            "skill_rank": rank,
        }

        public_profile = dict(profile or {})
        scene = self.scenes.current_scene
        persona = self.world.ensure_npc_persona(
            clean_name,
            public_identity=str(
                public_profile.get("public_identity")
                or f"{owner.name}的忠诚伙伴"
            ).strip(),
            role_in_story="玩家角色的忠诚伙伴",
            core_drive=str(
                public_profile.get("core_drive")
                or f"陪伴并协助{owner.name}"
            ).strip(),
            manner=str(public_profile.get("manner") or "").strip(),
            speech_style=str(public_profile.get("speech_style") or "").strip(),
            combat_style=str(public_profile.get("combat_style") or "").strip(),
            known_skills=list(dict.fromkeys(clean_skills)),
            combat_actions=[str(item["name"]) for item in attack_profiles],
            first_scene=str(getattr(scene, "name", "") or ""),
            current_location=str(
                getattr(scene, "participant_locations", {}).get(
                    owner.name,
                    getattr(scene, "location", ""),
                )
                if scene is not None
                else ""
            ),
            active_goal=str(
                public_profile.get("active_goal")
                or f"与{owner.name}一同行动"
            ).strip(),
            voice_examples=[
                str(item).strip()
                for item in public_profile.get("voice_examples", [])
                if str(item).strip()
            ]
            if isinstance(public_profile.get("voice_examples"), list)
            else [],
        )
        persona.npc_rank = "supporting"
        self.sync_scene(scene, scene_started=False)
        return self.characters.get(clean_name)

    def state_for_owner(self, owner_name: str) -> dict[str, object]:
        if not self.characters.exists(owner_name):
            return {}
        owner = self.characters.get(owner_name)
        state = owner.npc_skill_effects.get(self.OWNER_STATE_KEY)
        return state if isinstance(state, dict) else {}

    def companion_for(self, owner_name: str) -> Character | None:
        state = self.state_for_owner(owner_name)
        name = str(state.get("name") or "").strip()
        if not name or not self.characters.exists(name):
            return None
        companion = self.characters.get(name)
        return companion if self.is_companion(name) else None

    def owner_of(self, companion_name: str) -> str:
        if not self.is_companion(companion_name):
            return ""
        state = self._companion_state(self.characters.get(companion_name))
        return str(state.get("owner") or "").strip()

    def is_companion(self, name: str) -> bool:
        if not self.characters.exists(name):
            return False
        character = self.characters.get(name)
        return (
            self.COMPANION_STATE_KEY in character.traits
            and isinstance(
                character.npc_skill_effects.get(self.COMPANION_STATE_KEY),
                dict,
            )
        )

    def attack_profile(
        self,
        owner_name: str,
        attack_name: str = "",
    ) -> dict[str, object]:
        companion = self.require_available(owner_name)
        attacks = list(self._companion_state(companion).get("attacks") or [])
        requested = str(attack_name or "").strip()
        if requested:
            for attack in attacks:
                if str(attack.get("name") or "") == requested:
                    return deepcopy(attack)
            raise ValueError(
                f"【{companion.name}】没有基础攻击【{requested}】；可用："
                + "、".join(str(item.get("name") or "") for item in attacks)
            )
        if len(attacks) != 1:
            raise ValueError(
                f"【{companion.name}】有多种基础攻击，请明确选择："
                + "、".join(str(item.get("name") or "") for item in attacks)
            )
        return deepcopy(attacks[0])

    def require_available(self, owner_name: str) -> Character:
        companion = self.companion_for(owner_name)
        if companion is None:
            raise ValueError(f"【{owner_name}】尚未创建忠诚伙伴。")
        state = self._companion_state(companion)
        if bool(state.get("awaiting_rejoin")):
            raise ValueError(
                f"【{companion.name}】已经离开当前场景，要到下一个有【{owner_name}】在场的场景开始时才会归队。"
            )
        scene = self.scenes.current_scene
        if scene is not None and companion.name not in scene.participants:
            raise ValueError(f"【{companion.name}】当前不在这个场景。")
        return companion

    def assert_command_available(self, owner_name: str) -> None:
        companion = self.require_available(owner_name)
        state = self._companion_state(companion)
        if self.conflict.state.active:
            serial = int(self.conflict.state.turn_serial or 0)
            if int(state.get("last_command_turn_serial") or 0) == serial:
                raise ValueError("【忠诚伙伴】每个回合只能用自己的行动指挥一次。")

    def mark_command_used(self, owner_name: str) -> None:
        companion = self.require_available(owner_name)
        state = self._companion_state(companion)
        if self.conflict.state.active:
            serial = int(self.conflict.state.turn_serial or 0)
            state["last_command_turn_serial"] = serial

    def on_owner_turn_start(self, owner_name: str, turn_serial: int) -> None:
        companion = self.companion_for(owner_name)
        if companion is None:
            return
        self.conflict.clear_effects(companion.name, "guard")

    def mark_retreated(self, companion_name: str) -> None:
        if not self.is_companion(companion_name):
            return
        companion = self.characters.get(companion_name)
        state = self._companion_state(companion)
        scene = self.scenes.current_scene
        state["awaiting_rejoin"] = True
        state["retreated_scene_id"] = str(getattr(scene, "scene_id", "") or "")
        state["retreated_scene_name"] = str(
            getattr(scene, "name", "") or self.conflict.state.scene_name or ""
        )
        self.scenes.remove_participant(companion_name)

    def sync_scene(
        self,
        scene: SceneRecord | None,
        *,
        scene_started: bool,
    ) -> list[str]:
        if scene is None:
            return []
        rejoined: list[str] = []
        for owner in [
            character
            for character in self.characters.all()
            if "pc" in character.traits
        ]:
            companion = self.companion_for(owner.name)
            if companion is None:
                continue
            state = self._companion_state(companion)
            owner_present = owner.name in scene.participants
            if not owner_present:
                if companion.name in scene.participants:
                    self.scenes.remove_participant(companion.name)
                continue
            if bool(state.get("awaiting_rejoin")):
                retreated_scene_id = str(state.get("retreated_scene_id") or "")
                is_later_scene = bool(
                    scene_started
                    and str(scene.scene_id or "")
                    and str(scene.scene_id or "") != retreated_scene_id
                )
                if not is_later_scene:
                    continue
                companion.hp = companion.crisis_threshold
                state["awaiting_rejoin"] = False
                state["retreated_scene_id"] = ""
                state["retreated_scene_name"] = ""
                self.conflict.state.defeated_combatants.discard(
                    companion.name
                )
                rejoined.append(companion.name)
            location = str(
                scene.participant_locations.get(owner.name)
                or scene.location
                or self.scenes.location_of(owner.name)
            ).strip()
            self.scenes.add_participant(companion.name, location=location)
        return rejoined

    def apply_owner_rest(self, owner_name: str) -> dict[str, object] | None:
        companion = self.companion_for(owner_name)
        if companion is None:
            return None
        state = self._companion_state(companion)
        if bool(state.get("awaiting_rejoin")):
            return None
        hp_before, hp_after = self.characters.modify_resource(
            companion.name,
            "hp",
            companion.max_hp,
        )
        mp_before, mp_after = self.characters.modify_resource(
            companion.name,
            "mp",
            companion.max_mp,
        )
        self.characters.clear_statuses(companion.name)
        return {
            "name": companion.name,
            "hp_before": hp_before,
            "hp_after": hp_after,
            "mp_before": mp_before,
            "mp_after": mp_after,
        }

    def public_state(self, owner_name: str) -> dict[str, object]:
        companion = self.companion_for(owner_name)
        if companion is None:
            return {}
        state = self._companion_state(companion)
        return {
            "name": companion.name,
            "owner": owner_name,
            "species": self._species_name(companion),
            "level": companion.level,
            "hp": companion.hp,
            "max_hp": companion.max_hp,
            "mp": companion.mp,
            "max_mp": companion.max_mp,
            "crisis": companion.crisis_threshold,
            "present": bool(
                self.scenes.current_scene is None
                or companion.name in self.scenes.current_scene.participants
            ),
            "awaiting_rejoin": bool(state.get("awaiting_rejoin")),
            "attacks": [
                {
                    "name": str(item.get("name") or ""),
                    "attributes": list(item.get("attributes") or []),
                    "damage_type": str(item.get("damage_type") or ""),
                    "weapon_damage": int(item.get("weapon_damage") or 0),
                    "range": str(item.get("range") or ""),
                }
                for item in state.get("attacks", [])
                if isinstance(item, dict)
            ],
            "spells": list(companion.spells),
            "skills": dict(companion.skills),
        }

    @classmethod
    def apply_attack_profile(
        cls,
        companion: Character,
        profile: dict[str, object],
    ) -> dict[str, object]:
        previous = {
            "weapon_damage": companion.weapon_damage,
            "weapon_type": companion.weapon_type,
            "weapon_accuracy_attributes": list(companion.weapon_accuracy_attributes),
            "weapon_accuracy_modifier": companion.weapon_accuracy_modifier,
            "weapon_range": companion.weapon_range,
            "equipped_main_hand": companion.equipped_main_hand,
            "equipment_attack_targets_magic_defense": companion.equipment_attack_targets_magic_defense,
            "equipment_multi_attack": companion.equipment_multi_attack,
            "equipment_on_hit_status": companion.equipment_on_hit_status,
            "equipment_notes": list(companion.equipment_notes),
        }
        companion.weapon_damage = int(profile["weapon_damage"])
        companion.weapon_type = str(profile["damage_type"])
        companion.weapon_accuracy_attributes = list(profile["attributes"])
        companion.weapon_accuracy_modifier = int(profile["accuracy_modifier"])
        companion.weapon_range = str(profile["range"])
        companion.equipped_main_hand = str(profile["name"])
        companion.equipment_attack_targets_magic_defense = bool(
            profile["targets_magic_defense"]
        )
        companion.equipment_multi_attack = int(profile["multi_attack"])
        companion.equipment_on_hit_status = profile["status_effect_on_hit"]
        companion.equipment_notes = list(profile["notes"])
        return previous

    @staticmethod
    def restore_attack_profile(
        companion: Character,
        previous: dict[str, object],
    ) -> None:
        for key, value in previous.items():
            setattr(companion, key, value)

    @classmethod
    def _validate_attacks(
        cls,
        attacks: list[dict[str, object]],
        *,
        selected_skills: list[str],
        skill_options: dict[str, object],
        base_damage_bonus: int,
        check_bonus: int,
    ) -> list[dict[str, object]]:
        if not isinstance(attacks, list) or not 1 <= len(attacks) <= 2:
            raise ValueError("忠诚伙伴需要一到两种基础攻击。")
        special_attack_budget = sum(
            1 for skill in selected_skills if skill == "特殊攻击"
        )
        enhanced_targets = cls._option_list(skill_options, "强化伤害")
        profiles: list[dict[str, object]] = []
        used_names: set[str] = set()
        used_special_effects = 0
        for raw in attacks:
            if not isinstance(raw, dict):
                raise ValueError("每一种忠诚伙伴基础攻击都必须是对象。")
            name = str(raw.get("name") or "").strip()
            if not name or name in used_names:
                raise ValueError("忠诚伙伴的每种基础攻击都需要不同的名称。")
            used_names.add(name)
            raw_attributes = raw.get("attributes")
            if not isinstance(raw_attributes, list) or len(raw_attributes) != 2:
                raise ValueError(f"基础攻击【{name}】必须使用两项属性。")
            attributes = [
                cls.ATTRIBUTE_ALIASES.get(str(item or "").strip(), "")
                for item in raw_attributes
            ]
            if not all(attributes):
                raise ValueError(f"基础攻击【{name}】包含未知属性。")
            damage_type = normalize_damage_type(
                str(raw.get("damage_type") or "physical")
            )
            attack_range = str(raw.get("range") or "melee").strip().lower()
            if attack_range not in {"melee", "ranged"}:
                raise ValueError(f"基础攻击【{name}】的范围必须是melee或ranged。")
            raw_status = str(raw.get("status_effect_on_hit") or "").strip()
            status = normalize_status(raw_status) if raw_status else None
            multi_attack = int(raw.get("multi_attack") or 1)
            if not 1 <= multi_attack <= 3:
                raise ValueError(f"基础攻击【{name}】的多重攻击必须在1到3之间。")
            targets_magic = bool(raw.get("targets_magic_defense"))
            special_effect_count = int(status is not None) + int(
                multi_attack > 1
            ) + int(targets_magic)
            used_special_effects += special_effect_count
            notes = raw.get("notes") or []
            if not isinstance(notes, list) or any(
                not isinstance(item, str) for item in notes
            ):
                raise ValueError(f"基础攻击【{name}】的notes必须是字符串数组。")
            enhanced_count = sum(
                1
                for target in enhanced_targets
                if target in {name, "基础攻击", "攻击"}
            )
            profiles.append(
                {
                    "name": name,
                    "attributes": attributes,
                    "damage_type": damage_type,
                    "weapon_damage": 5
                    + int(base_damage_bonus)
                    + enhanced_count * 5,
                    "accuracy_modifier": int(check_bonus),
                    "range": attack_range,
                    "targets_magic_defense": targets_magic,
                    "multi_attack": multi_attack,
                    "status_effect_on_hit": status,
                    "notes": [
                        str(item).strip()
                        for item in notes
                        if str(item).strip()
                    ],
                }
            )
        if used_special_effects > special_attack_budget:
            raise ValueError(
                "基础攻击附加的多重攻击、针对魔防或异常状态效果数量，"
                "超过已选择的【特殊攻击】技能次数。"
            )
        return profiles

    @classmethod
    def _validate_spell_attributes(
        cls,
        spell_attributes: dict[str, list[str]],
        *,
        known_spells: list[str],
    ) -> dict[str, list[str]]:
        normalized: dict[str, list[str]] = {}
        known = set(known_spells)
        for spell_name, raw_attributes in spell_attributes.items():
            if spell_name not in known:
                raise ValueError(f"【{spell_name}】不是该忠诚伙伴已学会的法术。")
            if not isinstance(raw_attributes, list) or len(raw_attributes) != 2:
                raise ValueError(f"法术【{spell_name}】必须使用两项属性。")
            attributes = [
                cls.ATTRIBUTE_ALIASES.get(str(item or "").strip(), "")
                for item in raw_attributes
            ]
            if not all(attributes):
                raise ValueError(f"法术【{spell_name}】包含未知属性。")
            normalized[spell_name] = attributes
        for spell_name in known_spells:
            normalized.setdefault(spell_name, ["INS", "WLP"])
        return normalized

    @staticmethod
    def _option_list(
        options: dict[str, object],
        key: str,
    ) -> list[str]:
        value = options.get(key, [])
        if isinstance(value, str):
            return [value]
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def _owner(self, owner_name: str) -> Character:
        if not self.characters.exists(owner_name):
            raise ValueError(f"没有找到忠诚伙伴的主人【{owner_name}】。")
        owner = self.characters.get(owner_name)
        if "pc" not in owner.traits:
            raise ValueError("【忠诚伙伴】只能属于玩家角色。")
        return owner

    @classmethod
    def _companion_state(cls, companion: Character) -> dict[str, object]:
        state = companion.npc_skill_effects.get(cls.COMPANION_STATE_KEY)
        if not isinstance(state, dict):
            raise ValueError(f"【{companion.name}】缺少忠诚伙伴运行状态。")
        return state

    @staticmethod
    def _species_name(companion: Character) -> str:
        names = {
            "beast": "野兽",
            "construct": "构装体",
            "elemental": "元素",
            "plant": "植物",
        }
        for trait in companion.traits:
            if trait in names:
                return names[trait]
        return "未知"
