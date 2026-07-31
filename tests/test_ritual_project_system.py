import unittest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.project_manager import ProjectManager
from fu_gm.components.ritual_manager import RitualManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.world_state import WorldState
from fu_gm.expressor import Expressor
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import (
    Action,
    ActionType,
    Character,
    ProjectUse,
    RitualDiscipline,
    RitualPotency,
    RitualScope,
    WorldSheet,
)
from fu_gm.scene_orchestrator import SceneOrchestrator


class FakeRandom:
    def __init__(self, values):
        self.values = list(values)

    def randint(self, low, high):
        value = self.values.pop(0)
        if value < low or value > high:
            raise AssertionError(f"掷骰值 {value} 超出范围 {low}-{high}")
        return value


class RitualProjectSystemTests(unittest.TestCase):
    def test_chimerist_ritual_uses_the_attribute_pair_fixed_when_skill_was_learned(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="席芙",
                attributes={"DEX": 6, "MIG": 10, "INS": 6, "WLP": 10},
                max_hp=55,
                hp=55,
                max_mp=55,
                mp=55,
                classes={"拟兽使": 5},
                skills={"拟兽系仪式": 1},
                skill_options={"拟兽系仪式": ["力量+意志"]},
                traits=["pc"],
            )
        )
        manager = RitualManager(RulesEngine(), characters, ClockManager())

        plan = manager.plan_ritual(
            caster="席芙",
            name="借取狼群之嗅",
            discipline=RitualDiscipline.CHIMERISM,
            potency=RitualPotency.MINOR,
            scope=RitualScope.INDIVIDUAL,
            effect="循着残留气味找出逃亡者走过的道路。",
        )

        self.assertEqual(plan.attributes, ["MIG", "WLP"])
        with self.assertRaisesRegex(ValueError, "固定选择"):
            manager.plan_ritual(
                caster="席芙",
                name="临时改用洞察",
                discipline=RitualDiscipline.CHIMERISM,
                potency=RitualPotency.MINOR,
                scope=RitualScope.INDIVIDUAL,
                effect="尝试改用另一组属性。",
                attributes=["INS", "WLP"],
            )

    def test_chimerist_ritual_rejects_legacy_character_without_fixed_attribute_pair(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="席芙",
                attributes={"DEX": 6, "MIG": 10, "INS": 6, "WLP": 10},
                max_hp=55,
                hp=55,
                max_mp=55,
                mp=55,
                classes={"拟兽使": 5},
                skills={"拟兽系仪式": 1},
                traits=["pc"],
            )
        )
        manager = RitualManager(RulesEngine(), characters, ClockManager())

        with self.assertRaisesRegex(ValueError, "尚未记录"):
            manager.plan_ritual(
                caster="席芙",
                name="借取狼群之嗅",
                discipline=RitualDiscipline.CHIMERISM,
                potency=RitualPotency.MINOR,
                scope=RitualScope.INDIVIDUAL,
                effect="循着残留气味找出逃亡者走过的道路。",
            )

    def test_ritual_plan_calculates_cost_dl_attributes_and_rejects_forbidden_tags(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="米菈",
                attributes={"DEX": 6, "MIG": 6, "INS": 8, "WLP": 10},
                max_hp=35,
                hp=35,
                max_mp=120,
                mp=120,
                classes={"元素使": 5},
                skills={"元素系仪式": 1},
                traits=["pc"],
            )
        )
        manager = RitualManager(RulesEngine(), characters, ClockManager())

        plan = manager.plan_ritual(
            caster="米菈",
            name="唤雨守护结界",
            discipline=RitualDiscipline.ELEMENTALISM,
            potency=RitualPotency.MAJOR,
            scope=RitualScope.LARGE,
            effect="让暴雨短暂停止，为村庄争取撤离时间。",
            rare_material="风之精灵羽",
        )

        self.assertEqual(plan.mp_cost, 60)
        self.assertEqual(plan.target_number, 13)
        self.assertEqual(plan.attributes, ["INS", "WLP"])
        self.assertEqual(plan.clock_segments, 6)
        with self.assertRaisesRegex(ValueError, "direct_damage"):
            manager.plan_ritual(
                caster="米菈",
                name="火球雨",
                discipline=RitualDiscipline.ELEMENTALISM,
                potency=RitualPotency.MINOR,
                scope=RitualScope.INDIVIDUAL,
                effect="直接烧伤敌人。",
                forbidden_tags=["direct_damage"],
            )

    def test_ritualism_requires_ritual_training_or_ritualist_class(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="莱因",
                attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
                max_hp=45,
                hp=45,
                max_mp=60,
                mp=60,
                classes={"武器大师": 5},
                traits=["pc"],
            )
        )
        characters.add(
            Character(
                name="米菈",
                attributes={"DEX": 6, "MIG": 6, "INS": 10, "WLP": 10},
                max_hp=35,
                hp=35,
                max_mp=80,
                mp=80,
                classes={"元素使": 5},
                traits=["pc"],
            )
        )
        manager = RitualManager(RulesEngine(), characters, ClockManager())

        with self.assertRaisesRegex(ValueError, "仪式系仪式"):
            manager.plan_ritual(
                caster="莱因",
                name="唤醒古代法阵",
                discipline=RitualDiscipline.RITUALISM,
                potency=RitualPotency.MINOR,
                scope=RitualScope.INDIVIDUAL,
                effect="激活一座灵魂法阵。",
            )

        plan = manager.plan_ritual(
            caster="米菈",
            name="唤醒古代法阵",
            discipline=RitualDiscipline.RITUALISM,
            potency=RitualPotency.MINOR,
            scope=RitualScope.INDIVIDUAL,
            effect="激活一座灵魂法阵。",
        )

        self.assertEqual(plan.attributes, ["INS", "WLP"])

    def test_conflict_ritual_uses_clock_then_casts_and_spends_mp(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="米菈",
                attributes={"DEX": 6, "MIG": 6, "INS": 8, "WLP": 8},
                max_hp=35,
                hp=35,
                max_mp=80,
                mp=80,
                classes={"元素使": 5},
                skills={"元素系仪式": 1},
                traits=["pc"],
            )
        )
        rules = RulesEngine()
        rules._rng = FakeRandom([6, 6, 5, 5, 4, 4])
        clocks = ClockManager()
        manager = RitualManager(rules, characters, clocks)
        plan = manager.plan_ritual(
            caster="米菈",
            name="封住裂隙",
            discipline=RitualDiscipline.ELEMENTALISM,
            potency=RitualPotency.MINOR,
            scope=RitualScope.INDIVIDUAL,
            effect="暂时封住魔界裂隙。",
        )

        manager.start_conflict_ritual(plan)
        first_roll, first_change = manager.contribute_to_ritual(
            plan.clock_name,
            actor="米菈",
            spend_critical_opportunity=True,
        )
        with self.assertRaisesRegex(ValueError, "已经准备完成"):
            manager.contribute_to_ritual(plan.clock_name, actor="米菈")
        result = manager.cast_ritual(plan.clock_name, require_completed_clock=True)
        manager.finish_ritual(plan.clock_name)

        self.assertTrue(first_roll.success)
        self.assertEqual(first_roll.target_number, 7)
        self.assertEqual(first_change.delta, 4)
        self.assertTrue(result.success)
        self.assertEqual(characters.get("米菈").mp, 60)
        self.assertIn("暂时封住魔界裂隙", result.summary)
        self.assertFalse(clocks.exists(plan.clock_name))
        self.assertNotIn(plan.clock_name, manager.active_rituals)

    def test_project_manager_pays_cost_applies_flaw_and_tracks_daily_progress(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="诺亚",
                attributes={"DEX": 8, "MIG": 6, "INS": 10, "WLP": 8},
                max_hp=35,
                hp=35,
                max_mp=45,
                mp=45,
                classes={"造物使": 5},
                skills={"先见之明": 2},
                abilities=["可发起项目"],
                zenit=2000,
                traits=["pc"],
            )
        )
        manager = ProjectManager(characters)

        project = manager.start_project(
            inventor="诺亚",
            name="风灵滑翔翼",
            potency=RitualPotency.MAJOR,
            scope=RitualScope.INDIVIDUAL,
            use=ProjectUse.PERMANENT,
            effect="让小队短距离滑翔越过峡谷。",
            flaw="每天必须重新充能",
            material_credit=300,
        )
        helper_cost = manager.hire_helpers("风灵滑翔翼", payer="诺亚", count=1)
        first_day = manager.work_on_project("风灵滑翔翼", ["诺亚"])
        final_days = manager.work_on_project("风灵滑翔翼", ["诺亚"], days=2)

        self.assertEqual(project.material_cost, 1500)
        self.assertEqual(project.required_progress, 15)
        self.assertEqual(characters.get("诺亚").zenit, 250)
        self.assertEqual(helper_cost.amount, -750)
        self.assertEqual(first_day.after, 5)
        self.assertTrue(final_days.completed)
        self.assertIn("短距离滑翔", final_days.summary)

    def test_project_work_skips_missing_workers_without_crashing(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="诺亚",
                attributes={"DEX": 8, "MIG": 6, "INS": 10, "WLP": 8},
                max_hp=35,
                hp=35,
                max_mp=45,
                mp=45,
                classes={"造物使": 5},
                skills={"先见之明": 2},
                abilities=["可发起项目"],
                zenit=2000,
                traits=["pc"],
            )
        )
        manager = ProjectManager(characters)
        project = manager.start_project(
            inventor="诺亚",
            name="风灵滑翔翼",
            potency=RitualPotency.MINOR,
            scope=RitualScope.INDIVIDUAL,
            use=ProjectUse.PERMANENT,
            effect="让小队短距离滑翔越过峡谷。",
        )

        result = manager.work_on_project("风灵滑翔翼", ["诺亚", "伊莉雅"])

        self.assertEqual(result.workers, ["诺亚"])
        self.assertIn("未找到参与者：伊莉雅", result.summary)
        self.assertIn("未找到参与者：伊莉雅", project.notes[-1])

    def test_project_required_progress_rounds_up_partial_hundreds(self) -> None:
        characters = CharacterManager()
        manager = ProjectManager(characters)

        self.assertEqual(manager.required_progress_for_cost(1), 1)
        self.assertEqual(manager.required_progress_for_cost(100), 1)
        self.assertEqual(manager.required_progress_for_cost(150), 2)
        self.assertEqual(manager.required_progress_for_cost(1500), 15)

    def test_project_requires_tinkerer_training(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="瓦莉亚",
                attributes={"DEX": 8, "MIG": 8, "INS": 6, "WLP": 8},
                max_hp=45,
                hp=45,
                max_mp=40,
                mp=40,
                classes={"武器大师": 5},
                zenit=500,
                traits=["pc"],
            )
        )
        manager = ProjectManager(characters)

        with self.assertRaisesRegex(ValueError, "不是造物使"):
            manager.start_project(
                inventor="瓦莉亚",
                name="魔导机车",
                potency=RitualPotency.MODERATE,
                scope=RitualScope.SMALL,
                use=ProjectUse.PERMANENT,
                effect="高速穿越荒野。",
            )

    def test_orchestrator_exposes_ritual_and_project_workflows(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="诺亚",
                attributes={"DEX": 8, "MIG": 6, "INS": 10, "WLP": 8},
                max_hp=35,
                hp=35,
                max_mp=60,
                mp=60,
                classes={"造物使": 3, "元素使": 2},
                skills={"先见之明": 1, "元素系仪式": 1},
                abilities=["可发起项目"],
                zenit=1000,
                traits=["pc"],
            )
        )
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        rules = RulesEngine(seed=0)
        world_state = WorldState()
        app = SceneOrchestrator(
            character_manager=characters,
            clock_manager=clocks,
            conflict_manager=conflict,
            world_state=world_state,
            interceptor=ActionInterceptor(rules, characters, clocks, conflict, world_state),
            expressor=Expressor(),
            scene_manager=SceneManager(),
        )

        plan = app.plan_ritual(
            caster="诺亚",
            name="唤醒水晶门",
            discipline=RitualDiscipline.ELEMENTALISM,
            potency=RitualPotency.MINOR,
            scope=RitualScope.INDIVIDUAL,
            effect="打开旧遗迹的水晶门。",
        )
        project = app.start_project(
            inventor="诺亚",
            name="水晶罗盘",
            potency=RitualPotency.MINOR,
            scope=RitualScope.INDIVIDUAL,
            use=ProjectUse.CONSUMABLE,
            effect="定位最近的古代遗迹入口。",
        )

        self.assertEqual(plan.mp_cost, 20)
        self.assertEqual(project.material_cost, 100)
        self.assertTrue(world_state.memories)

    def test_orchestrator_persists_completed_project_exactly_once(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="诺亚",
                attributes={"DEX": 8, "MIG": 6, "INS": 10, "WLP": 8},
                max_hp=35,
                hp=35,
                max_mp=60,
                mp=60,
                classes={"造物使": 1},
                abilities=["可发起项目"],
                zenit=1000,
                traits=["pc"],
            )
        )
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        rules = RulesEngine(seed=0)
        world_state = WorldState()
        app = SceneOrchestrator(
            character_manager=characters,
            clock_manager=clocks,
            conflict_manager=conflict,
            world_state=world_state,
            interceptor=ActionInterceptor(
                rules,
                characters,
                clocks,
                conflict,
                world_state,
            ),
            expressor=Expressor(),
            scene_manager=SceneManager(),
        )
        project = app.start_project(
            inventor="诺亚",
            name="  水晶罗盘  ",
            potency=RitualPotency.MINOR,
            scope=RitualScope.INDIVIDUAL,
            use=ProjectUse.CONSUMABLE,
            effect="定位最近的古代遗迹入口。",
        )

        first = app.work_on_project(project.name, ["诺亚"])
        persisted_after_first = len(world_state.persistent_changes)
        second = app.work_on_project(project.name, ["诺亚"])

        self.assertTrue(first.completed)
        self.assertTrue(second.completed)
        self.assertEqual(project.name, "水晶罗盘")
        self.assertIn("水晶罗盘", app.project_manager.projects)
        self.assertEqual(len(world_state.persistent_changes), persisted_after_first)

    def test_interceptor_routes_ritual_actions_from_llm_action(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="米菈",
                attributes={"DEX": 6, "MIG": 6, "INS": 8, "WLP": 10},
                max_hp=35,
                hp=35,
                max_mp=120,
                mp=120,
                classes={"元素使": 5},
                skills={"元素系仪式": 1},
                traits=["pc"],
            )
        )
        rules = RulesEngine()
        rules._rng = FakeRandom([6, 6, 6, 6])
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        world_state = WorldState()
        ritual_manager = RitualManager(rules, characters, clocks)
        interceptor = ActionInterceptor(
            rules,
            characters,
            clocks,
            conflict,
            world_state,
            ritual_manager=ritual_manager,
            project_manager=ProjectManager(characters),
        )

        plan_resolution = interceptor.resolve(
            Action(
                ActionType.PLAN_RITUAL,
                {
                    "caster": "米菈",
                    "name": "唤醒水晶门",
                    "discipline": "elementalism",
                    "potency": "minor",
                    "scope": "individual",
                    "effect": "打开古代遗迹的水晶门。",
                    "start_conflict_clock": True,
                },
            )
        )
        interceptor.resolve(
            Action(
                ActionType.TRIGGER_OPPORTUNITY,
                {
                    "actor": "米菈",
                    "effect": "自定义",
                    "description": "水晶门映出一条旧裂纹。",
                    "window_id": interceptor.decision_window_manager.pending(
                        kind="critical_opportunity",
                        owner="米菈",
                    )[0].window_id,
                },
            )
        )
        cast_resolution = interceptor.resolve(
            Action(
                ActionType.CAST_RITUAL,
                {
                    "caster": "米菈",
                    "name": "净化符文",
                    "discipline": "elementalism",
                    "potency": "minor",
                    "scope": "individual",
                    "effect": "让污染符文暂时失效。",
                },
            )
        )

        self.assertEqual(plan_resolution.payload["ritual_plan"].mp_cost, 20)
        self.assertTrue(clocks.exists("仪式：唤醒水晶门"))
        self.assertEqual(clocks.get("仪式：唤醒水晶门").current, 2)
        plan_text = Expressor().render(plan_resolution)
        self.assertIn("启动仪式【唤醒水晶门】", plan_text)
        self.assertIn("已创建命刻", plan_text)
        self.assertNotIn("【仪式设计】", plan_text)
        self.assertTrue(cast_resolution.payload["ritual_result"].success)
        self.assertEqual(characters.get("米菈").mp, 100)
        cast_text = Expressor().render(cast_resolution)
        self.assertIn("仪式【净化符文】", cast_text)
        self.assertNotIn("【仪式结算】", cast_text)

    def test_conflict_ritual_start_failure_does_not_create_clock(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="米菈",
                attributes={"DEX": 6, "MIG": 6, "INS": 8, "WLP": 10},
                max_hp=35,
                hp=35,
                max_mp=120,
                mp=120,
                classes={"元素使": 5},
                skills={"元素系仪式": 1},
                traits=["pc"],
            )
        )
        rules = RulesEngine()
        rules._rng = FakeRandom([1, 1])
        clocks = ClockManager()
        interceptor = ActionInterceptor(
            rules,
            characters,
            clocks,
            ConflictManager(characters),
            WorldState(),
            ritual_manager=RitualManager(rules, characters, clocks),
            project_manager=ProjectManager(characters),
        )

        resolution = interceptor.resolve(
            Action(
                ActionType.PLAN_RITUAL,
                {
                    "caster": "米菈",
                    "name": "封住裂隙",
                    "discipline": "elementalism",
                    "potency": "minor",
                    "scope": "individual",
                    "effect": "暂时封住魔界裂隙。",
                    "start_conflict_clock": True,
                },
            )
        )

        self.assertFalse(resolution.payload["roll"].success)
        self.assertFalse(clocks.exists("仪式：封住裂隙"))
        self.assertIn("仪式命刻没有建立", Expressor().render(resolution))

    def test_conflict_ritual_start_reroll_replays_without_leaking_clock(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="米菈",
                attributes={"DEX": 6, "MIG": 6, "INS": 8, "WLP": 10},
                max_hp=35,
                hp=35,
                max_mp=120,
                mp=120,
                classes={"元素使": 5},
                skills={"元素系仪式": 1},
                traits=["pc"],
                theme="希望",
                fabula_points=2,
            )
        )
        rules = RulesEngine()
        rules._rng = FakeRandom([1, 2, 8])
        clocks = ClockManager()
        interceptor = ActionInterceptor(
            rules,
            characters,
            clocks,
            ConflictManager(characters),
            WorldState(),
            ritual_manager=RitualManager(rules, characters, clocks),
            project_manager=ProjectManager(characters),
        )

        first = interceptor.resolve(
            Action(
                ActionType.PLAN_RITUAL,
                {
                    "caster": "米菈",
                    "name": "唤醒水晶门",
                    "discipline": "elementalism",
                    "potency": "minor",
                    "scope": "individual",
                    "effect": "打开水晶门。",
                    "start_conflict_clock": True,
                },
            )
        )
        self.assertTrue(first.payload["check_result_provisional"])
        self.assertFalse(clocks.exists("仪式：唤醒水晶门"))
        trait_window = next(
            window
            for window in first.payload["post_check_windows"]
            if window["kind"] == "trait_invocation"
        )

        replayed = interceptor.resolve(
            Action(
                ActionType.INVOKE_TRAIT,
                {
                    "actor": "米菈",
                    "trait_name": "希望",
                    "reroll_indices": [0],
                    "window_id": trait_window["window_id"],
                },
            )
        )

        self.assertTrue(replayed.payload["roll"].success)
        self.assertTrue(replayed.payload["check_result_provisional"])
        self.assertFalse(clocks.exists("仪式：唤醒水晶门"))
        accept_window = interceptor.decision_window_manager.pending(
            kind="trait_invocation",
            owner="米菈",
            blocking_only=True,
        )[0]
        interceptor.resolve(
            Action(
                ActionType.NARRATE,
                {
                    "actor": "米菈",
                    "post_check_acceptance": True,
                    "window_id": accept_window.window_id,
                },
            )
        )
        self.assertTrue(clocks.exists("仪式：唤醒水晶门"))
        self.assertEqual(clocks.get("仪式：唤醒水晶门").current, 2)

    def test_tracked_ritual_start_rolls_and_progresses_clock_outside_conflict(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="赛璃",
                attributes={"DEX": 6, "MIG": 8, "INS": 10, "WLP": 8},
                max_hp=40,
                hp=40,
                max_mp=80,
                mp=80,
                classes={"御魂使": 5},
                skills={"御魂系仪式": 1},
                traits=["pc"],
            )
        )
        rules = RulesEngine()
        rules._rng = FakeRandom([8, 8])
        clocks = ClockManager()
        interceptor = ActionInterceptor(
            rules,
            characters,
            clocks,
            ConflictManager(characters),
            WorldState(),
            ritual_manager=RitualManager(rules, characters, clocks),
            project_manager=ProjectManager(characters),
        )

        resolution = interceptor.resolve(
            Action(
                ActionType.PLAN_RITUAL,
                {
                    "caster": "赛璃",
                    "name": "风铃回声",
                    "discipline": "spiritism",
                    "potency": "minor",
                    "scope": "small",
                    "effect": "让风铃暂时回响昨夜经过驿站的脚步和名字。",
                    "track_clock": True,
                },
            )
        )

        self.assertTrue(resolution.payload["roll"].success)
        self.assertTrue(clocks.exists("仪式：风铃回声"))
        self.assertEqual(clocks.get("仪式：风铃回声").current, 3)
        self.assertEqual(clocks.get("仪式：风铃回声").clock_type, "ritual")
        self.assertIn("并推进到 3/4", resolution.rules_text)

    def test_tracked_ritual_start_failure_does_not_create_clock_outside_conflict(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="赛璃",
                attributes={"DEX": 6, "MIG": 8, "INS": 10, "WLP": 8},
                max_hp=40,
                hp=40,
                max_mp=80,
                mp=80,
                classes={"御魂使": 5},
                skills={"御魂系仪式": 1},
                traits=["pc"],
            )
        )
        rules = RulesEngine()
        rules._rng = FakeRandom([1, 2])
        clocks = ClockManager()
        interceptor = ActionInterceptor(
            rules,
            characters,
            clocks,
            ConflictManager(characters),
            WorldState(),
            ritual_manager=RitualManager(rules, characters, clocks),
            project_manager=ProjectManager(characters),
        )

        resolution = interceptor.resolve(
            Action(
                ActionType.PLAN_RITUAL,
                {
                    "caster": "赛璃",
                    "name": "风铃回声",
                    "discipline": "spiritism",
                    "potency": "minor",
                    "scope": "small",
                    "effect": "让风铃暂时回响昨夜经过驿站的脚步和名字。",
                    "track_clock": True,
                },
            )
        )

        self.assertFalse(resolution.payload["roll"].success)
        self.assertFalse(clocks.exists("仪式：风铃回声"))
        self.assertIn("仪式命刻没有建立", resolution.rules_text)

    def test_interceptor_accepts_english_soul_ritual_alias(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="诺雅",
                attributes={"DEX": 6, "MIG": 6, "INS": 10, "WLP": 10},
                max_hp=35,
                hp=35,
                max_mp=80,
                mp=80,
                classes={"御魂使": 3},
                skills={"仪式御魂使术": 1},
                traits=["pc"],
            )
        )
        rules = RulesEngine()
        rules._rng = FakeRandom([7, 7])
        clocks = ClockManager()
        interceptor = ActionInterceptor(
            rules,
            characters,
            clocks,
            ConflictManager(characters),
            WorldState(),
            ritual_manager=RitualManager(rules, characters, clocks),
            project_manager=ProjectManager(characters),
        )

        resolution = interceptor.resolve(
            Action(
                ActionType.PLAN_RITUAL,
                {
                    "caster": "诺雅",
                    "name": "安抚星纹封印",
                    "discipline": "soul",
                    "potency": "minor",
                    "scope": "individual",
                    "effect": "安抚入口里残留的歌声。",
                },
            )
        )

        self.assertEqual(resolution.payload["ritual_plan"].discipline, RitualDiscipline.SPIRITISM)

    def test_interceptor_accepts_chinese_ritual_potency_and_scope_aliases(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="诺雅",
                attributes={"DEX": 6, "MIG": 6, "INS": 10, "WLP": 10},
                max_hp=35,
                hp=35,
                max_mp=80,
                mp=80,
                classes={"御魂使": 3},
                skills={"御魂系仪式": 1},
                traits=["pc"],
            )
        )
        rules = RulesEngine()
        rules._rng = FakeRandom([7, 7])
        clocks = ClockManager()
        interceptor = ActionInterceptor(
            rules,
            characters,
            clocks,
            ConflictManager(characters),
            WorldState(),
            ritual_manager=RitualManager(rules, characters, clocks),
            project_manager=ProjectManager(characters),
        )

        resolution = interceptor.resolve(
            Action(
                ActionType.PLAN_RITUAL,
                {
                    "caster": "诺雅",
                    "name": "风铃回声",
                    "discipline": "御魂使",
                    "potency": "轻微",
                    "scope": "小范围",
                    "effect": "确认旅人的记忆是否被导向灵魂中枢。",
                },
            )
        )

        plan = resolution.payload["ritual_plan"]
        self.assertEqual(plan.discipline, RitualDiscipline.SPIRITISM)
        self.assertEqual(plan.potency, RitualPotency.MINOR)
        self.assertEqual(plan.scope, RitualScope.SMALL)
        self.assertFalse(clocks.exists("仪式：风铃回声"))

    def test_interceptor_accepts_light_as_minor_ritual_potency_alias(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="诺雅",
                attributes={"DEX": 8, "MIG": 6, "INS": 10, "WLP": 8},
                max_hp=35,
                hp=35,
                max_mp=80,
                mp=80,
                classes={"御魂使": 5},
                skills={"御魂系仪式": 1},
                traits=["pc"],
            )
        )
        rules = RulesEngine()
        clocks = ClockManager()
        interceptor = ActionInterceptor(
            rules,
            characters,
            clocks,
            ConflictManager(characters),
            WorldState(),
            ritual_manager=RitualManager(rules, characters, clocks),
            project_manager=ProjectManager(characters),
        )

        resolution = interceptor.resolve(
            Action(
                ActionType.PLAN_RITUAL,
                {
                    "caster": "诺雅",
                    "name": "风铃回声",
                    "discipline": "spiritism",
                    "potency": "light",
                    "scope": "small",
                    "effect": "让风铃短暂回放公开经过的痕迹。",
                },
            )
        )

        self.assertEqual(resolution.payload["ritual_plan"].potency, RitualPotency.MINOR)

    def test_cast_ritual_with_incomplete_clock_returns_waiting_resolution(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="赛璃",
                attributes={"DEX": 6, "MIG": 8, "INS": 10, "WLP": 8},
                max_hp=40,
                hp=40,
                max_mp=80,
                mp=80,
                classes={"御魂使": 5},
                skills={"御魂系仪式": 1},
                traits=["pc"],
            )
        )
        rules = RulesEngine()
        clocks = ClockManager()
        ritual_manager = RitualManager(rules, characters, clocks)
        interceptor = ActionInterceptor(
            rules,
            characters,
            clocks,
            ConflictManager(characters),
            WorldState(),
            ritual_manager=ritual_manager,
            project_manager=ProjectManager(characters),
        )
        plan = ritual_manager.plan_ritual(
            caster="赛璃",
            name="风铃回声",
            discipline=RitualDiscipline.SPIRITISM,
            potency=RitualPotency.MINOR,
            scope=RitualScope.SMALL,
            effect="让风铃暂时回响昨夜经过驿站的脚步和名字。",
        )
        ritual_manager.start_conflict_ritual(plan)

        resolution = interceptor.resolve(
            Action(
                ActionType.CAST_RITUAL,
                {
                    "actor": "赛璃",
                    "clock_name": "仪式：风铃回声",
                    "require_completed_clock": True,
                },
            )
        )

        self.assertTrue(resolution.payload["ritual_waiting"])
        self.assertIn("还差 4 格", resolution.rules_text)
        self.assertEqual(characters.get("赛璃").mp, 80)

    def test_interceptor_sanitizes_ritual_effect_before_storing_plan(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="赛璃",
                attributes={"DEX": 6, "MIG": 8, "INS": 10, "WLP": 8},
                max_hp=40,
                hp=40,
                max_mp=80,
                mp=80,
                classes={"御魂使": 5},
                skills={"御魂系仪式": 1},
                traits=["pc"],
            )
        )
        rules = RulesEngine()
        clocks = ClockManager()
        interceptor = ActionInterceptor(
            rules,
            characters,
            clocks,
            ConflictManager(characters),
            WorldState(),
            ritual_manager=RitualManager(rules, characters, clocks),
            project_manager=ProjectManager(characters),
        )

        resolution = interceptor.resolve(
            Action(
                ActionType.PLAN_RITUAL,
                {
                    "caster": "赛璃",
                    "name": "风铃回声",
                    "discipline": "spiritism",
                    "potency": "minor",
                    "scope": "small",
                    "effect": (
                        "南星: 赛璃计划一个御魂仪式【风铃回声】：学科御魂，效力轻微，范围小范围，"
                        "效果是让风铃暂时回响昨夜经过驿站的脚步和名字，不直接伤害任何人。"
                    ),
                },
            )
        )

        plan = resolution.payload["ritual_plan"]
        self.assertEqual(plan.effect, "让风铃暂时回响昨夜经过驿站的脚步和名字，不直接伤害任何人。")
        self.assertNotIn("南星", plan.effect)
        self.assertNotIn("赛璃计划", plan.effect)

    def test_interceptor_routes_project_actions_from_llm_action(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="诺亚",
                attributes={"DEX": 8, "MIG": 6, "INS": 10, "WLP": 8},
                max_hp=35,
                hp=35,
                max_mp=45,
                mp=45,
                classes={"造物使": 5},
                zenit=500,
                traits=["pc"],
            )
        )
        rules = RulesEngine()
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        world_state = WorldState()
        project_manager = ProjectManager(characters)
        interceptor = ActionInterceptor(
            rules,
            characters,
            clocks,
            conflict,
            world_state,
            ritual_manager=RitualManager(rules, characters, clocks),
            project_manager=project_manager,
        )

        start_resolution = interceptor.resolve(
            Action(
                ActionType.START_PROJECT,
                {
                    "inventor": "诺亚",
                    "name": "水晶罗盘",
                    "potency": "minor",
                    "scope": "individual",
                    "use": "consumable",
                    "effect": "定位最近的古代遗迹入口。",
                },
            )
        )
        work_resolution = interceptor.resolve(
            Action(
                ActionType.WORK_PROJECT,
                {
                    "actor": "诺亚",
                    "project_name": "水晶罗盘",
                    "workers": ["诺亚"],
                    "days": 1,
                },
            )
        )

        self.assertEqual(start_resolution.payload["project"].material_cost, 100)
        self.assertEqual(characters.get("诺亚").zenit, 400)
        start_text = Expressor().render(start_resolution)
        self.assertIn("启动项目【水晶罗盘】", start_text)
        self.assertNotIn("【项目启动】", start_text)
        self.assertIn("材料需要 100Z", start_text)
        self.assertNotIn("规则上先记为", start_text)
        self.assertNotIn("用途：", start_text)
        self.assertNotIn("完成后写入：", start_text)
        self.assertTrue(work_resolution.payload["project_progress"].completed)
        work_text = Expressor().render(work_resolution)
        self.assertIn("项目【水晶罗盘】", work_text)
        self.assertNotIn("【项目推进】", work_text)

    def test_successful_ritual_persists_world_change(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="米菈",
                attributes={"DEX": 6, "MIG": 6, "INS": 8, "WLP": 10},
                max_hp=35,
                hp=35,
                max_mp=120,
                mp=120,
                classes={"元素使": 5},
                skills={"元素系仪式": 1},
                traits=["pc"],
            )
        )
        rules = RulesEngine()
        rules._rng = FakeRandom([6, 6])
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        world_state = WorldState()
        world_state.apply_world_sheet(WorldSheet(campaign_title="永雨之下", starting_region="永雨工业城下层"))
        interceptor = ActionInterceptor(
            rules,
            characters,
            clocks,
            conflict,
            world_state,
            ritual_manager=RitualManager(rules, characters, clocks),
            project_manager=ProjectManager(characters),
        )

        resolution = interceptor.resolve(
            Action(
                ActionType.CAST_RITUAL,
                {
                    "caster": "米菈",
                    "name": "净化雨水泵站",
                    "discipline": "elementalism",
                    "potency": "minor",
                    "scope": "small",
                    "effect": "下层泵站在本周内重新净化酸雨。",
                    "persistence_type": "facility",
                    "location": "永雨工业城下层",
                },
            )
        )

        self.assertTrue(resolution.payload["ritual_result"].success)
        self.assertEqual(world_state.persistent_changes[0].name, "净化雨水泵站")
        self.assertIn("净化雨水泵站", world_state.world_sheet.location_facilities["永雨工业城下层"][0])
        self.assertIn("长期变化", Expressor().render(resolution))

    def test_completed_project_persists_equipment_to_owner(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="诺亚",
                attributes={"DEX": 8, "MIG": 6, "INS": 10, "WLP": 8},
                max_hp=35,
                hp=35,
                max_mp=45,
                mp=45,
                classes={"造物使": 5},
                zenit=500,
                traits=["pc"],
            )
        )
        rules = RulesEngine()
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        world_state = WorldState()
        world_state.apply_world_sheet(WorldSheet(campaign_title="永雨之下", starting_region="永雨工业城下层"))
        project_manager = ProjectManager(characters)
        interceptor = ActionInterceptor(
            rules,
            characters,
            clocks,
            conflict,
            world_state,
            ritual_manager=RitualManager(rules, characters, clocks),
            project_manager=project_manager,
        )

        interceptor.resolve(
            Action(
                ActionType.START_PROJECT,
                {
                    "inventor": "诺亚",
                    "name": "星针护腕",
                    "potency": "minor",
                    "scope": "individual",
                    "use": "consumable",
                    "effect": "短暂定位附近最强烈的灵魂流。",
                    "output_type": "equipment",
                    "owner": "诺亚",
                },
            )
        )
        resolution = interceptor.resolve(
            Action(
                ActionType.WORK_PROJECT,
                {
                    "actor": "诺亚",
                    "project_name": "星针护腕",
                    "workers": ["诺亚"],
                    "days": 1,
                },
            )
        )

        self.assertTrue(resolution.payload["project_progress"].completed)
        self.assertTrue(project_manager.projects["星针护腕"].persisted)
        self.assertIn("星针护腕", characters.get("诺亚").equipment)
        self.assertIn("星针护腕", world_state.world_sheet.created_assets[0])
        self.assertIn("已写入长期状态", Expressor().render(resolution))


if __name__ == "__main__":
    unittest.main()
