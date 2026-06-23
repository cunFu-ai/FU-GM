import json
import unittest
from collections import Counter
from pathlib import Path

from fu_gm.prepared_locations import EXPANSION_LOCATION_SEEDS, PREPARED_LOCATION_SEEDS


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ICON_ROOT = PROJECT_ROOT / "assets" / "nortantis_custom" / "world_wonders"


class PreparedLocationIconTests(unittest.TestCase):
    def test_every_prepared_location_has_a_candidate_icon(self) -> None:
        icons_by_name: dict[str, tuple[dict[str, object], Path]] = {}

        for catalog_path in ICON_ROOT.glob("*/catalog.json"):
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            self.assertTrue(catalog["enabled"], catalog_path)
            self.assertEqual("nortantis_custom_images", catalog["runtime_registration"])
            for icon in catalog["icons"]:
                name = str(icon["name_zh"])
                self.assertNotIn(name, icons_by_name, f"duplicate icon name: {name}")
                icons_by_name[name] = (icon, catalog_path.parent)

        self.assertTrue(all(seed.icon_name for seed in PREPARED_LOCATION_SEEDS))
        prepared_names = {seed.icon_name for seed in PREPARED_LOCATION_SEEDS}
        duplicated_icon_names = {
            name for name, count in Counter(seed.icon_name for seed in PREPARED_LOCATION_SEEDS).items() if count > 1
        }
        self.assertEqual(set(), duplicated_icon_names)
        self.assertEqual(set(), prepared_names - icons_by_name.keys())

        for name in prepared_names:
            icon, folder = icons_by_name[name]
            self.assertTrue((folder / str(icon["file"])).is_file(), name)

    def test_prepared_expansion_icons_match_expansion_library_one_to_one(self) -> None:
        catalog_path = ICON_ROOT / "prepared_expansions" / "catalog.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        icon_names = {str(icon["name_zh"]) for icon in catalog["icons"]}
        seed_names = {seed.icon_name for seed in EXPANSION_LOCATION_SEEDS}

        self.assertEqual(seed_names, icon_names)

    def test_deprecated_duplicate_icons_are_not_enabled(self) -> None:
        deprecated = {"边境起始王国", "第七采掘城", "灵魂网络中枢"}
        enabled_names: set[str] = set()

        for catalog_path in ICON_ROOT.glob("*/catalog.json"):
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            if catalog.get("enabled") is not True:
                continue
            enabled_names.update(str(icon["name_zh"]) for icon in catalog["icons"])

        self.assertFalse(deprecated & enabled_names)


if __name__ == "__main__":
    unittest.main()
