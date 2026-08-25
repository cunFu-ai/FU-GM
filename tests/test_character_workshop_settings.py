from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fu_gm.character_workshop_settings import CharacterWorkshopSettings


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class CharacterWorkshopSettingsTests(unittest.TestCase):
    def _workflow_root(self, root: Path) -> Path:
        workflows = root / "workflows"
        workflows.mkdir()
        (workflows / "anima-api.json").write_text("{}", encoding="utf-8")
        (workflows / "krea-lora-api.json").write_text("{}", encoding="utf-8")
        return workflows

    def test_non_secret_settings_persist_but_api_key_is_memory_only(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            workflows = self._workflow_root(root)
            settings = CharacterWorkshopSettings(root / "data", workflow_root=workflows)

            updated = settings.update(
                {
                    "comfyui": {"port": 8288},
                    "llm": {
                        "api_base_url": "https://llm.example/v1",
                        "model": "portrait-model",
                        "api_key": "secret-key-must-not-be-written",
                    },
                }
            )

            self.assertTrue(updated["llm"]["api_key_configured"])
            saved = (root / "data" / "settings.json").read_text(encoding="utf-8")
            self.assertNotIn("secret-key-must-not-be-written", saved)
            self.assertNotIn("api_key", saved)

            restarted = CharacterWorkshopSettings(root / "data", workflow_root=workflows)
            payload = restarted.public_payload()
            self.assertEqual(payload["comfyui"]["port"], 8288)
            self.assertEqual(payload["llm"]["model"], "portrait-model")
            self.assertFalse(payload["llm"]["api_key_configured"])

    def test_comfyui_config_is_local_and_uses_bundled_workflows(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            workflows = self._workflow_root(root)
            settings = CharacterWorkshopSettings(root / "data", workflow_root=workflows)
            settings.update({"comfyui": {"port": 9191}})

            config = settings.comfyui_config()

            self.assertEqual(config.base_url, "http://127.0.0.1:9191")
            self.assertFalse(config.allow_remote)
            self.assertEqual(
                Path(config.anima_workflow),
                workflows.resolve() / "anima-api.json",
            )
            self.assertTrue(config.usable("anima"))
            self.assertFalse(config.usable("krea2"))
            self.assertTrue(config.usable("krea_lora"))

    def test_invalid_port_and_llm_url_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            settings = CharacterWorkshopSettings(tempdir)
            with self.assertRaisesRegex(ValueError, "1 到 65535"):
                settings.update({"comfyui": {"port": 70000}})
            with self.assertRaisesRegex(ValueError, "http"):
                settings.update({"llm": {"api_base_url": "file:///secret"}})

    def test_comfyui_connection_test_reports_version_without_exposing_raw_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            settings = CharacterWorkshopSettings(tempdir)
            response = _FakeResponse(
                {
                    "system": {"comfyui_version": "0.27.1"},
                    "devices": [{"name": "Local GPU"}],
                }
            )
            with patch(
                "fu_gm.character_workshop_settings.request.urlopen",
                return_value=response,
            ) as urlopen:
                result = settings.test_comfyui()

            self.assertTrue(result["ok"])
            self.assertEqual(result["version"], "0.27.1")
            self.assertEqual(result["device"], "Local GPU")
            self.assertEqual(urlopen.call_args.args[0].full_url, "http://127.0.0.1:8188/system_stats")


if __name__ == "__main__":
    unittest.main()
