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


class MemoryVisibility(str, Enum):
    PUBLIC = "public"
    PRIVATE = "private"


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


@dataclass
class Bond:
    target: str
    emotions: list[str] = field(default_factory=list)

    @property
    def strength(self) -> int:
        return min(3, max(0, len(self.emotions)))


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
    experience_points: int = 0
    hero_skills: list[str] = field(default_factory=list)
    bound_arcana: list[str] = field(default_factory=list)
    active_arcanum: str = ""
    max_inventory_points: int = 0
    zenit: int = 0
    equipment: list[str] = field(default_factory=list)
    equipment_templates: dict[str, str] = field(default_factory=dict)
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
    trigger_cooldowns: set[str] = field(default_factory=set)

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


@dataclass
class SceneRecord:
    name: str
    scene_type: SceneType
    location: str = ""
    participants: list[str] = field(default_factory=list)
    objective: str = ""
    summary: str = ""
    active: bool = True


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
    draw_icon: bool | None = None
    icon_id: str = ""
    threat_level: TravelThreatLevel = TravelThreatLevel.MEDIUM
    route_type: TravelRouteType = TravelRouteType.LAND
    faction: str = ""
    discovered: bool = True
    tags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


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
    transcript_path: str = ""
    transcript_txt_path: str = ""
    summary_path: str = ""
    memory_path: str = ""


class StoryArcPhase(str, Enum):
    OPENING = "opening"
    RISING = "rising"
    MIDPOINT = "midpoint"
    CRISIS = "crisis"
    FINALE = "finale"


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
    table_manner: str = "像 ACG 社团里带团的同桌 GM，先接住玩家想法，再给出两到三个可选方向。"


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
    classes: dict[str, int] = field(default_factory=dict)
    attributes: dict[str, int] = field(default_factory=dict)
    bonds: list[str] = field(default_factory=list)
    skills: dict[str, int] = field(default_factory=dict)
    spells: list[str] = field(default_factory=list)
    bound_arcana: list[str] = field(default_factory=list)
    equipment: list[str] = field(default_factory=list)
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
    starting_bond_suggestions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
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

    def current_participant(self) -> SessionZeroParticipant | None:
        if not self.participants:
            return None
        return self.participants[self.current_participant_index % len(self.participants)]


@dataclass
class SessionZeroResponse:
    message: str
    stage: SessionZeroStage
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
    spells: list[str] = field(default_factory=list)
    bound_arcana: list[str] = field(default_factory=list)
    abilities: list[str] = field(default_factory=list)
    equipment: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class HeroDraftValidationResult:
    draft_key: str
    ready: bool
    missing_fields: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
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
    extra_actions: int = 0
    survive_at_one: bool = False


@dataclass
class ConflictState:
    active: bool = False
    scene_name: str = ""
    round_number: int = 0
    turn_order: list[str] = field(default_factory=list)
    current_turn_index: int = 0
    current_bonus_actor: str | None = None
    queued_turns: list[str] = field(default_factory=list)
    turn_started_actor: str | None = None
    acted_this_round: list[str] = field(default_factory=list)
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
    surrendered_combatants: set[str] = field(default_factory=set)
    defeated_combatants: set[str] = field(default_factory=set)
    sacrifices: set[str] = field(default_factory=set)
    fallen_pcs: dict[str, str] = field(default_factory=dict)
    active_statuses: dict[str, list[StatusEffect]] = field(default_factory=dict)
    active_effects: list[TimedEffect] = field(default_factory=list)
    passive_survival_used: set[str] = field(default_factory=set)
    combat_log: list[CombatLogEntry] = field(default_factory=list)

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
    public_identity: str = ""
    role_in_story: str = ""
    core_drive: str = ""
    manner: str = ""
    speech_style: str = ""
    combat_style: str = ""
    first_scene: str = ""
    goals: list[str] = field(default_factory=list)
    taboos: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    memories: list[str] = field(default_factory=list)
    custom_prompt: str = ""


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
    retrieved_public_memory: list[str] = field(default_factory=list)
    gm_private_memory: list[str] = field(default_factory=list)
    memory_guidance: str = ""
