from __future__ import annotations

from pathlib import Path
import re
import unittest


class AstrBotPluginMetadataTests(unittest.TestCase):
    def test_metadata_version_matches_registered_plugin_version(self) -> None:
        plugin_dir = (
            Path(__file__).resolve().parents[1]
            / "integrations"
            / "astrbot"
            / "fu_gm_bridge"
        )
        metadata = (plugin_dir / "metadata.yaml").read_text(encoding="utf-8")
        main_source = (plugin_dir / "main.py").read_text(encoding="utf-8")

        metadata_match = re.search(r"(?m)^version:\s*([^\s]+)\s*$", metadata)
        register_match = re.search(
            r'@register\(\s*"fu_gm_bridge"\s*,\s*"cunfu"\s*,\s*"[^"]*"\s*,\s*"([^"]+)"\s*\)',
            main_source,
        )

        self.assertIsNotNone(metadata_match)
        self.assertIsNotNone(register_match)
        self.assertEqual(metadata_match.group(1), register_match.group(1))


if __name__ == "__main__":
    unittest.main()
