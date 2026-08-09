import tempfile
import unittest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.dungeon_manager import DungeonManager
from fu_gm.components.memory_store import CampaignMemoryStore
from fu_gm.components.project_manager import ProjectManager
from fu_gm.components.progression_manager import ProgressionManager
from fu_gm.components.ritual_manager import RitualManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.scene_frame_manager import SceneFrame, SceneFrameManager
from fu_gm.components.session_zero_manager import SessionZeroManager
from fu_gm.components.story_arc_manager import StoryArcManager
from fu_gm.components.travel_manager import TravelManager
from fu_gm.components.world_map_manager import WorldMapManager
from fu_gm.components.world_state import WorldState
from fu_gm.models import (
    Affinity,
    ChapterPackage,
    ChapterPackageScene,
    Character,
    Clock,
    DungeonArea,
    DungeonAreaType,
    DungeonDesignBrief,
    DungeonExploreMode,
    DungeonImportance,
    DungeonMap,
    DungeonPreparation,
    DungeonState,
    HeroDraft,
    MemoryVisibility,
    NPCAbilityProfile,
    NPCCombatBlueprint,
    NPCAttackProfile,
    ProjectState,
    ProjectUse,
    RitualDiscipline,
    RitualPlan,
    RitualPotency,
    RitualScope,
    SceneType,
    SessionEpisodeProgress,
    SessionSceneProgress,
    SecretLockLevel,
    StatusEffect,
    SwallowedTargetState,
    TravelRouteType,
    TravelThreatLevel,
    WorldRoutePlan,
)


class MemoryStoreTests(unittest.TestCase):
    def test_persistent_npc_conditions_roundtrip_with_active_conflict(self) -> None:
        characters = CharacterManager()
        for name, traits in (
            ("陷龙花", ["enemy", "植物"]),
            ("探险者", ["pc"]),
            ("石化同伴", ["pc"]),
        ):
            characters.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                    max_hp=80,
                    hp=80,
                    max_mp=40,
                    mp=40,
                    traits=traits,
                )
            )
        characters.get("石化同伴").special_conditions["petrified"] = (
            "石化啄击造成的持续石化"
        )
        clocks = ClockManager()
        escape_clock = "脱离【陷龙花】的吞噬（探险者）"
        clocks.add(
            Clock(
                name=escape_clock,
                max_segments=4,
                current=2,
                clock_type="objective",
                scope="scene",
                owner="探险者",
                source="陷龙花",
            )
        )
        conflict = ConflictManager(characters)
        conflict.start_scene(
            "食人花腹",
            ["陷龙花", "探险者", "石化同伴"],
            player_side=["探险者", "石化同伴"],
            enemy_side=["陷龙花"],
        )
        conflict.state.incapacitated_combatants["石化同伴"] = "石化"
        conflict.state.swallowed_targets["探险者"] = SwallowedTargetState(
            source="陷龙花",
            target="探险者",
            escape_clock=escape_clock,
            damage=20,
            damage_type="physical",
            created_round=2,
        )
        world = WorldState()

        with tempfile.TemporaryDirectory() as tmpdir:
            store = CampaignMemoryStore(tmpdir)
            store.save_campaign(
                "NPC持续状态",
                world_state=world,
                character_manager=characters,
                clock_manager=clocks,
                conflict_manager=conflict,
            )
            loaded_characters = CharacterManager()
            loaded_clocks = ClockManager()
            loaded_conflict = ConflictManager(loaded_characters)
            store.load_campaign(
                "NPC持续状态",
                world_state=WorldState(),
                character_manager=loaded_characters,
                clock_manager=loaded_clocks,
                conflict_manager=loaded_conflict,
            )

        swallowed = loaded_conflict.state.swallowed_targets["探险者"]
        self.assertIsInstance(swallowed, SwallowedTargetState)
        self.assertEqual(swallowed.source, "陷龙花")
        self.assertEqual(swallowed.created_round, 2)
        self.assertEqual(loaded_clocks.get(escape_clock).current, 2)
        self.assertEqual(
            loaded_characters.get("石化同伴").special_conditions["petrified"],
            "石化啄击造成的持续石化",
        )

    def test_npc_blueprint_dynamic_ability_roundtrips_as_typed_state(self) -> None:
        world = WorldState()
        world.npc_combat_blueprints["爆燃魔偶"] = NPCCombatBlueprint(
            blueprint_id="blueprint-one",
            npc_name="爆燃魔偶",
            source_template="爆炎元素",
            attributes={"DEX": 8, "INS": 6, "MIG": 8, "WLP": 10},
            max_hp=60,
            crisis_threshold=30,
            max_mp=60,
            defenses={"physical": 9, "magic": 8},
            affinities={"fire": Affinity.ABSORB, "ice": Affinity.WEAK},
            attacks=[
                NPCAttackProfile(
                    attack_id="flame-stream",
                    name="火焰射流",
                    attributes=["DEX", "WLP"],
                    damage_bonus=10,
                    damage_type="fire",
                )
            ],
            ability_profiles=[
                NPCAbilityProfile(
                    ability_id="explosion",
                    name="引爆",
                    source_skill="最后一搏",
                    trigger="zero_hp",
                    effect_type="fixed_damage",
                    target_scope="all_creatures",
                    amount=10,
                    damage_type="fire",
                    once_per_scene=True,
                )
            ],
        )
        characters = CharacterManager()
        clocks = ClockManager()
        conflict = ConflictManager(characters)

        with tempfile.TemporaryDirectory() as tmpdir:
            store = CampaignMemoryStore(tmpdir)
            store.save_campaign(
                "NPC蓝图存档",
                world_state=world,
                character_manager=characters,
                clock_manager=clocks,
                conflict_manager=conflict,
            )
            loaded_world = WorldState()
            store.load_campaign(
                "NPC蓝图存档",
                world_state=loaded_world,
                character_manager=CharacterManager(),
                clock_manager=ClockManager(),
                conflict_manager=ConflictManager(CharacterManager()),
            )

        loaded = loaded_world.npc_combat_blueprints["爆燃魔偶"]
        self.assertIsInstance(loaded, NPCCombatBlueprint)
        self.assertIsInstance(loaded.attacks[0], NPCAttackProfile)
        self.assertIsInstance(loaded.ability_profiles[0], NPCAbilityProfile)
        self.assertIs(loaded.affinities["fire"], Affinity.ABSORB)
        self.assertEqual(loaded.ability_profiles[0].statuses, [])

    def test_adventure_runtime_roundtrips_travel_dungeon_routes_and_rng(self) -> None:
        world = WorldState()
        characters = CharacterManager()
        characters.add(
            Character(
                name="诺艾尔",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                max_hp=45,
                hp=45,
                max_mp=45,
                mp=45,
                level=6,
                experience_points=15,
                traits=["pc"],
            )
        )
        progression = ProgressionManager(characters, world)
        progression._leveled_this_session.add("诺艾尔")
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        rules = RulesEngine(seed=37)
        travel = TravelManager(rules)
        travel.register_owned_transport("地面坐骑")
        journey = travel.travel(
            origin="托伦",
            destination="星落尖塔",
            threat_levels=[TravelThreatLevel.LOW],
            regions=["旧战场"],
        )
        active_journey = travel.begin_journey(
            journey_id="前往藤心村",
            origin="星落尖塔",
            destination="藤心村",
            threat_levels=[
                TravelThreatLevel.LOW,
                TravelThreatLevel.MEDIUM,
            ],
            regions=["复苏荒原", "藤蔓谷"],
            party_names=["诺艾尔"],
        )
        dungeon = DungeonManager(clocks, rules)
        dungeon.state = DungeonState(
            name="钢铁墓园",
            mode=DungeonExploreMode.DETAILED,
            active=True,
            current_area="齿轮门厅",
            areas=[
                DungeonArea(
                    name="齿轮门厅",
                    area_type=DungeonAreaType.PASSAGE,
                    description="仍在转动的门厅。",
                    discovered=True,
                )
            ],
        )
        dungeon.history = [
            DungeonState(
                name="旧矿井",
                mode=DungeonExploreMode.SCENE,
                active=False,
            )
        ]
        dungeon.design_history = [
            DungeonDesignBrief(
                name="钢铁墓园",
                importance=DungeonImportance.MAJOR,
                preparation=DungeonPreparation.PREPARED,
                recommended_mode=DungeonExploreMode.DETAILED,
                concept="会做梦的战争机械墓园",
                focus="失踪的灵魂",
                inhabitants="钢铁生命",
                peculiarity="藤蔓在齿轮间低语",
            )
        ]
        dungeon.maps = {
            "钢铁墓园": DungeonMap(
                dungeon_name="钢铁墓园",
                areas=list(dungeon.state.areas),
                entrance="齿轮门厅",
            )
        }
        world_map = WorldMapManager(world)
        world_map.route_plans = [
            WorldRoutePlan(
                origin="托伦",
                destination="星落尖塔",
                distance=2,
                travel_days=1,
                route_type=TravelRouteType.LAND,
                transport="地面坐骑",
                travel_multiplier=2,
                service_cost=0,
                threat_levels=[TravelThreatLevel.LOW],
                regions=["旧战场"],
            )
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = CampaignMemoryStore(tmpdir)
            store.save_campaign(
                "冒险中途存档",
                world_state=world,
                character_manager=characters,
                clock_manager=clocks,
                conflict_manager=conflict,
                travel_manager=travel,
                dungeon_manager=dungeon,
                world_map_manager=world_map,
                rules_engine=rules,
                progression_manager=progression,
            )
            expected_next_roll = rules.roll_die(12)

            loaded_world = WorldState()
            loaded_characters = CharacterManager()
            loaded_clocks = ClockManager()
            loaded_conflict = ConflictManager(loaded_characters)
            loaded_rules = RulesEngine(seed=999)
            loaded_travel = TravelManager(loaded_rules)
            loaded_dungeon = DungeonManager(loaded_clocks, loaded_rules)
            loaded_world_map = WorldMapManager(loaded_world)
            loaded_progression = ProgressionManager(
                loaded_characters,
                loaded_world,
            )
            store.load_campaign(
                "冒险中途存档",
                world_state=loaded_world,
                character_manager=loaded_characters,
                clock_manager=loaded_clocks,
                conflict_manager=loaded_conflict,
                travel_manager=loaded_travel,
                dungeon_manager=loaded_dungeon,
                world_map_manager=loaded_world_map,
                rules_engine=loaded_rules,
                progression_manager=loaded_progression,
            )

        self.assertEqual(loaded_travel.last_journey, journey)
        self.assertEqual(loaded_travel.history, [journey])
        self.assertIsNotNone(loaded_travel.active_journey)
        self.assertEqual(
            loaded_travel.active_journey.journey_id,
            active_journey.journey_id,
        )
        self.assertEqual(loaded_travel.active_journey.current_day, 0)
        self.assertEqual(
            loaded_travel.active_journey.threat_levels,
            [TravelThreatLevel.LOW, TravelThreatLevel.MEDIUM],
        )
        self.assertIn("托伦->星落尖塔", loaded_travel.routes)
        self.assertEqual(loaded_travel.owned_transports, {"地面坐骑"})
        self.assertEqual(loaded_dungeon.state.current_area, "齿轮门厅")
        self.assertEqual(loaded_dungeon.history[0].name, "旧矿井")
        self.assertEqual(loaded_dungeon.design_history[0].name, "钢铁墓园")
        self.assertEqual(loaded_dungeon.maps["钢铁墓园"].entrance, "齿轮门厅")
        self.assertEqual(loaded_world_map.route_plans[0].regions, ["旧战场"])
        self.assertEqual(loaded_rules.roll_die(12), expected_next_roll)
        self.assertFalse(loaded_progression.can_level_up("诺艾尔"))

    def test_semantic_map_roundtrips_with_campaign_snapshot(self) -> None:
        world = WorldState()
        world.upsert_map_location(
            "星落尖塔",
            feature_type="landmark",
            semantic_cell="P03",
        )
        world.semantic_map.terrain_rows = [
            "~" * 20,
            "~" + "C" * 18 + "~",
            *(["~C" + "." * 16 + "C~"] * 8),
            "~" + "C" * 18 + "~",
            "~" * 20,
        ]
        world.semantic_map.location_cells = {"星落尖塔": "P03"}
        world.semantic_map.source = "gm_semantic_placement"
        world.semantic_map.revision = 3
        characters = CharacterManager()
        clocks = ClockManager()
        conflict = ConflictManager(characters)

        with tempfile.TemporaryDirectory() as tmpdir:
            store = CampaignMemoryStore(tmpdir)
            store.save_campaign(
                "语义地图存档",
                world_state=world,
                character_manager=characters,
                clock_manager=clocks,
                conflict_manager=conflict,
            )
            loaded_world = WorldState()
            loaded_characters = CharacterManager()
            loaded_clocks = ClockManager()
            loaded_conflict = ConflictManager(loaded_characters)
            store.load_campaign(
                "语义地图存档",
                world_state=loaded_world,
                character_manager=loaded_characters,
                clock_manager=loaded_clocks,
                conflict_manager=loaded_conflict,
            )

        self.assertEqual(
            loaded_world.semantic_map.location_cells["星落尖塔"],
            "P03",
        )
        self.assertEqual(loaded_world.semantic_map.revision, 3)
        self.assertEqual(
            loaded_world.map_locations["星落尖塔"].semantic_cell,
            "P03",
        )

    def test_session_zero_workflow_roundtrips_with_campaign_snapshot(self) -> None:
        world = WorldState()
        session_zero = SessionZeroManager(world)
        session_zero.start(participants=["阿凛", "南星"])
        session_zero.set_proactive_questions_enabled("南星", False)
        session_zero.pause_proactive_nudges(
            "阿凛",
            topic="第一幕开端",
            evidence="让我想想。",
        )
        session_zero.observe_table_talk("阿凛", "我想从边境驿站开始。")
        session_zero.state.world.continent_name = "白钟大陆"
        characters = CharacterManager()
        clocks = ClockManager()
        conflict = ConflictManager(characters)

        with tempfile.TemporaryDirectory() as tmpdir:
            store = CampaignMemoryStore(tmpdir)
            store.save_campaign(
                "第零章存档",
                world_state=world,
                character_manager=characters,
                clock_manager=clocks,
                conflict_manager=conflict,
                session_zero_manager=session_zero,
            )

            loaded_world = WorldState()
            loaded_session_zero = SessionZeroManager(loaded_world)
            loaded_characters = CharacterManager()
            loaded_clocks = ClockManager()
            loaded_conflict = ConflictManager(loaded_characters)
            store.load_campaign(
                "第零章存档",
                world_state=loaded_world,
                character_manager=loaded_characters,
                clock_manager=loaded_clocks,
                conflict_manager=loaded_conflict,
                session_zero_manager=loaded_session_zero,
            )

        self.assertTrue(loaded_session_zero.state.active)
        self.assertEqual(
            [participant.name for participant in loaded_session_zero.state.participants],
            ["阿凛", "南星"],
        )
        self.assertEqual(loaded_session_zero.state.transcript[-1].message, "我想从边境驿站开始。")
        self.assertFalse(
            loaded_session_zero.find_participant("南星").proactive_questions_enabled
        )
        self.assertEqual(
            loaded_session_zero.state.proactive_pause["topic"],
            "第一幕开端",
        )
        self.assertIs(loaded_session_zero.state.world, loaded_world.world_profile)
        self.assertEqual(loaded_world.world_profile.continent_name, "白钟大陆")

    def test_ready_chapter_one_transition_roundtrips_with_snapshot(self) -> None:
        world = WorldState()
        session_zero = SessionZeroManager(world)
        session_zero.start(participants=["阿凛"])
        profile = session_zero.state.world
        profile.map_card = "自定义地图"
        profile.magic_tech_role = "魔法与科技彼此对立。"
        profile.kingdoms = {"索朗帝国": "旧蒸汽帝国。"}
        profile.historical_events = ["机械战争。"]
        profile.mysteries = ["重叠日。"]
        profile.world_threats = ["钢铁生命失控。"]
        profile.group_concept = "越狱同行者"
        profile.safety_lines = ["不出现性暴力"]
        profile.selected_first_act_summary = "从卡里巴村监狱越狱。"
        participant = session_zero.find_participant("阿凛")
        participant.answered_topics.extend(
            [
                "kingdom_contributions",
                "historical_event_contributions",
                "mystery_contributions",
                "threat_contributions",
            ]
        )
        profile.hero_drafts["阿凛"] = HeroDraft(
            player_name="阿凛",
            hero_name="伊莉雅",
            identity="出逃的魔导工匠",
            theme="希望",
            origin="第七采掘城",
            classes={"造物使": 3, "武器大师": 2},
            attributes={"敏捷": 8, "洞察": 10, "力量": 8, "意志": 6},
            skills={
                "便携装置": 1,
                "秘密配方": 1,
                "先见之明": 1,
                "碎骨": 1,
                "破防打击": 1,
            },
            equipment=["铁锤", "旅行装束"],
            confirmed=True,
        )
        session_zero.refresh_stage_from_state()
        session_zero.set_chapter_one_transition(
            "supplementing",
            speaker="阿凛",
            evidence="我还想补监狱长。",
        )
        characters = CharacterManager()
        clocks = ClockManager()
        conflict = ConflictManager(characters)

        with tempfile.TemporaryDirectory() as tmpdir:
            store = CampaignMemoryStore(tmpdir)
            store.save_campaign(
                "开章衔接存档",
                world_state=world,
                character_manager=characters,
                clock_manager=clocks,
                conflict_manager=conflict,
                session_zero_manager=session_zero,
            )
            loaded_world = WorldState()
            loaded_session_zero = SessionZeroManager(loaded_world)
            loaded_characters = CharacterManager()
            loaded_clocks = ClockManager()
            loaded_conflict = ConflictManager(loaded_characters)
            store.load_campaign(
                "开章衔接存档",
                world_state=loaded_world,
                character_manager=loaded_characters,
                clock_manager=loaded_clocks,
                conflict_manager=loaded_conflict,
                session_zero_manager=loaded_session_zero,
            )

        self.assertEqual(
            loaded_session_zero.state.chapter_one_transition["posture"],
            "supplementing",
        )
        self.assertEqual(
            loaded_session_zero.state.chapter_one_transition["evidence"],
            "我还想补监狱长。",
        )

    def test_legacy_session_zero_snapshot_recovers_active_workflow_from_scene(self) -> None:
        world = WorldState()
        world.world_profile.continent_name = "白钟大陆"
        characters = CharacterManager()
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        scenes = SceneManager()
        scenes.start_scene("Session 0 世界创建", SceneType.SESSION_ZERO)

        with tempfile.TemporaryDirectory() as tmpdir:
            store = CampaignMemoryStore(tmpdir)
            store.save_campaign(
                "旧第零章存档",
                world_state=world,
                character_manager=characters,
                clock_manager=clocks,
                conflict_manager=conflict,
                scene_manager=scenes,
            )

            loaded_world = WorldState()
            loaded_session_zero = SessionZeroManager(loaded_world)
            loaded_characters = CharacterManager()
            loaded_clocks = ClockManager()
            loaded_conflict = ConflictManager(loaded_characters)
            loaded_scenes = SceneManager()
            store.load_campaign(
                "旧第零章存档",
                world_state=loaded_world,
                character_manager=loaded_characters,
                clock_manager=loaded_clocks,
                conflict_manager=loaded_conflict,
                scene_manager=loaded_scenes,
                session_zero_manager=loaded_session_zero,
            )

        self.assertTrue(loaded_session_zero.state.active)
        self.assertIs(loaded_session_zero.state.world, loaded_world.world_profile)
        self.assertEqual(loaded_world.world_profile.continent_name, "白钟大陆")

    def test_load_repairs_legacy_none_npc_without_losing_exchange_memory(self) -> None:
        world = WorldState()
        world.ensure_npc_persona("白花守望会会长")
        world.ensure_npc_persona("none", public_identity="none")
        world.remember_npc_event("none", "会长已经同意开放北侧旧阶。")
        frames = SceneFrameManager()
        frames.current_frame = SceneFrame(
            scene_key="scene-1|风铃廊",
            scene_name="风铃廊问路",
            last_npc_speaker="白花守望会会长",
            settled_exchanges=[
                {
                    "exchange_id": "exchange-0",
                    "npc": "白花守望会会长",
                    "outcome": "accepted",
                    "settled_terms": "接受护持方案并放行北侧旧阶",
                },
                {
                    "exchange_id": "exchange-1",
                    "npc": "none",
                    "outcome": "accepted",
                    "settled_terms": "开放北侧旧阶",
                }
            ],
        )
        scenes = SceneManager()
        scenes.start_scene(
            "风铃廊问路",
            SceneType.STANDARD,
            participants=["白花守望会会长", "none"],
        )
        scenes.current_scene.open_conditions.append(
            {"condition_id": "condition-1", "npc": "none", "status": "resolved"}
        )
        characters = CharacterManager()
        clocks = ClockManager()
        conflict = ConflictManager(characters)

        with tempfile.TemporaryDirectory() as tmpdir:
            store = CampaignMemoryStore(tmpdir)
            store.save_campaign(
                "旧存档",
                world_state=world,
                character_manager=characters,
                clock_manager=clocks,
                conflict_manager=conflict,
                scene_manager=scenes,
                scene_frame_manager=frames,
            )

            loaded_world = WorldState()
            loaded_characters = CharacterManager()
            loaded_clocks = ClockManager()
            loaded_conflict = ConflictManager(loaded_characters)
            loaded_scenes = SceneManager()
            loaded_frames = SceneFrameManager()
            store.load_campaign(
                "旧存档",
                world_state=loaded_world,
                character_manager=loaded_characters,
                clock_manager=loaded_clocks,
                conflict_manager=loaded_conflict,
                scene_manager=loaded_scenes,
                scene_frame_manager=loaded_frames,
            )

        self.assertNotIn("none", loaded_world.npc_personas)
        self.assertEqual(loaded_world.resolve_npc_name("none"), "")
        self.assertIn(
            "会长已经同意开放北侧旧阶。",
            loaded_world.npc_personas["白花守望会会长"].memories,
        )
        self.assertEqual(
            loaded_frames.current_frame.settled_exchanges[0]["npc"],
            "白花守望会会长",
        )
        self.assertEqual(len(loaded_frames.current_frame.settled_exchanges), 1)
        self.assertEqual(
            loaded_scenes.current_scene.open_conditions[0]["npc"],
            "白花守望会会长",
        )
        self.assertEqual(
            loaded_scenes.current_scene.participants,
            ["白花守望会会长"],
        )

    def test_scene_participant_normalizer_repairs_legacy_target_bundles(self) -> None:
        participants = SceneManager.normalize_participants(
            [
                "伊莉雅",
                "失名旅人；灰金短斗篷的财团使者",
                "['赛璃', '洛岚']",
            ]
        )

        self.assertEqual(
            participants,
            ["伊莉雅", "失名旅人", "灰金短斗篷的财团使者", "赛璃", "洛岚"],
        )

    def test_scene_transition_resets_fallback_action_round(self) -> None:
        scenes = SceneManager()
        scenes.free_action_round_number = 4
        scenes.free_action_round_required_actors = ["瓦莉亚", "米菈"]
        scenes.free_action_round_acted_actors = ["瓦莉亚"]

        scenes.start_scene("月下营火", SceneType.INTERLUDE)
        scenes.end_scene("队伍启程。")

        self.assertEqual(scenes.free_action_round_number, 1)
        self.assertEqual(scenes.free_action_round_required_actors, [])
        self.assertEqual(scenes.free_action_round_acted_actors, [])

    def test_parallel_scene_and_frame_roundtrip_preserves_camera_branches(self) -> None:
        world = WorldState()
        characters = CharacterManager()
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        scenes = SceneManager()
        frames = SceneFrameManager()
        registration = scenes.start_scene(
            "登记小室",
            SceneType.STANDARD,
            location="白花碑驿站·登记小室",
            participants=["伊莉雅"],
        )
        frames.current_frame = SceneFrame(
            scene_key=f"{registration.scene_id}|登记小室",
            scene_name="登记小室",
            source_scene_id=registration.scene_id,
            location=registration.location,
            public_facts=["伊莉雅正在翻查旧册。"],
        )
        scenes.actor_locations["艾薇娅"] = "白花碑驿站"
        frames.suspend_current_frame()
        branch, _ = scenes.focus_actor_branch(
            "艾薇娅",
            name="白花碑回撤点",
            location="白花碑后方",
        )
        frames.current_frame = SceneFrame(
            scene_key=f"{branch.scene_id}|白花碑回撤点",
            scene_name="白花碑回撤点",
            source_scene_id=branch.scene_id,
            location=branch.location,
            public_facts=["艾薇娅守在回撤标记旁。"],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            store = CampaignMemoryStore(tmpdir)
            store.save_campaign(
                "并行镜头存档",
                world_state=world,
                character_manager=characters,
                clock_manager=clocks,
                conflict_manager=conflict,
                scene_manager=scenes,
                scene_frame_manager=frames,
            )
            loaded_world = WorldState()
            loaded_characters = CharacterManager()
            loaded_clocks = ClockManager()
            loaded_conflict = ConflictManager(loaded_characters)
            loaded_scenes = SceneManager()
            loaded_frames = SceneFrameManager()
            store.load_campaign(
                "并行镜头存档",
                world_state=loaded_world,
                character_manager=loaded_characters,
                clock_manager=loaded_clocks,
                conflict_manager=loaded_conflict,
                scene_manager=loaded_scenes,
                scene_frame_manager=loaded_frames,
            )

        self.assertEqual(loaded_scenes.current_scene.name, "白花碑回撤点")
        self.assertEqual([item.name for item in loaded_scenes.suspended_scenes], ["登记小室"])
        self.assertEqual(loaded_frames.current_frame.public_facts, ["艾薇娅守在回撤标记旁。"])
        self.assertIn(registration.scene_id, loaded_frames.suspended_frames)
        self.assertEqual(
            loaded_frames.suspended_frames[registration.scene_id].public_facts,
            ["伊莉雅正在翻查旧册。"],
        )

    def test_scene_working_brief_roundtrips_without_promoting_declarations(self) -> None:
        world = WorldState()
        characters = CharacterManager()
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        scenes = SceneManager()
        frames = SceneFrameManager()
        scene = scenes.start_scene(
            "卡里巴村监狱",
            SceneType.STANDARD,
            participants=["诺艾尔"],
        )
        frames.current_frame = SceneFrame(
            scene_key=f"{scene.scene_id}|卡里巴村监狱",
            scene_name="卡里巴村监狱",
            source_scene_id=scene.scene_id,
            working_brief={
                "version": 1,
                "source_events": [
                    {
                        "event_id": "event-1",
                        "speaker": "诺艾尔",
                        "text": "诺艾尔示意巡守接过牌子。",
                        "status": "gm_replied_without_state_change",
                        "tool_names": [],
                    }
                ],
                "committed_transactions": [],
                "fact_evidence": [],
                "last_authoritative_outcome": "",
                "last_public_reply": "巡守看向牌子，没有伸手。",
                "updated_at": "2026-08-04T00:00:00+00:00",
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            store = CampaignMemoryStore(tmpdir)
            store.save_campaign(
                "工作简报存档",
                world_state=world,
                character_manager=characters,
                clock_manager=clocks,
                conflict_manager=conflict,
                scene_manager=scenes,
                scene_frame_manager=frames,
            )
            loaded_world = WorldState()
            loaded_characters = CharacterManager()
            loaded_clocks = ClockManager()
            loaded_conflict = ConflictManager(loaded_characters)
            loaded_scenes = SceneManager()
            loaded_frames = SceneFrameManager()
            store.load_campaign(
                "工作简报存档",
                world_state=loaded_world,
                character_manager=loaded_characters,
                clock_manager=loaded_clocks,
                conflict_manager=loaded_conflict,
                scene_manager=loaded_scenes,
                scene_frame_manager=loaded_frames,
            )

        brief = loaded_frames.current_frame.working_brief
        self.assertEqual(
            brief["source_events"][0]["text"],
            "诺艾尔示意巡守接过牌子。",
        )
        self.assertEqual(brief["committed_transactions"], [])
        self.assertEqual(brief["fact_evidence"], [])
        self.assertEqual(brief["last_authoritative_outcome"], "")

    def test_load_coalesces_legacy_duplicate_scenes_at_exact_location(self) -> None:
        world = WorldState()
        characters = CharacterManager()
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        scenes = SceneManager()
        frames = SceneFrameManager()

        first = scenes.start_scene(
            "登记小室查册",
            SceneType.STANDARD,
            location="白花碑驿站·登记小室",
            participants=["赛璃", "洛岚"],
        )
        frames.current_frame = SceneFrame(
            scene_key=f"{first.scene_id}|登记小室",
            scene_name="登记小室查册",
            source_scene_id=first.scene_id,
            location=first.location,
            public_facts=["洛岚已经找到空白的完成栏。"],
        )
        frames.suspend_current_frame()
        scenes._suspend_current_scene()
        second = scenes.start_scene(
            "登记小室查册",
            SceneType.STANDARD,
            location="白花碑驿站·登记小室",
            participants=["艾薇娅", "苍祈", "财团巡逻队"],
        )
        frames.current_frame = SceneFrame(
            scene_key=f"{second.scene_id}|登记小室",
            scene_name="登记小室查册",
            source_scene_id=second.scene_id,
            location=second.location,
            public_facts=["财团巡逻队已经抵达门外。"],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            store = CampaignMemoryStore(tmpdir)
            store.save_campaign(
                "重复场景旧存档",
                world_state=world,
                character_manager=characters,
                clock_manager=clocks,
                conflict_manager=conflict,
                scene_manager=scenes,
                scene_frame_manager=frames,
            )
            loaded_world = WorldState()
            loaded_characters = CharacterManager()
            loaded_clocks = ClockManager()
            loaded_conflict = ConflictManager(loaded_characters)
            loaded_scenes = SceneManager()
            loaded_frames = SceneFrameManager()
            store.load_campaign(
                "重复场景旧存档",
                world_state=loaded_world,
                character_manager=loaded_characters,
                clock_manager=loaded_clocks,
                conflict_manager=loaded_conflict,
                scene_manager=loaded_scenes,
                scene_frame_manager=loaded_frames,
            )

        self.assertEqual(loaded_scenes.suspended_scenes, [])
        self.assertEqual(
            loaded_scenes.current_scene.participants,
            ["艾薇娅", "苍祈", "财团巡逻队", "赛璃", "洛岚"],
        )
        self.assertEqual(
            loaded_frames.current_frame.public_facts,
            ["财团巡逻队已经抵达门外。", "洛岚已经找到空白的完成栏。"],
        )
        self.assertEqual(loaded_frames.suspended_frames, {})

    def test_world_state_accepts_chinese_memory_visibility_aliases(self) -> None:
        world = WorldState()

        public_event = world.record_memory_event("守望会愿意听取证据。", visibility="公开")
        private_event = world.record_memory_event("艾蕾娜仍在犹豫。", visibility="私密")
        relation = world.record_relation("艾薇娅", "试图说服", "艾蕾娜", visibility="公共")

        self.assertEqual(public_event.visibility, MemoryVisibility.PUBLIC)
        self.assertEqual(private_event.visibility, MemoryVisibility.PRIVATE)
        self.assertEqual(relation.visibility, MemoryVisibility.PUBLIC)

    def test_save_and_load_campaign_snapshot_roundtrip(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="瓦莉亚",
                attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 8},
                max_hp=45,
                hp=21,
                max_mp=30,
                mp=12,
                traits=["pc"],
                statuses=[StatusEffect.SLOW],
                bound_arcana=["霜"],
                npc_specialty_bonuses={"施法检定": 3},
                npc_skill_effects={
                    "伤害抵抗": {"damage_types": ["fire", "ice"]},
                },
                npc_spell_check_bonus=5,
                npc_spell_damage_bonus=5,
                npc_spell_specific_damage_bonuses={"落雷": 5},
            )
        )
        clocks = ClockManager()
        clocks.add(Clock(name="帝国追兵逼近", max_segments=6, current=3))
        conflict = ConflictManager(characters)
        conflict.start_scene("断桥之战", ["瓦莉亚"])
        scene = SceneManager()
        scene.start_scene("断桥之战", SceneType.CONFLICT, location="旧王国边境断桥")
        scene.current_scene.pending_transition_location = "旧王国边境·西岸营地"
        scene.current_scene.pending_transition_reason = "瓦莉亚护送伤员穿过断桥"
        scene.current_scene.pending_transition_participants = ["瓦莉亚", "伤员"]
        scene.current_scene.action_round_number = 3
        scene.current_scene.action_round_required_actors = ["瓦莉亚", "银羽骑士"]
        scene.current_scene.action_round_acted_actors = ["瓦莉亚"]
        scene.current_scene.action_round_auto_advance_skip_names = ["帝国追兵逼近"]
        scene.current_scene.participant_locations["瓦莉亚"] = "旧王国边境断桥"
        scene.current_scene.participant_positions["瓦莉亚"] = "西侧桥柱"
        scene.current_scene.participant_activities["瓦莉亚"] = "护住伤员"
        scene.actor_locations["瓦莉亚"] = "旧王国边境断桥"
        scene.actor_positions["瓦莉亚"] = "西侧桥柱"
        scene_frames = SceneFrameManager()
        scene_frames.current_frame = SceneFrame(
            scene_key="scene-1|旧王国边境断桥",
            scene_name="断桥之战",
            source_scene_id="scene-1",
            location="旧王国边境断桥",
            public_facts=["伤员已经退到西侧桥柱后，不在帝国弩手的视线内。"],
            established_facts=["伤员已经退到西侧桥柱后，不在帝国弩手的视线内。"],
            committed_consequences=["东侧桥索已经断裂。"],
            recent_beats=["瓦莉亚护着伤员退到西侧桥柱后。"],
        )
        world = WorldState()
        world.world_profile.villain_seeds.append("银羽骑士试图唤醒旧王国机甲。")
        world.record_memory_event("瓦莉亚在断桥挡住了帝国机甲。", kind="scene_summary", entities=["瓦莉亚", "帝国机甲"])
        world.record_relation("瓦莉亚", "憎恨", "帝国", evidence="故乡被帝国焚毁")
        world.register_chapter_package(
            ChapterPackage(
                chapter_title="断桥之战",
                synopsis="官方章节式桥段：守住断桥并保护银羽线索。",
                iconic_elements=["银色羽毛"],
                scenes=[
                    ChapterPackageScene(
                        title="断桥开场",
                        scene_type="conflict",
                        purpose="让帝国机甲展示威胁。",
                    )
                ],
            )
        )
        world.record_transparency_audit(
            "npc_question_answered",
            True,
            "NPC 问答请求已有明确回应。",
            source="test",
        )
        world.upsert_gm_secret(
            "silver_feather",
            title="银羽骑士真实身份",
            content="银羽骑士是反派卡尔被封印的善性人格。",
            lock_level=SecretLockLevel.DRAFT,
            related_entities=["银羽骑士", "卡尔"],
            public_clues=["银色羽毛"],
        )
        world.ensure_npc_persona(
            "银羽骑士",
            aliases=["银羽"],
            public_identity="戴银羽面具的流浪骑士",
            secrets=["与卡尔共享灵魂"],
            current_location="旧王国边境断桥",
            current_mood="戒备",
            current_stance="暂时帮助英雄",
            active_goal="阻止机甲越过断桥",
            voice_examples=["先退到桥柱后面。"],
        )
        world.remember_npc_event(
            "银羽骑士",
            "亲眼看见瓦莉亚封住裂隙。",
            scene_id="scene-1",
            source="test",
            salience=4,
        )

        rules = RulesEngine(seed=0)
        ritual = RitualManager(rules, characters, clocks)
        plan = RitualPlan(
            name="封住裂隙",
            caster="瓦莉亚",
            discipline=RitualDiscipline.RITUALISM,
            potency=RitualPotency.MINOR,
            scope=RitualScope.INDIVIDUAL,
            effect="暂时封住魔界裂隙。",
            mp_cost=20,
            target_number=7,
            attributes=["INS", "WLP"],
            clock_segments=4,
            clock_name="仪式：封住裂隙",
        )
        ritual.active_rituals[plan.clock_name] = plan
        project = ProjectManager(characters)
        project.projects["水晶罗盘"] = ProjectState(
            name="水晶罗盘",
            inventor="瓦莉亚",
            potency=RitualPotency.MINOR,
            scope=RitualScope.INDIVIDUAL,
            use=ProjectUse.CONSUMABLE,
            effect="定位最近的古代遗迹入口。",
            material_cost=100,
            required_progress=1,
            current_progress=1,
            completed=True,
        )
        story_arc = StoryArcManager(world, clocks)
        story_arc.sync_from_world_profile()
        story_arc.advance_villain_pressure(
            story_arc.state.villain_pressure[0].track_id,
            amount=2,
            reason="银羽骑士夺走了机甲钥匙。",
        )
        story_arc.state.current_session_progress = SessionEpisodeProgress(
            session_number=1,
            active_scene_id="scene-1",
            scene_ids=["scene-1"],
            substantial_scene_ids=["scene-1"],
            scene_progress={
                "scene-1": SessionSceneProgress(
                    scene_id="scene-1",
                    player_actions=2,
                    material_changes=1,
                    consequences=1,
                )
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            store = CampaignMemoryStore(tmpdir)
            snapshot_path = store.save_campaign(
                "永雨之下",
                world_state=world,
                character_manager=characters,
                clock_manager=clocks,
                conflict_manager=conflict,
                scene_manager=scene,
                scene_frame_manager=scene_frames,
                ritual_manager=ritual,
                project_manager=project,
                story_arc_manager=story_arc,
            )

            new_world = WorldState()
            new_characters = CharacterManager()
            new_clocks = ClockManager()
            new_conflict = ConflictManager(new_characters)
            new_scene = SceneManager()
            new_scene_frames = SceneFrameManager()
            new_ritual = RitualManager(rules, new_characters, new_clocks)
            new_project = ProjectManager(new_characters)
            new_story_arc = StoryArcManager(new_world, new_clocks)

            store.load_campaign(
                "永雨之下",
                world_state=new_world,
                character_manager=new_characters,
                clock_manager=new_clocks,
                conflict_manager=new_conflict,
                scene_manager=new_scene,
                scene_frame_manager=new_scene_frames,
                ritual_manager=new_ritual,
                project_manager=new_project,
                story_arc_manager=new_story_arc,
            )

            self.assertTrue(snapshot_path.exists())
            self.assertTrue((snapshot_path.parent / "events.jsonl").exists())
            self.assertEqual(new_characters.get("瓦莉亚").statuses, [StatusEffect.SLOW])
            self.assertEqual(new_characters.get("瓦莉亚").bound_arcana, ["霜"])
            self.assertEqual(
                new_characters.get("瓦莉亚").npc_specialty_bonuses,
                {"施法检定": 3},
            )
            self.assertEqual(
                new_characters.get("瓦莉亚").npc_skill_effects,
                {"伤害抵抗": {"damage_types": ["fire", "ice"]}},
            )
            self.assertEqual(new_characters.get("瓦莉亚").npc_spell_check_bonus, 5)
            self.assertEqual(new_characters.get("瓦莉亚").npc_spell_damage_bonus, 5)
            self.assertEqual(
                new_characters.get("瓦莉亚").npc_spell_specific_damage_bonuses,
                {"落雷": 5},
            )
            self.assertEqual(new_clocks.get("帝国追兵逼近").current, 3)
            self.assertTrue(new_conflict.state.active)
            self.assertEqual(new_scene.current_scene.location, "旧王国边境断桥")
            self.assertEqual(
                new_scene.current_scene.pending_transition_location,
                "旧王国边境·西岸营地",
            )
            self.assertEqual(
                new_scene.current_scene.pending_transition_participants,
                ["瓦莉亚", "伤员"],
            )
            self.assertEqual(new_scene.current_scene.action_round_number, 3)
            self.assertEqual(
                new_scene.current_scene.action_round_required_actors,
                ["瓦莉亚", "银羽骑士"],
            )
            self.assertEqual(new_scene.current_scene.action_round_acted_actors, ["瓦莉亚"])
            self.assertEqual(
                new_scene.current_scene.action_round_auto_advance_skip_names,
                ["帝国追兵逼近"],
            )
            self.assertEqual(
                new_scene.current_scene.participant_locations["瓦莉亚"],
                "旧王国边境断桥",
            )
            self.assertEqual(
                new_scene.current_scene.participant_positions["瓦莉亚"],
                "西侧桥柱",
            )
            self.assertEqual(
                new_scene.current_scene.participant_activities["瓦莉亚"],
                "护住伤员",
            )
            self.assertEqual(new_scene.actor_locations["瓦莉亚"], "旧王国边境断桥")
            self.assertEqual(new_scene.actor_positions["瓦莉亚"], "西侧桥柱")
            self.assertIsNotNone(new_scene_frames.current_frame)
            self.assertEqual(
                new_scene_frames.current_frame.public_facts,
                ["伤员已经退到西侧桥柱后，不在帝国弩手的视线内。"],
            )
            self.assertEqual(
                new_scene_frames.current_frame.committed_consequences,
                ["东侧桥索已经断裂。"],
            )
            self.assertIn("仪式：封住裂隙", new_ritual.active_rituals)
            self.assertTrue(new_project.projects["水晶罗盘"].completed)
            self.assertEqual(new_world.gm_secrets["silver_feather"].public_clues, ["银色羽毛"])
            self.assertEqual(new_world.npc_personas["银羽骑士"].secrets, ["与卡尔共享灵魂"])
            self.assertEqual(new_world.resolve_npc_name("银羽"), "银羽骑士")
            self.assertEqual(new_world.npc_personas["银羽骑士"].current_mood, "戒备")
            self.assertEqual(new_world.npc_personas["银羽骑士"].active_goal, "阻止机甲越过断桥")
            self.assertEqual(new_world.npc_personas["银羽骑士"].memory_records[0]["scene_id"], "scene-1")
            self.assertEqual(new_world.active_chapter_package, "断桥之战")
            self.assertIn("银色羽毛", new_world.iconic_elements)
            self.assertEqual(new_world.active_chapter().scenes[0].title, "断桥开场")
            self.assertEqual(new_world.transparency_audit_log[-1].check_name, "npc_question_answered")
            self.assertEqual(new_story_arc.state.villain_pressure[0].current, 2)
            self.assertIn("机甲钥匙", new_story_arc.state.villain_pressure[0].last_action)
            restored_scene_progress = new_story_arc.state.current_session_progress.scene_progress["scene-1"]
            self.assertIsInstance(restored_scene_progress, SessionSceneProgress)
            self.assertTrue(restored_scene_progress.substantial)

    def test_story_change_cannot_rewrite_protected_iconic_element(self) -> None:
        world = WorldState()
        world.register_iconic_element("白花风铃", element_type="chapter", description="章节关键线索。")

        with self.assertRaisesRegex(ValueError, "标志性元素"):
            world.apply_story_fact("白花风铃其实是洛岚的姐姐，并且已经死亡。")

        self.assertFalse(any("已接受物语改写" in memory for memory in world.memories))
        self.assertFalse(world.transparency_audit_log[-1].passed)

    def test_gm_secret_revisions_are_versioned_and_public_facts_are_locked(self) -> None:
        world = WorldState()
        world.upsert_gm_secret(
            "silver_feather",
            title="银羽骑士真实身份",
            content="银羽骑士是卡尔的兄长。",
            lock_level=SecretLockLevel.SEEDED,
            public_clues=["银色羽毛"],
        )

        revised = world.revise_gm_secret(
            "silver_feather",
            new_content="银羽骑士是卡尔被封印的善性人格。",
            reason="更能映照主角的自我救赎主题。",
            preserve_clues=["银色羽毛"],
        )

        self.assertEqual(revised.content, "银羽骑士是卡尔被封印的善性人格。")
        self.assertEqual(len(revised.revisions), 1)
        self.assertEqual(revised.revisions[0].previous_content, "银羽骑士是卡尔的兄长。")

        world.set_gm_secret_lock("silver_feather", SecretLockLevel.PUBLIC)
        with self.assertRaisesRegex(ValueError, "公开事实"):
            world.revise_gm_secret("silver_feather", new_content="银羽骑士其实不存在。")

    def test_retrieve_relevant_memory_respects_private_visibility(self) -> None:
        world = WorldState()
        world.record_memory_event("卡尔占领了精灵村庄。", kind="scene_summary", entities=["卡尔", "精灵村庄"])
        world.record_memory_event(
            "卡尔真正害怕的是自己的善性人格。",
            kind="gm_secret",
            visibility=MemoryVisibility.PRIVATE,
            entities=["卡尔"],
        )
        world.record_relation("瓦莉亚", "憎恨", "卡尔", evidence="卡尔摧毁了她的故乡")

        public_results = world.retrieve_relevant_memory("卡尔 精灵村庄")
        private_results = world.retrieve_relevant_memory("卡尔 善性人格", include_private=True)

        self.assertTrue(any("精灵村庄" in item for item in public_results))
        self.assertFalse(any("善性人格" in item for item in public_results))
        self.assertTrue(any("善性人格" in item for item in private_results))

    def test_collective_npc_identity_roundtrips_with_campaign_snapshot(self) -> None:
        world = WorldState()
        world.ensure_npc_persona(
            "辉钢财团巡逻队",
            entity_kind="collective",
            public_identity="辉钢财团巡逻队",
            active_goal="完成现场核验",
        )
        characters = CharacterManager()
        clocks = ClockManager()
        conflict = ConflictManager(characters)

        with tempfile.TemporaryDirectory() as tmpdir:
            store = CampaignMemoryStore(tmpdir)
            store.save_campaign(
                "集体角色存档",
                world_state=world,
                character_manager=characters,
                clock_manager=clocks,
                conflict_manager=conflict,
            )
            loaded_world = WorldState()
            loaded_characters = CharacterManager()
            loaded_clocks = ClockManager()
            loaded_conflict = ConflictManager(loaded_characters)
            store.load_campaign(
                "集体角色存档",
                world_state=loaded_world,
                character_manager=loaded_characters,
                clock_manager=loaded_clocks,
                conflict_manager=loaded_conflict,
            )

        persona = loaded_world.npc_personas["辉钢财团巡逻队"]
        self.assertEqual(persona.entity_kind, "collective")
        self.assertEqual(persona.active_goal, "完成现场核验")


if __name__ == "__main__":
    unittest.main()
