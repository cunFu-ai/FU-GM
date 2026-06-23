import unittest

from fu_gm.prepared_locations import (
    EPIC_LOCATION_SEEDS,
    EXPANSION_LOCATION_SEEDS,
    NATURAL_LOCATION_SEEDS,
    PREPARED_LOCATION_ALIASES,
    PREPARED_LOCATION_SEEDS,
    TECHNO_LOCATION_SEEDS,
    prepared_location_by_name,
)


class PreparedLocationLibraryTests(unittest.TestCase):
    def test_each_expansion_contributes_all_ten_example_locations(self) -> None:
        expected = {
            "epic": {
                "奥涅里亚", "猎神之森", "幻境城", "尤克特拉希尔", "特提斯海",
                "恩迪尔", "边界地", "阿尔坎蒂斯", "撒拉菲姆", "震世尖塔",
            },
            "natural": {
                "獾之神庙", "微风村", "烛火湾", "蔚蓝丛林", "暗渊",
                "艾森斯塔特", "黄金城", "落潮镇", "岩石荒漠", "眩晕峰",
            },
            "techno": {
                "阿加塞恩", "轴心城", "本瑟姆综合楼", "圣恩号", "七号采掘器",
                "魔鬼螺号", "尼达维利尔", "北斗星", "灵魂中枢", "弗拉加拉斯",
            },
        }

        self.assertEqual(expected["epic"], {seed.name for seed in EPIC_LOCATION_SEEDS})
        self.assertEqual(expected["natural"], {seed.name for seed in NATURAL_LOCATION_SEEDS})
        self.assertEqual(expected["techno"], {seed.name for seed in TECHNO_LOCATION_SEEDS})
        self.assertEqual(30, len(EXPANSION_LOCATION_SEEDS))
        self.assertEqual(37, len(PREPARED_LOCATION_SEEDS))
        self.assertEqual(37, len({seed.name for seed in PREPARED_LOCATION_SEEDS}))
        self.assertNotIn("边境起始王国", {seed.name for seed in PREPARED_LOCATION_SEEDS})
        self.assertNotIn("第七采掘城", {seed.name for seed in PREPARED_LOCATION_SEEDS})
        self.assertNotIn("灵魂网络中枢", {seed.name for seed in PREPARED_LOCATION_SEEDS})

    def test_every_expansion_location_has_complete_gm_material(self) -> None:
        for seed in EXPANSION_LOCATION_SEEDS:
            with self.subTest(location=seed.name):
                self.assertTrue(seed.source_book.startswith("《最终物语："))
                self.assertTrue(seed.brief)
                self.assertTrue(seed.use_when)
                self.assertTrue(seed.keywords)
                self.assertTrue(seed.terrain)
                self.assertTrue(seed.travel_dice)
                self.assertTrue(seed.common_elements)
                self.assertTrue(seed.dangers)
                self.assertTrue(seed.discoveries)
                self.assertTrue(seed.themes)
                self.assertTrue(seed.typical_features)
                self.assertTrue(seed.campaign_position)
                self.assertTrue(seed.villain_plans)
                self.assertGreaterEqual(len(seed.questions), 5)
                self.assertEqual(3, len(seed.story_hooks))
                self.assertEqual(tuple(hook.title for hook in seed.story_hooks), seed.hooks)
                self.assertTrue(all(hook.summary and hook.beats for hook in seed.story_hooks))
                self.assertTrue(seed.icon_name)
                self.assertEqual(seed.name, seed.icon_name)

    def test_detailed_prompt_payload_keeps_location_guidance(self) -> None:
        seed = prepared_location_by_name("尼达维利尔")

        self.assertIsNotNone(seed)
        payload = seed.prompt_payload(detailed=True)

        self.assertEqual("军用实验室", payload["archetype"])
        self.assertEqual(3, len(payload["story_hooks"]))
        self.assertTrue(payload["campaign_position"])
        self.assertTrue(payload["villain_plans"])

    def test_deprecated_core_names_resolve_to_canonical_expansion_locations(self) -> None:
        self.assertEqual("奥涅里亚", PREPARED_LOCATION_ALIASES["边境起始王国"])
        self.assertEqual("七号采掘器", PREPARED_LOCATION_ALIASES["第七采掘城"])
        self.assertEqual("灵魂中枢", PREPARED_LOCATION_ALIASES["灵魂网络中枢"])
        self.assertEqual("奥涅里亚", prepared_location_by_name("边境起始王国").name)
        self.assertEqual("七号采掘器", prepared_location_by_name("第七采掘城").name)
        self.assertEqual("灵魂中枢", prepared_location_by_name("灵魂网络中枢").name)


if __name__ == "__main__":
    unittest.main()
