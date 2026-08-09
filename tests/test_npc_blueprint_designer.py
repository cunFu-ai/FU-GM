from __future__ import annotations

import json
import threading
import unittest

from fu_gm.components.npc_blueprint_compiler import NPCBlueprintCompiler
from fu_gm.components.npc_blueprint_designer import NPCBlueprintDesigner
from fu_gm.components.world_state import WorldState
from fu_gm.core_bestiary import CORE_BESTIARY_ENTRIES
from fu_gm.models import StatusEffect
from fu_gm.spellbook import is_known_spell


class RecordingSelectionClient:
    def __init__(
        self,
        *,
        entered: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self.entered = entered
        self.release = release

    def create_chat_completion(self, **kwargs: object) -> str:
        self.calls.append(dict(kwargs))
        messages = kwargs["messages"]
        prompt = json.loads(messages[-1].content)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            self.release.wait(timeout=5)
        return json.dumps(
            {
                "template_name": prompt["candidates"][0]["name"],
                "selection_reason": "概念与当前职责最接近。",
                "tactics": {
                    "opening": "先确认英雄的阵形",
                    "cycle": ["攻击最暴露的目标"],
                    "crisis": "进入危机后改变行动模式",
                    "telegraph": "强力行动前展示清楚的蓄势",
                    "retreat": "目标无法实现时撤退",
                },
            },
            ensure_ascii=False,
        )


class NPCBlueprintDesignerTests(unittest.TestCase):
    def test_no_model_uses_humanoid_for_an_unauthored_social_npc_tie(self) -> None:
        world = WorldState()
        persona = world.ensure_npc_persona(
            "灰衣追猎者",
            public_identity="财团追猎者",
            role_in_story="封锁旧路并夺回遗物",
            core_drive="完成委托后撤离",
            traits=["耐心", "冷酷", "谨慎", "惜命"],
        )

        blueprint = NPCBlueprintDesigner(world).prepare_sync(
            persona,
            level=5,
        )

        self.assertEqual(blueprint.species, "humanoid")

    def test_model_receives_bounded_environment_and_at_most_eight_candidates(self) -> None:
        world = WorldState()
        persona = world.ensure_npc_persona(
            "雾中猎手",
            public_identity="追踪旧路的猎手",
            role_in_story="阻止英雄离开驿站",
            core_drive="把失忆旅人带回财团",
            combat_style="先封路，再从高处射击",
            traits=["耐心", "冷酷", "熟悉山路", "惜命"],
        )
        client = RecordingSelectionClient()
        designer = NPCBlueprintDesigner(world, client=client, model="test-model")

        blueprint = designer.prepare_sync(
            persona,
            level=12,
            scene_context={
                "scene_name": "白花碑驿站" * 30,
                "location": "旧路闸门",
                "premise": "旅人正在等待撤离",
                "current_pressure": "巡逻队逐渐逼近",
                "opposition_goal": "封住唯一出口",
                "npc_role_now": "负责截断退路",
                "visible_elements": [f"可见物件{i}" * 30 for i in range(9)],
                "private_secret_not_allowed": "这不应发送给选模模型",
            },
        )

        self.assertTrue(client.calls)
        call = client.calls[0]
        prompt = json.loads(call["messages"][-1].content)
        self.assertLessEqual(len(prompt["candidates"]), 8)
        self.assertLessEqual(len(prompt["current_environment"]["scene_name"]), 120)
        self.assertEqual(len(prompt["current_environment"]["visible_elements"]), 4)
        self.assertNotIn("private_secret_not_allowed", prompt["current_environment"])
        self.assertEqual(call["operation"], "npc_blueprint_design")
        self.assertEqual(blueprint.source_template, prompt["candidates"][0]["name"])

    def test_duplicate_background_request_reuses_one_pending_job(self) -> None:
        world = WorldState()
        persona = world.ensure_npc_persona(
            "铜面守门人",
            public_identity="遗迹守门人",
            role_in_story="守住地下入口",
            traits=["沉默", "警觉", "沉重", "守序"],
        )
        entered = threading.Event()
        release = threading.Event()
        client = RecordingSelectionClient(entered=entered, release=release)
        designer = NPCBlueprintDesigner(world, client=client, model="test-model")

        first = designer.submit(persona, level=10, background=True)
        self.assertTrue(entered.wait(timeout=2))
        second = designer.submit(persona, level=10, background=True)
        release.set()
        completed = designer.wait(first["job_id"], timeout=3)

        self.assertEqual(first["job_id"], second["job_id"])
        self.assertTrue(second["reused"])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(completed["status"], "ready")

    def test_ready_blueprint_is_rebuilt_when_requested_level_changes(self) -> None:
        world = WorldState()
        persona = world.ensure_npc_persona(
            "铜面守门人",
            public_identity="遗迹守门人",
            role_in_story="守住地下入口",
            traits=["沉默", "警觉", "沉重", "守序"],
        )
        client = RecordingSelectionClient()
        designer = NPCBlueprintDesigner(world, client=client, model="test-model")

        first = designer.prepare_sync(persona, level=5)
        second = designer.prepare_sync(persona, level=10)

        self.assertEqual(first.requested_level, 5)
        self.assertEqual(second.requested_level, 10)
        self.assertEqual(len(client.calls), 2)
        self.assertNotEqual(first.blueprint_id, second.blueprint_id)

    def test_scene_specific_result_is_rejected_after_scene_ends(self) -> None:
        world = WorldState()
        persona = world.ensure_npc_persona(
            "潮门巡守",
            public_identity="潮门巡守",
            role_in_story="守住潮门",
            traits=["警觉", "忠诚", "谨慎", "熟悉潮汐"],
        )
        entered = threading.Event()
        release = threading.Event()
        scene = {"id": "scene-one"}
        client = RecordingSelectionClient(entered=entered, release=release)
        designer = NPCBlueprintDesigner(
            world,
            client=client,
            model="test-model",
            current_scene_id=lambda: scene["id"],
        )

        submitted = designer.submit(
            persona,
            level=10,
            scene_id="scene-one",
            background=True,
        )
        self.assertTrue(entered.wait(timeout=2))
        scene["id"] = ""
        release.set()
        completed = designer.wait(submitted["job_id"], timeout=3)

        self.assertEqual(completed["status"], "stale")
        self.assertNotIn("潮门巡守", world.npc_combat_blueprints)

    def test_persona_revision_change_rejects_inflight_result(self) -> None:
        world = WorldState()
        persona = world.ensure_npc_persona(
            "灰衣使者",
            public_identity="财团使者",
            role_in_story="提出最后通牒",
            core_drive="收回遗物",
            traits=["傲慢", "严谨", "克制", "多疑"],
        )
        entered = threading.Event()
        release = threading.Event()
        client = RecordingSelectionClient(entered=entered, release=release)
        designer = NPCBlueprintDesigner(world, client=client, model="test-model")

        submitted = designer.submit(persona, level=15, background=True)
        self.assertTrue(entered.wait(timeout=2))
        persona.active_goal = "转而保护遗物持有者"
        release.set()
        completed = designer.wait(submitted["job_id"], timeout=3)

        self.assertEqual(completed["status"], "stale")
        self.assertNotIn("灰衣使者", world.npc_combat_blueprints)

    def test_every_core_bestiary_entry_compiles_into_an_executable_card(self) -> None:
        world = WorldState()
        designer = NPCBlueprintDesigner(world)
        supported_other_actions = {
            "传递魔力",
            "愤怒鼻息",
            "攻击蓄力",
            "仙人掌汁液",
        }

        for entry in CORE_BESTIARY_ENTRIES:
            npc_name = f"测试改皮-{entry.name}"
            persona = world.ensure_npc_persona(
                npc_name,
                public_identity=npc_name,
                role_in_story="图鉴继承审计",
                traits=list(entry.typical_traits),
            )
            blueprint = designer.prepare_sync(
                persona,
                level=entry.level,
                preferred_template=entry.name,
            )
            character = NPCBlueprintCompiler.materialize(blueprint)

            self.assertEqual(blueprint.source_template, entry.name)
            self.assertTrue(character.npc_attacks, entry.name)
            self.assertTrue(
                all(is_known_spell(spell) for spell in character.spells),
                entry.name,
            )
            self.assertTrue(
                all(
                    raw.partition("：")[0] in supported_other_actions
                    for raw in character.npc_other_actions
                ),
                entry.name,
            )

    def test_core_bestiary_triggered_traits_compile_as_typed_abilities(self) -> None:
        world = WorldState()
        designer = NPCBlueprintDesigner(world)
        expected = {
            "巨齿百足虫": {"affinity_change"},
            "硕鼠": {"check_bonus", "clock_extra_segments"},
            "轰炮蚁": {"terrain_guard"},
            "日光熊": {"modify_attack"},
            "白嚎怪": {"status_immunity_aura"},
            "魔导机兵": {"affinity_change", "modify_attack"},
            "强盗": {"clear_statuses"},
            "守卫": {"defense_bonus", "interpose"},
            "爆炎元素": {"fixed_damage"},
            "木乃伊": {"status_apply"},
            "电光轮": {"clock_extra_segments"},
            "幻菇人": {"affinity_change"},
        }

        for template_name, effect_types in expected.items():
            entry = next(
                item for item in CORE_BESTIARY_ENTRIES if item.name == template_name
            )
            persona = world.ensure_npc_persona(
                f"类型化-{template_name}",
                public_identity=template_name,
                role_in_story="图鉴触发特性审计",
                traits=list(entry.typical_traits),
            )
            blueprint = designer.prepare_sync(
                persona,
                level=entry.level,
                preferred_template=template_name,
            )

            self.assertEqual(
                {profile.effect_type for profile in blueprint.ability_profiles},
                effect_types,
                template_name,
            )

    def test_core_bestiary_attack_choices_are_not_guessed_from_prose(self) -> None:
        world = WorldState()
        designer = NPCBlueprintDesigner(world)
        cases = {
            "魔法提灯": ("元素释放", "random_damage_types", 6),
            "猫妖": ("鬼火", "damage_type_options", 2),
            "宁芙": ("四季之触", "status_options_on_hit", 4),
            "狙击手": ("狙击弓", "status_options_on_hit", 2),
        }

        for template_name, (attack_name, field_name, option_count) in cases.items():
            entry = next(
                item for item in CORE_BESTIARY_ENTRIES if item.name == template_name
            )
            persona = world.ensure_npc_persona(
                f"选项化-{template_name}",
                public_identity=template_name,
                role_in_story="图鉴攻击选项审计",
            )
            blueprint = designer.prepare_sync(
                persona,
                level=entry.level,
                preferred_template=template_name,
            )
            attack = next(item for item in blueprint.attacks if item.name == attack_name)

            self.assertEqual(len(getattr(attack, field_name)), option_count)
            if field_name == "status_options_on_hit":
                self.assertIsNone(attack.status_effect_on_hit)

    def test_core_bestiary_attack_effects_compile_as_structured_rules(self) -> None:
        world = WorldState()
        designer = NPCBlueprintDesigner(world)
        cases = [
            ("巨齿百足虫", "曲面切割", "bonus_if_previous_guard", 5),
            ("吸血蝙蝠", "吸血", "recover_hp_fraction", 0.5),
            ("碎响小丑", "小丑飞踢", "conditional_damage_bonus", 5),
            ("强盗", "蛮力肘击", "target_mp_loss", 10),
            ("浮空水母", "酸蚀之触", "target_ip_loss", 1),
            ("拟形怪", "偷取物品", "target_ip_loss", 2),
            ("骷髅法师", "法杖", "recover_mp_on_hit", 5),
            ("岩躯野猪", "巨岩暴冲", "self_hp_loss_if_all_miss", 20),
        ]

        for template_name, attack_name, field_name, expected in cases:
            entry = next(
                item for item in CORE_BESTIARY_ENTRIES if item.name == template_name
            )
            persona = world.ensure_npc_persona(
                f"攻击规则-{template_name}",
                public_identity=template_name,
                role_in_story="图鉴攻击规则审计",
            )
            blueprint = designer.prepare_sync(
                persona,
                level=entry.level,
                preferred_template=template_name,
            )
            attack = next(item for item in blueprint.attacks if item.name == attack_name)

            self.assertEqual(getattr(attack, field_name), expected, template_name)

        clown = next(
            item
            for item in designer.prepare_sync(
                world.ensure_npc_persona(
                    "攻击规则-碎响小丑-异常",
                    public_identity="碎响小丑",
                ),
                level=10,
                preferred_template="碎响小丑",
            ).attacks
            if item.name == "小丑飞踢"
        )
        self.assertEqual(
            set(clown.conditional_target_statuses),
            {StatusEffect.DAZED, StatusEffect.SHAKEN},
        )

        structured_cases = {
            ("锋翼鸟", "锋翼俯冲"): "suppress_trait",
            ("蛇足女妖", "冰冷凝视"): "action_restriction",
            ("爆炎元素", "火焰射流"): "suppress_resistance",
            ("魔眼", "混乱凝视"): "action_penalty",
            ("缠根藤", "腐化藤蔓"): "action_restriction_while_status",
            ("木乃伊", "古墓利爪"): "affinity_while_status",
        }
        for (template_name, attack_name), effect_type in structured_cases.items():
            entry = next(
                item for item in CORE_BESTIARY_ENTRIES if item.name == template_name
            )
            blueprint = designer.prepare_sync(
                world.ensure_npc_persona(
                    f"结构化攻击-{template_name}",
                    public_identity=template_name,
                ),
                level=entry.level,
                preferred_template=template_name,
            )
            attack = next(item for item in blueprint.attacks if item.name == attack_name)
            self.assertEqual(attack.effects[0].effect_type, effect_type)


if __name__ == "__main__":
    unittest.main()
