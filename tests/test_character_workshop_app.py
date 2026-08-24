from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fu_gm.character_workshop_app import (
    CharacterWorkshopService,
    prepare_portable_environment,
)


class CharacterWorkshopAppTests(unittest.TestCase):
    def test_portable_environment_clears_inherited_credentials_but_enables_local_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ",
            {
                "FU_GM_API_KEY": "must-not-survive",
                "FU_GM_MODEL_SAMPLE_API_KEY": "must-not-survive",
            },
            clear=False,
        ):
            prepare_portable_environment(Path(tempdir))
            self.assertEqual("portable", os.environ["FU_GM_DISTRIBUTION_MODE"])
            self.assertEqual("1", os.environ["FU_GM_PORTRAIT_FEATURE_ENABLED"])
            self.assertEqual("1", os.environ["FU_GM_COMFYUI_ENABLED"])
            self.assertNotIn("FU_GM_API_KEY", os.environ)
            self.assertNotIn("FU_GM_MODEL_SAMPLE_API_KEY", os.environ)

    def test_portable_service_exposes_only_character_workshop_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ",
            {
                "FU_GM_DISTRIBUTION_MODE": "portable",
                "FU_GM_PORTRAIT_FEATURE_ENABLED": "1",
            },
        ):
            service = CharacterWorkshopService(data_root=tempdir, use_llm=False)
            root_status, root = service.handle("GET", "/")
            health_status, health = service.handle("GET", "/health")
            settings_status, settings = service.handle(
                "GET", "/v1/workshop/settings"
            )
            portrait_status, portrait = service.handle("POST", "/v1/portraits/generate", {})
            gm_status, gm = service.handle("POST", "/v1/chat", {})

        self.assertEqual(root_status, 200)
        page = root.body.decode("utf-8")
        self.assertIn("最终物语角色工房", page)
        self.assertIn("Fabula Ultima", page)
        self.assertIn("最终物语中的英雄", page)
        self.assertNotIn('id="campaignId"', page)
        self.assertEqual(health_status, 200)
        self.assertEqual(health["service"], "fu-character-workshop")
        self.assertEqual(health["storage"], "standalone_roster")
        self.assertTrue(health["portrait_generation"])
        self.assertEqual(settings_status, 200)
        self.assertEqual(settings["comfyui"]["host"], "127.0.0.1")
        self.assertEqual(settings["comfyui"]["port"], 8188)
        self.assertFalse(settings["llm"]["api_key_configured"])
        self.assertFalse(hasattr(service, "runtimes"))
        self.assertEqual(portrait_status, 422)
        self.assertEqual(gm_status, 404)
        self.assertFalse(portrait["ok"])
        self.assertFalse(gm["ok"])


if __name__ == "__main__":
    unittest.main()
