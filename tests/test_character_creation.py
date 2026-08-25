import unittest

from fu_gm.components.character_creation_manager import CharacterCreationManager
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.session_zero_manager import SessionZeroManager
from fu_gm.components.sheet_exporter import SheetExporter
from fu_gm.components.world_state import WorldState
from fu_gm.expressor import Expressor
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import Bond, Character, HeroDraft, HeroCreationProfile, SessionZeroResponse, SessionZeroStage
from fu_gm.scene_orchestrator import SceneOrchestrator


class FakeRandom:
    def __init__(self, values):
        self.values = list(values)

    def randint(self, low, high):
        value = self.values.pop(0)
        if value < low or value > high:
            raise AssertionError(f"掷骰值 {value} 超出范围 {low}-{high}")
        return value


class CharacterCreationTests(unittest.TestCase):
    def build_world_state(self) -> WorldState:
        world_state = WorldState()
        session_zero = SessionZeroManager(world_state)
        session_zero.start()
        session_zero.apply_response(
            SessionZeroResponse(
                message="世界创建完成。",
                stage=SessionZeroStage.READY,
                world_updates={
                    "campaign_title": "永雨之下",
                    "world_style": "科技奇幻",
                    "core_themes": ["剥削与反抗"],
                    "group_concept": "反抗腐败强权的革命者小队",
                    "starting_region": "永雨工业城下层",
                    "major_locations": {"永雨工业城": "上层偷走阳光，下层承受魔导烟雨。"},
                    "factions": {"辉钢财团": "垄断灵魂能源的企业贵族。"},
                    "villain_seeds": ["辉钢财团的继承人把剥削包装成奇迹。"],
                    "mysteries": ["被抽取的灵魂能源最终流向了哪里？"],
                    "safety_lines": ["不详细描写血腥折磨。"],
                    "completed": True,
                },
            )
        )
        return world_state

    def test_create_starting_player_character_from_session_zero_context(self) -> None:
        characters = CharacterManager()
        rules = RulesEngine()
        rules._rng = FakeRandom([3, 5])
        manager = CharacterCreationManager(characters, self.build_world_state(), rules_engine=rules)

        result = manager.create_player_character(
            HeroCreationProfile(
                player_name="阿凛",
                hero_name="米菈",
                identity="逃离财团实验室的魔导技师",
                theme="自由",
                origin="永雨工业城下层",
                classes={"造物使": 2, "御魂使": 2, "守护者": 1},
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                bonds=[Bond(target="永雨工业城下层", emotions=["忠诚"])],
                skills={"便携装置": 1, "秘密配方": 1, "灵魂魔法": 2, "保镖": 1},
                skill_options={"便携装置": ["魔导装置"]},
                spells=["治愈", "护盾"],
                equipment=["钢匕首", "符文盾", "旅行装束"],
                notes=["她知道辉钢财团的地下能源管线。"],
            )
        )

        hero = result.character
        self.assertEqual(hero.level, 5)
        self.assertEqual(hero.fabula_points, 3)
        self.assertEqual(hero.classes, {"造物使": 2, "御魂使": 2, "守护者": 1})
        self.assertEqual(hero.max_hp, 50)
        self.assertEqual(hero.max_mp, 50)
        self.assertEqual(hero.inventory_points, 8)
        self.assertEqual(hero.max_inventory_points, 8)
        self.assertEqual(hero.defenses, {"physical": 11, "magic": 11})
        self.assertEqual(hero.initiative, -1)
        self.assertEqual(hero.equipped_main_hand, "钢匕首")
        self.assertEqual(hero.equipped_off_hand, "符文盾")
        self.assertEqual(hero.weapon_damage, 4)
        self.assertEqual(hero.weapon_accuracy_attributes, ["DEX", "INS"])
        self.assertEqual(hero.weapon_accuracy_modifier, 1)
        self.assertEqual(hero.skills["灵魂魔法"], 2)
        self.assertEqual(result.equipment_cost, 400)
        self.assertEqual(result.fate_roll, (3, 5))
        self.assertEqual(hero.zenit, 180)
        self.assertIn("可装备职业盔甲", hero.abilities)
        self.assertIn("可装备职业盾牌", hero.abilities)
        self.assertIn("pc", hero.traits)
        self.assertEqual(characters.get("米菈").identity, "逃离财团实验室的魔导技师")
        self.assertIn("职业免费增益：最大 MP +5", result.applied_benefits)

    def test_hero_draft_accepts_chinese_attribute_names(self) -> None:
        world_state = self.build_world_state()
        world_state.world_profile.hero_drafts["阿凛"] = HeroDraft(
            player_name="阿凛",
            hero_name="米菈",
            identity="逃离财团实验室的魔导技师",
            theme="自由",
            origin="永雨工业城下层",
            classes={"造物使": 2, "御魂使": 2, "守护者": 1},
            attributes={"敏捷": 8, "洞察": 8, "力量": 8, "意志": 8},
            skills={"便携装置": 1, "秘密配方": 1, "灵魂魔法": 2, "保镖": 1},
            skill_options={"便携装置": ["魔导装置"]},
            spells=["治愈", "护盾"],
            equipment=["钢匕首", "符文盾", "旅行装束"],
            notes=["她知道辉钢财团的地下能源管线。"],
            confirmed=True,
        )
        manager = CharacterCreationManager(CharacterManager(), world_state)

        validation = manager.validate_hero_draft("阿凛")

        self.assertTrue(validation.ready, validation.errors + validation.missing_fields)
        self.assertEqual(validation.profile.attributes, {"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8})

    def test_starting_equipment_can_use_flavor_name_with_rules_template(self) -> None:
        characters = CharacterManager()
        rules = RulesEngine()
        rules._rng = FakeRandom([2, 6])
        manager = CharacterCreationManager(characters, self.build_world_state(), rules_engine=rules)

        result = manager.create_player_character(
            HeroCreationProfile(
                player_name="白河",
                hero_name="伊织",
                identity="用纸牌占卜地下城路径的赌徒",
                theme="幸运",
                origin="永雨工业城下层",
                classes={"浪客": 2, "旅人": 2, "御魂使": 1},
                attributes={"DEX": 10, "INS": 8, "MIG": 6, "WLP": 8},
                skills={"高速": 1, "窃取灵魂": 1, "宝藏猎人": 1, "通晓道路": 1, "灵魂魔法": 1},
                spells=["治愈术"],
                equipment=["投掷卡牌（手里剑模板）", "和服（按丝质衬衫结算）"],
                notes=["她坚持说每张牌都有自己的命运。"],
            )
        )

        hero = result.character
        self.assertEqual(hero.equipment, ["投掷卡牌", "和服"])
        self.assertEqual(hero.equipment_templates, {"投掷卡牌": "手里剑", "和服": "丝质衬衫"})
        self.assertEqual(hero.equipped_main_hand, "投掷卡牌")
        self.assertEqual(hero.equipped_armor, "和服")
        self.assertEqual(hero.weapon_accuracy_attributes, ["DEX", "INS"])
        self.assertEqual(hero.weapon_damage, 4)
        self.assertEqual(hero.weapon_range, "ranged")
        self.assertEqual(hero.defenses, {"physical": 11, "magic": 10})
        self.assertEqual(hero.initiative, -1)
        self.assertEqual(result.equipment_cost, 250)
        exported = SheetExporter().export_character_markdown(hero)
        self.assertIn("投掷卡牌=>手里剑", exported)
        self.assertIn("和服=>丝质衬衫", exported)

    def test_dual_shield_warrior_can_start_with_two_renamed_shields(self) -> None:
        characters = CharacterManager()
        rules = RulesEngine()
        rules._rng = FakeRandom([2, 4])
        manager = CharacterCreationManager(characters, self.build_world_state(), rules_engine=rules)

        result = manager.create_player_character(
            HeroCreationProfile(
                player_name="loading",
                hero_name="伊大石",
                identity="放弃厨师生涯的魔法学院学徒",
                theme="守护",
                origin="土豆村",
                classes={"守护者": 4, "元素使": 1},
                attributes={"DEX": 6, "INS": 6, "MIG": 10, "WLP": 10},
                skills={
                    "保镖": 1,
                    "防御精通": 1,
                    "双盾战士": 1,
                    "挺身守护": 1,
                    "元素系仪式": 1,
                },
                equipment=[
                    "大黑锅（符文盾模板）",
                    "大黑锅（符文盾模板）",
                    "青铜板甲",
                ],
                equipment_slots={
                    "main_hand": "大黑锅（符文盾模板）",
                    "off_hand": "大黑锅（符文盾模板）",
                    "armor": "青铜板甲",
                },
            )
        )

        hero = result.character
        self.assertEqual(result.equipment_cost, 500)
        self.assertEqual(hero.equipment.count("大黑锅"), 2)
        self.assertEqual(hero.equipment_templates["大黑锅"], "符文盾")
        self.assertEqual(hero.equipped_main_hand, "大黑锅")
        self.assertEqual(hero.equipped_shield, "大黑锅")
        self.assertEqual(hero.equipped_off_hand, "")
        self.assertEqual(hero.defenses, {"physical": 15, "magic": 10})
        self.assertEqual(hero.weapon_accuracy_attributes, ["MIG", "MIG"])
        self.assertEqual(hero.weapon_damage, 6)
        self.assertEqual(hero.weapon_range, "melee")

    def test_dual_shield_loadout_requires_two_owned_shields(self) -> None:
        manager = CharacterCreationManager(CharacterManager(), self.build_world_state())

        with self.assertRaisesRegex(ValueError, "数量不足"):
            manager.build_equipment_plan(
                ["大黑锅（符文盾模板）"],
                ["可装备职业盾牌"],
                {"DEX": 6, "INS": 6, "MIG": 10, "WLP": 10},
                {
                    "main_hand": "大黑锅（符文盾模板）",
                    "off_hand": "大黑锅（符文盾模板）",
                },
                {"双盾战士": 1},
            )

    def test_starting_equipment_keeps_spares_separate_from_opening_loadout(self) -> None:
        manager = CharacterCreationManager(
            CharacterManager(),
            self.build_world_state(),
        )

        plan = manager.build_equipment_plan(
            ["匕首（钢匕首模板）", "细剑", "符文盾"],
            ["可装备职业近战武器", "可装备职业盾牌"],
            {"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
            {
                "main_hand": "细剑",
                "shield": "符文盾",
            },
        )

        self.assertEqual(plan.cost, 500)
        self.assertEqual(plan.names, ["无防具", "匕首", "细剑", "符文盾"])
        self.assertEqual(plan.templates["匕首"], "钢匕首")
        self.assertEqual(plan.main_hand, "细剑")
        self.assertEqual(plan.off_hand, "符文盾")
        self.assertNotEqual(plan.main_hand, "匕首")

    def test_equipment_template_correction_replaces_placeholder_without_clearing_slot(self) -> None:
        session_zero = SessionZeroManager(self.build_world_state())
        draft = HeroDraft(
            player_name="村夫",
            hero_name="诺艾尔",
            equipment=["匕首", "细剑", "钢匕首"],
        )

        session_zero._apply_hero_draft_patch(
            draft,
            {
                "equipment": ["匕首（钢匕首模板）"],
                "remove_equipment": ["匕首", "钢匕首"],
                "equipment_slots": {
                    "main_hand": "细剑",
                    "off_hand": "匕首",
                },
            },
        )

        self.assertEqual(draft.equipment, ["细剑", "匕首（钢匕首模板）"])
        self.assertEqual(
            draft.equipment_slots,
            {"main_hand": "细剑", "off_hand": "匕首"},
        )

    def test_starting_equipment_accepts_improvised_weapon_templates_and_aliases(self) -> None:
        characters = CharacterManager()
        rules = RulesEngine()
        rules._rng = FakeRandom([1, 1])
        manager = CharacterCreationManager(characters, self.build_world_state(), rules_engine=rules)

        result = manager.create_player_character(
            HeroCreationProfile(
                player_name="白河",
                hero_name="阿尔",
                identity="会把杯子当武器的酒馆斗士",
                theme="自由",
                origin="边境酒馆",
                classes={"浪客": 2, "旅人": 2, "御魂使": 1},
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                skills={"阴狠手段": 1, "闪避": 1, "充足补给": 1, "宝物猎人": 1, "灵魂魔法": 1},
                spells=["治愈术"],
                equipment=["酒瓶（临时武器（近战）模板）", "飞牌（临时武器(远程)模板）", "智者之袍"],
            )
        )

        hero = result.character
        self.assertEqual(hero.equipment_templates["酒瓶"], "临时武器(近战)")
        self.assertEqual(hero.equipment_templates["飞牌"], "临时武器(远程)")
        self.assertEqual(hero.equipment_templates["智者之袍"], "贤者之袍")
        self.assertEqual(result.equipment_cost, 200)
        self.assertEqual(hero.zenit, 320)

    def test_starting_equipment_rejects_untemplated_custom_item(self) -> None:
        manager = CharacterCreationManager(CharacterManager(), self.build_world_state())

        with self.assertRaisesRegex(ValueError, "自定义外观需要写明数值模板"):
            manager.create_player_character(
                HeroCreationProfile(
                    player_name="白河",
                    hero_name="伊织",
                    identity="秘宝猎人",
                    theme="幸运",
                    origin="永雨工业城下层",
                    classes={"浪客": 2, "旅人": 2, "御魂使": 1},
                    attributes={"DEX": 10, "INS": 8, "MIG": 6, "WLP": 8},
                    skills={"阴狠手段": 1, "闪避": 1, "充足补给": 1, "宝物猎人": 1, "灵魂魔法": 1},
                    spells=["治愈术"],
                    equipment=["月光伞"],
                )
            )

    def test_starting_equipment_budget_counts_all_initial_equipment(self) -> None:
        manager = CharacterCreationManager(CharacterManager(), self.build_world_state())

        with self.assertRaisesRegex(ValueError, "超过 500Z"):
            manager.create_player_character(
                HeroCreationProfile(
                    player_name="白河",
                    hero_name="朱诺",
                    identity="背着整套军械的神射手",
                    theme="使命",
                    origin="永雨工业城下层",
                    classes={"神射手": 3, "武器大师": 1, "旅人": 1},
                    attributes={"DEX": 10, "INS": 8, "MIG": 8, "WLP": 6},
                    skills={"弹幕射击": 1, "鹰眼": 1, "远程武器精通": 1, "利刃风暴": 1, "宝物猎人": 1},
                    equipment=["手枪", "钢匕首", "贤者之袍"],
                )
            )

    def test_character_creation_accepts_extracted_class_and_skill_aliases(self) -> None:
        characters = CharacterManager()
        rules = RulesEngine()
        rules._rng = FakeRandom([1, 6])
        manager = CharacterCreationManager(characters, self.build_world_state(), rules_engine=rules)

        result = manager.create_player_character(
            HeroCreationProfile(
                player_name="阿凛",
                hero_name="露米娅",
                identity="爱拆宝箱的魔导机关师",
                theme="好奇",
                origin="阿斯特拉庭",
                classes={"造物使": 2, "御魂使": 2, "奥灵使": 1},
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                skills={"便携装置": 1, "秘密配方": 1, "灵魂魔法": 2, "契约与召唤": 1},
                skill_options={"便携装置": ["魔导装置"]},
                spells=["治愈术", "屏障"],
                equipment=["法杖", "丝质衬衫"],
            )
        )

        hero = result.character
        self.assertEqual(hero.classes, {"造物使": 2, "御魂使": 2, "奥灵使": 1})
        self.assertEqual(hero.skills["便携装置"], 1)
        self.assertEqual(hero.skills["灵魂魔法"], 2)
        self.assertEqual(hero.skills["契约与召唤"], 1)
        self.assertEqual(hero.equipped_main_hand, "法杖")
        self.assertEqual(hero.equipped_off_hand, "")

    def test_legacy_two_handed_off_hand_marker_is_not_loaded_as_equipment(self) -> None:
        characters = CharacterManager()

        characters.add(
            Character(
                name="艾丽妮",
                attributes={"DEX": 6, "INS": 10, "MIG": 6, "WLP": 10},
                max_hp=35,
                hp=35,
                max_mp=55,
                mp=55,
                equipped_main_hand="法杖",
                equipped_off_hand="双手占用",
                equipment=["法杖", "贤者之袍", "魔典"],
            )
        )

        self.assertEqual(characters.get("艾丽妮").equipped_off_hand, "")

    def test_invalid_starting_class_allocation_is_rejected(self) -> None:
        manager = CharacterCreationManager(CharacterManager(), self.build_world_state())

        with self.assertRaisesRegex(ValueError, "2 到 3 个职业"):
            manager.create_player_character(
                HeroCreationProfile(
                    player_name="阿凛",
                    hero_name="米菈",
                    identity="魔导技师",
                    theme="自由",
                    origin="永雨工业城",
                    classes={"造物使": 5},
                    attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                    skills={"便携装置": 5},
                )
            )

        with self.assertRaisesRegex(ValueError, "总职业等级必须等于 5"):
            manager.create_player_character(
                HeroCreationProfile(
                    player_name="阿凛",
                    hero_name="米菈",
                    identity="魔导技师",
                    theme="自由",
                    origin="永雨工业城",
                    classes={"造物使": 2, "御魂使": 2},
                    attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                    skills={"便携装置": 2, "灵魂魔法": 2},
                )
            )

        with self.assertRaisesRegex(ValueError, "细剑"):
            manager.create_player_character(
                HeroCreationProfile(
                    player_name="阿凛",
                    hero_name="米菈",
                    identity="魔导技师",
                    theme="自由",
                    origin="永雨工业城",
                    classes={"造物使": 2, "御魂使": 2, "旅人": 1},
                    attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                    skills={"便携装置": 2, "灵魂魔法": 2, "通晓道路": 1},
                    skill_options={"便携装置": ["魔导装置", "魔导装置"]},
                    spells=["治愈术", "屏障"],
                    equipment=["细剑"],
                )
            )

        with self.assertRaisesRegex(ValueError, "超过 500Z"):
            manager.create_player_character(
                HeroCreationProfile(
                    player_name="阿凛",
                    hero_name="米菈",
                    identity="守护者",
                    theme="自由",
                    origin="永雨工业城",
                    classes={"守护者": 3, "武器大师": 2},
                    attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                    skills={"保镖": 1, "防御精通": 2, "利刃风暴": 1, "近战武器掌握": 1},
                    equipment=["钢制板甲", "符文盾", "细剑"],
                )
            )

    def test_starting_character_rejects_four_classes(self) -> None:
        characters = CharacterManager()
        rules = RulesEngine()
        rules._rng = FakeRandom([2, 4])
        manager = CharacterCreationManager(characters, self.build_world_state(), rules_engine=rules)

        with self.assertRaisesRegex(ValueError, "2 到 3 个职业"):
            manager.create_player_character(
                HeroCreationProfile(
                    player_name="村夫",
                    hero_name="诺艾尔",
                    identity="秘宝猎人",
                    theme="野心",
                    origin="托伦",
                    classes={"元素使": 1, "武器大师": 2, "旅人": 1, "游说家": 1},
                    attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                    skills={"元素魔法": 1, "碎骨": 1, "破防打击": 1, "宝物猎人": 1, "谴责": 1},
                    spells=["元素幕障"],
                    equipment=["钢匕首", "旅行装束"],
                )
            )
    def test_spell_granting_skill_rejects_unknown_or_wrong_school_spells(self) -> None:
        manager = CharacterCreationManager(CharacterManager(), self.build_world_state())

        with self.assertRaisesRegex(ValueError, "未知或未接入标准法术：火焰箭"):
            manager.create_player_character(
                HeroCreationProfile(
                    player_name="阿凛",
                    hero_name="露米",
                    identity="星灯学徒",
                    theme="希望",
                    origin="星灯镇",
                    classes={"元素使": 1, "博学家": 2, "旅人": 2},
                    attributes={"DEX": 8, "INS": 10, "MIG": 6, "WLP": 8},
                    skills={"元素魔法": 1, "灵光洞见": 1, "知识就是力量": 1, "充足补给": 1, "宝物猎人": 1},
                    spells=["火焰箭"],
                    equipment=["法杖", "旅行装束"],
                )
            )

        with self.assertRaisesRegex(ValueError, "属于御魂使法术"):
            manager.create_player_character(
                HeroCreationProfile(
                    player_name="阿凛",
                    hero_name="露米",
                    identity="星灯学徒",
                    theme="希望",
                    origin="星灯镇",
                    classes={"元素使": 1, "博学家": 2, "旅人": 2},
                    attributes={"DEX": 8, "INS": 10, "MIG": 6, "WLP": 8},
                    skills={"元素魔法": 1, "灵光洞见": 1, "知识就是力量": 1, "充足补给": 1, "宝物猎人": 1},
                    spells=["治愈术"],
                    equipment=["法杖", "旅行装束"],
                )
            )

    def test_each_class_free_benefit_is_applied_to_starting_sheet(self) -> None:
        manager = CharacterCreationManager(CharacterManager(), self.build_world_state())
        expectations = {
            "奥灵使": (0, 5, 0, []),
            "拟兽使": (0, 5, 0, []),
            "暗刃骑士": (5, 0, 0, ["可装备职业近战武器", "可装备职业盔甲"]),
            "元素使": (0, 5, 0, []),
            "熵术士": (0, 5, 0, []),
            "怒焰斗士": (5, 0, 0, ["可装备职业近战武器", "可装备职业盔甲"]),
            "守护者": (5, 0, 0, ["可装备职业盔甲", "可装备职业盾牌"]),
            "博学家": (0, 5, 0, []),
            "游说家": (0, 5, 0, []),
            "浪客": (0, 0, 2, []),
            "神射手": (5, 0, 0, ["可装备职业远程武器", "可装备职业盾牌"]),
            "御魂使": (0, 5, 0, []),
            "造物使": (0, 0, 2, ["可发起项目"]),
            "旅人": (0, 0, 2, []),
            "武器大师": (5, 0, 0, ["可装备职业近战武器", "可装备职业盾牌"]),
        }

        for class_name, (hp, mp, ip, abilities) in expectations.items():
            with self.subTest(class_name=class_name):
                benefits = manager.class_benefits({class_name: 1})
                self.assertEqual(benefits["hp"], hp)
                self.assertEqual(benefits["mp"], mp)
                self.assertEqual(benefits["ip"], ip)
                for ability in abilities:
                    self.assertIn(ability, benefits["abilities"])

    def test_finalize_campaign_creation_outputs_party_and_world_sheets(self) -> None:
        characters = CharacterManager()
        world_state = self.build_world_state()
        manager = CharacterCreationManager(characters, world_state)
        manager.create_player_character(
            HeroCreationProfile(
                player_name="阿凛",
                hero_name="米菈",
                identity="逃离财团实验室的魔导技师",
                theme="自由",
                origin="永雨工业城下层",
                classes={"造物使": 2, "御魂使": 2, "守护者": 1},
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                bonds=[Bond(target="辉钢财团", emotions=["不信任", "仇恨"])],
                skills={"便携装置": 1, "秘密配方": 1, "灵魂魔法": 2, "保镖": 1},
                skill_options={"便携装置": ["魔导装置"]},
                spells=["治愈术", "屏障"],
                equipment=["钢匕首", "旅行装束"],
            )
        )

        bundle = manager.finalize_campaign_creation(
            shared_goal="揭露辉钢财团抽取灵魂能源的真相。",
            party_notes=["第一幕从下层停电开始。"],
        )

        self.assertEqual(bundle.world_sheet.campaign_title, "永雨之下")
        self.assertEqual(bundle.party_sheet.group_concept, "反抗腐败强权的革命者小队")
        self.assertEqual(bundle.party_sheet.shared_goal, "揭露辉钢财团抽取灵魂能源的真相。")
        self.assertEqual(bundle.party_sheet.members[0].hero_name, "米菈")
        self.assertIn("辉钢财团", bundle.party_sheet.members[0].bonds[0])
        self.assertIn("钢匕首", bundle.party_sheet.members[0].equipment)
        self.assertEqual(bundle.party_sheet.members[0].skills["灵魂魔法"], 2)
        self.assertIs(world_state.party_sheet, bundle.party_sheet)
        self.assertIs(world_state.world_sheet, bundle.world_sheet)
        self.assertTrue(any("闭环完成" in memory for memory in world_state.memories))

    def test_validate_incomplete_hero_draft_reports_missing_details(self) -> None:
        characters = CharacterManager()
        world_state = self.build_world_state()
        world_state.world_profile.hero_drafts["阿凛"] = HeroDraft(
            player_name="阿凛",
            hero_name="米菈",
            identity="逃离财团实验室的魔导技师",
        )
        manager = CharacterCreationManager(characters, world_state)

        validation = manager.validate_hero_draft("阿凛")

        self.assertFalse(validation.ready)
        self.assertIn("主题", validation.missing_fields)
        self.assertIn("故乡", validation.missing_fields)
        self.assertIn("职业分配", validation.missing_fields)
        self.assertTrue(validation.errors)
        self.assertEqual(validation.profile.hero_name, "米菈")

    def test_chimerist_skill_attribute_choices_are_required_and_canonicalized(self) -> None:
        manager = CharacterCreationManager(
            CharacterManager(),
            self.build_world_state(),
        )
        skills = {"拟兽系仪式": 1, "形意咒法": 1}

        with self.assertRaisesRegex(ValueError, "习得时必须选择"):
            manager.validate_skill_options(
                skills,
                {},
                require_complete=True,
            )

        options = manager.validate_skill_options(
            skills,
            {
                "拟兽系仪式": ["MIG+WLP"],
                "形意咒法": ["INS+WLP"],
            },
            require_complete=True,
        )

        self.assertEqual(options["拟兽系仪式"], ["力量+意志"])
        self.assertEqual(options["形意咒法"], ["洞察+意志"])

    def test_validate_complete_draft_still_requires_starting_equipment(self) -> None:
        characters = CharacterManager()
        world_state = self.build_world_state()
        world_state.world_profile.hero_drafts["loading"] = HeroDraft(
            player_name="loading",
            hero_name="艾丽妮",
            identity="被放逐的学徒",
            theme="归属",
            origin="星落尖塔",
            classes={"元素使": 2, "旅人": 1, "博学家": 2},
            attributes={"敏捷": 8, "洞察": 10, "力量": 6, "意志": 8},
            skills={
                "集中心智": 1,
                "知识就是力量": 1,
                "见多识广": 1,
                "元素魔法": 1,
                "元素系仪式": 1,
            },
            spells=["元素武器"],
            confirmed=True,
        )
        manager = CharacterCreationManager(characters, world_state)

        validation = manager.validate_hero_draft("loading")

        self.assertFalse(validation.ready)
        self.assertEqual(validation.missing_fields, ["起始装备"])

    def test_validate_draft_with_classes_but_no_attributes_does_not_leak_key_error(self) -> None:
        characters = CharacterManager()
        world_state = self.build_world_state()
        world_state.world_profile.hero_drafts["阿凛"] = HeroDraft(
            player_name="阿凛",
            hero_name="露米",
            identity="从天文塔逃出来的魔法学徒",
            theme="好奇",
            origin="星灯镇",
            classes={"元素使": 2, "博学家": 2, "旅人": 1},
        )
        manager = CharacterCreationManager(characters, world_state)

        validation = manager.validate_hero_draft("阿凛")

        self.assertFalse(validation.ready)
        self.assertIn("四项属性骰", validation.missing_fields)
        self.assertIn("职业技能", validation.missing_fields)
        self.assertFalse(any(error == "'DEX'" for error in validation.errors))

    def test_validate_draft_reports_missing_spell_choice_for_spell_granting_skill(self) -> None:
        characters = CharacterManager()
        world_state = self.build_world_state()
        world_state.world_profile.hero_drafts["阿凛"] = HeroDraft(
            player_name="阿凛",
            hero_name="露米",
            identity="从天文塔逃出来的魔法学徒",
            theme="希望",
            origin="星灯镇",
            classes={"元素使": 1, "博学家": 2, "旅人": 2},
            attributes={"DEX": 8, "INS": 10, "MIG": 6, "WLP": 8},
            skills={"元素魔法": 1, "灵光洞见": 1, "知识就是力量": 1, "充足补给": 1, "宝物猎人": 1},
            equipment=["法杖", "旅行装束"],
        )
        manager = CharacterCreationManager(characters, world_state)

        validation = manager.validate_hero_draft("阿凛")

        self.assertFalse(validation.ready)
        self.assertIn("元素使法术（还需 1 个）", validation.missing_fields)

    def test_validate_hero_draft_rejects_invalid_starting_attribute_total(self) -> None:
        characters = CharacterManager()
        world_state = self.build_world_state()
        world_state.world_profile.hero_drafts["阿凛"] = HeroDraft(
            player_name="阿凛",
            hero_name="米菈",
            identity="逃离财团实验室的魔导技师",
            theme="自由",
            origin="永雨工业城下层",
            classes={"造物使": 2, "御魂使": 2, "守护者": 1},
            attributes={"DEX": 12, "INS": 10, "MIG": 8, "WLP": 6},
            skills={"便携装置": 1, "秘密配方": 1, "灵魂魔法": 2, "保镖": 1},
            skill_options={"便携装置": ["魔导装置"]},
        )
        manager = CharacterCreationManager(characters, world_state)

        validation = manager.validate_hero_draft("阿凛")

        self.assertFalse(validation.ready)
        self.assertTrue(
            any("起始属性必须采用规则书组合" in error for error in validation.errors)
        )

    def test_create_player_character_from_confirmed_hero_draft(self) -> None:
        characters = CharacterManager()
        world_state = self.build_world_state()
        rules = RulesEngine()
        rules._rng = FakeRandom([4, 4])
        world_state.world_profile.hero_drafts["阿凛"] = HeroDraft(
            player_name="阿凛",
            hero_name="米菈",
            identity="逃离财团实验室的魔导技师",
            theme="自由",
            origin="永雨工业城下层",
            classes={"造物使": 2, "御魂使": 2, "守护者": 1},
            attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
            bonds=["辉钢财团：不信任、仇恨"],
            skills={"便携装置": 1, "秘密配方": 1, "灵魂魔法": 2, "保镖": 1},
            skill_options={"便携装置": ["魔导装置"]},
            spells=["治愈", "护盾"],
            equipment=["钢匕首", "旅行装束"],
            notes=["她知道辉钢财团的地下能源管线。"],
            confirmed=True,
        )
        manager = CharacterCreationManager(characters, world_state, rules_engine=rules)

        validation = manager.validate_hero_draft("阿凛")
        result = manager.create_player_character_from_draft("阿凛")

        self.assertTrue(validation.ready)
        self.assertEqual(result.character.name, "米菈")
        self.assertEqual(result.character.bonds[0].target, "辉钢财团")
        self.assertEqual(result.character.bonds[0].emotions, ["猜忌", "憎恨"])
        self.assertEqual(result.fate_roll, (4, 4))
        self.assertEqual(characters.get("米菈").classes["造物使"], 2)

    def test_reconcile_legacy_bond_object_string(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="艾丽妮",
                attributes={"DEX": 6, "INS": 10, "MIG": 6, "WLP": 10},
                max_hp=35,
                hp=35,
                max_mp=55,
                mp=55,
                traits=["pc"],
                bonds=[Bond(target="{'target'", emotions=["猜忌"])],
            )
        )
        world_state = self.build_world_state()
        world_state.world_profile.hero_drafts["loading"] = HeroDraft(
            player_name="loading",
            hero_name="艾丽妮",
            bonds=["{'target': '诺艾尔', 'emotions': ['不信任'], 'intensity': 1}"],
        )
        manager = CharacterCreationManager(characters, world_state)

        repaired = manager.reconcile_legacy_bonds()

        self.assertEqual(repaired, ["艾丽妮 的旧格式羁绊已规范化。"])
        self.assertEqual(characters.get("艾丽妮").bonds[0].target, "诺艾尔")
        self.assertEqual(characters.get("艾丽妮").bonds[0].emotions, ["猜忌"])

    def test_unconfirmed_hero_draft_cannot_be_created_until_confirmed(self) -> None:
        characters = CharacterManager()
        world_state = self.build_world_state()
        world_state.world_profile.hero_drafts["阿凛"] = HeroDraft(
            player_name="阿凛",
            hero_name="米菈",
            identity="逃离财团实验室的魔导技师",
            theme="自由",
            origin="永雨工业城下层",
            classes={"造物使": 2, "御魂使": 2, "守护者": 1},
            attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
            skills={"便携装置": 1, "秘密配方": 1, "灵魂魔法": 2, "保镖": 1},
            skill_options={"便携装置": ["魔导装置"]},
            spells=["治愈术", "灵魂之幕"],
            equipment=["钢匕首"],
        )
        manager = CharacterCreationManager(characters, world_state)

        with self.assertRaisesRegex(ValueError, "尚未确认"):
            manager.create_player_character_from_draft("阿凛")

        validation = manager.confirm_hero_draft("阿凛")

        self.assertTrue(validation.ready)
        self.assertTrue(world_state.world_profile.hero_drafts["阿凛"].confirmed)

    def test_orchestrator_exposes_creation_closure(self) -> None:
        characters = CharacterManager()
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        world_state = self.build_world_state()
        rules = RulesEngine(seed=0)
        app = SceneOrchestrator(
            character_manager=characters,
            clock_manager=clocks,
            conflict_manager=conflict,
            world_state=world_state,
            interceptor=ActionInterceptor(rules, characters, clocks, conflict, world_state),
            expressor=Expressor(),
            scene_manager=SceneManager(),
        )

        suggestions = app.suggest_hero_angles()
        result = app.create_player_character(
            HeroCreationProfile(
                player_name="阿凛",
                hero_name="米菈",
                identity="逃离财团实验室的魔导技师",
                theme="自由",
                origin="永雨工业城下层",
                classes={"tinkerer": 2, "spiritist": 2, "guardian": 1},
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                skills={"便携装置": 1, "秘密配方": 1, "灵魂魔法": 2, "保镖": 1},
                skill_options={"便携装置": ["魔导装置"]},
                spells=["治愈术", "屏障"],
                equipment=["钢匕首"],
            )
        )
        bundle = app.finalize_campaign_creation(shared_goal="让下层重见天空。")

        self.assertTrue(any("财阀" in suggestion for suggestion in suggestions))
        self.assertEqual(result.character.classes["造物使"], 2)
        self.assertEqual(bundle.party_sheet.members[0].hero_name, "米菈")

    def test_orchestrator_creates_confirmed_drafts_and_finalizes_bundle(self) -> None:
        characters = CharacterManager()
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        world_state = self.build_world_state()
        world_state.world_profile.hero_drafts["阿凛"] = HeroDraft(
            player_name="阿凛",
            hero_name="米菈",
            identity="逃离财团实验室的魔导技师",
            theme="自由",
            origin="永雨工业城下层",
            classes={"造物使": 2, "御魂使": 2, "守护者": 1},
            attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
            skills={"便携装置": 1, "秘密配方": 1, "灵魂魔法": 2, "保镖": 1},
            skill_options={"便携装置": ["魔导装置"]},
            spells=["治愈术", "灵魂之幕"],
            equipment=["钢匕首"],
            confirmed=True,
        )
        rules = RulesEngine(seed=0)
        app = SceneOrchestrator(
            character_manager=characters,
            clock_manager=clocks,
            conflict_manager=conflict,
            world_state=world_state,
            interceptor=ActionInterceptor(rules, characters, clocks, conflict, world_state),
            expressor=Expressor(),
            scene_manager=SceneManager(),
        )

        validation = app.validate_hero_draft("阿凛")
        results = app.create_confirmed_player_characters_from_drafts()
        bundle = app.finalize_campaign_creation(shared_goal="让下层重见天空。")

        self.assertTrue(validation.ready)
        self.assertIn("阿凛", results)
        self.assertEqual(results["阿凛"].character.name, "米菈")
        self.assertEqual(bundle.party_sheet.members[0].hero_name, "米菈")




if __name__ == "__main__":
    unittest.main()
