import tempfile
import unittest
from pathlib import Path

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
from fu_gm.models import Bond, HeroCreationProfile, SessionZeroResponse, SessionZeroStage
from fu_gm.scene_orchestrator import SceneOrchestrator


class FakeRandom:
    def __init__(self, values):
        self.values = list(values)

    def randint(self, low, high):
        value = self.values.pop(0)
        if value < low or value > high:
            raise AssertionError(f"掷骰值 {value} 超出范围 {low}-{high}")
        return value


def build_world_state() -> WorldState:
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
                "pillars": {
                    "危险中的世界": "辉钢财团的灵魂能源网络正在吞掉下层街区。",
                    "魔法和技术": "魔导机械以灵魂流为燃料，奇迹和剥削只有一墙之隔。",
                },
                "core_themes": ["剥削与反抗", "失去与重生"],
                "group_concept": "反抗腐败强权的革命者小队",
                "starting_region": "永雨工业城下层",
                "major_locations": {"永雨工业城": "上层偷走阳光，下层承受魔导烟雨。"},
                "factions": {"辉钢财团": "垄断灵魂能源的企业贵族。"},
                "villain_seeds": ["辉钢财团继承人把剥削包装成奇迹。"],
                "mysteries": ["被抽取的灵魂能源最终流向了哪里？"],
                "safety_lines": ["不详细描写血腥折磨。"],
                "safety_veils": ["儿童遇险淡出处理。"],
                "completed": True,
            },
        )
    )
    return world_state


def build_campaign_bundle():
    characters = CharacterManager()
    world_state = build_world_state()
    rules = RulesEngine()
    rules._rng = FakeRandom([3, 5])
    manager = CharacterCreationManager(characters, world_state, rules_engine=rules)
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
            spells=["治愈", "护盾"],
            equipment=["钢匕首", "符文盾", "旅行装束"],
        )
    )
    return manager.finalize_campaign_creation(
        shared_goal="揭露辉钢财团抽取灵魂能源的真相。",
        party_notes=["第一幕从下层停电开始。"],
    )


class SheetExporterTests(unittest.TestCase):
    def test_export_campaign_markdown_and_json_payload(self) -> None:
        bundle = build_campaign_bundle()
        export = SheetExporter().export_campaign(bundle)

        self.assertIn("# 世界表：永雨之下", export.world_markdown)
        self.assertIn("辉钢财团", export.world_markdown)
        self.assertIn("# 小队表", export.party_markdown)
        self.assertIn("革命者小队", export.party_markdown)
        self.assertIn("米菈", export.character_markdowns)
        self.assertIn("# 角色表：米菈", export.character_markdowns["米菈"])
        self.assertIn("DEF：11", export.character_markdowns["米菈"])
        self.assertIn("灵魂魔法 2", export.character_markdowns["米菈"])
        self.assertEqual(export.json_payload["world_sheet"]["campaign_title"], "永雨之下")
        self.assertEqual(export.json_payload["party_sheet"]["members"][0]["hero_name"], "米菈")
        self.assertEqual(export.json_payload["characters"][0]["zenit"], 180)

    def test_write_campaign_exports_creates_local_files(self) -> None:
        bundle = build_campaign_bundle()
        with tempfile.TemporaryDirectory() as temp_dir:
            export = SheetExporter().write_campaign_exports(bundle, temp_dir)
            written = export.written_files

            self.assertTrue(Path(written["world_markdown"]).exists())
            self.assertTrue(Path(written["party_markdown"]).exists())
            self.assertTrue(Path(written["json"]).exists())
            self.assertTrue(Path(written["character:米菈"]).exists())
            self.assertIn("角色表：米菈", Path(written["character:米菈"]).read_text(encoding="utf-8"))

    def test_orchestrator_exposes_sheet_exports(self) -> None:
        characters = CharacterManager()
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        world_state = build_world_state()
        rules = RulesEngine()
        rules._rng = FakeRandom([3, 5])
        app = SceneOrchestrator(
            character_manager=characters,
            clock_manager=clocks,
            conflict_manager=conflict,
            world_state=world_state,
            interceptor=ActionInterceptor(rules, characters, clocks, conflict, world_state),
            expressor=Expressor(),
            scene_manager=SceneManager(),
            character_creation_manager=CharacterCreationManager(characters, world_state, rules_engine=rules),
        )
        app.create_player_character(
            HeroCreationProfile(
                player_name="阿凛",
                hero_name="米菈",
                identity="逃离财团实验室的魔导技师",
                theme="自由",
                origin="永雨工业城下层",
                classes={"造物使": 2, "御魂使": 2, "守护者": 1},
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                bonds=[Bond(target="辉钢财团", emotions=["不信任"])],
                skills={"便携装置": 1, "秘密配方": 1, "灵魂魔法": 2, "保镖": 1},
                skill_options={"便携装置": ["魔导装置"]},
                spells=["治愈术", "屏障"],
                equipment=["钢匕首"],
            )
        )
        app.finalize_campaign_creation(shared_goal="让下层重见天空。")

        export = app.export_campaign_sheets()

        self.assertIn("让下层重见天空", export.party_markdown)
        self.assertIn("角色表：米菈", export.character_markdowns["米菈"])


if __name__ == "__main__":
    unittest.main()
