from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any


class Affinity(str, Enum):
    NORMAL = "normal"
    WEAK = "weak"
    RESIST = "resist"
    IMMUNE = "immune"
    ABSORB = "absorb"


class DifficultyLevel(IntEnum):
    SIMPLE = 7
    NORMAL = 10
    HARD = 13
    VERY_HARD = 16


class StatusEffect(str, Enum):
    SLOW = "slow"
    DAZED = "dazed"
    WEAKENED = "weakened"
    SHAKEN = "shaken"
    ENRAGED = "enraged"
    POISONED = "poisoned"


class EnemyRank(str, Enum):
    SOLDIER = "soldier"
    ELITE = "elite"
    CHAMPION = "champion"
    VILLAIN = "villain"


class EffectTiming(str, Enum):
    OWNER_TURN_START = "owner_turn_start"
    OWNER_TURN_END = "owner_turn_end"
    ROUND_END = "round_end"
    SCENE_END = "scene_end"


class TriggerTiming(str, Enum):
    CRITICAL_SUCCESS = "critical_success"
    FUMBLE = "fumble"
    AFTER_HIT = "after_hit"
    BEFORE_ZERO_HP = "before_zero_hp"
    TRAVEL_DISCOVERY = "travel_discovery"


class SpellTarget(str, Enum):
    SELF = "self"
    ONE_ALLY = "one_ally"
    ONE_ENEMY = "one_enemy"
    ONE_CREATURE = "one_creature"
    UP_TO_THREE_CREATURES = "up_to_three_creatures"
    ANY_VISIBLE_CREATURES = "any_visible_creatures"
    ALL_ENEMIES = "all_enemies"


class SpellEffectType(str, Enum):
    DAMAGE = "damage"
    MP_DAMAGE = "mp_damage"
    HEAL = "heal"
    DEFENSE_BUFF = "defense_buff"
    DEFENSE_FLOOR = "defense_floor"
    AFFINITY_BUFF = "affinity_buff"
    STATUS_APPLY = "status_apply"
    STATUS_CLEAR = "status_clear"
    STATUS_IMMUNITY = "status_immunity"
    WEAPON_ENCHANT = "weapon_enchant"
    ATTRIBUTE_BUFF = "attribute_buff"
    EXTRA_ACTION = "extra_action"
    SURVIVE_ONCE = "survive_once"
    DISPEL = "dispel"
    CHECK_BONUS = "check_bonus"
    DAMAGE_VULNERABILITY = "damage_vulnerability"
    IMMEDIATE_ATTACK = "immediate_attack"
    NARRATIVE = "narrative"


class SceneType(str, Enum):
    STANDARD = "standard"
    SESSION_ZERO = "session_zero"
    CONFLICT = "conflict"
    INTERLUDE = "interlude"
    GM = "gm"
    REST = "rest"
    TRAVEL = "travel"
    DUNGEON = "dungeon"


class RestType(str, Enum):
    WILDERNESS = "wilderness"
    SETTLEMENT = "settlement"


class TravelThreatLevel(str, Enum):
    MINOR = "minor"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EXTREME = "extreme"


class TravelEventType(str, Enum):
    QUIET = "quiet"
    DANGER = "danger"
    DISCOVERY = "discovery"


class TravelRouteType(str, Enum):
    LAND = "land"
    WATER = "water"
    UNDERWATER = "underwater"
    AIR = "air"


class DungeonExploreMode(str, Enum):
    SCENE = "scene"
    DETAILED = "detailed"
    SKIP = "skip"


class DungeonAreaType(str, Enum):
    ENTRANCE = "entrance"
    PASSAGE = "passage"
    CHALLENGE = "challenge"
    TREASURE = "treasure"
    SAFE_ROOM = "safe_room"
    BOSS = "boss"


class DungeonImportance(str, Enum):
    MAJOR = "major"
    MINOR = "minor"


class DungeonPreparation(str, Enum):
    PREPARED = "prepared"
    IMPROVISED = "improvised"


class EncounterDifficulty(str, Enum):
    EASY = "easy"
    NORMAL = "normal"
    HARD = "hard"
    BOSS = "boss"


class EquipmentItemType(str, Enum):
    WEAPON = "weapon"
    ARMOR = "armor"
    SHIELD = "shield"
    ACCESSORY = "accessory"
    ARTIFACT = "artifact"


class RitualDiscipline(str, Enum):
    ARCANISM = "arcanism"
    CHIMERISM = "chimerism"
    ELEMENTALISM = "elementalism"
    ENTROPISM = "entropism"
    RITUALISM = "ritualism"
    SPIRITISM = "spiritism"


class RitualPotency(str, Enum):
    MINOR = "minor"
    MODERATE = "moderate"
    MAJOR = "major"
    EXTREME = "extreme"


class RitualScope(str, Enum):
    INDIVIDUAL = "individual"
    SMALL = "small"
    LARGE = "large"
    HUGE = "huge"


class ProjectUse(str, Enum):
    CONSUMABLE = "consumable"
    PERMANENT = "permanent"


class PersistentChangeType(str, Enum):
    WORLD_FACT = "world_fact"
    FACILITY = "facility"
    EQUIPMENT = "equipment"
    CONSUMABLE = "consumable"
    TRANSPORT = "transport"


class StoryItemStatus(str, Enum):
    AVAILABLE = "available"
    CARRIED = "carried"
    PLACED = "placed"
    DESTROYED = "destroyed"
    CONSUMED = "consumed"


class MemoryVisibility(str, Enum):
    PUBLIC = "public"
    PRIVATE = "private"


def normalize_memory_visibility(value: MemoryVisibility | str | None) -> MemoryVisibility:
    if isinstance(value, MemoryVisibility):
        return value
    raw = str(value or MemoryVisibility.PUBLIC.value).strip().lower()
    aliases = {
        "public": MemoryVisibility.PUBLIC,
        "公开": MemoryVisibility.PUBLIC,
        "公共": MemoryVisibility.PUBLIC,
        "玩家可见": MemoryVisibility.PUBLIC,
        "publicly": MemoryVisibility.PUBLIC,
        "private": MemoryVisibility.PRIVATE,
        "私密": MemoryVisibility.PRIVATE,
        "秘密": MemoryVisibility.PRIVATE,
        "gm": MemoryVisibility.PRIVATE,
        "gm_only": MemoryVisibility.PRIVATE,
        "gm only": MemoryVisibility.PRIVATE,
        "仅gm": MemoryVisibility.PRIVATE,
        "仅 gm": MemoryVisibility.PRIVATE,
    }
    return aliases.get(raw, MemoryVisibility.PUBLIC)


class SecretLockLevel(str, Enum):
    DRAFT = "draft"
    SEEDED = "seeded"
    PUBLIC = "public"


class SessionZeroStage(str, Enum):
    TONE = "tone"
    PILLARS = "pillars"
    GROUP = "group"
    HEROES = "heroes"
    THREATS = "threats"
    SAFETY = "safety"
    PROLOGUE = "prologue"
    READY = "ready"


class ActionType(str, Enum):
    MINOR_ACTION = "MinorAction"
    ASSIST = "Assist"
    ATTACK = "Attack"
    SPELL = "Spell"
    GUARD = "Guard"
    EQUIP = "Equip"
    HINDER = "Hinder"
    INVESTIGATE = "Investigate"
    OBJECTIVE = "Objective"
    SKILL = "Skill"
    USE_INVENTORY = "UseInventory"
    TINKERER_GADGET = "TinkererGadget"
    SHOP = "Shop"
    REST = "Rest"
    OPEN_CHEST = "OpenChest"
    AWARD_REWARD = "AwardReward"
    EXPLORE_DUNGEON = "ExploreDungeon"
    NEXT_TURN = "NextTurn"
    PLAN_RITUAL = "PlanRitual"
    CONTRIBUTE_RITUAL = "ContributeRitual"
    CAST_RITUAL = "CastRitual"
    START_PROJECT = "StartProject"
    HIRE_PROJECT_HELPERS = "HireProjectHelpers"
    WORK_PROJECT = "WorkProject"
    REQUEST_ROLL = "RequestRoll"
    MODIFY_RESOURCE = "ModifyResource"
    ADVANCE_CLOCK = "AdvanceClock"
    INVOKE_TRAIT = "InvokeTrait"
    INVOKE_BOND = "InvokeBond"
    NPCACT = "NPCAct"
    NARRATE = "Narrate"
    TRIGGER_OPPORTUNITY = "TriggerOpportunity"
    ACCEPT_STORY_CHANGE = "AcceptStoryChange"
    START_CONFLICT = "StartConflict"
    MANAGE_BOND = "ManageBond"
    SELL_ITEM = "SellItem"
    PLAYER_VS_PLAYER = "PlayerVsPlayer"
    ABSENT_PLAYER = "AbsentPlayer"
    RESOLVE_ZERO_HP = "ResolveZeroHP"
    RESOLVE_DECISION = "ResolveDecision"


class DecisionWindowStatus(str, Enum):
    PENDING = "pending"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass
class DecisionWindow:
    """A persisted player/GM choice that can pause a rules transaction.

    ``payload`` contains resume metadata only; player-facing wording belongs to
    the expression layer.  The generic shape lets checks, opportunities,
    zero-HP choices, and held actions share one lifecycle without sharing their
    individual rules.
    """

    window_id: str
    kind: str
    owner: str
    prompt: str = ""
    options: list[dict[str, Any]] = field(default_factory=list)
    status: DecisionWindowStatus = DecisionWindowStatus.PENDING
    scope_kind: str = "scene"
    scope_id: str = ""
    blocking: bool = False
    allowed_responders: list[str] = field(default_factory=list)
    action_type: str = ""
    transaction_id: str = ""
    resume_point: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    dedupe_key: str = ""
    created_at: str = ""
    resolved_at: str = ""
    resolution: dict[str, Any] = field(default_factory=dict)


@dataclass
class Bond:
    target: str
    emotions: list[str] = field(default_factory=list)

    @property
    def strength(self) -> int:
        return min(3, max(0, len(self.emotions)))


@dataclass
class NPCAttackEffect:
    """One structured effect attached to an NPC basic attack."""

    effect_type: str
    trigger: str = "on_hit"
    target_scope: str = "target"
    damage_type: str = ""
    damage_types: list[str] = field(default_factory=list)
    affinity: Affinity | None = None
    status: StatusEffect | None = None
    required_status: StatusEffect | None = None
    required_status_before_hit: bool = False
    amount: int = 0
    action_types: list[str] = field(default_factory=list)
    trait: str = ""
    expires_on: EffectTiming | None = None
    check_attributes: list[str] = field(default_factory=list)
    target_number: int = 0
    clock_segments: int = 0
    note: str = ""


@dataclass
class SwallowedTargetState:
    """One creature currently trapped inside an NPC during a conflict."""

    source: str
    target: str
    escape_clock: str
    damage: int = 20
    damage_type: str = "physical"
    created_round: int = 0


@dataclass
class NPCAttackProfile:
    """One authoritative basic attack on an NPC combat sheet.

    Player characters still use the equipment-shaped ``weapon_*`` fields.
    NPCs may have several genuinely different attacks in the core bestiary,
    so mirroring only the first attack into those legacy fields is lossy.
    """

    attack_id: str
    name: str
    attributes: list[str]
    damage_bonus: int
    damage_type: str = "physical"
    accuracy_modifier: int = 0
    range: str = "melee"
    targets_magic_defense: bool = False
    multi_attack: int = 1
    status_effect_on_hit: StatusEffect | None = None
    damage_type_options: list[str] = field(default_factory=list)
    random_damage_types: list[str] = field(default_factory=list)
    status_options_on_hit: list[StatusEffect] = field(default_factory=list)
    conditional_damage_bonus: int = 0
    conditional_target_statuses: list[StatusEffect] = field(default_factory=list)
    conditional_any_target_status: bool = False
    bonus_if_previous_guard: int = 0
    recover_hp_fraction: float = 0.0
    recover_mp_on_hit: int = 0
    target_mp_loss: int = 0
    target_ip_loss: int = 0
    self_hp_loss_if_all_miss: int = 0
    effects: list[NPCAttackEffect] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class NPCSpellProfile:
    """Private rules-card data for a core-bestiary or custom NPC spell."""

    name: str
    rules_name: str = ""
    attributes: list[str] = field(default_factory=list)
    mp_cost: int = 0
    target: str = ""
    duration: str = "瞬发"
    effect: str = ""


@dataclass
class NPCAbilityProfile:
    """Typed NPC-only skill effect used by the deterministic combat runtime.

    Free-form descriptions remain useful for presentation, but they never grant
    rules authority.  ``trigger`` and ``effect_type`` form a deliberately small
    executable vocabulary that can be validated before the NPC enters combat.
    """

    ability_id: str
    name: str
    source_skill: str
    trigger: str
    effect_type: str
    target_scope: str = "self"
    amount: int = 0
    damage_type: str = ""
    affinity_changes: dict[str, Affinity] = field(default_factory=dict)
    statuses: list[StatusEffect] = field(default_factory=list)
    attack_name: str = ""
    multi_attack: int = 1
    ignore_resist: bool = False
    blocked_by_damage_types: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    expires_on: EffectTiming | None = None
    once_per_scene: bool = False
    description: str = ""


@dataclass
class NPCCombatBlueprint:
    """Validated private combat sheet prepared independently from the GM turn.

    The core GM only needs the blueprint status and identifier.  Full numbers,
    inherited bestiary text and tactical notes stay outside its conversational
    context until a conflict or an explicit inspection actually needs them.
    """

    blueprint_id: str
    npc_name: str
    npc_id: str = ""
    status: str = "ready"
    design_mode: str = "inherit"
    source_template: str = ""
    source_note: str = ""
    scene_id: str = ""
    persona_revision: str = ""
    request_signature: str = ""
    prompt_schema_revision: str = ""
    blueprint_schema_revision: str = ""
    design_model: str = ""
    bestiary_revision: str = ""
    requested_species: str = ""
    preferred_template: str = ""
    requested_level: int = 5
    level: int = 5
    species: str = "humanoid"
    rank: str = "soldier"
    champion_value: int = 1
    combat_side: str = "enemy"
    is_villain: bool = False
    ultima_points: int = 0
    traits: list[str] = field(default_factory=list)
    attributes: dict[str, int] = field(default_factory=dict)
    max_hp: int = 0
    crisis_threshold: int = 0
    max_mp: int = 0
    initiative: int = 0
    defenses: dict[str, int] = field(default_factory=dict)
    affinities: dict[str, Affinity] = field(default_factory=dict)
    status_immunities: list[StatusEffect] = field(default_factory=list)
    attacks: list[NPCAttackProfile] = field(default_factory=list)
    spells: list[NPCSpellProfile] = field(default_factory=list)
    other_actions: list[str] = field(default_factory=list)
    trait_rules: list[str] = field(default_factory=list)
    ability_profiles: list[NPCAbilityProfile] = field(default_factory=list)
    selected_skills: list[str] = field(default_factory=list)
    tactics: dict[str, Any] = field(default_factory=dict)
    validation_notes: list[str] = field(default_factory=list)
    generated_at: str = ""


@dataclass
class Character:
    name: str
    attributes: dict[str, int]
    max_hp: int
    hp: int
    max_mp: int
    mp: int
    level: int = 5
    crisis_threshold: int = 0
    inventory_points: int = 0
    fabula_points: int = 0
    identity: str = ""
    theme: str = ""
    origin: str = ""
    # Portable character-card metadata. These fields deliberately live on the
    # authoritative character so portraits and extension data survive normal
    # campaign save/load cycles without leaking into GM-private world notes.
    card_id: str = ""
    card_revision: int = 1
    player_name: str = ""
    creation_fate_roll: list[int] = field(default_factory=list)
    creation_equipment_cost: int = 0
    notes: list[str] = field(default_factory=list)
    appearance: dict[str, Any] = field(default_factory=dict)
    portrait: dict[str, Any] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)
    bonds: list[Bond] = field(default_factory=list)
    weapon_damage: int = 0
    weapon_type: str = "physical"
    defenses: dict[str, int] = field(default_factory=lambda: {"physical": 10, "magic": 10})
    affinities: dict[str, Affinity] = field(default_factory=dict)
    traits: list[str] = field(default_factory=list)
    statuses: list[StatusEffect] = field(default_factory=list)
    guarding: bool = False
    guarded_target: str | None = None
    temporary_affinities: dict[str, Affinity] = field(default_factory=dict)
    defense_bonuses: dict[str, int] = field(default_factory=lambda: {"physical": 0, "magic": 0})
    defense_floors: dict[str, int] = field(default_factory=lambda: {"physical": 0, "magic": 0})
    temporary_status_immunities: set[StatusEffect] = field(default_factory=set)
    attribute_bonuses: dict[str, int] = field(
        default_factory=lambda: {"DEX": 0, "INS": 0, "MIG": 0, "WLP": 0}
    )
    weapon_damage_type_override: str | None = None
    initiative: int = 0
    abilities: list[str] = field(default_factory=list)
    spells: list[str] = field(default_factory=list)
    classes: dict[str, int] = field(default_factory=dict)
    skills: dict[str, int] = field(default_factory=dict)
    skill_options: dict[str, list[str]] = field(default_factory=dict)
    chimerist_spell_species: dict[str, str] = field(default_factory=dict)
    skill_counters: dict[str, int] = field(default_factory=dict)
    npc_specialty_bonuses: dict[str, int] = field(default_factory=dict)
    npc_skill_effects: dict[str, Any] = field(default_factory=dict)
    npc_spell_check_bonus: int = 0
    npc_spell_damage_bonus: int = 0
    npc_spell_specific_damage_bonuses: dict[str, int] = field(default_factory=dict)
    npc_spell_attributes: dict[str, list[str]] = field(default_factory=dict)
    npc_attacks: list[NPCAttackProfile] = field(default_factory=list)
    npc_spell_profiles: list[NPCSpellProfile] = field(default_factory=list)
    npc_other_actions: list[str] = field(default_factory=list)
    npc_trait_rules: list[str] = field(default_factory=list)
    npc_ability_profiles: list[NPCAbilityProfile] = field(default_factory=list)
    npc_tactics: dict[str, Any] = field(default_factory=dict)
    npc_source_template: str = ""
    experience_points: int = 0
    hero_skills: list[str] = field(default_factory=list)
    bound_arcana: list[str] = field(default_factory=list)
    active_arcanum: str = ""
    max_inventory_points: int = 0
    zenit: int = 0
    equipment: list[str] = field(default_factory=list)
    equipment_templates: dict[str, str] = field(default_factory=dict)
    # Ownership and immediate access are distinct.  A prison evidence locker,
    # ceremonial disarmament, or a dropped pack must not delete purchases from
    # the character sheet, but inaccessible items cannot remain equipped.
    unavailable_equipment: dict[str, dict[str, str]] = field(default_factory=dict)
    suspended_equipment_slots: dict[str, str] = field(default_factory=dict)
    equipped_armor: str = "无防具"
    equipped_shield: str = ""
    equipped_main_hand: str = "徒手攻击"
    equipped_off_hand: str = ""
    equipped_accessory: str = ""
    weapon_accuracy_attributes: list[str] = field(default_factory=lambda: ["DEX", "MIG"])
    weapon_accuracy_modifier: int = 0
    weapon_range: str = "melee"
    permanent_status_immunities: set[StatusEffect] = field(default_factory=set)
    equipment_status_immunities: set[StatusEffect] = field(default_factory=set)
    equipment_affinities: dict[str, Affinity] = field(default_factory=dict)
    equipment_defense_bonuses: dict[str, int] = field(default_factory=lambda: {"physical": 0, "magic": 0})
    equipment_attribute_bonuses: dict[str, int] = field(
        default_factory=lambda: {"DEX": 0, "INS": 0, "MIG": 0, "WLP": 0}
    )
    equipment_accuracy_bonus: int = 0
    equipment_spell_bonus: int = 0
    equipment_initiative_bonus: int = 0
    equipment_attack_damage_bonus: int = 0
    equipment_spell_damage_bonus: int = 0
    equipment_healing_bonus: int = 0
    equipment_multi_attack: int = 0
    equipment_attack_targets_magic_defense: bool = False
    equipment_ignore_resist: bool = False
    equipment_ignore_all_affinities: bool = False
    equipment_on_hit_status: StatusEffect | None = None
    equipment_notes: list[str] = field(default_factory=list)
    lucky_number: int = 7
    trigger_cooldowns: set[str] = field(default_factory=set)
    permanent_trigger_keys: set[str] = field(default_factory=set)
    permanent_skill_ranks_applied: dict[str, int] = field(default_factory=dict)
    # Conditions such as petrification outlive an ordinary scene and are not
    # part of Fabula Ultima's six removable combat statuses.
    special_conditions: dict[str, str] = field(default_factory=dict)

    @property
    def in_crisis(self) -> bool:
        threshold = self.crisis_threshold if self.crisis_threshold > 0 else self.max_hp // 2
        return self.hp <= threshold

    def bond_strength_with(self, target: str) -> int:
        strengths = [bond.strength for bond in self.bonds if bond.target == target]
        return max(strengths) if strengths else 0


@dataclass
class Clock:
    name: str
    max_segments: int
    current: int = 0
    clock_type: str = "objective"
    stakes: str = ""
    gm_note: str = ""
    auto_advance: str = ""
    visibility: str = "foreground"
    auto_advance_timing: str = "action_round_end"
    auto_advance_owner: str = ""
    auto_advance_every: int = 1
    auto_advance_progress: int = 0
    advance_on_rest: bool = False
    pacing_weight: int = 1
    scope: str = ""
    scene_id: str = ""
    owner: str = ""
    source: str = ""
    status: str = "active"
    completion_consequence: str = ""
    resolution_note: str = ""


@dataclass
class SceneFixture:
    """A fixed, interactable part of one scene rather than portable inventory."""

    fixture_id: str
    name: str
    description: str = ""
    current_state: str = ""
    allowed_operations: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    source_authority: str = ""
    history: list[dict[str, str]] = field(default_factory=list)


@dataclass
class SceneRecord:
    name: str
    scene_type: SceneType
    location: str = ""
    participants: list[str] = field(default_factory=list)
    # ``participant_locations`` stores branch-level dramatic locations used to
    # decide which parallel scene owns an actor. ``participant_positions`` is
    # only the actor's stance inside that same scene. Keeping these concepts
    # separate prevents "stand in front of the traveller" from manufacturing a
    # new isolated scene and dropping the rest of the cast.
    participant_locations: dict[str, str] = field(default_factory=dict)
    participant_positions: dict[str, str] = field(default_factory=dict)
    participant_activities: dict[str, str] = field(default_factory=dict)
    objective: str = ""
    summary: str = ""
    active: bool = True
    scene_id: str = ""
    open_conditions: list[dict[str, Any]] = field(default_factory=list)
    # A prepared session opportunity is GM-facing context, not a destination
    # or a plot instruction.  Persisting its functional identity on the scene
    # lets a later camera stay in the same location without losing whether it
    # is the investigation, climax, or aftermath beat of the session.
    session_opportunity_key: str = ""
    session_opportunity_role: str = ""
    session_opportunity_title: str = ""
    session_opportunity_purpose: str = ""
    session_opportunity_situation: str = ""
    pending_transition_location: str = ""
    pending_transition_reason: str = ""
    pending_transition_participants: list[str] = field(default_factory=list)
    # NPCs and bystanders do not need fabricated combat statistics merely to
    # receive a scene-duration narrative effect.  These records remain scoped
    # to this scene and disappear from active play when the scene is archived.
    narrative_effects: list[dict[str, Any]] = field(default_factory=list)
    # Free scenes do not have a strict initiative order, but automatic clocks
    # still need a stable fictional time unit.  One action round completes only
    # after every participating PC has contributed one meaningful action.
    action_round_number: int = 1
    action_round_required_actors: list[str] = field(default_factory=list)
    action_round_acted_actors: list[str] = field(default_factory=list)
    action_round_auto_advance_skip_names: list[str] = field(default_factory=list)
    # PCs who gave up resistance regain consciousness only when a later scene
    # that actually includes them begins. Keeping the receipt on the scene lets
    # the GM narrate that transition without inferring it from raw HP values.
    recovered_fallen_pcs: list[str] = field(default_factory=list)
    scene_fixtures: dict[str, SceneFixture] = field(default_factory=dict)


@dataclass
class RestResult:
    rest_type: RestType
    safe_source: str
    recovered_characters: list[str]
    ip_spent: int = 0
    threat_clock_changes: list[ClockChange] = field(default_factory=list)
    summary: str = ""


@dataclass
class TravelDayResult:
    day: int
    region: str
    threat_level: TravelThreatLevel
    die_size: int
    roll: int
    event_type: TravelEventType
    summary: str
    event_detail: str = ""
    mechanical_hint: str = ""
    discovered_location: str = ""
    danger_tags: list[str] = field(default_factory=list)
    trigger_results: list[TriggerResult] = field(default_factory=list)
    hard_rule_summary: str = ""
    llm_narrative_prompt: str = ""


@dataclass
class TriggerResult:
    actor: str
    source: str
    timing: TriggerTiming
    summary: str
    target: str = ""
    resource_change: ResourceChange | None = None
    prevented_zero_hp: bool = False
    extra_damage: int = 0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class JourneyResult:
    origin: str
    destination: str
    days: int
    day_results: list[TravelDayResult] = field(default_factory=list)
    route_type: TravelRouteType = TravelRouteType.LAND
    distance: int = 0
    transport: str = "徒步"
    travel_multiplier: int = 1
    service_cost: int = 0
    summary: str = ""


@dataclass
class JourneyProgress:
    journey_id: str
    origin: str
    destination: str
    total_days: int
    threat_levels: list[TravelThreatLevel] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    route_type: TravelRouteType = TravelRouteType.LAND
    distance: int = 0
    transport: str = "徒步"
    travel_multiplier: int = 1
    service_cost: int = 0
    party_size: int = 1
    party_names: list[str] = field(default_factory=list)
    default_threat_level: TravelThreatLevel = TravelThreatLevel.MEDIUM
    threat_die_step_reduction: int = 0
    discovery_threshold: int = 1
    current_day: int = 0
    day_results: list[TravelDayResult] = field(default_factory=list)
    pending_event_day: int = 0
    event_resolution_notes: list[str] = field(default_factory=list)
    status: str = "traveling"
    summary: str = ""
    interruption_reason: str = ""
    end_location: str = ""


@dataclass
class TravelRouteRecord:
    origin: str
    destination: str
    route_type: TravelRouteType
    distance: int
    transport: str
    travel_days: int
    default_threat_level: TravelThreatLevel
    regions: list[str] = field(default_factory=list)
    discoveries: list[str] = field(default_factory=list)
    dangers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TransportationOption:
    name: str
    route_type: TravelRouteType
    price: int
    passenger_capacity: int
    travel_multiplier: int
    owned: bool = False
    description: str = ""


@dataclass(frozen=True)
class TravelEventTemplate:
    name: str
    description: str
    mechanical_hint: str = ""
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class DungeonEventTemplate:
    name: str
    description: str
    mechanical_hint: str = ""
    tags: tuple[str, ...] = ()


@dataclass
class AdventureEventContext:
    region: str
    description: str = ""
    terrain: str = ""
    faction: str = ""
    threat_level: TravelThreatLevel = TravelThreatLevel.MEDIUM
    route_type: TravelRouteType = TravelRouteType.LAND
    public_memory: list[str] = field(default_factory=list)
    private_hooks: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class MapLocation:
    name: str
    x: int = 0
    y: int = 0
    description: str = ""
    terrain: str = "草原"
    feature_type: str = ""
    position_hint: str = ""
    relative_to: str = ""
    relative_position: str = ""
    semantic_cell: str = ""
    draw_icon: bool | None = None
    icon_id: str = ""
    threat_level: TravelThreatLevel = TravelThreatLevel.MEDIUM
    route_type: TravelRouteType = TravelRouteType.LAND
    faction: str = ""
    discovered: bool = True
    tags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class SemanticMapLayout:
    """Compact machine-readable geography used by the GM and renderer.

    The route graph remains authoritative for travel rules. This layout only
    owns spatial placement, terrain occupancy and the mapping between symbolic
    grid cells and the latest rendered map.
    """

    version: int = 1
    grid_width: int = 20
    grid_height: int = 12
    terrain_rows: list[str] = field(default_factory=list)
    location_cells: dict[str, str] = field(default_factory=dict)
    location_points: dict[str, dict[str, Any]] = field(default_factory=dict)
    source: str = ""
    manifest_path: str = ""
    revision: int = 0
    updated_at: str = ""


@dataclass
class MapRouteSegment:
    region: str
    distance_days: int = 1
    threat_level: TravelThreatLevel = TravelThreatLevel.MEDIUM
    terrain: str = ""
    description: str = ""


@dataclass
class MapRouteEdge:
    route_id: str
    origin: str
    destination: str
    distance_days: int = 1
    default_threat_level: TravelThreatLevel = TravelThreatLevel.MEDIUM
    route_type: TravelRouteType = TravelRouteType.LAND
    terrain: str = ""
    description: str = ""
    bidirectional: bool = True
    discovered: bool = True
    segments: list[MapRouteSegment] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class WorldRoutePlan:
    origin: str
    destination: str
    distance: int
    travel_days: int
    route_type: TravelRouteType
    transport: str
    travel_multiplier: int
    service_cost: int
    threat_levels: list[TravelThreatLevel] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    event_tables_by_region: dict[str, dict[str, list[TravelEventTemplate]]] = field(default_factory=dict)
    waypoints: list[str] = field(default_factory=list)
    memory_hooks: list[str] = field(default_factory=list)
    route_source: str = "explicit"
    route_edge_ids: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class DungeonArea:
    name: str
    area_type: DungeonAreaType
    description: str = ""
    exits: list[str] = field(default_factory=list)
    danger_clock: str = ""
    trap: str = ""
    treasure: str = ""
    reward_item: str = ""
    reward_zenit: int | None = None
    reward_rarity: str = "standard"
    boss: str = ""
    event_templates: list[DungeonEventTemplate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    cleared: bool = False
    discovered: bool = False
    trap_disarmed: bool = False
    treasure_collected: bool = False


@dataclass
class DungeonExplorationResult:
    actor: str
    dungeon_name: str
    area_name: str
    area_type: DungeonAreaType
    action: str
    description: str = ""
    exits: list[str] = field(default_factory=list)
    trap: str = ""
    trap_triggered: bool = False
    trap_disarmed: bool = False
    treasure: str = ""
    reward_item: str = ""
    reward_zenit: int | None = None
    reward_rarity: str = "standard"
    treasure_found: bool = False
    treasure_collected: bool = False
    boss: str = ""
    boss_revealed: bool = False
    event_name: str = ""
    event_detail: str = ""
    event_tags: list[str] = field(default_factory=list)
    danger_change: ClockChange | None = None
    area_cleared: bool = False
    notes: list[str] = field(default_factory=list)
    summary: str = ""
    hard_rule_summary: str = ""
    llm_narrative_prompt: str = ""


@dataclass
class DungeonMap:
    dungeon_name: str
    areas: list[DungeonArea] = field(default_factory=list)
    entrance: str = ""
    boss_room: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class DungeonState:
    name: str
    mode: DungeonExploreMode
    active: bool = False
    location: str = ""
    danger_clocks: list[str] = field(default_factory=list)
    concept: str = ""
    focus: str = ""
    inhabitants: str = ""
    peculiarity: str = ""
    purpose: str = ""
    key_point: str = ""
    rewards: list[str] = field(default_factory=list)
    obstacles: list[str] = field(default_factory=list)
    areas: list[DungeonArea] = field(default_factory=list)
    current_area: str = ""
    boss_room: str = ""
    notes: list[str] = field(default_factory=list)
    completion_status: str = ""
    completion_summary: str = ""


@dataclass
class DungeonDesignBrief:
    name: str
    importance: DungeonImportance
    preparation: DungeonPreparation
    recommended_mode: DungeonExploreMode
    concept: str
    focus: str
    inhabitants: str
    peculiarity: str
    purpose: str = ""
    style: str = ""
    threats: list[str] = field(default_factory=list)
    obstacles: list[str] = field(default_factory=list)
    rewards: list[str] = field(default_factory=list)
    danger_clocks: dict[str, int] = field(default_factory=dict)
    key_point: str = ""
    guidance: list[str] = field(default_factory=list)
    flow_checklist: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class RitualPlan:
    name: str
    caster: str
    discipline: RitualDiscipline
    potency: RitualPotency
    scope: RitualScope
    effect: str
    mp_cost: int
    target_number: int
    attributes: list[str]
    clock_segments: int
    clock_name: str = ""
    rare_material: str = ""
    forbidden_tags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    scene_id: str = ""
    started_turn_serial: int = 0
    ready_turn_serial: int = 0


@dataclass
class RitualCastResult:
    plan: RitualPlan
    roll: RollOutcome | None = None
    mp_change: ResourceChange | None = None
    success: bool = False
    catastrophe: str = ""
    summary: str = ""


@dataclass
class ProjectState:
    name: str
    inventor: str
    potency: RitualPotency
    scope: RitualScope
    use: ProjectUse
    effect: str
    material_cost: int
    required_progress: int
    current_progress: int = 0
    output_type: PersistentChangeType = PersistentChangeType.WORLD_FACT
    owner: str = ""
    location: str = ""
    flaw: str = ""
    special_materials: list[str] = field(default_factory=list)
    cost_materials: list[str] = field(default_factory=list)
    helpers: int = 0
    completed: bool = False
    persisted: bool = False
    created_asset_id: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class PersistentChange:
    change_type: PersistentChangeType
    name: str
    description: str
    source: str
    owner: str = ""
    location: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class StoryItemEvent:
    operation: str
    actor: str
    changed_at: str
    from_holder: str = ""
    to_holder: str = ""
    from_location: str = ""
    to_location: str = ""
    from_state: str = ""
    to_state: str = ""
    public_fact: str = ""
    source: str = ""


@dataclass
class StoryItem:
    """A unique fictional object whose custody matters after this turn."""

    item_id: str
    name: str
    description: str = ""
    holder: str = ""
    location: str = ""
    current_state: str = ""
    status: StoryItemStatus = StoryItemStatus.AVAILABLE
    tags: list[str] = field(default_factory=list)
    history: list[StoryItemEvent] = field(default_factory=list)


@dataclass
class InventoryUseResult:
    actor: str
    item_name: str
    ip_change: ResourceChange
    resource_changes: list[ResourceChange] = field(default_factory=list)
    damage_results: list[dict[str, Any]] = field(default_factory=list)
    status_changes: list[str] = field(default_factory=list)
    created_asset: PersistentChange | None = None
    summary: str = ""


@dataclass
class TinkererGadgetResult:
    actor: str
    gadget_type: str
    mode: str
    ip_change: ResourceChange | None = None
    rolls: list[int] = field(default_factory=list)
    target_roll: int = 0
    effect_roll: int = 0
    targets: list[str] = field(default_factory=list)
    resource_changes: list[ResourceChange] = field(default_factory=list)
    damage_results: list[dict[str, Any]] = field(default_factory=list)
    status_changes: list[str] = field(default_factory=list)
    created_asset: PersistentChange | None = None
    nested_resolution: Any = None
    summary: str = ""


@dataclass
class ShopTransaction:
    actor: str
    item_name: str
    quantity: int
    total_cost: int
    zenit_before: int
    zenit_after: int
    ip_before: int = 0
    ip_after: int = 0
    added_items: list[str] = field(default_factory=list)
    removed_items: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class ServiceTransaction:
    payer: str
    service_name: str
    service_type: str
    total_cost: int
    zenit_before: int
    zenit_after: int
    party_size: int = 1
    days: int = 0
    settlement_size: str = ""
    transport: str = ""
    summary: str = ""


@dataclass
class TransportPurchase:
    buyer: str
    transport_name: str
    total_cost: int
    zenit_before: int
    zenit_after: int
    owner: str = "小队"
    passenger_capacity: int = 0
    travel_multiplier: int = 1
    route_type: TravelRouteType = TravelRouteType.LAND
    created_asset: PersistentChange | None = None
    summary: str = ""


@dataclass
class DungeonRewardPlacement:
    dungeon_name: str
    area_name: str
    reward_item: str = ""
    reward_zenit: int = 0
    rarity: str = "standard"
    summary: str = ""
    hard_rule_summary: str = ""
    llm_narrative_prompt: str = ""


@dataclass
class ChestReward:
    opener: str
    chest_name: str
    zenit: int = 0
    items: list[str] = field(default_factory=list)
    rare_items: list[str] = field(default_factory=list)
    ip_restored: int = 0
    summary: str = ""
    hard_rule_summary: str = ""
    llm_narrative_prompt: str = ""


@dataclass
class SessionReward:
    party_level: int
    zenit: int
    rare_items: list[str] = field(default_factory=list)
    summary: str = ""
    hard_rule_summary: str = ""
    llm_narrative_prompt: str = ""


@dataclass
class ChapterSettlement:
    chapter_title: str
    participating_pcs: list[str]
    experience_report: SessionExperienceReport
    reward: SessionReward
    world_changes: list[str] = field(default_factory=list)
    level_up_available: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class HeroLogEntry:
    """可审计的单名英雄章节记录。

    官方战役式跑法需要知道某个 PC 参加过哪些章节、拿过哪些奖励；
    自宅团也能用它追踪长期角色成长，而不必从完整 transcript 里翻旧账。
    """

    hero_name: str
    chapter_title: str
    session_id: str = ""
    campaign_id: str = ""
    player_name: str = ""
    gm_name: str = ""
    created_at: str = ""
    starting_level: int = 0
    ending_level: int = 0
    xp_awarded: int = 0
    zenit_awarded: int = 0
    rare_items: list[str] = field(default_factory=list)
    rewards: list[str] = field(default_factory=list)
    story_flags: list[str] = field(default_factory=list)
    bonds_changed: list[str] = field(default_factory=list)
    approvals: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class RareItemApproval:
    request_id: str
    item_name: str
    requester: str = ""
    item_type: str = ""
    source: str = ""
    status: str = "pending"
    created_at: str = ""
    approved_at: str = ""
    price: int = 0
    effects: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class ChapterBeat:
    title: str
    beat_type: str = "scene"
    status: str = "pending"
    expected_minutes: int = 0
    summary: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class ChapterRunRecord:
    chapter_title: str
    session_id: str = ""
    campaign_id: str = ""
    status: str = "draft"
    gm_name: str = ""
    synopsis: str = ""
    intro_prompt: str = ""
    conclusion_prompt: str = ""
    timebox_minutes: int = 180
    participants: list[str] = field(default_factory=list)
    beats: list[ChapterBeat] = field(default_factory=list)
    shared_creation_slots: list[str] = field(default_factory=list)
    iconic_elements: list[str] = field(default_factory=list)
    rewards: list[str] = field(default_factory=list)
    downtime_notes: list[str] = field(default_factory=list)
    temporary_bonds: list[str] = field(default_factory=list)
    gm_scenes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class ChapterPackageScene:
    title: str
    scene_type: str = "scene"
    location: str = ""
    purpose: str = ""
    when_to_use: str = ""
    required_elements: list[str] = field(default_factory=list)
    optional_elements: list[str] = field(default_factory=list)
    success_condition: str = ""
    exit_condition: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class ChapterPackage:
    """Official-campaign style scenario packet.

    It is a GM-facing structure, not transcript text. It keeps fixed intro/outro,
    shared-creation slots, iconic elements and scene options together so an
    improvised campaign can still feel like a coherent chapter.
    """

    chapter_title: str
    synopsis: str = ""
    intro_prompt: str = ""
    conclusion_prompt: str = ""
    timebox_minutes: int = 180
    shared_creation_slots: list[str] = field(default_factory=list)
    iconic_elements: list[str] = field(default_factory=list)
    scenes: list[ChapterPackageScene] = field(default_factory=list)
    adversary_notes: list[str] = field(default_factory=list)
    reward_notes: list[str] = field(default_factory=list)
    gm_notes: list[str] = field(default_factory=list)
    status: str = "draft"
    # ``public_fact`` preserves older authored packages whose intro is a fact
    # to show verbatim. ``gm_direction`` is backstage staging guidance that a
    # scene writer must realize, never transcript text.
    intro_prompt_mode: str = "public_fact"


@dataclass
class IconicElementState:
    name: str
    element_type: str = "generic"
    description: str = ""
    protection_level: str = "protected"
    allowed_interactions: list[str] = field(default_factory=list)
    restrictions: list[str] = field(default_factory=list)
    source: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class TransparencyAuditEntry:
    check_name: str
    passed: bool
    message: str
    severity: str = "info"
    source: str = ""


@dataclass
class RewardBudget:
    party_level: int
    pc_count: int
    max_item_value: int | None
    average_value: int
    tier: int
    summary: str = ""


@dataclass(frozen=True)
class RareItemQuality:
    name: str
    item_type: EquipmentItemType | str
    price_modifier: int
    description: str
    tags: list[str] = field(default_factory=list)


@dataclass
class RareItemDesign:
    name: str
    item_type: EquipmentItemType | str
    base_item: str
    price: int
    description: str = ""
    damage_type: str = "physical"
    accuracy_attributes: list[str] = field(default_factory=list)
    accuracy_modifier: int = 0
    damage_bonus: int = 0
    hands: int = 0
    range_type: str = ""
    required_ability: str = ""
    qualities: list[RareItemQuality] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class EncounterDesign:
    party_level: int
    pc_count: int
    difficulty: EncounterDifficulty
    soldier_equivalent: int
    suggested_enemy_level_range: str
    expected_enemy_damage: int = 0
    expected_soldier_hp: int = 0
    enemy_mix: list[str] = field(default_factory=list)
    battle_principles: list[str] = field(default_factory=list)
    resource_pressure_notes: list[str] = field(default_factory=list)
    level_relationship_notes: list[str] = field(default_factory=list)
    ideal_duration_rounds: str = "3-4"
    transparency_notes: list[str] = field(default_factory=list)
    special_mechanics: list[str] = field(default_factory=list)
    risk_checks: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class MemoryEvent:
    event_id: str
    created_at: str
    kind: str
    summary: str
    visibility: MemoryVisibility = MemoryVisibility.PUBLIC
    entities: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    source: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryRelation:
    source: str
    relation: str
    target: str
    visibility: MemoryVisibility = MemoryVisibility.PUBLIC
    evidence: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class GMSecretRevision:
    revised_at: str
    previous_content: str
    new_content: str
    reason: str = ""
    preserve_clues: list[str] = field(default_factory=list)


@dataclass
class GMSecret:
    secret_id: str
    title: str
    content: str
    lock_level: SecretLockLevel = SecretLockLevel.DRAFT
    created_at: str = ""
    updated_at: str = ""
    related_entities: list[str] = field(default_factory=list)
    public_clues: list[str] = field(default_factory=list)
    revisions: list[GMSecretRevision] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class ProjectProgressResult:
    project: ProjectState
    workers: list[str]
    progress_added: int
    before: int
    after: int
    completed: bool
    summary: str = ""


@dataclass
class ExperienceGain:
    character_name: str
    before: int
    after: int
    amount: int
    can_level_up: bool


@dataclass
class SessionExperienceReport:
    participating_pcs: list[str]
    base_xp: int
    ultima_spent: int
    fabula_spent: int
    fabula_xp: int
    total_xp: int
    gains: list[ExperienceGain] = field(default_factory=list)
    summary: str = ""


@dataclass
class LevelUpResult:
    character_name: str
    level_before: int
    level_after: int
    xp_before: int
    xp_after: int
    class_name: str
    class_level_before: int
    class_level_after: int
    skill_name: str
    skill_rank_after: int
    attribute_increase: str = ""
    hero_skill: str = ""
    mastered_class: str = ""
    max_hp_before: int = 0
    max_hp_after: int = 0
    max_mp_before: int = 0
    max_mp_after: int = 0
    max_ip_before: int = 0
    max_ip_after: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class MemoryRecallResult:
    query: str
    entities: list[str] = field(default_factory=list)
    public_memory: list[str] = field(default_factory=list)
    private_memory: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class SessionTranscriptEntry:
    campaign_id: str
    session_id: str
    created_at: str
    role: str
    speaker: str
    content: str
    channel_id: str = ""
    message_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StorySessionSummary:
    campaign_id: str
    session_id: str
    title: str
    created_at: str
    public_summary: str
    short_memory: str
    timeline: list[str] = field(default_factory=list)
    spotlight_characters: list[str] = field(default_factory=list)
    important_npcs: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    rewards: list[str] = field(default_factory=list)
    unresolved_threads: list[str] = field(default_factory=list)
    private_notes: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    evidence_lines: list[str] = field(default_factory=list)
    transcript_path: str = ""
    transcript_txt_path: str = ""
    summary_path: str = ""
    memory_path: str = ""
    # 场次摘要是从逐条记录派生的召回索引，不是可直接改写世界的事实源。
    authority: str = "derived_non_authoritative"
    source_entry_count: int = 0
    # 同步收团只写入可确定重建的 heuristic 摘要。后台 LLM 只能
    # 在来源版本仍一致时替换这份派生索引，不得改写权威状态。
    generation_method: str = "heuristic_sync"
    source_state_version: int = 0
    source_snapshot_version: str = ""
    source_summary_job_id: str = ""


class StoryArcPhase(str, Enum):
    OPENING = "opening"
    RISING = "rising"
    MIDPOINT = "midpoint"
    CRISIS = "crisis"
    FINALE = "finale"


class CampaignLength(str, Enum):
    SHORT = "short"
    STANDARD = "standard"
    LONG = "long"


@dataclass
class StoryThread:
    thread_id: str
    title: str
    thread_type: str = "plot"
    status: str = "seeded"
    summary: str = ""
    entities: list[str] = field(default_factory=list)
    related_tags: list[str] = field(default_factory=list)
    public_clues: list[str] = field(default_factory=list)
    private_notes: list[str] = field(default_factory=list)
    progress: int = 0
    priority: int = 1
    source: str = ""


@dataclass
class VillainPressureTrack:
    track_id: str
    villain: str
    goal: str
    stage: str = "seeded"
    clock_name: str = ""
    segments: int = 6
    current: int = 0
    visible_consequence: str = ""
    last_action: str = ""
    related_threads: list[str] = field(default_factory=list)
    source: str = ""


@dataclass
class RevealCandidate:
    reveal_id: str
    title: str
    secret: str = ""
    status: str = "seeded"
    required_clues: int = 2
    public_clues: list[str] = field(default_factory=list)
    related_entities: list[str] = field(default_factory=list)
    best_phase: str = "midpoint"
    source: str = ""


@dataclass
class LocationReturnState:
    location: str
    status: str = "stable"
    last_seen: str = ""
    changes: list[str] = field(default_factory=list)
    next_prompt: str = ""
    source: str = ""


@dataclass
class NextSessionAgenda:
    opening_image: str = ""
    recommended_focus: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    suggested_scene_type: str = "standard"
    pressure_moves: list[str] = field(default_factory=list)
    scene_closure: list[str] = field(default_factory=list)
    campaign_pacing: list[str] = field(default_factory=list)
    director_moves: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class CampaignPacingProfile:
    length: CampaignLength = CampaignLength.STANDARD
    target_sessions: int = 35
    target_arcs: int = 5
    session_hours: int = 4
    current_arc: int = 1
    current_arc_title: str = "第一幕"
    boss_every_sessions: int = 5
    minor_climax_every_sessions: int = 3
    notes: list[str] = field(default_factory=list)


@dataclass
class PressureBudget:
    phase: StoryArcPhase = StoryArcPhase.OPENING
    max_foreground_pressure_clocks: int = 1
    max_auto_advance_clocks: int = 1
    max_public_clock_lines: int = 3
    allow_multi_threat_pressure: bool = False
    boss_pressure_allowed: bool = False
    guidance: list[str] = field(default_factory=list)


@dataclass
class SessionFeedbackSignals:
    session_number: int = 1
    meaningful_turns: int = 0
    scene_count: int = 0
    resource_spend_events: int = 0
    unresolved_thread_count: int = 0
    villain_drought_sessions: int = 0
    reveal_uptake: float = 1.0
    stalled_beats: int = 0
    foreground_pressure_count: int = 0
    choice_count: int = 0
    consequence_count: int = 0
    villain_move_observed: bool = False
    reveal_understood: bool = False
    resource_pressure_ratio: float = 0.0
    local_question_changed: bool = False
    local_question_resolved: bool = False
    session_question_resolved: bool = False
    session_close_requested: bool = False
    deliberate_cliffhanger: bool = False
    reversal_reached: bool = False
    memory_anchor_complete: bool = False
    session_identity_distinct: bool = True
    cause_effect_linked: bool = True
    gm_control_present: bool = True
    npc_answer_complete: bool = True
    player_agency_preserved: bool = True
    signature_image_evolved: bool = False
    local_payoff_present: bool = False
    previous_consequence_recalled: bool = True
    memory_similarity_to_recent: float = 0.0
    pending_blocking_decision_count: int = 0
    pending_scene_commitment_count: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class SessionSceneOpportunity:
    """A movable scene situation prepared for one table session.

    Opportunities are not an ordered plot.  The GM may use, combine, relocate,
    or discard them according to player choices, while preserving public facts.
    """

    scene_key: str = ""
    scene_role: str = "situation"
    title: str = ""
    location: str = ""
    situation: str = ""
    purpose: str = ""
    pressure: str = ""
    entry_points: list[str] = field(default_factory=list)
    possible_changes: list[str] = field(default_factory=list)
    clue_route_ids: list[str] = field(default_factory=list)
    npc_names: list[str] = field(default_factory=list)
    required_elements: list[str] = field(default_factory=list)
    required_npc_names: list[str] = field(default_factory=list)
    optional: bool = True


@dataclass
class SessionClueRoute:
    """One of several independent ways to reach an important conclusion."""

    route_id: str = ""
    conclusion: str = ""
    approach: str = ""
    source: str = ""
    visible_lead: str = ""
    success_reveal: str = ""
    fallback: str = ""


@dataclass
class SessionNPCRole:
    """The current dramatic job and intent of an NPC in this session."""

    name: str = ""
    persona_id: str = ""
    public_role: str = ""
    goal_now: str = ""
    leverage: str = ""
    authority_scope: str = ""
    concrete_demand: str = ""
    acceptance_rule: str = ""
    promised_result: str = ""
    public_lead: str = ""
    fulfillment_routes: list[str] = field(default_factory=list)
    refusal_move: str = ""
    voice_cue: str = ""
    private_secret: str = ""
    if_helped: str = ""
    if_blocked: str = ""


@dataclass
class SessionDramaticContract:
    session_number: int = 1
    title: str = ""
    location: str = ""
    dramatic_question: str = ""
    local_question_key: str = ""
    opening_disruption: str = ""
    signature_image: str = ""
    spotlight_hero: str = ""
    focus_thread: str = ""
    opposition_goal: str = ""
    dilemma: str = ""
    reversal: str = ""
    climax_type: str = ""
    closure_requirement: str = ""
    situation_facts: list[str] = field(default_factory=list)
    flexible_secrets: list[str] = field(default_factory=list)
    opening_equipment_restrictions: list[dict[str, Any]] = field(default_factory=list)
    potential_scenes: list[SessionSceneOpportunity] = field(default_factory=list)
    clue_routes: list[SessionClueRoute] = field(default_factory=list)
    important_npcs: list[SessionNPCRole] = field(default_factory=list)
    fantastic_details: list[str] = field(default_factory=list)
    escalation_ladder: list[str] = field(default_factory=list)
    possible_payoffs: list[str] = field(default_factory=list)
    irreversible_change: str = ""
    ending_echo: str = ""
    stinger: str = ""
    callback_seed: str = ""
    inherited_consequence: str = ""
    memory_anchor: str = ""
    status: str = "planned"
    # The contract itself is persisted in both the current plan and the
    # session-contract history.  Keeping the preparation identity beside it
    # lets a later process distinguish a reusable pre-generated contract from
    # one whose authoritative inputs changed after it was prepared.
    preparation_fingerprint: str = ""
    preparation_status: str = "unprepared"
    preparation_source: str = ""
    prepared_at: str = ""


@dataclass
class PreparedSessionContractCache:
    """Private, persistent result of an off-path Session Zero prefetch."""

    schema_version: int = 1
    fingerprint: str = ""
    contract: SessionDramaticContract = field(
        default_factory=SessionDramaticContract
    )
    model: str = ""
    review_model: str = ""
    quality_status: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)
    prepared_at: str = ""
    source_state_version: int = 0


@dataclass
class SessionSceneProgress:
    """Authoritative evidence accumulated inside one played scene.

    Merely opening or renaming a scene is not progress.  A scene becomes a
    substantial part of the table session only after players engage with it
    and the world produces a committed change, or after a decisive payoff is
    resolved there.  Keeping this evidence per scene prevents a reversal from
    an earlier camera satisfying every later act transition.
    """

    scene_id: str = ""
    scene_role: str = ""
    location: str = ""
    opening_image: str = ""
    opening_signature: str = ""
    opening_signature_realized: bool = False
    player_actions: int = 0
    player_responded: bool = False
    material_changes: int = 0
    consequences: int = 0
    local_payoffs: int = 0
    reveals: int = 0
    opposition_moves: int = 0
    climax_events: int = 0
    local_question_changed: bool = False
    local_question_resolved: bool = False
    reversal_reached: bool = False
    ended: bool = False
    close_reason: str = ""

    @property
    def has_local_outcome(self) -> bool:
        return bool(
            self.consequences
            or self.local_payoffs
            or self.climax_events
            or self.local_question_changed
            or self.local_question_resolved
        )

    @property
    def substantial(self) -> bool:
        # A decisive payoff may be delivered by the opposition or environment
        # after the players' preceding choice, so it need not itself be tagged
        # as another player action.  Ordinary setup and atmosphere never count.
        decisive = bool(
            self.local_payoffs
            or self.climax_events
            or self.local_question_changed
            or self.local_question_resolved
        )
        return decisive or bool(self.player_actions and self.material_changes)


@dataclass
class SessionEpisodeProgress:
    """Backstage evidence that one table session has actually developed.

    This is deliberately evidence-first.  A planned reversal or climax does
    not count until play produces a public event that can be recorded here.
    The GM may revise every unrevealed idea, while player choices and their
    established consequences remain stable campaign facts.
    """

    session_number: int = 1
    stage: str = "opening"
    scene_ids: list[str] = field(default_factory=list)
    active_scene_id: str = ""
    scene_progress: dict[str, SessionSceneProgress] = field(default_factory=dict)
    substantial_scene_ids: list[str] = field(default_factory=list)
    meaningful_turns: int = 0
    player_choices: list[str] = field(default_factory=list)
    concrete_consequences: list[str] = field(default_factory=list)
    local_payoffs: list[str] = field(default_factory=list)
    revealed_changes: list[str] = field(default_factory=list)
    climax_events: list[str] = field(default_factory=list)
    opposition_moves: list[str] = field(default_factory=list)
    public_images: list[str] = field(default_factory=list)
    callback_events: list[str] = field(default_factory=list)
    recent_action_signatures: list[str] = field(default_factory=list)
    stagnant_player_turns: int = 0
    max_stagnant_player_turns: int = 0
    last_player_material_change_turn: int = 0
    gm_beat_purposes: list[str] = field(default_factory=list)
    gm_beat_player_turns: list[int] = field(default_factory=list)
    resource_snapshot: dict[str, dict[str, int]] = field(default_factory=dict)
    resource_spend_events: int = 0
    resource_pressure_ratio: float = 0.0
    signature_image_evolved: bool = False
    opening_signature: str = ""
    opening_signature_realized: bool = False
    previous_consequence_recalled: bool = False
    local_question_changed: bool = False
    local_question_resolved: bool = False
    session_question_resolved: bool = False
    session_close_requested: bool = False
    deliberate_cliffhanger: bool = False
    reversal_reached: bool = False
    memory_image: str = ""
    memory_choice: str = ""
    memory_consequence: str = ""
    closure_ready: bool = False
    # Narrative closure is separate from administrative pause/save.  A fresh
    # question in the aftermath reopens the table until a player answers it;
    # an explicit pause may still persist the current scene at any stage.
    closure_stage: str = "active"
    awaiting_player_response: bool = False
    pending_player_prompt: str = ""
    pending_player_scene_id: str = ""
    aftermath_response_count: int = 0
    next_session_hooks: list[str] = field(default_factory=list)
    closing_mode: str = ""
    last_event: str = ""


@dataclass
class SessionPacingPlan:
    session_number: int = 1
    arc_index: int = 1
    arc_title: str = "第一幕"
    phase: StoryArcPhase = StoryArcPhase.OPENING
    strong_start: str = ""
    expected_scene_count: tuple[int, int] = (2, 4)
    expected_table_turns: tuple[int, int] = (18, 28)
    reveal_quota: int = 1
    pressure_budget: PressureBudget = field(default_factory=PressureBudget)
    villain_cadence: str = "反派以痕迹、代理人或后果出现，不急着亲自压场。"
    boss_cadence: str = "不急于 Boss 战。"
    gm_autonomy_cadence: list[str] = field(default_factory=list)
    session_structure: list[str] = field(default_factory=list)
    gm_notes: list[str] = field(default_factory=list)
    dramatic_contract: SessionDramaticContract = field(default_factory=SessionDramaticContract)
    feedback_adjustments: list[str] = field(default_factory=list)


@dataclass
class CampaignArcState:
    phase: StoryArcPhase = StoryArcPhase.OPENING
    session_count: int = 0
    chapter_count: int = 0
    processed_session_ids: list[str] = field(default_factory=list)
    threads: list[StoryThread] = field(default_factory=list)
    villain_pressure: list[VillainPressureTrack] = field(default_factory=list)
    reveals: list[RevealCandidate] = field(default_factory=list)
    locations: list[LocationReturnState] = field(default_factory=list)
    agenda: NextSessionAgenda = field(default_factory=NextSessionAgenda)
    pacing_profile: CampaignPacingProfile = field(default_factory=CampaignPacingProfile)
    current_pacing_plan: SessionPacingPlan = field(default_factory=SessionPacingPlan)
    current_session_progress: SessionEpisodeProgress = field(default_factory=SessionEpisodeProgress)
    session_feedback_history: list[SessionFeedbackSignals] = field(default_factory=list)
    session_contract_history: list[SessionDramaticContract] = field(default_factory=list)
    session_progress_history: list[SessionEpisodeProgress] = field(default_factory=list)
    # A model-reviewed candidate for ``session_count + 1``.  This envelope is
    # private preparation only: it must not be installed into
    # ``current_pacing_plan`` or ``session_contract_history`` until the normal
    # start-session transaction validates and consumes it.
    prepared_next_session_contract: PreparedSessionContractCache | None = None
    last_updated: str = ""


@dataclass
class GMStyleProfile:
    name: str = "时悠"
    voice: str = "轻快、宅系、像社团主持人一样会吐槽，但在规则和安全边界上很可靠"
    agenda: list[str] = field(
        default_factory=lambda: [
            "让英雄的选择推动世界",
            "提出能点燃玩家想象的问题",
            "把八大支柱转化为可玩的地点、冲突与反派",
            "保持乐观的英雄基调，同时允许悲剧和代价存在",
        ]
    )
    table_manner: str = "像 ACG 社团线上群聊里的 GM，先接住玩家想法，再给出两到三个可选方向。"


@dataclass
class HeroDraft:
    """Session 0 中尚未定稿的角色卡草稿。

    草稿允许玩家一点点补想法；只有转成 HeroCreationProfile 后才会进入硬规则建卡。
    """

    player_name: str = ""
    hero_name: str = ""
    identity: str = ""
    theme: str = ""
    origin: str = ""
    class_preferences: list[str] = field(default_factory=list)
    classes: dict[str, int] = field(default_factory=dict)
    attributes: dict[str, int] = field(default_factory=dict)
    bonds: list[str] = field(default_factory=list)
    skills: dict[str, int] = field(default_factory=dict)
    skill_options: dict[str, list[str]] = field(default_factory=dict)
    spells: list[str] = field(default_factory=list)
    bound_arcana: list[str] = field(default_factory=list)
    equipment: list[str] = field(default_factory=list)
    equipment_slots: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    confirmed: bool = False


@dataclass
class ProloguePrompt:
    group_key: str
    option: int
    title: str
    premise: str
    questions: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class FirstActCandidate:
    candidate_id: str
    title: str
    group_key: str
    option: int
    premise: str
    questions: list[str] = field(default_factory=list)
    suggested_bonds: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    votes: list[str] = field(default_factory=list)


@dataclass
class FirstActVoteResult:
    winner: FirstActCandidate | None = None
    candidates: list[FirstActCandidate] = field(default_factory=list)
    vote_counts: dict[str, int] = field(default_factory=dict)
    summary: str = ""


@dataclass
class GMSecretAuditEntry:
    secret_id: str
    title: str
    lock_level: str
    related_entities: list[str] = field(default_factory=list)
    public_clues: list[str] = field(default_factory=list)
    revision_count: int = 0
    tags: list[str] = field(default_factory=list)
    content: str = ""
    risks: list[str] = field(default_factory=list)


@dataclass
class GMSecretAuditReport:
    entries: list[GMSecretAuditEntry] = field(default_factory=list)
    orphan_notes: list[str] = field(default_factory=list)
    public_facts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class OptionalRuleState:
    enabled: bool = False
    note: str = ""
    source: str = ""


@dataclass
class WorldCreationProfile:
    campaign_title: str = ""
    continent_name: str = ""
    tone_preferences: list[str] = field(default_factory=list)
    playstyle_themes: list[str] = field(default_factory=list)
    party_dynamic: str = ""
    description_style: str = ""
    violence_guideline: str = ""
    evil_guidelines: list[str] = field(default_factory=list)
    romance_guideline: str = ""
    consensus_notes: list[str] = field(default_factory=list)
    pre_session_ready: bool = False
    optional_rules: dict[str, OptionalRuleState] = field(default_factory=dict)
    world_style: str = ""
    world_shape: str = ""
    map_card: str = ""
    travel_day_length: str = ""
    magic_tech_role: str = ""
    pillars: dict[str, str] = field(default_factory=dict)
    core_themes: list[str] = field(default_factory=list)
    group_concept: str = ""
    starting_region: str = ""
    major_locations: dict[str, str] = field(default_factory=dict)
    kingdoms: dict[str, str] = field(default_factory=dict)
    kingdom_contributors: dict[str, list[str]] = field(default_factory=dict)
    historical_events: list[str] = field(default_factory=list)
    historical_event_contributors: dict[str, list[str]] = field(default_factory=dict)
    factions: dict[str, str] = field(default_factory=dict)
    villain_seeds: list[str] = field(default_factory=list)
    villain_mirrors: list[str] = field(default_factory=list)
    mysteries: list[str] = field(default_factory=list)
    mystery_contributors: dict[str, list[str]] = field(default_factory=dict)
    world_threats: list[str] = field(default_factory=list)
    threat_contributors: dict[str, list[str]] = field(default_factory=dict)
    safety_lines: list[str] = field(default_factory=list)
    safety_veils: list[str] = field(default_factory=list)
    hero_drafts: dict[str, HeroDraft] = field(default_factory=dict)
    gm_secret_notes: list[str] = field(default_factory=list)
    gm_inspiration_tags: list[str] = field(default_factory=list)
    gm_guidance_notes: list[str] = field(default_factory=list)
    gm_story_beats: list[str] = field(default_factory=list)
    gm_prepared_locations: dict[str, str] = field(default_factory=dict)
    first_act_candidates: list[FirstActCandidate] = field(default_factory=list)
    first_act_votes: dict[str, str] = field(default_factory=dict)
    selected_first_act_id: str = ""
    selected_first_act_summary: str = ""
    first_act_questions: list[str] = field(default_factory=list)
    first_act_question_answers: dict[str, list[str]] = field(default_factory=dict)
    first_act_skipped_questions: list[str] = field(default_factory=list)
    first_act_opening_equipment_restrictions: list[dict[str, Any]] = field(
        default_factory=list
    )
    starting_bond_suggestions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    pending_proposals: list[dict[str, Any]] = field(default_factory=list)
    custom_world_settings: dict[str, str] = field(default_factory=dict)
    gm_private_world_settings: dict[str, dict[str, Any]] = field(default_factory=dict)
    world_setting_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    world_setting_audit_log: list[dict[str, Any]] = field(default_factory=list)
    world_setting_revision: int = 0
    completed: bool = False


@dataclass
class SessionZeroTurn:
    speaker: str
    message: str
    stage: SessionZeroStage
    accepted_facts: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)


@dataclass
class SessionZeroParticipant:
    name: str
    role: str = "玩家"
    contributions: list[str] = field(default_factory=list)
    answered_topics: list[str] = field(default_factory=list)
    pending_question: str = ""
    proactive_questions_enabled: bool = True


@dataclass
class SessionZeroState:
    active: bool = False
    stage: SessionZeroStage = SessionZeroStage.TONE
    gm_style: GMStyleProfile = field(default_factory=GMStyleProfile)
    world: WorldCreationProfile = field(default_factory=WorldCreationProfile)
    transcript: list[SessionZeroTurn] = field(default_factory=list)
    participants: list[SessionZeroParticipant] = field(default_factory=list)
    current_participant_index: int = 0
    polling_round: int = 0
    proactive_pause: dict[str, Any] = field(default_factory=dict)
    chapter_one_transition: dict[str, Any] = field(default_factory=dict)
    prepared_chapter_one_session: PreparedSessionContractCache | None = None

    def current_participant(self) -> SessionZeroParticipant | None:
        if not self.participants:
            return None
        return self.participants[self.current_participant_index % len(self.participants)]


@dataclass
class SessionZeroResponse:
    message: str
    stage: SessionZeroStage
    action: str = "reply"
    accepted_facts: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    world_updates: dict[str, Any] = field(default_factory=dict)


@dataclass
class HeroCreationProfile:
    player_name: str
    hero_name: str
    identity: str
    theme: str
    origin: str
    classes: dict[str, int]
    attributes: dict[str, int]
    bonds: list[Bond] = field(default_factory=list)
    skills: dict[str, int] = field(default_factory=dict)
    skill_options: dict[str, list[str]] = field(default_factory=dict)
    spells: list[str] = field(default_factory=list)
    bound_arcana: list[str] = field(default_factory=list)
    abilities: list[str] = field(default_factory=list)
    equipment: list[str] = field(default_factory=list)
    equipment_slots: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class HeroDraftValidationResult:
    draft_key: str
    ready: bool
    missing_fields: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unresolved_skill_choices: list[dict[str, Any]] = field(default_factory=list)
    profile: HeroCreationProfile | None = None


@dataclass
class CharacterCreationResult:
    character: Character
    applied_benefits: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    next_questions: list[str] = field(default_factory=list)
    equipment_cost: int = 0
    fate_roll: tuple[int, int] = (0, 0)
    starting_zenit: int = 0


@dataclass
class PartyMemberEntry:
    player_name: str
    hero_name: str
    identity: str
    theme: str
    origin: str
    classes: dict[str, int]
    skills: dict[str, int] = field(default_factory=dict)
    skill_options: dict[str, list[str]] = field(default_factory=dict)
    equipment: list[str] = field(default_factory=list)
    zenit: int = 0
    bonds: list[str] = field(default_factory=list)


@dataclass
class PartySheet:
    group_concept: str = ""
    shared_goal: str = ""
    starting_region: str = ""
    members: list[PartyMemberEntry] = field(default_factory=list)
    party_notes: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)


@dataclass
class WorldSheet:
    campaign_title: str = ""
    continent_name: str = ""
    world_style: str = ""
    pillars: dict[str, str] = field(default_factory=dict)
    core_themes: list[str] = field(default_factory=list)
    starting_region: str = ""
    major_locations: dict[str, str] = field(default_factory=dict)
    factions: dict[str, str] = field(default_factory=dict)
    villain_seeds: list[str] = field(default_factory=list)
    villain_mirrors: list[str] = field(default_factory=list)
    mysteries: list[str] = field(default_factory=list)
    selected_first_act: str = ""
    starting_bond_suggestions: list[str] = field(default_factory=list)
    persistent_changes: list[str] = field(default_factory=list)
    created_assets: list[str] = field(default_factory=list)
    location_facilities: dict[str, list[str]] = field(default_factory=dict)
    safety_lines: list[str] = field(default_factory=list)
    safety_veils: list[str] = field(default_factory=list)


@dataclass
class SafetyDeclarationResult:
    declaration_type: str
    item: str
    speaker: str = ""
    anonymous: bool = False
    accepted: bool = True
    message: str = ""
    guidance: str = ""


@dataclass
class CampaignCreationBundle:
    world_sheet: WorldSheet
    party_sheet: PartySheet
    characters: list[Character] = field(default_factory=list)


@dataclass
class SheetExportBundle:
    world_markdown: str
    party_markdown: str
    character_markdowns: dict[str, str] = field(default_factory=dict)
    json_payload: dict[str, Any] = field(default_factory=dict)
    written_files: dict[str, str] = field(default_factory=dict)


@dataclass
class EscalationStage:
    name: str
    ultima_points: int
    # ``villain_upgrade`` follows the Ultima-point villain advancement rules.
    # ``boss_phase`` is an encounter phase change: it restores the form but
    # does not award Fabula Points or replenish Ultima Points.
    transition_kind: str = "villain_upgrade"
    preparation_round: bool = False
    hp_restore: int | None = None
    mp_restore: int | None = None
    added_statuses: list[StatusEffect] = field(default_factory=list)
    affinity_changes: dict[str, Affinity] = field(default_factory=dict)
    added_abilities: list[str] = field(default_factory=list)
    added_spells: list[str] = field(default_factory=list)
    action_count: int | None = None
    preferred_actions: list[str] = field(default_factory=list)
    tactic_hints: list[str] = field(default_factory=list)
    public_cue: str = ""
    note: str = ""


@dataclass
class TimedEffect:
    owner: str
    effect_type: str
    expires_on: EffectTiming
    target: str | None = None
    source: str = ""
    effect_key: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    note: str = ""


@dataclass(frozen=True)
class SpellDefinition:
    name: str
    mp_cost: int
    target: SpellTarget
    effect_type: SpellEffectType
    attributes: list[str]
    requires_check: bool = False
    duration: EffectTiming | None = None
    fixed_damage: int = 0
    damage_type: str = "arcane"
    defense_type: str = "magic"
    description: str = ""
    status_effect: StatusEffect | None = None
    selectable_statuses: tuple[StatusEffect, ...] = ()
    selectable_status_count: int = 1
    selectable_damage_types: tuple[str, ...] = ()
    selectable_attributes: tuple[str, ...] = ()
    affinity_changes: dict[str, Affinity] = field(default_factory=dict)
    defense_bonus: dict[str, int] = field(default_factory=dict)
    defense_floor: dict[str, int] = field(default_factory=dict)
    status_immunities: tuple[StatusEffect, ...] = ()
    attribute_bonus: dict[str, int] = field(default_factory=dict)
    weapon_damage_type: str | None = None
    clear_all_statuses: bool = False
    ignore_resist: bool = False
    drain_to: str | None = None
    drain_requires_target_above_zero: bool = True
    extra_actions: int = 0
    survive_at_one: bool = False
    automatic_effect: bool = False
    fixed_damage_only: bool = False
    apply_status_on_success: bool = False
    check_bonus: int = 0
    incoming_damage_bonus: int = 0
    immediate_attack: bool = False
    opportunity_turn_penalty: int = 0
    opportunity_ground_flying: bool = False
    mp_cost_per_target: bool = True
    minimum_level: int = 0
    allowed_npc_ranks: tuple[str, ...] = ()
    npc_last_turn_only: bool = False
    resource_fraction_loss: float = 0.0
    clear_selected_status: bool = False


@dataclass
class ConflictState:
    active: bool = False
    scene_name: str = ""
    # A conflict may temporarily take over an existing free or dungeon scene.
    # Persist the parent identity through initiative windows and save/load so
    # ending combat can return to that exact scene instead of manufacturing a
    # generic standard scene.
    parent_scene_id: str = ""
    parent_scene_name: str = ""
    parent_scene_type: str = ""
    parent_scene_objective: str = ""
    parent_scene_summary: str = ""
    round_number: int = 0
    turn_order: list[str] = field(default_factory=list)
    # Explicit sides are authoritative for full-turn allied NPCs. Traits
    # remain a legacy fallback for old snapshots created before these fields.
    player_side: list[str] = field(default_factory=list)
    enemy_side: list[str] = field(default_factory=list)
    current_turn_index: int = 0
    current_bonus_actor: str | None = None
    # Preserve why the currently executing bonus turn exists. Rank turns obey
    # the enemy multi-turn alternation rule; ordinary bonus turns keep their
    # own immediate timing.
    current_bonus_kind: str | None = None
    queued_turns: list[str] = field(default_factory=list)
    # Kept parallel to ``queued_turns``. ``rank`` actions must alternate with
    # the player side while an unacted PC remains; ordinary bonus actions may
    # have their own immediate timing.
    queued_turn_kinds: list[str] = field(default_factory=list)
    turn_started_actor: str | None = None
    # Removing the acting base combatant already moves ``current_turn_index``
    # onto its successor. The normal end-turn increment must then be skipped
    # once or that successor would lose its turn.
    current_base_actor_removed: bool = False
    # Removing the final base slot skips the normal index increment, but still
    # needs to close that action round exactly once.
    current_base_actor_removed_ended_round: bool = False
    # End-of-turn effects may require a persisted choice before initiative
    # can advance. This survives save/load so the benefit is never skipped.
    pending_turn_end_actor: str | None = None
    turn_serial: int = 0
    acted_this_round: list[str] = field(default_factory=list)
    auto_advance_skip_names_this_round: list[str] = field(default_factory=list)
    pending_assists: dict[str, list[str]] = field(default_factory=dict)
    held_actions: list[dict[str, Any]] = field(default_factory=list)
    ultima_points: dict[str, int] = field(default_factory=dict)
    exalted_enemies: set[str] = field(default_factory=set)
    enemy_ranks: dict[str, EnemyRank] = field(default_factory=dict)
    villains: set[str] = field(default_factory=set)
    villain_appearance_awarded: set[str] = field(default_factory=set)
    enemy_action_counts: dict[str, int] = field(default_factory=dict)
    action_penalties: dict[str, int] = field(default_factory=dict)
    escalation_stages: dict[str, list[EscalationStage]] = field(default_factory=dict)
    current_escalation_stage: dict[str, int] = field(default_factory=dict)
    escaped_combatants: set[str] = field(default_factory=set)
    # Successful cross-scene movement during a conflict first removes the
    # movers from initiative, then lands them after the conflict closes.  The
    # deferred records keep those two state changes in one recoverable
    # transaction instead of switching the focused scene underneath combat.
    pending_exit_transitions: list[dict[str, Any]] = field(default_factory=list)
    surrendered_combatants: set[str] = field(default_factory=set)
    defeated_combatants: set[str] = field(default_factory=set)
    sacrifices: set[str] = field(default_factory=set)
    # ``fallen_pcs`` is the active unconscious state created by giving up
    # resistance. It deliberately survives conflict teardown until the next
    # scene in which that PC participates.
    fallen_pcs: dict[str, str] = field(default_factory=dict)
    # Defeat consequences remain campaign facts after the PC wakes up or is
    # restored by 重燃希望. A PC may accumulate more than one consequence.
    pc_defeat_consequences: dict[str, list[str]] = field(default_factory=dict)
    # Ordinary NPCs reduced to zero HP are not automatically killed. The player
    # who dealt the final blow chooses their fate, which remains campaign state.
    defeated_npc_fates: dict[str, str] = field(default_factory=dict)
    incapacitated_combatants: dict[str, str] = field(default_factory=dict)
    swallowed_targets: dict[str, SwallowedTargetState] = field(default_factory=dict)
    active_statuses: dict[str, list[StatusEffect]] = field(default_factory=dict)
    active_effects: list[TimedEffect] = field(default_factory=list)
    passive_survival_used: set[str] = field(default_factory=set)
    combat_log: list[CombatLogEntry] = field(default_factory=list)
    pending_decisions: list[dict[str, Any]] = field(default_factory=list)

    def current_actor(self) -> str | None:
        if self.current_bonus_actor is not None:
            return self.current_bonus_actor
        if not self.turn_order:
            return None
        return self.turn_order[self.current_turn_index % len(self.turn_order)]


@dataclass
class Action:
    action_type: ActionType
    parameters: dict[str, Any]


@dataclass
class CombatLogEntry:
    round_number: int
    actor: str
    event_type: str
    summary: str


@dataclass
class RollOutcome:
    actor: str
    attributes: list[str]
    dice: list[tuple[int, int]]
    total: int
    modifier: int
    high_roll: int
    target_number: int
    success: bool
    critical_success: bool
    fumble: bool
    opportunity_count: int = 0
    margin: int = 0
    target: str | None = None
    reason: str = ""
    damage: int = 0
    damage_type: str = "physical"
    applied_affinity: Affinity = Affinity.NORMAL
    hp_after: int | None = None


@dataclass
class ResourceChange:
    target: str
    resource: str
    amount: int
    before: int
    after: int
    reason: str = ""


@dataclass
class ClockChange:
    clock_name: str
    before: int
    after: int
    delta: int
    max_segments: int
    reason: str = ""
    clock_type: str = ""
    stakes: str = ""
    completion_consequence: str = ""


@dataclass
class ActionResolution:
    action: Action
    rules_text: str
    payload: dict[str, Any]


@dataclass
class ConflictEvent:
    target: str
    event_type: str
    summary: str
    ultima_spent: int = 0
    fabula_awarded: int = 0
    stage_name: str = ""
    consequence: str = ""
    statuses_cleared: bool = False
    hp_after: int | None = None
    mp_after: int | None = None


@dataclass
class NPCPersona:
    name: str
    npc_id: str = ""
    # Scene startup may need a stable identity before the GM has authored the
    # NPC's actual motives and voice.  Such records stay explicit placeholders
    # so a later profile tool can enrich them without overwriting established
    # continuity.
    profile_status: str = "established"
    # A collective is a persistent speaking actor such as a patrol, council or
    # crowd.  It may hold a shared stance and memory, but it is not permission
    # to invent a named leader or treat every member as one individual.
    entity_kind: str = "individual"
    aliases: list[str] = field(default_factory=list)
    public_identity: str = ""
    role_in_story: str = ""
    core_drive: str = ""
    manner: str = ""
    speech_style: str = ""
    combat_style: str = ""
    # Four concise traits are the NPC counterpart to a PC's Identity/Theme/
    # Origin and may be invoked by villains for rerolls (core rules p.302).
    traits: list[str] = field(default_factory=list)
    npc_rank: str = "minor"
    leverage: str = ""
    authority_scope: str = ""
    knowledge_scope: str = ""
    refusal_move: str = ""
    known_skills: list[str] = field(default_factory=list)
    combat_actions: list[str] = field(default_factory=list)
    first_scene: str = ""
    goals: list[str] = field(default_factory=list)
    taboos: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    memories: list[str] = field(default_factory=list)
    custom_prompt: str = ""
    current_location: str = ""
    current_mood: str = ""
    current_stance: str = ""
    active_goal: str = ""
    completed_goals: list[str] = field(default_factory=list)
    relationships: dict[str, str] = field(default_factory=dict)
    last_seen_scene: str = ""
    status: str = "active"
    voice_examples: list[str] = field(default_factory=list)
    memory_records: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SupportOutcome:
    supporter: str
    roll: RollOutcome
    bonus: int


@dataclass
class TeamCheckOutcome:
    leader: str
    attributes: list[str]
    leader_roll: RollOutcome
    support_outcomes: list[SupportOutcome]
    support_bonus: int
    final_total: int
    target_number: int
    success: bool


@dataclass
class PendingCheckBatch:
    """Persisted multi-part check whose final consequence waits for all rolls."""

    batch_id: str
    kind: str
    source_action_type: str
    source_parameters: dict[str, Any]
    actor_order: list[str]
    roles: dict[str, str] = field(default_factory=dict)
    rolls: dict[str, RollOutcome] = field(default_factory=dict)
    roll_history: list[dict[str, RollOutcome]] = field(default_factory=list)
    published_roll_actors: list[str] = field(default_factory=list)
    status: str = "pending"
    result: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    completed_at: str = ""


@dataclass
class OpposedCheckOutcome:
    left: str
    right: str
    attributes: list[str]
    left_roll: RollOutcome
    right_roll: RollOutcome
    winner: str
    attempts: int


@dataclass
class GamePanel:
    game_phase: str
    active_clocks: list[str]
    pc_status: list[str]
    enemy_status: list[str]
    recent_chat: str
    current_actor: str | None = None
    table_status: list[str] = field(default_factory=list)
    safety_guidance: str = ""
    optional_rules_guidance: str = ""
    retrieved_public_memory: list[str] = field(default_factory=list)
    gm_private_memory: list[str] = field(default_factory=list)
    memory_guidance: str = ""
