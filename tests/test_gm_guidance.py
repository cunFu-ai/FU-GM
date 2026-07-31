import unittest

from fu_gm.gm_guidance import build_gm_guidance, infer_inspiration_tags, question_hint_for_step
from fu_gm.models import WorldCreationProfile


class GMGuidanceTests(unittest.TestCase):
    def test_infers_guidance_from_world_content_without_player_selecting_expansion(self) -> None:
        world = WorldCreationProfile(
            world_style="反抗财阀的群像冒险",
            magic_tech_role="公司垄断灵魂能源，上层城市享受阳光，下层街区承受污染。",
            starting_region="永雨工业城",
            major_locations={"下层街区": "工厂烟雨里有被遗忘的居民和旧神庙。"},
        )

        tags = infer_inspiration_tags(world)

        self.assertIn("techno_pressure", tags)
        self.assertIn("dungeon_mystery", tags)
        self.assertNotIn("techno_fantasy_selected", tags)

    def test_builds_prepared_locations_and_story_beats_for_inferred_spirit(self) -> None:
        world = WorldCreationProfile(
            magic_tech_role="魔导工厂从森林根系中抽取灵魂能源，村庄正在失衡。",
            major_locations={"风铃村": "林边村庄，水车开始倒转。"},
            mysteries=["被抽走的灵魂能源最终流向了哪里？"],
        )

        guidance = build_gm_guidance(world)
        location_names = {seed.name for seed in guidance.location_seeds}

        self.assertIn("techno_pressure", guidance.inspiration_tags)
        self.assertIn("natural_home", guidance.inspiration_tags)
        self.assertTrue({"企业星城", "风铃村", "灵魂中枢"} & location_names)
        self.assertTrue(any("真实代价" in beat or "失衡" in beat for beat in guidance.story_beats))
        self.assertTrue(any("不要求玩家选择" in item for item in guidance.tone_guidance))
        self.assertTrue(any("地点" in item and "秘密" in item for item in guidance.location_guidance))
        self.assertTrue(any("身份" in item and "主题" in item for item in guidance.character_guidance))
        self.assertTrue(any("场景" in item and "可互动" in item for item in guidance.scene_framework))
        self.assertTrue(any("NPC" in item and "功能位" in item for item in guidance.npc_guidance))
        self.assertTrue(any("事情已经在发生" in item for item in guidance.opening_moves))

    def test_summary_exposes_world_type_location_character_and_opening_guidance(self) -> None:
        from fu_gm.gm_guidance import summarize_guidance_for_prompt

        world = WorldCreationProfile(
            world_style="自然奇幻与海上群岛",
            magic_tech_role="潮汐与灵魂之河互相牵引，港口依靠古老灯塔导航。",
            starting_region="镜线内海",
        )

        payload = summarize_guidance_for_prompt(world)

        self.assertIn("tone_guidance", payload)
        self.assertIn("location_guidance", payload)
        self.assertIn("character_guidance", payload)
        self.assertIn("scene_framework", payload)
        self.assertIn("npc_guidance", payload)
        self.assertIn("opening_moves", payload)
        self.assertTrue(any("海" in item or "港" in item for item in payload["tone_guidance"] + payload["location_guidance"]))

    def test_question_hints_are_step_specific(self) -> None:
        world = WorldCreationProfile(
            magic_tech_role="企业控制交通和媒体，把灵魂能源包装成救世奇迹。",
        )

        hint = question_hint_for_step(world, "threat")

        self.assertIn("系统", hint)
        self.assertIn("坏人", hint)


if __name__ == "__main__":
    unittest.main()
